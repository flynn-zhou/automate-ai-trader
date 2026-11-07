# -*- coding: utf-8 -*-
"""持仓漂移会不会被发现？

实盘里持仓和数据库对不上有四种常见原因：
  1. 下单请求超时，但交易所其实已经成交（回报丢了）
  2. 用户自己在手机 App 上买/卖
  3. 部分成交
  4. 交易所强平/风控

现在 reconcile_live() 只在【启动时】调用一次，之后再也不对账。
所以上面任何一种情况发生后，引擎会一直按错误的持仓跑，
直到下次重启——而它可能几周都不重启。
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


class SpotEx:
    """执行器桩：交易所那边有币，但引擎不知道。"""
    name = "spot"

    def __init__(self, real_qty):
        self.real_qty = real_qty
        self.orders = []

    async def market_order(self, side, qty, reason="", reduce_only=False):
        class F:
            def __init__(self, q):
                self.ok = True
                self.price = 77815.0
                self.qty = q
                self.fee = 0.0
                self.order_id = "1"
                self.status = "filled"
                self.reason = ""
        self.orders.append((side, qty))
        return F(qty)

    async def equity(self):
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
                "position_qty": self.real_qty}

    async def position(self):
        return {"qty": self.real_qty, "side": "long" if self.real_qty else "",
                "entry": None}

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
    feed = Feed(77815.0)
    ex = SpotEx(real_qty=0.0)
    eng = DecisionEngine(cfg, feed, ex, tdb, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    try:
        yield eng, ex, tdb
    finally:
        tdb.stop()


def _uptrend(eng):
    hist = [100.0, 99, 98, 97, 96, 95, 94, 93]
    eng.trend.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
                      for i, p in enumerate(hist)]
    eng.trend.last_eval_bar = 0
    return hist + [77815.0]


@pytest.mark.asyncio
async def test_drift_is_not_detected_without_periodic_reconcile(env):
    """当前行为：交易所突然多出仓位，引擎毫无察觉。"""
    eng, ex, tdb = env
    prices = _uptrend(eng)
    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(prices)

    # 引擎认为空仓
    assert eng.pos is None

    # 交易所那边突然有 0.002 BTC（超时成交 / 用户手动买 / 部分成交）
    ex.real_qty = 0.002

    # 跑 20 根 3s K 线（1 分钟）
    for _ in range(20):
        await eng._step(snap)

    # 引擎现在的持仓是怎么来的？两种可能：
    #   a) 对账发现漂移，接管了那 0.002 BTC   -> 正确
    #   b) 它以为空仓，于是【又买了一次】      -> 就是我们要防的事故
    assert eng.pos is not None, "引擎完全没有察觉交易所已有持仓"
    assert eng.pos["qty"] == pytest.approx(ex.real_qty, rel=1e-6), (
        "引擎记录 %.6f BTC，交易所实际 %.6f BTC。"
        "它没有对账，而是按「空仓」又开了一次仓——"
        "实盘里这意味着仓位翻倍而止损只保护一半。已下过的单：%r"
        % (eng.pos["qty"], ex.real_qty, ex.orders))
    assert not ex.orders, (
        "引擎在交易所已有 %.6f BTC 的情况下又下了单：%r"
        % (ex.real_qty, ex.orders))


@pytest.mark.asyncio
async def test_confirm_periodic_reconcile_actually_runs(env):
    """证明是【周期对账】发现了漂移，而不是别的路径碰巧救了场。"""
    eng, ex, tdb = env
    prices = _uptrend(eng)
    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(prices)

    ex.real_qty = 0.002

    calls = {"n": 0}
    real = eng.reconcile_live

    async def spy():
        calls["n"] += 1
        return await real()

    eng.reconcile_live = spy

    await eng._step(snap)
    assert calls["n"] == 1, "首次 _step 就应该对账一次"
    assert eng.pos is not None and eng.pos["qty"] == pytest.approx(0.002, rel=1e-6)
    assert not ex.orders, "接管后不应该再下单"

    # 5 分钟内不会再对账（避免每根 K 线都打一次交易所）
    await eng._step(snap)
    await eng._step(snap)
    assert calls["n"] == 1, "5 分钟内重复对账了 %d 次，会浪费限频" % calls["n"]


@pytest.mark.asyncio
async def test_reconcile_failure_does_not_stop_trading(env):
    """对账失败不能中断交易：拿不到真实持仓时，保持认知比停下来更安全。"""
    eng, ex, tdb = env
    prices = _uptrend(eng)
    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(prices)

    async def boom():
        raise RuntimeError("网络断了")

    eng.reconcile_live = boom

    # 不应该抛出，也不应该卡死
    await eng._step(snap)
    assert eng.last_error is None or "网络断了" not in str(eng.last_error), (
        "对账异常被错误地记成了引擎错误")


@pytest.mark.asyncio
async def test_paper_mode_does_not_reconcile(env):
    """纸面模式没有交易所持仓，不该浪费请求。"""
    eng, ex, tdb = env
    ex.name = "paper"
    prices = _uptrend(eng)
    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(prices)

    calls = {"n": 0}

    async def spy():
        calls["n"] += 1

    eng.reconcile_live = spy
    await eng._step(snap)
    assert calls["n"] == 0, "纸面模式不该调用实盘对账"
