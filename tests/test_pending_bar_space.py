# -*- coding: utf-8 -*-
"""`BAR_SPACE` 必须是「扫哪条序列」的**唯一来源**（HANDOFF §23.21）。

★ 修的是什么（第 5 次「两套」）：
  以前「扫哪条序列」有两套实现 ——
    · `detect_all` 按 `BAR_SPACE[kind]` 选 `seq`
    · 探测器里却硬编码 `ctx.bars15` / `ctx.bars5`
  两者只在「三条形态各自恰好是 15m/5m/15m」时才碰巧一致。
  把三引线改到 5m 立刻 `IndexError`（`detect_all` 用 5m 的下标，
  探测器却去取 15m 的列表）。

★ 同时修掉的隐含假设：`Plan.bar_index` / `bar_ts` 属于哪条序列
  以前是**隐含 15m**。改到 5m 后 `run_plan` 的 `start_at` 会按 15m 解释
  5m 的下标（起点跑到很后面），`claim_for` 会拿 5m 的 ts 比 15m 的 K
  （永远不挂单），而且都不报错。
"""
import os
import sys
from dataclasses import dataclass

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai_trader.trading import pending_plan as P    # noqa: E402
from ai_trader.trading import thesis as T          # noqa: E402

M15 = 900_000
M5 = 300_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % M15)


@dataclass
class B:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float = 1.0


def _mk(n, space_ms, start=T0, base=100.0):
    return [B(ts=start + i * space_ms, o=base, h=base + 1, l=base - 1,
              c=base) for i in range(n)]


def _ctx(n15=6, n5=20):
    return P.PlanContext(bars15=_mk(n15, M15), book=None,
                         bars_5m=_mk(n5, M5))


# ---------------------------------------------------------------- 唯一来源

def test_bar_space_maps_each_kind_to_its_series():
    ctx = _ctx()
    # ★ 三引线按用户口径切到 5m（HANDOFF §23.21）
    assert ctx.space_for(P.KIND_WICKS) == "5m"
    assert ctx.space_for(P.KIND_INVERSE_V) == "5m"
    assert ctx.space_for(P.KIND_EMA_REJECT) == "15m"
    assert ctx.series_for(P.KIND_INVERSE_V) is ctx.bars5
    assert ctx.series_for(P.KIND_WICKS) is ctx.bars5
    assert ctx.series_for(P.KIND_EMA_REJECT) is ctx.bars15


def test_space_ms_follows_bar_space():
    ctx = _ctx()
    assert ctx.space_ms_for(P.KIND_INVERSE_V) == M5
    assert ctx.space_ms_for(P.KIND_WICKS) == M5
    assert ctx.space_ms_for(P.KIND_EMA_REJECT) == M15


def test_unknown_bar_space_fails_loudly(monkeypatch):
    """`BAR_SPACE` 里出现不认识的周期必须硬报错。

    静默当成 15m 就是「两套」复发的入口：新增周期时忘了接线，
    表现为「探测器扫的还是老序列」，不报错。
    """
    monkeypatch.setitem(P.BAR_SPACE, P.KIND_WICKS, "3m")
    ctx = _ctx()
    with pytest.raises(ValueError, match="BAR_SPACE"):
        ctx.space_for(P.KIND_WICKS)


def test_detect_all_scans_the_series_bar_space_names(monkeypatch):
    """★ 把三引线改到 5m，`detect_all` 必须去扫 5m 那条（20 根）。

    这条就是原 bug 的回归测试：以前只改 `BAR_SPACE` 会 `IndexError`。
    """
    calls = []

    def stub(i, ctx_, params, direction):
        calls.append((i, len(ctx_.series_for(P.KIND_WICKS))))
        return None, "stub"

    monkeypatch.setitem(P.DETECTORS, P.KIND_WICKS, stub)
    monkeypatch.setitem(P.BAR_SPACE, P.KIND_WICKS, "5m")
    ctx = _ctx(n15=6, n5=20)
    P.detect_all(ctx, kinds=(P.KIND_WICKS,), require_window=False,
                 include_mirror=False)
    assert calls, "探测器没被调用"
    assert all(n == 20 for _, n in calls), (
        "series_for 没跟着 BAR_SPACE 走：%r" % (calls[:3],))
    assert max(i for i, _ in calls) == 19, "应扫满 5m 的 20 根"


