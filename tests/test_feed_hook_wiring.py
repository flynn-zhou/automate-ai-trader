# -*- coding: utf-8 -*-
"""生产触发链路：feed.bar_hooks -> _on_bar_sync -> create_task -> _step -> _step_trend。

前面 test_engine_http_e2e.py 是【直接调用】_step_trend()，绕过了真正的触发器。
但在生产里，引擎唯一被唤醒的方式是 feed 在每根 3s bar 收盘时调用 bar_hooks。
这条链路从没被测过——而它断了，整个策略就是死的，且日志上看不出来：
feed 照常采、看板照常动、K 线照常入库，只有"决策"永远不发生。
"""
import asyncio
import os
import sys
import tempfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.market.agg import Bar
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000
IV3S = 3000


class _Hub:
    def __init__(self):
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


class _Agg:
    """_step() 用 agg.current(3000).close 取价。"""
    def __init__(self, px):
        self._bar = Bar(interval_ms=IV3S, open_time=0, open=px, high=px,
                        low=px, close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._bar if iv == IV3S else None


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
    cfg = AppCfg()
    cfg.trading.mode = "paper"
    cfg.trading.strategy = "trend"
    cfg.trading.sma_lookback = 5
    cfg.trading.paper_balance = 100.0
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = Feed(30000.0)
    ex = PaperExecutor(feed, cfg)
    eng = DecisionEngine(cfg, feed, ex, tdb, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    eng.attach()          # <- 真正把引擎挂到 feed 上
    try:
        yield eng, feed, tdb
    finally:
        tdb.stop()


def _hist(prices):
    return [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
            for i, p in enumerate(prices)]


def _bars(prices):
    return [Bar(interval_ms=IV4H, open_time=i * IV4H, open=p, high=p,
                low=p, close=p, volume=1.0, closed=True)
            for i, p in enumerate(prices)]


async def _fire(feed, prices, px):
    """模拟 feed 收盘一根 3s bar 时调用 bar_hooks 的动作。"""
    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(prices)
    last = Bar(interval_ms=IV3S, open_time=0, open=px, high=px, low=px,
               close=px, volume=1.0, closed=True)
    before = len(asyncio.all_tasks())
    for hook in feed.bar_hooks:
        hook(snap, last)
    # hook 里 create_task 了，等它跑完
    for _ in range(200):
        await asyncio.sleep(0.01)
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()]
        if len(pending) <= before:
            break
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_hook_is_actually_registered(env):
    """attach() 必须把引擎挂到 bar_hooks 上，否则引擎永远不会被唤醒。"""
    eng, feed, tdb = env
    assert feed.bar_hooks, "attach() 没有注册任何 hook，引擎永远不会被触发"
    assert any("_on_bar" in getattr(h, "__name__", "") for h in feed.bar_hooks)


@pytest.mark.asyncio
async def test_hook_drives_engine_and_appends_4h_bar(env):
    """通过 hook 触发时，引擎必须真的把 4h bar 追加进策略。"""
    eng, feed, tdb = env
    prices = [100.0, 99, 98, 97, 96, 95, 94, 93, 30000.0]
    eng.trend.bars = _hist(prices[:-1])
    n_before = len(eng.trend.bars)

    await _fire(feed, prices, 30000.0)

    assert len(eng.trend.bars) == n_before + 1, (
        "引擎被触发了，但 4h K 线没有追加——信号永远不会产生")
    assert eng.trend.bars[-1]["c"] == 30000.0


@pytest.mark.asyncio
async def test_hook_produces_a_decision_row(env):
    """每根 bar 都要留一行决策记录（三条铁律之一），且是通过 hook 产生的。"""
    eng, feed, tdb = env
    prices = [100.0, 99, 98, 97, 96, 95, 94, 93, 30000.0]
    eng.trend.bars = _hist(prices[:-1])
    await _fire(feed, prices, 30000.0)
    await asyncio.sleep(0.4)
    assert eng.decisions >= 1, "hook 触发了但没有产生任何决策记录"


@pytest.mark.asyncio
async def test_engine_error_does_not_kill_the_loop(env, monkeypatch):
    """决策步骤抛异常必须被吞掉并记录，不能让主循环死掉。

    7x24 的关键：任何单次决策失败都只是那一轮没做判断，
    下一根 bar 还会继续。如果异常逃出去，事件循环的任务会静默消失。
    """
    eng, feed, tdb = env

    async def boom(*a, **k):
        raise RuntimeError("模拟决策内部崩溃")

    monkeypatch.setattr(eng, "_step", boom)
    prices = [100.0, 99, 98, 97, 96, 95, 94, 93, 30000.0]
    eng.trend.bars = _hist(prices[:-1])

    await _fire(feed, prices, 30000.0)      # 不应抛出

    assert "模拟决策内部崩溃" in (eng.last_error or ""), (
        "异常没有被记录到 last_error，出问题时无从排查")


@pytest.mark.asyncio
async def test_second_hook_call_is_dropped_when_busy(env, monkeypatch):
    """上一轮还没跑完时，新的 bar 应被丢弃而不是排队（防雪崩）。"""
    eng, feed, tdb = env
    started = []

    async def slow(*a, **k):
        started.append(1)
        await asyncio.sleep(0.5)

    monkeypatch.setattr(eng, "_step", slow)
    prices = [100.0, 99, 98, 97, 96, 95, 94, 93, 30000.0]
    eng.trend.bars = _hist(prices[:-1])

    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(prices)
    last = Bar(interval_ms=IV3S, open_time=0, open=30000.0, high=30000.0,
               low=30000.0, close=30000.0, volume=1.0, closed=True)
    for hook in feed.bar_hooks:
        hook(snap, last)
    await asyncio.sleep(0.05)
    for hook in feed.bar_hooks:          # 第二次，此时锁还占着
        hook(snap, last)
    await asyncio.sleep(0.8)

    assert len(started) == 1, (
        "锁被占时仍然开始了新一轮，会导致决策排队雪崩（实际 %d 次）" % len(started))
