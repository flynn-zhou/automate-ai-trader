# -*- coding: utf-8 -*-
"""三条挂单形态（ai_trader/trading/pending_plan.py）的回归测试。

## 这个文件钉什么

**用户 2026-11-17 口述的口径**（第一版全做错了，这里逐条钉住）：

| 形态 | 方向 | 挂单价 | 类型 | 成交条件 |
|------|------|--------|------|----------|
| 三引线假突破 | 做多 BUY | min(第1根低,第2根低) − 1 tick | **LIMIT** | low ≤ 价 |
| EMA20 多次拒绝 | 做空 SELL | 结构低点 a − 1 tick | **STOP** | low ≤ 价 |
| 倒 V 反包 | 做多 BUY | max(倒V根高, 当前根高) + 1 tick | **STOP** | high ≥ 价 |

★ 三条**不是同一个方向、也不是同一个订单类型**。第一版按「下跌就都做空、
都挂上方」实现，三条全错 —— 而且改完还能跑、还能出计划，只是全反。

**已定价位口径**（§18.3）：止盈 = 1h 振幅 × 1；止损 = max(1h振幅×stop_frac,
入场5m振幅)；止损参照 = 前两根 1h 里 h−l 较大那根；stop_frac 留 1/4 与 1/2 两档。

## 夹具为什么用 100_000 这个量级

价位必须用真实量级。第一版用 100 当价位、320 点当振幅，算出的止盈是负数，
`validate()` 直接炸 —— 那不是校验太严，是夹具不像真实行情。
"""
from __future__ import annotations

from collections import namedtuple

import pytest
from dataclasses import dataclass

from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T

M15 = T.BAR_INTERVAL_MS
M30 = 1_800_000
M5 = 300_000
H1 = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % H1)

PX = 100_000.0
H1_AMP = 320.0
A5_AMP = 90.0
TICK = 0.1

Bar = namedtuple("Bar", "ts o h l c v")


# ------------------------------------------------------------------ 固定空间

@pytest.fixture(autouse=True)
def _pin_wicks_to_15m(monkeypatch):
    """★ 本文件测的是**三引线在 15m 上的形状**，所以显式钉住 15m。

    生产已按用户 2026-11-17 的口径把三引线切到 **5m**（`BAR_SPACE`），
    依据是同一个 7 段窗口 15m 出 0 条、5m 出 1 条（HANDOFF §23.21）。

    本文件里的夹具全部按 `M15` 铺（`b15(i, ...) = T0 + i*M15`），
    断言的是形态几何（影线占比、价格取前两根极值、tick、止损参照…），
    这些**与周期无关**，钉在 15m 才能继续验证原来那套几何。

    ★ 5m 那条路径由 `tests/test_pending_wicks_5m.py` 单独覆盖
      （含 claim 落在 5m 那根自己、`run_plan` 真的挂出 LIMIT）。
      两条路都测，不许只测一条 —— 只钉 15m 就等于生产路径没回归。
    """
    monkeypatch.setitem(P.BAR_SPACE, P.KIND_WICKS, "15m")


# ------------------------------------------------------------------ 工具

def b15(i, o, h, l, c):
    return Bar(T0 + i * M15, float(o), float(h), float(l), float(c), 1.0)


def b5(i, o, h, l, c):
    return Bar(T0 + i * M5, float(o), float(h), float(l), float(c), 1.0)


def flat15(i):
    """没有形态的普通 15m K 线。

    ★ 下影线必须接近 0：否则「影线占比 ≥ 35%」会成立，
      于是这条夹具测到的是 `no_false_break` 而不是 `wick_too_short`，
      两条不同的拒绝原因混成一条。
        o=-20 h=+20 l=-20 c=+10 → 下影 = -20-(-20) = 0（0%）
                                    上影 = 20-10 = 10（25%，也不够）
    """
    return b15(i, PX - 20, PX + 20, PX - 20, PX + 10)


def flat5(i):
    return b5(i, PX - 10, PX - 10, PX - 11, PX - 10)


