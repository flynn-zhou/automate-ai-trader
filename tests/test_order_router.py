# -*- coding: utf-8 -*-
"""出站路由层与事件契约（BUS-A）。

BUS-A 的验收标准只有一条：**行为零变化**。
所以这里的测试不测"新功能"，测的是三件容易在重构中被悄悄改掉的事：

  1. 闸门的**顺序**（顺序即优先级，顺序错就是另一套风控）
  2. 拦住之后**不再跑后面的闸**（否则后面的闸会有副作用却没人看）
  3. 下单失败必须是**可观测的返回值**，不能被当成成功

第 3 条是 2026-11-24 那 15 条幽灵动作的直接教训：
失败只写一行日志、上层照样标记已执行，于是一路推出假的成交和假平仓。
"""
from __future__ import annotations

import pytest

from ai_trader.trading.events import (GATE_HALTED, GATE_MANUAL_LOT, GATE_SIZING,
                                      ROUTE_EMPTY, ROUTE_PLACED, ROUTE_REJECTED,
                                      EntryIntent, ExitIntent, GateResult,
                                      PositionClosed, PositionOpened, RouteResult)
from ai_trader.trading.order_router import OrderRouter


# ------------------------------------------------------------------ 脚手架
def _intent(**over):
    kw = dict(strategy="pending", side="long", entry=60000.0, stop=59000.0,
              target=64000.0, qty=0.01, order_type="LIMIT", client_id="AI-pend-x",
              reason="三引线", thesis_id="t1", bar_ts=1795000000000)
    kw.update(over)
    return EntryIntent(**kw)


class _Recorder:
    """记录闸门被调用的顺序，并可在指定闸上返回拒绝。"""

    def __init__(self, reject_at=None, skip_at=()):
        self.calls = []
        self.reject_at = reject_at
        self.skip_at = skip_at

    def gate(self, name):
        async def _g(intent, ctx):
            self.calls.append(name)
            if name in self.skip_at:
                return None
            if name == self.reject_at:
                return GateResult(gate=name, ok=False, detail="拦住了")
            return GateResult(gate=name, ok=True)
        _g.__name__ = name
        return _g


def _placer(log, ok=True, err="", order=None):
    async def _p(intent, ctx):
        log.append("place")
        return ok, order, err
    return _p


# ------------------------------------------------------- 1. 顺序即优先级
@pytest.mark.asyncio
async def test_gates_run_in_declared_order():
    """闸门必须按注册顺序跑 —— 顺序就是优先级。"""
    rec = _Recorder()
    placed = []
    r = OrderRouter([rec.gate(GATE_UNATTENDED_STR), rec.gate(GATE_SIZING),
                     rec.gate(GATE_HALTED)], _placer(placed))
    res = await r.route(_intent())
    assert rec.calls == [GATE_UNATTENDED_STR, GATE_SIZING, GATE_HALTED]
    assert placed == ["place"]
    assert res.placed


GATE_UNATTENDED_STR = "无人值守闸"


@pytest.mark.asyncio
async def test_rejected_gate_stops_the_chain():
    """第一道拦住就停：后面的闸**一次都不许跑**。

    ★ 为什么这条重要：后面的闸可能有副作用（限额计数、告警、限频）。
      如果拦住之后继续跑，等于"没通过还扣了预算"。
    """
    rec = _Recorder(reject_at=GATE_MANUAL_LOT)
    placed = []
    r = OrderRouter([rec.gate(GATE_UNATTENDED_STR), rec.gate(GATE_MANUAL_LOT),
                     rec.gate(GATE_SIZING)], _placer(placed))
    res = await r.route(_intent())
    assert rec.calls == [GATE_UNATTENDED_STR, GATE_MANUAL_LOT]
    assert placed == [], "被拦住却还是下单了"
    assert res.status == ROUTE_REJECTED
    assert res.failed_gate == GATE_MANUAL_LOT
    assert GATE_MANUAL_LOT in res.rejection
    assert "拦住了" in res.rejection


