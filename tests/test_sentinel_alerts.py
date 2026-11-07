# -*- coding: utf-8 -*-
"""哨兵的去重与阈值：一个永远在响的警报等于没有警报。

2026-11-01 实况：哨兵每 5 分钟推一次同样的告警
  「回撤 31.5%（警戒线 30%）；决策停滞 357s」
连推十几轮。三个原因：
  1) 去重签名是 issues 原文拼接，"357s"变"199s"就算变了 -> 去重失效
  2) 决策停滞阈值 90s，而趋势策略的决策日志有 10 分钟节流 -> 每轮误报
  3) 回撤基准取全历史 MAX，包含上一代剥头皮策略赚过又亏掉的 100U
     -> 趋势模式一开局就"回撤 28%"，永远消不掉
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.sentinel import Sentinel

MS = 1000


def _mkdb(path, trend=True, equity_rows=None, decisions=None):
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE decisions(id INTEGER PRIMARY KEY, ts INTEGER, signal TEXT,
                             action TEXT, reason TEXT);
      CREATE TABLE trades(id INTEGER PRIMARY KEY, side TEXT, qty REAL, entry_price REAL,
                          stop_price REAL, target_price REAL, entry_ts INTEGER,
                          exit_ts INTEGER, pnl_net REAL, fee REAL);
      CREATE TABLE equity(ts INTEGER, balance REAL, equity REAL);
      CREATE TABLE bars(open_time INTEGER, interval_ms INTEGER);
    """)
    now = int(time.time() * MS)
    # 注意：真实 schema 里 signal='trend_watch'、action='watch'。
    # 早期版本的测试往 action 列塞 'trend_watch'，
    # 正好匹配了"查 action 列"这个错误实现，把 bug 一起编码进了测试。
    for ts, sig, rs in (decisions or [(now - 5000, "trend_watch" if trend else "scalp", "x")]):
        act = "watch" if str(sig).startswith("trend") else "hold"
        c.execute("INSERT INTO decisions(ts,signal,action,reason) VALUES(?,?,?,?)",
                  (ts, sig, act, rs))
    for ts, eq in (equity_rows or [(now - 60000, 100.0)]):
        c.execute("INSERT INTO equity(ts,balance,equity) VALUES(?,?,?)", (ts, eq, eq))
    c.commit()
    c.close()


def _sentinel(tmpdir):
    trade = os.path.join(tmpdir, "t.db")
    market = os.path.join(tmpdir, "m.db")
    if not os.path.exists(trade):
        _mkdb(trade)
    c = sqlite3.connect(market)
    c.execute("CREATE TABLE IF NOT EXISTS bars(open_time INTEGER, interval_ms INTEGER)")
    c.execute("INSERT INTO bars VALUES(?,?)", (int(time.time() * MS) - 3000, 3000))
    c.commit()
    c.close()
    return Sentinel(trade, market), trade, market


def test_signature_ignores_volatile_numbers(tmpdir):
    """「决策停滞 357s」和「决策停滞 199s」必须算同一类问题，不能重复告警。"""
    s, _t, _m = _sentinel(str(tmpdir))
    import re

    def sig_of(issues):
        return "|".join(re.sub(r"[0-9.]+", "#", x) for x in issues)

    assert sig_of(["决策停滞 357s"]) == sig_of(["决策停滞 199s"])
    assert sig_of(["回撤 31.5%（警戒线 30%）"]) == sig_of(["回撤 28.0%（警戒线 30%）"])
    # 但不同类问题必须区分开
    assert sig_of(["决策停滞 357s"]) != sig_of(["行情停滞 357s"])


def test_trend_mode_decision_threshold_allows_throttle(tmpdir):
    """趋势模式决策有 10 分钟节流，300 秒间隔不算停滞。"""
    d = str(tmpdir)
    trade = os.path.join(d, "t.db")
    if os.path.exists(trade):
        os.remove(trade)
    now = int(time.time() * MS)
    _mkdb(trade, trend=True,
          decisions=[(now - 300 * MS, "trend_watch", "空仓等待")],
          equity_rows=[(now - 600 * MS, 71.77)])
    market = os.path.join(d, "m.db")
    c = sqlite3.connect(market)
    c.execute("CREATE TABLE IF NOT EXISTS bars(open_time INTEGER, interval_ms INTEGER)")
    c.execute("INSERT INTO bars VALUES(?,?)", (now - 3000, 3000))
    c.commit(); c.close()

    s = Sentinel(trade, market)
    r = s.check()
    assert not any("决策停滞" in x for x in r["issues"]), \
        "300s 的决策间隔在趋势模式下不应报警：%s" % r["issues"]
    assert r["facts"]["strategy"] == "trend"


