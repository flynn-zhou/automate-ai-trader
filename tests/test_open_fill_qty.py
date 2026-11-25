# -*- coding: utf-8 -*-
"""开仓记账必须用实际成交量，不能用计划量。

事故背景（2026-11-01 代码审查发现）：
_open() 里 self.pos 和 trades 表用的都是 plan.qty，
而 FillResult 本身带着真实成交量 fill.qty。
一旦执行层因余额截断、或交易所部分成交，
引擎就会以为仓位比实际更大——平仓时卖出超过实际持有的币。
现货会被交易所直接拒掉（然后仓位永远平不掉），永续则会变成反向开仓。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.base import FillResult
from ai_trader.trading.engine import DecisionEngine


class _Hub:
    def publish(self, *a, **k): pass


class Feed:
    def __init__(self):
        self.last_price = 30000.0
        self.cfg = AppCfg()
        self.hub = _Hub()


class PartialExecutor:
    """永远只成交一半的执行器，模拟部分成交。"""
    name = "spot"

    def __init__(self, fill_ratio=0.5):
        self.ratio = fill_ratio
        self.pos_qty = 0.0

    async def equity(self):
        return {"balance": 100.0, "unrealized": 0.0, "equity": 100.0,
                "position_qty": self.pos_qty, "position_side": "long" if self.pos_qty else ""}

    async def market_order(self, side, qty, reason="", reduce_only=False,
                           client_id=""):
        got = round(qty * self.ratio, 8)
        self.pos_qty += got
        return FillResult(ok=True, price=30000.0, qty=got, fee=0.03,
                          order_id="x", status="filled")


class _DB:
    def __init__(self):
        self.trades, self.fills = [], []

    def log_order(self, d): pass

    def log_fill(self, d): self.fills.append(d)

    def log_trade(self, d):
        self.trades.append(d)
        return len(self.trades)

    def log_risk(self, *a, **k): pass

    def log_decision(self, *a, **k): pass


@pytest.mark.asyncio
async def test_open_records_actual_fill_qty():
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    feed = Feed()
    ex = PartialExecutor(fill_ratio=0.5)
    eng = DecisionEngine(cfg, feed, ex, _DB())
    eng.db = _DB()

    plan = eng._trend_plan(30000.0, 100.0, {"ok": True})
    assert plan.ok
    planned = plan.qty

    await eng._open({"name": "trend_up", "reason": "测试"}, plan,
                    30000.0, 100.0, 1700000000000, {}, {})

    assert eng.pos is not None
    # 只成交了一半，仓位必须记一半
    assert eng.pos["qty"] == pytest.approx(planned * 0.5, rel=1e-6), \
        "仓位记录的必须是实际成交量 %.8f，而不是计划量 %.8f" % (eng.pos["qty"], planned)
    assert eng.db.trades[0]["qty"] == pytest.approx(planned * 0.5, rel=1e-6)
    assert eng.db.fills[0]["qty"] == pytest.approx(planned * 0.5, rel=1e-6)


@pytest.mark.asyncio
async def test_open_records_full_qty_on_full_fill():
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    feed = Feed()
    ex = PartialExecutor(fill_ratio=1.0)
    eng = DecisionEngine(cfg, feed, ex, _DB())
    eng.db = _DB()

    plan = eng._trend_plan(30000.0, 100.0, {"ok": True})
    await eng._open({"name": "trend_up", "reason": "测试"}, plan,
                    30000.0, 100.0, 1700000000000, {}, {})
    assert eng.pos["qty"] == pytest.approx(plan.qty, rel=1e-9)


@pytest.mark.asyncio
async def test_open_sends_client_id_at_order_time():
    """开仓的 clientOrderId 必须在**下单那一刻**就下发给交易所。

    ★ 2026-11-25：原来 `_open` 是先下单、后算 `client_id`（那是给
      `log_order` 用的），于是交易所那张单上是它自己生成的随机串，
      本地这条 id 永远对不上任何成交 —— 成交归因（靠 `AI-` 前缀认领）
      对此完全失明，我们自己的开仓成交会被判成「用户手工仓」接管。

    这条断言的样子就是证据：桩收到的 `client_id` 必须非空且带 AI- 前缀。
    """
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    feed = Feed()
    ex = PartialExecutor(fill_ratio=1.0)
    seen = []
    orig = ex.market_order

    async def _spy(side, qty, reason="", reduce_only=False, client_id=""):
        seen.append(client_id)
        return await orig(side, qty, reason=reason,
                          reduce_only=reduce_only, client_id=client_id)

    ex.market_order = _spy
    eng = DecisionEngine(cfg, feed, ex, _DB())
    eng.db = _DB()

    plan = eng._trend_plan(30000.0, 100.0, {"ok": True})
    await eng._open({"name": "trend_up", "reason": "测试"}, plan,
                    30000.0, 100.0, 1700000000000, {}, {})

    assert seen, "没下单？"
    assert seen[0], ("开仓单没有下发 clientOrderId —— 成交会被判成人工仓")
    assert str(seen[0]).startswith("AI-"), (
        "归因靠 AI- 前缀认领，实际 %r" % (seen[0],))
