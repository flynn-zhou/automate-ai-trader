# -*- coding: utf-8 -*-
"""fake_binance 的挂单能力（限价 / 止损 / 撤单 / 查询 / 部分成交）。

## 这些测试守的是什么

原来 `_perp_order` **忽略 `type`**：不管发 LIMIT 还是 STOP_MARKET，都返回
`status: FILLED` 并立刻改持仓。后果不是「测不出来」，而是**测出来的是假的** ——
任何基于挂单的策略在测试里都会表现为「一挂就成交」，包括那些根本不该成交的。

所以这一组测试的重点不是「能挂单」，而是：

1. 挂单**不**改持仓（`_apply_fill` 只在成交时调）
2. 限价单在价格没走到之前一直是 `NEW`
3. 价格走过去了才成交，且 LIMIT 成交价取**挂单价**（保守下界）
4. 止损单在触发价之前不动，穿过后按触发时市价成交
5. 撤单之后状态是 `CANCELED` 且不再能成交
6. 部分成交累计：`PARTIALLY_FILLED` → `FILLED`
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import sys
import time
from urllib.parse import urlencode

import pytest

pytest.importorskip("aiohttp")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

from fake_binance import FakeBinance          # noqa: E402


def _signed(params: dict, secret: str) -> str:
    p = dict(params)
    p["timestamp"] = str(int(time.time() * 1000))
    payload = urlencode(sorted(p.items()))
    sig = hmac.new(secret.encode(), payload.encode(),
                   hashlib.sha256).hexdigest()
    return payload + "&signature=" + sig


class Cli(object):
    """极小的 HTTP 客户端。每个用例自带一个，避免 session 泄漏。"""

    def __init__(self, srv):
        self.srv = srv
        self.sess = None

    async def __aenter__(self):
        import aiohttp
        self.sess = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *a):
        if self.sess:
            await self.sess.close()

    async def req(self, method, path, params=None):
        """POST 参数走 body、GET/DELETE 走 query —— 和 `live.py::_req` 一致。

        都塞 query 也能过签名校验（`_verify` 在 body 为空时会回落到 query），
        但处理函数自己读的是 body，参数就丢了 —— 那种失败会表现成
        `KeyError: 'side'`，看起来像 handler 写错，其实是传参位置的问题。
        """
        params = params or {}
        qs = _signed(params, self.srv.api_secret)
        headers = {"X-MBX-APIKEY": self.srv.api_key}
        if method == "POST":
            url = "%s%s" % (self.srv.base(), path)
            async with self.sess.request(method, url, headers=headers,
                                         data=qs) as r:
                return r.status, await r.json()
        url = "%s%s?%s" % (self.srv.base(), path, qs)
        async with self.sess.request(method, url, headers=headers) as r:
            return r.status, await r.json()


def test_limit_buy_does_not_fill_until_price_comes_down():
    """限价买单挂在下方：价格没跌到就不该成交，持仓也不该变。

    这是最基础的一条，也是原来完全测不了的一条 —— 老实现会直接
    返回 FILLED 并把持仓加上去。
    """

    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                    "quantity": "0.010", "price": "70000", "timeInForce": "GTC"})
                assert st == 200, body
                assert body["status"] == "NEW", body
                assert body["executedQty"] == "0.000"
                # 关键：没成交就不许动持仓
                assert srv.positions.get("BTCUSDT", {}).get("amt", 0.0) == 0.0

                # 价格还没到
                srv.set_price(71000.0)
                st2, body2 = await c.req("GET", "/fapi/v1/order",
                                         {"orderId": body["orderId"]})
                assert st2 == 200 and body2["status"] == "NEW"

                # 跌到挂单价
                srv.set_price(70000.0)
                st3, body3 = await c.req("GET", "/fapi/v1/order",
                                         {"orderId": body["orderId"]})
                assert body3["status"] == "FILLED", body3
                assert float(body3["executedQty"]) == pytest.approx(0.010)
                # LIMIT 成交价取挂单价，不是当前价
                assert float(body3["avgPrice"]) == pytest.approx(70000.0)
                assert srv.positions["BTCUSDT"]["amt"] == pytest.approx(0.010)
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_limit_sell_rests_and_fills_at_the_limit_price():
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT",
                    "quantity": "0.020", "price": "78000"})
                assert body["status"] == "NEW"
                srv.set_price(77000.0)
                assert srv.order_book[body["orderId"]]["status"] == "NEW"
                srv.set_price(78000.0)
                o = srv.order_book[body["orderId"]]
                assert o["status"] == "FILLED"
                assert o["avgPrice"] == pytest.approx(78000.0)
                assert srv.positions["BTCUSDT"]["amt"] == pytest.approx(-0.020)
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_marketable_limit_fills_immediately():
    """挂进来时就已经价内的限价单，立刻成交（真实交易所也是这样）。"""
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                    "quantity": "0.010", "price": "76000"})   # 高于现价 → 价内
                assert body["status"] == "FILLED", body
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_stop_market_sell_triggers_on_the_way_down():
    """卖出止损：价格跌破触发价才成交，之前一直挂着。"""
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "SELL",
                    "type": "STOP_MARKET", "quantity": "0.010",
                    "stopPrice": "74000"})
                assert st == 200, body
                assert body["status"] == "NEW"
                srv.set_price(74500.0)
                assert srv.order_book[body["orderId"]]["status"] == "NEW"
                srv.set_price(73990.0)
                o = srv.order_book[body["orderId"]]
                assert o["status"] == "FILLED"
                # STOP_MARKET 是市价单：按触发时的价格成交
                assert o["avgPrice"] == pytest.approx(73990.0)
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_stop_market_buy_triggers_on_the_way_up():
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "STOP_MARKET",
                    "quantity": "0.010", "stopPrice": "76000"})
                assert body["status"] == "NEW"
                srv.set_price(75990.0)
                assert srv.order_book[body["orderId"]]["status"] == "NEW"
                srv.set_price(76010.0)
                assert srv.order_book[body["orderId"]]["status"] == "FILLED"
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_open_orders_only_lists_live_orders():
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                _, a = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                    "quantity": "0.010", "price": "70000"})
                _, b = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "SELL",
                    "type": "STOP_MARKET", "quantity": "0.010",
                    "stopPrice": "74000"})
                st, lst = await c.req("GET", "/fapi/v1/openOrders",
                                      {"symbol": "BTCUSDT"})
                assert st == 200
                assert sorted(x["orderId"] for x in lst) == \
                    sorted([a["orderId"], b["orderId"]])
                # 一张成交、一张撤掉之后，列表应该空
                srv.set_price(70000.0)
                await c.req("DELETE", "/fapi/v1/order",
                            {"orderId": b["orderId"]})
                _, lst2 = await c.req("GET", "/fapi/v1/openOrders",
                                      {"symbol": "BTCUSDT"})
                assert lst2 == [], lst2
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_cancel_marks_canceled_and_stops_later_fills():
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                _, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                    "quantity": "0.010", "price": "70000"})
                st, cn = await c.req("DELETE", "/fapi/v1/order",
                                     {"orderId": body["orderId"]})
                assert st == 200 and cn["status"] == "CANCELED"
                # 撤了之后价格走过来也不许成交
                srv.set_price(69000.0)
                assert srv.order_book[body["orderId"]]["status"] == "CANCELED"
                assert srv.positions.get("BTCUSDT", {}).get("amt", 0.0) == 0.0
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_cancel_unknown_order_returns_binance_error():
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, body = await c.req("DELETE", "/fapi/v1/order",
                                       {"orderId": "999999"})
                assert st == 400
                assert body["code"] == -2011
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_cancel_already_filled_order_is_rejected():
    """已成交的单不能撤 —— 静默「撤成功」会让上层以为敞口被取消了。"""
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                _, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                    "quantity": "0.010", "price": "76000"})   # 立刻成交
                assert body["status"] == "FILLED"
                st, err = await c.req("DELETE", "/fapi/v1/order",
                                      {"orderId": body["orderId"]})
                assert st == 400 and err["code"] == -2011
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_partial_fill_accumulates_to_filled():
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            srv.partial_fill_ratio = 0.5
            async with Cli(srv) as c:
                _, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "SELL",
                    "type": "STOP_MARKET", "quantity": "1.000",
                    "stopPrice": "74000"})
                oid = body["orderId"]
                srv.set_price(73900.0)          # 触发 → 只成交一半
                o = srv.order_book[oid]
                assert o["status"] == "PARTIALLY_FILLED", o["status"]
                assert o["executedQty"] == pytest.approx(0.5)
                _, q = await c.req("GET", "/fapi/v1/order", {"orderId": oid})
                assert q["status"] == "PARTIALLY_FILLED"
                assert float(q["executedQty"]) == pytest.approx(0.5)
                # 再推一次价格 → 剩下的成交
                srv.set_price(73800.0)
                assert srv.order_book[oid]["status"] == "FILLED"
                assert srv.positions["BTCUSDT"]["amt"] == pytest.approx(-1.0)
                assert len(srv.fills) == 2
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_client_order_id_can_be_queried_and_must_be_unique():
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                    "quantity": "0.010", "price": "70000",
                    "newClientOrderId": "strat-0001"})
                assert st == 200
                assert body["clientOrderId"] == "strat-0001"
                st2, q = await c.req("GET", "/fapi/v1/order",
                                     {"origClientOrderId": "strat-0001"})
                assert st2 == 200 and q["orderId"] == body["orderId"]
                # 重复的 clientOrderId 必须拒 —— 否则重启后无法按 id 对账
                st3, err = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                    "quantity": "0.010", "price": "69000",
                    "newClientOrderId": "strat-0001"})
                assert st3 == 400 and err["code"] == -2010
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_limit_without_price_is_rejected():
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, err = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
                    "quantity": "0.010"})
                assert st == 400 and err["code"] == -1102
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_stop_without_stop_price_is_rejected():
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, err = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "SELL",
                    "type": "STOP_MARKET", "quantity": "0.010"})
                assert st == 400 and err["code"] == -1102
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_market_order_still_fills_immediately():
    """MARKET 的行为不许被挂单改造带坏 —— 这是所有现有测试的地基。"""
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                st, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                    "quantity": "0.010"})
                assert body["status"] == "FILLED"
                assert srv.positions["BTCUSDT"]["amt"] == pytest.approx(0.010)
                assert srv.positions["BTCUSDT"]["entry"] == pytest.approx(75000.0)
                # 市价单不该出现在挂单账本里
                assert srv.order_book == {}
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())


def test_reduce_only_close_does_not_flip_the_position():
    """reduceOnly 平仓不改入场价，也不该把持仓反向。"""
    async def run():
        srv = await FakeBinance(price=75000.0).start()
        try:
            async with Cli(srv) as c:
                await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                    "quantity": "0.010"})
                _, body = await c.req("POST", "/fapi/v1/order", {
                    "symbol": "BTCUSDT", "side": "SELL",
                    "type": "STOP_MARKET", "quantity": "0.010",
                    "stopPrice": "74000", "reduceOnly": "true"})
                srv.set_price(73900.0)
                o = srv.order_book[body["orderId"]]
                assert o["status"] == "FILLED"
                assert o["reduceOnly"] is True
                assert srv.positions["BTCUSDT"]["amt"] == pytest.approx(0.0)
                assert srv.positions["BTCUSDT"]["entry"] == pytest.approx(0.0)
        finally:
            await srv.stop()

    asyncio.get_event_loop().run_until_complete(run())
