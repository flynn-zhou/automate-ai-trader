# -*- coding: utf-8 -*-
"""熔断止损路径：6.84 年回测里 0 次触发，所以这是从未真正执行过的代码。

现货 1x 没有爆仓，25% 熔断止损是【唯一的尾部保护】。
它恰恰会在最坏的时候执行——也就是最不该出问题的时候。

覆盖三件事：
  1. 正常熔断：真的发出卖单、清持仓
  2. 熔断后同一根 4h 不许立刻回场（否则会止损->再进->再止损）
  3. 平仓成功但【权益查询失败】时，持仓必须仍然被清掉

第 3 条是重点：卖单成交是既成事实，与"能不能查到余额"无关。

【构造陷阱，写在这里免得下次再踩】
熔断触发时会先向交易所复核价格（防行情陈旧导致假止损）。
所以只改本地价、不改假交易所的价，引擎会判定"行情陈旧，不执行"，
测试就会以一种看起来像缺陷、其实是前提错误的方式失败。
真实崩盘是两边一起跌，所以这里 fake.price 必须同步打下去。
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
STOP_PCT = 25.0


class _Hub:
    def publish(self, m):
        pass


class _Agg:
    def __init__(self, px):
        self.set(px)

    def set(self, px):
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


class _EquityFails:
    """透明代理：equity() 总是抛异常，其余一律转发给真执行器。

    只坏"查余额"这一件事，下单/签名/手续费照旧走真实路径——
    否则测的就不是这个缺陷了。
    """

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, k):
        return getattr(self._inner, k)

    @property
    def name(self):
        return self._inner.name

    async def equity(self):
        raise RuntimeError("模拟 /api/v3/account 超时")


def _bars4h(prices):
    return [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
            for i, p in enumerate(prices)]


async def _build(wrap=None):
    fake = await FakeBinance(usdt=100.0, btc=0.0, price=PX).start()
    tmp = tempfile.mkdtemp()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = STOP_PCT
    cfg.trading.trend_min_notional = 5.0
    cfg.trading.trend_hysteresis_pct = 0.2
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = _Feed(PX)
    ex = SpotExecutor(feed, cfg, fake.api_key, fake.api_secret, base=fake.base())
    use = wrap(ex) if wrap else ex
    eng = DecisionEngine(cfg, feed, use, tdb)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    bars = _bars4h([100.0, 99, 98, 97, 96, 95, 94, 93, 120.0])
    eng.trend.bars = bars
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    # 必须和 seed_from_rest() 保持一致：它补完历史后也要把 last_eval_bar
    # 推进到最后一根已收盘 K，否则防重入闸门（if last_eval_bar and ...）
    # 会因 0 为假被整体跳过。
    eng.trend.last_eval_bar = int(bars[-1]["t"])
    eng._last_equity = 100.0
    return eng, ex, fake, tdb, feed


async def _teardown(ex, fake, tdb):
    tdb.stop()
    try:
        await ex.close()
    except Exception:
        pass
    await fake.stop()


async def _open_long(eng, feed):
    feed.last_price = PX
    feed.agg.set(PX)
    sig = eng.trend.want_entry(PX)
    assert sig is not None, "构造前提：必须存在进场信号"
    plan = eng._trend_plan(PX, 100.0, sig)
    assert plan.ok, plan.reason
    await eng._open(sig, plan, PX, 100.0, 1793520000000, {}, {})
    assert eng.pos is not None, "构造前提：开仓必须成功"
    return plan


def _crash(eng, fake, feed, px):
    """让本地价与交易所价【同时】跌到 px —— 真实崩盘两边一致。"""
    feed.last_price = px
    feed.agg.set(px)
    fake.price = px


@pytest.mark.asyncio
async def test_hard_stop_really_closes():
    """跌破 25% 熔断线：必须真的卖出、清仓。"""
    eng, ex, fake, tdb, feed = await _build()
    try:
        plan = await _open_long(eng, feed)
        assert plan.stop == pytest.approx(PX * (1 - STOP_PCT / 100.0)), \
            "熔断线必须是 -25%"

        _crash(eng, fake, feed, plan.stop * 0.97)
        before = len([o for o in fake.orders if o["side"] == "SELL"])
        await eng._step({})

        sells = [o for o in fake.orders if o["side"] == "SELL"]
        assert len(sells) > before, "跌破熔断线必须发出卖单"
        assert eng.pos is None, "熔断后引擎必须清空持仓"

        acct = await ex.equity()
        assert acct["position_qty"] < 0.00001, (
            "熔断后交易所仍有 %.8f BTC" % acct["position_qty"])
        # 回撤应接近 25%，不能远超
        assert acct["equity"] > 100.0 * 0.72, (
            "熔断后权益 %.2f，比止损线跌得还多得多" % acct["equity"])
        assert acct["equity"] < 100.0, "必须扣了手续费"
    finally:
        await _teardown(ex, fake, tdb)


@pytest.mark.asyncio
async def test_stale_price_does_not_stop_out():
    """反向：只有本地价跌、交易所价没跌 —— 判为行情陈旧，不许误杀。

    这条是上一条的对照。没有它，上一条可以靠"任何下跌都止损"通过。
    """
    eng, ex, fake, tdb, feed = await _build()
    try:
        plan = await _open_long(eng, feed)
        # 只改本地价，交易所仍报 30000
        feed.last_price = plan.stop * 0.97
        feed.agg.set(plan.stop * 0.97)
        await eng._step({})
        assert eng.pos is not None, "本地价陈旧造成的假熔断不该把仓位卖掉"
        assert not [o for o in fake.orders if o["side"] == "SELL"], \
            "不该发出卖单"
    finally:
        await _teardown(ex, fake, tdb)


@pytest.mark.asyncio
async def test_hard_stop_blocks_reentry_same_bar():
    """熔断后同一根 4h 内不许立刻回场。"""
    eng, ex, fake, tdb, feed = await _build()
    try:
        plan = await _open_long(eng, feed)
        _crash(eng, fake, feed, plan.stop * 0.97)
        await eng._step({})
        assert eng.pos is None

        # 价格瞬间回抽到远高于均线（同一根 4h 内）
        _crash(eng, fake, feed, 200.0)
        n_before = len(fake.orders)
        await eng._step({})
        assert len(fake.orders) == n_before, (
            "熔断后同一根 4h 内又开了仓，会变成止损->再进->再止损的死循环")
        assert eng.pos is None
    finally:
        await _teardown(ex, fake, tdb)


@pytest.mark.asyncio
async def test_close_clears_position_even_if_equity_query_fails():
    """卖单已成交但权益查询失败：持仓仍必须被清掉。"""
    eng, ex, fake, tdb, feed = await _build(wrap=_EquityFails)
    try:
        plan = await _open_long(eng, feed)
        _crash(eng, fake, feed, plan.stop * 0.97)
        await eng._step({})

        sold = [o for o in fake.orders if o["side"] == "SELL"]
        assert sold, "构造前提：卖单必须已经成交（否则这条测的是别的东西）"

        assert eng.pos is None, (
            "卖单已成交、库里也记了平仓，但引擎仍认为自己持仓——"
            "清持仓被放在了会失败的权益查询之后。"
            "此后引擎不会再进场（漏掉最好那笔进场 = 总收益少 46%），"
            "还会反复去卖一个 0 余额的仓。")
    finally:
        await _teardown(ex, fake, tdb)


# ---------------- seed_from_rest 必须推进 last_eval_bar ----------------

def _fake_opener(rows):
    """替换 urllib 的 opener，返回给定 K 线，避免测试碰网络。"""
    class _Resp(object):
        def __init__(self, payload):
            self._p = payload

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener(object):
        def open(self, req, timeout=None):
            import json as _j
            return _Resp(_j.dumps(rows).encode())

    return lambda *a, **k: _Opener()


def test_seed_from_rest_sets_last_eval_bar(monkeypatch):
    """补历史之后，last_eval_bar 必须等于最后一根已收盘 K 的时间。

    不设的话它是 0，而熔断防重入的闸门写的是
    "if last_eval_bar and last_eval_bar <= block_bar"——
    0 为假，闸门整体失效，「止损->立即再进->再止损」在重启后的
    第一根 4h 内完全敞开，每次亏 25%。
    """
    import time as _t
    from ai_trader.trading import trend as trend_mod

    cfg = AppCfg().trading
    cfg.sma_lookback = 5
    st = TrendStrategy(cfg, symbol="BTCUSDT")

    now = int(_t.time() * 1000)
    rows = []
    t0 = now - 40 * IV4H
    for i in range(30):
        px = 100.0 + i
        rows.append([t0 + i * IV4H, str(px), str(px), str(px), str(px),
                     "1.0", 0, "0", 0, "0", "0", "0"])
    monkeypatch.setattr(trend_mod.urllib.request, "build_opener",
                        _fake_opener(rows))

    n = st.seed_from_rest(need=20)
    assert n > 0, "构造前提：必须真的补到历史"
    assert st.last_eval_bar != 0, (
        "seed_from_rest 没有推进 last_eval_bar —— 熔断防重入闸门在"
        "重启后的第一根 4h 内失效，会止损->立即再进->再止损")
    assert st.last_eval_bar == int(st.bars[-1]["t"]), (
        "last_eval_bar 必须等于最后一根已收盘 K 的时间")
