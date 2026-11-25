# -*- coding: utf-8 -*-
"""条件单必须走 Algo Order 端点（-4120 迁移）。

背景（2026-11-25 生产实测，不是文档推测）：
  币安已把条件单（STOP_MARKET / STOP / TAKE_PROFIT_MARKET）从
  `/fapi/v1/order` 下线，旧端点固定返回
      -4120 Order type not supported for this endpoint.
           Please use the Algo Order API endpoints instead.

  代价：A1 那版「接管仓交易所侧保护单」自上线起**一次都没挂成功过**
  —— 近 1 小时 1725 条 adopted_stop_order_failed，人工仓在交易所侧
  从来没有保护。所以这不是"接口偶尔失败"，是端点迁移。

同时钉住三个容易写错、错了又不报错的点：
  · 字段名不同（algoId/clientAlgoId/orderType/triggerPrice/algoStatus）
  · 条件单**不在** `/fapi/v1/openOrders` 里（两个挂单簿分开）
  · 没有单张查询条件单的接口 → 只能拉列表按 clientAlgoId 匹配
"""
import asyncio
from types import SimpleNamespace

import pytest

from ai_trader.exchange.live import LiveExecutor


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


class _Ex(LiveExecutor):
    """只替换 `_req` 的执行器：走真实的参数拼装/解析代码。"""

    def __init__(self, responses):
        self.calls = []
        self._responses = responses
        self.halted = ""
        self.halted_ts = 0.0
        self.cfg = SimpleNamespace(
            trading=SimpleNamespace(symbol="BTCUSDT", leverage=22.0))
        self.feed = SimpleNamespace(last_price=60000.0)

    async def _req(self, method, path, params=None, **kw):
        self.calls.append((method, path, dict(params or {})))
        key = (method, path)
        v = self._responses.get(key)
        if isinstance(v, Exception):
            raise v
        return v

    async def spec(self):
        return {"step": 0.001, "tick": 0.1, "qty_precision": 3,
                "price_precision": 1, "min_notional": 5.0}

    def round_qty(self, q, step):
        return round(int(q / step + 1e-9) * step, 12)

    def _round_px(self, px, spec):
        return round(round(px / 0.1) * 0.1, 1)

    def _maybe_clear_halt(self):
        pass


def _paths(ex):
    return [(m, p) for m, p, _ in ex.calls]


# ---------------------------------------------------- 1. 条件单走新端点

def test_stop_market_goes_to_algo_endpoint_not_the_old_one():
    ex = _Ex({("POST", "/fapi/v1/algoOrder"):
              {"algoId": 3000002442338017,
               "clientAlgoId": "AI-SLP-123", "orderType": "STOP_MARKET",
               "algoStatus": "NEW", "triggerPrice": "57000.0",
               "quantity": "0.01"}})
    o, err = _run(ex.place_order("SELL", 0.01, order_type="STOP_MARKET",
                                 stop_price=57000.0, reduce_only=True,
                                 client_id="AI-SLP-123"))
    assert err == "", err
    assert ("POST", "/fapi/v1/algoOrder") in _paths(ex)
    assert ("POST", "/fapi/v1/order") not in _paths(ex), (
        "条件单又发到旧端点了 —— 那就是 -4120，保护单一张都挂不上")
    assert o.order_id == "3000002442338017"
    assert o.status == "NEW"


def test_algo_request_uses_the_algo_field_names():
    ex = _Ex({("POST", "/fapi/v1/algoOrder"):
              {"algoId": 1, "algoStatus": "NEW"}})
    _run(ex.place_order("SELL", 0.01, order_type="STOP_MARKET",
                        stop_price=57000.0, reduce_only=True,
                        client_id="AI-SLP-123"))
    body = [p for m, path, p in ex.calls
            if path == "/fapi/v1/algoOrder"][0]
    assert body["algoType"] == "CONDITIONAL", (
        "少了 algoType=CONDITIONAL，端点会拒单")
    assert body["triggerPrice"] == "57000.0"
    assert "stopPrice" not in body, "Algo 端点不认 stopPrice"
    assert body["clientAlgoId"] == "AI-SLP-123"
    assert "newClientOrderId" not in body, (
        "混用旧字段名会被静默忽略 → 重启后查不回同一张单 → 重挂第二张")
    assert body["reduceOnly"] == "true"


def test_limit_orders_still_use_the_old_endpoint():
    """限价单**没有**迁移，别顺手把它也改到 algo 端点去。"""
    ex = _Ex({("POST", "/fapi/v1/order"):
              {"orderId": 77, "clientOrderId": "AI-pend-x", "status": "NEW",
               "type": "LIMIT", "origQty": "0.01", "price": "59000.0"}})
    o, err = _run(ex.place_order("BUY", 0.01, order_type="LIMIT",
                                 price=59000.0, client_id="AI-pend-x"))
    assert err == "", err
    assert ("POST", "/fapi/v1/order") in _paths(ex)
    assert ("POST", "/fapi/v1/algoOrder") not in _paths(ex)
    assert o.order_id == "77"


# ------------------------------------- 2. 响应字段名（错了会"看起来没挂上"）

