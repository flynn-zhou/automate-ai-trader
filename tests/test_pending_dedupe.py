# -*- coding: utf-8 -*-
"""去重：同一窗口内、同形态同方向只保留第一条。

★ 这条规则的来历（实测，HANDOFF §23.19）：
  2026-08-04 那个窗口里「上涨EMA拒绝」连续出了 5 条，挂单价全是 64397.7。
  形态在连续几根上都成立，探测器就会连续出单 —— 实盘等于 5 倍仓位，
  统计上等于同一个 Setup 被数了 5 次。

★ 以及一个**必须靠测试盯住**的坑：
  记录窗口键必须发生在「判窗口的同一个地方」，不许事后用 `plan.bar_ts`
  反推 —— 实测 `plan.bar_ts` 与判窗口用的收盘时刻差一个周期，
  反推会让窗口边缘那条落到别的窗口，去重就漏了。
  外部表现是「UP 窗口 5 段，却出了 6 条上涨倒V」。
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

HOUR = 3_600_000
M15 = 900_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % HOUR)


def mk(kind, direction, bar_ts, price=100.0):
    return P.Plan(kind=kind, direction=direction,
                  order_type=T.ORDER_STOP, bar_index=0, bar_ts=bar_ts,
                  order_price=price, stop_price=price - 1,
                  target_price=price + 2)


# ---------------------------------------------------------------- 单元

def test_one_plan_per_kind_direction_window():
    """同窗口同形态同方向 3 条 → 只留第一条。"""
    plans = [mk(P.KIND_EMA_REJECT, P.DOWN, T0 + i * M15, 100.0 + i) for i in range(3)]
    keys = [(T0, T0 + 6 * HOUR)] * 3
    got = P.dedupe_plans(plans, keys)
    assert len(got) == 1
    assert got[0].bar_ts == T0, "必须留**第一条**（最早的），不是最后一条"


def test_different_windows_are_kept():
    """不同窗口各留一条。"""
    plans = [mk(P.KIND_EMA_REJECT, P.DOWN, T0 + i * 10 * HOUR) for i in range(3)]
    keys = [(T0 + i * 10 * HOUR, T0 + (i + 1) * 10 * HOUR) for i in range(3)]
    got = P.dedupe_plans(plans, keys)
    assert len(got) == 3


def test_different_kinds_are_kept():
    """同一窗口里不同形态各自留一条（它们是不同的交易逻辑）。"""
    plans = [mk(k, P.UP, T0 + i * M15) for i, k in enumerate(P.KINDS)]
    keys = [(T0, T0 + 6 * HOUR)] * 3
    got = P.dedupe_plans(plans, keys)
    assert len(got) == 3


def test_opposite_directions_are_kept():
    """同窗口里方向相反的两条都留（虽然生产上不该出现，去重不管这事）。"""
    plans = [mk(P.KIND_EMA_REJECT, P.DOWN, T0), mk(P.KIND_EMA_REJECT, P.UP, T0 + M15)]
    keys = [(T0, T0 + 6 * HOUR)] * 2
    assert len(P.dedupe_plans(plans, keys)) == 2


def test_plans_outside_any_window_are_not_deduped():
    """窗口键 None → 不去重。丢了会表现成「少了几条」，比报错难查。"""
    plans = [mk(P.KIND_EMA_REJECT, P.DOWN, T0 + i * M15) for i in range(3)]
    got = P.dedupe_plans(plans, [None, None, None])
    assert len(got) == 3


def test_disabled_returns_everything():
    plans = [mk(P.KIND_EMA_REJECT, P.DOWN, T0 + i * M15) for i in range(3)]
    keys = [(T0, T0 + 6 * HOUR)] * 3
    assert len(P.dedupe_plans(plans, keys, one_per_window=False)) == 3


def test_length_mismatch_fails_loudly():
    """长度不一致必须硬报错 —— 对齐错了会静默丢计划。"""
    plans = [mk(P.KIND_EMA_REJECT, P.DOWN, T0)]
    with pytest.raises(ValueError, match="window_keys"):
        P.dedupe_plans(plans, [])


# ---------------------------------------------------------------- 集成

def _ctx_with_window(n_bars, window_bars):
    """造 n_bars 根 15m；窗口覆盖 [T0, T0+window_bars*M15)。

    ★ 右端不包含：bar 0 的**收盘**是 T0+M15，所以 window_bars=6
      恰好覆盖 bar 0..4 的收盘（5 根候选）。
    """
    @dataclass
    class B:
        ts: int
        o: float
        h: float
        l: float
        c: float
        v: float

    bars = [B(ts=T0 + i * M15, o=100.0, h=101.0, l=99.0, c=100.0, v=1.0)
            for i in range(n_bars)]
    win = P.OpenWindow(T0, T0 + window_bars * M15, P.DOWN)
    ctx = P.PlanContext(bars15=bars, book=None, windows=[win])
    return ctx, bars


def test_detect_all_dedupes_repeated_signals_in_one_window(monkeypatch):
    """★ 集成：探测器每根都出单，但同一窗口只许返回 1 条。

    这条同时盯住「窗口键必须在判窗口处记录」那个坑：
    用假的探测器返回 bar_ts = 序列里的 ts（不是收盘时刻），
    如果实现改成事后用 bar_ts 反推窗口，这里就会算出 2 条而不是 1 条。
    """
    ctx, bars = _ctx_with_window(n_bars=8, window_bars=6)

    def always(i, ctx_, params, direction):
        return mk(P.KIND_EMA_REJECT, direction, int(bars[i].ts)), "ok"

    monkeypatch.setitem(P.DETECTORS, P.KIND_EMA_REJECT, always)
    # 只跑基准版（KIND_WICKS 基准方向 UP，背景 DOWN）；关镜像免得翻倍
    plans, stats = P.detect_all(ctx, kinds=(P.KIND_EMA_REJECT,),
                                require_window=True, include_mirror=False)
    assert len(plans) == 1, "同窗口里 5 根都出单，去重后应只剩 1 条"
    assert stats.get("%s:ok" % P._variant_name(P.KIND_EMA_REJECT, P.DOWN)) == 5, (
        "stats 必须记**去重前**的 5 次；禁止静默过滤")


def test_detect_all_dedupe_can_be_turned_off(monkeypatch):
    """关掉去重 → 5 条全在（用于对比测量，也证明去重真的是它干的）。"""
    ctx, bars = _ctx_with_window(n_bars=8, window_bars=6)

    def always(i, ctx_, params, direction):
        return mk(P.KIND_EMA_REJECT, direction, int(bars[i].ts)), "ok"

    monkeypatch.setitem(P.DETECTORS, P.KIND_EMA_REJECT, always)
    plans, _ = P.detect_all(ctx, kinds=(P.KIND_EMA_REJECT,), require_window=True,
                            include_mirror=False, dedupe=False)
    assert len(plans) == 5

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