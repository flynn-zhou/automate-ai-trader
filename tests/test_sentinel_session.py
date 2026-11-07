# -*- coding: utf-8 -*-
"""哨兵的「本次运行累计」必须按当前策略算，不能把旧策略的亏损算进来。

实测到的矛盾输出（生产环境真实生成）：

    权益 71.77U，本次运行累计 -28.23U（-28.23%），距峰值回撤 0.0%
    当前空仓
    巡检无异常。
    判断：本次运行已亏损超过 25%，建议立刻停手复盘（可 systemctl stop ai-trader）。

同一份报告既说「巡检无异常、回撤 0.0%」，又建议停手复盘。
而那 -28.23% 全部来自【已废弃的剥头皮策略】。

这和第 41 轮（大屏战绩属于废弃策略）、第 44 轮（Guard 峰值不持久）
是同一类病：**基准属于另一个策略**。
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.sentinel import Sentinel
from ai_trader.trade_db import TradeDB


def _mk(equity_points, decisions):
    """建两个临时库：交易库（equity/decisions）和行情库（bars）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    tdb.stop()
    c = sqlite3.connect(path)
    for ts, sig in decisions:
        c.execute("INSERT INTO decisions (ts,bar_time,feature_time,symbol,price,"
                  "signal,action,reason,equity) VALUES (?,?,?,?,?,?,?,?,?)",
                  (ts, ts, ts, "BTCUSDT", 1.0, sig, "hold", "seed", 100.0))
    for ts, eq in equity_points:
        c.execute("INSERT INTO equity (ts,balance,unrealized,equity,position_qty)"
                  " VALUES (?,?,?,?,?)", (ts, eq, 0.0, eq, 0.0))
    c.commit()
    c.close()

    # 行情库只要 bars 表够 sentinel 查最新 3s 桶
    fd2, mpath = tempfile.mkstemp(suffix=".db")
    os.close(fd2)
    m = sqlite3.connect(mpath)
    m.execute("CREATE TABLE bars (interval_ms INTEGER, open_time INTEGER, "
              "o REAL, h REAL, l REAL, c REAL, v REAL)")
    m.execute("INSERT INTO bars VALUES (3000, ?, 1,1,1,1,1)",
              (int(time.time() * 1000),))
    m.commit()
    m.close()
    return path, mpath


def _sent(path, mpath, monkeypatch):
    s = Sentinel(path, mpath)
    monkeypatch.setattr(s, "_trend_mode", lambda: True)
    monkeypatch.setattr(s, "_note", lambda *a, **k: None)
    monkeypatch.setattr(s, "_alert", lambda *a, **k: None)
    return s


def test_session_pnl_uses_current_strategy_baseline(monkeypatch):
    """旧策略把 100 亏到 71.77；新策略上线后权益没动。
    本次运行累计必须是 0，而不是 -28.23。"""
    path, mpath = _mk([(100, 100.0), (200, 85.0), (300, 71.77), (400, 71.77)],
                      [(150, ""), (350, "trend_wait")])
    try:
        f = _sent(path, mpath, monkeypatch).check()["facts"]
        assert f["strategy"] == "trend"
        assert abs(f["session_pnl"]) < 0.01, (
            u"本次运行累计 %.2f，把旧策略亏掉的 28.23 也算进来了。"
            u"趋势策略上线后权益没动，这里应该是 0。" % f["session_pnl"])
        assert abs(f["session_pnl_pct"]) < 0.01, (
            u"百分比 %.2f%% 仍然是旧策略的账" % f["session_pnl_pct"])
    finally:
        os.unlink(path)
        os.unlink(mpath)


def test_daily_report_does_not_advise_stopping_on_old_losses(monkeypatch):
    """日报不能基于旧策略的亏损建议停手复盘。"""
    path, mpath = _mk([(100, 100.0), (200, 71.77), (400, 71.77)],
                      [(150, ""), (350, "trend_wait")])
    try:
        f = _sent(path, mpath, monkeypatch).daily_report()["facts"]
        assert f["session_pnl_pct"] > -25.0, (
            u"日报会输出 %.2f%% 并据此建议「停手复盘」。"
            u"但那是旧策略的亏损，新策略没有亏钱。" % f["session_pnl_pct"])
    finally:
        os.unlink(path)
        os.unlink(mpath)


