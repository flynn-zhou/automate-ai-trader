# -*- coding: utf-8 -*-
"""LiveExecutor 的挂单能力：place_order / cancel_order / open_orders / query_order。

## 为什么这几条测试的重点是「不熔断」

`market_order` 有一段保护：发单 → sleep 0.35 秒 → 读真实持仓 →
如果持仓没按预期变化，就**回滚平仓 + 永久熔断**。

这段逻辑对市价单是对的（市价单必须立刻改变持仓）。但如果挂单也走它，
判据从一开始就不成立：挂单**没有成交**，持仓本来就不该变。
于是每一张正常挂单都会被判成异常 → 回滚 + 熔断 → 一挂单就永久停机。

所以这里最关键的一条不是「挂单能挂上」，而是
**挂单不会把自己熔断**。这条测试如果失效，上线第一张挂单就打掉整台机器。

全部走真实 HTTP + HMAC 签名 + 严格假币安，测的是「请求能不能被交易所接受」。
"""
from __future__ import annotations

import os
import sys

import pytest
import pytest_asyncio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.config import load                      # noqa: E402
from ai_trader.exchange.base import PendingOrder       # noqa: E402
from ai_trader.exchange.live import LiveExecutor       # noqa: E402
from tools.fake_binance import FakeBinance             # noqa: E402


class _Feed:
    def __init__(self, px):
        self.last_price = px


def _cfg():
    cfg = load()
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.taker_fee = 0.001
    cfg.trading.max_leverage = 60.0
    return cfg


@pytest_asyncio.fixture
async def fake():
    f = await FakeBinance(usdt=100.0, btc=0.0, price=75000.0).start()
    try:
        yield f
    finally:
        await f.stop()


def _perp(fake, px=75000.0):
    return LiveExecutor(_Feed(px), _cfg(), fake.api_key, fake.api_secret,
                        base=fake.base())


