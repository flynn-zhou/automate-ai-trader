# -*- coding: utf-8 -*-
"""对账接管的仓（用户手工开的单）不许被策略出场平掉。

真实事故（11-12，账户 292U）：
  21:09:48  用户在币安 App 手工买入 0.075 BTC @ 72475
  21:12:21  reconcile_live() 发现"交易所有仓、库里没有" -> 接管
  22:00:03  引擎按唐奇安通道出场平掉，真实净额 -3.4826U
  22:54:29  用户又手工买入 0.05 BTC @ 71373
  22:57:51  被接管，【同一秒】被平掉（这时 21:00 那根 1h 早已收在新低下方，
            want_exit() 在接管后的第一个 tick 就成立）

用户的原话是：「我们还没有策略呢，是什么在干预我的单子」。
用户下的处置：人工仓不判策略出场，但保留熔断止损。

要钉住的不变量（四条，缺一条这个 bug 就能换个姿势复活）：
  1. 接管仓：通道/SMA 出场条件成立时【不许平】
  2. 接管仓：跌到熔断止损时【必须平】——用户明确要留这条兜底
  3. 策略仓：同样的出场条件【必须平】——否则"全都不平"也能过第 1 条
  4. 判定必须靠 open_reason，能活过进程重启
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.market.agg import Bar
from ai_trader.trade_db import ADOPT_REASON, TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000
IV3S = 3000
ENTRY = 100.0


class Fill:
    def __init__(self, qty, px, fee=0.0):
        self.ok = True
        self.price = px
        self.qty = qty
        self.fee = fee
        self.order_id = "1"
        self.status = "filled"
        self.reason = ""


class Ex:
    name = "spot"

    def __init__(self):
        self.orders = []
        self.held = 0.0

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty))
        if side == "buy":
            self.held += qty
        else:
            self.held = max(0.0, self.held - qty)
        return Fill(qty, pluck_px())

    async def equity(self):
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
                "position_qty": self.held}

    async def position(self):
        return {"qty": self.held, "side": "long" if self.held else "",
                "entry": ENTRY if self.held else 0.0}

    async def verify_price(self, px):
        return px


def pluck_px():
    """平仓成交价：让 Ex 自己不用关心行情，统一用当前测试价。"""
    return _PX[0]


_PX = [ENTRY]


class _Hub:
    def publish(self, m):
        pass


class _Agg:
    def __init__(self, px):
        self.set(px)

    def set(self, px, hi=None, lo=None):
        self._b = Bar(interval_ms=IV3S, open_time=0, open=px,
                      high=hi if hi is not None else px,
                      low=lo if lo is not None else px,
                      close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._b if iv == IV3S else None


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self.agg = _Agg(px)
        self.src = "ws"


def _snap():
    return {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}


def _env(px=ENTRY, open_reason=ADOPT_REASON, stop=50.0):
    """造一个"已持仓"的引擎。trend 走 4h SMA 出场。"""
    tmp = tempfile.mkdtemp()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    ex = Ex()
    _PX[0] = px
    eng = DecisionEngine(cfg, Feed(px), ex, tdb, guard=None)
    eng.ex = ex
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    # 7 根收在 90~105，SMA5 ~ 100.6；下面再补一根收在 90 -> 已在均线下方
    eng.trend.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
                      for i, p in enumerate([90.0, 95, 98, 99, 100, 101, 105.0])]
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    ex.held = 0.001
    eng.pos = {"side": "long", "qty": 0.001, "entry": ENTRY, "stop": stop,
               "target": 0.0, "entry_ts": 1, "bars": 0, "best": ENTRY,
               "worst": ENTRY, "trade_id": None, "max_hold_bars": 600,
               "open_reason": open_reason, "fee": 0.0, "signal": "trend_entry"}
    eng.db.log_trade({
        "symbol": "BTCUSDT", "side": "long", "qty": 0.001, "entry_ts": 1,
        "entry_price": ENTRY, "stop_price": stop, "target_price": 0.0,
        "open_reason": open_reason, "paper": 1, "strategy": "trend",
        "max_hold_bars": 600})
    return eng, ex, tdb


def _cross_below(eng, px=90.0):
    """让最后一根【已收盘】4h 收在 SMA 下方 —— want_exit() 的触发条件。"""
    eng.trend.bars = eng.trend.bars + [
        {"t": 7 * IV4H, "o": 92.0, "h": 92.0, "l": 89.0, "c": px, "v": 1.0}]
    _PX[0] = px
    eng.feed.last_price = px
    eng.feed.agg.set(px, hi=px, lo=89.0)


# ---------------------------------------------------------------- 1. 不判策略出场
@pytest.mark.asyncio
async def test_adopted_position_is_not_closed_by_strategy_exit():
    """接管仓：SMA 出场条件成立也必须【不动】。

    这就是 11-12 用户那两笔单子被平的直接原因。
    """
    eng, ex, tdb = _env(open_reason=ADOPT_REASON)
    try:
        _cross_below(eng)
        assert eng.trend.want_exit(_PX[0]) is not None, \
            "构造前提：策略此刻确实想出场，否则测不出豁免"
        await eng._step(_snap())
        assert eng.pos is not None, (
            u"人工仓被策略出场平掉了：%r。用户手工开的单不该由引擎判断出场" % (ex.orders,))
        assert ex.orders == [], u"不该发出任何平仓单，实际 %r" % (ex.orders,)
    finally:
        tdb.stop()


# ---------------------------------------------------------------- 2. 保留熔断止损
@pytest.mark.asyncio
async def test_adopted_position_still_hits_the_hard_stop():
    """接管仓：跌到熔断止损必须平 —— 这是用户明确要求保留的兜底。

    只测"豁免出场"的话，一个"接管仓完全不管"的实现也能过；
    资金真的归零时它一样不会动。
    """
    eng, ex, tdb = _env(open_reason=ADOPT_REASON, stop=95.0)
    try:
        # 价格 90，止损 95 -> 已经跌破
        eng.feed.last_price = 90.0
        _PX[0] = 90.0
        eng.feed.agg.set(90.0, hi=91.0, lo=89.0)
        await eng._step(_snap())
        assert eng.pos is None, u"跌破熔断止损却没平仓：%r" % (eng.pos,)
        # 大小写不敏感：策略仓走 `_close` 发小写 "sell"，
        # 人工仓走 `_adopted_close` 发大写 "SELL"（生产执行器内部会规范化）。
        # 这条测的是"跌破熔断必须平"，不是"用了哪种大小写"。
        assert ex.orders and str(ex.orders[0][0]).lower() == "sell", \
            u"应该发出一笔卖出平仓，实际 %r" % (ex.orders,)
    finally:
        tdb.stop()


# ---------------------------------------------------------------- 3. 反向约束
@pytest.mark.asyncio
async def test_strategy_position_is_still_closed_by_strategy_exit():
    """反向约束：策略自己开的仓，同样的出场条件【必须】照常平。

    上面两条只证明"接管仓不动"。没有这一条，把 want_exit 整个删掉也能全过。
    """
    eng, ex, tdb = _env(open_reason="趋势开仓")
    try:
        _cross_below(eng)
        await eng._step(_snap())
        assert eng.pos is None, "策略仓该出场却没平，趋势策略等于失效"
        assert ex.orders and ex.orders[0][0] == "sell", \
            u"应该发出一笔卖出平仓，实际 %r" % (ex.orders,)
    finally:
        tdb.stop()


# ---------------------------------------------------------------- 4. 活过重启
@pytest.mark.asyncio
async def test_adoption_marker_survives_a_restart():
    """判定必须靠 open_reason 而不是内存布尔。

    引擎重启后 self.pos 是从 trades 表恢复的，任何内存标记都会丢。
    丢了的表现是「重启后又开始悄悄平用户的仓」，而且没有任何报错。
    """
    eng, ex, tdb = _env(open_reason=ADOPT_REASON)
    try:
        eng.db.stop()
        tdb2 = TradeDB(eng.db._path)
        tdb2.start()
        rows = tdb2.closed_trades()
        open_row = tdb2.open_trade()
        assert open_row is not None, "构造前提：得有一条未平仓记录"
        assert open_row["open_reason"] == ADOPT_REASON, (
            u"重启后读回的 open_reason 是 %r，豁免规则会失效"
            % (open_row["open_reason"],))
        assert rows == []
        tdb2.stop()
    finally:
        try:
            tdb.stop()
        except Exception:
            pass


# ---------------------------------------------------------------- 5. 统计口径
def test_stats_exclude_adopted_trades():
    """接管仓不能被算成策略战绩。

    11-11 用户手工来回买卖，一天写出 12 条 pnl_net=0 的空记录。
    算进 donchian 的话：交易数 +12、胜率被拉低、而它们既不是策略开的
    也不是策略平的。用户看大屏会以为策略在密集交易且全亏。
    """
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    try:
        common = {"symbol": "BTCUSDT", "side": "long", "qty": 1.0,
                  "entry_price": 100.0, "stop_price": 0.0, "target_price": 0.0,
                  "paper": 0, "strategy": "donchian"}
        # 一条真实策略单：赚 10
        tdb.log_trade(dict(common, entry_ts=1000, exit_ts=2000,
                           exit_price=110.0, pnl_gross=10.0, fee=0.0,
                           pnl_net=10.0, open_reason="突破开仓",
                           close_reason="通道出场"))
        # 三条接管单：两条 0、一条亏 5
        for i, (pnl, orsn) in enumerate([(0.0, ADOPT_REASON),
                                         (0.0, ADOPT_REASON),
                                         (-5.0, ADOPT_REASON)]):
            tdb.log_trade(dict(common, entry_ts=3000 + i, exit_ts=4000 + i,
                               exit_price=100.0, pnl_gross=pnl, fee=0.0,
                               pnl_net=pnl, open_reason=orsn,
                               close_reason="实盘对账：接管交易所实际持仓"))
        # 等写入线程把队列排空
        import time as _t
        for _ in range(200):
            if tdb.stats("donchian")["trades"] > 0:
                break
            _t.sleep(0.02)
        st = tdb.stats("donchian")
        assert st["trades"] == 1, (
            u"策略战绩混进了接管单：交易数 %d（应为 1）。"
            u"这 4 条里只有 1 条是策略真的开的" % st["trades"])
        assert st["pnl_net"] == pytest.approx(10.0), \
            u"净利被接管单污染：%r" % (st["pnl_net"],)
        assert st["win_rate"] == pytest.approx(1.0), \
            u"胜率被 0 盈亏的接管单拉低：%r" % (st["win_rate"],)
    finally:
        tdb.stop()
