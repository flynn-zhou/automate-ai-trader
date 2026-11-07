# -*- coding: utf-8 -*-
"""现货执行器的安全测试。

现货没有爆仓风险，但有一类独有的危险：买入时 USDT 不足、卖出时 BTC 不足，
API 会直接拒单；更隐蔽的是"下单返回成功但余额没变"。
和永续一样，这里只信任余额查询的结果，不信任下单回报。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import TradingCfg
from ai_trader.exchange.spot import SpotExecutor


class FakeSpot:
    def __init__(self, usdt=100.0, btc=0.0):
        self.usdt = usdt
        self.btc = btc
        self.orders = []
        self.ignore_orders = False

    async def __call__(self, method, path, params, signed):
        if path.endswith("/exchangeInfo"):
            return {"symbols": [{
                "baseAssetPrecision": 8, "baseAsset": "BTC", "quoteAsset": "USDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.00001000",
                     "minQty": "0.00001000"},
                    {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]}]}
        if path.endswith("/account"):
            return {"balances": [
                {"asset": "USDT", "free": str(self.usdt), "locked": "0"},
                {"asset": "BTC", "free": str(self.btc), "locked": "0"}]}
        if path.endswith("/order"):
            self.orders.append(dict(params))
            q = float(params["quantity"])
            px = 77700.0
            if not self.ignore_orders:
                if params["side"] == "BUY":
                    self.btc = round(self.btc + q, 8)
                    self.usdt = round(self.usdt - q * px, 8)
                else:
                    self.btc = round(self.btc - q, 8)
                    self.usdt = round(self.usdt + q * px, 8)
            return {"orderId": 9, "executedQty": str(q),
                    "cummulativeQuoteQty": str(q * px)}
        raise AssertionError("未预期端点 " + path)


class Feed:
    last_price = 77700.0


class Cfg:
    def __init__(self):
        self.trading = TradingCfg()


def make(fx):
    ex = SpotExecutor(Feed(), Cfg(), "k", "s")
    ex._transport = fx
    return ex


def test_round_qty_spot_step():
    ex = make(FakeSpot())
    assert ex.round_qty(0.000123456, 0.00001) == 0.00012
    assert ex.round_qty(0.000009, 0.00001) == 0.0


@pytest.mark.asyncio
async def test_buy_updates_balance():
    fx = FakeSpot()
    ex = make(fx)
    r = await ex.market_order("buy", 0.001, "开仓")
    assert r.ok
    assert fx.btc == pytest.approx(0.001)
    st = await ex.equity()
    assert st["position_qty"] == pytest.approx(0.001)
    assert st["position_side"] == "long"


@pytest.mark.asyncio
async def test_min_notional_is_five_usdt():
    fx = FakeSpot()
    ex = make(fx)
    r = await ex.market_order("buy", 0.00005, "太小")   # 0.00005*77700 = 3.9U
    assert not r.ok and "最小" in r.reason


@pytest.mark.asyncio
async def test_silent_failure_halts():
    fx = FakeSpot()
    fx.ignore_orders = True
    ex = make(fx)
    r = await ex.market_order("buy", 0.001, "静默失败")
    assert not r.ok and ex.halted and "核对失败" in ex.halted


@pytest.mark.asyncio
async def test_sell_closes_position():
    fx = FakeSpot(usdt=0.0, btc=0.001)
    ex = make(fx)
    r = await ex.market_order("sell", 0.001, "平仓")
    assert r.ok
    assert fx.btc == pytest.approx(0.0)
