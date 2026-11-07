# -*- coding: utf-8 -*-
"""把「趋势单不受连亏冷却约束」从偶然行为变成显式契约。

发现过程：
  _close() 里对亏损单累加 consecutive_losses，
  达到 max_consecutive_losses(=3) 就设 cooldown_until 并写一条 risk_event。
  而 check_risk_state() 遇到冷却会拒绝开仓。

  但趋势模式在第 342 行就分派给 _step_trend，而 _step_trend 从不调用
  check_risk_state —— 所以冷却对趋势单完全不生效。

为什么这必须锁死（而不是顺手补上）：
  这个策略胜率 15.5%，平均 5.5 笔才赢一次，历史上最长连亏 13 笔。
  如果真的「连亏 3 笔冷却 30 分钟」，会在最需要一个信号的时候把它掐掉。
  而按实测，漏掉一次进场等于少 46% 的利润。

  所以现在这个不对称是对的。谁把它当成 bug 去修复，就会把策略毁掉。
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
        self.held = 0.0

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty))
        if side == "buy":
            self.held += qty
        else:
            self.held = max(0.0, self.held - qty)
        return Fill(qty, 200.0)

    async def equity(self):
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
                "position_qty": self.held}

    async def position(self):
        return {"qty": self.held, "side": "buy" if self.held else "",
                "entry": 200.0 if self.held else None}

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
    ex = Ex()
    eng = DecisionEngine(cfg, Feed(200.0), ex, tdb, guard=None)
    eng.ex = ex
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
                      for i, p in enumerate([150.0, 140, 130, 120, 110, 100, 200.0])]
    eng.trend.seeded = True
    try:
        yield eng, tdb, ex
    finally:
        tdb.stop()


def _snap():
    s = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    s[IV4H] = [Bar(interval_ms=IV4H, open_time=6 * IV4H, open=200.0,
                   high=200.0, low=200.0, close=200.0, volume=1.0, closed=True)]
    return s


@pytest.mark.asyncio
async def test_cooldown_does_not_block_trend_entry(env):
    """处于连亏冷却中，趋势单仍然要照常进。"""
    import time
    eng, tdb, ex = env
    eng.risk.consecutive_losses = 99
    eng.risk.cooldown_until = int(time.time() * 1000) + 3600 * 1000
    await eng._step(_snap())
    buys = [o for o in ex.orders if o[0] == "buy"]
    assert buys, (
        u"连亏冷却把趋势单拦住了。这个策略历史上连亏过 13 笔，若连亏 3 笔就冷却，"
        u"那笔救命的大单会被反复挡住。")


@pytest.mark.asyncio
async def test_trend_still_enters_when_daily_loss_limit_exceeded(env):
    """日亏损上限对趋势单同样不生效——单笔最大亏损约 11%，够不着 50%。"""
    eng, tdb, ex = env
    eng.risk.day_pnl = -9999.0
    eng.risk.halted_reason = u"当日亏损已达上限"
    await eng._step(_snap())
    buys = [o for o in ex.orders if o[0] == "buy"]
    assert buys, (
        u"日亏损上限把趋势单拦住了。两笔交易通常隔好几天，"
        u"单笔最大亏损约 11%，一天亏掉 50% 在结构上不可能。")
