# -*- coding: utf-8 -*-
"""重启时持仓 + 熔断止损后，同一根 4h 内不许靠启动对账补回。

失败驱动。缺陷（只在实盘重启时才走到）：
  1. 进程带着持仓重启 -> trend.need_reconcile=True（__init__ 里设的）
  2. reconcile_live 接管仓位，【不经过 _open】——而 _open 才是正常清标志的地方
  3. 价格跌破 25% 熔断 -> _close 平仓，同时 _trend_block_bar = last_eval_bar
  4. 下一根 3s：空仓 + 价仍在 SMA 上方 -> startup_entry 触发 -> 立刻补回

engine.py 的注释早就点名过这个危险（"这笔仓一旦靠熔断止损平掉而价格仍在
均线上方，就会立刻无条件补回，绕过必须等下一根 4h 才能重入的规则"），
但那个防护写在 _open 里，接管路径不经过 _open，所以拦不住。

实测（变异）：两处修复同时撤掉时会补开 1 单；只撤任一处则被另一处掩盖。
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.base import FillResult
from ai_trader.trade_db import TradeDB
from ai_trader.trading import trend as trend_mod
from ai_trader.trading.engine import DecisionEngine

PX = 78000.0      # 必须是真实量级：0.00127*120=0.15U 会被零头闸拦掉
QTY = 0.00127


class _Bar:
    def __init__(self, ot, o, h, l, c, v=1.0, iv=14400000):
        self.interval_ms = iv
        self.open_time = ot
        self.o = self.open = o
        self.h = self.high = h
        self.l = self.low = l
        self.c = self.close = c
        self.v = self.volume = v


class _Feed:
    src = "WS"

    def __init__(self):
        self.last_price = PX
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self._cur = _Bar(0, PX, PX * 1.01, PX * 0.99, PX, iv=3000)
        self.agg = type("A", (), {"current": staticmethod(lambda iv: self._cur)})()


class _Spot:
    name = "spot"

    def __init__(self, qty=QTY):
        self.orders = []
        self.pos_qty = qty
        self.entry = PX

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty))
        self.pos_qty = 0.0 if side == "sell" else qty
        return FillResult(ok=True, price=PX, qty=qty, fee=qty * PX * 0.001,
                          order_id="1", status="filled", reason="")

    async def equity(self):
        usdt = max(0.0, 100.0 - self.pos_qty * PX)
        return {"balance": usdt, "equity": usdt + self.pos_qty * PX,
                "position_qty": self.pos_qty}

    async def position(self):
        return {"qty": self.pos_qty, "entry": self.entry, "side": "long"}


def _engine_with_uptrend(tdb, ex):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.trend_stop_pct = 25.0
    eng = DecisionEngine(cfg, _Feed(), ex, tdb, guard=None)
    t = trend_mod.TrendStrategy(cfg.trading, symbol=cfg.trading.symbol,
                                exec_market="spot")
    closes = [PX * 0.8] * 200 + [PX] * 3          # 长期站在 SMA 上方
    t.bars = [{"t": i * 14400000, "o": c, "h": c, "l": c, "c": c, "v": 1.0}
              for i, c in enumerate(closes)]
    t.last_eval_bar = 999
    eng.trend = t
    return eng


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _tmpdb():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    return tdb, path


def test_takeover_clears_reconcile_flag():
    """接管到持仓之后，对账标志必须清掉：持仓了就不存在"补上车"。

    不清的话它会一直挂着（接管不经过 _open），于是这笔仓一旦被熔断平掉、
    价格却还在均线上方，startup_entry 会立刻把仓补回去。
    """
    tdb, path = _tmpdb()
    try:
        ex = _Spot()
        eng = _engine_with_uptrend(tdb, ex)
        assert eng.trend.need_reconcile is True, "启动时应当挂着对账标志"

        eng.pos = None                       # 真实启动：内存空仓
        _run(eng.reconcile_live())           # 交易所却有仓 -> 接管

        assert eng.pos is not None, "应当接管交易所的持仓"
        assert eng.trend.need_reconcile is False, (
            "接管到持仓后对账标志仍然挂着——这笔仓一旦熔断平掉，"
            "startup_entry 会在同一根 4h 内无条件补回")
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_no_reentry_after_stop_in_same_bar():
    """熔断止损之后，同一根 4h 内任何路径都不许进场。

    这是 _trend_block_bar 存在的理由：否则会变成
    "止损 -> 立即再进 -> 再止损"的死循环。
    """
    tdb, path = _tmpdb()
    try:
        ex = _Spot()
        eng = _engine_with_uptrend(tdb, ex)
        eng.pos = None
        _run(eng.reconcile_live())

        ex.pos_qty = 0.0                     # 熔断止损把仓平掉
        eng.pos = None
        eng._trend_block_bar = 999           # 本根 4h 已熔断出局

        bars = {3000: [_Bar(0, PX, PX * 1.01, PX * 0.99, PX, iv=3000)],
                14400000: eng.trend.bars}
        n0 = len(ex.orders)
        _run(eng._step_trend(bars, PX, 100.0, 1700000000000))
        n1 = len(ex.orders)

        assert n1 == n0, (
            "熔断止损之后同一根 4h 内又下了 %d 单（价格仍在 SMA 上方）。"
            "这会让止损-再进-再止损循环起来" % (n1 - n0))
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_startup_catch_up_still_works():
    """反证：正常的启动补仓不能被这两处修复误伤。

    进程在上涨途中重启、空仓、站在均线上方 —— 这是 startup_entry
    真正要解决的场景，必须照常补进。
    """
    tdb, path = _tmpdb()
    try:
        ex = _Spot(qty=0.0)                  # 交易所也空仓
        eng = _engine_with_uptrend(tdb, ex)
        eng.pos = None
        _run(eng.reconcile_live())           # 两边都空 -> 不动
        assert eng.trend.need_reconcile is True, "空仓时对账机会必须保留"

        bars = {3000: [_Bar(0, PX, PX * 1.01, PX * 0.99, PX, iv=3000)],
                14400000: eng.trend.bars}
        n0 = len(ex.orders)
        _run(eng._step_trend(bars, PX, 100.0, 1700000000000))
        n1 = len(ex.orders)
        assert n1 > n0, (
            "空仓 + 站在 SMA 上方 + 启动对账未完成，却没有补开——"
            "这正是 startup_entry 要解决的「错过整段趋势」")
        assert eng.trend.need_reconcile is False, "开成之后对账标志应清零"
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass

# ====================================================================
# 以下三组是第 61 轮写下的启动对账语义测试（纯 TrendStrategy 层）。
# 第 63 轮我在写上面这组引擎级测试时，用整文件覆盖的方式把本文件冲掉了，
# 后来靠服务器上的第 62 轮部署快照才恢复回来——所以放在这里一并保留。
#
# 它们锁的是 startup_entry 的时机语义：
#   - 站在均线下方 -> 对账完成（本来就该空仓）
#   - 站在均线上方但没开成 -> 对账【未兑现】，下次还要再来
#   - 真的开成之后 -> 对账才算完成
# 与上面那组【入场闸门】互为补充，不是重复。
# ====================================================================

IV4H = 14400000


def _mk(prices):
    cfg = AppCfg()
    cfg.trading.sma_lookback = 5
    st = trend_mod.TrendStrategy(cfg.trading, symbol="BTCUSDT")
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(prices)]
    st.seeded = True
    st.need_reconcile = True
    return st


def test_below_sma_completes_reconcile():
    """价格在均线下方：对账完成，本来就不该持多。"""
    st = _mk([100.0] * 6 + [50.0])
    assert st.startup_entry(50.0) is None
    assert st.need_reconcile is False, "在均线下方就该把对账标记为完成"


def test_above_sma_keeps_reconcile_until_entry_happens():
    """价格在均线上方：对账未兑现，标志必须保留。"""
    st = _mk([100.0] * 6 + [200.0])
    rec = st.startup_entry(200.0)
    assert rec is not None, "在均线上方却没有补仓建议"
    assert st.need_reconcile is True, (
        "还没开成仓就把对账标记消费掉了——"
        "资金到位后不会补这笔，只能等下一次上穿。"
        "这正是「回测赚钱、实盘没上车」。")


def test_reconcile_stops_after_entry_confirmed():
    """真的开成仓之后，对账才算完成，不能无限重提。"""
    st = _mk([100.0] * 6 + [200.0])
    assert st.startup_entry(200.0) is not None
    st.mark_reconciled()
    assert st.need_reconcile is False
    assert st.startup_entry(200.0) is None, "开成之后还在反复提补仓"
