# -*- coding: utf-8 -*-
"""实盘执行器 vs 假币安：完整 HTTP + 签名 + 解析链路。

SpotExecutor / LiveExecutor 至今一次都没真正执行过——所有交易都是纸面的。
等用户注资后，第一次真实下单才会第一次走到这条链路。
这个项目所有致命 bug 都出现在"从未执行过的代码路径"上，所以必须现在测。

这组测试真的起 HTTP 服务、真的算 HMAC、真的扣余额，
测的是"请求能不能被币安接受"，而不只是"函数返回值对不对"。
"""
import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load
from ai_trader.exchange.spot import SpotExecutor, SpotError
from ai_trader.exchange.live import LiveExecutor, LiveError
from tools.fake_binance import FakeBinance


class _Feed:
    """执行器只用到 feed.last_price，别的一概不碰。"""
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


def _spot(fake, px=75000.0):
    return SpotExecutor(_Feed(px), _cfg(), fake.api_key, fake.api_secret,
                        base=fake.base())


def _perp(fake, px=75000.0):
    return LiveExecutor(_Feed(px), _cfg(), fake.api_key, fake.api_secret,
                        base=fake.base())


@pytest.mark.asyncio
async def test_spot_spec_parsed_from_real_shape(fake):
    ex = _spot(fake)
    try:
        spec = await ex.spec()
        assert spec["step"] == 0.00001, spec
        assert spec["min_notional"] == 5.0, spec
        assert spec["base"] == "BTC" and spec["quote"] == "USDT"
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_spot_equity_reads_balances(fake):
    ex = _spot(fake)
    try:
        e = await ex.equity()
        assert e["balance"] == 100.0
        assert e["position_qty"] == 0.0
        assert e["equity"] == 100.0
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_spot_buy_then_sell_roundtrip(fake):
    """真实走一遍：签名 -> 下单 -> 解析成交 -> 余额核对。"""
    ex = _spot(fake)
    try:
        r = await ex.market_order("buy", 0.001, "test")
        assert r.ok, r.reason
        # 关键：买入 0.001 BTC，但到账只有 0.000999。
        # 真实币安现货把手续费扣在【得到的币】上（0.1% = 0.000001 BTC），
        # 所以 r.qty 必须是扣费后的净额，否则平仓会多卖而被拒。
        assert r.qty == pytest.approx(0.000999, rel=1e-6), (
            "记录数量必须是扣掉 BTC 手续费后的净到账，实际 %.8f" % r.qty)
        assert r.price == pytest.approx(75000.0)
        assert r.fee == pytest.approx(75.0 * 0.001, rel=1e-6)
        # 交易所那边的余额真的动了（同样是净额）
        assert fake.btc == pytest.approx(0.000999, rel=1e-6)

        # 卖的就是我们记录的那个净额——这才卖得掉
        r2 = await ex.market_order("sell", r.qty, "close")
        assert r2.ok, (
            "卖出失败：%s。记录数量大于实际持仓时，真实盘上仓位永远平不掉。"
            % r2.reason)
        # 卖出量按 step(0.00001) 向下取整：0.000999 -> 0.00099，留下 9e-06 粉尘。
        # 这个方向安全（宁可留一点，也不能向上取整被拒单），
        # 粉尘会在下次买入时并入，攒够一个 step 可一起卖出。
        assert 0 <= fake.btc < 0.00001, (
            "平仓后残留 %.8f 超出 step 范围" % fake.btc)
        assert fake.usdt < 100.0            # 两次手续费
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_spot_rejects_bad_signature(fake):
    """签名算错必须被交易所拒——否则测的只是假传输层。"""
    ex = SpotExecutor(_Feed(75000.0), _cfg(), fake.api_key, "WRONG_SECRET",
                      base=fake.base())
    try:
        with pytest.raises(SpotError) as ei:
            await ex.equity()
        assert "-1022" in str(ei.value) or "Signature" in str(ei.value), ei.value
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_spot_rejects_bad_api_key(fake):
    ex = SpotExecutor(_Feed(75000.0), _cfg(), "nope", fake.api_secret,
                      base=fake.base())
    try:
        with pytest.raises(SpotError) as ei:
            await ex.equity()
        assert "-2015" in str(ei.value) or "API-key" in str(ei.value), ei.value
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_spot_buy_truncates_to_available_balance(fake):
    """余额买不满时应当截断成交，而不是整单失败。

    设计意图写在 spot.py 里："能买多少买多少，不是整单失败"。
    这也是现场最容易出问题的地方：100U 的账户想买 0.01 BTC（750U），
    交易所会直接 -2010 拒单，而截断后能正常建仓。
    """
    ex = _spot(fake)
    try:
        r = await ex.market_order("buy", 0.01, "too big")   # 750U > 100U
        assert r.ok, r.reason
        assert r.qty < 0.01, "没有截断，订单会超出余额"
        # 100U / 75000 / 1.0015，向下取整到 0.00001 步长
        assert r.qty == pytest.approx(0.00133, abs=1e-5)
        assert fake.usdt >= 0, "截断后余额不应为负"
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_spot_exchange_rejection_becomes_ok_false_not_exception(fake):
    """行情断线（last_price=0）时截断被跳过，交易所会真的拒单。

    这条才是危险路径：px 为 0 时截断逻辑整段被跳过，
    订单直达交易所、返回 HTTP 400。
    必须变成 FillResult(ok=False) 而不是抛异常——抛异常会冒到主循环把它打死。
    """
    ex = SpotExecutor(_Feed(0.0), _cfg(), fake.api_key, fake.api_secret,
                      base=fake.base())
    try:
        r = await ex.market_order("buy", 0.01, "no price")
        assert not r.ok, "余额不足却被当成成功"
        assert r.reason, "失败必须带原因"
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_perp_spec_parsed(fake):
    ex = _perp(fake)
    try:
        spec = await ex.spec()
        assert spec["step"] == 0.001
        assert spec["min_notional"] == 50.0
        assert spec["tick"] == 0.10
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_perp_set_leverage_reaches_exchange(fake):
    """set_leverage 必须真的打到交易所——它此前全项目零调用。"""
    ex = _perp(fake)
    try:
        await ex.set_leverage(3)
        assert fake.leverage == 3, "杠杆没有真的传到交易所"
        # 幂等：同样的值不再发请求
        n = len(fake.calls)
        await ex.set_leverage(3)
        assert len(fake.calls) == n, "幂等检查失效，重复发了请求"
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_perp_mark_price_from_premium_index(fake):
    ex = _perp(fake)
    try:
        px = await ex.mark_price()
        assert px == pytest.approx(75000.0)
        assert ("GET", "/fapi/v1/premiumIndex") in fake.calls
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_perp_open_and_close_position(fake):
    ex = _perp(fake)
    try:
        r = await ex.market_order("buy", 0.001, "open")
        assert r.ok, r.reason
        pos = await ex.position()
        assert pos["qty"] == pytest.approx(0.001)
        assert pos["entry"] == pytest.approx(75000.0)
        assert pos["side"] == "long"

        r2 = await ex.market_order("sell", 0.001, "close", reduce_only=True)
        assert r2.ok, r2.reason
        pos2 = await ex.position()
        assert pos2["qty"] == pytest.approx(0.0, abs=1e-9)
    finally:
        await ex.close()
