# -*- coding: utf-8 -*-
"""成交归属（client_id）与「不知道」三态。

2026-11-25 真钱自检单查出来的 P0，链条是：

    币安 `/fapi/v1/userTrades` 返回里**没有 `clientOrderId`**
    → `recent_fills` 取到空串
    → `classify_delta` 判 manual
    → 系统自己的成交被当「用户手工仓」接管
    → 挂单预期看到有仓就压制自己 → 永不落账、`net_r` 永远 0
    → 而「成交回报丢了」这种必须硬停的故障**永远不会硬停**

这组测试钉住两件事：
  1. 归属必须能从 `orderId` 反查补回（否则整条链又反向）
  2. 「补不出来」必须与「确认是手单」区分开（None ≠ ""）
"""
import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load                       # noqa: E402
from ai_trader.exchange.live import LiveExecutor        # noqa: E402
from ai_trader.trading import engine as E               # noqa: E402
from tools.fake_binance import FakeBinance              # noqa: E402


class _Feed(object):
    last_price = 60000.0


@pytest_asyncio.fixture
async def fake():
    f = await FakeBinance(usdt=1000.0, btc=0.0, price=60000.0).start()
    try:
        yield f
    finally:
        await f.stop()


def _cfg(fake):
    cfg = load()
    cfg.trading.mode = "live"
    cfg.trading.symbol = "BTCUSDT"
    return cfg


def _ex(fake):
    return LiveExecutor(_Feed(), _cfg(fake), fake.api_key, fake.api_secret,
                        base=fake.base())


async def _fill_one(ex, fake, client_id=""):
    """挂一张价内限价买单并让它成交，返回成交列表。"""
    od, err = await ex.place_order("buy", 0.01, order_type="LIMIT",
                                   price=59900.0, client_id=client_id)
    assert not err, "挂单失败：%s" % err
    fake.set_price(59000.0)               # 触发限价买单
    return await ex.recent_fills(hours=24)


@pytest.mark.asyncio
async def test_user_trades_really_has_no_client_id(fake):
    """先钉住交易所的形状：userTrades 的原始返回里没有 clientOrderId。

    ★ 这是本组测试的地基。哪天有人在桩里「顺手」补上这个字段，
      下面那条反查测试就再也不会失败，而线上会继续错。
    """
    ex = _ex(fake)
    try:
        await _fill_one(ex, fake, client_id="AI-pend-abc")
        rows = await ex._req(
            "GET", "/fapi/v1/userTrades",
            {"symbol": "BTCUSDT", "limit": 10})
        assert rows, "应该有成交"
        assert "clientOrderId" not in rows[0], (
            "桩不该给 userTrades 补 clientOrderId —— 真实交易所没有这个字段")
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_recent_fills_backfills_client_id_from_order_id(fake):
    """归属靠 orderId 从 allOrders 反查补回：我们的单必须认成 ours。"""
    ex = _ex(fake)
    try:
        fills = await _fill_one(ex, fake, client_id="AI-pend-abc")
        assert fills, "应该有成交"
        assert fills[0]["client_id"] == "AI-pend-abc", (
            "归属没补上：%r —— 补不上的话这笔成交会被当成人手单接管，"
            "而系统自己的仓就再也落不了账" % (fills[0]["client_id"],))
        prov, _used = E.classify_delta(fills, 0.01)
        assert prov == E.PROV_OURS, (
            "自己的成交必须判 ours（真故障方向），实际 %r" % (prov,))
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_manual_fill_still_classified_manual(fake):
    """真正的手工单（无 AI- 前缀）仍判 manual —— 别把接管改坏了。"""
    ex = _ex(fake)
    try:
        fills = await _fill_one(ex, fake, client_id="")
        prov, _used = E.classify_delta(fills, 0.01)
        assert prov == E.PROV_MANUAL, (
            "没有 AI- 前缀的成交必须判 manual（§13.2 接管、不硬停），"
            "实际 %r" % (prov,))
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_unknown_attribution_backs_up_to_unknown(fake):
    """allOrders 查不出来的成交 → client_id=None → unknown（硬停方向）。

    ★ 这里钉的是「不知道 ≠ 不是我们的」：
      把 None 当成空串，就等于**替交易所断言**「这是用户手单」，
      而真实情况可能是我们自己的成交回报丢了 —— 必须保守地停。
    """
    ex = _ex(fake)
    try:
        await _fill_one(ex, fake, client_id="AI-pend-abc")
        orig = ex._req

        async def _broken(method, path, params=None, **kw):
            if path == "/fapi/v1/allOrders":
                raise RuntimeError("allOrders 挂了")
            return await orig(method, path, params, **kw)

        ex._req = _broken
        fills = await ex.recent_fills(hours=24)
        assert fills and fills[0]["client_id"] is None, (
            "补不出归属必须是 None（不知道），不能是空串（确认是手单）：%r"
            % (fills[0]["client_id"] if fills else None,))
        prov, _used = E.classify_delta(fills, 0.01)
        assert prov == E.PROV_UNKNOWN, (
            "归属未知必须落 unknown（硬停方向），实际 %r" % (prov,))
    finally:
        await ex.close()


