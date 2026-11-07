# -*- coding: utf-8 -*-
"""下单持久失败时，引擎会以多快的频率重试？

这不是理论问题：现货账户余额不足、最小名义不满足、或者交易所临时拒单时，
如果每根 3s K 线都重下一次，就是 28,800 次/天。
币安的限频会把 IP 封掉——那不是亏钱的问题，是彻底不能再交易。

对比参照：Guard 里对"连亏"有冷却（cooldown_min），
对"下单失败"却什么都没有。失败比连亏更频繁，反而没有刹车。
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
    def __init__(self, ok=False, reason="insufficient balance"):
        self.ok = ok
        self.reason = reason
        self.status = "REJECTED"
        self.order_id = None
        self.price = 77815.0
        self.qty = 0.0
        self.fee = 0.0


class BrokenExecutor:
    """永远拒单的执行器，用来测失败路径。"""
    name = "spot"

    def __init__(self):
        self.calls = []

    async def market_order(self, side, qty, reason=""):
        self.calls.append((side, qty))
        return Fill(ok=False)

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
    feed = Feed(77815.0)
    ex = BrokenExecutor()
    eng = DecisionEngine(cfg, feed, ex, tdb, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    try:
        yield eng, ex, tdb
    finally:
        tdb.stop()


def _setup_uptrend(eng):
    """构造一个「价格刚上穿均线」的状态，让 want_entry 必然成立。"""
    # 前 8 根低位横盘，最后一根跳到高位 => 上穿
    hist = [100.0, 99, 98, 97, 96, 95, 94, 93]
    eng.trend.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
                      for i, p in enumerate(hist)]
    eng.trend.last_eval_bar = 0
    return hist + [77815.0]


@pytest.mark.asyncio
async def test_failed_order_is_not_retried_every_bar(env):
    """下单失败后必须有退避：不能每根 3s K 线都重下一次。"""
    eng, ex, tdb = env
    prices = _setup_uptrend(eng)

    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(prices)

    # 模拟 10 根 3s bar（30 秒）连续触发
    for _ in range(10):
        await eng._step(snap)

    n = len(ex.calls)
    assert n <= 2, (
        "30 秒内下了 %d 次单。按这个速率是一天 %d 次——币安会直接封 IP。"
        "下单失败必须有退避。" % (n, n * 2880))


@pytest.mark.asyncio
async def test_order_failure_is_recorded_as_risk(env):
    """失败要留痕，否则事后不知道第一笔为什么没成交。"""
    eng, ex, tdb = env
    prices = _setup_uptrend(eng)
    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(prices)
    await eng._step(snap)
    await eng._step(snap)
    # log_risk 是异步入队（单写者 + 后台线程），必须等它落盘再读。
    # 直接在同一库上抢读会读到空表，然后误判成"没记风险"。
    tdb.stop()
    import sqlite3
    db = sqlite3.connect(tdb._path, timeout=5)
    try:
        kinds = [r[0] for r in db.execute("SELECT kind FROM risk_events")]
    finally:
        db.close()
    assert "order_failed" in kinds, "下单失败没有记 risk 事件"