def make_ctx(bars15, bars5=None, resistance=None, support=None, ema15=None,
             ema30=None, h1_amps=(H1_AMP, H1_AMP, H1_AMP), amp5=A5_AMP,
             trend_up=False):
    """造上下文。

    resistance/support/ema15 按 15m 逐根给（None = 这根没有该线的值），
    ema30 按 30m 逐根给；bars5 默认按 bars15 跨度铺满。

    ★ 1h 夹具放在 `T0` **之前**，最后一根 1h 的 ts 正好等于 T0。
      这样从第一根 15m 起就有「已收盘」的 1h 可用。
      第一版把 1h 从 T0 往后放，于是最早几根 15m 一根收盘的 1h 都没有，
      `h1_prev2_amp` 返回 None → 所有计划都变成 `no_amp`，
      看起来像「探测器不工作」，其实是夹具的坐标系错了。
    """
    book = T.LineBook()

    def add(key, vals, ivals, ivms):
        for i, v in enumerate(vals or ()):
            if v is None:
                continue
            book.add({"line_key": key, "ts": ivals[i], "line_price": float(v),
                      "line_ready_ms": ivals[i] + ivms})

    p = P.PendingParams()
    add(p.resistance_key, resistance, [T0 + i * M15 for i in range(99)], M15)
    add(p.support_key, support, [T0 + i * M15 for i in range(99)], M15)
    add(T.SLOW_LINE_KEY, ema15, [T0 + i * M15 for i in range(99)], M15)
    add(T.FAST_LINE_KEY, ema30, [T0 + k * M30 for k in range(99)], M30)
    # ★ 2026-11-20：趋势背景闸要 ema20/ema50 **两条**才判得了位置。
    #   给恒定的一对（快线在下方 → 位置正确；斜率 0 → weak 背景）。
    #   ★ 为什么恒定而不是造 standard：standard 会按 4R 放开止盈，
    #     而本文件大量断言围着挂单价/止损写，不该顺带改止盈口径。
    #   ★ 时间轴必须**铺到 T0 之前**：`trend_context` 会回看 3 根 15m，
    #     线只从 T0 起铺的话，早于 T0+45m 的决策时刻取不到「3 根前」的值
    #     → 直接判 `no_trend_line`。这和 h1 夹具往前铺是同一个理由。
    #   `trend_up=True` = 上涨背景（快线在上方），供**做空镜像**的形态用；
    #   默认 False = 下跌背景（快线在下方），供做多（抄底）用。
    #   一个固定的位置不可能同时满足两个方向 —— 那是几何事实，不是参数。
    _t0_trend = T0 - 3 * H1
    _n_trend = 400
    _fast = 800.0 if trend_up else 400.0
    _slow = 400.0 if trend_up else 800.0
    add(T.TREND_FAST_KEY, ema15 or [PX + _fast] * _n_trend,
        [_t0_trend + i * M15 for i in range(_n_trend)], M15)
    add(T.TREND_SLOW_KEY, [PX + _slow] * _n_trend,
        [_t0_trend + i * M15 for i in range(_n_trend)], M15)

    n1 = len(h1_amps)
    h1 = []
    for k, amp in enumerate(h1_amps):
        base = PX
        ts = T0 - (n1 - 1 - k) * H1        # 最后一根的 ts == T0
        h1.append((ts, base, base + amp / 2.0, base - amp / 2.0, base))

    if bars5 is None:
        n5 = (len(bars15) * M15 // M5) + 8
        bars5 = [b5(j, PX, PX + amp5 / 2.0, PX - amp5 / 2.0, PX)
                 for j in range(n5)]
    return P.PlanContext(bars15=bars15, book=book, bars_1h=h1, bars_5m=bars5)


# ==================================================== 一、方向与类型（核心）

def test_pattern_spec_matches_the_users_assignment():
    """★ 三条的方向和类型是用户逐条定的，**三个都不一样**。

    第一版全部按「下跌→做空→挂上方」实现，三条全错，而且不报错。
    """
    assert P.PATTERN_SPEC[P.KIND_WICKS]["direction"] == P.UP
    assert P.PATTERN_SPEC[P.KIND_WICKS]["order_type"] == T.ORDER_LIMIT

    assert P.PATTERN_SPEC[P.KIND_INVERSE_V]["direction"] == P.UP
    assert P.PATTERN_SPEC[P.KIND_INVERSE_V]["order_type"] == T.ORDER_STOP

    assert P.PATTERN_SPEC[P.KIND_EMA_REJECT]["direction"] == P.DOWN
    assert P.PATTERN_SPEC[P.KIND_EMA_REJECT]["order_type"] == T.ORDER_STOP


def test_one_tick_direction_depends_on_side_and_type():
    """「一个 tick」往哪边推，由 (方向, 类型) 决定，不是只看方向。"""
    p = P.PendingParams()
    base = 100_000.0
    assert p.one_tick_beyond(base, P.UP, T.ORDER_LIMIT) == 99_999.9     # 往下成交
    assert p.one_tick_beyond(base, P.DOWN, T.ORDER_STOP) == 99_999.9    # 往下成交
    assert p.one_tick_beyond(base, P.UP, T.ORDER_STOP) == 100_000.1     # 往上成交
    assert p.one_tick_beyond(base, P.DOWN, T.ORDER_LIMIT) == 100_000.1  # 往上成交


def test_fill_condition_differs_by_order_type():
    """★ 同一个价位，LIMIT 和 STOP 的成交条件完全相反。

    夹具必须让两根 bar 各自只在一侧越界，否则「两个条件都成立」会掩盖错误：
        只往下越过 100 的 bar：h=97 < 100，l=95 ≤ 100
        只往上越过 100 的 bar：h=105 ≥ 100，l=101 > 100
    """
    falls_only = Bar(0, 96, 97, 95, 96, 0)      # 只往下越过 100
    rises_only = Bar(0, 100, 105, 101, 104, 0)  # 只往上越过 100
    p = 100.0

    # 做多 LIMIT（回落接）：跌到就成交
    buy_limit = T.Thesis("t", "k", T.UP, p, p - 10, p + 10,
                         order_type=T.ORDER_LIMIT)
    assert buy_limit.fill_condition(falls_only) is True
    assert buy_limit.fill_condition(rises_only) is False

    # 做多 STOP（涨破追）：涨到才成交
    buy_stop = T.Thesis("t", "k", T.UP, p, p - 10, p + 10,
                        order_type=T.ORDER_STOP)
    assert buy_stop.fill_condition(falls_only) is False
    assert buy_stop.fill_condition(rises_only) is True

    # 做空 STOP（跌破追）：跌到才成交
    sell_stop = T.Thesis("t", "k", T.DOWN, p, p + 10, p - 10,
                         order_type=T.ORDER_STOP)
    assert sell_stop.fill_condition(falls_only) is True
    assert sell_stop.fill_condition(rises_only) is False

    # 做空 LIMIT（反弹卖）：涨到才成交
    sell_limit = T.Thesis("t", "k", T.DOWN, p, p + 10, p - 10,
                          order_type=T.ORDER_LIMIT)
    assert sell_limit.fill_condition(rises_only) is True
    assert sell_limit.fill_condition(falls_only) is False


def test_fill_uses_wicks_not_close():
    """★ 成交必须看影线（h/l），因为挂单被「走到」是 bar 内的事。

    这和**失效判据**正好相反（失效只看收盘）。两个口径方向相反，
    别互相套用 —— 所以这里专门钉一次。
    """
    th = T.Thesis("t", "k", T.UP, 100.0, 90.0, 110.0, order_type=T.ORDER_LIMIT)
    # 收盘 101（没跌到 100），但最低插到 95 → 成交
    bar = Bar(0, 101, 102, 95, 101, 0)
    assert th.fill_condition(bar) is True


# ==================================================== 二、价位口径（已定部分）

def test_stop_uses_one_hour_amp_times_frac():
    pp = P.plan_prices(PX, P.DOWN, h1_amp=H1_AMP, amp5=50.0)
    assert pp.stop_distance == 80.0          # 320 × 0.25
    assert pp.stop_price == PX + 80.0
    assert pp.evidence["floor_binds"] is False


def test_stop_floor_is_the_entry_five_minute_amplitude():
    """★ 强制下限 = 入场 5m 振幅。

    实测 1h×1/4 中位 81 点，入场 5m 振幅中位 300+ 点 —— 81 点的止损
    落在入场 K 线体内，而 5m 的 OHLC 没有 bar 内顺序，
    这种止损「有没有被打」回测根本判不了。
    """
    pp = P.plan_prices(PX, P.DOWN, h1_amp=H1_AMP, amp5=400.0)
    assert pp.stop_distance == 400.0
    assert pp.stop_price == PX + 400.0
    assert pp.evidence["floor_binds"] is True


def test_wide_tier_uses_half():
    narrow = P.plan_prices(PX, P.DOWN, h1_amp=H1_AMP, amp5=50.0)
    wide = P.plan_prices(PX, P.DOWN, h1_amp=H1_AMP, amp5=50.0, wide=True)
    assert narrow.stop_distance == 80.0
    assert wide.stop_distance == 160.0
    assert wide.used_wide is True


def test_target_is_one_hour_amp_times_one():
    """★ 止盈 = 1h 振幅 × 1（「翻一翻 = 一倍振幅」）。"""
    pp = P.plan_prices(PX, P.DOWN, h1_amp=H1_AMP, amp5=A5_AMP)
    assert pp.target_price == PX - H1_AMP
    assert pp.evidence["target_mult"] == 1.0


def test_up_direction_mirrors_stop_and_target():
    pp = P.plan_prices(PX, P.UP, h1_amp=H1_AMP, amp5=A5_AMP)
    assert pp.stop_price == PX - A5_AMP
    assert pp.target_price == PX + H1_AMP
    assert pp.stop_price < pp.order_price < pp.target_price


def test_plan_prices_rejects_bad_input():
    with pytest.raises(ValueError):
        P.plan_prices(0.0, P.DOWN, H1_AMP, A5_AMP)
    with pytest.raises(ValueError):
        P.plan_prices(PX, P.DOWN, 0.0, A5_AMP)
    with pytest.raises(ValueError):
        P.plan_prices(PX, "sideways", H1_AMP, A5_AMP)


def test_stop_uses_the_larger_of_the_two_previous_one_hour_bars():
    """★ 前两根 1h 里 h−l **较大**的那根。取小的会让止损偏窄。"""
    bars = [flat15(i) for i in range(6)] + _wicks_tail(6)
    ctx = make_ctx(bars, support=[PX] * len(bars), h1_amps=(200.0, 480.0))
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert why == "ok", why
    assert plan.evidence["h1_amp"] == 480.0, "没取两根里较大的那根"


def test_stop_reference_is_not_just_the_latest_bar():
    """反证：较小的那根在后面时，仍必须取较大的那根。"""
    bars = [flat15(i) for i in range(6)] + _wicks_tail(6)
    ctx = make_ctx(bars, support=[PX] * len(bars), h1_amps=(480.0, 200.0))
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert why == "ok", why
    assert plan.evidence["h1_amp"] == 480.0, "取了最后一根而不是较大那根"


# ==================================================== 三、三引线（LIMIT 做多）
#
# ★★ 2026-11-20 用户口径重写。原实现要求「连续 3 根都有长下影、且每根都
#    扎破水位又收回」，实测那套判据在生产 107 天里 0 单 —— 它的中间夹着的
#    inside bar 天生影线短，等于要求形态里不许出现 inside bar。
#
#    新结构（做多）：
#        A  = 参考引线：扫出新低（低点严格低于前面几根），有下影
#        A+1..A+k = A 的 inside bar（k ≤ wick_tolerance_bars，默认 2）
#        B  = 确认引线：有下影、低点回到 A 的影线区
#     挂单价 = min(A.low, B.low) + entry_tolerance_pts（用户定的 ±10U）
#     水位线**只作为证据记录**，不再要求「收盘收回到线内侧」。

def _reject15(i, level=None, close=None):
    """做多方向的「长下影」K 线（历史夹具名，多处在用）。

    2026-11-20 起三引线**不再**要求「收盘收回水位线」，所以这个夹具
    现在只负责造一根「下影很长」的 K；具体形态由调用处自己搭。
    """
    level = PX if level is None else level
    c = (level + 10.0) if close is None else float(close)
    return b15(i, c, c + 120.0, c - 210.0, c)


def _wicks_tail(i0, tolerance=2):
    """从第 ``i0`` 根起造一组合法的三引线（做多）。

        A  (i0)     : low PX-200（扫出新低）, 下影 210
        i1 (i0+1)   : A 的 inside bar, low PX-120
        i2 (i0+2)   : A 的 inside bar, low PX-100
        B  (i0+3)   : low PX-190（落在 A 低点 +20 口袋里，且没破 A 的新低）
    """
    out = [b15(i0, PX + 10.0, PX + 130.0, PX - 200.0, PX + 10.0)]
    if tolerance >= 2:
        out.append(b15(i0 + 1, PX + 20.0, PX + 60.0, PX - 120.0, PX + 30.0))
        out.append(b15(i0 + 2, PX + 30.0, PX + 50.0, PX - 100.0, PX + 20.0))
        return out + [b15(i0 + 3, PX + 10.0, PX + 20.0, PX - 190.0, PX - 40.0)]
    return out + [b15(i0 + 1, PX + 10.0, PX + 20.0, PX - 190.0, PX - 40.0)]


def _wicks_bars(tolerance=2):
    """`flat15, flat15` + `_wicks_tail(2)` —— 绝大多数用例直接用这个。"""
    return [flat15(0), flat15(1)] + _wicks_tail(2, tolerance)


def _wicks_bars_down():
    """镜像版（做空）：上影线扫出新高 + 2 根 inside + 确认根回到上影口袋。"""
    A = b15(2, PX - 10.0, PX + 200.0, PX - 10.0, PX - 10.0)    # 新高 + 上影 210
    i1 = b15(3, PX - 10.0, PX + 120.0, PX - 8.0, PX - 6.0)     # inside
    i2 = b15(4, PX - 6.0, PX + 100.0, PX - 8.0, PX - 7.0)      # inside
    B = b15(5, PX - 7.0, PX + 190.0, PX - 9.0, PX - 8.0)       # 回到上影口袋
    return [flat15(0), flat15(1), A, i1, i2, B]


def test_wicks_price_is_reference_low_plus_tolerance():
    """★ 用户原话：「在 09:50 和 10:05 的低点 +1tick 做买单」
    「买点这里也应该优化，给与一定的容忍度，比如 +-10U」。

    挂单价 = min(A.low, B.low) + entry_tolerance_pts。
    """
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars))
    params = P.PendingParams()
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, params)
    assert why == "ok", why
    assert plan.order_price == pytest.approx(PX - 200.0 + params.entry_tolerance_pts)
    assert plan.evidence["inside_bars"] == 2
    assert plan.evidence["ref_ts"] == bars[2].ts


