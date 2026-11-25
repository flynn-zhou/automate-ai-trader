# -*- coding: utf-8 -*-
"""BUS-B：决策落库由出站路由层的结论统一决定 —— **拒绝也必然产出一条决策**。

背景
----
用户 2026-11-24（第三次重申同一个要求）：

    「有手工仓时不挂新单，不代表系统不决策，决策行为要落库留痕，以便回溯，
      只是因为手工的存在，所以不真实下单而已……系统决策跟下不下单没一点关系，
      系统不停，决策不止」

上一轮（A1/A2）已经做到：每根 5m 无论有没有下单都写一条汇总决策。
但还留了一个洞 —— **被闸门拦下时，决策行看不出原因**：

    挂单失败 → `_pending_place` 只返回 False
             → 拒绝原因只进了 `risk_events`
             → 决策行写「等触发（挂单 61100 / 止损 60100）」

那句话在失败场景下是**假的**：单根本没挂出去，价格永远等不到它。
用户在决策流上看到「在等触发价」，实际整晚被安全闸挡着 ——
这正是本项目反复出现的那类缺陷：状态对，因果说错了。

本文件的测试钉住两件事：
  1. 被闸门拦下 → 决策行必须写出**是哪道闸**和**为什么**
  2. action 必须区分「被拦」与「在等」（看板按 action 着色/筛选）
"""
from __future__ import annotations

import types

import pytest

from ai_trader.trading import engine as E
from ai_trader.trading import thesis as T
from ai_trader.trading.events import (GATE_SIZING, GATE_UNATTENDED,
                                      ROUTE_EMPTY, ROUTE_REJECTED,
                                      EntryIntent, RouteResult)


# --------------------------------------------------------------- 共用桩
def _row(tid, price):
    from ai_trader.trading import pending_thesis_store as PTS
    th = T.Thesis(thesis_id=tid, kind="x", direction=T.UP,
                  order_price=price, stop_price=price * 0.98,
                  target_price=price * 1.04, order_type=T.ORDER_LIMIT,
                  family=T.FAMILY_REVERSAL, surprise_pts=0.0)
    return th, PTS.row_of(th, state=T.EXPECTED, next_index=0,
                          bar_ts=1795400000000, ambiguous=False, note="")


class _Db(object):
    def __init__(self):
        self.risks = []
        self.decisions = []

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, detail))

    def log_decision(self, d):
        self.decisions.append(d)

    def log_equity(self, *a, **kw):
        pass

    def log_trade(self, *a, **kw):
        pass

    def close_trade(self, *a, **kw):
        pass


# ------------------------------------- 1. 拒绝原因必须进决策（核心）
def test_blocked_brief_names_the_gate_and_the_reason():
    """被闸门拦下时，那句话必须点明**哪道闸**+**为什么**。

    这是 BUS-B 的核心：拒绝不能只活在 risk_events 里。
    """
    eng = types.SimpleNamespace()
    th, _ = _row("甲|up|1795400000000|61100.0", 61100.0)
    txt = E.DecisionEngine._pending_thesis_brief(
        eng, th, "EXPECTED", 61000.0,
        blocked="%s：日亏损已达上限" % GATE_UNATTENDED)
    assert "应挂" in txt, "仍必须写出本应挂出什么（否则回溯看不出系统想干嘛）"
    assert "61100" in txt, "必须带挂单价"
    assert GATE_UNATTENDED in txt, "必须点明是哪道闸，实际：%s" % txt
    assert "日亏损已达上限" in txt, "必须带闸门的原话理由，实际：%s" % txt
    assert "未下单" in txt


