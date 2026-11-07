# -*- coding: utf-8 -*-
"""短暂的权益查询失败，不能丢掉一次进场机会。

为什么这条这么重要（本轮实测数字）：
  这个策略 85% 的收益来自最好的 3 笔交易，漏掉 1 笔就少 46% 的利润，
  漏掉 5 笔 6.84 年白干。
  所以"进场必须万无一失"不是口号，是有明确价格的。

上一轮我加了「权益过期时阻止开仓」——那是对的（不能不知道有多少钱就下注）。
但它有一个风险：如果权益查询恰好在 4h 上穿那一刻失败，
会不会把这笔进场永久丢掉？

关键事实：want_entry 是无状态的，只看最近两根已收盘 4h K 线的上穿。
所以在那根 K 线内，每 3 秒都会重新给出信号——
只要权益恢复，下一秒就该进场。

如果这里写错了（比如失败时把 need_reconcile 或某个标志消费掉），
用户就会"信号到了、钱也在、但就是没买"，而且没有任何报错。
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
        self.eq_fails = False

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty))
        if side == "buy":
            self.held += qty
        else:
            self.held = max(0.0, self.held - qty)
        return Fill(qty, 100.0)

    async def equity(self):
        if self.eq_fails:
            raise RuntimeError("HTTP 503 超时")
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
                "position_qty": self.held}

    async def position(self):
        return {"qty": self.held, "side": "buy" if self.held else "",
                "entry": 100.0 if self.held else None}

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
    eng = DecisionEngine(cfg, Feed(100.0), ex, tdb, guard=None)
    eng.ex = ex
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    # 前 6 根在均线下方，最后一根上穿（这是"上穿事件"那一根）
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
async def test_transient_equity_failure_does_not_lose_the_entry(env):
    "「」权益查询失败一次 -> 恢复后必须照常进场。「」"
    eng, tdb, ex = env
    assert eng.trend.want_entry(200.0) is not None, "前提：这根应该是上穿"

    # 第一次：权益查不到
    ex.eq_fails = True
    await eng._step(_snap())
    assert not [o for o in ex.orders if o[0] == "buy"], \
        "权益未知时不该下单（这是上一轮修对的，不能退回去）"

    # 第二次：权益恢复，仍在同一根 4h 内
    ex.eq_fails = False
    await eng._step(_snap())
    buys = [o for o in ex.orders if o[0] == "buy"]
    assert buys, (
        "权益恢复后没有进场。信号还在（上穿事件在整根 4h 内都成立），"
        "钱也在，但就是没买——而且不会有任何报错。"
        "按实测，漏掉一次进场等于少 46% 的利润。")


@pytest.mark.asyncio
async def test_entry_signal_persists_across_steps_within_the_bar(env):
    """同一根 4h K 线内反复调用，信号必须一直存在（否则重试没有意义）。"""
    eng, tdb, ex = env
    for k in range(5):
        assert eng.trend.want_entry(200.0) is not None, \
            "第 %d 次调用时信号消失了——那样「权益失败后重试」就无从谈起" % (k + 1)
