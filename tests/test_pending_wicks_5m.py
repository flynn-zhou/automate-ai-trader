# -*- coding: utf-8 -*-
"""三引线在 **5m** 上的端到端回归（生产路径）。

★ 为什么单独一个文件：用户 2026-11-17 把三引线从 15m 切到 5m
  （`BAR_SPACE[KIND_WICKS] = "5m"`，依据见 HANDOFF §23.21：
  同一个 7 段开仓窗口，15m 出 0 条、5m 出 1 条）。

  `tests/test_pending_plan.py` 钉的是**15m 版**的形态几何（显式把
  `BAR_SPACE` 钉回 15m），所以那条路径不会被这层切换覆盖到。
  如果只有 15m 的测试，生产切到 5m 之后「claim 到底挂不挂得出去」
  没有任何回归 —— 而这类错的表现恰恰是**静默空转**。

★ 这个文件钉三件事：
  1. 5m 上能真的检出计划，且 `plan.bar_space == "5m"`；
  2. claim 落在**信号那根 5m 自己**的收盘（`claim_on_entry=True`）——
     否则 claim 会拿 5m 的 ts 去比 15m 的 K，永远不挂单；
  3. `run_plan` 能把这条计划推进到「挂出 LIMIT」，`start_at` 换算正确。
"""
import os
import sys
from collections import namedtuple

import pytest
from dataclasses import dataclass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai_trader.trading import pending_plan as P    # noqa: E402
from ai_trader.trading import thesis as T          # noqa: E402

M5 = 300_000
M15 = T.BAR_INTERVAL_MS
H1 = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % H1)

PX = 100_000.0
TICK = 0.1

Bar = namedtuple("Bar", "ts o h l c v")


def b5(i, o, h, l, c):
    return Bar(T0 + i * M5, float(o), float(h), float(l), float(c), 1.0)


class FakeBook:
    """只认一条水平线的 LineBook 替身。"""

    def __init__(self, price):
        self.price = float(price)

    _OFFSETS = {T.TREND_FAST_KEY: 400.0, T.TREND_SLOW_KEY: 800.0}

    def at(self, key, ts):
        # ★ 2026-11-20：趋势背景闸要 ema20/ema50 两条且位置正确。
        #   只认一条水平线的替身会让两条线相等 → 判成「在尝试反转」。
        return T.LinePoint(price=self.price + self._OFFSETS.get(key, 0.0),
                           src_ts=int(ts), ready_ms=int(ts))