def test_blocked_and_waiting_are_different_sentences():
    """「被拦」与「在等」必须是两句不同的话。

    旧缺陷就出在这里：两者都写「等触发」，于是决策流上无法区分。
    """
    eng = types.SimpleNamespace()
    th, _ = _row("乙|up|1795400000000|61200.0", 61200.0)
    waiting = E.DecisionEngine._pending_thesis_brief(eng, th, "EXPECTED", 61000.0)
    blocked = E.DecisionEngine._pending_thesis_brief(
        eng, th, "EXPECTED", 61000.0, blocked=GATE_SIZING + "：名义 12U < 55U")
    assert waiting != blocked
    assert "等触发" in waiting
    assert "等触发" not in blocked, (
        "被拦下时绝不能再说「等触发」—— 那句话是假的，单根本没挂出去")
    assert GATE_SIZING in blocked


def test_suppressed_still_wins_over_blocked():
    """人工仓压制与闸门拦截是两件事，压制优先（它更具体）。"""
    eng = types.SimpleNamespace()
    th, _ = _row("丙|up|1795400000000|61300.0", 61300.0)
    txt = E.DecisionEngine._pending_thesis_brief(
        eng, th, "EXPECTED", 61000.0, suppressed=True,
        blocked=GATE_UNATTENDED + "：任意理由")
    assert "人工仓" in txt
    assert "只决策不下单" in txt


def test_empty_block_reason_keeps_the_old_text():
    """没被拦时行为必须和以前逐字一致（BUS-B 不许改顺手其它东西）。"""
    eng = types.SimpleNamespace()
    th, _ = _row("丁|up|1795400000000|61400.0", 61400.0)
    txt = E.DecisionEngine._pending_thesis_brief(eng, th, "EXPECTED", 61000.0)
    assert txt.startswith("丁 等触发（挂单 61400.00 / 止损 ")
    assert "被【" not in txt


# ------------------------------------------ 2. 路由结论 -> 决策 gating
def test_blocked_gate_splits_gate_name_from_detail():
    """`decisions.gates` 要 {gate, ok, detail}，路由给的是「名：理由」。"""
    f = E.DecisionEngine._blocked_gate
    assert f(GATE_UNATTENDED + "：数据源 DOWN") == GATE_UNATTENDED
    assert f(GATE_SIZING + "：名义不足") == GATE_SIZING
    # 没有分隔符时不能把整句当成闸门名
    assert f("模型看不懂的一句话") == "出站拦截"
    assert f("") == "出站拦截"


# ------------------------------- 3. _pending_place 必须留下路由结论
def _place_engine_stub(can_open=True, place_err=""):
    """跑真实 `_pending_place` 的最小桩，用来验 `_last_route` 被写入。"""
    placed = []

    class _Ex(object):
        async def place_order(self, side, qty, order_type="LIMIT",
                              price=None, client_id="", **kw):
            placed.append((side, qty))
            if place_err:
                return None, place_err
            return types.SimpleNamespace(status="NEW"), ""

    class _Guard(object):
        def can_open(self):
            return (can_open, "" if can_open else "日亏损已达上限")

    st = types.SimpleNamespace()
    st.ex = _Ex()
    st.db = _Db()
    st.guard = _Guard()
    st.tcfg = types.SimpleNamespace(symbol="BTCUSDT", strategy="pending")
    st._pending_size = lambda price, stop, otype: (0.01, "")
    st._last_route = None

    for name in ("_pending_place", "_pending_router",
                 "_gate_pending_unattended", "_gate_pending_size",
                 "_place_pending_order"):
        raw = E.DecisionEngine.__dict__[name]
        if isinstance(raw, staticmethod):
            setattr(st, name, raw.__func__)
        else:
            def _bound(*a, _n=name, **kw):
                return getattr(E.DecisionEngine, _n)(st, *a, **kw)
            _bound.__name__ = name
            setattr(st, name, _bound)
    return st, placed


def _act():
    return types.SimpleNamespace(
        kind="place_order", thesis_id="t1", reason="thesis_armed",
        price=61000.0, side="sell", order_type="LIMIT", bar_ts=1795400000000)


def _th_obj():
    return types.SimpleNamespace(order_price=61000.0, stop_price=61100.0,
                                 target_price=60800.0, trend="down")


