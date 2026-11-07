# -*- coding: utf-8 -*-
"""趋势分支的两道安全闸门。

1) 数据源 DOWN 禁止开仓。REQUIREMENTS 写明"看不见行情就不许开枪"，
   但趋势分支原本完全没有这道闸门——scalp 路径检查了 guard.can_open()，
   趋势路径没检查，等于数据源监控在最需要它的策略上是失效的。

2) 熔断止损不能只信本地价格。行情断线时本地价是陈旧的，
   陈旧价假触发会把健康仓位按市价卖掉；卖掉后要等下一次金叉才能回场，可能几周。
   触发时必须向交易所要一次权威价格复核。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000


class _Hub:
    def publish(self, *a, **k): pass


class Feed:
    def __init__(self, px=80000.0, src="WS"):
        self.last_price = px
        self.cfg = AppCfg()
        self.hub = _Hub()
        self.src = src

        class _Agg:
            def current(self, iv):
                class _B:
                    close = px
                return _B()
        self.agg = _Agg()


class _DB:
    def __init__(self):
        self.risks, self.decisions, self.orders, self.fills, self.trades = [], [], [], [], []

    def log_risk(self, kind, msg):
        self.risks.append((kind, msg))

    def log_decision(self, *a, **k):
        self.decisions.append((a, k))

    def log_order(self, d): self.orders.append(d)
    def log_fill(self, d): self.fills.append(d)
    def log_trade(self, d): self.trades.append(d); return 1
    def log_equity(self, d): pass
    def close_trade(self, d): pass
    def open_trade(self, d): return 1


class Ex:
    """可控执行器：position_qty=已持仓，mark_price=交易所权威价。"""
    name = "spot"

    def __init__(self, mark=80000.0, qty=0.001):
        self.mark = mark
        self.qty = qty
        self.sold = []

    async def equity(self):
        return {"balance": 0.0, "unrealized": 0.0, "equity": 100.0,
                "position_qty": self.qty, "position_side": "long" if self.qty else ""}

    async def mark_price(self):
        return self.mark

    async def market_order(self, side, qty, reason="", reduce_only=False):
        from ai_trader.exchange.base import FillResult
        self.sold.append((side, qty))
        return FillResult(ok=True, price=self.mark, qty=qty, fee=0.08, status="filled")

    async def position(self):
        return {"qty": self.qty, "entry": None, "side": "long" if self.qty else ""}


class Guard:
    def __init__(self, ok=True, why=""):
        self.ok, self.why = ok, why

    def can_open(self):
        return (self.ok, self.why)

    def update(self, *a, **k): pass
    def notify(self, *a, **k): pass
    def notify_decision(self, *a, **k): pass


def _eng(px=80000.0, src="WS", guard=None, ex=None, pos=None):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = 25.0
    feed = Feed(px, src)
    ex = ex or Ex(mark=px)
    db = _DB()
    e = DecisionEngine(cfg, feed, ex, db, guard=guard)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    # 收盘必须【逐根走高】，让最后一根站在均线上方。
    # 原来 8 根全设成 100.0，SMA5 = 100.0，above = (100 > 100) = False，
    # 于是策略本来就该按"收盘跌破 SMA"出场——
    # 那会让"陈旧价不许触发假止损"这条测试变成在测另一件事。
    # 这个测试要隔离的是【熔断路径】，就必须先排除 SMA 出场。
    e.trend.bars = [{"t": i * IV4H, "o": 100.0 + i, "h": 100.0 + i,
                     "l": 100.0 + i, "c": 100.0 + i, "v": 1.0} for i in range(8)]
    e.trend.seeded = True
    e.trend.need_reconcile = False
    e.pos = pos
    return e, ex, db


@pytest.mark.asyncio
async def test_entry_blocked_when_feed_down():
    """数据源 DOWN：必须拦在开仓之前。"""
    guard = Guard(ok=False, why="数据源 DOWN 已 65s，禁止开仓")
    e, ex, db = _eng(guard=guard)
    await e._step_trend({}, 80000.0, 100.0, 1700000000000)
    assert e.pos is None, "闸门拦下时不能开仓"
    msgs = [str(d[0]) for d in db.decisions]
    assert any("开仓闸门拦截" in m for m in msgs), "必须留下被拦截的决策记录"


@pytest.mark.asyncio
async def test_stale_price_does_not_trigger_phantom_stop():
    """本地价跌穿止损、但交易所价没跌穿：判定为行情陈旧，不许平仓。"""
    pos = {"side": "long", "qty": 0.001, "entry": 100000.0, "stop": 75000.0,
           "target": 0.0, "entry_ts": 1, "bars": 0, "best": 100000.0,
           "trade_id": 1, "max_hold_bars": 10**9, "open_reason": "",
           "fee": 0.1, "signal": "trend"}
    # 本地价 70000（跌穿 75000 止损），但交易所真实价 80000
    e, ex, db = _eng(px=70000.0, ex=Ex(mark=80000.0, qty=0.001), pos=pos)
    await e._step_trend({}, 70000.0, 100.0, 1700000000000)
    assert e.pos is not None, "陈旧价造成的假止损必须被拦下，不能平仓"
    assert not ex.sold, "不应该真的卖出"
    assert any(k == "stale_price" for k, _ in db.risks)


@pytest.mark.asyncio
async def test_real_stop_still_fires():
    """交易所价确实跌穿止损：必须照常熔断平仓。"""
    pos = {"side": "long", "qty": 0.001, "entry": 100000.0, "stop": 75000.0,
           "target": 0.0, "entry_ts": 1, "bars": 0, "best": 100000.0,
           "trade_id": 1, "max_hold_bars": 10**9, "open_reason": "",
           "fee": 0.1, "signal": "trend"}
    e, ex, db = _eng(px=70000.0, ex=Ex(mark=69500.0, qty=0.001), pos=pos)
    await e._step_trend({}, 70000.0, 100.0, 1700000000000)
    assert e.pos is None, "真实跌穿必须平仓"
    assert ex.sold, "必须真的卖出"
