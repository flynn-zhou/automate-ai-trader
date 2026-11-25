# -*- coding: utf-8 -*-
"""MGN-2：可用保证金闸 + 外来占位单清理。

★ 这条链动的是**真钱**：它会撤销交易所上的真实挂单。
  所以每个测试都在钉一条「什么时候**不能**动手」的不变量，
  而不只是「什么时候应该动手」：

    1. 够用 → 一张单都不许碰（撤单是真实动作，多撤一张就是多改一次账户）
    2. reduceOnly 外来单 → 不许碰（撤它不解决保证金，只会制造裸仓）
    3. 撤到够用 → 立刻停（不做"一次清空"）
    4. 撤完仍不够 → 必须拒绝，不许硬闯
    5. 自己的单（AI- 前缀）→ 不许碰（那是状态机在管的）
    6. 读不到余额 → 按"不够"保守处理，但理由必须写"查不到"而非"没钱"
"""
import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from ai_trader.exchange.base import PendingOrder
from ai_trader.trading import engine as E
from ai_trader.trading import events as EV


# ------------------------------------------------------------------ 替身

class _Order(object):
    """交易所上的一张挂单（足够的字段子集）。"""

    def __init__(self, client_id, qty=0.02, price=60000.0, reduce_only=False,
                 working=True, order_id=None):
        self.client_id = client_id
        self.order_id = order_id or ("oid-" + client_id)
        self.qty = qty
        self.price = price
        self.stop_price = 0.0
        self.reduce_only = reduce_only
        self.working = working


class _Ex(object):
    """执行器替身：可用余额可编排、记录每一次真实动作。"""

    def __init__(self, avail_series, orders=(), query_working=True):
        #: 依次返回的可用余额；用完后一直返回最后一个
        self._series = list(avail_series)
        self._orders = list(orders)
        self._query_working = query_working
        self.cancelled = []
        self.queried = []
        self.avail_calls = 0

    async def available_margin(self):
        self.avail_calls += 1
        if self._series:
            self._last = self._series.pop(0)
        # ★ 序列用完后返回**最后一个值**，不是 0 ——
        #   否则"余额够用"会变成"余额变 0"，测试会以"仍不足"失败，
        #   而真正要验的是"够用之后不再继续撤"。
        return getattr(self, "_last", 0.0)

    async def open_orders(self, symbol=None):
        return list(self._orders)

    async def query_order(self, order_id="", client_id="", symbol=None):
        self.queried.append(client_id or order_id)
        return SimpleNamespace(working=self._query_working)

    async def cancel_order(self, order_id="", client_id="", symbol=None):
        self.cancelled.append(client_id or order_id)
        # 撤掉之后它就不在挂单簿上了
        self._orders = [o for o in self._orders
                        if (o.client_id or o.order_id) != (client_id or order_id)]
        return True


class _DB(object):
    def __init__(self):
        self.risks = []

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, str(detail)))

    def kinds(self):
        return [k for k, _ in self.risks]


def _env(ex, gate="买卖"):
    st = SimpleNamespace()
    st.ex = ex
    st.db = _DB()
    st._sizing_params = lambda: {"sizing.leverage": 22.0}
    # 只绑这一条链上真正用到的方法（产品代码里没有 getattr 兜底）
    for name in ("_gate_pending_margin", "_pending_need_margin",
                 "_reclaim_margin", "_place_pending_order"):
        setattr(st, name, getattr(E.DecisionEngine, name).__get__(st))
    return st


def _intent(entry=60000.0, tid="形态|up|1|60000.0"):
    # ★ side 必须是 `long`/`short`（EntryIntent 自己校验），不是 LONG/BUY。
    return EV.EntryIntent(strategy="pending", side="long",
                          entry=entry, stop=59000.0, order_type="LIMIT",
                          client_id="AI-pend-1", thesis_id=tid)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _gate(st, avail_orders=(), ctx=None):
    ctx = ctx if ctx is not None else {"qty": 0.01}
    return _run(st._gate_pending_margin(_intent(), ctx)), ctx


# ------------------------------------------------------------------ 1. 够就不碰

def test_enough_margin_touches_nothing():
    """可用 ≥ 需要 → 放行，且**一张单都不撤、一次都不查**。"""
    ex = _Ex([500.0], orders=[_Order("foreign-1", qty=0.06, price=57839.0)])
    st = _env(ex)
    res, ctx = _gate(st)

    assert res.ok is True, "可用充足却被拦：%s" % (res.detail,)
    assert ex.cancelled == [], (
        "可用保证金够用时撤了外来单 %r —— 撤单是真实动作，不该顺手做"
        % (ex.cancelled,))
    assert ex.avail_calls == 1, (
        "够用时不该反复查余额（查了 %d 次）" % ex.avail_calls)
    assert ctx["need_margin"] == pytest.approx(0.01 * 60000.0 / 22.0)


