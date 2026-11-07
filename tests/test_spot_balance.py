# -*- coding: utf-8 -*-
"""现货首单资金约束：不能让"余额不足"变成整单失败。

事故背景（2026-11-01 代码审查发现）：
_trend_plan 原本按 notional = equity * lev 算仓位，即拿 100% 权益去买。
但买入还要付手续费，账户恰好 100U 时买 100U 的币，
交易所会按"余额不足"拒掉——第一笔真实交易就会失败。
两处修：仓位计算预留缓冲；执行层按可用余额截断。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy


class FakeSpot:
    def __init__(self, usdt=100.0, px=30000.0):
        self.usdt = usdt
        self.btc = 0.0
        self.px = px
        self.orders = []

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
            q = float(params["quantity"])
            cost = q * self.px
            if params["side"] == "BUY" and cost > self.usdt:
                raise RuntimeError("insufficient balance")
            self.orders.append((params["side"], q))
            if params["side"] == "BUY":
                self.btc = round(self.btc + q, 8)
                self.usdt = round(self.usdt - cost, 8)
            else:
                self.btc = round(self.btc - q, 8)
                self.usdt = round(self.usdt + cost, 8)
            return {"orderId": len(self.orders), "executedQty": str(q),
                    "cummulativeQuoteQty": str(cost)}
        raise AssertionError("未预期端点 " + path)


class Feed:
    def __init__(self, px=30000.0):
        self.last_price = px
        self.cfg = AppCfg()


def _ex(usdt=100.0, px=30000.0):
    cfg = AppCfg()
    cfg.trading.exec_market = "spot"
    feed = Feed(px)
    ex = SpotExecutor(feed, cfg, "k", "s")
    fx = FakeSpot(usdt, px)
    ex._transport = fx
    return ex, fx, cfg, feed


def test_trend_plan_leaves_fee_buffer():
    """100U 权益、价 30000 时，下单数量必须留出费用余量。"""
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.trend_leverage = 1.0
    feed = Feed(30000.0)
    ex, _fx, _c, _f = _ex(usdt=100.0, px=30000.0)
    eng = DecisionEngine(cfg, feed, ex, None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    plan = eng._trend_plan(30000.0, 100.0, {"ok": True})
    assert plan.ok, plan.reason
    # 全款买入需要 100U + 手续费；必须小于 100U 才有成交余地
    assert plan.notional < 100.0, "必须留出手续费缓冲，实得 %.4f" % plan.notional
    assert plan.notional > 99.0, "缓冲不应过大，实得 %.4f" % plan.notional


@pytest.mark.asyncio
async def test_buy_is_capped_to_available_balance():
    """即便上层算错了数量，执行层也必须截断而不是整单失败。"""
    ex, fx, cfg, feed = _ex(usdt=100.0, px=30000.0)
    # 故意要一个买不起的数量（100.5U 市值）
    r = await ex.market_order("buy", 100.5 / 30000.0, "测试")
    assert r.ok, r.reason
    assert fx.btc > 0
    assert fx.usdt >= 0, "不能把 USDT 买成负数"
    assert fx.btc * 30000.0 <= 100.0 + 1e-6


@pytest.mark.asyncio
async def test_buy_rejected_when_balance_too_small():
    ex, fx, cfg, feed = _ex(usdt=3.0, px=30000.0)
    r = await ex.market_order("buy", 0.0005, "测试")   # 15U，买不起
    assert not r.ok
    assert fx.btc == 0.0