def test_classify_delta_three_way():
    """纯函数层：ours / manual / unknown 三态各归各位。"""
    ours = [{"side": "BUY", "qty": 0.01, "client_id": "AI-pend-x"}]
    man = [{"side": "BUY", "qty": 0.01, "client_id": ""}]
    unk = [{"side": "BUY", "qty": 0.01, "client_id": None}]
    assert E.classify_delta(ours, 0.01)[0] == E.PROV_OURS
    assert E.classify_delta(man, 0.01)[0] == E.PROV_MANUAL
    assert E.classify_delta(unk, 0.01)[0] == E.PROV_UNKNOWN


def test_unknown_wins_over_manual_when_mixed():
    """一半能认出来、一半认不出来 → 仍按 unknown（不能挑着相信）。"""
    mixed = [{"side": "BUY", "qty": 0.01, "client_id": ""},
             {"side": "BUY", "qty": 0.02, "client_id": None}]
    assert E.classify_delta(mixed, 0.03)[0] == E.PROV_UNKNOWN


def test_ours_wins_over_unknown_when_mixed():
    """只要有一笔能确认是我们的 → ours（更严重的结论优先）。"""
    mixed = [{"side": "BUY", "qty": 0.01, "client_id": "AI-pend-x"},
             {"side": "BUY", "qty": 0.02, "client_id": None}]
    assert E.classify_delta(mixed, 0.03)[0] == E.PROV_OURS


# --------------------------------------------- 市价单也必须能认领
#
# 2026-11-25 生产实测：手工平仓那笔成交的 client_id 是交易所自己生成的
# `8sgedyZgQSvDXiO9Sn1luN` —— 因为 `market_order()` 当时不下发
# `newClientOrderId`。而市价单正是**平仓和止损**：
# 一笔止损成交被判成人工仓接管之后，§13.2 规定接管仓不判策略出场，
# 等于止损失效。「我们不知道自己平了仓」必须硬停，不能静默接管。

@pytest.mark.asyncio
async def test_market_order_can_carry_client_id(fake):
    """市价单带上 AI- 前缀 → 成交能被认领成 ours。"""
    ex = _ex(fake)
    try:
        fill = await ex.market_order("buy", 0.01, reason="测试开仓",
                                     client_id="AI-123-1")
        assert fill.ok, fill.reason
        fills = await ex.recent_fills(hours=24)
        assert fills and fills[0]["client_id"] == "AI-123-1", (
            "市价单的归属没补上：%r —— 平仓/止损成交会被当人工仓接管"
            % (fills[0]["client_id"] if fills else None,))
        prov, _used = E.classify_delta(fills, 0.01)
        assert prov == E.PROV_OURS
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_market_order_without_client_id_is_not_ours(fake):
    """不下发 id 时交易所会给个随机串 → 不是 ours（钉住现状，说明为什么要传）。"""
    ex = _ex(fake)
    try:
        fill = await ex.market_order("buy", 0.01, reason="测试")
        assert fill.ok, fill.reason
        fills = await ex.recent_fills(hours=24)
        cid = fills[0]["client_id"]
        assert cid and not str(cid).startswith(E.AI_CLIENT_PREFIX), (
            "不传 newClientOrderId 时应当拿到交易所生成的随机 id，实际 %r"
            % (cid,))
        assert E.classify_delta(fills, 0.01)[0] == E.PROV_MANUAL
    finally:
        await ex.close()


def test_close_client_id_is_short_enough_and_prefixed():
    """平仓专用的 id：够短、纯 ASCII、带 AI- 前缀（币安硬约束 + 归因判据）。"""
    cid = E._close_client_id("pend", "下跌三引线假突破限价单|up|17|58930.0")
    assert cid.startswith("AI-"), cid
    assert len(cid) <= 35, "币安要求 clientOrderId < 36 字符：%r(%d)" % (
        cid, len(cid))
    assert cid.isascii(), "中文/竖线不能进 clientOrderId：%r" % (cid,)
    # 同一个预期必须算出逐字相同的 id（幂等重放与撤单都靠这一点）
    assert cid == E._close_client_id(
        "pend", "下跌三引线假突破限价单|up|17|58930.0")
    # 不同预期不能撞
    assert cid != E._close_client_id(
        "pend", "下跌三引线假突破限价单|up|18|58930.0")
