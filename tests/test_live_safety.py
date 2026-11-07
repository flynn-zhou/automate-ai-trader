# -*- coding: utf-8 -*-
"""实盘执行器的安全测试：用假交易所模拟最危险的事故。

最要命的一类不是"下单报错"，而是"下单返回成功、仓却没开"：
网络超时重发、精度被静默截断、reduceOnly 语义误用都会造成这种状态。
钱在交易所里，唯一可信的事实是持仓查询，所以这里专门测"核对不上会怎样"。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import TradingCfg
from ai_trader.exchange.live import LiveExecutor


class FakeExchange:
    """最小假交易所：只实现 executor 会用到的四个端点。"""

    def __init__(self, qty=0.0, entry=0.0, wallet=100.0):
        self.qty = qty
        self.entry = entry
        self.wallet = wallet
        self.orders = []
        self.ignore_orders = False   # True = 下单返回成功但仓位不动（静默失败）

    async def __call__(self, method, path, params, signed):
        if path.endswith("/exchangeInfo"):
            return {"symbols": [{
                "symbol": "BTCUSDT", "quantityPrecision": 3, "pricePrecision": 1,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"}]}]}
        if path.endswith("/positionRisk"):
            return [{"symbol": "BTCUSDT", "positionAmt": str(self.qty),
                     "entryPrice": str(self.entry)}]
        if path.endswith("/account"):
            return {"totalWalletBalance": str(self.wallet), "totalUnrealizedProfit": "0"}
        if path.endswith("/leverage"):
            return {"leverage": params.get("leverage")}
        if path.endswith("/order"):
            self.orders.append(dict(params))
            q = float(params["quantity"])
            if not self.ignore_orders:
                signed_q = q if params["side"] == "BUY" else -q
                self.qty = round(self.qty + signed_q, 8)
            return {"orderId": 1, "executedQty": str(q), "avgPrice": "77700"}
        raise AssertionError("未预期的端点 " + path)


class Feed:
    last_price = 77700.0


class Cfg:
    """外层配置的替身：执行器只读 cfg.trading.*。"""
    def __init__(self, **kw):
        self.trading = TradingCfg(**kw)


def make(fx, **kw):
    ex = LiveExecutor(Feed(), Cfg(**kw), "k", "s")
    ex._transport = fx
    return ex


def test_round_qty_floors_to_step():
    ex = make(FakeExchange())
    assert ex.round_qty(0.0019, 0.001) == 0.001
    assert ex.round_qty(0.002, 0.001) == 0.002
    assert ex.round_qty(0.0009, 0.001) == 0.0, "不足一步必须是 0，不能四舍五入进位"


@pytest.mark.asyncio
async def test_open_then_position_verified():
    fx = FakeExchange()
    ex = make(fx)
    r = await ex.market_order("buy", 0.002, "测试开仓")
    assert r.ok and fx.qty == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_silent_failure_triggers_halt_and_rollback():
    """下单返回成功但仓位没变 —— 必须发现、必须熔断。"""
    fx = FakeExchange()
    fx.ignore_orders = True
    ex = make(fx)
    r = await ex.market_order("buy", 0.002, "静默失败")
    assert not r.ok
    assert ex.halted, "核对不上时必须熔断"
    assert "持仓核对失败" in ex.halted


@pytest.mark.asyncio
async def test_halted_blocks_further_orders():
    fx = FakeExchange()
    fx.ignore_orders = True
    ex = make(fx)
    await ex.market_order("buy", 0.002, "第一次")
    n = len(fx.orders)
    r = await ex.market_order("buy", 0.002, "熔断后")
    assert not r.ok and len(fx.orders) == n, "熔断后不能再发单"


@pytest.mark.asyncio
async def test_halted_still_allows_closing():
    """熔断期间必须还能平仓——否则止损失效、敞口无上限。

    与 spot.py 同一条不变量：熔断是「账目不可信」的保守姿态，
    不是「不许交易」的通牒。拒绝平仓比账目暂时不准危险得多。
    """
    fx = FakeExchange(qty=0.002, entry=77000.0, wallet=100.0)
    ex = make(fx)
    ex.halted = "持仓核对失败（瞬态）"
    import time as _t
    ex.halted_ts = _t.time()
    r = await ex.market_order("sell", 0.002, "平仓", reduce_only=True)
    assert r.ok, u"熔断期间平仓被拒（%r），止损就死了" % (r.reason,)
    assert fx.qty == pytest.approx(0.0), "平仓单没有真正执行"


@pytest.mark.asyncio
async def test_real_halt_records_timestamp():
    """真实触发熔断时必须记下时刻，否则它永远不会过期。

    手工设 halted 会绕过置位代码，把「置位忘记记时间」放过去
    （变异验证里确实漏了）。所以这里制造一次真实的静默失败。
    """
    fx = FakeExchange(qty=0.0, entry=0.0, wallet=100.0)
    fx.ignore_orders = True
    ex = make(fx)
    import time as _t
    t0 = _t.time()
    r = await ex.market_order("buy", 0.002, "静默失败")
    assert not r.ok, "回报说成交、仓位没动，不该判成功"
    assert ex.halted, "这种情况必须熔断"
    assert getattr(ex, "halted_ts", 0) >= t0, (
        u"熔断没有记录置位时刻（halted_ts=%r），它就不可能自动过期。"
        % (getattr(ex, "halted_ts", None),))


@pytest.mark.asyncio
async def test_halt_expires_automatically():
    """熔断必须自己过期，否则 7x24 运行每次瞬态错误都要人工重启。"""
    fx = FakeExchange(qty=0.0, entry=0.0, wallet=100.0)
    ex = make(fx)
    ex.halted = "持仓核对失败（瞬态）"
    ex.halted_ts = 1.0                 # 很久以前
    r = await ex.market_order("buy", 0.002, "开仓")
    assert r.ok, u"过期的熔断没有自动解除：%r" % (r.reason,)
    assert ex.halted == ""


@pytest.mark.asyncio
async def test_min_notional_rejected():
    fx = FakeExchange()
    ex = make(fx)
    r = await ex.market_order("buy", 0.001, "太小")   # 0.001*77700=77.7 够
    assert r.ok
    # 把价格压到极低使名义不足
    Feed.last_price = 1000.0
    try:
        fx2 = FakeExchange()
        ex2 = make(fx2)
        r2 = await ex2.market_order("buy", 0.001, "名义不足")
        assert not r2.ok and "最小" in r2.reason
    finally:
        Feed.last_price = 77700.0


@pytest.mark.asyncio
async def test_reduce_only_flag_passed():
    fx = FakeExchange(qty=0.002, entry=77000)
    ex = make(fx)
    await ex.market_order("sell", 0.002, "平仓", reduce_only=True)
    assert fx.orders[-1].get("reduceOnly") == "true"
