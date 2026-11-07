# -*- coding: utf-8 -*-
"""熔断不能把止损一起关掉。

2026-11-02 发现的真缺陷：
  spot.py 在「余额核对失败」时把 self.halted 置成一段消息，之后 market_order
  开头就返回「已熔断」。而这个属性【全仓库没有任何一处清除】——只有重启进程才会重置。

  后果链：
    1. 币安账户接口在成交后 0.4s 内还没刷新（瞬态延迟） -> 核对判失败
    2. self.halted 被置位
    3. _close 调 market_order -> 拿到「已熔断」-> 平仓失败
    4. 指数退避重试到 300s 一次，永远失败
    5. 【止损失效】，持仓敞口无上限，直到有人发现并重启

  一个会关掉止损的安全机制，不是安全机制。
  所以这里锁两条不变量：
    A. 熔断只拦【开仓】；减仓平仓（reduce_only）必须照常放行。
    B. 熔断必须能自己过期——瞬态问题自愈，持续问题会立刻再次触发并留日志，
       而不是等人工重启。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import TradingCfg
from ai_trader.exchange.spot import SpotExecutor

PX = 77700.0


class FakeSpot:
    def __init__(self, usdt=100.0, btc=0.001):
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
            if not self.ignore_orders:
                if params["side"] == "BUY":
                    self.btc = round(self.btc + q, 8)
                    self.usdt = round(self.usdt - q * PX, 8)
                else:
                    self.btc = round(self.btc - q, 8)
                    self.usdt = round(self.usdt + q * PX, 8)
            return {"orderId": 9, "executedQty": str(q),
                    "cummulativeQuoteQty": str(q * PX)}
        raise AssertionError("未预期端点 " + path)


class Feed:
    last_price = PX


class Cfg:
    def __init__(self):
        self.trading = TradingCfg()


def make(fx):
    ex = SpotExecutor(Feed(), Cfg(), "k", "s")
    ex._transport = fx
    return ex


def _halt(ex, msg="余额核对失败：期望 0.00100000，实际 0.00000000"):
    ex.halted = msg
    ex.halted_ts = getattr(ex, "halted_ts", 0.0) or __import__("time").time()


@pytest.mark.asyncio
async def test_halted_executor_still_blocks_opens():
    """熔断期间不许开仓（原有保护，必须保留）。"""
    fx = FakeSpot(usdt=100.0, btc=0.0)
    ex = make(fx)
    _halt(ex)
    r = await ex.market_order("buy", 0.001, "开仓", reduce_only=False)
    assert not r.ok, "熔断期间竟然还能开仓"
    assert "熔断" in r.reason
    assert not fx.orders, "熔断期间发出了真实订单"


@pytest.mark.asyncio
async def test_halted_executor_can_still_close():
    """熔断期间【必须】还能平仓——否则止损就是死的。"""
    fx = FakeSpot(usdt=0.0, btc=0.002)
    ex = make(fx)
    _halt(ex)
    r = await ex.market_order("sell", 0.002, "平仓", reduce_only=True)
    assert r.ok, (
        u"熔断期间平仓被拒（%r）。这条路径会导致止损失效、持仓敞口无上限。"
        % (r.reason,))
    assert fx.btc == pytest.approx(0.0), "平仓单没有真正执行"


@pytest.mark.asyncio
async def test_halt_expires_so_transient_errors_self_heal():
    """瞬态错误造成的熔断必须能自己过期，否则 7x24 运行永远要靠人重启。"""
    fx = FakeSpot(usdt=100.0, btc=0.0)
    ex = make(fx)
    ex.halted = "余额核对失败（瞬态）"
    ex.halted_ts = 1.0                 # 很久以前
    r = await ex.market_order("buy", 0.001, "开仓", reduce_only=False)
    assert r.ok, u"过期的熔断没有自动解除，交易被永久停摆：%r" % (r.reason,)
    assert ex.halted == "", "解除后 halted 应该被清空"


@pytest.mark.asyncio
async def test_real_halt_records_timestamp():
    """真正触发熔断时必须同时记下时刻，否则它永远不会过期。

    这条不能用手工置位来测：手工设 halted 只会绕过置位代码，
    把「置位时忘记记时间」这个缺陷放过去（变异验证时确实漏了）。
    所以这里制造一次真实的「回报说成交、余额没动」。
    """
    import time as _t

    fx = FakeSpot(usdt=100.0, btc=0.0)
    fx.ignore_orders = True            # 交易所收单但不动余额 -> 核对必然失败
    ex = make(fx)
    t0 = _t.time()
    r = await ex.market_order("buy", 0.001, "静默失败", reduce_only=False)
    assert not r.ok, "回报说成交、余额没动，不该判成功"
    assert ex.halted, "这种情况必须熔断"
    assert getattr(ex, "halted_ts", 0) >= t0, (
        u"熔断没有记录置位时刻（halted_ts=%r），它就不可能自动过期——"
        u"一次瞬态错误就要靠人工重启。"
        % (getattr(ex, "halted_ts", None),))


@pytest.mark.asyncio
async def test_fresh_halt_is_not_cleared():
    """刚置位的熔断不能被马上放行，否则保护形同虚设。"""
    fx = FakeSpot(usdt=100.0, btc=0.0)
    ex = make(fx)
    import time as _t
    ex.halted = "刚刚熔断"
    ex.halted_ts = _t.time()
    r = await ex.market_order("buy", 0.001, "开仓", reduce_only=False)
    assert not r.ok, "刚熔断就被放行了"
    assert ex.halted, "新鲜熔断被误清除"
