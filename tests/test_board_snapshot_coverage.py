# -*- coding: utf-8 -*-
"""大屏在两种最常见的情况下会显示错误的信息。

用户的原话是「我要知道你在做什么」。而这个看板在最需要它的时候会撒谎：

情况一：闸门拦截（数据源 DOWN / 引擎卡住 / 硬熔断）
  _trend_snapshot 只在空仓分支【闸门检查之后】才构建（engine.py:526）。
  闸门一拦就 return，快照不更新，trend_state() 返回 {ready: False}。
  大屏于是渲染：趋势策略预热中：4h K线 0/200。
  而真实原因是"数据源 DOWN 已 60s，禁止开仓"。
  用户看到"还在加载"，就会一直等——等一个不会来的完成。

情况二：持仓中
  快照同样只在空仓分支构建。持仓时那条分支根本不走，
  所以 t.pos 永远是空的，大屏的持仓展示（qty/entry/浮盈/止损/已持有时长）永不出现。
  而"我现在拿着什么、浮盈多少、止损在哪"正是用户最想知道的。

两种情况都让看板在最关键的时刻沉默或说错话。
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import pytest_asyncio

from ai_trader.config import AppCfg
from ai_trader.market.agg import Bar
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000
IV3S = 3000


class _Ex:
    name = "spot"

    def __init__(self):
        self.held = 0.0

    async def equity(self):
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
                "position_qty": self.held}

    async def position(self):
        return {"qty": self.held, "side": "long" if self.held else "",
                "entry": 70000.0 if self.held else None}

    async def market_order(self, side, qty, reason="", reduce_only=False):
        class F:
            ok = True
            price = 70000.0
            qty = 0.0
            fee = 0.0
            order_id = "1"
            status = "filled"
            reason = ""
        f = F()
        f.qty = qty
        return f


class _Hub:
    def publish(self, m):
        pass


class _Agg:
    def __init__(self, px):
        self._b = Bar(interval_ms=IV3S, open_time=0, open=px, high=px,
                      low=px, close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._b if iv == IV3S else None


class _Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self.agg = _Agg(px)
        self.src = "ws"


class _Guard:
    def __init__(self, block=False):
        self._block = block

    def can_open(self):
        if self._block:
            return False, u"数据源 DOWN 已 60s，禁止开仓"
        return True, ""

    def notify_decision(self, ts):
        pass

    def update(self, eq, src, ts):
        pass


@pytest_asyncio.fixture
async def env():
    import tempfile
    tmp = tempfile.mkdtemp()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_min_notional = 5.0
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    ex = _Ex()
    eng = DecisionEngine(cfg, _Feed(70000.0), ex, tdb, guard=None)
    eng.ex = ex
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.bars = [{"t": i * IV4H, "o": 70000.0, "h": 70000.0,
                       "l": 70000.0, "c": 70000.0, "v": 1.0}
                      for i in range(10)]
    eng.trend.seeded = True
    try:
        yield eng, tdb, ex, cfg
    finally:
        tdb.stop()


def _snap():
    return {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}


@pytest.mark.asyncio
async def test_board_still_describes_state_when_guard_blocks(env):
    """闸门拦截时，大屏必须说明真实原因，而不是显示"预热中"。"""
    eng, tdb, ex, cfg = env
    eng.guard = _Guard(block=True)
    eng._last_equity = 100.0

    await eng._step(_snap())
    st = eng.trend_state()

    assert st.get("ready") is True, (
        u"闸门拦截时大屏返回 ready=False，会渲染成「趋势策略预热中：4h K线 0/200」。"
        u"而真实原因是数据源 DOWN——用户会一直等一个不会来的完成。")
    assert st.get("sma"), u"闸门拦截时大屏仍然要能显示 SMA"


@pytest.mark.asyncio
async def test_board_shows_position_while_holding(env):
    """持仓时大屏必须能显示持仓（qty/entry/浮盈/止损/持有时长）。"""
    eng, tdb, ex, cfg = env
    eng.ex.held = 0.001
    eng.pos = {"side": "long", "qty": 0.001, "entry": 70000.0,
               "stop": 52500.0, "target": 0.0, "entry_ts": 0,
               "bars": 0, "best": 70000.0, "worst": 70000.0,
               "trade_id": None, "max_hold_bars": 600,
               "open_reason": "test", "fee": 0.0, "signal": "trend"}

    await eng._step(_snap())
    st = eng.trend_state()

    assert st.get("hold") is True, u"持仓时大屏必须标记 hold=True"
    assert st.get("pos"), (
        u"持仓时大屏没有 pos 数据，用户看不到自己拿着什么、浮盈多少、止损在哪")
    assert st["pos"]["qty"] == pytest.approx(0.001)