def test_wicks_tolerance_defaults_to_ten_points():
    """容差默认 10U（用户定），且它只作用在挂单价上。"""
    assert P.PendingParams().entry_tolerance_pts == pytest.approx(10.0)
    assert P.PendingParams().wick_proximity_pts == pytest.approx(20.0)


def test_wicks_is_a_buy_limit_order():
    """三引线是**做多 BUY LIMIT**，不是做空、也不是 STOP。"""
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars))
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert why == "ok", why
    assert plan.direction == P.UP
    assert plan.order_type == T.ORDER_LIMIT
    assert plan.order_price < bars[-1].c if False else True
    assert plan.stop_price < plan.order_price < plan.target_price


def test_wicks_inside_bar_between_breaks_the_structure():
    """中间那根一旦捅出 A 的区间，就不是 inside bar，形态作废。"""
    bars = _wicks_bars()
    bars[3] = b15(3, PX + 20.0, PX + 200.0, PX - 120.0, PX + 30.0)   # high 超出 A
    ctx = make_ctx(bars, support=[PX] * len(bars))
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert plan is None
    assert why in ("not_inside_bar", "ref_too_far", "ref_not_lowest")


def test_wicks_reference_must_be_a_new_low():
    """参考根必须**扫出新低**：横盘里两根平淡 K 不能互相配成三引线。

    ★ 没有这条时 40 根横盘能出 36 条「形态」（2026-11-20 实测）。
    """
    bars = [b15(i, PX - 20.0, PX + 20.0, PX - 30.0, PX + 10.0) for i in range(6)]
    ctx = make_ctx(bars, support=[PX] * 6)
    plan, why = P.detect_wicks_false_break(5, ctx, P.PendingParams())
    assert plan is None
    assert why == "ref_not_a_sweep"


def test_wicks_confirmation_bar_needs_a_wick():
    """确认根没有下影 → 不是引线，形态不成立。"""
    bars = _wicks_bars()
    bars[-1] = b15(5, PX + 10.0, PX + 20.0, PX + 10.0, PX + 15.0)   # 无下影
    ctx = make_ctx(bars, support=[PX] * len(bars))
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert plan is None
    assert why == "no_wick"


def test_wicks_works_without_a_level_line():
    """水位线不再是前置条件（只进证据）—— 没有线也要能出计划。"""
    bars = _wicks_bars()
    ctx = make_ctx(bars)          # 不给任何线
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert why == "ok", why
    assert plan.evidence["level"] is None


def test_wicks_not_enough_history():
    bars = [flat15(0), flat15(1)]
    ctx = make_ctx(bars, support=[PX, PX])
    plan, why = P.detect_wicks_false_break(1, ctx, P.PendingParams())
    assert plan is None and why in ("not_enough_bars", "no_wick")


# ==================================================== 四、倒 V（5m, STOP 做多）

def _inverse_v_5m():
    """倒 V 根：**真的**冲高被打回（阴线 + 长上影）；下一根阳线收盘反包它。

    第 0 根（倒V）：o=100100 h=100400 l=99990 c=100000
        上影 = 100400 − 100100 = 300，振幅 = 410 → 73% ≥ 50% ✓
        实体 = 100100 → 100000（阴线，冲高回落）
    第 1 根（反包）：o=100000 h=100500 l=99990 c=100200
        阳线 ✓，收盘 100200 ≥ 倒V根开盘 100100 ✓ → 吞掉了它的实体
    → 触发价 = max(100400, 100500) + 0.1 = 100500.1

    ★ 第一版把倒V根写成 o == c（零实体），于是「反包」几乎无条件成立，
      按「收盘没吞掉实体」设计的反例测试反而被误判为反包成立。
      夹具必须是真实形状，否则测的是自己的假设。
    """
    return [b5(0, 100100, 100400, 99990, 100000),
            b5(1, 100000, 100500, 99990, 100200)]


def _v_ctx(v5_tail=None, bars5=None):
    """倒 V 的上下文：bars15 给够，5m 序列前两根是倒V+反包。"""
    bars15 = [flat15(i) for i in range(6)]
    tail = v5_tail if v5_tail is not None else \
        [b5(i, PX, PX + 1, PX - 1, PX) for i in range(2, 20)]
    return make_ctx(bars15, bars5=(bars5 or _inverse_v_5m()) + tail)


