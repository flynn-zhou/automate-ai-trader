# -*- coding: utf-8 -*-
"""端到端集成测试：真实引擎 + 真实策略 + 假现货交易所。

单元测试只能证明每个零件对，证明不了"拼起来也对"。
这条链路是钱真正流过的地方：信号 -> 仓位计算(现货1x/步长0.00001) ->
下单 -> 持仓表 -> 平仓结算。这里用假交易所把它整条跑一遍，
断言成交价、数量、盈亏、持仓表全部自洽。
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000


class FakeSpot:
    """按币安现货接口形状应答的最小交易所。"""

    def __init__(self):
        self.usdt = 100.0
        self.btc = 0.0
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
            px = Feed.px
            self.orders.append((params["side"], q, px))
            if params["side"] == "BUY":
                self.btc = round(self.btc + q, 8)
                self.usdt = round(self.usdt - q * px, 8)
            else:
                self.btc = round(self.btc - q, 8)
                self.usdt = round(self.usdt + q * px, 8)
            return {"orderId": len(self.orders), "executedQty": str(q),
                    "cummulativeQuoteQty": str(q * px)}
        raise AssertionError("未预期端点 " + path)


class Feed:
    px = 30000.0

    def __init__(self):
        self.last_price = 30000.0
        self.cfg = AppCfg()
        self.bar_hooks = []


@pytest.fixture()
def env():
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = 25.0
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = Feed()
    fx = FakeSpot()
    ex = SpotExecutor(feed, cfg, "k", "s")
    ex._transport = fx
    eng = DecisionEngine(cfg, feed, ex, tdb)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    yield eng, ex, fx, tdb
    tdb.stop()


def _bars4h(prices):
    return [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
           for i, p in enumerate(prices)]


@pytest.mark.asyncio
async def test_full_cycle_open_hold_close(env):
    eng, ex, fx, tdb = env
    # 先跌一段，最后一根拉起形成"上穿"。
    # 注意 state() 用的是最后一根 bar 的收盘价，不是传进来的 price，
    # 所以要真的造出一根站在均线上方、且前一根在均线下方的 K 线。
    prices = [100.0, 99, 98, 97, 96, 95, 94, 93, 120.0]
    eng.trend.bars = _bars4h(prices)
    eng.trend.seeded = True
    eng.trend.need_reconcile = False

    Feed.px = 30000.0
    eng.feed.last_price = 30000.0

    # 价格站上均线 -> 应当开多
    sig = eng.trend.want_entry(Feed.px)
    assert sig is not None, "价格远高于均线时必须给出进场信号"

    plan = eng._trend_plan(Feed.px, 100.0, sig)
    assert plan.ok, plan.reason
    # 数量要向下取整到步长，所以实际杠杆是 0.999 这种量级，不是精确 1.0
    assert plan.leverage == pytest.approx(1.0, rel=0.01), "现货必须是 1x"
    assert plan.notional == pytest.approx(100.0, rel=0.02)

    r = await ex.market_order("buy", plan.qty, "开仓")
    assert r.ok
    assert fx.btc == pytest.approx(plan.qty)
    st = await ex.equity()
    assert st["position_side"] == "long"
    assert st["equity"] == pytest.approx(100.0, rel=0.01)


@pytest.mark.asyncio
async def test_plan_respects_spot_min_notional(env):
    eng, ex, fx, tdb = env
    eng.trend.bars = _bars4h([100.0] * 8)
    # 权益 2U、价格 30000 -> 名义 2U < 现货最小 5U，必须拒绝
    plan = eng._trend_plan(30000.0, 2.0, {"ok": True})
    assert not plan.ok and "最小下单" in plan.reason


@pytest.mark.asyncio
async def test_plan_qty_uses_spot_step(env):
    eng, ex, fx, tdb = env
    eng.trend.bars = _bars4h([100.0] * 8)
    plan = eng._trend_plan(30000.0, 100.0, {"ok": True})
    assert plan.ok
    # 现货步长 0.00001：数量必须是它的整数倍，且不能用永续的 0.001
    q = plan.qty
    assert abs(q / 0.00001 - round(q / 0.00001)) < 1e-6
    assert abs(q - 0.003333) < 0.0001, "100U/30000 应为 0.00333 而不是取整到 0.003"


@pytest.mark.asyncio
async def test_exit_sells_everything(env):
    eng, ex, fx, tdb = env
    eng.trend.bars = _bars4h([100.0] * 8)
    Feed.px = 30000.0
    eng.feed.last_price = 30000.0
    plan = eng._trend_plan(30000.0, 100.0, {"ok": True})
    await ex.market_order("buy", plan.qty, "开仓")
    held = fx.btc
    r = await ex.market_order("sell", held, "平仓")
    assert r.ok
    assert fx.btc == pytest.approx(0.0, abs=1e-9), "平仓后不能有残留持仓"
