# -*- coding: utf-8 -*-
"""永续执行器必须暴露 mark_price，否则幽灵止损保护在永续模式下静默失效。

背景：引擎的熔断止损判的是【本地行情缓存价】。行情断线时它是陈旧的，
用陈旧价误判会把健康仓位按市价卖掉，之后要等下一次金叉才能回场（可能几周）。
引擎在触发止损时会调 ex.mark_price() 复核一次——

但 _confirm_stop_price 在拿不到这个方法时返回 0.0（表示"无法确认，按本地价执行"），
于是【永续模式下这道保护等于不存在】。而用户账户正是永续账户。

注意端点：用 /fapi/v1/premiumIndex 而不是 /fapi/v1/ticker/price。
前者返回真正的 markPrice（与强平/风控同口径），后者是最近成交价。
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
async def test_engine_confirm_stop_uses_perp_mark_price():
    """端到端：引擎复核止损价时，永续执行器确实被调用。"""
    from ai_trader.trading.engine import DecisionEngine

    called = {"n": 0}

    async def transport(method, path, params, signed):
        called["n"] += 1
        return {"markPrice": "76000.0"}

    ex = _mk(transport)
    cfg = load()
    feed = _Feed()
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.ex = ex
    eng.tcfg = cfg.trading
    eng.feed = feed

    px = await DecisionEngine._confirm_stop_price(eng, 60000.0)
    assert called["n"] == 1, "引擎没有真的去向交易所复核"
    assert px == pytest.approx(76000.0)