def test_need_margin_uses_the_same_leverage_as_sizing():
    """需要保证金 = 名义 ÷ `sizing.leverage`（与定仓同一来源）。"""
    st = _env(_Ex([0.0]))
    need = st._pending_need_margin(0.01, 60000.0)
    assert need == pytest.approx(60000.0 * 0.01 / 22.0), (
        "保证金口径与定仓不同源：%r" % (need,))
    # 缺参不许瞎算
    assert st._pending_need_margin(0, 60000.0) == 0.0
    assert st._pending_need_margin(0.01, 0) == 0.0


# ------------------------------------------------------------------ 2. 不够才撤

def test_short_margin_cancels_foreign_order_and_passes():
    """不够 → 撤外来占位单，撤完够用就放行。"""
    ex = _Ex([10.0, 200.0],
             orders=[_Order("foreign-1", qty=0.06, price=57839.0)])
    st = _env(ex)
    res, ctx = _gate(st)

    assert ex.cancelled == ["foreign-1"], "该撤的那张没撤：%r" % (ex.cancelled,)
    assert res.ok is True, "撤完够用了却仍被拦：%s" % (res.detail,)
    assert "pending_foreign_order_cancelled" in st.db.kinds(), (
        "撤了别人的单却没留痕：%r" % (st.db.kinds(),))
    assert "pending_margin_reclaimed" in st.db.kinds()


def test_reduce_only_foreign_order_is_never_cancelled():
    """reduceOnly 外来单不撤 —— 它开不出新仓，撤掉只会让持仓失去保护。"""
    ex = _Ex([10.0, 10.0],
             orders=[_Order("sl-guard", qty=0.05, price=59000.0,
                            reduce_only=True)])
    st = _env(ex)
    res, ctx = _gate(st)

    assert ex.cancelled == [], (
        "撤了 reduceOnly 保护单 %r —— 这会让真实持仓裸奔" % (ex.cancelled,))
    assert res.ok is False, "撤不到单却放行了"
    assert "reduceOnly" in res.detail, (
        "理由里必须写明跳过了 reduceOnly 单：%s" % (res.detail,))


def test_own_orders_are_never_cancelled():
    """`AI-` 前缀是系统自己的单，交状态机管；本闸不许碰。"""
    ex = _Ex([10.0, 10.0],
             orders=[_Order("AI-pend-abc", qty=0.06, price=57839.0),
                     _Order("AI-SL-123", qty=0.05, price=59000.0,
                            reduce_only=True)])
    st = _env(ex)
    res, ctx = _gate(st)

    assert ex.cancelled == [], "撤了自己的单 %r" % (ex.cancelled,)
    assert res.ok is False


def test_stops_cancelling_as_soon_as_margin_is_enough():
    """撤到够用立刻停，不许一次清空。"""
    ex = _Ex([10.0, 500.0],
             orders=[_Order("big", qty=0.10, price=60000.0),
                     _Order("small", qty=0.01, price=60000.0)])
    st = _env(ex)
    res, ctx = _gate(st)

    assert len(ex.cancelled) == 1, (
        "够用了还在继续撤（撤了 %r）" % (ex.cancelled,))
    assert res.ok is True


def test_cancels_largest_notion_first():
    """按名义从大到小撤：一张大单往往就够了，避免为凑小钱撤一堆。"""
    ex = _Ex([10.0, 500.0],
             orders=[_Order("small", qty=0.01, price=60000.0),
                     _Order("big", qty=0.10, price=60000.0)])
    st = _env(ex)
    _gate(st)
    assert ex.cancelled == ["big"], "撤单顺序不是按名义从大到小：%r" % (
        ex.cancelled,)


# ------------------------------------------------------------------ 3. 拒绝分支

def test_still_short_after_clearing_everything_is_a_rejection():
    """撤完仍不够 → 拒绝、记 risk、不许硬闯。"""
    ex = _Ex([10.0, 10.0, 0.0],
             orders=[_Order("f1", qty=0.06, price=57839.0)])
    st = _env(ex)
    res, ctx = _gate(st)

    assert res.ok is False
    assert res.gate == EV.GATE_MARGIN
    assert "pending_margin_short" in st.db.kinds()
    assert "不足" in res.detail


