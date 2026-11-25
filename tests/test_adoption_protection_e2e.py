# -*- coding: utf-8 -*-
"""端到端：接管人工仓的那一瞬，交易所侧必须有保护单 —— 不等任何 K 线。

这是用户裁决的验收（原话：「我们做的是抄底行为，可能在 10 分钟内发生
超级大波动」）。之前的行为：接管后要等一根**已收盘 15m** 才挂保护单，
窗口最长 15 分钟。所以这条测试的判据是：

    接管完成的那一刻（还没有任何 K 线），STOP_MARKET 已经在交易所上了。

用真实的 `LiveExecutor` + `FakeBinance` 跑，而不是桩：桩能证明"调用了"，
证明不了"单真的挂在交易所上"。
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
    """够用的行情替身：`_sync_adopted_stop_order` 只读 last_price。"""

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


@pytest.mark.asyncio
async def test_adoption_places_protection_at_once_without_any_bars(fake, tmp_path):
    cfg = _cfg(fake)
    px = 60000.0
    feed = _Feed(px)
    ex = LiveExecutor(feed, cfg, fake.api_key, fake.api_secret,
                      base=fake.base())
    tdb = TradeDB(str(tmp_path / "t.db"))
    tdb.start()
    try:
        # 交易所上先有一笔"人工仓"（用真实下单造出来，带交易所生成的 id）
        fill = await ex.market_order("buy", 0.01, reason="模拟人工开仓")
        assert fill.ok, fill.reason

        eng = E.DecisionEngine(cfg, feed, ex, tdb, guard=None)
        # 引擎不知道这笔仓（数据库为空），所以对账会把它接管进 adopted_pos
        await eng._reconcile_positions()

        adopted = getattr(eng, "adopted_pos", None)
        assert adopted, "对账没有接管手工仓（前置条件不成立，测试无意义）"

        # ★ 核心断言：这一刻还没有任何 K 线（没有 15m），
        #   但交易所侧必须已经有 reduceOnly 的 STOP_MARKET。
        orders = await ex.open_orders()
        stops = [o for o in orders
                 if str(getattr(o, "type", "")).upper() == "STOP_MARKET"]
        assert stops, (
            "接管之后交易所侧没有保护单（open_orders=%r）—— "
            "这正是「等一根 15m 才挂」的裸奔窗口" % (
                [(getattr(o, "type", ""), getattr(o, "status", ""))
                 for o in orders],))
        o = stops[0]
        assert o.reduce_only, "保护单必须是 reduceOnly（否则触发会反向开仓）"
        assert o.side.upper() == "SELL", "多仓的保护单方向必须是 SELL"
        assert float(o.stop_price or 0) > 0, (
            "保护单没有触发价：%r" % (getattr(o, "stop_price", None),))
        # 触发价必须低于现价（多仓止损），否则挂上去就立刻触发
        assert float(o.stop_price) < px, (
            "多仓止损价 %.2f 不低于现价 %.2f —— 挂上去即触发" % (
                float(o.stop_price), px))
        # 手数要和仓位一致
        assert abs(float(o.qty or 0) - 0.01) < 1e-9, (
            "保护单手数 %r ≠ 仓位 0.01" % (getattr(o, "qty", None),))
    finally:
        tdb.stop()
        await ex.close()
