# -*- coding: utf-8 -*-
"""Guard 第 6 条闸「持仓与账户不一致 -> 硬停」没有任何接线。

为什么单独写这个文件（第 115 轮）：
  同一套问法第 3 次用——前两轮分别是
    第 113 轮：第 3 条闸（引擎卡住）结构上永不成立
    第 114 轮：第 4 条闸（当日亏损）没有解除路径
  这轮问第 6 条：**「持仓与账户不一致 -> 硬停」，谁去触发的？**

  实测（静态 + 动态两条独立证据）：

  静态：_halt_hard() 在全 ai_trader/ 里的调用点
        只有 1 处：guard.py:175 的回撤硬停。
        engine.py 的 reconcile_live() 发现不一致时，只做了
        "以交易所为准修正本地认知"，然后 notify 一句就结束了。

  动态：真 Guard + 真引擎，制造「交易所有 0.001 BTC、数据库空仓」
        -> engine.pos 被正确接管
        -> guard.can_open() = (True, '')      ← 照常开仓
        -> guard.halted_hard = False
        -> guard.halt_reason = ''

  也就是说：文档写着「真金白银时对账不一致 = 必须先查」，
  而代码里的实际行为是「不一致？没关系，按交易所修正一下，继续交易」。

  生产佐证：日志里「实盘对账不一致」出现 **0 次**，
  risk_events 里也从来没有 guard_halt —— 这条闸一次都没触发过。

本文件钉四件事：
  1. 发现不一致时，必须真的硬停（can_open 变假）；
  2. 硬停理由要说清是"对账不一致"，不是回撤；
  3. 对账【通过】时绝不能硬停（正常运行时每 5 分钟一次，误停就是永久停机）；
  4. 零头（低于最小下单的余额）不算不一致，不能因此硬停。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

PX = 78000.0


class _Hub:
    def publish(self, *a, **k):
        pass


class _Feed:
    def __init__(self, px=PX):
        self.last_price = px
        self.cfg = AppCfg()
        self.hub = _Hub()
        self.src = "WS"
        outer = self

        class _Agg:
            def current(self, iv):
                class _B:
                    close = px
                return _B()
        self.agg = _Agg()


class _DB:
    def __init__(self):
        self.risks = []
        self.trades = []

    def log_risk(self, k, m):
        self.risks.append((k, m))

    def log_decision(self, *a, **k):
        pass

    def log_order(self, d):
        pass

    def log_fill(self, d):
        pass

    def log_trade(self, d):
        self.trades.append(d)
        return 1

    def log_equity(self, d):
        pass

    def close_trade(self, d):
        pass

    def open_trade(self, d):
        return None


class _Ex:
    """现货执行器替身：交易所有 qty 个 BTC。"""

    name = "spot"

    def __init__(self, qty=0.0, entry=PX):
        self.qty = qty
        self.entry = entry
        self.mark = PX

    async def equity(self):
        return {"balance": 100.0, "unrealized": 0.0,
                "equity": 100.0 + self.qty * self.mark,
                "position_qty": self.qty,
                "position_side": "long" if self.qty else ""}

    async def mark_price(self):
        return self.mark

    async def position(self):
        return {"qty": self.qty, "entry": self.entry,
                "side": "long" if self.qty else ""}

    async def market_order(self, side, qty, reason="", reduce_only=False):
        from ai_trader.exchange.base import FillResult
        return FillResult(ok=True, price=self.mark, qty=qty, fee=0.08, status="filled")


def _eng(ex, db_qty=None, guard=None):
    """db_qty=None 表示数据库空仓；否则给一个已在手的仓位。"""
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    feed = _Feed()
    db = _DB()
    g = guard if guard is not None else Guard(cfg)
    e = DecisionEngine(cfg, feed, ex, db, guard=g)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    e.trend.seeded = True
    e.trend.need_reconcile = False
    if db_qty is not None:
        e.pos = {"side": "long", "qty": db_qty, "entry": PX,
                 "stop": PX * 0.75, "target": 0.0, "entry_ts": 1, "bars": 0,
                 "best": PX, "worst": PX, "trade_id": 1,
                 "max_hold_bars": 10 ** 9, "open_reason": "", "fee": 0.1,
                 "signal": "trend", "dust": 0.0}
    return e, db, g


@pytest.mark.asyncio
async def test_adopting_an_orphan_position_hard_halts():
    """★ 交易所有仓、数据库空仓 —— 这是文档点名要硬停的情形。

    修之前：接管完就当没事发生，can_open 仍然是 True。
    """
    ex = _Ex(qty=0.001)
    e, db, g = _eng(ex, db_qty=None)
    await e.reconcile_live()
    assert e.pos is not None, "构造前提：孤儿仓位应被接管"
    ok, why = g.can_open()
    assert not ok, (u"交易所有 0.001 BTC 而数据库空仓，对账不一致，"
                    u"文档要求硬停，实际 can_open=%r" % (ok,))
    assert g.st.halted_hard is True, u"这不是「临时限制」，必须是人工资复位的硬停"
    assert u"对账" in (why or ""), u"硬停理由要能看出是对账不一致，实际 %r" % (why,)


@pytest.mark.asyncio
async def test_disappeared_position_hard_halts():
    """反面：数据库有仓、交易所空仓（仓位凭空消失）也必须硬停。

    这一条比"凭空多出来"更危险：引擎会以为止损还在保护一笔不存在的仓。
    """
    ex = _Ex(qty=0.0)
    e, db, g = _eng(ex, db_qty=0.001)
    await e.reconcile_live()
    assert e.pos is None, "构造前提：数据库那笔仓应被标记为已平"
    ok, why = g.can_open()
    assert not ok, u"仓位凭空消失却没有硬停：can_open=%r" % (ok,)
    assert g.st.halted_hard is True


@pytest.mark.asyncio
async def test_a_matching_position_never_halts():
    """反向约束：对账【通过】时绝不能硬停。

    实盘每 5 分钟对账一次。误停 = 从此再也不开仓，而且它会一直重复。
    这条和上一条必须成对存在——只测"不一致要停"，一个"永远停"的实现也能过。
    """
    ex = _Ex(qty=0.001)
    e, db, g = _eng(ex, db_qty=0.001)
    await e.reconcile_live()
    ok, why = g.can_open()
    assert ok, u"持仓一致却硬停了：%r" % (why,)
    assert g.st.halted_hard is False


@pytest.mark.asyncio
async def test_dust_below_min_notional_is_not_a_mismatch():
    """零头不算不一致，不能因此停摆。

    现货买入手续费扣在 BTC 上，到账净额不是 step 整数倍，平仓必留零头。
    把 1U 的零头当"不一致"，实盘每 5 分钟就会硬停一次，等于永远不能交易。
    """
    # 0.000005 BTC * 78000 = 0.39U，远低于现货最小名义 5U
    ex = _Ex(qty=0.000005)
    e, db, g = _eng(ex, db_qty=None)
    await e.reconcile_live()
    ok, why = g.can_open()
    assert ok, u"零头被当成不一致，实盘会每 5 分钟硬停一次：%r" % (why,)
    assert e.pos is None, "零头不该被当作持仓"


@pytest.mark.asyncio
async def test_the_halt_says_it_was_a_reconciliation_mismatch():
    """硬停理由必须写清楚是哪一路，不能和回撤硬停混成一个。

    两者的处置完全不同：回撤硬停意味着"本金基本没了"，
    对账硬停意味着"账实不符，去查发生了什么"。
    看板上显示成同一句话，人就查错方向了。
    """
    ex = _Ex(qty=0.001)
    e, db, g = _eng(ex, db_qty=None)
    await e.reconcile_live()
    snap = g.snapshot()
    assert snap["halted_hard"] is True
    assert u"回撤" not in snap["halt_reason"], (
        u"对账不一致被写成了回撤硬停：%r" % (snap["halt_reason"],))
    assert u"对账" in snap["halt_reason"]


def test_guard_has_a_public_way_to_halt_on_mismatch():
    """结构约束：Guard 必须有一个专门给"账实不符"用的硬停入口。

    直接盯源码，因为行为测试可以被另一种写法骗过——
    比如在 engine 里自己造一个 halt_reason 字符串写进去。
    要的是"这条闸真的有接线"这个结构事实。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ai_trader", "guard.py")
    src = open(p, encoding="utf-8").read()
    assert "def position_mismatch" in src, (
        u"guard.py 里没有 position_mismatch()——第 6 条闸（持仓与账户不一致 -> 硬停）"
        u"没有任何入口，只能是文档上的一句话")
    # 并且 engine 必须真的调用它
    p2 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "ai_trader", "trading", "engine.py")
    src2 = open(p2, encoding="utf-8").read()
    assert "position_mismatch" in src2, (
        u"engine.py 从不调用 position_mismatch()，这条闸等于没接线")
