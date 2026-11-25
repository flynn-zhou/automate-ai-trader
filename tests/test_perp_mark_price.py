# -*- coding: utf-8 -*-
"""永续执行器必须暴露 mark_price（记账口径），止损复核另走最新成交价。

背景：引擎的熔断止损判的是【本地行情缓存价】。行情断线时它是陈旧的，
用陈旧价误判会把健康仓位按市价卖掉，之后要等下一次金叉才能回场（可能几周）。

★ A2（2026-11-25 交易域审查）改掉了复核价的来源：
  复核原来走 `mark_price()`（premiumIndex.markPrice），现在走
  `last_trade_price()`（ticker/price 的最新成交价）。

  原因是可复现的：触发价来自成交（3 秒聚合收盘），复核价来自标记价，
  两者不同源。快跌时 last 先动、mark 慢半拍 → 「本地已触发、交易所未触发」
  → 判「行情陈旧」→ **拒绝平仓**，而那正是最该平的那一秒。
  而且币安 STOP/STOP_MARKET 的触发口径本来就是 CONTRACT_PRICE（成交价），
  不是标记价 —— 拿标记价复核，口径本身就对不上。

  `mark_price()` 则**仍然必须有**：平仓记账要用权威价（那个端点与强平/风控
  同一口径），不能用可能偏掉的成交价污染账本。所以本文件保留了
  "premiumIndex 端点、不签名"这两条断言。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load
from ai_trader.exchange.live import LiveExecutor


class _Feed:
    last_price = 70000.0


def _mk(transport=None):
    cfg = load()
    ex = LiveExecutor(_Feed(), cfg, "k", "s")
    if transport is not None:
        ex._transport = transport
    return ex


@pytest.mark.asyncio
async def test_live_executor_has_mark_price():
    """LiveExecutor 必须实现 mark_price（缺了就等于幽灵止损保护失效）。"""
    ex = _mk()
    assert hasattr(ex, "mark_price"), "永续执行器缺少 mark_price，幽灵止损保护会静默失效"
    assert callable(ex.mark_price)


@pytest.mark.asyncio
async def test_mark_price_uses_premium_index():
    """必须走 premiumIndex 拿 markPrice，而不是 ticker/price。"""
    seen = {}

    async def transport(method, path, params, signed):
        seen["path"] = path
        seen["signed"] = signed
        return {"symbol": "BTCUSDT", "markPrice": "76543.21", "indexPrice": "76540.0"}

    ex = _mk(transport)
    px = await ex.mark_price()
    assert abs(px - 76543.21) < 0.01
    assert seen["path"] == "/fapi/v1/premiumIndex", "端点选错：%s" % seen["path"]
    assert seen["signed"] is False, "行情类接口不需要签名，签名会多一次密钥暴露"


@pytest.mark.asyncio
async def test_mark_price_falls_back_to_feed_when_empty():
    """交易所没给价时退回本地价，绝不能返回 0（返回 0 会让止损逻辑误判）。"""

    async def transport(method, path, params, signed):
        return {"symbol": "BTCUSDT"}

    ex = _mk(transport)
    px = await ex.mark_price()
    assert px == pytest.approx(70000.0)


@pytest.mark.asyncio
async def test_engine_confirm_stop_uses_last_trade_price():
    """端到端：引擎复核止损价时，取的是**最新成交价**（A2）。

    这条以前断言的是 markPrice —— 那是 A2 之前的设计。改口径的理由见
    文件头：触发价来自成交，复核也必须来自成交，否则快跌时两者必然
    分叉，而分叉的后果是"拒绝平仓"。
    """
    from ai_trader.trading.engine import DecisionEngine

    seen = {"n": 0, "path": ""}

    async def transport(method, path, params, signed):
        seen["n"] += 1
        seen["path"] = path
        return {"symbol": "BTCUSDT", "price": "76000.0"}

    ex = _mk(transport)
    cfg = load()
    feed = _Feed()
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.ex = ex
    eng.tcfg = cfg.trading
    eng.feed = feed

    px = await DecisionEngine._confirm_stop_price(eng, 60000.0)
    assert seen["n"] == 1, "引擎没有真的去向交易所复核"
    assert seen["path"] == "/fapi/v1/ticker/price", \
        "复核用了 %s —— 与触发价不同源" % seen["path"]
    assert px == pytest.approx(76000.0)


@pytest.mark.asyncio
async def test_last_trade_price_endpoint_is_unsigned():
    """行情类接口不签名（少一次密钥暴露）。"""
    seen = {}

    async def transport(method, path, params, signed):
        seen["signed"] = signed
        return {"symbol": "BTCUSDT", "price": "76123.5"}

    ex = _mk(transport)
    assert await ex.last_trade_price() == pytest.approx(76123.5)
    assert seen["signed"] is False