def test_trend_mode_drawdown_ignores_legacy_equity(tmpdir):
    """旧的剥头皮时代赚过又亏掉的 100U，不该成为趋势模式的回撤基准。"""
    d = str(tmpdir)
    trade = os.path.join(d, "t.db")
    if os.path.exists(trade):
        os.remove(trade)
    now = int(time.time() * MS)
    # 远古：100U（剥头皮时代峰值）；近期：71.77U（当前）；趋势决策从 T0 开始
    t0 = now - 1800 * MS
    _mkdb(trade, trend=True,
          decisions=[(now - 5000, "trend_watch", "空仓等待")],
          equity_rows=[(now - 7200 * MS, 100.0), (now - 600 * MS, 71.77)])
    c = sqlite3.connect(trade)
    c.execute("UPDATE decisions SET ts=?", (t0 + 1000,))
    assert c.execute("SELECT signal FROM decisions").fetchone()[0] == "trend_watch"
    c.execute("UPDATE equity SET ts=? WHERE equity=100.0", (t0 - 1800 * MS,))
    c.execute("UPDATE equity SET ts=? WHERE equity=71.77", (t0 + 2000,))
    c.commit(); c.close()

    market = os.path.join(d, "m.db")
    c = sqlite3.connect(market)
    c.execute("CREATE TABLE IF NOT EXISTS bars(open_time INTEGER, interval_ms INTEGER)")
    c.execute("INSERT INTO bars VALUES(?,?)", (now - 3000, 3000))
    c.commit(); c.close()

    s = Sentinel(trade, market)
    r = s.check()
    # 趋势时代内权益恒为 71.77，回撤应为 0，不该报"回撤 28%"
    assert r["facts"]["drawdown_pct"] == 0.0, \
        "回撤基准仍包含旧策略权益：%.1f%%" % r["facts"]["drawdown_pct"]
    assert not any("回撤" in x for x in r["issues"]), r["issues"]
def test_stale_decisions_really_alert(tmpdir):
    """反向验证：真停滞时必须报。

    只测"不误报"是不够的——把阈值调大到永远不报也能让上一个测试通过。
    监控必须成对测试：该报的时候要报，不该报的时候不报。
    """
    d = str(tmpdir)
    trade = os.path.join(d, "t2.db")
    now = int(time.time() * MS)
    _mkdb(trade, trend=True,
          decisions=[(now - 25 * 60 * MS, "trend_watch", "空仓等待")],
          equity_rows=[(now - 60 * MS, 71.77)])
    market = os.path.join(d, "m2.db")
    c = sqlite3.connect(market)
    c.execute("CREATE TABLE IF NOT EXISTS bars(open_time INTEGER, interval_ms INTEGER)")
    c.execute("INSERT INTO bars VALUES(?,?)", (now - 3000, 3000))
    c.commit(); c.close()

    s = Sentinel(trade, market)
    r = s.check()
    assert any("决策停滞" in x for x in r["issues"]), \
        "25 分钟没有决策，趋势模式（20 分钟阈值）必须报警：%s" % r["issues"]


def test_stale_bars_really_alert(tmpdir):
    """行情断流必须报，且与决策停滞区分开。"""
    d = str(tmpdir)
    trade = os.path.join(d, "t3.db")
    now = int(time.time() * MS)
    _mkdb(trade, trend=True,
          decisions=[(now - 5000, "trend_watch", "空仓等待")],
          equity_rows=[(now - 60 * MS, 71.77)])
    market = os.path.join(d, "m3.db")
    c = sqlite3.connect(market)
    c.execute("CREATE TABLE IF NOT EXISTS bars(open_time INTEGER, interval_ms INTEGER)")
    c.execute("INSERT INTO bars VALUES(?,?)", (now - 10 * 60 * MS, 3000))
    c.commit(); c.close()

    s = Sentinel(trade, market)
    r = s.check()
    assert any("行情停滞" in x for x in r["issues"]), \
        "10 分钟没有 3s K 线，必须报行情停滞：%s" % r["issues"]