def test_unreadable_balance_says_unknown_not_poor():
    """读不到余额：保守拒绝，但理由必须写"查不到"而不是"没钱"。"""
    ex = _Ex([-1.0])
    st = _env(ex)
    res, ctx = _gate(st)

    assert res.ok is False, "读不到余额却放行了"
    assert "读不到" in res.detail, (
        "把'查不到'说成了'余额不足' —— 排查时会一路追错方向：%s"
        % (res.detail,))
    assert "pending_margin_unknown" in st.db.kinds()
    assert ex.cancelled == [], "余额未知时不许撤任何单"


def test_cannot_compute_need_does_not_pass_silently():
    """算不出需要多少保证金 → 拒绝（而不是默认放行）。"""
    ex = _Ex([500.0])
    st = _env(ex)
    res, ctx = _gate(st, ctx={"qty": 0.0})
    assert res.ok is False, "算不出需要多少却放行，等于没有这道闸"
    assert "算不出" in res.detail


# ------------------------------------------------------------------ 4. 能力与接线

def test_paper_executor_is_skipped():
    """纸面执行器没有 available_margin → 跳过，不算拦截。"""
    st = SimpleNamespace()
    st.ex = SimpleNamespace()          # 没有 available_margin
    st.db = _DB()
    st._pending_need_margin = lambda *a: 1.0
    st._gate_pending_margin = E.DecisionEngine._gate_pending_margin.__get__(st)
    res, ctx = _gate(st)
    assert res.ok is True, "纸面模式下不该被保证金闸拦"
    assert st.db.risks == []


def test_gate_is_wired_after_sizing_and_before_placing():
    """接线：闸门顺序必须是 无人值守 → 仓位 → 可用保证金 → 下单。

    ★ 排错顺序会静默失效：保证金闸在仓位闸之前就拿不到手数，
      算出来的是"用配置本金估的缺口"，和真实手数不是一个数。
    """
    st = _env(_Ex([500.0]))
    for name in ("_gate_pending_unattended", "_gate_pending_size"):
        setattr(st, name, getattr(E.DecisionEngine, name).__get__(st))
    r = E.DecisionEngine._pending_router(st)
    names = r.gate_names()
    assert names == ["_gate_pending_unattended", "_gate_pending_size",
                     "_gate_pending_margin"], (
        "闸门顺序变了（保证金闸必须排在仓位闸之后、下单之前）：%r" % (names,))


def test_query_confirms_working_before_cancelling():
    """撤之前先确认还挂着：已成交的单撤不掉，别把正常成交记成故障。"""
    ex = _Ex([10.0, 200.0], orders=[_Order("f1", qty=0.06, price=57839.0)],
             query_working=False)
    st = _env(ex)
    res, ctx = _gate(st)
    assert ex.queried == ["f1"], "撤之前没查状态"
    assert ex.cancelled == [], (
        "已经不在挂着的单还被撤了一次（会把正常成交记成撤单故障）")


# ------------------------------------------------------------------ 5. 决策文案手数

def _brief(**kw):
    """调真实的 `_pending_thesis_brief`（Thesis 用最小替身）。"""
    st = SimpleNamespace()
    th = SimpleNamespace(thesis_id="下跌三引线假突破限价单|up|1|58930.0",
                         order_price=58930.0, stop_price=58018.66,
                         target_price=62216.1, direction="up")
    return E.DecisionEngine._pending_thesis_brief(
        st, th, "EXPECTED", 59214.9, **kw)


def test_brief_prints_the_real_qty_when_known():
    """已知手数必须写进决策文案（MGN-3）。"""
    txt = _brief(blocked="可用保证金闸：可用 10U < 需要 27U", qty=0.012)
    assert "0.012000" in txt, "已知手数没写进决策文案：%s" % (txt,)
    assert "@ 58930.00" in txt


def test_brief_never_prints_a_fake_zero_qty():
    """手数未知时**不许**打 0.000000。

    ★ 原来两支写 `getattr(th, "qty", 0.0)`，而 `Thesis` 根本没有 qty 字段
      → 决策行长期是「应挂 up 0.000000 @ 58930.00」，读起来像"定仓算出了 0"。
      不写手数比写一个假的 0 诚实。
    """
    txt = _brief(blocked="仓位闸：名义 12U < 55U")
    assert "0.000000" not in txt, (
        "手数未知却打了 0.000000 —— 会被读成'定仓算出 0 张'：%s" % (txt,))
    assert "应挂up @ 58930.00" in txt, (
        "不写手数之后句子结构必须仍然通顺：%s" % (txt,))


def test_suppressed_brief_also_uses_the_real_qty():
    txt = _brief(suppressed=True, qty=0.02)
    assert "0.020000" in txt, "压制分支没用手数：%s" % (txt,)
    assert "0.000000" not in txt