def _ctx(bars5, level, *, h1_amp=320.0):
    """15m 与 1h 都要**铺到 T0 之前**，否则 `h1_prev2_amp` 取不到
    「前两根已收盘的 1h」→ 返回 None → `no_amp` → 一条计划都没有。
    （第一版夹具就是这么写的，现象是「明明形态对，却 0 条」。）
    """
    end = int(bars5[-1].ts) + M5
    h1_start = T0 - 6 * H1
    n1h = int((end - h1_start) // H1) + 2
    b1h = [Bar(h1_start + i * H1, PX, PX + h1_amp / 2, PX - h1_amp / 2, PX, 1.0)
           for i in range(n1h)]
    m15_start = T0 - 2 * H1
    n15 = int((end - m15_start) // M15) + 2
    b15 = [Bar(m15_start + i * M15, PX, PX + h1_amp / 2, PX - h1_amp / 2, PX, 1.0)
           for i in range(n15)]
    return P.PlanContext(bars15=b15, book=FakeBook(level), bars_1h=b1h,
                         bars_5m=bars5)


#: 新口径三引线在 5m 序列上的位置：A 在第 5 根，确认根 B 在第 8 根。
#: ★ B 必须落在 **15m 网格之外**（第 8 根 = 40 分钟），否则
#:   `plan_bar` 用例分不清「按 bar_space 取 5m」和「写死去 15m 找」。
WICKS_A_IDX = 5
WICKS_B_IDX = 8


def _wicks_5m(level=101_000.0, n=40):
    """造一组 5m 三引线（2026-11-20 口径）。

        A  (第 5 根)：扫出新低 PX-200，下影 150
        i1 (第 6 根)：A 的 inside bar
        i2 (第 7 根)：A 的 inside bar
        B  (第 8 根)：回到 A 低点 +20 的口袋里（low PX-190），下影 130

    → 挂单价 = min(A.low, B.low) + 10 = PX-190。
    `level` 只用来喂水位线（新口径下它只进证据，不是前置条件）。
    """
    bars = []
    for i in range(n):
        if i == WICKS_A_IDX:
            bars.append(b5(i, PX, PX + 20.0, PX - 200.0, PX - 50.0))
        elif i == WICKS_A_IDX + 1:
            bars.append(b5(i, PX - 40.0, PX + 10.0, PX - 120.0, PX - 60.0))
        elif i == WICKS_A_IDX + 2:
            bars.append(b5(i, PX - 60.0, PX + 0.0, PX - 100.0, PX - 70.0))
        elif i == WICKS_B_IDX:
            bars.append(b5(i, PX - 70.0, PX - 60.0, PX - 190.0, PX - 80.0))
        else:
            bars.append(b5(i, PX - 20.0, PX + 30.0, PX - 30.0, PX + 10.0))
    return bars


def test_bar_space_says_wicks_run_on_5m():
    """生产口径：三引线扫 5m（别被谁悄悄改回 15m）。"""
    assert P.BAR_SPACE[P.KIND_WICKS] == "5m"


def test_wicks_are_detected_on_the_5m_series():
    bars = _wicks_5m()
    ctx = _ctx(bars, level=101_000.0)
    plans, stats = P.detect_all(ctx, kinds=(P.KIND_WICKS,),
                                require_window=False, include_mirror=False)
    assert len(plans) == 1, ("5m 上应检出 1 条三引线；实际 %d 条，stats=%r"
                             % (len(plans), stats))
    p = plans[0]
    assert p.bar_space == "5m", "计划必须记 5m 时钟，否则 start_at/claim 全错"
    assert p.bar_ts in {b.ts for b in bars}, "bar_ts 必须来自 5m 序列"
    assert p.order_type == T.ORDER_LIMIT
    assert p.direction == P.UP, "三引线基准版是抄底做多"


def test_claim_lands_on_the_5m_signal_bar():
    """★ claim 必须落在信号那根 5m 自己。

    配错时钟（拿 5m 的 ts 去比 15m 的 K）不会报错，
    只会「永远不挂单」—— 倒V 踩过 343/1053 条。
    """
    bars = _wicks_5m()
    ctx = _ctx(bars, level=101_000.0)
    plans, _ = P.detect_all(ctx, kinds=(P.KIND_WICKS,),
                            require_window=False, include_mirror=False)
    p = plans[0]
    assert p.claim_on_entry is True, (
        "5m 形态必须在入场序列上声明；否则 claim 比的是 15m 的 K，永不命中")
    claim = P.claim_for(p)
    hit, ev = claim(0, [b for b in bars], None)
    assert ev["claim_space"] == "5m"
    # 只有那根 5m 命中
    hits = [i for i in range(len(bars)) if claim(i, bars, None)[0]]
    assert hits == [bars.index(next(b for b in bars if b.ts == p.bar_ts))], (
        "claim 应只在那根 5m 上为 True；实际命中 %r" % (hits,))


def test_run_plan_arms_a_limit_on_the_5m_clock():
    """端到端：检出的 5m 三引线能被状态机推进到挂单。"""
    bars = _wicks_5m()
    ctx = _ctx(bars, level=101_000.0)
    plans, _ = P.detect_all(ctx, kinds=(P.KIND_WICKS,),
                            require_window=False, include_mirror=False)
    p = plans[0]
    b15 = [Bar(T0 + i * M15, PX, PX + 160.0, PX - 160.0, PX, 1.0)
           for i in range(int((int(bars[-1].ts) - T0) // M15) + 2)]
    res = P.run_plan(p, b15, ctx.book, bars5=bars)
    states = [t.to_state for t in res.transitions]
    assert "ARMED" in states, (
        "5m 三引线没被挂出去（states=%r）—— 典型原因是 claim 时钟配错" % (states,))
    assert res.transitions, "一条转换都没有 = 静默空转"


def test_plan_bar_reads_the_series_named_by_bar_space():
    """★ `ctx.plan_bar(plan)` 必须按 `bar_space` 取那根 K。

    变异体 `plan-bar-hardcodes-15m`（改回只看 `bars15`）一开始**存活** ——
    没有任何测试摸过 `plan_bar`。夹具里形态落在 5m 序列的第 8 根
    （= T0+40 分钟，**不在 15m 网格上**），所以写死 15m 会返回 None。
    """
    bars = _wicks_5m()
    ctx = _ctx(bars, level=101_000.0)
    plans, _ = P.detect_all(ctx, kinds=(P.KIND_WICKS,),
                            require_window=False, include_mirror=False)
    p = plans[0]
    got = ctx.plan_bar(p)
    assert got is not None, (
        "plan_bar 取不到这根 5m（bar_ts=%d）—— 多半是写死去 15m 序列找了"
        % (p.bar_ts,))
    assert int(got.ts) == int(p.bar_ts)
    # 那根 5m（确认根）的 high 是 PX-60；15m 序列里的 high 都是 PX+160
    assert float(got.h) == PX - 60.0, (
        "取到的是 15m 的 K（h=%r），不是那根 5m" % (got.h,))


def test_start_at_is_converted_to_the_15m_series():
    """5m 下标必须按时间换算成 15m 下标，不许直接当 15m 用。"""
    b15 = [Bar(T0 + i * M15, PX, PX + 160.0, PX - 160.0, PX, 1.0)
           for i in range(20)]
    plan = P.Plan(kind=P.KIND_WICKS, direction=P.UP, order_type=T.ORDER_LIMIT,
                  bar_index=40, bar_ts=b15[5].ts, bar_space="5m",
                  order_price=PX - 100, stop_price=PX - 300,
                  target_price=PX + 100)
    assert P._b15_index_for_plan(plan, b15) == 5, (
        "5m 下标 40 直接当 15m 用会从第 40 根开始 → 越界/几乎不推进")

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