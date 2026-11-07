# -*- coding: utf-8 -*-
"""系统将来的【第一笔真实交易】，完整走引擎自己的决策循环。

为什么单独写这一条
------------------
现有的实盘排练（test_live_full_rehearsal.py）很扎实，但它开仓是这样做的：

    sig  = eng.trend.want_entry(PX)        # 手动问信号
    plan = eng._trend_plan(PX, 100.0, sig) # 手动算计划
    await eng._open(sig, plan, ...)        # 手动下单

也就是说：信号判断、仓位计算、下单三段各自都测了，但
**「引擎自己在 _step 里决定要不要开仓」这段编排没被测过**。

而这三段之间的编排恰恰是有内容的：
  - 闸门顺序（ready -> 熔断 -> 买得起 -> 退避 -> want_entry）
  - 「本根 4h 已熔断出局」用 _trend_block_bar 拦住重开
  - 下单失败后的退避 _order_retry_ts
  - 买入前 _equity_stale 拒绝入场

这些全在 _step_trend 里，只有真的调 _step 才会走到。
真实运行时，用户的第一笔交易就是这条路径产生的。

这条测试不 mock 引擎内部任何一步：喂真实 4h K 线，调 eng._step()，
让引擎自己开仓、自己持仓、自己平仓。
"""
import os
import sys
import tempfile

import pytest

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
    """每轮 _step 都要传 bars_by_iv；引擎只从里面取 4h 那一档。

    这里给空列表，因为策略的 4h 历史已经直接设在 trend.bars 上。
    给空 4h 也顺带验证了「这一轮没有新 4h 收盘」时引擎照样能判断，
    而这正是真实运行 99.99% 的形态（4 小时才有一根）。
    """
    return {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}


@pytest.fixture()
def env():
    import asyncio

    loop = asyncio.new_event_loop()
    fake = loop.run_until_complete(
        FakeBinance(usdt=100.0, btc=0.0, price=PX).start())
    tmp = tempfile.mkdtemp()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = 25.0
    cfg.trading.trend_min_notional = 5.0
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = _Feed(PX)
    ex = SpotExecutor(feed, cfg, fake.api_key, fake.api_secret, base=fake.base())
    eng = DecisionEngine(cfg, feed, ex, tdb)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    try:
        yield eng, ex, fake, tdb, feed, loop
    finally:
        try:
            loop.run_until_complete(ex.close())
        except Exception:
            pass
        loop.run_until_complete(fake.stop())
        loop.close()
        tdb.stop()


def _run(loop, coro):
    return loop.run_until_complete(coro)


def test_engine_opens_the_first_trade_by_itself(env):
    """引擎调 _step 自己开仓 —— 不需要任何人手动调 _open。"""
    eng, ex, fake, tdb, feed, loop = env

    # 前 8 根在均线下方，最后一根拉起 -> 上穿。
    # 8 根 100/99/.../93 的均值 = 96.0，最后一根 120 远在均线上方。
    eng.trend.bars = _bars4h([100.0, 99, 98, 97, 96, 95, 94, 93, 120.0])
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    eng._last_equity = 100.0

    assert eng.pos is None
    assert len(fake.orders) == 0

    _run(loop, eng._step(_snap()))

    # 引擎必须【自己】下单，且只下一单
    assert len(fake.orders) == 1, (
        "引擎没有自己开仓：订单数 %d。这条路径就是用户第一笔真实交易走的路。"
        % len(fake.orders))
    assert fake.orders[0]["side"] == "BUY"
    assert eng.pos is not None, "下单成功了却没记持仓，止损会失去保护对象"
    assert eng.pos["side"] == "long"
    assert eng.pos["qty"] > 0
    assert eng.pos["stop"] > 0, (
        "止损价是 0 等于这道保护静默消失，仓位会一直挂到跌破均线才出场")

    # 数据库里必须有未平仓记录（重启后靠它恢复）。
    # tdb 是单写入队列，落盘有延迟，必须等而不是 sleep 一次。
    import time as _t
    ot = None
    deadline = _t.time() + 8.0
    while _t.time() < deadline:
        ot = tdb.open_trade()
        if ot is not None:
            break
        _t.sleep(0.1)
    assert ot is not None, "成交了但 trades 表没有未平仓行，重启会丢仓"
    assert ot["side"] == "long"
    assert ot["stop_price"] > 0, "落库的止损价是 0，重启后会失去保护"


def test_engine_does_not_open_twice(env):
    """同一段行情里连续调 _step，不能重复开仓。"""
    eng, ex, fake, tdb, feed, loop = env
    eng.trend.bars = _bars4h([100.0, 99, 98, 97, 96, 95, 94, 93, 120.0])
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    eng._last_equity = 100.0

    _run(loop, eng._step(_snap()))
    n_after_first = len(fake.orders)
    assert n_after_first == 1

    # 再来 5 轮（真实运行时每 3 秒一轮）
    for _ in range(5):
        _run(loop, eng._step(_snap()))

    assert len(fake.orders) == n_after_first, (
        "持仓中又开了新仓：订单数 %d -> %d。"
        "趋势策略 1x 满仓，重复买入会直接击穿杠杆约束。"
        % (n_after_first, len(fake.orders)))