def test_algo_response_fields_are_mapped():
    """algoId/clientAlgoId/orderType/triggerPrice/algoStatus 必须都译对。"""
    ex = _Ex({})
    o = LiveExecutor._as_algo_pending({
        "algoId": 3000002442338017, "clientAlgoId": "AI-SLP-9",
        "symbol": "BTCUSDT", "side": "SELL", "orderType": "STOP_MARKET",
        "quantity": "0.01", "actualQty": "0.0",
        "triggerPrice": "57491.4", "algoStatus": "NEW",
        "reduceOnly": True, "actualOrderId": ""})
    assert o.order_id == "3000002442338017"
    assert o.client_id == "AI-SLP-9"
    assert o.type == "STOP_MARKET"
    assert o.stop_price == pytest.approx(57491.4)
    assert o.qty == pytest.approx(0.01)
    assert o.status == "NEW"
    assert o.working is True, (
        "algoStatus=NEW 必须被认成「还挂着」；认错了就会每 3 秒重挂一张")


# --------------------------------- 3. 两个挂单簿（合并查，别漏条件单）

def test_open_orders_merges_the_algo_book():
    ex = _Ex({("GET", "/fapi/v1/openOrders"):
              [{"orderId": 1, "clientOrderId": "AI-pend-a", "status": "NEW",
                "type": "LIMIT", "origQty": "0.01", "side": "BUY"}],
              ("GET", "/fapi/v1/openAlgoOrders"):
              [{"algoId": 2, "clientAlgoId": "AI-SLP-b",
                "algoStatus": "NEW", "orderType": "STOP_MARKET",
                "quantity": "0.01", "triggerPrice": "57000", "side": "SELL"}]})
    got = _run(ex.open_orders())
    ids = {o.client_id for o in got}
    assert ids == {"AI-pend-a", "AI-SLP-b"}, (
        "条件单不在 openOrders 里，不合并就会误判成「没有保护单」→ 重挂")


def test_algo_book_failure_does_not_silently_look_empty():
    """条件单簿拉不到时不能返回空列表（那等于宣布"没有保护单"）。"""
    from ai_trader.exchange.live import LiveError

    ex = _Ex({("GET", "/fapi/v1/openAlgoOrders"): LiveError("boom")})
    with pytest.raises(LiveError):
        _run(ex.open_algo_orders())


# ------------------------------------- 4. 查询与撤单要认得 algoId

def test_query_falls_back_to_the_algo_book_by_client_id():
    from ai_trader.exchange.live import LiveError

    ex = _Ex({("GET", "/fapi/v1/order"): LiveError("-2013 does not exist"),
              ("GET", "/fapi/v1/openAlgoOrders"):
              [{"algoId": 5, "clientAlgoId": "AI-SLP-7", "algoStatus": "NEW",
                "orderType": "STOP_MARKET", "quantity": "0.01",
                "triggerPrice": "57000", "side": "SELL"}]})
    o = _run(ex.query_order(client_id="AI-SLP-7"))
    assert o is not None, "条件单被查漏了 → 每 tick 重挂一张"
    assert o.order_id == "5"


def test_cancel_falls_back_to_the_algo_endpoint():
    from ai_trader.exchange.live import LiveError

    ex = _Ex({("DELETE", "/fapi/v1/order"): LiveError("-2013"),
              ("DELETE", "/fapi/v1/algoOrder"): {"code": "200"}})
    ok = _run(ex.cancel_order(order_id="3000002442338017"))
    assert ok is True
    assert ("DELETE", "/fapi/v1/algoOrder") in _paths(ex)


def test_cancel_algo_treats_already_gone_as_success():
    from ai_trader.exchange.live import LiveError

    ex = _Ex({("DELETE", "/fapi/v1/order"): LiveError("-2013"),
              ("DELETE", "/fapi/v1/algoOrder"):
              LiveError('-2013 Order does not exist.')})
    assert _run(ex.cancel_order(order_id="123")) is True, (
        "已经不在的单按撤单成功处理（目的状态已达成）")
    # ★ 只钉返回值不够：外层还有一层「查一次、不在挂单簿就按成功」的兜底，
    #   把内层的 -2013 判断删掉它照样返回 True（变异测试实测存活）。
    #   真正的契约是：**不需要再查一次**就知道成功了。
    assert ("GET", "/fapi/v1/order") not in _paths(ex), (
        "撤单失败后又去查了一次订单 —— 说明没有把 -2013 认成"
        "「本来就不在」，而是走了兜底查询（多一次网络往返，"
        "且把「撤单失败」和「本来就没有」混成了同一条路径）")


# ------------------------- 5. 成交价：条件单本身没有均价，要跟 actualOrderId

def test_closed_order_lookup_uses_all_orders_fallback():
    from ai_trader.exchange.live import LiveError

    ex = _Ex({("GET", "/fapi/v1/order"): LiveError("-2013"),
              ("GET", "/fapi/v1/allOrders"):
              [{"orderId": 9, "clientOrderId": "AI-SLP-3", "status": "FILLED",
                "avgPrice": "57400.0", "executedQty": "0.01",
                "type": "STOP_MARKET", "side": "SELL"}]})
    o = _run(ex.closed_order_by_client_id("AI-SLP-3"))
    assert o is not None and o.avg_price == pytest.approx(57400.0)


def test_closed_order_lookup_returns_none_rather_than_guessing():
    from ai_trader.exchange.live import LiveError

    ex = _Ex({("GET", "/fapi/v1/order"): LiveError("-2013"),
              ("GET", "/fapi/v1/allOrders"): [],
              ("GET", "/fapi/v1/openAlgoOrders"): []})
    assert _run(ex.closed_order_by_client_id("AI-SLP-nope")) is None, (
        "查不到就必须返回 None —— 猜一个价会把真实亏损记成别的数")
