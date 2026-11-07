# -*- coding: utf-8 -*-
"""真正的端到端：决策引擎 -> 现货执行器 -> 真实 HTTP -> 签名 -> 成交 -> 记账 -> 平仓结算。

和已有的 test_engine_spot_e2e.py 的区别（那个不够）：
  1. 它用 ex._transport 注入假传输层，【完全绕过 HTTP 和签名】。
     签名算错、端点写错、字段名写错，那个测试全都发现不了。
  2. 它直接调 _trend_plan() 和 market_order()，【_open() / _close() 从没被执行过】。
     而这两个方法才是真正写数据库、算盈亏、维护 self.pos 的地方。

这组测试驱动 _step_trend()，走完整条链路，对着一个真的会算 HMAC、
真的会扣余额的 HTTP 服务。这是用户注资后第一次真实下单会走的全部代码。
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


class _Hub:
    """引擎在开/平仓后会把决策推给看板。测试里只记录，不渲染。"""
    def __init__(self):
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()


def _hist4h(prices):
    """策略内部 bars 的形态：和 seed_from_rest() 产出的 dict 一致。"""
    return [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
            for i, p in enumerate(prices)]


def _bars4h(prices):
    """喂给引擎的 bar 必须是真实的 Bar 对象，不能是 dict。

    on_bar() 读的是 bar.open_time / .open / .close 这些属性，
    getattr(dict, "open_time", None) 返回 None -> 直接 return False，
    【整根 K 线被静默丢弃】。我第一版用 dict 喂进去，结果策略永远少一根，
    上穿条件永远不成立，还以为是策略本身的问题。
    测试必须按真实数据结构写，不能按我对它的想象写。
    """
    return [Bar(interval_ms=IV4H, open_time=i * IV4H, open=p, high=p,
                low=p, close=p, volume=1.0, closed=True)
            for i, p in enumerate(prices)]


def _bars_by_iv(prices):
    return {IV4H: _bars4h(prices)}


def _seed(eng, prices):
    """按生产约定预置历史：bars 不含最后一根，最后一根由 bars_by_iv 送进来，
    由 _step_trend 里的 on_bar() 追加。

    这个约定很容易搞错：on_bar 会 append，而 state() 判的是 cl[-1]。
    如果把完整序列预先塞进 bars，再送一次同样的序列，
    就会被 on_bar 的 t <= bars[-1].t 去重，SMA 不是你以为的那个数。
    """
    eng.trend.bars = _hist4h(prices[:-1])
    eng.trend.seeded = True
    eng.trend.need_reconcile = False


@pytest_asyncio.fixture
async def env():
    fake = await FakeBinance(usdt=100.0, btc=0.0, price=30000.0).start()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = 25.0
    cfg.trading.taker_fee = 0.001
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = Feed(30000.0)
    ex = SpotExecutor(feed, cfg, fake.api_key, fake.api_secret, base=fake.base())
    eng = DecisionEngine(cfg, feed, ex, tdb, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    try:
        yield eng, ex, fake, tdb
    finally:
        await ex.close()
        await fake.stop()
        tdb.stop()


# 先跌一段，最后一根拉起来 -> 上穿 SMA（n=5）
_UP = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 93.0, 30000.0]
# 在高位横六根把均线抬到 30000 附近，再 30500 -> 24000 打下穿
_DOWN = _UP[:-1] + [30000.0] * 6 + [30500.0, 24000.0]


@pytest.mark.asyncio
async def test_step_trend_opens_real_position(env):
    """信号 -> _open() -> 真实下单 -> 交易所余额真的变了 -> 数据库有开仓行。"""
    eng, ex, fake, tdb = env
    _seed(eng, _UP)

    await eng._step_trend(_bars_by_iv(_UP), 30000.0, 100.0, 1_700_000_000_000)

    assert fake.orders, "引擎没有向交易所发出任何订单"
    assert fake.orders[0]["side"] == "BUY"
    assert fake.btc > 0, "交易所余额没有变化，说明订单没真的成交"
    assert eng.pos is not None, "引擎没有记录持仓"
    assert eng.pos["side"] == "long"
    assert eng.pos["entry"] == pytest.approx(30000.0)

    import asyncio
    await asyncio.sleep(0.5)
    ot = tdb.open_trade()
    assert ot is not None, "数据库没有开仓记录"
    assert ot["entry_price"] == pytest.approx(30000.0)
    assert ot["stop_price"] > 0, "止损价不能是 0（等于永不止损）"
    assert ot["stop_price"] == pytest.approx(30000.0 * 0.75, rel=1e-6)


@pytest.mark.asyncio
async def test_step_trend_closes_and_settles(env):
    """跌破 SMA -> _close() -> 真实卖出 -> 数据库平仓。"""
    eng, ex, fake, tdb = env
    import asyncio

    _seed(eng, _UP)
    await eng._step_trend(_bars_by_iv(_UP), 30000.0, 100.0, 1_700_000_000_000)
    assert eng.pos is not None
    qty = eng.pos["qty"]
    assert fake.btc == pytest.approx(qty, rel=1e-9)

    fake.price = 24000.0
    eng.feed.last_price = 24000.0
    _seed(eng, _DOWN)
    await eng._step_trend(_bars_by_iv(_DOWN), 24000.0, 100.0, 1_700_100_000_000)

    assert eng.pos is None, "引擎仍认为有持仓，但已经卖出了"
    # 平仓后允许 < 1 step(0.00001) 的粉尘：卖出量按 step 向下取整，
    # 取整本身就是安全方向（宁可留下一点，也不能向上取整导致余额不足被拒）。
    # 粉尘会在下次买入时并入，攒够一个 step 可以一起卖掉，不会丢失。
    assert 0 <= fake.btc < 0.00001, (
        "平仓后残留 %.8f 超出 step 范围（step=0.00001）" % fake.btc)
    assert any(o["side"] == "SELL" for o in fake.orders), "没有发出卖单"

    await asyncio.sleep(0.5)
    assert tdb.open_trade() is None, "数据库里还有未平仓记录"


@pytest.mark.asyncio
async def test_order_failure_does_not_create_position(env):
    """下单被交易所拒绝时，绝不能留下一个"以为有仓"的状态。

    这是最危险的一类分歧：引擎以为有仓、实际交易所没有，
    于是止损永远打在一个不存在的仓上。
    """
    eng, ex, fake, tdb = env
    fake.reject_next_order = "Account has insufficient balance for requested action."
    _seed(eng, _UP)

    await eng._step_trend(_bars_by_iv(_UP), 30000.0, 100.0, 1_700_000_000_000)

    assert eng.pos is None, "下单失败却记录了持仓"
    assert fake.btc == pytest.approx(0.0, abs=1e-9)
