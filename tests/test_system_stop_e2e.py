# -*- coding: utf-8 -*-
"""端到端：**策略仓**（我们自己的仓）在交易所侧必须有地板单。

与 `test_adoption_protection_e2e` 是姊妹测试，但测的是更常见的那条路：
策略自己开的仓。此前它只有进程内 3 秒轮询的保护 —— 进程一挂就裸奔。

用真实的 `LiveExecutor` + 严格 `FakeBinance`（它忠实模拟了 -4120：
条件单在旧端点会被拒），所以这条测试同时钉住两件事：
  1. 地板单**真的挂在交易所上**（不是"调用了挂单方法"）
  2. 走的是 Algo 端点（走旧端点会被 -4120 拒掉，测试立刻红）
"""
from __future__ import annotations

import os
import sys
import types

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg                       # noqa: E402
from ai_trader.exchange.live import LiveExecutor          # noqa: E402
from ai_trader.trade_db import TradeDB                    # noqa: E402
from ai_trader.trading import engine as E                 # noqa: E402
from tools.fake_binance import FakeBinance                # noqa: E402


class _Feed(object):
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = types.SimpleNamespace(publish=lambda m: None)
        self.src = "ws"
        self.agg = types.SimpleNamespace(current=lambda iv: None)


def _cfg(fake):
    cfg = AppCfg()
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.exec_market = "live"
    cfg.trading.strategy = "pending"
    cfg.trading.api_key = fake.api_key
    cfg.trading.api_secret = fake.api_secret
    cfg.trading.testnet = False
    return cfg


@pytest_asyncio.fixture
async def fake():
    srv = FakeBinance(usdt=1000.0, btc=0.0, price=60000.0)
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


def _pos(entry=60000.0, stop=58000.0, qty=0.01, ts=1795600000000):
    return {"side": "long", "qty": qty, "entry": entry, "stop": stop,
            "target": 66000.0, "entry_ts": ts, "open_reason": "策略开仓",
            "paper": 0}


@pytest.mark.asyncio
async def test_system_stop_lands_on_the_exchange_via_algo_endpoint(fake):
    cfg = _cfg(fake)
    feed = _Feed(60000.0)
    ex = LiveExecutor(feed, cfg, fake.api_key, fake.api_secret,
                      base=fake.base())
    tdb = TradeDB(str(__import__("tempfile").mkdtemp() + "/t.db"))
    tdb.start()
    try:
        eng = E.DecisionEngine(cfg, feed, ex, tdb, guard=None)
        eng.pos = _pos()

        await eng._maintain_system_protection(60000.0)

        # ★ 判据：交易所的**条件单簿**里真有这张单
        algos = await ex.open_algo_orders()
        assert algos, ("策略仓在交易所侧没有地板单 —— 进程一挂就裸奔。"
                       "（若这里为空，先看是不是走了旧端点被 -4120 拒了）")
        o = algos[0]
        assert o.type.upper() == "STOP_MARKET"
        assert o.reduce_only, "地板单必须 reduceOnly（否则触发会反向开仓）"
        assert o.side.upper() == "SELL", "多仓的保护单方向必须是 SELL"
        assert float(o.stop_price or 0) > 0
        assert float(o.stop_price) < 60000.0, "止损价不低于现价 → 挂上即触发"
        assert abs(float(o.qty or 0) - 0.01) < 1e-9
        # 幂等：再跑一次不许挂第二张
        await eng._maintain_system_protection(60000.0)
        assert len(await ex.open_algo_orders()) == 1, "重复挂了第二张地板单"

        # 平仓后必须撤掉（不能白占保证金）
        eng.pos = None
        await eng._maintain_system_protection(60000.0)
        assert await ex.open_algo_orders() == [], "平仓后地板单还挂着"
    finally:
        tdb.stop()
        await ex.close()


@pytest.mark.asyncio
async def test_old_endpoint_really_rejects_conditional_orders(fake):
    """钉住假币安的忠实度：旧端点对 STOP_MARKET 必须 -4120。

    ★ 这条测试保护的是**其他测试的可信度**：如果假币安放行了旧端点，
      「条件单走 Algo」那组测试就永远不会因为回归而变红。
    """
    cfg = _cfg(fake)
    ex = LiveExecutor(_Feed(60000.0), cfg, fake.api_key, fake.api_secret,
                      base=fake.base())
    try:
        # 绕开 place_order 的分流，**直接**打旧端点：要验的正是
        # "旧端点不接受条件单"这个交易所事实本身。
        from ai_trader.exchange.live import LiveError
        with pytest.raises(LiveError) as ei:
            await ex._req("POST", "/fapi/v1/order", {
                "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET",
                "quantity": "0.010", "stopPrice": "58000.0",
                "reduceOnly": "true", "newClientOrderId": "AI-SLP-old"})
        assert "-4120" in str(ei.value), (
            "假币安没有忠实模拟 -4120（实际：%s）—— 那么「迁回旧端点」"
            "这个回归在任何测试里都抓不住" % (ei.value,))
    finally:
        await ex.close()