def test_session_pnl_still_negative_when_current_strategy_really_loses(monkeypatch):
    """反向约束：当前策略真的亏了，必须照实报。"""
    path, mpath = _mk([(100, 100.0), (200, 71.77), (400, 50.0)],
                      [(150, ""), (350, "trend_wait")])
    try:
        f = _sent(path, mpath, monkeypatch).check()["facts"]
        assert abs(f["session_pnl"] - (50.0 - 71.77)) < 0.5, (
            u"当前策略真的从 71.77 亏到 50.0，必须照实报，实际 %.2f"
            % f["session_pnl"])
    finally:
        os.unlink(path)
        os.unlink(mpath)


# ---------------- 第 59 轮补：哨兵也必须按账本算 ----------------

def _mk_with_modes(rows, decisions):
    """rows: [(ts, equity, paper)] —— 显式区分纸面/实盘账本。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    tdb.stop()
    c = sqlite3.connect(path)
    for ts, sig in decisions:
        c.execute("INSERT INTO decisions (ts,bar_time,feature_time,symbol,price,"
                  "signal,action,reason,equity) VALUES (?,?,?,?,?,?,?,?,?)",
                  (ts, ts, ts, "BTCUSDT", 1.0, sig, "hold", "seed", 100.0))
    for ts, eq, paper in rows:
        c.execute("INSERT INTO equity (ts,balance,unrealized,equity,"
                  "position_qty,paper) VALUES (?,?,?,?,?,?)",
                  (ts, eq, 0.0, eq, 0.0, paper))
    c.commit()
    c.close()
    fd2, mpath = tempfile.mkstemp(suffix=".db")
    os.close(fd2)
    m = sqlite3.connect(mpath)
    m.execute("CREATE TABLE bars (interval_ms INTEGER, open_time INTEGER, "
              "o REAL, h REAL, l REAL, c REAL, v REAL)")
    m.execute("INSERT INTO bars VALUES (3000, ?, 1,1,1,1,1)",
              (int(time.time() * 1000),))
    m.commit()
    m.close()
    return path, mpath


def test_sentinel_drawdown_ignores_paper_ledger():
    """实盘模式下，回撤不能拿纸面峰值当基准。

    实测：服务器库里 paper=1 的峰值 104.74，实盘从 100 起步。
    不过滤的话哨兵会报「回撤 4.5%」，而这段回撤在实盘里根本没发生过。
    极端情况（纸面峰值 250）会直接报 60% 回撤，把人吓去停系统。
    """
    now = int(time.time() * 1000)
    # 决策时间必须【早于】两条权益：strategy_start_ts_at 取的是
    # MIN(ts) WHERE signal LIKE 'trend%'，权益若落在它之前会被直接排除，
    # 测试就变成"因为错误的理由通过"——第 59 轮实测踩到过。
    path, mpath = _mk_with_modes(
        [(now - 7200000, 250.0, 1),          # 纸面峰值
         (now - 3600000, 100.0, 0)],          # 实盘首笔
        [(now - 10800000, "trend_watch")])
    try:
        s = Sentinel(path, mpath, mode="live")
        r = s.check()
        dd = r["facts"]["drawdown_pct"]
        assert r["facts"].get("peak_equity") != 250.0, (
            u"峰值直接取到了纸面账本的 250")
        assert dd < 1.0, (
            u"实盘从 100 起步、纸面峰值 250，回撤却报了 %.1f%% —— "
            u"纸面账本渗进了实盘告警" % dd)
    finally:
        os.unlink(path)
        os.unlink(mpath)


def test_sentinel_session_pnl_ignores_paper_ledger():
    """本次运行累计也只能算当前账本。

    纸面跑过 100 -> 250，实盘从 100 起步。混算的话用户会看到
    「本次运行 +150%」这种完全属于另一个账本的数字。
    """
    now = int(time.time() * 1000)
    path, mpath = _mk_with_modes(
        [(now - 7200000, 250.0, 1),
         (now - 3600000, 100.0, 0)],
        [(now - 10800000, "trend_watch")])
    try:
        s = Sentinel(path, mpath, mode="live")
        r = s.check()
        pnl = r["facts"]["session_pnl_pct"]
        assert abs(pnl) < 1.0, (
            u"实盘首日、纸面历史 250，本次运行却报了 %.1f%% —— "
            u"基准取到了纸面账本" % pnl)
    finally:
        os.unlink(path)
        os.unlink(mpath)
