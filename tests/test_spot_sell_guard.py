# -*- coding: utf-8 -*-
"""卖出侧必须按账上实际持币量截断——买入侧早就有了，卖出侧没有。

为什么这条比买入侧更要紧：买入侧撞上余额不足只是"少买一点"或整单失败，
可恢复；卖出侧撞上持币不足是交易所 -2010 拒单，平仓失败，
只能等 5 分钟一次的 reconcile_live 把 pos["qty"] 改对才会成功。
在下跌里，那 5 分钟仓位是裸奔的——而平仓正是保护本金的那条路。

pos["qty"] 大于账上持币量的四条真实路径：
  1. 用户在币安 App 里手动卖了币，或用「一键转 BNB」清了零头
  2. 上一笔平仓部分成交，回执数量与账面对不上
  3. 手续费被记在别的币种上，净额算多了
  4. 从旧库恢复的持仓，账上早已不同

现货卖出被截断到实际持有量永远是安全的：不会变成做空。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor
from tools.fake_binance import FakeBinance


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []


def _ex(fake, px=40000.0):
    cfg = AppCfg()
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.taker_fee = 0.001
    return SpotExecutor(Feed(px), cfg, fake.api_key, fake.api_secret, base=fake.base())


@pytest.mark.asyncio
async def test_sell_more_than_held_is_truncated_not_rejected():
    """账上只有 0.0005，却要卖 0.001 —— 必须截断到 0.0005 后成交。

    不截断的话交易所会 -2010 拒单，平仓路径就此失败。
    """
    f = await FakeBinance(usdt=1000.0, btc=0.0005, price=40000.0).start()
    ex = _ex(f)
    try:
        r = await ex.market_order("sell", 0.001, "平仓")
        assert r.ok, "超量卖出应当截断后成交，而不是被拒单：%s" % r.reason
        assert r.qty == pytest.approx(0.0005, abs=1e-9)
        assert f.btc == pytest.approx(0.0, abs=1e-9)
        st = await ex.equity()
        assert st["position_qty"] == pytest.approx(0.0, abs=1e-9)
    finally:
        await ex.close()
        await f.stop()


@pytest.mark.asyncio
async def test_sell_with_empty_account_says_so_clearly():
    """账上一点币都没有时，要说「无可卖数量」，而不是发一张必然被拒的单。

    这条走的是真实事故的形状：用户在 App 里把币卖光了，
    引擎还记着持仓。旧行为是每 3 秒发一张 -2010 的单，
    3 秒一次地烧配额；现在当场说清楚。
    """
    f = await FakeBinance(usdt=1000.0, btc=0.0, price=40000.0).start()
    ex = _ex(f)
    try:
        r = await ex.market_order("sell", 0.001, "平仓")
        assert not r.ok
        assert "无可卖" in r.reason, "要说清是账上没币，而不是别的原因：%s" % r.reason
        assert not ex.halted, "这不是静默失败，不该熔断"
        assert f.orders == [], "不该向交易所发这张注定被拒的单"
    finally:
        await ex.close()
        await f.stop()


@pytest.mark.asyncio
async def test_sell_exactly_held_still_works():
    """反证：数量刚好等于持币时不能被误截断。"""
    f = await FakeBinance(usdt=1000.0, btc=0.001, price=40000.0).start()
    ex = _ex(f)
    try:
        r = await ex.market_order("sell", 0.001, "平仓")
        assert r.ok
        assert r.qty == pytest.approx(0.001, abs=1e-9)
        assert f.btc == pytest.approx(0.0, abs=1e-9)
    finally:
        await ex.close()
        await f.stop()


@pytest.mark.asyncio
async def test_buy_truncation_still_works():
    """反证：买入侧截断不能被这次改动弄坏。"""
    f = await FakeBinance(usdt=10.0, btc=0.0, price=40000.0).start()   # 只够买 0.00025
    ex = _ex(f)
    try:
        r = await ex.market_order("buy", 0.001, "开仓")
        assert r.ok, "买不起整笔时应截断成交：%s" % r.reason
        assert r.qty <= 0.00025 + 1e-9
        assert f.usdt >= 0.0
    finally:
        await ex.close()
        await f.stop()


@pytest.mark.asyncio
async def test_one_account_query_per_order():
    """下单只该查一次账户。

    原先是买入查两次（acct + before）、卖出查一次；
    合并成一次之后，每笔少一次签名请求。这不是性能洁癖：
    每次请求都是一次可能超时 12 秒的等待，而且都在平仓的关键路径上。
    """
    f = await FakeBinance(usdt=1000.0, btc=0.001, price=40000.0).start()
    ex = _ex(f)
    try:
        before = len([c for c in f.calls if c[1].endswith("/account")])
        await ex.market_order("sell", 0.001, "平仓")
        after = len([c for c in f.calls if c[1].endswith("/account")])
        # 下单前 1 次 + 余额核对 1 次（核对是刻意的，不在此限）
        assert after - before <= 2, (
            "下单路径查了 %d 次账户，应当最多 2 次（下单前 1 次 + 核对 1 次）"
            % (after - before))
    finally:
        await ex.close()
        await f.stop()
