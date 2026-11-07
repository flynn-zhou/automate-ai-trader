# -*- coding: utf-8 -*-
"""Guard 的 60% 硬停线必须在重启后依然有效。

这是失败驱动测试：先证明当前代码在"重启"场景下漏判。

缺陷：
  Guard 把峰值放在内存里，进程重启就归零，
  第一次 update() 把峰值设成【当时权益】。
  于是这道闸度量的是「从上次重启回撤」，而不是它文档写的"从峰值回撤"。

  实证过的后果：真实峰值 190、权益跌到 40（真实回撤 78.9%），
  Guard 只看到 50%（因为重启后峰值成了 80），60% 硬停不触发。

对照：sentinel 早就从 equity 表算峰值（持久、正确），
      guard 从内存算（易失）。同一个概念两处实现，给出两个数。
      而 guard 是真正下闸的那个。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.trade_db import TradeDB


def _db_with(points, strategy="trend"):
    """建一个临时审计库，equity 表里放若干 (ts, equity)，再放一条策略决策。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    try:
        c = sqlite3.connect(path)
        c.execute("INSERT INTO decisions (ts,bar_time,feature_time,symbol,price,"
                  "signal,action,reason,equity) VALUES (?,?,?,?,?,?,?,?,?)",
                  (1000, 1000, 1000, "BTCUSDT", 1.0, strategy + "_wait",
                   "hold", "seed", 100.0))
        for ts, eq in points:
            c.execute("INSERT INTO equity (ts,balance,unrealized,equity,position_qty)"
                      " VALUES (?,?,?,?,?)", (ts, eq, 0.0, eq, 0.0))
        c.commit()
        c.close()
    finally:
        tdb.stop()
    return TradeDB(path), path


def test_peak_equity_comes_from_db_not_memory():
    """核心：峰值必须能从库里读到，且只算当前策略时代之后。"""
    tdb, path = _db_with([(900, 999.0), (2000, 190.0), (3000, 80.0)])
    try:
        peak = tdb.peak_equity("trend")
        # ts=900 早于策略开始（1000），999.0 不能被算进去
        assert peak == pytest.approx(190.0), (
            u"峰值应为策略时代内的 190.0，实际 %.2f。"
            u"若把策略开始之前的 999.0 算进来，就是「了另一代策略的历史」" % peak)
    finally:
        os.unlink(path)


def test_guard_restores_peak_across_restart():
    """重启后 Guard 必须记得真实峰值。"""
    tdb, path = _db_with([(2000, 190.0), (3000, 80.0)])
    try:
        cfg = AppCfg()
        cfg.trading.strategy = "trend"
        g = Guard(cfg, tdb=tdb)          # <-- 构造 = 进程启动
        g.update(80.0, "WS", 1700000000000)
        snap = g.snapshot()
        assert snap["peak_equity"] == pytest.approx(190.0), (
            u"重启后峰值被重置成了当前权益 %.2f，真实峰值是 190.0。"
            u"这样这道闸度量的就是「从上次重启回撤」。" % snap["peak_equity"])
        # snapshot() 把回撤 round 到 2 位，所以用绝对容差而不是相对容差
        assert snap["drawdown_pct"] == pytest.approx((190.0 - 80.0) / 190.0 * 100, abs=0.01)
    finally:
        os.unlink(path)


def test_hard_stop_fires_at_true_drawdown_after_restart():
    """真实回撤过 60% 时必须下闸，哪怕刚重启过。"""
    tdb, path = _db_with([(2000, 190.0), (3000, 190.0)])
    try:
        cfg = AppCfg()
        cfg.trading.strategy = "trend"
        g = Guard(cfg, tdb=tdb)
        g.update(190.0, "WS", 1700000000000)
        assert not g.st.halted_hard
        # 跌到 40：从 190 算是 78.9%
        g.update(40.0, "WS", 1700000300000)
        assert g.st.halted_hard, (
            u"真实回撤 78.9%% 却没有硬停。重启不该让这道闸失效。")
    finally:
        os.unlink(path)


