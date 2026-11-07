# -*- coding: utf-8 -*-
"""停机跨过了 4h 收盘，重启后引擎必须立刻补上这笔单。

为什么这条特别重要：
  回测显示，晚 2 根 4h K 线边际减半，晚 3 根就输给买入持有。
  所以"重启后能否按最近一根已收盘 K 线评估"直接决定策略活不活。
  健康检查每 5 分钟跑一次，重启最多晚 5 分钟——远小于 4 小时，安全。
  但这个"安全"必须被验证过，不能假设。
"""
import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.market.agg import Bar
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

import tempfile

IV4H = 14400000
IV3S = 3000


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

    def __init__(self):
        self.orders = []

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty))
        return Fill(qty, 77815.0)

    async def equity(self):
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
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
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    eng = DecisionEngine(cfg, Feed(77815.0), Ex(), tdb, guard=None)
    # 关键：模拟"重启"——趋势模块是全新的，只有 seed 进来的历史
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    try:
        yield eng, tdb
    finally:
        tdb.stop()


@pytest.mark.asyncio
async def test_restart_after_signal_bar_still_opens(env):
    """上穿发生在停机期间，重启后必须补单。"""
    eng, tdb = env
    st = eng.trend

    # 历史：低位横盘 8 根，最后一根（停机期间收盘的）跳到高位 = 上穿
    hist = [100.0, 99, 98, 97, 96, 95, 94, 93] + [77815.0]
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(hist)]
    st.seeded = True
    st.need_reconcile = False

    assert eng.pos is None
    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(hist)

    await eng._step(snap)

    assert eng.pos is not None, (
        "停机期间完成了上穿，重启后引擎没有开仓。"
        "回测显示晚 3 根 4h 就输给买入持有，所以这条路必须通。")
    assert eng.pos["side"] == "long"
    assert eng.ex.orders, "没有真的下单"


@pytest.mark.asyncio
async def test_restart_with_downtrend_does_not_open(env):
    """反例：重启时是下跌趋势，绝不能因为"刚重启"就开仓。"""
    eng, tdb = env
    st = eng.trend
    hist = [100.0, 101, 102, 103, 104, 105, 106, 107] + [90.0]
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(hist)]
    st.seeded = True
    st.need_reconcile = False

    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(hist)
    await eng._step(snap)

    assert eng.pos is None, "下跌趋势里开了仓"
    assert not eng.ex.orders, "不该下单"