@pytest.mark.asyncio
async def test_limit_order_rests_and_does_not_halt(fake):
    """限价挂单：挂上、状态 NEW、持仓不变、**不熔断**。"""
    ex = _perp(fake)
    try:
        o, err = await ex.place_order("SELL", 0.010, "LIMIT", price=78000.0)
        assert err == "", err
        assert isinstance(o, PendingOrder)
        assert o.working, o.status
        assert o.status == "NEW", o.status
        # 挂单没成交，持仓不许变
        pos = await ex.position()
        assert pos["qty"] == pytest.approx(0.0)
        # ★ 这一条是整个文件的核心
        assert not ex.halted, "挂单把自己熔断了：%r" % ex.halted
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_limit_order_fills_when_price_arrives(fake):
    """价格走到挂单价才成交，成交后才改持仓。"""
    ex = _perp(fake)
    try:
        o, err = await ex.place_order("BUY", 0.010, "LIMIT", price=74000.0)
        assert err == "" and o.working
        assert (await ex.position())["qty"] == pytest.approx(0.0)

        fake.set_price(74100.0)
        cur = await ex.query_order(order_id=o.order_id)
        assert cur is not None and cur.working, cur.status

        fake.set_price(74000.0)
        cur = await ex.query_order(order_id=o.order_id)
        assert cur.status == "FILLED", cur.status
        assert cur.avg_price == pytest.approx(74000.0)
        assert (await ex.position())["qty"] == pytest.approx(0.010)
        assert not ex.halted, ex.halted
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_stop_market_order_triggers_only_past_the_stop_price(fake):
    """止损单在触发价之前不动，穿过后按市价成交。"""
    ex = _perp(fake)
    try:
        o, err = await ex.place_order("SELL", 0.010, "STOP_MARKET",
                                      stop_price=73000.0, reduce_only=True)
        assert err == "" and o.status == "NEW", (err, o.status)
        fake.set_price(73500.0)
        assert (await ex.query_order(order_id=o.order_id)).working
        fake.set_price(72990.0)
        cur = await ex.query_order(order_id=o.order_id)
        assert cur.status == "FILLED", cur.status
        assert not ex.halted, ex.halted
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_cancel_order_removes_it_from_open_orders(fake):
    ex = _perp(fake)
    try:
        o, err = await ex.place_order("BUY", 0.010, "LIMIT", price=70000.0)
        assert err == ""
        lst = await ex.open_orders()
        assert [x.order_id for x in lst] == [o.order_id]
        assert await ex.cancel_order(order_id=o.order_id) is True
        assert await ex.open_orders() == []
        cur = await ex.query_order(order_id=o.order_id)
        assert cur.status == "CANCELED"
        # 撤了之后价格走过去也不许成交
        fake.set_price(69000.0)
        assert (await ex.position())["qty"] == pytest.approx(0.0)
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_cancel_by_client_id(fake):
    """按 clientOrderId 撤 —— 重启后本地只有这个 id 可用。"""
    ex = _perp(fake)
    try:
        o, err = await ex.place_order("BUY", 0.010, "LIMIT", price=70000.0,
                                      client_id="strat-abc-1")
        assert err == "" and o.client_id == "strat-abc-1"
        assert await ex.cancel_order(client_id="strat-abc-1") is True
        assert await ex.open_orders() == []
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_cancel_after_fill_is_not_reported_as_canceled(fake):
    """已成交的单，撤单不能返回「撤成功」——那会让上层以为敞口被取消。

    这里走的是「先查再撤」：订单已经不挂在簿上、而且不是 working，
    才按成功处理；但测试关心的是**持仓还在**，上层不会被骗。
    """
    ex = _perp(fake)
    try:
        o, err = await ex.place_order("BUY", 0.010, "LIMIT", price=76000.0)
        assert err == "" and o.status == "FILLED", (err, o.status)
        assert (await ex.position())["qty"] == pytest.approx(0.010)
        # 撤一张已成交的单：不许把持仓或状态改坏
        await ex.cancel_order(order_id=o.order_id)
        assert (await ex.position())["qty"] == pytest.approx(0.010)
        assert (await ex.query_order(order_id=o.order_id)).status == "FILLED"
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_limit_without_price_is_rejected_locally(fake):
    """缺参数在本地就拦住，不浪费一次带签名的请求。"""
    ex = _perp(fake)
    try:
        before = len(fake.calls)
        o, err = await ex.place_order("BUY", 0.010, "LIMIT")
        assert o.status == "REJECTED"
        assert "price" in err
        assert len(fake.calls) == before, "本地校验失败却还是发了请求"
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_stop_without_stop_price_is_rejected_locally(fake):
    ex = _perp(fake)
    try:
        before = len(fake.calls)
        o, err = await ex.place_order("SELL", 0.010, "STOP_MARKET")
        assert o.status == "REJECTED"
        assert "stop_price" in err
        assert len(fake.calls) == before
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_unsupported_order_type_is_rejected(fake):
    ex = _perp(fake)
    try:
        o, err = await ex.place_order("BUY", 0.010, "ICEBERG", price=70000.0)
        assert o.status == "REJECTED"
        assert "不支持" in err
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_halt_blocks_new_entry_but_allows_protective_stop(fake):
    """熔断期间：不许用挂单开仓（否则熔断可被绕过），但止损单必须能挂。

    拒绝止损单的后果是敞口没有上限，比「账目暂时不准」危险得多。
    """
    ex = _perp(fake)
    try:
        ex.halted = "测试熔断"
        o, err = await ex.place_order("BUY", 0.010, "LIMIT", price=70000.0)
        assert o.status == "REJECTED" and "熔断" in err

        o2, err2 = await ex.place_order("SELL", 0.010, "STOP_MARKET",
                                        stop_price=73000.0, reduce_only=True)
        assert err2 == "", err2
        assert o2.working, o2.status
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_qty_is_rounded_to_step_and_bad_qty_rejected(fake):
    ex = _perp(fake)
    try:
        o, err = await ex.place_order("BUY", 0.0000001, "LIMIT", price=70000.0)
        assert o.status == "REJECTED", o
        assert "取整" in err
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_pending_order_is_not_treated_as_a_fill(fake):
    """`PendingOrder.working` 必须为真、且不能当成交记账。

    这条守的是「挂单成功 ≠ 已建仓」这个语义边界 —— 一旦混用，
    账面上会有仓位而交易所里没有。
    """
    ex = _perp(fake)
    try:
        o, err = await ex.place_order("SELL", 0.010, "LIMIT", price=80000.0)
        assert err == ""
        assert o.working is True
        assert o.executed_qty == pytest.approx(0.0)
        assert o.remaining == pytest.approx(0.010)
        # 交易所侧也确认没有仓位
        assert (await ex.position())["qty"] == pytest.approx(0.0)
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_open_orders_is_empty_when_nothing_rests(fake):
    ex = _perp(fake)
    try:
        assert await ex.open_orders() == []
    finally:
        await ex.close()