def test_engine_closes_by_itself_and_settles(env):
    """价格下穿均线后，引擎自己平仓并把盈亏写进 trades 表。"""
    eng, ex, fake, tdb, feed, loop = env
    eng.trend.bars = _bars4h([100.0, 99, 98, 97, 96, 95, 94, 93, 120.0])
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    eng._last_equity = 100.0

    _run(loop, eng._step(_snap()))
    assert eng.pos is not None
    entry_qty = eng.pos["qty"]

    # 换一根跌到均线下方的 4h。5 根 [120]*5 + [90] 的均值 = 115，
    # 90 明显在下方 -> 下穿。
    eng.trend.bars = _bars4h([120.0] * 5 + [90.0])
    price_down = PX * 0.90
    feed.last_price = price_down
    # 假交易所的成交价是它自己的 self.price（不是 feed.last_price），
    # 所以要连它一起改；否则卖单还按 30000 成交，毛利恒为 0。
    fake.price = price_down
    eng.feed.agg._b = Bar(interval_ms=IV3S, open_time=0, open=price_down,
                          high=price_down, low=price_down, close=price_down,
                          volume=1.0, closed=True)

    _run(loop, eng._step(_snap()))

    assert eng.pos is None, "价格下穿均线了，引擎还没平仓"
    sells = [o for o in fake.orders if o["side"] == "SELL"]
    assert len(sells) == 1, "必须恰好发出一张卖单，实际 %d" % len(sells)

    # 平仓必须落库并结算
    import time as _t
    deadline = _t.time() + 6.0
    closed = None
    while _t.time() < deadline:
        closed = tdb.closed_trades(limit=5)
        if closed:
            break
        _t.sleep(0.1)
    assert closed, "平仓了但 trades 表没有已平仓记录，纸面账本会永远停在开仓那一刻"

    t = closed[0]
    assert t["exit_price"] > 0
    # ---- 盈亏必须按【实际卖出量】算，不是按持仓量 ----
    #
    # 现货卖出按 step=0.00001 向下取整，所以卖出量必然略小于持仓量，
    # 差额留在账户里变成卖不掉的粉尘（小于一个 step，交易所不收）。
    # 拿持仓量算盈亏会系统性高估——这是「记账用实际成交量」那条规则的另一半。
    step = 0.00001
    sold_qty = round(int(entry_qty / step) * step, 8)
    assert t["qty"] == pytest.approx(sold_qty, abs=1e-9), (
        "落库数量 %.8f 不是 step 向下取整后的成交量 %.8f" % (t["qty"], sold_qty))
    assert t["qty"] < entry_qty, (
        "卖出量 %.8f 不小于持仓量 %.8f——这个用例没造出 step 取整残留，"
        "粉尘那条不变量就没被验到" % (t["qty"], entry_qty))

    expect_gross = (t["exit_price"] - t["entry_price"]) * t["qty"]
    assert expect_gross < 0, "价格跌了，多头毛亏必须为负"
    assert t["pnl_gross"] == pytest.approx(expect_gross, rel=1e-6, abs=1e-9), (
        "毛盈亏 %.6f 与 (出场-入场)*实际卖出量 %.6f 不符" % (t["pnl_gross"], expect_gross))
    assert t["fee"] > 0, "两笔手续费必须被记下来，否则净盈亏是虚的"
    # 净 = 毛 - 两笔费。差额必须正好是手续费，不能是别的数。
    assert t["pnl_net"] == pytest.approx(t["pnl_gross"] - t["fee"], rel=1e-6, abs=1e-9), (
        "净=毛-费 这条账不平：%.6f != %.6f - %.6f"
        % (t["pnl_net"], t["pnl_gross"], t["fee"]))

    # ---- 粉尘的钱不能凭空消失 ----
    #
    # 已平仓记录只覆盖卖出去的那部分；剩下那点 BTC 还在账户里。
    # 如果 equity() 不把它算进去，这笔钱就等于被系统"吃掉"了——
    # 每笔交易吞一点，账户会缓慢漏血，而且没有任何记录。
    acct_after = _run(loop, ex.equity())
    assert acct_after["position_qty"] > 0, (
        "平仓后账户里应该还剩一点粉尘（step 取整残留），实际没有")
    assert acct_after["position_qty"] < step, (
        "残留在 %.8f，比一个 step 还大——那不该叫粉尘" % acct_after["position_qty"])
    # 权益应包含粉尘市值：纯 USDT + 粉尘市值
    dust_value = acct_after["position_qty"] * t["exit_price"]
    assert acct_after["equity"] == pytest.approx(
        acct_after["balance"] + dust_value, rel=1e-6, abs=1e-6), (
        "权益 %.6f 不等于 USDT %.6f + 粉尘 %.6f——粉尘被系统吃掉了"
        % (acct_after["equity"], acct_after["balance"], dust_value))
