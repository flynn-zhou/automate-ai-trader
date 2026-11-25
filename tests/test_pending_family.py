# -*- coding: utf-8 -*-
"""策略家族契约：``PATTERN_FAMILY`` → ``Plan.family`` → ``Thesis.family``。

★ 为什么单独钉：失效判据的轴是**家族**（HANDOFF §23.23）。
  这条链断在任何一环，都会**静默**退回默认值 —— 不报错，
  只是判据变了：
    · `PATTERN_FAMILY` 标错 → 趋势被当反转（永不因 ema 失效）
    · `Plan.family` 默认成反转 → 忘传就静默豁免（原缺陷的形状）
    · `_build` 写死趋势 → 反转被 ema 白杀（原缺陷本身）
    · `to_thesis` 丢家族 → 计划上对、预期上错

★ 家族与**镜像无关**：上涨/下跌只切换方向，不切换家族。
  这一点最容易被「按方向判」的直觉带偏。
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

Bar = namedtuple("Bar", "ts o h l c v")


def _seq(n, space_ms, start, half_range=160.0):
    return [Bar(start + i * space_ms, PX, PX + half_range, PX - half_range,
                PX, 1.0) for i in range(n)]


class _Book:
    """线值替身。

    ★ 2026-11-20 两处修正：

      ① **不能对所有键返回同一个值**。趋势背景闸要比较 ema20 与 ema50 的位置，
         两条线相等时「快线在慢线下方」不成立 → 被判成「在尝试反转」→
         三引线/倒V 一条都出不来。真实行情里不同周期的均线不可能恒等。

      ② **位置必须能按背景翻转**。闸是按方向判的：
           做多（下跌背景） ema20 < ema50
           做空（上涨镜像） ema20 > ema50
         一个固定替身不可能同时满足两者，而 `test_mirror_keeps_the_family`
         正是同时跑 base 与 mirror 的那条测试。
    """

    def __init__(self, fast_above: bool = False):
        #: True = 上涨背景（快线在上方，供做空/镜像）；False = 下跌背景。
        self.fast_above = bool(fast_above)
        if self.fast_above:
            self._prices = {T.TREND_FAST_KEY: PX + 800.0,
                            T.TREND_SLOW_KEY: PX + 400.0}
        else:
            self._prices = {T.TREND_FAST_KEY: PX + 400.0,
                            T.TREND_SLOW_KEY: PX + 800.0}

    def at(self, key, ts):
        return T.LinePoint(price=self._prices.get(key, PX),
                           src_ts=int(ts), ready_ms=int(ts))


def _ctx(fast_above: bool = False):
    b5 = _seq(40, M5, T0)
    return P.PlanContext(bars15=_seq(20, M15, T0 - 2 * H1),
                         book=_Book(fast_above=fast_above),
                         bars_1h=_seq(20, H1, T0 - 6 * H1), bars_5m=b5)


# ------------------------------------------------------------ 表本身

def test_pattern_family_table():
    """三条形态的家族口径（用户 2026-11-17）。

    三引线 / 倒V 是「大跌抄底」→ 反转（只认止损价）；
    EMA 拒绝是「反弹做空」→ 趋势（ema 打脸即失效）。
    """
    assert P.PATTERN_FAMILY[P.KIND_WICKS] == T.FAMILY_REVERSAL
    assert P.PATTERN_FAMILY[P.KIND_INVERSE_V] == T.FAMILY_REVERSAL
    assert P.PATTERN_FAMILY[P.KIND_EMA_REJECT] == T.FAMILY_TREND


def test_every_kind_has_a_family():
    """新增形态必须登记家族 —— 漏登记要当场炸，不许静默取默认值。"""
    for k in P.KINDS:
        assert k in P.PATTERN_FAMILY, "形态 %r 没登记家族" % (k,)
        assert P.PATTERN_FAMILY[k] in (T.FAMILY_REVERSAL, T.FAMILY_TREND)


# ------------------------------------------------------------ 默认值

def test_plan_default_family_is_trend():
    """★ `Plan.family` 默认**趋势**，不是反转。

    默认取反转 = 任何忘了填的路径都静默豁免 ema。那正是原缺陷的形状：
    判据没了，肉眼看不出来。
    """
    p = P.Plan(kind="x", direction=P.UP, order_type=T.ORDER_LIMIT,
               bar_index=0, bar_ts=T0, order_price=1.0, stop_price=0.9,
               target_price=1.2)
    assert p.family == T.FAMILY_TREND


def test_thesis_default_family_is_trend():
    th = T.Thesis("t", "x", T.UP, order_price=1.0, stop_price=0.9,
                  target_price=1.2)
    assert th.family == T.FAMILY_TREND


# ------------------------------------------------------------ _build 接线

def test_build_records_reversal_family():
    """倒V 是反转 —— `_build` 必须从表里取，不许写死趋势。"""
    plan, why = P._build(kind=P.KIND_INVERSE_V, direction=P.UP,
                         order_type=T.ORDER_STOP, i=10, ctx=_ctx(),
                         params=P.PendingParams(), order_price=PX + 50.0,
                         evidence={}, bar=None)
    assert why == "ok", why
    assert plan.family == T.FAMILY_REVERSAL


def test_build_records_trend_family():
    plan, why = P._build(kind=P.KIND_EMA_REJECT, direction=P.DOWN,
                         order_type=T.ORDER_STOP, i=10, ctx=_ctx(),
                         params=P.PendingParams(), order_price=PX - 50.0,
                         evidence={}, bar=None)
    assert why == "ok", why
    assert plan.family == T.FAMILY_TREND


@pytest.mark.parametrize("kind", P.KINDS)
def test_mirror_keeps_the_family(kind):
    """★ 镜像只换方向，**不换家族**。

    「按方向判家族」的直觉就是从这里错掉的：上涨里的倒V 是**做空**，
    但它仍然是反转，不该被 ema 判死。
    """
    base_dir = P.PATTERN_SPEC[kind]["direction"]
    mirror_dir = P.DOWN if base_dir == P.UP else P.UP
    params = P.PendingParams()
    family = P.PATTERN_FAMILY[kind]
    for d in (base_dir, mirror_dir):
        # ★ 每个方向用它自己的背景，而「正确背景」由**族**决定：
        #     反转族（三引线/倒V）：逆着做 → 做多要跌、做空要涨
        #     趋势族（EMA拒绝）：  顺着做 → 做空要跌、做多要涨
        #   拿同一个 ctx 跑两个方向，趋势闸必然拒掉其中一个 ——
        #   它拒的理由（位置不对）跟这条测试要验的（家族不随方向变）
        #   毫无关系。夹具配错会把它伪装成「家族改了」。
        want_down = (d == P.UP) if family == T.FAMILY_REVERSAL else (d == P.DOWN)
        ctx = _ctx(fast_above=not want_down)
        otype = P.PATTERN_SPEC[kind]["order_type"]
        close = PX
        # 挂单价放在能通过「还没跑过去」校验的一侧
        falls = T.triggers_when_price_falls(d, otype)
        price = close - 50.0 if falls else close + 50.0
        plan, why = P._build(kind=kind, direction=d, order_type=otype,
                             i=10, ctx=ctx, params=params,
                             order_price=price, evidence={}, bar=None)
        assert why == "ok", (kind, d, why)
        assert plan.family == P.PATTERN_FAMILY[kind], (
            "%s 方向 %s 的家族变成了 %r —— 镜像不该改家族"
            % (kind, d, plan.family))


def test_to_thesis_carries_the_family():
    """计划上的家族必须过到预期上，否则 `run` 会退回默认（趋势）。"""
    plan, _ = P._build(kind=P.KIND_WICKS, direction=P.UP,
                       order_type=T.ORDER_LIMIT, i=10, ctx=_ctx(),
                       params=P.PendingParams(), order_price=PX - 50.0,
                       evidence={}, bar=None)
    th = P.to_thesis(plan)
    assert th.family == plan.family == T.FAMILY_REVERSAL, (
        "to_thesis 丢了家族：plan=%r thesis=%r" % (plan.family, th.family))

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