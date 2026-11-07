# -*- coding: utf-8 -*-
"""权益查询失败时，引擎必须【拒绝入场但照常出场】——这条守卫以前没有任何测试。

怎么发现的（第 101 轮）：
  第 100 轮覆盖率审计显示 `_open` 的 1089-1093 行未覆盖。那几行是：

      if getattr(self, "_equity_stale", False):
          self._log_decision(..., "skip", "权益数据过期（查询失败），暂停开仓", ...)
          return

  注释写着「不知道有多少钱就下注，轻则被交易所拒单，重则按错误规模建仓」，
  而且故意放在 _open 里（不是各调用点），这样将来加新的进场路径会自动被覆盖。

  ★ 更糟：tests/test_first_live_trade.py 的 docstring 第 19 行【声称】覆盖了这一条
    （「买入前 _equity_stale 拒绝入场」），实际整个文件里 _equity_stale 只出现在
    那句 docstring 里。注释在撒谎，而它让人以为这里有保护。

  实测确认：把守卫改成 if False，跑 211 个相关测试【全绿】。

设计是【有意不对称】的（engine.py 399-401）：
    权益过期 -> 照常出场（卖多少是已知的，不需要权益）
    权益过期 -> 拒绝入场（不知道有多少钱，就不能下注）

★ 第一版测试写错了，值得记下来：
  我直接设 eng._equity_stale = True 再调 _step，结果订单一照样下了。
  原因是 _step 一开头就真的去查一次权益（engine.py:403-407），查询成功就把
  _equity_stale 归 False —— 我设的值在到达 _open 之前就被覆盖了。
  也就是说这个守卫【只在 equity() 真的失败时才可达】。
  所以测试必须让 equity() 抛异常，而不是摆个标志。
"""
import asyncio
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


def _snap():
    return {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}


@pytest.fixture()
def env():
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


def _prime_ready_to_enter(eng, price=PX):
    """喂成「刚上穿 SMA、下一步就该开仓」的状态（滞回带设 0，排除干扰）。"""
    eng.trend.hyst = 0.0
    eng.trend.bars = [{"t": i * IV4H, "o": 20000.0, "h": 20000.0,
                       "l": 20000.0, "c": 20000.0, "v": 1.0} for i in range(5)]
    eng.trend.bars.append({"t": 5 * IV4H, "o": price, "h": price,
                            "l": price, "c": price, "v": 1.0})


def _break_equity(eng, msg="模拟：权益接口超时"):
    """让 equity() 真的抛异常 —— 这是守卫唯一的可达方式。

    同时补上 _last_equity：真实事故里几乎都是「曾经查到过、后来查不到了」，
    而 engine.py:417 在没有历史值时会直接 return，走不到守卫。
    """
    async def _boom():
        raise RuntimeError(msg)
    eng.ex.equity = _boom
    eng._last_equity = 100.0


def test_stale_equity_blocks_the_order_at_the_exchange(env):
    """权益查询失败 -> 交易所【一笔订单都收不到】。"""
    eng, ex, fake, tdb, feed, loop = env
    _prime_ready_to_enter(eng)
    _break_equity(eng)

    before = len(fake.orders)
    loop.run_until_complete(eng._step(_snap()))
    after = len(fake.orders)

    assert after == before, (
        u"权益查询失败时仍然下单了 %d 笔。这个守卫守的是「不知道有多少钱就下注」。"
        u"订单：%r" % (after - before, fake.orders[before:]))
    assert eng.pos is None, u"权益过期却建立了持仓：%r" % (eng.pos,)


def test_stale_equity_records_a_skip_decision(env):
    """拒绝入场必须在决策流里说出来，否则看板只会看到「什么都不做」。"""
    eng, ex, fake, tdb, feed, loop = env
    _prime_ready_to_enter(eng)
    _break_equity(eng)
    loop.run_until_complete(eng._step(_snap()))

    d = getattr(eng, "last_decision", None)
    assert d, u"权益过期却没有任何决策记录，哨兵和看板都会认为系统正常"
    blob = str(d)
    assert "过期" in blob or "stale" in blob.lower(), (
        u"决策记录没说明为什么不开仓：%r" % (d,))


def test_the_same_setup_does_open_when_equity_is_fine(env):
    """反向对照：同状态、权益正常时必须真的下单。

    没有这条，上面两条可能因为「这个环境根本开不出仓」而永远绿 ——
    那是一种通过得不对的测试。
    """
    eng, ex, fake, tdb, feed, loop = env
    _prime_ready_to_enter(eng)

    before = len(fake.orders)
    loop.run_until_complete(eng._step(_snap()))
    after = len(fake.orders)

    assert after > before, (
        u"权益正常时也没下单：这个环境根本开不出仓，"
        u"那「过期时不下单」就证明不了任何事。决策：%r"
        % (getattr(eng, "last_decision", None),))
    assert eng.pos is not None, u"下单了但没有持仓记录"


def test_stale_is_reported_even_when_holding_a_position(env):
    """持仓时权益查询失败：__照常可出场__，但状态必须标出 stale。

    这是 engine.py 399-401 那条有意的不对称：卖多少是已知的，不需要权益。
    所以「过期」不能变成「连平仓也不做」——那会把持仓活活套住。
    """
    eng, ex, fake, tdb, feed, loop = env
    _prime_ready_to_enter(eng)
    loop.run_until_complete(eng._step(_snap()))
    assert eng.pos is not None, u"先决条件不成立：这一步应该已经建仓"

    _break_equity(eng)
    loop.run_until_complete(eng._step(_snap()))
    assert getattr(eng, "_equity_stale", False), (
        u"权益查询失败却没标 stale，上层无法知道入场已暂停")
    assert eng.pos is not None, (
        u"只是权益查询失败，持仓被平掉了 —— 不该发生：出场不需要权益")


def test_stale_flag_clears_once_equity_is_readable_again(env):
    """权益恢复后标志必须自动清除，否则一次抖动就是永久停摆。"""
    eng, ex, fake, tdb, feed, loop = env
    _prime_ready_to_enter(eng)
    _break_equity(eng)
    loop.run_until_complete(eng._step(_snap()))
    assert getattr(eng, "_equity_stale", False), u"权益失败却没标 stale"

    del eng.ex.equity
    loop.run_until_complete(eng._step(_snap()))
    assert not getattr(eng, "_equity_stale", False), (
        u"权益已经能查到了，_equity_stale 还是 True —— "
        u"一次权益查询失败会让引擎永久停止开仓，且不报错")