@pytest.mark.asyncio
async def test_all_gates_are_recorded_not_only_the_failed_one():
    """结果里保留**所有**跑过的闸，包括放行的。

    事后回溯最常问"它当时过了哪几关"，只记最后一道失败等于把通过的信息丢了。
    """
    rec = _Recorder(reject_at=GATE_SIZING)
    r = OrderRouter([rec.gate(GATE_UNATTENDED_STR), rec.gate(GATE_SIZING)],
                    _placer([]))
    res = await r.route(_intent())
    assert [g.gate for g in res.gates] == [GATE_UNATTENDED_STR, GATE_SIZING]
    assert [g.ok for g in res.gates] == [True, False]


# --------------------------------------------- 2. 跳过 = 不适用，不是拒绝
@pytest.mark.asyncio
async def test_skipped_gate_is_not_recorded_and_does_not_block():
    """闸门返回 None = 本次不适用：不记入结果、不影响后续。"""
    rec = _Recorder(skip_at=(GATE_SIZING,))
    placed = []
    r = OrderRouter([rec.gate(GATE_UNATTENDED_STR), rec.gate(GATE_SIZING),
                     rec.gate(GATE_HALTED)], _placer(placed))
    res = await r.route(_intent())
    assert rec.calls == [GATE_UNATTENDED_STR, GATE_SIZING, GATE_HALTED]
    assert [g.gate for g in res.gates] == [GATE_UNATTENDED_STR, GATE_HALTED]
    assert res.placed and placed == ["place"]


# --------------------------------------- 3. 下单失败必须可观测（幽灵动作）
@pytest.mark.asyncio
async def test_placer_failure_is_returned_not_swallowed():
    """下单失败 → status=empty + error 带原因，绝不能伪装成成功。

    2026-11-24：-4015 让每张单都挂不出去，但 15 条 thesis 都被推成 FILLED
    又平掉 —— 全是没有成交基础的幽灵动作。根因就是失败只活在日志里。
    """
    r = OrderRouter([], _placer([], ok=False, err="code=-4015"))
    res = await r.route(_intent())
    assert res.status == ROUTE_EMPTY
    assert res.placed is False
    assert res.error == "code=-4015"
    assert "下单失败" in res.rejection


@pytest.mark.asyncio
async def test_placed_result_keeps_the_order_object():
    class _Order:
        executed_qty = 0.012
    r = OrderRouter([], _placer([], order=_Order()))
    res = await r.route(_intent())
    assert res.status == ROUTE_PLACED
    assert res.order is not None
    assert res.placed_qty == pytest.approx(0.012)


# ----------------------------------------------------------- 4. 异常不吞
@pytest.mark.asyncio
async def test_gate_exception_propagates():
    """闸门异常必须向上抛，不能变成本模块的"拒绝"。

    ★ 这条守的是行为一致性：改之前闸门异常会冒到
      `_apply_pending_action` 的 except，记 `pending_action_error`
      并让动作**不算已执行**（下一根 K 线重试）。
      路由层若吞掉并当成拒绝，"重试"语义就没了 —— 那是行为变化。
    """
    async def _boom(intent, ctx):
        raise RuntimeError("闸门炸了")
    r = OrderRouter([_boom], _placer([]))
    with pytest.raises(RuntimeError, match="闸门炸了"):
        await r.route(_intent())


@pytest.mark.asyncio
async def test_placer_exception_propagates():
    async def _boom(intent, ctx):
        raise ValueError("下单炸了")
    r = OrderRouter([], _boom)
    with pytest.raises(ValueError, match="下单炸了"):
        await r.route(_intent())


# ---------------------------------------------- 5. EntryIntent 自校验
def test_entry_intent_rejects_bad_side():
    with pytest.raises(ValueError, match="long/short"):
        _intent(side="buy")


