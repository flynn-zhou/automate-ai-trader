# -*- coding: utf-8 -*-
"""注资 100 USDT 之后，第一笔仓到底开不开得出来。

这是整个项目最终要过的关，之前从没用【恰好 100U】验证过。
历史教训就在代码注释里：纸面模式曾按永续规格（步长 0.001）撮合现货策略，
权益 71.77U 时一手要 77.80U，直接被判"买不起"——而现货一手只要 0.78U。
同一笔钱在两种规格下结论完全相反，一个能开仓一个不能。

所以这里锁死三件事：
  1. 100U + 现货 + 现价 ≈ 77800 -> 必须能开仓
  2. 下单数量必须落在现货步长网格上（0.00001），且不超过可用余额
  3. 名义必须 >= 现货最小名义 5U（否则交易所直接拒单）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine

import tempfile

PX = 77815.0          # 实测现价量级
STEP = 0.00001


class _Hub:
    def publish(self, m):
        pass


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self.src = "ws"
        self.agg = None


def _engine(equity, market="spot", mode="paper"):
    cfg = AppCfg()
    cfg.trading.mode = mode
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = market
    cfg.trading.paper_balance = equity
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = Feed(PX)
    ex = PaperExecutor(feed, cfg)
    return DecisionEngine(cfg, feed, ex, tdb, guard=None), tdb


def test_100_usdt_is_enough_to_open_on_spot():
    """核心断言：100U 现货必须开得出仓。"""
    eng, tdb = _engine(100.0)
    try:
        note = eng._affordability_note(PX, 100.0)
        assert note == "", ("100U 被判资金不足，实际第一笔交易永远不会发生：" + note)
    finally:
        tdb.stop()


def test_legacy_bug_71_usdt_spot_is_affordable_too():
    """当初踩坑的那个数字：71.77U 在现货规格下其实完全买得起。"""
    eng, tdb = _engine(71.77)
    try:
        assert eng._affordability_note(PX, 71.77) == ""
    finally:
        tdb.stop()


def test_perp_spec_would_have_blocked_it():
    """反证：同样的钱用永续规格就会被判买不起。

    这条测试的意义是防止有人"顺手"把 _is_spot() 改回去——
    那样 100U 账户在现货下会因为 0.001 步长 + 55U 最小名义而空转。
    """
    eng, tdb = _engine(100.0, market="perp")
    try:
        eng.ex.name = "live"          # 强制走永续口径
        assert eng._qty_step() == 0.001
        assert eng._min_notional() == 55.0
    finally:
        tdb.stop()


def test_spot_plan_qty_is_on_grid_and_within_balance():
    """仓位必须落在现货步长网格上，且不超过 100U 余额（含手续费缓冲）。"""
    eng, tdb = _engine(100.0)
    try:
        from ai_trader.trading.risk import Plan
        plan = eng._trend_plan(PX, 100.0, {"ok": True, "name": "trend_up"})
        assert plan is not None, "100U 算不出计划仓位"
        qty = plan.qty
        assert qty > 0, "仓位为 0，等于不交易"
        # 步长网格
        n = qty / STEP
        assert abs(n - round(n)) < 1e-6, "数量不在现货步长网格上：%r" % qty
        # 名义必须够最小名义
        notional = qty * PX
        assert notional >= 5.0, "名义 %.2fU 低于现货最小名义 5U，会被拒单" % notional
        # 不能超过可用余额（否则首单就报余额不足）
        assert notional <= 100.0, "名义 %.2fU 超过 100U 余额" % notional
        # 也不该浪费：至少用掉八成
        assert notional >= 80.0, "只用了 %.1fU，仓位过小" % notional
    finally:
        tdb.stop()


def test_spot_plan_never_exceeds_balance_due_to_rounding():
    """取整必须向下。四舍五入会向上超配，现货会被截断导致记账与实际不符。"""
    eng, tdb = _engine(100.0)
    try:
        for px in (77815.0, 33333.0, 123456.0, 512.0):
            plan = eng._trend_plan(px, 100.0, {"ok": True, "name": "trend_up"})
            assert plan is not None
            assert plan.qty * px <= 100.0 * 1.0005, (
                "价格 %.1f 时名义 %.2fU 超配" % (px, plan.qty * px))
    finally:
        tdb.stop()


def test_zero_equity_says_wallet_hint():
    """余额为 0 时必须提示钱包，而不是让运维干等一个不会来的成交。"""
    eng, tdb = _engine(0.0)
    try:
        note = eng._affordability_note(PX, 0.0)
        assert "现货钱包" in note, "没说清钱该放哪个钱包：" + note
    finally:
        tdb.stop()
