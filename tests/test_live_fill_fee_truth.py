# -*- coding: utf-8 -*-
"""永续下单的手续费必须记真数，不能因为回报格式整段变 0。

真实事故（第 129 轮，账户 292U 上亏了 6.32U 的"记账利润"）：

  日志   [实盘] sell 0.000000 @ 72479.4 持仓 +0.000000
         <- executedQty 是 "0"，avgPrice 也是 "0"

  根因   POST /fapi/v1/order 不传 newOrderRespType 时，币安永续返回 ACK：
         只有 orderId/symbol/status，executedQty 与 avgPrice 都是 "0"。
         于是 live.py 里
             fee = abs(avg * filled) * taker_fee
         因为 filled = 0 恒等于 0 —— 【每一笔】手续费都记成 0。

  qty 侥幸没错，是因为 engine._close() 里有 `fill.qty > 0` 的兜底；
  fee 这一侧没有任何兜底。

  两笔的真实账（交易所 /fapi/v1/userTrades 的 commission）：
     毛 +0.3225  费 3.805099  ->  净 -3.4826   （库里记 +0.33）
     毛 +3.6250  费 2.499867  ->  净 +1.1251   （库里记 +3.63）
  合计 -2.3575，与账户权益变化 294.951699 -> 292.594233 = -2.357466 完全吻合。
  也就是说库里凭手续费多记了 6.32U 的利润，占本金 2.2%。

本文件钉五件事：
  1. ACK 回报（成交量为 0）时，必须补查订单拿到真实成交量/成交价；
  2. 手续费必须取 /fapi/v1/userTrades 的真实 commission（区分 maker/taker）；
  3. 拿不到真实手续费时按 taker 估算，但【绝不能是 0】；
  4. 下单请求必须带 newOrderRespType=RESULT —— 否则第 1 条每次都要多打一次接口；
  5. 反向约束：真实 commission 可用时，不能被估算值盖掉。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import TradingCfg
from ai_trader.exchange.live import LiveExecutor

SYM = "BTCUSDT"
PX = 72479.4
QTY = 0.075
COMMISSION = 2.717974       # 交易所 userTrades 里的真实值（taker 0.05%）


class Feed:
    last_price = PX


class Cfg:
    def __init__(self, **kw):
        self.trading = TradingCfg(**kw)


class Fake:
    """可按需打开/关闭各个端点的假交易所。

    ack=True       POST /order 返回 ACK 形状（成交量为 0）—— 生产实际行为
    fill_known     GET  /order 是否给得出成交结果
    commission     是否给得出 userTrades（None = 端点直接报错）
    """

    def __init__(self, ack=True, fill_known=True, commission=COMMISSION,
                 start_qty=QTY):
        self.ack = ack
        self.fill_known = fill_known
        self.commission = commission
        self.qty = start_qty
        self.order_params = []
        self.user_trade_calls = 0
        self.get_order_calls = 0

    async def __call__(self, method, path, params, signed):
        if path.endswith("/exchangeInfo"):
            return {"symbols": [{
                "symbol": SYM, "quantityPrecision": 3, "pricePrecision": 1,
                "baseAsset": "BTC", "quoteAsset": "USDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001",
                     "minQty": "0.001"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"}]}]}
        if path.endswith("/positionRisk"):
            return [{"symbol": SYM, "positionAmt": str(self.qty),
                     "entryPrice": str(PX)}]
        if path.endswith("/userTrades"):
            self.user_trade_calls += 1
            if self.commission is None:
                raise AssertionError("userTrades 不可用")
            return [{"orderId": 111, "qty": str(QTY), "price": str(PX),
                     "commission": str(self.commission),
                     "commissionAsset": "USDT", "maker": False}]
        if path.endswith("/order"):
            if method == "POST":
                self.order_params.append(dict(params))
                q = float(params["quantity"])
                self.qty = round(self.qty - q, 8) if params["side"] == "SELL" \
                    else round(self.qty + q, 8)
                if self.ack:
                    # 生产实际返回：没有 executedQty / avgPrice
                    return {"orderId": 111, "symbol": SYM, "status": "NEW",
                            "clientOrderId": "x"}
                return {"orderId": 111, "status": "FILLED",
                        "executedQty": str(q), "avgPrice": str(PX),
                        "cumQuote": str(q * PX)}
            self.get_order_calls += 1
            if not self.fill_known:
                raise AssertionError("order 查询不可用")
            return {"orderId": 111, "status": "FILLED",
                    "executedQty": str(QTY), "avgPrice": str(PX),
                    "cumQuote": str(QTY * PX)}
        raise AssertionError("未预期的端点 " + path)


def make(fx):
    ex = LiveExecutor(Feed(), Cfg(), "k", "s")
    ex._transport = fx
    return ex


# ------------------------------------------------------------------ 1 + 2
@pytest.mark.asyncio
async def test_ack_response_still_yields_real_qty_price_and_commission():
    """★ ACK 回报下，成交量/成交价来自补查，手续费来自 userTrades。"""
    fx = Fake(ack=True)
    ex = make(fx)
    r = await ex.market_order("sell", QTY, "通道出场", reduce_only=True)
    assert r.ok, r.reason
    assert r.qty == pytest.approx(QTY), (
        u"成交量是 %r，而 ACK 回报里没有它 —— 必须补查订单" % (r.qty,))
    assert r.price == pytest.approx(PX, rel=1e-6), \
        u"成交价是 %r，应该用交易所的真实均价" % (r.price,)
    assert r.fee == pytest.approx(COMMISSION), (
        u"手续费是 %r，应为交易所 userTrades 的 commission %r。"
        u"记成 0 会让这笔盈亏凭空多出 2.72U" % (r.fee, COMMISSION))
    assert fx.get_order_calls == 1, "ACK 回报没有触发补查订单"


# ------------------------------------------------------------------ 5
@pytest.mark.asyncio
async def test_real_commission_is_not_overwritten_by_the_estimate():
    """反向约束：真实 commission 可用时，不能被估算值盖掉。

    只测"fee > 0"的话，直接用 abs(avg*qty)*taker_fee 也能过 ——
    但那样 maker 成交（0.02%）会被按 taker（0.05%）多记 2.5 倍成本。
    """
    fx = Fake(ack=False, commission=1.087125)   # 真实 maker 费，远小于 taker 估算
    ex = make(fx)
    r = await ex.market_order("sell", QTY, "测试", reduce_only=True)
    assert r.ok, r.reason
    assert r.fee == pytest.approx(1.087125), (
        u"真实手续费 1.087125 被估算值覆盖成 %r。" % (r.fee,))
    estimate = QTY * PX * 0.0005
    assert r.fee < estimate


# ------------------------------------------------------------------ 3
@pytest.mark.asyncio
async def test_fee_is_never_zero_even_when_nothing_can_be_looked_up():
    """什么都查不到时退回 taker 估算，但【绝不能是 0】。

    这是原 bug 的直接症状：filled=0 -> fee=0 -> 每一笔都少记手续费。
    """
    fx = Fake(ack=True, fill_known=False, commission=None)
    ex = make(fx)
    r = await ex.market_order("sell", QTY, "测试", reduce_only=True)
    assert r.ok, r.reason
    assert r.fee > 0, (
        u"手续费又变成 0 了 —— 这正是 11-12 两笔账对不上的原因。"
        u"拿不到真实值时必须按 taker 费率估算一个保守上界，不能假装免费")
    assert r.fee == pytest.approx(QTY * PX * 0.0005, rel=1e-6)


# ------------------------------------------------------------------ 4
@pytest.mark.asyncio
async def test_order_request_asks_for_the_fill_detail():
    """结构约束：下单必须带 newOrderRespType=RESULT。

    不带的话币安永续默认 ACK，executedQty/avgPrice 都是 0，
    每次下单都要多打一次 GET /fapi/v1/order 才拿得到成交结果 ——
    而 worker 少打那次，手续费就整段消失。
    """
    fx = Fake(ack=False)
    ex = make(fx)
    await ex.market_order("sell", QTY, "测试", reduce_only=True)
    assert fx.order_params, "没有发出下单请求"
    p = fx.order_params[-1]
    assert p.get("newOrderRespType") == "RESULT", (
        u"下单参数里没有 newOrderRespType=RESULT，实际 %r。"
        u"币安永续默认 ACK，成交量与成交价都会是 0" % (p.get("newOrderRespType"),))
    assert p.get("reduceOnly") == "true", "减仓单没带 reduceOnly"
