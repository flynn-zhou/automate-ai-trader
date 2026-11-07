# -*- coding: utf-8 -*-
"""现货买入再卖出：这一整个往返必须走得通。

为什么单独测这个：
  真实币安现货买入时，手续费扣在【成交得到的币】上（买 BTC 就扣 BTC）。
  所以 executedQty（成交数量）> 实际到账数量。
  如果拿 executedQty 当持仓数量，平仓卖出时会多卖 0.1%，
  交易所直接以"余额不足"拒单——仓位永远平不掉，止损也就形同虚设。

  这个 bug 在旧版假交易所上测不出来，因为它把手续费记在了 USDT 上。
  也就是说：假交易所比现实宽容，于是真盘上才会爆。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor
from tools.fake_binance import FakeBinance

PX = 77815.0


class Feed:
    def __init__(self, px=PX):
        self.last_price = px
        self.cfg = AppCfg()


async def _make(fake):
    cfg = AppCfg()
    cfg.trading.symbol = "BTCUSDT"
    feed = Feed()
    ex = SpotExecutor(feed, cfg, fake.api_key, fake.api_secret, base=fake.base())
    return ex, feed


@pytest.mark.asyncio
async def test_buy_then_full_sell_round_trip_succeeds():
    """买进来多少，就必须能原样卖出去多少。"""
    fake = await FakeBinance(usdt=100.0, btc=0.0, price=PX).start()
    try:
        ex, feed = await _make(fake)

        buy = await ex.market_order("buy", 0.00128, reason="测试开仓")
        assert buy.ok, "买入失败：" + str(buy.reason)
        assert buy.qty > 0

        # 关键：记录的持仓数量必须 <= 实际持有的币。
        # 否则下一步卖出必然被拒。
        bal = (await ex.equity())["position_qty"]
        assert buy.qty <= bal + 1e-12, (
            "记录持仓 %.8f 超过了实际余额 %.8f——平仓会因余额不足被拒"
            % (buy.qty, bal))

        sell = await ex.market_order("sell", buy.qty, reason="测试平仓", reduce_only=True)
        assert sell.ok, (
            "卖出失败：%s。这说明买入记录的数量大于实际持仓，"
            "真实盘上仓位将永远平不掉。" % sell.reason)

        # 卖出量会被 step 向下取整，所以必然留下一点残留（< 1 step）。
        # 这个方向是对的：宁可留一点粉尘，也不能向上取整导致"余额不足"被拒。
        # 残留会在下一次买入时并入，攒够一个 step 就能一起卖掉，不会丢失。
        after = (await ex.equity())["position_qty"]
        step = 0.00001
        assert 0 <= after < step, (
            "平仓后残留 %.8f 超出了 step %.5f 的合理范围" % (after, step))
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_buy_records_net_quantity_after_btc_fee():
    """买入记录的必须是【扣掉 BTC 手续费之后】的到账数量。"""
    fake = await FakeBinance(usdt=100.0, btc=0.0, price=PX).start()
    try:
        ex, feed = await _make(fake)
        qty_in = 0.00128
        buy = await ex.market_order("buy", qty_in, reason="测试")
        assert buy.ok

        bal = (await ex.equity())["position_qty"]
        assert buy.qty == pytest.approx(bal, abs=1e-9), (
            "记录 %.8f 与实际余额 %.8f 不一致" % (buy.qty, bal))

        # 0.1% taker：到账应约为 99.9%
        assert buy.qty == pytest.approx(qty_in * 0.999, rel=1e-2), (
            "净到账 %.8f 不符合扣 0.1%% 手续费的预期" % buy.qty)
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_fee_is_reported_in_usdt_equivalent():
    """费用统一折算成 USDT，才能和 pnl 相加。"""
    fake = await FakeBinance(usdt=100.0, btc=0.0, price=PX).start()
    try:
        ex, feed = await _make(fake)
        buy = await ex.market_order("buy", 0.00128, reason="测试")
        assert buy.ok
        # 手续费约 0.1% 名义额 ≈ 0.0996U
        assert buy.fee == pytest.approx(0.00128 * PX * 0.001, rel=0.15), (
            "手续费折算 %.6fU 不合理" % buy.fee)
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_partial_position_sell_leaves_correct_remainder():
    """只卖一半时，剩下的必须等于 总量 - 卖出量。"""
    fake = await FakeBinance(usdt=200.0, btc=0.0, price=PX).start()
    try:
        ex, feed = await _make(fake)
        buy = await ex.market_order("buy", 0.001, reason="测试")
        assert buy.ok
        half = ex.round_qty(buy.qty / 2, 0.00001)
        sell = await ex.market_order("sell", half, reason="测试半平", reduce_only=True)
        assert sell.ok, "半平失败：" + str(sell.reason)
        left = (await ex.equity())["position_qty"]
        assert left == pytest.approx(buy.qty - half, abs=1e-8)
    finally:
        await fake.stop()