def test_guard_without_db_still_works():
    """没有审计库时不能崩，退回从当前权益起算。"""
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    g = Guard(cfg)
    g.update(100.0, "WS", 1700000000000)
    assert g.snapshot()["peak_equity"] == pytest.approx(100.0)


def test_reset_still_starts_fresh():
    """人工复位后从当前权益重新起算——这是有意的语义，必须保留。"""
    tdb, path = _db_with([(2000, 190.0), (3000, 80.0)])
    try:
        cfg = AppCfg()
        cfg.trading.strategy = "trend"
        g = Guard(cfg, tdb=tdb)
        g.update(80.0, "WS", 1700000000000)
        g.reset("人工复盘完成")
        assert g.snapshot()["peak_equity"] == 0.0
        g.update(80.0, "WS", 1700000300000)
        assert g.snapshot()["peak_equity"] == pytest.approx(80.0), (
            u"复位后应从当前权益重新起算，不该再把历史的 190 捡回来")
    finally:
        os.unlink(path)


# ---------------- 第 59 轮补：峰值必须按账本过滤 ----------------

def _db_with_modes(rows, strategy="trend"):
    """rows: [(ts, equity, paper)]，显式区分纸面/实盘账本。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    try:
        c = sqlite3.connect(path)
        c.execute("INSERT INTO decisions (ts,bar_time,feature_time,symbol,price,"
                  "signal,action,reason,equity) VALUES (?,?,?,?,?,?,?,?,?)",
                  (1000, 1000, 1000, "BTCUSDT", 1.0, strategy + "_wait",
                   "hold", "seed", 100.0))
        for ts, eq, paper in rows:
            c.execute("INSERT INTO equity (ts,balance,unrealized,equity,"
                      "position_qty,paper) VALUES (?,?,?,?,?,?)",
                      (ts, eq, 0.0, eq, 0.0, paper))
        c.commit()
        c.close()
    finally:
        tdb.stop()
    return TradeDB(path), path


def test_peak_equity_filters_by_ledger():
    """peak_equity(paper=0) 只能看到实盘账本，不能让纸面峰值渗进来。

    纸面的钱不是真钱。混在一起算，实盘就会凭空带着一段从未发生过的回撤。
    """
    tdb, path = _db_with_modes([(2000, 250.0, 1), (2100, 30.0, 0)])
    try:
        assert tdb.peak_equity("trend") == pytest.approx(250.0), "不过滤时应取全量"
        assert tdb.peak_equity("trend", 0) == pytest.approx(30.0), (
            u"实盘账本的峰值应是 30.0；取到 250.0 说明纸面数据渗进了实盘风控")
        assert tdb.peak_equity("trend", 1) == pytest.approx(250.0)
    finally:
        os.unlink(path)


def test_paper_peak_never_blocks_live_start():
    """注资转实盘时，纸面历史峰值不能把实盘硬熔断。

    实测过的死法：库里纸面峰值 250，实盘刚注资 100，
    Guard 恢复出峰值 250 -> 第一个决策周期就判
    「从峰值 250.00U 回撤 60.0%」-> 硬熔断 -> **实盘永远开不了仓**。
    既不交易也不报错，只有一条 halt 事件。
    """
    tdb, path = _db_with_modes([(2000, 250.0, 1)])
    try:
        cfg = AppCfg()
        cfg.trading.strategy = "trend"
        cfg.trading.mode = "live"        # 注资后切实盘
        g = Guard(cfg, tdb=tdb)          # 构造 = 进程启动
        assert g.st.peak_equity == pytest.approx(0.0), (
            u"实盘账本还没有任何权益记录，恢复出的峰值应为 0；"
            u"取到 %.2f 说明用的是纸面的峰值" % g.st.peak_equity)
        g.update(100.0, "WS", 1700000000000)
        ok, why = g.can_open()
        assert ok, u"纸面峰值把实盘挡在门外：%s" % why
    finally:
        os.unlink(path)
