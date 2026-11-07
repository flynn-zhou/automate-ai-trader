# -*- coding: utf-8 -*-
"""实盘对账接管「查不到开仓价」的持仓：三条兜底分支必须都不留 0 止损。

为什么单独写（第 104 轮）：
  第 100 轮覆盖率审计显示 reconcile_live 的 251-252、260-265、267-268 从未执行，
  正是「现货查不到开仓价」那一段。已有的一批测试（test_cost_basis / test_position_drift /
  test_spot_reconcile / test_startup_reconcile）都直接调 _cost_basis_from_trades 或
  ex.cost_basis()，【没有一条走 reconcile_live 自己的兜底分支】。

  而这段代码里记着两个真实事故：

  1) engine.py:239-246 —— 绝不能留 entry=0：
       "entry=0 会让止损价也变成 0，等于\"永远满足止损\"，一开机就把仓位平掉。"
     也不能随便拿当前市价顶替：
       40000 买入 -> 进程崩了 -> 在 28000 重启。
       拿 28000 当成本，止损就成了 21000——可这仓已经浮亏 30% 了，
       而这道止损还要再跌 25% 才动，等于保护失效。
       而且平仓时 pnl 按 28000 算，把真实亏损记小了，边际统计全废。

  2) engine.py:281-287 —— 这里必须 log_trade（写），不是 open_trade（读）：
       "原先误写 open_trade(...) 并传了 dict，签名不匹配直接抛 TypeError，
        而 reconcile_live() 的调用方吞掉了异常，结果是：
        孤儿仓位每一轮都抛错、从不被真正接管，系统始终认为自己空仓，
        然后在\"有持仓\"的状态下还可能再买一次。
        这个错误只在【交易所有币、数据库没仓】时才触发，
        正常流程不会走到——所以纸面跑了半个月也没暴露。"

  所以这轮要钉住的不变量是：**接管出来的仓位，止损价绝不能是 0；
  放弃接管时，绝不能留下一个假的 pos。**
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.base import FillResult
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

PX = 78000.0
QTY = 0.00127        # 0.00127 * 78000 = 99U，高于现货最小下单 5U，不会被零头闸拦
STOP_PCT = 25.0


class _Hub:
    def publish(self, m):
        pass


class _Bar:
    def __init__(self, px, iv=3000):
        self.interval_ms = iv
        self.open_time = 0
        self.o = self.open = px
        self.h = self.high = px
        self.l = self.low = px
        self.c = self.close = px
        self.v = self.volume = 1.0


class _Feed:
    src = "WS"

    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self._b = _Bar(px) if px else None
        self.agg = type("A", (), {"current": staticmethod(lambda iv: self._b)})()


class _Spot:
    """现货执行器桩：position() 的 entry 可控（0 = 查不到开仓价）。"""
    name = "spot"

    def __init__(self, qty, entry):
        self.pos_qty = qty
        self.entry = entry
        self.orders = []

    async def position(self):
        return {"qty": self.pos_qty, "entry": self.entry, "side": "long"}

    async def equity(self):
        return {"balance": 100.0, "equity": 100.0 + self.pos_qty * PX,
                "position_qty": self.pos_qty}

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty))
        return FillResult(ok=True, price=PX, qty=qty, fee=0.0,
                          order_id="1", status="filled", reason="")


def _mk(qty=QTY, entry=0.0, last_px=PX, cost_basis=0.0):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = STOP_PCT
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    ex = _Spot(qty, entry)
    feed = _Feed(last_px)
    eng = DecisionEngine(cfg, feed, ex, tdb, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")

    async def _cb(_q):
        return cost_basis
    eng._cost_basis_from_trades = _cb
    return eng, ex, tdb, feed


def test_cost_basis_is_used_when_available() :
    """cost_basis 能算出正数时，接管必须【用真实成本】，不能退回现价。"""
    import asyncio
    eng, ex, tdb, feed = _mk(entry=0.0, cost_basis=50000.0)
    try:
        asyncio.get_event_loop().run_until_complete(eng.reconcile_live())
        assert eng.pos is not None, "交易所持仓却没能接管，引擎会认为自己空仓"
        assert eng.pos["entry"] == pytest.approx(50000.0), (
            u"接管成本用了 %r，而真实成交记录给出的是 50000.0" % (eng.pos["entry"],))
    finally:
        tdb.stop()


def test_last_price_is_the_fallback_and_it_is_recorded():
    """算不出真实成本时退回现价，但必须记 adopt_estimated_entry —— 这笔盈亏不可信。

    ★ 这条最初写错了两处，都值得记：
      1) 我写了个 tdb.risk_events(limit=20)，而 TradeDB 根本没有这个方法 ——
         hasattr 返回 False，rows 恒为 []，于是测试红。红的理由是假的：
         代码其实记了风险事件，是我读错了地方。
      2) 就算读出对了，也还得先 tdb.stop() 排空写队列。log_risk 是异步入队，
         直接抢读会读到空表，然后误判成「没记风险」。
         这个坑 tests/test_order_failure_backoff.py:153 已经记过一次了。
    """
    import asyncio
    import sqlite3
    eng, ex, tdb, feed = _mk(entry=0.0, cost_basis=0.0)
    asyncio.get_event_loop().run_until_complete(eng.reconcile_live())
    assert eng.pos is not None
    assert eng.pos["entry"] == pytest.approx(PX), (
        u"没有真实成本时应退回现价 %r，实际 %r" % (PX, eng.pos["entry"]))
    tdb.stop()          # 必须排空写队列再读，否则读到空表
    db = sqlite3.connect(tdb._path, timeout=5)
    try:
        kinds = [r[0] for r in db.execute("SELECT kind FROM risk_events")]
    finally:
        db.close()
    assert "adopt_estimated_entry" in kinds, (
        u"按估算成本接管却没留 adopt_estimated_entry 风险事件。"
        u"这笔盈亏不可信，必须能被追溯出来。实际记录：%r" % (kinds,))


def test_no_entry_and_no_price_abandons_the_takeover():
    """连行情价都没有时：放弃接管，绝不能留下一个假的 pos。"""
    import asyncio
    eng, ex, tdb, feed = _mk(entry=0.0, cost_basis=0.0, last_px=0.0)
    try:
        asyncio.get_event_loop().run_until_complete(eng.reconcile_live())
        assert eng.pos is None, (
            u"既无开仓价也无行情价，却造出了一个仓位：%r\n"
            u"这个仓位没有可信成本，止损价会算成 0 或乱数。" % (eng.pos,))
    finally:
        tdb.stop()


def test_adopted_position_never_has_a_zero_stop():
    """不变量：不管成本从哪来，接管出来的止损价都不能是 0。

    0 的含义是「永远满足止损」——多头永不触发、空头开机即平，两种都是灾难。
    """
    import asyncio
    for cb in (0.0, 50000.0, 120000.0):
        eng, ex, tdb, feed = _mk(entry=0.0, cost_basis=cb)
        try:
            asyncio.get_event_loop().run_until_complete(eng.reconcile_live())
            assert eng.pos is not None
            stop = eng.pos["stop"]
            assert stop > 0, (
                u"cost_basis=%r 时接管出的止损价是 %r —— 保护静默消失" % (cb, stop))
            assert stop == pytest.approx(eng.pos["entry"] * (1 - STOP_PCT / 100.0)), (
                u"止损位没按熔断 %.0f%% 算：stop=%r entry=%r"
                % (STOP_PCT, stop, eng.pos["entry"]))
        finally:
            tdb.stop()


def test_short_takeover_stop_is_above_entry():
    """空头接管：止损必须落在开仓价【上方】。"""
    import asyncio
    eng, ex, tdb, feed = _mk(qty=-QTY, entry=0.0, cost_basis=60000.0)
    try:
        asyncio.get_event_loop().run_until_complete(eng.reconcile_live())
        assert eng.pos is not None, "交易所空头持仓没能接管"
        assert eng.pos["side"] == "short"
        assert eng.pos["stop"] == pytest.approx(60000.0 * (1 + STOP_PCT / 100.0)), (
            u"空头止损应在成本上方：%r" % (eng.pos["stop"],))
        assert eng.pos["stop"] > eng.pos["entry"], u"空头止损落在成本下方，方向反了"
    finally:
        tdb.stop()

def test_broken_risk_log_does_not_block_the_takeover():
    """风险记录写失败时，接管仍必须完成。

    这条守的不变量：交易所真的有币、数据库以为空仓——这种状态下引擎不会再开新仓
    是对的，但如果【拒绝接管】，它就永远认为自己空仓，止损打在一个不存在的仓上。
    所以「记不上风险事件」绝不能变成「不接管」。

    反过来说，这个 try/except 也不能吞掉信息：engine.py:256 那条 CRITICAL
    已经把「只能以当前价当成本」喊出来了，信息没丢，所以这里吞异常是可以接受的。
    """
    import asyncio
    eng, ex, tdb, feed = _mk(entry=0.0, cost_basis=0.0)

    def _boom(k, m):
        raise RuntimeError("模拟：风险表写入失败")
    tdb.log_risk = _boom
    try:
        asyncio.get_event_loop().run_until_complete(eng.reconcile_live())
        assert eng.pos is not None, (
            u"风险记录写失败就把接管也放弃了 —— 引擎会认为自己空仓，"
            u"而交易所真的有币，止损打在一个不存在的仓上。")
        assert eng.pos["stop"] > 0, u"接管出的止损价不能是 0"
    finally:
        tdb.stop()
