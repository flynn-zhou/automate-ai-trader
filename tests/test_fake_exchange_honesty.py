# -*- coding: utf-8 -*-
"""被交易所拒掉的单，不能出现在成交明细里。

这不是在测「假交易所」，是在测【我们的测试基础设施有没有骗我们】。

真实事故形状：
  「买 0.001 然后卖 0.001」这条用例曾经是绿的，可那笔卖单其实被 -2010 拒了
  （手续费扣在 BTC 上，到账只有 0.000999）。假交易所在余额检查【之前】
  就写了成交明细，于是 cost_basis 回放时看到一张不存在的成交，刚好算出 0。
  它验的不是「卖出后归零」，而是「假交易所帮我记了一笔没发生的成交」。

假交易所比现实宽容，验证的就是一个不存在的世界。
这条用例直接钉住那个宽容点：任何被拒的订单都不得进入 myTrades。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fake_binance import FakeBinance

# 这里刻意【不用】SpotExecutor：执行器现在会把超量卖出截断掉，
# 那样就打不到"被交易所拒单"这条路径了。要验的是交易所那一侧，
# 所以直接发签名请求。


@pytest.mark.asyncio
async def test_rejected_order_is_absent_from_my_trades():
    """余额不足被 -2010 拒掉的卖单，不得写进成交明细。"""
    f = await FakeBinance(usdt=1000.0, btc=0.0005, price=40000.0).start()
    try:
        # 直接对交易所发一张必然被拒的单，绕开执行器的截断
        n_before = len(f.trades)
        with pytest.raises(Exception) as ei:
            await _raw_sell(f, 0.001)
        assert "-2010" in str(ei.value), "这单应当是余额不足被拒"
        assert len(f.trades) == n_before, (
            "被拒的订单写了一笔成交明细（%d -> %d）。"
            "任何依赖 myTrades 的测试都会被这个宽容骗过去："
            "对账、成本基准、边际统计都会看到不存在的成交。"
            % (n_before, len(f.trades)))
    finally:
        await f.stop()


@pytest.mark.asyncio
async def test_notional_rejected_order_is_absent_from_my_trades():
    """名义额不足被 -1013 拒掉的单，同样不得写进成交明细。"""
    f = await FakeBinance(usdt=1000.0, btc=0.001, price=40000.0).start()
    try:
        n_before = len(f.trades)
        with pytest.raises(Exception) as ei:
            await _raw_sell(f, 0.00001)          # 0.4U < 5U
        assert "-1013" in str(ei.value)
        assert len(f.trades) == n_before, (
            "名义额不足被拒的单也写了成交明细")
    finally:
        await f.stop()


async def _raw_sell(f, qty):
    """绕过 SpotExecutor，直接用签名请求打假交易所。"""
    import hashlib
    import hmac
    import time as _t
    from urllib.parse import urlencode

    import aiohttp

    p = {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET",
         "quantity": "%.8f" % qty, "timestamp": int(_t.time() * 1000),
         "recvWindow": 5000}
    qs = urlencode(p)
    sig = hmac.new(f.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    async with aiohttp.ClientSession() as s:
        async with s.post(f.base() + "/api/v3/order",
                          data=qs + "&signature=" + sig,
                          headers={"X-MBX-APIKEY": f.api_key}) as r:
            txt = await r.text()
            if r.status != 200:
                raise RuntimeError(txt)
            return txt