def test_entry_intent_rejects_zero_entry():
    with pytest.raises(ValueError, match="entry"):
        _intent(entry=0.0)


def test_entry_intent_allows_zero_stop_but_not_negative():
    """止损 0 合法（唐奇安刻意不设硬停损）；负数一定是上游算错。"""
    assert _intent(stop=0.0).stop == 0.0
    with pytest.raises(ValueError, match="stop"):
        _intent(stop=-1.0)


def test_entry_intent_requires_strategy():
    with pytest.raises(ValueError, match="strategy"):
        _intent(strategy="")


def test_exit_intent_slot_is_closed_set():
    """出场意图的 slot 只有两个值 —— 这是拆仓的产物，不能自由发明。"""
    assert ExitIntent(slot="adopted", action="EXIT_STOP").slot == "adopted"
    with pytest.raises(ValueError, match="system/adopted"):
        ExitIntent(slot="manual", action="EXIT_STOP")


def test_gate_result_requires_a_name():
    with pytest.raises(ValueError, match="名字"):
        GateResult(gate="", ok=False, detail="x")


# ------------------------------------------------- 6. 决策落库形状不变
def test_as_decision_gates_matches_existing_shape():
    """`decisions.gates` 列已有读者，路由结果转出来的形状必须一致。"""
    res = RouteResult(status=ROUTE_REJECTED, gates=(
        GateResult(gate=GATE_SIZING, ok=False, detail="名义 12U < 最小 55U"),))
    assert res.as_decision_gates() == [
        {"gate": GATE_SIZING, "ok": False, "detail": "名义 12U < 最小 55U"}]


def test_router_gate_names_are_inspectable():
    """启动时能打出"这条链上有哪几关、什么顺序"。"""
    rec = _Recorder()
    r = OrderRouter([rec.gate(GATE_UNATTENDED_STR), rec.gate(GATE_SIZING)],
                    _placer([]))
    assert r.gate_names() == [GATE_UNATTENDED_STR, GATE_SIZING]


def test_position_events_carry_close_booking_numbers():
    """平仓事件必须同时带毛/费/净 —— 口径由 close_booking 唯一决定。"""
    ev = PositionClosed(slot="system", side="long", qty=0.032, entry=60880.0,
                        exit_price=61289.4, gross=13.1008, fee=2.18, net=10.9208,
                        reason="挂单策略出场")
    assert ev.net == pytest.approx(ev.gross - ev.fee)
    op = PositionOpened(slot="adopted", side="short", qty=0.04, entry=60000.0,
                        stop=60970.9)
    assert op.slot == "adopted"


def test_router_requires_callable_placer():
    with pytest.raises(TypeError, match="placer"):
        OrderRouter([], "not-callable")


# ============================================================ 7. 真正接线
# 下面两条是 BUS-A 的**核心验收**：证明 `_pending_place` 真的走路由层。
#
# ★ 为什么非要这一条：这个仓库已经有过两个"孤儿"——
#   `/api/trade/decisions` 接口写好了没有任何页面调用；
#   `position_guard.decide()`（完整的 P0→P5 出场流水线）生产零调用。
#   新模块写完不接线，测试全绿也等于没做。

