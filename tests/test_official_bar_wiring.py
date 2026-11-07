# -*- coding: utf-8 -*-
"""接线级验证：_step_trend 必须真的把【官方 4h K】喂给策略。

上一组测试只证明 bar_from_kline 能用。这条证明它被接进了决策路径——
否则等于写了一堆没人调用的代码（本项目历史上真实发生过）。

用持平的假价格（不产生信号），只让成交量区分本地/官方：
这样测试既验证了"喂进去的是哪根 K"，又不会牵扯进下单链路。
"""
import os
import sys
import tempfile
import time

import pytest
import pytest_asyncio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.config import AppCfg
from ai_trader.market.agg import Bar
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000


class _Hub:
    def publish(self, *_a, **_k):
        pass

    subscribers = 0


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()


class _Exec:
    """只够让 _step_trend 走完，不应被调用到下单。"""

    async def close(self):
        pass

    async def equity(self):
        return {"equity": 100.0, "position_qty": 0.0}

    async def market_order(self, *_a, **_k):
        raise AssertionError(u"本测试不该真的下单")


@pytest_asyncio.fixture
async def env():
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.sma_lookback = 5
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = Feed(100.0)
    eng = DecisionEngine(cfg, feed, _Exec(), tdb, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    # 6 根持平历史，n=5 时 state() 能算；最后一根由 bars_by_iv 送进来
    base_t = ((int(time.time() * 1000) // IV4H) * IV4H) - IV4H * 20
    eng.trend.bars = [{"t": base_t + i * IV4H, "o": 100.0, "h": 100.0,
                       "l": 100.0, "c": 100.0, "v": 1.0} for i in range(6)]
    try:
        yield eng, base_t
    finally:
        tdb.stop()


@pytest.mark.asyncio
async def test_step_trend_feeds_official_bar(env, monkeypatch):
    """取到官方 K 时，喂进策略的成交量必须是官方的，不是本地残缺量。"""
    eng, base_t = env
    t_new = base_t + 6 * IV4H          # 刚收盘的一根（新鲜）
    local_v = 3.0                      # 本地只攒到 3 个币

    official_raw = [t_new, "100.0", "100.0", "100.0", "100.0",
                    "1234.567", t_new + IV4H - 1, "0", 100, "0", "0", "0"]

    calls = []

    def fake_fetch(open_time):
        calls.append(open_time)
        return eng.trend.bar_from_kline(official_raw)

    monkeypatch.setattr(eng.trend, "fetch_official_bar", fake_fetch)

    bars = {IV4H: [Bar(interval_ms=IV4H, open_time=t_new, open=100.0,
                       high=100.0, low=100.0, close=100.0, volume=local_v,
                       closed=True)]}
    await eng._step_trend(bars, 100.0, 100.0, t_new + IV4H + 3000)

    assert calls == [t_new], u"没有去取官方 K 线（接线断了）：%r" % calls
    got = eng.trend.bars[-1]
    assert got["t"] == t_new
    assert got["v"] == pytest.approx(1234.567), (
        u"喂进去的是本地残缺量 %r，官方量 1234.567 没生效" % got["v"])


@pytest.mark.asyncio
async def test_step_trend_falls_back_when_fetch_fails(env, monkeypatch):
    """取不到官方 K 时绝不能不判断——必须退回本地 bar。"""
    eng, base_t = env
    t_new = base_t + 6 * IV4H

    monkeypatch.setattr(eng.trend, "fetch_official_bar", lambda _t: None)

    bars = {IV4H: [Bar(interval_ms=IV4H, open_time=t_new, open=100.0,
                       high=100.0, low=100.0, close=100.0, volume=7.0,
                       closed=True)]}
    await eng._step_trend(bars, 100.0, 100.0, t_new + IV4H + 3000)

    got = eng.trend.bars[-1]
    assert got["t"] == t_new, u"取不到官方 K 时整根 K 被丢弃了"
    assert got["v"] == pytest.approx(7.0), u"退回本地 bar 时应保留本地量"


@pytest.mark.asyncio
async def test_snapshot_reports_bar_source_and_volume(env, monkeypatch):
    """快照必须说清"最近一根 4h K 是哪来的"。

    为什么这条是必需的而不是装饰：
    官方与本地 bar 的【收盘价一致】，所以 SMA 完全区分不出用的是哪一份
    （第 83 轮就做过一次用 SMA 比对、看着成功但根本区分不了的验证）。
    唯一能区分两种口径的量是【成交量】：官方 8558 vs 本地 117。
    不把它报出来，"已回退到本地残缺 bar"在线上就完全不可见。
    """
    eng, base_t = env
    t_new = base_t + 6 * IV4H

    official_raw = [t_new, "100.0", "100.0", "100.0", "100.0",
                    "8558.161", t_new + IV4H - 1, "0", 100, "0", "0", "0"]
    monkeypatch.setattr(eng.trend, "fetch_official_bar",
                        lambda _t: eng.trend.bar_from_kline(official_raw))

    bars = {IV4H: [Bar(interval_ms=IV4H, open_time=t_new, open=100.0,
                       high=100.0, low=100.0, close=100.0, volume=117.73,
                       closed=True)]}
    await eng._step_trend(bars, 100.0, 100.0, t_new + IV4H + 3000)

    snap = eng._trend_snapshot
    assert snap.get("bar_src") == "official", (
        u"快照没说清来源：%r" % snap.get("bar_src"))
    assert snap.get("bar_vol") == pytest.approx(8558.161), (
        u"快照报的量不是官方量：%r" % snap.get("bar_vol"))
    assert snap.get("bar_vol_local") == pytest.approx(117.73), (
        u"快照没报本地量，看不出差异：%r" % snap.get("bar_vol_local"))


@pytest.mark.asyncio
async def test_snapshot_marks_fallback_source(env, monkeypatch):
    """回退到本地 bar 时，快照必须明确标出来 —— 不能和正常情况长得一样。"""
    eng, base_t = env
    t_new = base_t + 6 * IV4H
    monkeypatch.setattr(eng.trend, "fetch_official_bar", lambda _t: None)

    bars = {IV4H: [Bar(interval_ms=IV4H, open_time=t_new, open=100.0,
                       high=100.0, low=100.0, close=100.0, volume=42.0,
                       closed=True)]}
    await eng._step_trend(bars, 100.0, 100.0, t_new + IV4H + 3000)

    snap = eng._trend_snapshot
    assert "回退" in str(snap.get("bar_src", "")), (
        u"回退时来源没标记：%r" % snap.get("bar_src"))
    assert snap.get("bar_vol") == pytest.approx(42.0)


@pytest.mark.asyncio
async def test_stale_bar_does_not_trigger_fetch(env, monkeypatch):
    """很久以前的 K（补历史/回放）不该再发 REST 请求。"""
    eng, base_t = env
    calls = []
    monkeypatch.setattr(eng.trend, "fetch_official_bar",
                        lambda t: calls.append(t) or None)
    old_t = base_t + 6 * IV4H
    bars = {IV4H: [Bar(interval_ms=IV4H, open_time=old_t, open=100.0,
                       high=100.0, low=100.0, close=100.0, volume=5.0,
                       closed=True)]}
    await eng._step_trend(bars, 100.0, 100.0, old_t + 3 * 24 * 3600 * 1000)
    assert calls == [], u"对老旧 K 线也发起了 REST 请求：%r" % calls