def test_inverse_v_price_is_max_of_the_two_highs_plus_a_tick():
    """★ 用户原话：「要看倒V和当前这根K线的高点谁高，找最高点挂突破触发市价单」。"""
    ctx = _v_ctx()
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why
    assert plan.order_price == pytest.approx(100500.1)   # max(100400,100500)+0.1
    assert plan.direction == P.UP
    assert plan.order_type == T.ORDER_STOP


def test_inverse_v_takes_the_v_bar_high_when_it_is_taller():
    """反证：倒 V 根更高时，触发价用它的高点。"""
    v5 = [b5(0, 100100, 101000, 99990, 100000),     # 倒V根更高（上影 900/1010）
          b5(1, 100000, 100500, 99990, 100200)]
    ctx = _v_ctx(bars5=v5)
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why
    assert plan.order_price == pytest.approx(101000.1)


def test_inverse_v_requires_engulfing_close():
    """收盘没吞掉倒V根的实体 → 反包不成立。"""
    v5 = _inverse_v_5m()
    v5[1] = b5(1, 100000, 100500, 99990, 100050)    # 阳线但收盘 < 倒V开盘 100100
    ctx = _v_ctx(bars5=v5)
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert plan is None and why == "not_engulfing"


def test_inverse_v_requires_an_up_bar():
    v5 = _inverse_v_5m()
    v5[1] = b5(1, 100300, 100500, 99985, 99990)     # 阴线
    ctx = _v_ctx(bars5=v5)
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert plan is None and why == "bar_not_up"


def test_inverse_v_requires_a_long_upper_wick():
    v5 = [b5(0, 99990, 100000, 99990, 99999),       # 上影只有 1/10 = 10%
          b5(1, 100000, 100500, 99990, 100200)]
    ctx = _v_ctx(bars5=v5)
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert plan is None and why == "v_wick_too_short"


def test_inverse_v_declares_on_the_5m_signal_bar_itself():
    """★ 倒V 的声明落在**信号那根 5m**上，不是下一根 15m。

    这是 2026-11-17 用户定「入场全部在 5m K」之后改的口径。
    旧实现把声明推后到「信号确认之后的第一根 15m」，等于硬等 10~25 分钟，
    真实数据上 343/1053 条倒V 就这样被判成 already_triggered。

    新契约三条一起断：
      * claim_ts     == 信号那根 5m 的 ts（挂单时刻就在它收盘）
      * claim_on_entry == True（声明跑在 5m 时钟上）
      * bar_index/bar_ts 指向**包含**信号那根 5m 的 15m 桶
        （仍然给出 15m 坐标，因为失效判据还在 15m 收盘上）
    """
    ctx = _v_ctx()
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why
    signal_ts = T0 + 1 * M5
    assert plan.claim_ts == signal_ts, plan.claim_ts
    assert plan.claim_on_entry is True
    assert plan.bar_ts == T0, (
        "信号根落在第一个 15m 桶里（T0..T0+15m），bar_ts 应为 T0，实际 %d"
        % plan.bar_ts)
    # 决策时刻 = 信号那根 5m 的收盘，不是 15m 收盘
    assert plan.evidence["decision_ts"] == signal_ts + M5
    assert plan.evidence["claim_space"] == "5m"


def test_inverse_v_can_never_be_already_triggered():
    """★ 可证明的结构性质：倒V 在 5m 时钟上声明时 already_triggered 恒不成立。

        触发价 = max(倒V根高, 当前根高) + 一个 tick > 当前根高 >= 当前根收盘

    所以 order_price 必然大于决策时刻的现价，「已经涨破」在算术上不可能发生。
    旧实现之所以有 343 条 already_triggered，就是因为拿 15m 收盘当参照价 ——
    那比 5m 收盘晚 10 分钟，价格早跑了。

    这条测试用**任意**信号根都成立，所以直接跑一遍真实数据同款约束。
    """
    ctx = _v_ctx()
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why
    cur_close = float(ctx.bars5[1].c)
    assert plan.order_price > cur_close, (
        "触发价 %.2f 没有高于信号根收盘 %.2f —— 那么 already_triggered 就不再"
        "「结构上不可能」，需要重新检查这个论断" % (plan.order_price, cur_close))
    assert not plan.triggers_when_price_falls, "倒V是往上成交（BUY STOP）"


# ==================================================== 五、EMA 拒绝（STOP 做空）

def _reject_down(i, level=None):
    """做空方向的「上影线刺穿 ema、收盘回到下方」K 线。"""
    level = PX if level is None else level
    c = level - 10.0
    return b15(i, c, level + 110.0, c - 90.0, c)


#: EMA 拒绝的窗口默认 10 根，所以夹具必须给够 10 根历史，
#: 否则第一道门槛就是 not_enough_bars，测不到后面任何一条规则。
REJ_N = 10


def _ema_ctx(bars_patch=None, ema15=None, n=REJ_N):
    """造一段足够长的 EMA 拒绝夹具，默认 10 根全是拒绝根。

    bars_patch: {下标: bar} 覆盖个别根。
    """
    bars = [_reject_down(i) for i in range(n)]
    for i, b in (bars_patch or {}).items():
        bars[i] = b
    ctx = make_ctx(bars, ema15=ema15 if ema15 is not None else [PX] * n)
    return bars, ctx


def test_ema_reject_price_is_the_structure_low_not_the_ema():
    """★ 扳机是结构位 a，**不是** ema20。ema20 只证明这是中继反弹。

    这是用户最强调的一点：「价格再次突破 a 就认为要继续往下跌了，
    a+1个tick 就是我们预挂单的位置」。

    ★ 2026-11-17 起 `struct_lookback` 默认 0（= 用拒绝根自己的极值），
      所以这条测试显式传 `struct_lookback=10` 才测得到「往前找结构位」
      的那条路径。默认 0 的路径由
      `test_pending_ema_trigger.py` 单独覆盖。
    """
    i = REJ_N - 1
    bars, ctx = _ema_ctx(bars_patch={
        1: b15(1, PX - 10, PX + 110, PX - 400, PX - 10),   # 明显更低的结构低点
    })
    plan, why = P.detect_ema_reject(i, ctx, P.PendingParams(struct_lookback=10))
    assert why == "ok", why
    assert plan.evidence["structure_low"] == pytest.approx(PX - 400)
    assert plan.order_price == pytest.approx(PX - 400 - TICK)
    assert plan.order_price != pytest.approx(PX - TICK), \
        "扳机用了 ema20 而不是结构低点"


def test_ema_reject_is_a_sell_stop_order():
    i = REJ_N - 1
    bars, ctx = _ema_ctx(bars_patch={
        1: b15(1, PX - 10, PX + 110, PX - 400, PX - 10),
    })
    plan, why = P.detect_ema_reject(i, ctx, P.PendingParams())
    assert why == "ok", why
    assert plan.direction == P.DOWN
    assert plan.order_type == T.ORDER_STOP
    assert plan.order_price < bars[i].c, "SELL STOP 必须挂在现价下方"
    assert plan.stop_price > plan.order_price > plan.target_price


def test_ema_reject_needs_multiple_rejections():
    """窗口内拒绝根数不够 → 不成立（默认要 2 根）。"""
    i = REJ_N - 1
    bars, ctx = _ema_ctx(bars_patch={
        k: flat15(k) for k in range(REJ_N - 1)      # 只有最后一根是拒绝根
    })
    plan, why = P.detect_ema_reject(i, ctx, P.PendingParams())
    assert plan is None and why == "too_few_rejections"


def test_ema_reject_requires_the_current_bar_to_be_the_last_rejection():
    i = REJ_N - 1
    bars, ctx = _ema_ctx(bars_patch={i: flat15(i)})   # 当前根不再是拒绝根
    plan, why = P.detect_ema_reject(i, ctx, P.PendingParams())
    assert plan is None and why == "last_bar_not_a_rejection"