def _pending_engine_stub(can_open=True, place_err=""):
    """只够跑 `_pending_place` 的引擎桩，绑**真实**的路由/闸门/下单方法。"""
    import types
    from ai_trader.trading import engine as E

    placed = []

    class _Ex(object):
        async def place_order(self, side, qty, order_type="LIMIT",
                              price=None, client_id="", **kw):
            placed.append((side, qty, order_type, price, client_id))
            if place_err:
                return None, place_err
            return types.SimpleNamespace(status="NEW"), ""

    class _Db(object):
        def __init__(self):
            self.risks = []

        def log_risk(self, kind, detail=""):
            self.risks.append((kind, detail))

        def kinds(self):
            return [k for k, _ in self.risks]

    class _Guard(object):
        def can_open(self):
            return (can_open, "" if can_open else "日亏损已达上限")

    st = types.SimpleNamespace()
    st.ex = _Ex()
    st.db = _Db()
    st.guard = _Guard()
    st.tcfg = types.SimpleNamespace(symbol="BTCUSDT", strategy="pending")
    # 手数计算绕开（它需要完整 sizing 配置）；本测试验的是路由，不是 sizing。
    st._pending_size = lambda price, stop, otype: (0.01, "")

    for name in ("_pending_place", "_pending_router",
                 "_gate_pending_unattended", "_gate_pending_size",
            "_gate_pending_margin", "_pending_need_margin",
            "_reclaim_margin",
                 "_place_pending_order"):
        raw = E.DecisionEngine.__dict__[name]
        if isinstance(raw, staticmethod):
            setattr(st, name, raw.__func__)
        else:
            def _bound(*a, _n=name, **kw):
                return getattr(E.DecisionEngine, _n)(st, *a, **kw)
            # ★ 保留真实名字：生产里这些是绑定方法，`__name__` 就是方法名。
            #   桩若留成 "<lambda>"，`gate_names()` 这类"可观测性"断言
            #   就变成了在测桩本身，而不是产品行为。
            _bound.__name__ = name
            setattr(st, name, _bound)
    return st, placed


def _sell_action():
    import types
    return types.SimpleNamespace(
        kind="place_order", thesis_id="t1", reason="thesis_armed",
        price=61000.0, side="sell", order_type="LIMIT", bar_ts=1795000000000)


def _thesis():
    import types
    return types.SimpleNamespace(order_price=61000.0, stop_price=61100.0,
                                 target_price=60800.0, trend="down")


@pytest.mark.asyncio
async def test_pending_place_goes_through_the_router():
    """`_pending_place` 必须真的走路由层，并且 long/short 译回买/卖。"""
    st, placed = _pending_engine_stub()
    from ai_trader.trading import engine as E
    ok = await E.DecisionEngine._pending_place(st, _sell_action(), _thesis())
    assert ok is True
    assert len(placed) == 1
    side, qty, otype, price, cid = placed[0]
    # thesis.side 是小写 sell → 下单接口要 "SELL"
    assert side == "SELL"
    assert qty == pytest.approx(0.01)
    assert cid.startswith("AI-pend-")
    assert "pending_placed" in st.db.kinds()


@pytest.mark.asyncio
async def test_pending_place_is_blocked_by_gate_without_calling_the_exchange():
    """闸门拦住时**一次都不能下单** —— 这是路由层存在的全部意义。

    同时验证拦截原因落在 `pending_blocked_by_guard`（沿用旧事件名，
    因为运维面板和历史记录都按这个名字搜）。
    """
    st, placed = _pending_engine_stub(can_open=False)
    from ai_trader.trading import engine as E
    ok = await E.DecisionEngine._pending_place(st, _sell_action(), _thesis())
    assert ok is False
    assert placed == [], "被闸门拦住了却还是下了单"
    assert "pending_blocked_by_guard" in st.db.kinds()


@pytest.mark.asyncio
async def test_pending_place_failure_keeps_the_old_risk_event():
    """下单失败仍记 `pending_place_failed`，并返回 False（上层据此不标记已执行）。"""
    st, placed = _pending_engine_stub(place_err='code=-4015')
    from ai_trader.trading import engine as E
    ok = await E.DecisionEngine._pending_place(st, _sell_action(), _thesis())
    assert ok is False
    assert len(placed) == 1
    assert "pending_place_failed" in st.db.kinds()


def test_pending_router_declares_its_gates_in_order():
    """拦截链的顺序必须可直接读出：无人值守闸 → 仓位闸。"""
    st, _ = _pending_engine_stub()
    names = st._pending_router().gate_names()
    assert names == ["_gate_pending_unattended", "_gate_pending_size",
                     "_gate_pending_margin"]

