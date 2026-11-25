# -*- coding: utf-8 -*-
"""人工复位必须活过一次重启（2026-11-19 生产实证）。

事故形状：`POST /api/guard/reset` 之后 `peak_equity` 609.5499 → 65.5737、
`drawdown_pct` 89.24 → 0.0、`can_open` True，一切看起来都对。
然后**部署重启了一次**，`peak_equity` 又变回 609.5499、
`halt_kind` 变成 `drawdown`、`can_open` 又变回 False。

根因：`reset()` 只把**内存**里的 `peak_equity` 归零，而 `_restore_peak()`
在进程启动时按 `baseline_ts` 为起点从 `equity` 表重算历史最高权益。
基线不动，重算出来还是那个旧峰值，回撤率立刻回到 89%，
第 1 条闸（回撤 ≥60% 硬停）当场再停一次。

这类缺陷是本仓库的老熟人：**「同一个事实，两个来源」**——
内存里一套、库里一套，而库里那套才是重启后真正生效的。

★ 两个测试卫生要点（都踩过）：

  1. `TradeDB` 必须 `start()`。不 start 就没有写队列的消费者，
     `save_guard_state()` 会被静默丢掉，测试于是读到空 dict。
  2. 回撤闸在 `update()` 里，**不在构造时**。只断言 `can_open()` 会得到
     一个假通过：任何时刻它都是 True（除非 halted 已经为真）。
     必须真的跑一拍 `update(equity, src, now_ms)` 才算"重启后系统怎么想"。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.trade_db import TradeDB

PEAK = 600.0
NOW_EQ = 65.0


def _cfg():
    cfg = AppCfg()
    cfg.trading.strategy = "pending"
    cfg.trading.mode = "live"
    return cfg


def _db_path(tmp_path):
    return str(tmp_path / "trade.db")


def _seed(path):
    """写两条权益：10 天前 600U（历史峰值）、现在 65U。"""
    tdb = TradeDB(path)
    tdb.start()
    tdb.log_equity({"ts": int(time.time() * 1000) - 86_400_000 * 10,
                    "equity": PEAK, "balance": PEAK, "paper": 0,
                    "strategy": "pending"})
    tdb.log_equity({"ts": int(time.time() * 1000),
                    "equity": NOW_EQ, "balance": NOW_EQ, "paper": 0,
                    "strategy": "pending"})
    tdb.stop()


def _open(path):
    tdb = TradeDB(path)
    tdb.start()
    return tdb


def _boot(cfg, tdb, eq=NOW_EQ):
    """模拟一次进程重启 + 第一拍决策。

    Guard 构造时只从库里恢复（峰值 + 硬停），回撤闸在 update() 里判。
    两步都走完，看到的才是"重启之后系统真实怎么想"。
    """
    g = Guard(cfg, tdb)
    g.update(eq, "WS", int(time.time() * 1000))
    return g


def test_a_restart_without_reset_halts_on_drawdown(tmp_path):
    """前提：不复位就重启，60% 回撤闸必须真的停（这条闸本身是好的）。

    90% 回撤远超 60% 线。如果这一条都停不下来，
    后面几条测试全部失去意义。
    """
    path = _db_path(tmp_path)
    _seed(path)
    tdb = _open(path)
    try:
        g = _boot(_cfg(), tdb)
        assert g.st.peak_equity == pytest.approx(PEAK), (
            u"重启后应恢复历史峰值 %s，实际 %r" % (PEAK, g.st.peak_equity))
        assert g.st.halted_hard is True, u"90%% 回撤却没有硬停"
        assert g.st.halt_kind == "drawdown"
        assert g.can_open()[0] is False
    finally:
        tdb.stop()


def test_reset_rebaselines_peak_in_memory(tmp_path):
    """复位当场：峰值归零（内存层面的既有行为，先钉住）。"""
    path = _db_path(tmp_path)
    _seed(path)
    tdb = _open(path)
    try:
        g = _boot(_cfg(), tdb)
        g.reset("test")
        assert g.st.halted_hard is False
        assert g.st.peak_equity == 0.0
        assert g.can_open()[0] is True
    finally:
        tdb.stop()


def test_reset_persists_the_new_baseline(tmp_path):
    """复位必须把 baseline_ts 写进库，而且写的是「现在」。"""
    path = _db_path(tmp_path)
    _seed(path)
    tdb = _open(path)
    try:
        before = int(time.time() * 1000)
        g = _boot(_cfg(), tdb)
        g.reset("test")
        after = int(time.time() * 1000)
        tdb.stop()
    finally:
        try:
            tdb.stop()
        except Exception:
            pass

    gs = _open(path).load_guard_state() or {}
    ts = int(gs.get("baseline_ts") or 0)
    assert ts >= before - 5000, (
        u"复位没有把 baseline_ts 推到当前（%r）：重启会按旧基线重算峰值，"
        u"复位白做" % (gs.get("baseline_ts"),))
    assert ts <= after + 5000


def test_reset_survives_a_restart(tmp_path):
    """★★ 生产实证的那条：复位之后重启，不许再停。

    没有这条测试，「复位只在内存生效」这个 bug 会一直活着——
    因为它表现得完全正确，直到下一次重启（这次就是部署重启撞出来的）。
    """
    path = _db_path(tmp_path)
    _seed(path)

    tdb = _open(path)
    try:
        g = _boot(_cfg(), tdb)
        assert g.st.halted_hard is True, u"构造前提：重启后应已因回撤硬停"
        g.reset("test")
        tdb.stop()
    finally:
        try:
            tdb.stop()
        except Exception:
            pass

    # ↓ 又一次重启：全新 Guard + 全新 TradeDB，只能从库里读
    tdb2 = _open(path)
    try:
        g2 = _boot(_cfg(), tdb2)
        ok, why = g2.can_open()
        assert g2.st.halted_hard is False, (
            u"复位后重启又硬停了：peak_equity=%r（应为 0 或当前权益）、"
            u"kind=%r、why=%r —— reset() 必须把 baseline_ts 一起推到当前"
            % (g2.st.peak_equity, g2.st.halt_kind, why))
        assert ok, u"复位后重启仍然不许开仓：%r" % (why,)
        assert g2.st.peak_equity == pytest.approx(NOW_EQ), (
            u"重启后峰值被算成 %r，应当是复位后的当前权益 %s —— "
            u"算回历史峰值（%s）就是基线没生效"
            % (g2.st.peak_equity, NOW_EQ, PEAK))
    finally:
        tdb2.stop()