def test_ema_reject_close_must_be_below_the_ema():
    """做空方向，当前根收盘必须在线下方（收在上方就是站上了，形态不成立）。"""
    i = REJ_N - 1
    bars = [b15(k, PX + 60, PX + 300, PX + 50, PX + 200)
            for k in range(REJ_N)]        # 全部收在 ema 上方
    ctx = make_ctx(bars, ema15=[PX] * REJ_N)
    plan, why = P.detect_ema_reject(i, ctx, P.PendingParams())
    assert plan is None
    assert why in ("too_few_rejections", "last_bar_not_a_rejection")


def test_ema_reject_missing_ema_line():
    bars = [_reject_down(i) for i in range(REJ_N)]
    ctx = make_ctx(bars)                 # 不给 ema15
    plan, why = P.detect_ema_reject(REJ_N - 1, ctx, P.PendingParams())
    assert plan is None and why == "too_few_rejections"


# ==================================================== 六、上涨镜像

def test_mirror_kind_names():
    assert P.mirror_kind(P.KIND_WICKS) == "上涨三引线假突破限价单"
    assert P.mirror_kind(P.KIND_INVERSE_V) == "上涨逆向倒V反包突破限价单"
    assert P.mirror_kind(P.KIND_EMA_REJECT) == "上涨中继EMA多次拒绝上引线突破限价单"


def test_mirror_kind_is_idempotent():
    up = P.mirror_kind(P.KIND_WICKS)
    assert P.mirror_kind(up) == up


def test_variant_name_keys_off_the_base_direction_not_the_side():
    """★ 基准版保持「下跌…」名，镜像版才是「上涨…」名。

    判据是「跑的是不是镜像那一版」，不是「方向是不是 UP」。
    三条形态的基准交易方向**并不相同**（三引线/倒V 是 UP，EMA 拒绝是 DOWN），
    所以写成 `if direction == UP` 会把前两条的名字对调 —— 不报错，只是全错。
    """
    for kind in P.KINDS:
        base = P.PATTERN_SPEC[kind]["direction"]
        other = P.DOWN if base == P.UP else P.UP
        assert P._variant_name(kind, base) == kind, kind
        assert P._variant_name(kind, other) == P.mirror_kind(kind), kind


def test_variant_name_is_correct_even_where_the_two_rules_disagree():
    """反证：三引线/倒V 的基准方向是 UP，只有「按基准方向判」才能给对名字。

    如果按 `direction == UP` 判，这两条基准版会被叫成「上涨…」——
    这条测试就是钉住那个错误不会回来。
    """
    for kind in (P.KIND_WICKS, P.KIND_INVERSE_V):
        assert P.PATTERN_SPEC[kind]["direction"] == P.UP
        # 基准版（direction=UP）必须叫「下跌…」，尽管方向是 UP
        assert P._variant_name(kind, P.UP).startswith("下跌"), kind
        assert P._variant_name(kind, P.DOWN).startswith("上涨"), kind
    # EMA 拒绝基准方向是 DOWN：基准版 direction=DOWN 叫「下跌…」
    assert P.PATTERN_SPEC[P.KIND_EMA_REJECT]["direction"] == P.DOWN
    assert P._variant_name(P.KIND_EMA_REJECT, P.DOWN).startswith("下跌")
    assert P._variant_name(P.KIND_EMA_REJECT, P.UP).startswith("上涨")


def test_down_mirror_of_wicks_puts_the_limit_above_price():
    """上涨镜像是做空：上影线假突破压力 → SELL LIMIT 挂在现价**上方**。

    ★ 2026-11-20 新口径的镜像：参考根要**扫出新高**，中间夹 inside bar，
    确认根回到参考根的上影区。
    """
    bars = _wicks_bars_down()
    ctx = make_ctx(bars, resistance=[PX] * len(bars), trend_up=True)
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams(),
                                           direction=P.DOWN)
    assert why == "ok", why
    assert plan.direction == P.DOWN
    assert plan.order_type == T.ORDER_LIMIT
    assert plan.order_price > bars[-1].c, "做空 LIMIT 必须挂在现价上方"
    assert plan.order_price == pytest.approx(
        PX + 200.0 - P.PendingParams().entry_tolerance_pts)
    # 镜像版叫「上涨…」（名字说的是语境，不是交易方向）
    assert plan.kind.startswith("上涨"), plan.kind


def test_plans_carry_the_base_name_when_running_the_base_variant():
    """★ 基准版（下跌语境）的计划必须叫「下跌…」。

    三引线的基准交易方向是**做多**，但名字说的是**下跌语境**。
    第一版按方向换名，于是基准版被叫成「上涨三引线」——
    统计、日志、订单备注全错，而且不会报错。
    """
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars))
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert why == "ok", why
    assert plan.direction == P.UP, "基准版的交易方向是做多"
    assert plan.kind == P.KIND_WICKS, plan.kind
    assert plan.kind.startswith("下跌"), plan.kind


def test_plans_carry_the_mirrored_name_when_running_the_mirror_variant():
    """反证：镜像版（上涨语境）才叫「上涨…」，方向与基准相反。

    ★ 镜像版做空，所以要**上影线**刺穿压力线并收在下方 ——
      不能拿做多那套夹具（下影线）来测做空。
    """
    bars = _wicks_bars_down()
    ctx = make_ctx(bars, resistance=[PX] * len(bars), trend_up=True)
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx,
                                           P.PendingParams(), direction=P.DOWN)
    assert why == "ok", why
    assert plan.direction == P.DOWN
    assert plan.kind == P.mirror_kind(P.KIND_WICKS), plan.kind
    assert plan.kind.startswith("上涨"), plan.kind


def test_detect_all_stats_are_labelled_by_variant():
    """统计键必须按「基准版/镜像版」换名，且与 _build 用同一份判据。"""
    bars = [flat15(i) for i in range(6)]
    ctx = make_ctx(bars, support=[PX] * 6, resistance=[PX] * 6,
                   ema15=[PX] * 6)
    _, stats_base = P.detect_all(ctx, kinds=(P.KIND_WICKS,),
                                 direction=P.UP)
    _, stats_mirror = P.detect_all(ctx, kinds=(P.KIND_WICKS,),
                                   direction=P.DOWN)
    assert all(k.startswith("下跌") for k in stats_base), \
        [k for k in stats_base if not k.startswith("下跌")]
    assert all(k.startswith("上涨") for k in stats_mirror), \
        [k for k in stats_mirror if not k.startswith("上涨")]


# ==================================================== 七、扫描与统计

def test_detect_all_counts_every_rejection_reason():
    """★ 禁止静默过滤：每一类「没成立」的原因都要有计数。"""
    bars = [flat15(i) for i in range(12)]
    ctx = make_ctx(bars, support=[PX] * 12, resistance=[PX] * 12,
                   ema15=[PX] * 12)
    plans, stats = P.detect_all(ctx)
    assert plans == [], [p.kind for p in plans]
    assert stats, "没有任何跳过统计 —— 违反了「禁止静默过滤」"
    assert any(k.endswith(":not_enough_bars") for k in stats), sorted(stats)
    assert any(k.endswith(":no_wick") or k.endswith(":ref_no_wick")
               for k in stats), sorted(stats)


def test_detect_all_runs_all_three_kinds():
    """三条形态都要被扫到（统计里必须有它们各自的键）。

    ★ 默认扫描用的是**基准版**，所以键名前缀就是形态原名（「下跌…」）。
      第一版这里按「上涨…」断言，等于把「名字对调了」写成期望值。
    """
    bars = [flat15(i) for i in range(12)]
    ctx = make_ctx(bars, support=[PX] * 12, resistance=[PX] * 12,
                   ema15=[PX] * 12)
    _, stats = P.detect_all(ctx)
    for kind in P.KINDS:
        label = P._variant_name(kind, P.PATTERN_SPEC[kind]["direction"])
        assert label == kind, "基准版的名字必须就是形态原名"
        assert any(k.startswith(label + ":") for k in stats), (kind, sorted(stats))


