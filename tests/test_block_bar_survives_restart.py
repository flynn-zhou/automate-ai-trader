# -*- coding: utf-8 -*-
"""熔断止损后的「本根 4h 不许再进」必须扛过进程重启（第 126 轮）。

要守的不变量
------------
1. 熔断止损之后，同一根 4h 内即使进程重启，也不许靠启动对账补回。
2. 下一根 4h 到了，必须能正常进场——不能变成永久停开。
3. 正常启动补仓不受影响。

缺陷的形状
----------
`_trend_block_bar` 是 **engine 实例的内存字段**（engine.py:58），初值 0。
熔断止损时在 engine.py:724 设成 `trend.last_eval_bar`。

而进程一重启：

    engine._trend_block_bar = 0                     # 内存字段，归零
    trend.last_eval_bar     = 最后一根已收盘 4h   # seed_from_rest 会补（trend.py:142）
    trend.need_reconcile    = True                  # __init__ 里设的

于是闸门判断：

    if last_eval_bar and last_eval_bar <= _trend_block_bar:
    999 <= 0  ->  False  ->  闸门放行

而 `startup_entry` 看到「空仓 + 站在 SMA*(1+h) 上方」就立刻补开。

这正是注释里点名的死循环：

    止损 -> 重启 -> 启动对账立刻补回 -> 再止损 -> ...

每次 25%，两轮就把 100U 打成 56U。

为什么这一轮才做
--------------
`tests/test_startup_reconcile.py:151` 里写的是：

    eng._trend_block_bar = 999           # 本根 4h 已熔断出局

**测试自己把状态摆好了**，于是从来没走过「重启后它还在不在」这条路。
这和第 118 轮那个缺陷是同一族：把答案写进自己的 fixture。
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.base import FillResult
from ai_trader.trade_db import TradeDB
from ai_trader.trading import trend as trend_mod
from ai_trader.trading.engine import DecisionEngine

PX = 78000.0
QTY = 0.00127
IV4H = 14400000


class _Bar:
    def __init__(self, ot, o, h, l, c, v=1.0, iv=IV4H):
        self.interval_ms = iv
        self.open_time = ot
        self.o = self.open = o
        self.h = self.high = h
        self.l = self.low = l
        self.c = self.close = c
        self.v = self.volume = v


class _Feed:
    src = "WS"

    def __init__(self, px=PX):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self._cur = _Bar(0, px, px * 1.01, px * 0.99, px, iv=3000)
        self.agg = type("A", (), {"current": staticmethod(lambda iv: self._cur)})()


class _Spot:
    name = "spot"

    def __init__(self, qty=0.0):
        self.orders = []
        self.pos_qty = qty
        self.entry = PX

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty, reason))
        self.pos_qty = 0.0 if side == "sell" else qty
        return FillResult(ok=True, price=PX, qty=qty, fee=qty * PX * 0.001,
                          order_id="1", status="filled", reason="")

    async def equity(self):
        usdt = max(0.0, 100.0 - self.pos_qty * PX)
        return {"balance": usdt, "equity": usdt + self.pos_qty * PX,
                "position_qty": self.pos_qty}

    async def position(self):
        return {"qty": self.pos_qty, "entry": self.entry, "side": "long"}


class _DB:
    """最小 TradeDB：记录写入，不落盘。

    但 block_bar 的存取【必须真的走一遍】——否则这个测试只是在测
    "我把状态摆好了"，而本轮缺陷恰恰是"状态没能活过一次重启"。
    用一个可跨实例共享的 dict 代表那条持久化通道。
    """

    def __init__(self, store=None):
        self.trades = []
        self.risks = []
        self.decisions = []
        self._store = store if store is not None else {}

    def save_block_bar(self, bar_t):
        self._store["block_bar"] = int(bar_t or 0)

    def load_block_bar(self):
        return int(self._store.get("block_bar", 0) or 0)

    def open_trade(self):
        return None

    def log_risk(self, kind, detail):
        self.risks.append((kind, detail))

    def log_decision(self, d):
        self.decisions.append(d)

    def log_fill(self, d):
        pass

    def log_order(self, d):
        pass

    def log_equity(self, d):
        pass

    def log_trade(self, d):
        self.trades.append(d)

    def close_trade(self, d):
        self.trades.append(d)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mk_engine(px=PX, store=None):
    """造一个「站在 SMA 上方、有持仓、止损已跌破」的引擎。"""
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.trend_stop_pct = 25.0
    ex = _Spot(qty=QTY)
    db = _DB(store)
    eng = DecisionEngine(cfg, _Feed(px), ex, db, guard=None)
    t = trend_mod.TrendStrategy(cfg.trading, symbol=cfg.trading.symbol,
                                exec_market="spot")
    # 最后一根收在 PX，之前长期在 0.8*PX，所以 SMA 远低于现价 -> 稳稳在上方
    closes = [PX * 0.8] * 200 + [PX] * 3
    t.bars = [{"t": i * IV4H, "o": c, "h": c, "l": c, "c": c, "v": 1.0}
              for i, c in enumerate(closes)]
    t.last_eval_bar = (len(closes) - 1) * IV4H
    t.need_reconcile = True
    eng.trend = t
    return eng, ex, db, t


def _bars(eng, price):
    return {3000: [_Bar(0, price, price * 1.01, price * 0.99, price, iv=3000)],
            IV4H: eng.trend.bars}


def test_a_stop_sets_the_block_bar():
    """前提：熔断止损之后，引擎自己把 block_bar 设上（不是我手工摆）。"""
    eng, ex, db, t = _mk_engine()
    eng.pos = {"side": "long", "qty": QTY, "entry": PX, "stop": PX * 0.9,
               "target": 0.0, "entry_ts": 0, "bars": 0,
               "best": PX, "worst": PX, "trade_id": None, "fee": 0.0}
    px = PX * 0.85          # 跌破 0.9*PX 熔断线
    _run(eng._step_trend(_bars(eng, px), px, 100.0, 1700000000000))
    assert ex.orders, u"前提不成立：熔断止损没有下出平仓单"
    assert eng._trend_block_bar > 0, (
        u"熔断止损之后 block_bar 仍然是 %r，闸门根本没设上"
        % (eng._trend_block_bar,))
    assert eng.pos is None, u"前提不成立：仓没平掉"


def test_the_next_bar_lifts_the_block():
    """★ 反向：下一根 4h 到了必须能正常进场——不能变成永久停开。

    只测"重启后拦住"，一个"永远拦住"的实现也能过，
    而那正是第 121 轮那个「永久停摆」的形状。
    """
    eng, ex, db, t = _mk_engine()
    eng.pos = {"side": "long", "qty": QTY, "entry": PX, "stop": PX * 0.9,
               "target": 0.0, "entry_ts": 0, "bars": 0,
               "best": PX, "worst": PX, "trade_id": None, "fee": 0.0}
    px = PX * 0.85
    _run(eng._step_trend(_bars(eng, px), px, 100.0, 1700000000000))
    block_bar = eng._trend_block_bar
    assert block_bar > 0

    shared = dict(db._store)
    eng2, ex2, db2, t2 = _mk_engine(store=shared)
    t2.last_eval_bar = block_bar
    eng2.attach()
    # 下一根 4h 收盘：last_eval_bar 推进到 block_bar 之后
    t2.last_eval_bar = block_bar + IV4H
    t2.bars.append({"t": block_bar + IV4H, "o": PX, "h": PX, "l": PX,
                    "c": PX, "v": 1.0})
    px2 = PX * 0.99
    _run(eng2._step_trend(_bars(eng2, px2), px2, 100.0, 1700000000000 + IV4H))

    buys = [o for o in ex2.orders if o[0] == "buy"]
    assert buys, (
        u"下一根 4h 已经收盘，重启后仍然开不了仓 —— 这条闸变成了永久停开。\n"
        u"block_bar=%r last_eval_bar=%r" % (block_bar, t2.last_eval_bar))


def test_the_block_bar_survives_a_restart():
    """★ 核心：熔断止损 -> 进程重启 -> 同一根 4h 内不许补回。

    重启后 `_trend_block_bar` 是 0、`last_eval_bar` 是最后一根已收盘 K、
    `need_reconcile` 是 True —— 三个条件凑在一起，
    `last_eval_bar <= block_bar` 变成 `999 <= 0` = False，闸门直接放行。
    """
    eng, ex, db, t = _mk_engine()
    eng.pos = {"side": "long", "qty": QTY, "entry": PX, "stop": PX * 0.9,
               "target": 0.0, "entry_ts": 0, "bars": 0,
               "best": PX, "worst": PX, "trade_id": None, "fee": 0.0}
    px = PX * 0.85
    _run(eng._step_trend(_bars(eng, px), px, 100.0, 1700000000000))
    assert eng.pos is None
    block_bar = eng._trend_block_bar
    assert block_bar > 0

    # ---- 进程重启：新引擎、新执行器、策略状态从库/接口恢复 ----
    # store 跨实例共享 = 模拟"这条状态写进了数据库"，
    # 而 _trend_block_bar 是 __init__ 里的 0 —— 正是线上真实情形。
    shared = dict(db._store)
    eng2, ex2, db2, t2 = _mk_engine(store=shared)
    # seed_from_rest 会把 last_eval_bar 补到最后一根已收盘 K（trend.py:142）
    t2.last_eval_bar = block_bar
    t2.need_reconcile = True
    # 关键：新引擎的内存字段是 __init__ 里的 0，
    # 必须靠 attach() 从库里读回来。
    assert eng2._trend_block_bar == 0, u"前提不成立：新引擎的 block_bar 不是 0"
    eng2.attach()
    assert eng2._trend_block_bar == block_bar, (
        u"attach() 没有把熔断出局标记读回来（%r vs %r）"
        % (eng2._trend_block_bar, block_bar))

    px2 = PX * 0.99         # 价格回到 SMA 上方（这正是补仓会发生的条件）
    _run(eng2._step_trend(_bars(eng2, px2), px2, 100.0, 1700000000000 + 60000))

    assert not ex2.orders, (
        u"熔断止损之后进程重启，同一根 4h 内又下了单：%r。\n"
        u"这就是「止损 -> 重启 -> 立刻补回 -> 再止损」的死循环，每次 25%%。"
        % (ex2.orders,))
