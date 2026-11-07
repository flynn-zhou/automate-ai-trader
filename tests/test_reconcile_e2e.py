# -*- coding: utf-8 -*-
"""端到端：资金不足导致没能上车，钱到了必须能补上。

这是这个账户最真实的场景：
  1. 进程重启，价格已在 SMA 上方（上穿事件早就过去了）
  2. 启动对账想补仓，但权益太小，计划被拒
  3. 一直空仓
  4. 用户注资
  5. 【必须】不需要等下一次上穿就能补进去

旧实现在第 2 步就把 need_reconcile 清掉了，第 5 步永远等不到。
"""
import os
import sys
import tempfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.market.agg import Bar
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000
IV3S = 3000
PX = 77800.0


class Fill:
    def __init__(self, qty, px):
        self.ok = True
        self.price = px
        self.qty = qty
        self.fee = 0.0
        self.order_id = "1"
        self.status = "filled"
        self.reason = ""


class Ex:
    name = "spot"

    def __init__(self, equity):
        self.eq = equity
        self.orders = []

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty))
        return Fill(qty, PX)

    async def equity(self):
        return {"equity": self.eq, "balance": self.eq, "unrealized": 0.0,
                "position_qty": 0.0}

    async def position(self):
        return {"qty": 0.0, "side": "", "entry": None}

    async def verify_price(self, px):
        return px


class _Hub:
    def publish(self, m):
        pass


class _Agg:
    def __init__(self, px):
        self._b = Bar(interval_ms=IV3S, open_time=0, open=px, high=px,
                      low=px, close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._b if iv == IV3S else None


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self.agg = _Agg(px)
        self.src = "ws"


def _bars(prices):
    return [Bar(interval_ms=IV4H, open_time=i * IV4H, open=p, high=p,
                low=p, close=p, volume=1.0, closed=True)
            for i, p in enumerate(prices)]


@pytest_asyncio.fixture
async def env():
    tmp = tempfile.mkdtemp()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_min_notional = 5.0
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    ex = Ex(1.0)          # 一开始只有 1U，买不起
    eng = DecisionEngine(cfg, Feed(PX), ex, tdb, guard=None)
    # 引擎构造时不会自动建趋势模块，必须显式挂上去（和 main.py 一致）
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    try:
        yield eng, tdb, ex, cfg
    finally:
        tdb.stop()


def _above_hist():
    return [100.0, 101, 102, 103, 104, 105, 120, 200.0]


@pytest.mark.asyncio
async def test_poor_then_funded_still_enters(env):
    """买不起 -> 注资 -> 必须补上，不需要新的上穿。"""
    eng, tdb, ex, cfg = env
    st = eng.trend
    hist = _above_hist()
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(hist)]
    st.seeded = True
    st.need_reconcile = True

    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(hist)

    # 第一次：只有 1U，开不成
    await eng._step(snap)
    assert eng.pos is None, "1U 也开仓了？"
    assert not ex.orders, "买不起却发了单"
    assert st.need_reconcile is True, (
        "没开成就把对账标志清掉了——注资后会永远等不到上车机会")

    # 注资
    ex.eq = 100.0
    await eng._step(snap)
    assert eng.pos is not None, (
        "注资之后仍然没有补仓。价格一直在均线上方，"
        "want_entry 认的上穿事件永远不会再发生，这段趋势就整段错过了。")
    assert ex.orders, "没有真的下单"


@pytest.mark.asyncio
async def test_below_sma_marks_reconcile_done(env):
    """在均线下方：本来就该空仓，对账应标记完成，不要一直挂着。"""
    eng, tdb, ex, cfg = env
    st = eng.trend
    hist = [200.0, 190, 180, 170, 160, 150, 140, 50.0]
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(hist)]
    st.seeded = True
    st.need_reconcile = True

    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(hist)
    await eng._step(snap)
    assert eng.pos is None
    assert st.need_reconcile is False, "在均线下方还不清对账标志"