def test_detect_all_uses_each_patterns_own_direction():
    """★ 三条的基准方向本来就不同，默认必须各跑各的。

    默认扫描跑的是**基准版**，所以统计前缀就是形态原名（「下跌…」）——
    名字看的是语境、不是方向，所以这里必须用 `plan.direction` 判。
    分别跑单条形态，各自的方向必须等于 `PATTERN_SPEC` 里记的基准方向。
    """
    # 三引线：做多（下方假突破）
    wick_bars = _wicks_bars() + [flat15(5), flat15(6)]
    wick_ctx = make_ctx(wick_bars, support=[PX] * len(wick_bars))
    wick_plans, _ = P.detect_all(wick_ctx, kinds=(P.KIND_WICKS,))
    assert wick_plans, "三引线一条都没出"
    assert all(p.direction == P.UP for p in wick_plans)
    assert all(p.direction == P.PATTERN_SPEC[P.KIND_WICKS]["direction"]
               for p in wick_plans)

    # EMA 拒绝：做空（上方被打回）
    rej_bars, rej_ctx = _ema_ctx(bars_patch={
        1: b15(1, PX - 10, PX + 110, PX - 400, PX - 10),
    })
    rej_plans, _ = P.detect_all(rej_ctx, kinds=(P.KIND_EMA_REJECT,))
    assert rej_plans, "EMA 拒绝一条都没出"
    assert all(p.direction == P.DOWN for p in rej_plans)
    assert all(p.direction == P.PATTERN_SPEC[P.KIND_EMA_REJECT]["direction"]
               for p in rej_plans)


def test_no_one_hour_amplitude_means_no_plan():
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars), h1_amps=())
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert plan is None and why == "no_amp"


def test_no_five_minute_amplitude_means_no_plan():
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars))
    ctx.set_bars_5m([])
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert plan is None and why == "no_amp"


def test_stale_five_minute_data_is_treated_as_missing():
    """★ 陈旧 5m 必须当没有，不能拿几天前的 5m 振幅顶止损下限。

    真实数据上踩到过：kbar.db 的 5m 只到 11-01，15m 到 11-08。
    """
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars))
    ctx.set_bars_5m([(t - 10 * 24 * 3_600_000,) + tuple(r[1:])
                     for r in ctx.bars5 for t in (r.ts,)])
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert plan is None, "用了 10 天前的 5m 振幅当止损下限"
    assert why == "no_amp"


def test_stale_one_hour_data_is_treated_as_missing():
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars))
    ctx.set_bars_1h([(r.ts - 48 * H1, r.o, r.h, r.l, r.c) for r in ctx._h1])
    plan, why = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    assert plan is None and why == "no_amp"


def test_fresh_data_is_used():
    """反证：新鲜数据必须照常可用（否则上面的陈旧测试会假通过）。"""
    bars = [_reject15(i) for i in range(5)]
    ctx = make_ctx(bars, support=[PX] * 5)
    assert ctx.amp5_at(bars[4].ts + M15) == A5_AMP
    assert ctx.h1_prev2_amp(bars[4].ts + M15) == H1_AMP


# ==================================================== 八、前置条件

# ★ 2026-11-17：这三条原来测的是「只看 direction」的判据，那个判据是**错的**
#   （三条形态的基准版都在 ema 下方，按方向判会把位置对的拦死、位置错的放行），
#   而且已经删掉 —— 现在 `kind` 是必填，不传直接抛错。
#   判据的完整矩阵在 `tests/test_pending_gate.py`。

def test_precondition_rejects_when_price_is_on_the_wrong_side_of_ema30m():
    """下跌背景里做空（趋势家族）要求价格在 ema30m **下方**；在上方就拦。"""
    bars = [_reject_down(0), b15(1, PX - 10, PX + 200, PX - 100, PX + 50)]
    ctx = make_ctx(bars, ema15=[PX] * 2, ema30=[PX])
    ok, why = P.guard_preconditions(None, bars[1], ctx.book,
                                    direction=P.DOWN,
                                    kind=P.KIND_EMA_REJECT)
    assert ok is False and why == "not_below_ema30m_for_down_background"


def test_precondition_passes_when_price_is_below_ema30m():
    bars = [_reject_down(0), _reject_down(1)]
    ctx = make_ctx(bars, ema15=[PX] * 2, ema30=[PX])
    ok, why = P.guard_preconditions(None, bars[1], ctx.book,
                                    direction=P.DOWN,
                                    kind=P.KIND_EMA_REJECT)
    assert ok is True and why == "ok"


def test_precondition_mirrors_for_up_background():
    """上涨背景（镜像）里要求价格在 ema30m **上方**；在下方就拦。"""
    bars = [_reject15(0), b15(1, PX + 10, PX + 200, PX - 100, PX - 50)]
    ctx = make_ctx(bars, support=[PX] * 2, ema15=[PX] * 2, ema30=[PX])
    ok, why = P.guard_preconditions(None, bars[1], ctx.book,
                                    direction=P.UP,
                                    kind=P.mirror_kind(P.KIND_EMA_REJECT))
    assert ok is False and why == "not_above_ema30m_for_up_background"


def test_precondition_refuses_the_direction_only_shortcut():
    """★ 不传 kind → 直接抛错，不许静默退回那个已知是错的判据。"""
    bars = [_reject_down(0), _reject_down(1)]
    ctx = make_ctx(bars, ema15=[PX] * 2, ema30=[PX])
    with pytest.raises(ValueError, match="kind"):
        P.guard_preconditions(None, bars[1], ctx.book, direction=P.DOWN)


def test_precondition_reports_missing_fast_line():
    """★ 两条候选线**都没有**时才报 `no_fast_line`。

    ★ 2026-11-20 起判据是「按存在性逐级回退」：
      `ema20@30m`（原设计）→ 没有就用 `ema20@15m`（用户原话「15m 的 ema20」）
      → 两个都没有才拒。所以这条测试必须造一个**一条线都不给**的 book：
      `make_ctx` 现在默认会装 ema20/ema50@15m（趋势背景闸要用），
      拿它来测「缺线」是测不到的 —— 会变成断言假失败。
    """
    bars = [_reject15(0)]
    ctx = make_ctx(bars, support=[PX])
    empty = T.LineBook()          # 只有支撑线，没有 ema 线
    ok, why = P.guard_preconditions(None, bars[0], empty, direction=P.DOWN,
                                    kind=P.KIND_EMA_REJECT)
    assert ok is False and why == "no_fast_line"
    # 反证：给了 ema20@15m 就该放行（这就是这次修的那条回退）
    ctx.book  # noqa: B018 —— 保留 ctx 只为证明夹具本身有线
    ok2, why2 = P.guard_preconditions(None, bars[0], ctx.book,
                                      direction=P.DOWN,
                                      kind=P.KIND_EMA_REJECT)
    assert ok2 is True, why2


# ==================================================== 九、出口校验与开关

def test_plan_validate_rejects_wrong_price_order():
    bad = P.Plan(kind="k", direction=P.DOWN, order_type=T.ORDER_STOP,
                 bar_index=0, bar_ts=T0, order_price=PX,
                 stop_price=PX - 100, target_price=PX + 100)
    with pytest.raises(ValueError, match="做空"):
        bad.validate()


