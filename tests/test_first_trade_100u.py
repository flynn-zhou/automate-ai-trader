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


def test_100_usdt_is_enough_to_open_on_the_contract_account():
    """核心断言：100U 在合约口径下必须开得出仓。

    ★ 2026-11-17 只做合约：以前这条叫 `..._on_spot`，靠的是现货 0.00001 步长。
      现在只有一套规格，所以这条测试验的就是℈它会不会
      因为 55U 最小名义而把账户判成“买不起”。
    """
    eng, tdb = _engine(100.0)
    try:
        note = eng._affordability_note(PX, 100.0)
        assert note == "", ("100U 被判资金不足，实际第一笔交易永远不会发生：" + note)
    finally:
        tdb.stop()


def test_legacy_bug_71_usdt_is_now_structurally_unaffordable():
    """★ 反转了：当年那个 71.77U 在合约口径下确实开不出仓。

    现货时代它买得起（一手 0.78U），所以当时反驳了“它买不起”这个猜测。
    现在一手是 0.001 BTC（约 77.75U），71.77U 连一手都不到 —— 这是**结构事实**，
    不是“更贵”。把它钉住是为了防止有人把它当成“小 bug”去“修”。
    """
    eng, tdb = _engine(71.77)
    try:
        note = eng._affordability_note(PX, 71.77)
        assert note, "71.77U 居然被判为能开仓 —— 一手 0.001 BTC 就要 77.75U"
        assert "资金不足" in note, note
        assert "合约钱包" in note, note
    finally:
        tdb.stop()


def test_there_is_only_one_spec_now():
    """★ 只剩一套规格：步长 0.001、最小名义 55U。

    以前这个文件用一对相反的用例钉住“两套规格”（现货 vs 永续）。
    现在第二套被删了，所以要钉的反而是“**不存在第二套**”。
    """
    eng, tdb = _engine(100.0, market="perp")
    try:
        assert eng._qty_step() == 0.001
        assert eng._min_notional() == 55.0
        # 反向锁：强行把配置改成 spot 也不能变回去
        eng.tcfg.exec_market = "spot"
        eng.ex.name = "spot"
        assert eng._qty_step() == 0.001
        assert eng._min_notional() == 55.0
    finally:
        tdb.stop()


def test_plan_qty_is_on_grid_and_within_balance():
    """仓位必须落在步长网格上，且不超过余额（含手续费缓冲）。"""
    eng, tdb = _engine(100.0)
    try:
        plan = eng._trend_plan(PX, 100.0, {"ok": True, "name": "trend_up"})
        assert plan is not None, "100U 算不出计划仓位"
        qty = plan.qty
        assert qty > 0, "仓位为 0，等于不交易"
        n = qty / STEP
        assert abs(n - round(n)) < 1e-6, "数量不在步长网格上：%r" % qty
        notional = qty * PX
        assert notional >= 55.0, "名义 %.2fU 低于合约最小名义 55U，会被拒单" % notional
        assert notional <= 100.0, "名义 %.2fU 超过 100U 余额" % notional
    finally:
        tdb.stop()


def test_plan_never_exceeds_balance_due_to_rounding():
    """取整必须向下。四舍五入会向上超配，高杠杆下会被截断导致记账与实际不符。"""
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
        assert "合约钱包" in note, "没说清钱该放哪个钱包：" + note
        assert "现货钱包" not in note, "仍在提示现货钱包：" + note
    finally:
        tdb.stop()