def test_detector_does_not_index_out_of_range_when_switching_space(monkeypatch):
    """★ 原 bug 的直接复现：15m 只有 6 根、5m 有 20 根时改到 5m。

    旧实现探测器读 `ctx.bars15[i]` → `IndexError`。
    """
    def stub(i, ctx_, params, direction):
        # 真实探测器就是这么取根的
        _ = ctx_.series_for(P.KIND_WICKS)[i]
        return None, "stub"

    monkeypatch.setitem(P.DETECTORS, P.KIND_WICKS, stub)
    monkeypatch.setitem(P.BAR_SPACE, P.KIND_WICKS, "5m")
    ctx = _ctx(n15=6, n5=20)
    plans, stats = P.detect_all(ctx, kinds=(P.KIND_WICKS,),
                                require_window=False, include_mirror=False)
    assert plans == []
    assert stats  # 跑了


# ---------------------------------------------------------------- 计划时钟

def test_plan_bar_space_defaults_to_15m():
    p = P.Plan(kind=P.KIND_WICKS, direction=P.UP, order_type=T.ORDER_LIMIT,
               bar_index=3, bar_ts=T0 + 3 * M15, order_price=1.0,
               stop_price=0.9, target_price=1.2)
    assert p.bar_space == "15m"


def test_claim_space_reports_the_real_clock(monkeypatch):
    """`claim_for` 的 evidence 不许说谎。

    旧实现写死 `"5m" if claim_on_entry else "15m"` ——
    三引线改到 5m 后 `claim_on_entry` 仍是 False，evidence 会报 "15m"，
    排查时被它带偏。
    """
    plan = P.Plan(kind=P.KIND_WICKS, direction=P.UP, order_type=T.ORDER_LIMIT,
                  bar_index=0, bar_ts=T0, bar_space="5m",
                  order_price=1.0, stop_price=0.9, target_price=1.2)
    claim = P.claim_for(plan)
    hit, ev = claim(0, [B(ts=T0, o=1, h=1, l=1, c=1)], None)
    assert hit is True
    assert ev["claim_space"] == "5m", (
        "evidence 报了 %r，但这条计划的 bar_ts 属于 5m" % (ev["claim_space"],))


def test_build_records_bar_space_from_bar_space_table(monkeypatch):
    """★ `_build` 必须把「这条计划属于哪条序列」记进 `Plan.bar_space`。

    变异体 `plan-bar-space-not-recorded`（把 `bar_space=space` 改成
    写死 ``"15m"``）一开始**存活** —— 因为倒V 本来就用 15m 时钟，
    写死也看不出来。这条测试专门盯住「三引线改到 5m」的情形。
    """
    monkeypatch.setitem(P.BAR_SPACE, P.KIND_WICKS, "5m")
    bars15 = _mk(6, M15)
    bars5 = _mk(40, M5)
    # 1h 序列要往前铺够，否则 `h1_prev2_amp` 返回 None（→ no_amp）
    bars1h = _mk(20, 3_600_000, start=T0 - 5 * 3_600_000)
    # ★ 2026-11-20：趋势背景闸要 ema20/ema50 两条（15m）。给恒定的一对
    #   （快线在下方 = 下跌背景位置正确；斜率 0 ⇒ weak 背景）。
    #   这条测试验的是 `bar_space` 有没有记对，与趋势强弱无关；
    #   但 `book=None` 会让闸以 `no_trend_line` 拒掉，测不到想测的东西。
    book = T.LineBook()
    book.add({"line_key": T.TREND_FAST_KEY, "ts": T0 - 3_600_000,
              "line_price": float(bars5[10].c) + 400.0,
              "line_ready_ms": T0 - 3_600_000})
    book.add({"line_key": T.TREND_SLOW_KEY, "ts": T0 - 3_600_000,
              "line_price": float(bars5[10].c) + 800.0,
              "line_ready_ms": T0 - 3_600_000})
    ctx = P.PlanContext(bars15=bars15, book=book, bars_1h=bars1h,
                         bars_5m=bars5)
    params = P.PendingParams()
    # 方向 UP + LIMIT：挂单价必须低于现价
    close = float(bars5[10].c)
    plan, why = P._build(kind=P.KIND_WICKS, direction=P.UP,
                         order_type=T.ORDER_LIMIT, i=10, ctx=ctx,
                         params=params, order_price=close - 5.0,
                         evidence={}, bar=None)
    assert why == "ok", why
    assert plan.bar_space == "5m", (
        "bar_space 记成了 %r，但这条计划扫的是 5m 序列" % (plan.bar_space,))
    assert plan.bar_ts == bars5[10].ts, "取错了序列"