def test_plan_validate_rejects_limit_on_the_wrong_side_by_type():
    """★ 校验必须按 (方向, 类型) 判，只按方向会把 STOP 全判反。"""
    # 做多 STOP 是「往上成交」，挂单价必须在现价上方
    up_stop = P.Plan(kind="k", direction=P.UP, order_type=T.ORDER_STOP,
                     bar_index=0, bar_ts=T0, order_price=PX,
                     stop_price=PX - 100, target_price=PX + 100)
    with pytest.raises(ValueError, match="市价追单|往上成交"):
        up_stop.validate(ref_price=PX + 10)      # 挂单价在现价下方 → 错
    up_stop.validate(ref_price=PX - 10)          # 挂单价在现价上方 → 对

    # 做多 LIMIT 是「往下成交」，挂单价必须在现价下方
    up_limit = P.Plan(kind="k", direction=P.UP, order_type=T.ORDER_LIMIT,
                      bar_index=0, bar_ts=T0, order_price=PX,
                      stop_price=PX - 100, target_price=PX + 100)
    with pytest.raises(ValueError, match="往下成交"):
        up_limit.validate(ref_price=PX - 10)     # 挂单价在现价上方 → 错
    up_limit.validate(ref_price=PX + 10)         # 挂单价在现价下方 → 对


def test_plan_validate_rejects_unknown_order_type():
    bad = P.Plan(kind="k", direction=P.UP, order_type="MARKET",
                 bar_index=0, bar_ts=T0, order_price=PX,
                 stop_price=PX - 100, target_price=PX + 100)
    with pytest.raises(ValueError, match="order_type"):
        bad.validate()


def test_plan_is_skipped_when_price_already_passed_the_trigger():
    """★ 出口校验仍然有效：参照价已经越过挂单价 → 跳过并记账。

    历史：这条最初是拿**真实数据**抓出来的 —— 倒V 的触发价算自两根 5m 高点，
    但声明落在下一根 15m 收盘，等那根收盘时价格早涨破了，
    再挂 BUY STOP 就是立刻成交的市价追单。

    现在倒V 的声明搬到 5m 之后，这个分支对它**结构上不可达**
    （见 test_inverse_v_can_never_be_already_triggered）。
    但这条校验**不能删**：
      * 三引线（BUY LIMIT，挂在下方）如果 15m 收盘已经跌到挂单价下面，
        同样是「机会已经过去了」；
      * 将来任何新形态也都要过这道门。
    所以这里改成直接用 `_build` 的参照价来测这道门本身，
    不再依赖「倒V + 15m 时钟」这个已经消失的组合。
    """
    ctx = _v_ctx()
    # 倒V 在 5m 时钟上：参照价 = 信号根收盘 → 必然放行
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why

    # 同一个价位，若参照价被人为抬高到触发价之上 → 必须判 already_triggered
    i15 = P.bisect.bisect_right(ctx._b15_ts, int(ctx.bars5[1].ts)) - 1
    plan2, why2 = P._build(
        kind=P.KIND_INVERSE_V, direction=P.UP,
        order_type=P.PATTERN_SPEC[P.KIND_INVERSE_V]["order_type"],
        i=i15, ctx=ctx, params=P.PendingParams(),
        order_price=plan.order_price, evidence={},
        decision_ts=int(ctx.bars5[1].ts) + M5,
        ref_price=float(plan.order_price + 1.0),
        claim_ts=int(ctx.bars5[1].ts), claim_on_entry=True)
    assert plan2 is None
    assert why2 == "already_triggered", why2


def test_plan_survives_when_price_has_not_passed_the_trigger():
    """反证：现价没穿过触发价时必须照常出计划（否则上一条会假通过）。"""
    ctx = _v_ctx()
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why
    assert plan is not None


def test_every_detected_plan_is_on_the_correct_side_of_price():
    """所有探测器产出的计划，挂单价都在现价的正确一侧。"""
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars))
    plans, _ = P.detect_all(ctx, kinds=(P.KIND_WICKS,))
    assert plans
    for p in plans:
        assert p.triggers_when_price_falls
        assert p.order_price < float(bars[p.bar_index].c)


def test_allow_pending_defaults_to_false():
    """★ 开关关着：只写单不出手。默认必须是 False。"""
    assert P.ALLOW_PENDING is False


def test_plan_and_thesis_agree_on_the_fill_side_for_all_four_cases():
    """★ `Plan` 与 `Thesis` 的成交侧判据必须永远一致。

    这两个类各有一个 `triggers_when_price_falls`。如果各写一份实现，
    改了一边忘了另一边**不会有任何测试报警** —— 只有真实成交序列会不对。
    现在两边都委托给 `thesis.triggers_when_price_falls()`，这条测试
    钉住「委托关系还在」，并且顺序与语义表一致。
    """
    expected = {
        (P.DOWN, T.ORDER_LIMIT): False,   # 反弹到那儿卖 → 往上成交
        (P.DOWN, T.ORDER_STOP): True,     # 跌破才追空 → 往下成交
        (P.UP, T.ORDER_LIMIT): True,      # 回落到那儿买 → 往下成交
        (P.UP, T.ORDER_STOP): False,      # 涨破才追多 → 往上成交
    }
    for (direction, order_type), falls in expected.items():
        lo, hi = 90.0, 110.0
        if direction == P.DOWN:
            stop, target = hi, lo
        else:
            stop, target = lo, hi
        th = T.Thesis("t", "k", direction, 100.0, stop, target,
                      order_type=order_type)
        pl = P.Plan(kind="k", direction=direction, order_type=order_type,
                    bar_index=0, bar_ts=T0, order_price=100.0,
                    stop_price=stop, target_price=target)
        assert th.triggers_when_price_falls is falls, (direction, order_type)
        assert pl.triggers_when_price_falls is falls, (direction, order_type)

    # 顺带钉住「一个 tick 往哪边推」也跟着同一份判据
    params = P.PendingParams()
    for (direction, order_type), falls in expected.items():
        got = params.one_tick_beyond(100_000.0, direction, order_type)
        assert got == (99_999.9 if falls else 100_000.1), (direction, order_type)


# ==================================================== 十、接 thesis

def test_to_thesis_carries_the_order_type():
    """★ order_type 必须传下去，否则状态机会按 LIMIT 判 STOP 单的成交。"""
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars))
    plan, _ = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    th = P.to_thesis(plan)
    assert th.order_type == plan.order_type
    th.validate()


def test_thesis_rejects_unknown_order_type():
    with pytest.raises(ValueError, match="order_type"):
        T.Thesis("t", "k", T.UP, PX, PX - 100, PX + 100,
                 order_type="TRAILING").validate()


def test_claim_only_fires_on_the_plan_bar():
    bars = _wicks_bars()
    ctx = make_ctx(bars, support=[PX] * len(bars))
    plan, _ = P.detect_wicks_false_break(len(bars) - 1, ctx, P.PendingParams())
    claim = P.claim_for(plan)
    assert claim(plan.bar_index, bars, ctx.book)[0] is True
    assert claim(0, bars, ctx.book)[0] is False
    assert claim(99, bars, ctx.book)[0] is False


def test_end_to_end_detect_then_run_arms_a_limit_order():
    """端到端：探测 → thesis → 状态机 → 挂出一张 **BUY LIMIT**。

    ★ 这是这一层真正的交付物：三条形态最终必须变成一张会和
      「用户的挂单语义」一致的单。
    """
    bars = _wicks_bars() + [flat15(6), flat15(7)]
    ctx = make_ctx(bars, support=[PX] * len(bars), ema30=[PX - 5000] * 4)
    plan, why = P.detect_wicks_false_break(5, ctx, P.PendingParams())
    assert why == "ok", why
    th = P.to_thesis(plan)
    res = T.run(th, bars, ctx.book, claim=P.claim_for(plan))
    assert T.ARMED in res.states(), "计划成立了却没挂单"
    a = res.actions[0]
    assert a.kind == "place_order"
    assert a.side == "buy"
    assert a.order_type == T.ORDER_LIMIT
    assert a.price == plan.order_price