@pytest.mark.asyncio
async def test_successful_place_records_a_placed_route():
    st, placed = _place_engine_stub()
    ok = await E.DecisionEngine._pending_place(st, _act(), _th_obj())
    assert ok is True
    assert st._last_route is not None
    assert st._last_route.placed is True
    assert st._last_route.rejection == ""


@pytest.mark.asyncio
async def test_gate_rejection_is_visible_on_the_route_result():
    """被闸门拦下时，`_last_route` 必须留下闸门名与理由，供决策行引用。"""
    st, placed = _place_engine_stub(can_open=False)
    ok = await E.DecisionEngine._pending_place(st, _act(), _th_obj())
    assert ok is False
    assert placed == []
    r = st._last_route
    assert r is not None
    assert r.status == ROUTE_REJECTED
    assert r.failed_gate == GATE_UNATTENDED
    assert "日亏损已达上限" in r.rejection


@pytest.mark.asyncio
async def test_exchange_failure_also_lands_on_the_route_result():
    st, placed = _place_engine_stub(place_err='code=-4015')
    ok = await E.DecisionEngine._pending_place(st, _act(), _th_obj())
    assert ok is False
    r = st._last_route
    assert r is not None
    assert r.status == ROUTE_EMPTY
    assert "code=-4015" in r.error


# ============================================ 4. 端到端：真的走 _step_pending
#
# ★ 这一节是补上一次真实漏网用来的。
#
#   上面第 1~3 节全部通过，但 `_step_pending` 里我误写了一个裸名
#   `_blocked_gate(...)`（它是类方法，裸名在方法体内根本不存在）→ NameError，
#   被 `tests/test_pending_wiring.py` 抓到。
#
#   教训：直接调 `_pending_thesis_brief` 的单元测试**证明不了**它被接进主链。
#   必须有一条测试从 `_step_pending` 进去，否则接线错了照样全绿。

def _e2e_engine(tmp_path, can_open=True, guard_why="日亏损已达上限"):
    """真实 DecisionEngine（复用 test_pending_wiring 的构造方式）。"""
    from tests.test_pending_wiring import _DB, _Feed, _RecEx, _engine
    eng, _store = _engine(tmp_path, dry=False)

    class _Guard(object):
        def can_open(self):
            return (can_open, "" if can_open else guard_why)

    eng.guard = _Guard()
    # 抓决策：替身 DB 默认丢弃，这里换成收集器
    eng.decisions_seen = []

    def _cap(d):
        eng.decisions_seen.append(d)

    eng.db.log_decision = _cap
    return eng


def _seed(eng, price=61000.0, stop_mult=0.98):
    """种一条预期。

    ★ `stop_mult` 默认 0.98（2%）会**被仓位闸拒绝** —— 生产口径下
      最大止损距离是 1.618%（爆仓距离 4.0455% / 安全倍数 2.5）。
      想让闸门放行的测试必须传更紧的止损（如 0.99）。
      这个默认值本身也是一条活证据：仓位闸确实在按 breached 阈值工作。
    """
    from ai_trader.trading import pending_thesis_store as PTS
    th = T.Thesis(thesis_id="e2e|up|1795400000000|61000.0", kind="x",
                  direction=T.UP, order_price=price,
                  stop_price=price * stop_mult, target_price=price * 1.04,
                  order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL,
                  surprise_pts=0.0)
    eng.pending.upsert_thesis(PTS.row_of(
        th, state=T.EXPECTED, next_index=0, bar_ts=1795400000000,
        ambiguous=False, note=""))


def _run_step(eng, price=61000.0):
    def _bars(ts0, n, step_ms, px=61000.0):
        out = []
        for i in range(n):
            t = ts0 + i * step_ms
            out.append(types.SimpleNamespace(
                open_time=t, ts=t, open=px, high=px + 10, low=px - 10,
                close=px, volume=1.0, interval_ms=step_ms))
        return out
    bars = {300000: _bars(1795400000000, 4, 300000),
            900000: _bars(1795400000000, 4, 900000)}
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        eng._step_pending(bars, price, 100.0, 1795400900000))


