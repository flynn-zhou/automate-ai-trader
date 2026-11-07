# -*- coding: utf-8 -*-
"""哨兵看不到最严重的那两种失败。

场景：用户注资后切成 live，但 API key 有问题（拼错、权限不足、IP 白名单没配）：
  - 凭证【存在】，所以 main.py 的启动校验放行
  - 每次 equity() 都失败 -> _equity_stale = True
  - 入场统一在 _open 里被拦，记一条 skip：
      signal = "trend_up"（信号自己的名字）
      reason = "权益数据过期（查询失败），暂停开仓"

而哨兵第 4.5 条「信号想开仓却一直没开成」当时查的是：
    signal = trend_entry AND action = skip
      OR reason LIKE %资金不足%
      OR reason LIKE %下单失败%
      OR reason LIKE %退避%

  - signal 是 trend_up，不等于 trend_entry -> 不匹配
  - reason 是「权益数据过期」，不含那三个关键词 -> 不匹配

于是：系统一遍遍记录「我想开仓但权益查不到」，哨兵一声不吭。
进程活着、行情在动、决策在写、看板有数——所有指标都正常，只有钱不会动。

这正是本项目反复出现的失败模式：安静。
"""
import os
import sqlite3
import sys
import tempfile
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.sentinel import Sentinel
from ai_trader.trade_db import TradeDB


def _mk(tmp):
    tdb_path = os.path.join(tmp, "market_trade.db")
    mdb_path = os.path.join(tmp, "market.db")
    c = sqlite3.connect(mdb_path)
    c.execute("CREATE TABLE IF NOT EXISTS bars (open_time INTEGER, interval_ms INTEGER)")
    c.execute("INSERT INTO bars VALUES (?, 3000)", (int(time.time() * 1000),))
    c.commit()
    c.close()
    return tdb_path, mdb_path


def _dec(tdb, now_ms, i, signal, reason, equity=100.0):
    tdb.log_decision({
        "ts": now_ms - i * 60000,
        "bar_time": now_ms - i * 60000,
        "feature_time": now_ms - i * 60000,
        "symbol": "BTCUSDT",
        "signal": signal,
        "action": "skip",
        "reason": reason,
        "price": 30000.0,
        "equity": equity,
    })


def test_sentinel_catches_stale_equity_skip():
    """权益查不到导致的 skip 必须告警（以前看不见）。"""
    tmp = tempfile.mkdtemp()
    tdb_path, mdb_path = _mk(tmp)
    tdb = TradeDB(tdb_path)
    tdb.start()
    try:
        now_ms = int(time.time() * 1000)
        for i in range(5):
            _dec(tdb, now_ms, i, "trend_up",
                 "权益数据过期（查询失败），暂停开仓")
        time.sleep(1.5)

        r = Sentinel(tdb_path, mdb_path).check()
        facts = r["facts"]
        issues = r["issues"]

        assert facts.get("trend_blocked_2h") >= 5, (
            u"哨兵没统计到这 5 次「权益查不到所以不开仓」，",
            u"trend_blocked_2h=%s。这是凭证失效时唯一的表现形式。"
            % facts.get("trend_blocked_2h"))
        hit = [i for i in issues if "挡住" in i]
        assert hit, u"没有任何告警，issues=%s" % issues
        # 告警必须带上具体原因，否则人还得自己去翻日志
        assert "权益数据过期" in hit[0], (
            u"告警没带具体原因：%s" % hit[0])
    finally:
        tdb.stop()


def test_sentinel_still_catches_plan_rejected():
    """原来能抓到的那条路径不能因为改查询条件而回归。"""
    tmp = tempfile.mkdtemp()
    tdb_path, mdb_path = _mk(tmp)
    tdb = TradeDB(tdb_path)
    tdb.start()
    try:
        now_ms = int(time.time() * 1000)
        for i in range(3):
            _dec(tdb, now_ms, i, "trend_entry", "计划被拒：资金不足", equity=1.0)
        time.sleep(1.5)

        r = Sentinel(tdb_path, mdb_path).check()
        assert r["facts"].get("trend_blocked_2h") >= 3, (
            u"trend_entry/skip 这条路径回归了")
        assert any("挡住" in i for i in r["issues"])
    finally:
        tdb.stop()


def test_sentinel_silent_when_nothing_blocked():
    """没有阻塞时不能误报，否则真问题会被淹掉。"""
    tmp = tempfile.mkdtemp()
    tdb_path, mdb_path = _mk(tmp)
    tdb = TradeDB(tdb_path)
    tdb.start()
    try:
        now_ms = int(time.time() * 1000)
        # 正常的 hold/watch 决策，没有任何 skip
        for i in range(5):
            tdb.log_decision({
                "ts": now_ms - i * 60000,
                "bar_time": now_ms - i * 60000,
                "feature_time": now_ms - i * 60000,
                "symbol": "BTCUSDT",
                "signal": "trend_watch",
                "action": "watch",
                "reason": "空仓等待：现价在均线下方",
                "price": 30000.0, "equity": 100.0,
            })
        time.sleep(1.5)

        r = Sentinel(tdb_path, mdb_path).check()
        assert not [i for i in r["issues"] if "挡住" in i], (
            u"没被挡住却告警了，误报会淹掉真问题：%s" % r["issues"])
    finally:
        tdb.stop()


def test_sentinel_catches_insufficient_funds_logged_as_hold():
    """资金不足但记在 action=hold 上，也必须告警。

    这条是把一个真实回归钉住的。

    我第一版修复把 action='skip' 提成了整条查询的前置条件，
    于是所有"想开仓但钱不够"的记录全被漏掉——
    因为它们记在 action='hold' 上，不是 skip。

    线上实测到 4 条这样的记录：
      trend_wait / hold / 「资金不足：权益 71.77U，
                            建一手至少需要 55.00U 名义」

    那是货真价实的阻塞，而且正是 100U 以下账户最常见的阻塞。
    单元测试当时全绿，是拿真实数据查新旧口径才发现旧=4 新=0。
    """
    tmp = tempfile.mkdtemp()
    tdb_path, mdb_path = _mk(tmp)
    tdb = TradeDB(tdb_path)
    tdb.start()
    try:
        now_ms = int(time.time() * 1000)
        for i in range(4):
            tdb.log_decision({
                "ts": now_ms - i * 60000,
                "bar_time": now_ms - i * 60000,
                "feature_time": now_ms - i * 60000,
                "symbol": "BTCUSDT",
                "signal": "trend_wait",
                "action": "hold",
                "reason": "资金不足：权益 71.77U，建一手至少需要 55.00U 名义",
                "price": 78000.0, "equity": 71.77,
            })
        time.sleep(1.5)

        r = Sentinel(tdb_path, mdb_path).check()
        assert r["facts"].get("trend_blocked_2h") >= 4, (
            u"资金不足但记在 action=hold 上的记录被漏掉了，",
            u"trend_blocked_2h=%s。这是 100U 以下账户最常见的阻塞，"
            u"漏掉它等于哨兵对最可能发生的问题视而不见。"
            % r["facts"].get("trend_blocked_2h"))
        assert any("挡住" in i for i in r["issues"])
    finally:
        tdb.stop()