def test_build_defaults_to_15m_for_15m_kinds():
    """15m 的形态仍然记 "15m"（别把默认值改坏）。

    ★ 用 EMA 拒绝（仍是 15m）——三引线已经切 5m，不能再拿它当 15m 样本。
    """
    bars15 = _mk(20, M15)
    # ★ 2026-11-20：EMA 拒绝**也过趋势背景闸**（趋势族做空要下跌背景）。
    #   `book=None` 会让它以 `no_trend_line` 先被拒，测不到 bar_space。
    #   线铺在 T0 之前开始，因为 15m 口径会回看 45 分钟。
    book = T.LineBook()
    _px = float(bars15[10].c)
    for k in range(60):
        ts = T0 - 40 * M15 + k * M15
        book.add({"line_key": T.TREND_FAST_KEY, "ts": ts,
                  "line_price": _px + 400.0, "line_ready_ms": ts + M15})
        book.add({"line_key": T.TREND_SLOW_KEY, "ts": ts,
                  "line_price": _px + 800.0, "line_ready_ms": ts + M15})
    ctx = P.PlanContext(bars15=bars15, book=book,
                        bars_1h=_mk(20, 3_600_000, start=T0 - 5 * 3_600_000),
                        bars_5m=_mk(40, M5))
    params = P.PendingParams()
    close = float(bars15[10].c)
    plan, why = P._build(kind=P.KIND_EMA_REJECT, direction=P.DOWN,
                         order_type=T.ORDER_STOP, i=10, ctx=ctx,
                         params=params, order_price=close - 5.0,
                         evidence={}, bar=None)
    assert why == "ok", why
    assert plan.bar_space == "15m"
    assert plan.bar_ts == bars15[10].ts


def test_b15_index_for_plan_converts_across_spaces():
    """5m 下标必须换算成 15m 下标，否则 `start_at` 起点跑到很后面。"""
    bars15 = _mk(10, M15)
    plan = P.Plan(kind=P.KIND_WICKS, direction=P.UP, order_type=T.ORDER_LIMIT,
                  bar_index=0, bar_ts=bars15[4].ts, bar_space="5m",
                  order_price=1.0, stop_price=0.9, target_price=1.2)
    assert P._b15_index_for_plan(plan, bars15) == 4


def test_b15_index_for_plan_passes_15m_through():
    bars15 = _mk(10, M15)
    plan = P.Plan(kind=P.KIND_WICKS, direction=P.UP, order_type=T.ORDER_LIMIT,
                  bar_index=7, bar_ts=bars15[7].ts, bar_space="15m",
                  order_price=1.0, stop_price=0.9, target_price=1.2)
    assert P._b15_index_for_plan(plan, bars15) == 7

# ---------------------------------------------------------------- 闸门开关
#
# ★ 2026-11-17：第③道闸（`guard_preconditions`）接进了 `_build` 的
#   生产路径。本文件测的是**形状 / 价位 / 时钟 / 家族**，
#   背景线（ema20@30m）在这些夹具里没有意义，也不该伪造一条来凑。
#   所以这里把闸关掉；闸本身的测试在
#   `tests/test_pending_gate.py`。
@dataclass(frozen=True)
class _NoGuardParams(P.PendingParams):
    apply_guard: bool = False


@pytest.fixture(autouse=True)
def _shapes_only(monkeypatch):
    monkeypatch.setattr(P, "PendingParams", _NoGuardParams)
    monkeypatch.setattr(P, "PendingParams", _NoGuardParams)
    yield