def test_step_pending_writes_the_blocking_gate_into_the_decision(tmp_path):
    """★★ BUS-B 的验收：闸门拦住 → 决策行里**必须**出现闸门名和理由。

    走的是完整主链（`_step_pending` → `_pending_place` → `OrderRouter`），
    不是直接调 brief —— 接线错了这条会红。
    """
    eng = _e2e_engine(tmp_path, can_open=False)
    _seed(eng)
    _run_step(eng)

    assert eng.ex.placed == [], "闸门说不行却下了单"
    blob = repr([d.get("reason") for d in eng.decisions_seen])
    assert eng.decisions_seen, "被闸门拦下也必须产出一条决策"
    assert GATE_UNATTENDED in blob, (
        "决策里没写出是哪道闸拦的，实际：%s" % blob)
    assert "日亏损已达上限" in blob, "决策里没写出拦截理由，实际：%s" % blob


def test_step_pending_marks_the_action_as_blocked(tmp_path):
    """被拦下时 action 必须是 `pending_blocked`，不能混进普通 pending。

    看板按 action 着色/筛选。如果拦截也写 pending，
    用户筛出来的"在等触发"里混着"整晚被挡着"的记录，等于没筛。
    """
    eng = _e2e_engine(tmp_path, can_open=False)
    _seed(eng)
    _run_step(eng)

    acts = [d.get("action") for d in eng.decisions_seen]
    assert "pending_blocked" in acts, "action 没区分被拦，实际：%r" % (acts,)


def test_step_pending_records_gates_column_when_blocked(tmp_path):
    """`decisions.gates` 要带上被拒的那道闸（与 scalp/trend 的决策形状对齐）。"""
    eng = _e2e_engine(tmp_path, can_open=False)
    _seed(eng)
    _run_step(eng)

    gates = [g for d in eng.decisions_seen for g in (d.get("gates") or [])]
    assert gates, "被拦时 gates 列为空，看板上少了一整列因果"
    assert any(g.get("gate") == GATE_UNATTENDED and g.get("ok") is False
               for g in gates), "gates 里没有记录被拒的闸，实际：%r" % (gates,)


def test_step_pending_does_not_say_blocked_when_the_guard_allows(tmp_path):
    """反向对照：两道闸都放行时，action 不许写成 pending_blocked。

    没有这条，上面那条可能因为"反正总是被拦"而恒绿。
    ★ 止损必须落在 1.618% 上限内，否则仓位闸会（正确地）拒单。
    """
    eng = _e2e_engine(tmp_path, can_open=True)
    _seed(eng, stop_mult=0.99)
    _run_step(eng)

    acts = [d.get("action") for d in eng.decisions_seen]
    assert "pending_blocked" not in acts, (
        "闸门放行了却写成被拦，实际：%r" % (acts,))


def test_sizing_gate_rejection_also_reaches_the_decision(tmp_path):
    """第二道闸（仓位闸）拒绝时，同样必须写进决策。

    这条用的是生产口径下的真实算路：止损 2% > 上限 1.618%，
    仓位闸会以「爆仓会先于止损触发」为由拒绝 —— 正是上轮修的那个缺陷。
    """
    eng = _e2e_engine(tmp_path, can_open=True)
    _seed(eng, stop_mult=0.98)      # 2% 止损，超过 1.618% 上限
    _run_step(eng)

    assert eng.ex.placed == [], "仓位闸说不可行却下了单"
    blob = repr([d.get("reason") for d in eng.decisions_seen])
    assert GATE_SIZING in blob, "决策里没写出是仓位闸拦的，实际：%s" % blob
    assert "1.618" in blob or "止损" in blob, (
        "决策里没写出拒绝理由（应为止损距离超限），实际：%s" % blob)

