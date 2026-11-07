# -*- coding: utf-8 -*-
"""生产配置（h=0.2%）下，擦边交叉必须【一张单都不发】。

为什么单独写这一条：
  项目里所有实盘链路 e2e 都用 AppCfg() 的默认值跑，而
  trend_hysteresis_pct 的默认是 0.0；生产环境是 0.2。
  也就是说"生产配置下的实盘开仓路径"从未被验证过。

  两者唯一真正不同的地方就是擦边交叉：
    收盘刚高过裸均线一点点 -> h=0 会开仓，h=0.2 不该开仓。
  这个差别只在门槛附近显现，所以必须刻意把价格造在门槛附近，
  用大余量（比如均线 95、价格 120）去测是测不出来的——
  那种情形两档都通过，绿得毫无信息量。

这里走完整的实盘链路：引擎 _step -> _step_trend -> want_entry -> _open
-> SpotExecutor -> 真实 HTTP + HMAC 签名 -> 严格假币安。
"""
import os
import sys
import tempfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor
from ai_trader.market.agg import Bar
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy
from tools.fake_binance import FakeBinance

IV4H = 14400000
IV3S = 3000
PX = 30000.0


class _Hub:
    def publish(self, m):
        pass


class _Agg:
    def __init__(self, px):
        self._b = Bar(interval_ms=IV3S, open_time=0, open=px, high=px,
                      low=px, close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._b if iv == IV3S else None


class _Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self.agg = _Agg(px)
        self.src = "ws"


def _bars4h(prices):
    return [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
            for i, p in enumerate(prices)]


def _snap():
    return {}


async def _build(hyst, prices):
    """按指定滞回半宽搭一套真实的 引擎 + 执行器 + 假币安。"""
    fake = await FakeBinance(usdt=100.0, btc=0.0, price=PX).start()
    tmp = tempfile.mkdtemp()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = 25.0
    cfg.trading.trend_min_notional = 5.0
    cfg.trading.trend_hysteresis_pct = hyst
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = _Feed(PX)
    ex = SpotExecutor(feed, cfg, fake.api_key, fake.api_secret, base=fake.base())
    eng = DecisionEngine(cfg, feed, ex, tdb)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.bars = _bars4h(prices)
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    eng.feed.last_price = PX
    eng._last_equity = 100.0
    return eng, ex, fake, tdb


async def _teardown(eng, ex, fake, tdb):
    tdb.stop()
    try:
        await ex.close()
    except Exception:
        pass
    await fake.stop()


# 前 8 根递减到 93，最后一根 95.1。
#   SMA5（不含最后一根）= mean(93,94,95,96,97) = 95.0
#   裸门槛 95.0        -> 95.1 > 95.0   成立（h=0 会开仓）
#   带 0.2% 门槛 95.19 -> 95.1 > 95.19  不成立（h=0.2 不该开仓）
PRICES_BAND = [100.0, 99, 98, 97, 96, 95, 94, 93, 95.1]
LAST = 95.1


@pytest.mark.asyncio
@pytest.mark.parametrize("hyst,should_open", [(0.0, True), (0.2, False)])
async def test_marginal_cross_opens_only_without_band(hyst, should_open):
    """擦边交叉：h=0 开仓，h=0.2 一张单都不发。"""
    eng, ex, fake, tdb = await _build(hyst, PRICES_BAND)
    try:
        st = eng.trend.state(LAST)
        assert st["sma"] == pytest.approx(95.0), "构造前提：SMA5 必须是 95.0"
        assert st["above"] is True, "构造前提：收盘确实高于裸均线"

        sig = eng.trend.want_entry(LAST)
        if should_open:
            assert sig is not None, (
                "h=0 时 95.1 > 95.0 必须触发进场信号")
        else:
            assert sig is None, (
                "h=0.2%% 时 95.1 还在 95.19 门槛之下，不该有任何进场信号")
            assert st["enter_above"] is False

        # 走真实的 3 秒节拍，看交易所到底有没有收到单
        before = len(fake.orders)
        await eng._step(_snap())
        after = len(fake.orders)

        if should_open:
            assert after > before, (
                "h=0 时应当真的发出买单，实际订单数没变（%d -> %d）"
                % (before, after))
            assert eng.pos is not None, "开仓成功后引擎必须记录持仓"
        else:
            assert after == before, (
                "h=0.2%% 时不该发出任何订单，实际发了 %d 张。"
                "说明 _step 没有走带滞回的进场谓词——生产配置下的门槛是假的。"
                % (after - before))
            assert eng.pos is None, "不该建仓"
    finally:
        await _teardown(eng, ex, fake, tdb)


@pytest.mark.asyncio
@pytest.mark.parametrize("hyst", [0.0, 0.2])
async def test_decisive_cross_opens_at_both_settings(hyst):
    """反证：明确突破（远超门槛）时，两档都必须开仓。

    否则上面那条可以靠"永远不开仓"通过——那就是把滞回变成了"不止损/不进场"。
    """
    prices = [100.0, 99, 98, 97, 96, 95, 94, 93, 120.0]
    eng, ex, fake, tdb = await _build(hyst, prices)
    try:
        sig = eng.trend.want_entry(120.0)
        assert sig is not None, "价格 120 远高于门槛，必须触发信号"
        before = len(fake.orders)
        await eng._step(_snap())
        assert len(fake.orders) > before, "明确突破必须真的下单"
        assert eng.pos is not None
    finally:
        await _teardown(eng, ex, fake, tdb)