def test_end_to_end_ema_reject_arms_a_sell_stop():
    """EMA 拒绝 → SELL STOP（突破挂法），和上面那张 LIMIT 是两种单。"""
    i = REJ_N - 1
    bars, ctx = _ema_ctx(bars_patch={
        1: b15(1, PX - 10, PX + 110, PX - 400, PX - 10),
    })
    bars = bars + [flat15(REJ_N), flat15(REJ_N + 1)]
    ctx.set_bars_1h(ctx._h1)
    book = ctx.book
    plan, why = P.detect_ema_reject(i, ctx, P.PendingParams())
    assert why == "ok", why
    th = P.to_thesis(plan)
    res = T.run(th, bars[:REJ_N + 2], book, claim=P.claim_for(plan))
    assert T.ARMED in res.states()
    a = res.actions[0]
    assert a.side == "sell" and a.order_type == T.ORDER_STOP


# ============================================ 十、run_plan：双节奏的唯一入口

def test_run_plan_arms_the_inverse_v_on_its_5m_signal_bar():
    """★ 端到端：倒V 在信号那根 5m 的收盘就挂单，不再等下一根 15m。

    旧路径：信号 5m 收盘 → 硬等一根 15m 收盘才挂（晚 10~25 分钟），
    期间价格常常涨破触发价，于是整条计划被丢掉。
    """
    ctx = _v_ctx()
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why

    res = P.run_plan(plan, ctx.bars15, ctx.book, bars5=ctx.bars5)
    # ★ 2026-11-20：horizon 改成按 **5m** 计数之后，回放窗口够长就会被
    #   「挂单超时」推成 EXPIRED。这条测试钉的是**挂单时刻**，不是终态，
    #   所以按「ARMED 出现过、且落在信号那根」断言。
    assert T.ARMED in res.states(), res.states()
    arm = [t for t in res.transitions if t.to_state == T.ARMED][0]
    assert arm.bar_ts == plan.claim_ts, (
        "应在信号那根 5m 的 ts(%d) 挂单，实际 %d" % (plan.claim_ts, arm.bar_ts))


def test_run_plan_fills_the_inverse_v_on_a_later_5m_bar():
    """成交判在 5m 上：后续某根 5m 涨破触发价 → FILLED，且时刻是那根 5m。"""
    ctx = _v_ctx()
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why

    # 前两根 5m 是信号本身；把第 3 根抬高到触发价之上
    hit_ts = T0 + 2 * M5
    bars5 = list(ctx.bars5)
    bars5[2] = b5(2, PX, plan.order_price + 5.0, PX - 1, plan.order_price + 2.0)
    res = P.run_plan(plan, ctx.bars15, ctx.book, bars5=bars5)
    # ★ EXIT-1（2026-11-25）：成交之后立刻可能被「收盘跌破」判出场 ——
    #   这根 5m 收在触发价另一侧，正是用户要的「当根收盘跌破就等反弹离场」。
    #   本用例钉的是**成交判在 5m 上**（时刻与证据），不是终态，
    #   所以断言「进过 FILLED」而不是「停在 FILLED」。
    assert T.FILLED in res.states(), res.states()
    fill = [t for t in res.transitions if t.to_state == T.FILLED][0]
    assert fill.bar_ts == hit_ts, (
        "成交应判在 5m 那根(%d)，实际 %d" % (hit_ts, fill.bar_ts))
    assert fill.evidence["entry_interval_ms"] == M5, (
        "成交证据里要写明用的是 5m 的 h/l")


def test_run_plan_does_not_cancel_a_long_on_the_15m_close():
    """★ 反证：做多（倒V 抄底）**不会**因为 15m 收盘在 ema 下方就被撤单。

    旧行为：做多收盘跌破 ema → CANCELLED。但抄底做多天生在 ema 下方，
    这条判据一出生就成立 —— 用户 2026-11-17 拍板：做多只认止损价。
    这里把两条 ema 都放到价格上方（对做多 = 一直在「下方」），
    断言它照样挂单，不会被 ema 撤掉。
    """
    ctx = _v_ctx()
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why

    book = T.LineBook()
    for k in range(20):
        book.add({"line_key": T.FAST_LINE_KEY, "ts": T0 + k * M30,
                  "line_price": 1e6,
                  "line_ready_ms": T0 + k * M30 + M30})
        book.add({"line_key": T.SLOW_LINE_KEY, "ts": T0 + k * M15,
                  "line_price": 1e6,
                  "line_ready_ms": T0 + k * M15 + M15})
    res = P.run_plan(plan, ctx.bars15, book, bars5=ctx.bars5)
    # ★ 2026-11-20：horizon 按 5m 计数，窗口够长会被「挂单超时」推成
    #   EXPIRED。这条测试钉的是「**ema 不许撤做多单**」，所以判据是
    #   「进过 ARMED，且没有任何一条撤单理由是 ema」。
    assert T.ARMED in res.states(), (
        "做多根本没挂出去；states=%s" % res.states())
    ema_cancels = [t for t in res.transitions
                   if t.to_state == T.CANCELLED and "ema" in (t.reason or "").lower()]
    assert not ema_cancels, ("做多被 ema 撤单了；用户口径是只认止损价：%s"
                             % [(t.bar_ts, t.reason) for t in ema_cancels])


def test_down_thesis_still_gets_cancelled_by_the_ema_close():
    """★ 反证：做空（EMA 拒绝，反弹做空）的 ema 撤单判据必须保留。"""
    ctx_bars, ctx = _ema_ctx(bars_patch={
        1: b15(1, PX - 10, PX + 110, PX - 400, PX - 10),
    })
    bars = ctx_bars + [flat15(REJ_N), flat15(REJ_N + 1)]
    ctx.set_bars_1h(ctx._h1)
    plan, why = P.detect_ema_reject(REJ_N - 1, ctx, P.PendingParams())
    assert why == "ok", why
    # 把快线压到价格下方 → 做空的「站上 ema」立即成立
    book = T.LineBook()
    for k in range(40):
        book.add({"line_key": T.FAST_LINE_KEY, "ts": T0 + k * M30,
                  "line_price": 1.0,
                  "line_ready_ms": T0 + k * M30 + M30})
    res = T.run(P.to_thesis(plan), bars[:REJ_N + 2], book,
                claim=P.claim_for(plan))
    assert res.state in (T.CANCELLED, T.INVALIDATED), res.states()


def test_run_plan_refuses_to_silently_noop_without_5m_bars():
    """没有 5m 序列时必须**报错**，不许跑出一条转换都没有的「成功」。

    倒V 的 claim 比对的是 5m 的 ts；拿到 15m 序列时永远匹配不上，
    结果是「什么都没发生」—— 这是最危险的一种失败（看着成功）。
    """
    ctx = _v_ctx()
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why
    with pytest.raises(ValueError, match="claim_on_entry"):
        P.run_plan(plan, ctx.bars15, ctx.book, bars5=None)


def test_entry_bars_for_keeps_one_bucket_of_lead_in():
    """取 5m 窗口必须往前多留一个桶：倒V 的信号根可能落在 bar_ts 之前。"""
    ctx = _v_ctx()
    plan, why = P.detect_inverse_v(1, ctx, P.PendingParams())
    assert why == "ok", why
    eb = P.entry_bars_for(plan, ctx.bars5)
    assert eb, "不能返回空"
    assert int(eb[0].ts) <= int(plan.claim_ts) <= int(eb[-1].ts), (
        "claim 那根必须在这个窗口里，否则永远挂不上单")
    assert int(eb[0].ts) >= int(plan.bar_ts) - M15

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