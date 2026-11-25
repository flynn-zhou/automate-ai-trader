# -*- coding: utf-8 -*-
"""第③道闸接线 · 持仓时间闸 · 预期衰减 · 成本模型（2026-11-17）。

这个文件对应全盘审查后的 P0 + P1 + 第9条。每条测试都盯一个**具体的
失败模式**，不是「跑一遍没崩」。

被保护的口径（用户逐条确认过）：
  · 第③道闸必须在 `_build` 的生产路径上，不能只靠调用方自觉。
  · 闸门查线必须用**这条计划的决策时刻**，不能一律 +15m（那是前视）。
  · `_build` 的决策时刻必须跟 **bar_space**（bar 自己那条序列），
    不能用 `space_for(kind)` —— 倒V 两者差 10 分钟。
  · `wick_bars` 必须真的接到定价上，不许形状看 3 根、定价写死最后 2 根。
  · 证据里的 rule 字符串必须由**实际参数**拼出来，不许写死。
  · 成本必须进模型；名义 R:R 不是结论。
  · 成交之后必须有持仓时间闸（以前没有，最长持了 20.2 小时）。
  · 反转家族挂单期必须有破功判据（以前恒 PENDING，永不撤单）。
  · 跑到数据末尾还没终结 → 必须显式标 unresolved，不许静默。
"""
from collections import namedtuple
from dataclasses import dataclass

import pytest

from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T

M15 = T.BAR_INTERVAL_MS
M30 = 1_800_000
M5 = 300_000
H1 = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % M30)

PX = 100_000.0
H1_AMP = 320.0
A5_AMP = 90.0

Bar = namedtuple("Bar", "ts o h l c v")


def b15(i, o, h, l, c, v=1.0):
    return Bar(T0 + i * M15, o, h, l, c, v)


def bars15_of(n, c=PX, spread=10.0):
    return [b15(i, c, c + spread, c - spread, c) for i in range(n)]


def book_of(slow=None, fast=None, n=400):
    """slow 按 15m 逐根、fast 按 30m 逐根。None = 这根没有值。"""
    b = T.LineBook()
    for i, p in enumerate(slow or ()):
        if p is None:
            continue
        b.add({"line_key": T.SLOW_LINE_KEY, "ts": T0 + i * M15,
               "line_price": float(p), "line_ready_ms": T0 + i * M15 + M15})
    for k, p in enumerate(fast or ()):
        if p is None:
            continue
        b.add({"line_key": T.FAST_LINE_KEY, "ts": T0 + k * M30,
               "line_price": float(p), "line_ready_ms": T0 + k * M30 + M30})
    return b


def make_ctx(bars, bars5=None, ema15=None, ema30=None,
             support=None, resistance=None,
             h1_amps=(H1_AMP,) * 3, amp5=A5_AMP):
    book = book_of(slow=ema15, fast=ema30)
    # ★ 2026-11-20：趋势背景闸要 ema20/ema50 两条（15m），三族都要。
    #
    #   ① 铺满时间轴（**早于 T0 也要有**）：15m 口径的形态回看 45 分钟，
    #      线只从 T0 起铺会让 `at(close_ms-45m)` 落空 → `no_trend_line`。
    #   ② 慢线**从快线推导**（+400），因为 `TREND_FAST_KEY` 和 `SLOW_LINE_KEY`
    #      是同一个键 `ema20@15m` —— 测试里用 `ema15=[1e6]*20` 把价格压到
    #      均线下方时，快线的值就是 1e6，此时固定的 ema50=PX+800 会变成
    #      「快线在上方」→ 位置判反。推导出来就永远一致。
    #   恒定 ⇒ 斜率 0 ⇒ weak 背景（不放开止盈，最中性）。
    _fast_vals = list(ema15) if ema15 else [PX + 400.0] * 20
    _n = max(len(_fast_vals), 20) + 60
    _start = T0 - 60 * M15
    for k in range(_n):
        base = _fast_vals[k % len(_fast_vals)]
        if base is None:
            continue
        ts = _start + k * M15
        book.add({"line_key": T.TREND_FAST_KEY, "ts": ts,
                  "line_price": float(base), "line_ready_ms": ts + M15})
        book.add({"line_key": T.TREND_SLOW_KEY, "ts": ts,
                  "line_price": float(base) + 400.0,
                  "line_ready_ms": ts + M15})
    p = P.PendingParams()
    for i, v in enumerate(support or ()):
        if v is None:
            continue
        book.add({"line_key": p.support_key, "ts": T0 + i * M15,
                  "line_price": float(v), "line_ready_ms": T0 + i * M15 + M15})
    for i, v in enumerate(resistance or ()):
        if v is None:
            continue
        book.add({"line_key": p.resistance_key, "ts": T0 + i * M15,
                  "line_price": float(v), "line_ready_ms": T0 + i * M15 + M15})
    n1 = len(h1_amps)
    h1 = []
    for k, amp in enumerate(h1_amps):
        ts = T0 - (n1 - 1 - k) * H1
        h1.append((ts, PX, PX + amp / 2.0, PX - amp / 2.0, PX))
    if bars5 is None:
        n5 = (len(bars) * M15 // M5) + 8
        bars5 = [Bar(T0 + j * M5, PX, PX + amp5 / 2.0, PX - amp5 / 2.0, PX, 1.0)
                 for j in range(n5)]
    del p
    return P.PlanContext(bars15=bars, book=book, bars_1h=h1, bars_5m=bars5)


# ============================================ 一、第③道闸必须在生产路径上

def test_build_runs_the_precondition_gate():
    """★ 背景不对 → `_build` 自己就该拒绝，不用调用方去补。

    这是审查里最要命的一条：闸原来只被 tools 调用，
    `detect_all` 出来的计划从没经过它 —— 「四道闸」实际只有三道在流水线上。
    """
    # 做空（EMA 拒绝），价格却站在 ema30m **上方** → 位置错
    bars = [b15(1, PX, PX + 20, PX - 20, PX + 200)]
    ctx = make_ctx(bars, ema15=[PX], ema30=[PX])
    plan, why = P._build(kind=P.KIND_EMA_REJECT, direction=P.DOWN,
                         order_type=T.ORDER_STOP, i=0, ctx=ctx,
                         params=P.PendingParams(), order_price=PX + 250.0,
                         evidence={}, bar=bars[0], bar_space="15m")
    assert plan is None
    assert why.startswith("precondition:"), why
    assert "not_below_ema30m" in why


def test_build_passes_when_the_background_is_right():
    """反证：位置对的时候必须放行，否则上面那条会被「永远拒绝」蒙混过去。"""
    bars = [b15(1, PX, PX + 20, PX - 20, PX - 200)]
    ctx = make_ctx(bars, ema15=[PX], ema30=[PX])
    plan, why = P._build(kind=P.KIND_EMA_REJECT, direction=P.DOWN,
                         order_type=T.ORDER_STOP, i=0, ctx=ctx,
                         params=P.PendingParams(), order_price=PX - 250.0,
                         evidence={}, bar=bars[0], bar_space="15m")
    assert why == "ok", why
    assert plan is not None


def test_the_gate_can_be_turned_off_for_shape_tests():
    """`apply_guard=False` 只给单元测试用；它必须真的关掉闸。"""
    bars = [b15(1, PX, PX + 20, PX - 20, PX + 200)]
    ctx = make_ctx(bars, ema15=[PX], ema30=[PX])
    plan, why = P._build(kind=P.KIND_EMA_REJECT, direction=P.DOWN,
                         order_type=T.ORDER_STOP, i=0, ctx=ctx,
                         params=P.PendingParams(apply_guard=False),
                         order_price=PX + 100.0,
                         evidence={}, bar=bars[0], bar_space="15m")
    assert why == "ok", why
    assert plan is not None


def test_gate_uses_the_plans_own_clock_not_a_hardcoded_15m():
    """★ 5m 计划的闸必须查**决策时刻**，不是决策后 10 分钟。

    构造：ema30m 在决策时刻 = 100000（价格在下方 → 通过），
    但在 +15m 那一刻已经跳到 99000（价格在上方 → 会被误拦）。
    用错时钟就会把一条合规计划拦掉 —— 这就是前视带来的真实伤害。
    """
    bars = [b15(0, PX, PX + 20, PX - 20, PX - 200)]      # 5m 那根在下面另造
    b5 = [Bar(T0 + j * M5, PX, PX + 20, PX - 20, PX - 200, 1.0)
          for j in range(20)]
    book = book_of(slow=[PX])
    # fast：第一格 100000（决策时刻可读），第二格 99000（+15m 后才可读）
    p = P.PendingParams()
    book.add({"line_key": T.FAST_LINE_KEY, "ts": T0 - M30,
              "line_price": PX, "line_ready_ms": T0})
    book.add({"line_key": T.FAST_LINE_KEY, "ts": T0,
              "line_price": PX - 1_000.0, "line_ready_ms": T0 + M30})
    ctx = P.PlanContext(bars15=bars, book=book,
                        bars_1h=[(T0 - H1, PX, PX + 160, PX - 160, PX)],
                        bars_5m=b5)
    # 正确时钟 = 5m 决策时刻 = T0 + M5（此时第二格还没就绪）
    ok, why = P.guard_preconditions(None, b5[0], book, direction=P.DOWN,
                                    kind=P.KIND_EMA_REJECT,
                                    close_ms=T0 + M5)
    assert ok is True, why
    # 错误时钟 = T0 + M15（第二格已就绪，价格在上方）→ 会被误拦
    ok2, why2 = P.guard_preconditions(None, b5[0], book, direction=P.DOWN,
                                      kind=P.KIND_EMA_REJECT,
                                      close_ms=T0 + M30)
    assert ok2 is False and "not_below_ema30m" in why2
    del p, ctx


def test_gate_default_clock_follows_bar_space():
    """没给 close_ms 时，默认按**这条计划的周期**算，不是一律 15m。"""

    class _FakePlan:
        bar_space = "5m"
        kind = P.KIND_EMA_REJECT

    b5 = [Bar(T0 + j * M5, PX, PX + 20, PX - 20, PX - 100, 1.0) for j in range(4)]
    book = book_of()
    book.add({"line_key": T.FAST_LINE_KEY, "ts": T0 - M30,
              "line_price": PX, "line_ready_ms": T0})
    book.add({"line_key": T.FAST_LINE_KEY, "ts": T0,
              "line_price": PX - 5_000.0, "line_ready_ms": T0 + M30})
    # 5m 决策时刻 = T0 + M5 → 拿到 PX（通过）
    ok, why = P.guard_preconditions(_FakePlan(), b5[0], book, direction=P.DOWN,
                                    kind=P.KIND_EMA_REJECT)
    assert ok is True, why
    # ★ 判别点：5m K 的 ts = T0+15m。
    #   正确时钟 = T0+20m → 第二条线（T0 起、ready T0+30m）**还没就绪** → 用 PX → 通过；
    #   写死 15m = T0+30m → 第二条线就绪（PX-5000）→ 收盘价在它上方 → 该被拒。
    #   两种时钟给出相反裁决，这条才抓得住「闸门写死 15m」。
    b5_late = [Bar(T0 + 3 * M5, PX, PX + 20, PX - 20, PX - 100, 1.0)]
    ok2, why2 = P.guard_preconditions(_FakePlan(), b5_late[0], book,
                                      direction=P.DOWN,
                                      kind=P.KIND_EMA_REJECT)
    # 正确时钟下必须**通过**。写死 15m 的变异体会在这里拿到 PX-5000
    # 并判成 not_below_ema30m —— 于是这条测试抓住它。
    assert ok2 is True, why2


# ================================================ 二、_build 的时钟跟 bar_space

def test_build_clock_uses_bar_space_not_the_kind_space():
    """★ 倒V：bar_space='15m'，而 space_for(kind)='5m' —— 差 10 分钟。

    不传 decision_ts 时，决策时刻必须是 `bar.ts + 15m`。
    用 kind 的周期算会早 10 分钟，amp5/h1_amp 全取错位置，而且不报错。
    """
    bars = bars15_of(20)
    ctx = make_ctx(bars, ema15=[PX - 500.0] * 20, ema30=[PX - 500.0] * 20)
    plan, why = P._build(kind=P.KIND_INVERSE_V, direction=P.UP,
                         order_type=T.ORDER_STOP, i=10, ctx=ctx,
                         params=P.PendingParams(apply_guard=False),
                         order_price=PX + 50.0, evidence={}, bar=None,
                         bar_space="15m")
    assert why == "ok", why
    assert plan.evidence["decision_ts"] == bars[10].ts + M15


def test_build_clock_for_5m_kind_is_five_minutes():
    """反证：5m 的计划仍然按 5m 算（别把上面那条修成一律 15m）。"""
    bars = bars15_of(20)
    b5 = [Bar(T0 + j * M5, PX, PX + 45, PX - 45, PX, 1.0) for j in range(120)]
    ctx = make_ctx(bars, bars5=b5, ema15=[PX - 500.0] * 20,
                   ema30=[PX - 500.0] * 20)
    plan, why = P._build(kind=P.KIND_WICKS, direction=P.UP,
                         order_type=T.ORDER_LIMIT, i=40, ctx=ctx,
                         params=P.PendingParams(apply_guard=False),
                         order_price=PX - 50.0, evidence={}, bar=None,
                         bar_space="5m")
    assert why == "ok", why
    assert plan.evidence["decision_ts"] == b5[40].ts + M5


def test_space_ms_refuses_unknown_periods():
    """不认识的周期名必须抛错，不许静默当 15m。"""
    with pytest.raises(ValueError, match="不认识"):
        P._space_ms("1m")


# ==================================================== 三、wick_bars 接到定价

def _false_break_series():
    """造一段 **5m** 的三引线（2026-11-20 新口径）。

    ★ 这里踩过一次：三引线的 `BAR_SPACE` 是 5m，`detect_wicks_false_break`
      读的是 `ctx.series_for(KIND_WICKS)` = **bars5**。夹具造在 bars15 上
      就会得到 `no_false_break` —— 形状一根都对不上。

    结构（第 8 根起）：A 深针扫出 PX-500 的新低 → 第 9/10 根是 A 的
    inside bar → 第 11 根（确认根）回到 A 低点 +10 的口袋里。
    """
    bars = []
    for i in range(12):
        ts = T0 + i * M5
        if i == 8:                                    # A：深针，扫出新低
            bars.append(Bar(ts, PX, PX + 10, PX - 500.0, PX + 5, 1.0))
        elif i == 9:                                  # inside
            bars.append(Bar(ts, PX + 5, PX + 9, PX - 6, PX + 8, 1.0))
        elif i == 10:                                 # inside
            bars.append(Bar(ts, PX + 8, PX + 10, PX - 8, PX + 10, 1.0))
        elif i == 11:                                 # B：回到口袋（+10）
            bars.append(Bar(ts, PX + 10, PX + 18, PX - 490.0, PX + 12, 1.0))
        else:
            bars.append(Bar(ts, PX, PX + 10, PX - 10, PX, 1.0))
    return bars


def _wick_ctx():
    """三引线的夹具：形状造在 5m 上，支撑线放在 PX。"""
    shape = _false_break_series()
    # ★ 2026-11-20：**不要**再传 `ema15`。趋势背景闸要在 ema20@15m 与
    #   ema50@15m 之间判位置，而这里原来把 ema15 塞成 1e6（为了让
    #   「价格在 ema 下方」那道闸通过）—— 那会让 ema20 远高于 ema50，
    #   判成「在尝试反转」→ 三引线一条都出不来。
    #   现在 guard 走 ema20@30m（`ema30`），trend 用 make_ctx 的默认一对。
    return make_ctx(bars15_of(20), bars5=shape,
                    ema30=[1e6] * 20, support=[PX] * 20)


def test_entry_tolerance_drives_the_limit_price():
    """★ 挂单价必须**由容差参数真的算出来**，不是写死的常数。

    旧口径是「前 n-1 根最低点 − 1tick」，那套已经被 2026-11-20 用户口径
    取代（买点 = 两根引线的低点 + 容差）。这里守住同一件事：
    **改参数 → 挂单价必须跟着变，而且差值等于参数差值。**
    """
    ctx = _wick_ctx()

    def price_for(tol):
        p, why = P.detect_wicks_false_break(
            11, ctx, P.PendingParams(entry_tolerance_pts=tol, apply_guard=False))
        assert p is not None, why
        return p.order_price

    p0 = price_for(0.0)
    p10 = price_for(10.0)
    p25 = price_for(25.0)
    assert abs(p0 - (PX - 500.0)) < 0.05, p0
    assert abs(p10 - (PX - 500.0 + 10.0)) < 0.05, p10
    assert abs(p25 - (PX - 500.0 + 25.0)) < 0.05, p25
    assert p0 < p10 < p25


def test_entry_price_is_the_reference_low_plus_tolerance():
    """定价只认**参考引线的低点** + 容差；确认根、inside bar 都不参与。"""
    ctx = _wick_ctx()
    p, why = P.detect_wicks_false_break(
        11, ctx, P.PendingParams(entry_tolerance_pts=10.0, apply_guard=False))
    assert p is not None, why
    assert abs(p.order_price - (PX - 500.0 + 10.0)) < 0.05, p.order_price
    # 证据必须能指认「参考的是哪一根、中间夹了几根」
    assert int(p.evidence["ref_ts"]) == int((T0 + 8 * M5))
    assert int(p.evidence["inside_bars"]) == 2


# ================================================ 四、证据里的 rule 由参数生成

def test_rule_string_reflects_actual_params():
    """改参数 → 证据里的 rule 必须跟着改。写死就是假证据。"""
    r1 = P.plan_prices(PX, P.DOWN, H1_AMP, A5_AMP, P.PendingParams())
    r2 = P.plan_prices(PX, P.DOWN, H1_AMP, A5_AMP,
                       P.PendingParams(stop_floor_mult=2.5))
    assert "2.50" in r2.evidence["rule"]
    assert "2.50" not in r1.evidence["rule"]
    assert "1.00" in r1.evidence["rule"]


# ======================================================= 五、成本模型（P1）

def test_cost_is_not_zero_and_scales_with_fees():
    pp = P.plan_prices(PX, P.DOWN, H1_AMP, A5_AMP, P.PendingParams(),
                       order_type=T.ORDER_LIMIT)
    assert pp.cost_win_points > 0 and pp.cost_loss_points > 0
    # 亏的那条路（taker 出场 + 滑点）一定比赢的那条（maker 出场）贵
    assert pp.cost_loss_points > pp.cost_win_points


def test_stop_entry_costs_more_than_limit_entry():
    """同价位同方向，只是进场费种不同 —— STOP 必须更贵。"""
    lim = P.plan_prices(PX, P.DOWN, H1_AMP, A5_AMP, P.PendingParams(),
                        order_type=T.ORDER_LIMIT)
    stp = P.plan_prices(PX, P.DOWN, H1_AMP, A5_AMP, P.PendingParams(),
                        order_type=T.ORDER_STOP)
    assert stp.cost_loss_points > lim.cost_loss_points


def test_breakeven_win_rate_matches_the_definition():
    """盈亏平衡胜率 = 净亏 / (净盈 + 净亏)。这个数才是几何能不能活的判据。"""
    pp = P.plan_prices(PX, P.DOWN, H1_AMP, A5_AMP, P.PendingParams(),
                       order_type=T.ORDER_STOP)
    want = pp.net_loss_r / (pp.net_win_r + pp.net_loss_r)
    assert abs(pp.breakeven_win_rate - round(want, 4)) < 1e-6
    # 有成本 → 一定比「无成本名义 RR 反推」高
    nominal = 1.0 / (1.0 + pp.gross_rr)
    assert pp.breakeven_win_rate > nominal


def test_fee_share_of_stop_is_reported():
    """手续费占止损距离多少 —— 审查里最刺眼的那个数，必须进证据。"""
    pp = P.plan_prices(PX, P.DOWN, H1_AMP, A5_AMP, P.PendingParams(),
                       order_type=T.ORDER_STOP)
    share = pp.cost_loss_points / pp.stop_distance
    assert abs(pp.evidence["fee_share_of_stop"] - round(share, 4)) < 1e-6
    assert share > 0.0


def test_cost_can_exceed_the_whole_stop_distance():
    """★ 成本可以比止损还大 —— 这不是 bug，是这个系统最该被看见的事实。

    10 万价位、永续 taker 双边 0.1% = 100 点；
    而 1h 振幅 320 × 0.25 = 80 点，被 5m 振幅下限抬到 90 点。
    于是**手续费一项就超过整个止损距离**。
    以前成本恒为 0，这个事实在模型里根本不存在。
    """
    pp = P.plan_prices(PX, P.DOWN, 320.0, 90.0, P.PendingParams(),
                       order_type=T.ORDER_STOP)
    assert pp.stop_distance == 90.0        # 被 amp5 下限抬起，不是 80
    assert pp.evidence["floor_binds"] is True
    assert pp.cost_loss_points > pp.stop_distance
    # 净收益被成本吃穿 → 名义 R:R 再漂亮也没用
    assert pp.net_win_r < pp.gross_rr


def test_wider_stop_reduces_the_fee_share():
    """反证：止损放宽 → 摩擦占比下降（这是成本模型的因果，不是巧合）。"""
    narrow = P.plan_prices(PX, P.DOWN, H1_AMP, A5_AMP, P.PendingParams(),
                           order_type=T.ORDER_STOP)
    wide = P.plan_prices(PX, P.DOWN, H1_AMP, A5_AMP,
                         P.PendingParams(stop_frac=0.5),
                         order_type=T.ORDER_STOP)
    assert wide.stop_distance > narrow.stop_distance
    assert wide.evidence["fee_share_of_stop"] < \
        narrow.evidence["fee_share_of_stop"]


def test_cost_lands_in_the_plan_evidence():
    ctx = _wick_ctx()
    p, why = P.detect_wicks_false_break(
        11, ctx, P.PendingParams(wick_bars=3, apply_guard=False))
    assert p is not None, why
    for k in ("cost_loss_points", "cost_loss_r", "net_win_r", "net_loss_r",
              "breakeven_win_rate"):
        assert k in p.evidence, k
        assert p.evidence[k] > 0, (k, p.evidence[k])


# ==================================================== 六、持仓时间闸（P0）

def _long_thesis(**kw):
    base = dict(thesis_id="t", kind="测试", direction=T.UP,
                order_price=100.0, stop_price=90.0, target_price=200.0,
                order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL)
    base.update(kw)
    return T.Thesis(**base)


def _flat_bars(n, c=100.0, h=None, l=None):
    return [Bar(T0 + i * M15, c, h if h is not None else c + 1.0,
                l if l is not None else c - 1.0, c, 1.0) for i in range(n)]


def test_there_is_no_holding_time_gate():
    """★ 2026-11-17 用户裁决：**不设持仓时间闸**，用预期与现实管理。

    原话：「持仓时长我还是坚持用预期与现实管理，我们做日内短线基本不可能
    持仓超过 20h，10h 都难，如果你会测发现持仓那么久肯定有问题，
    说明对现实的认知不够！」

    这条测试是**反向锁**：谁哪天又把时钟闸加回来，这里会先响。
    ★ 那笔 20.2 小时的根因已经量出来了 —— 行情先朝有利方向走了 2.53R，
      没到 4R 止盈就反转回止损。是**止盈太远**，不是时钟
      （`tools/_diag_target_reach.py`、`tools/_diag_hold_time.py`）。
    """
    assert "max_hold_bars" not in T.Thesis.__dataclass_fields__
    assert "max_hold_bars" not in P.PendingParams().__dataclass_fields__
    assert not hasattr(T, "CLOSED_TIME"), "超时平仓状态被加回来了"
    # 行为面：横盘 40 根也不许被时间平掉
    th = _long_thesis()
    res = T.run(th, _flat_bars(40), book_of(slow=[1e6] * 40), start_at=0)
    assert res.state == T.FILLED, res.state


# ============================================== 七、预期衰减（第9条 · 反转）

def _stale_case(direction=T.UP, order_type=T.ORDER_LIMIT,
                order=95.0, stop=90.0, target=200.0, stale=5.0,
                closes=(100.0, 101.0, 103.0, 106.0, 107.0, 108.0)):
    """挂单后价格朝**远离触发价**的方向走。做多 LIMIT 在下方 → 涨价是远离。"""
    th = T.Thesis(thesis_id="s", kind="测试", direction=direction,
                  order_price=order, stop_price=stop, target_price=target,
                  order_type=order_type, family=T.FAMILY_REVERSAL,
                  stale_distance=stale, horizon_bars=0)
    bars = [Bar(T0 + i * M15, c, c + 0.5, c - 0.5, c, 1.0)
            for i, c in enumerate(closes)]
    return th, bars


def test_reversal_order_is_cancelled_when_price_runs_away():
    """★ 反转挂单期以前**恒 PENDING**，挂着的几小时里怎么走都不撤。"""
    th, bars = _stale_case()
    res = T.run(th, bars, book_of(slow=[1e6] * len(bars)), start_at=0)
    assert res.state == T.CANCELLED, res.state
    assert res.transitions[-1].evidence["cancel_reason"] == "thesis_stale"
    ev = res.transitions[-1].evidence
    assert ev["adverse"] >= 5.0, ev


def test_reversal_order_survives_when_price_comes_to_it():
    """反证：价格朝触发价靠拢时**不许**撤 —— 否则等于把所有反转都撤光。"""
    th, bars = _stale_case(closes=(100.0, 99.0, 98.0, 97.0, 96.0, 95.5))
    res = T.run(th, bars, book_of(slow=[1e6] * len(bars)), start_at=0)
    assert res.state != T.CANCELLED
    assert res.state == T.FILLED


def test_stale_distance_zero_disables_the_criterion():
    """0 = 不看。默认必须是 0（不给就照旧），避免意外改变口径。"""
    th, bars = _stale_case(stale=0.0)
    res = T.run(th, bars, book_of(slow=[1e6] * len(bars)), start_at=0)
    assert res.state != T.CANCELLED


def test_trend_family_does_not_get_a_stale_distance():
    """★ 只给反转：趋势已经由 ema 撤单，再加一条就是两个来源争着撤。"""
    ctx = _wick_ctx()
    p_rev, why = P.detect_wicks_false_break(
        11, ctx, P.PendingParams(wick_bars=3, apply_guard=False))
    assert p_rev is not None, why
    assert p_rev.family == T.FAMILY_REVERSAL
    assert P.to_thesis(p_rev).stale_distance > 0
    # ★ 趋势家族必须走**真实的 to_thesis** 来验（手搓一个 Thesis 验的是
    #   数据类默认值，验不到「to_thesis 会不会给错家族」——
    #   变异 `stale-given-to-trend-family` 就是这么活下来的）。
    bars = [b15(0, PX, PX + 20, PX - 20, PX - 200)]
    ctx = make_ctx(bars, ema15=[PX], ema30=[PX])
    # 闸门在 T0+15m 查线，而 T0 那根 30m 线要到 T0+30m 才就绪 ——
    # 所以补一根 T0-M30 起的（ready=T0），代表「上一根 30m 收盘的 ema」。
    ctx.book.add({"line_key": T.FAST_LINE_KEY, "ts": T0 - M30,
                  "line_price": PX, "line_ready_ms": T0})
    trend, why = P._build(kind=P.KIND_EMA_REJECT, direction=P.DOWN,
                          order_type=T.ORDER_STOP, i=0, ctx=ctx,
                          params=P.PendingParams(), order_price=PX - 250.0,
                          evidence={}, bar=bars[0], bar_space="15m")
    assert why == "ok", why
    assert trend.family == T.FAMILY_TREND
    assert P.to_thesis(trend).stale_distance == 0.0


def test_stale_distance_is_one_r_by_default():
    """默认 1.0R = 止损距离。走掉一个完整止损还没触发 → 形态不新鲜了。"""
    ctx = _wick_ctx()
    p, why = P.detect_wicks_false_break(
        11, ctx, P.PendingParams(wick_bars=3, apply_guard=False))
    assert p is not None, why
    th = P.to_thesis(p, params=P.PendingParams(stale_stop_mult=1.0))
    sd = float(p.evidence["stop_distance"])
    assert abs(th.stale_distance - round(sd, 4)) < 1e-6


# ==================================================== 八、非终态必须显式标出

def test_unresolved_is_reported_when_claim_never_happens():
    """★ claim 永不成立 → 停在 EXPECTED。以前完全静默，看起来像跑成功了。"""
    th = _long_thesis()

    def never(i, bars, book):
        return False, {"why": "测试：永不 claim"}

    bars = _flat_bars(6)
    res = T.run(th, bars, book_of(slow=[1e6] * 6), claim=never, start_at=0)
    assert res.state == T.EXPECTED
    assert res.terminal is False
    assert res.unresolved is True
    assert "EXPECTED" in res.unresolved_reason or "claim" in res.unresolved_reason


def test_finished_run_is_not_marked_unresolved():
    """反证：正常终结的不许被标成 unresolved。"""
    th = _long_thesis()
    bars = _flat_bars(8)
    bars[3] = Bar(T0 + 3 * M15, 100.0, 101.0, 85.0, 86.0, 1.0)   # 砸穿 90 止损
    res = T.run(th, bars, book_of(slow=[1e6] * 8), start_at=0)
    assert res.state == T.CLOSED_SL, res.state
    assert res.terminal is True
    assert res.unresolved is False
    assert res.unresolved_reason == ""


def test_run_plan_surfaces_the_unresolved_reason():
    """`run_plan` 是策略层到状态机的唯一桥，它也得能看见这个标记。"""
    ctx = _wick_ctx()
    p, why = P.detect_wicks_false_break(
        11, ctx, P.PendingParams(wick_bars=3, apply_guard=False))
    assert p is not None, why
    # 声明落在很后面的一根上，序列里没有 → claim 永不成立
    p2 = type(p)(**{**p.__dict__, "claim_ts": T0 + 10_000 * M15})
    res = P.run_plan(p2, ctx.bars15, ctx.book, bars5=ctx.bars5)
    assert res.unresolved is True
    assert res.unresolved_reason


def test_invalidation_close_is_reached_without_a_time_gate():
    """★ 失效平仓走的是「粗周期块 + `if closed: continue`」这条路径。

    ★ 2026-11-17：时间闸撤掉之后这条测试仍然要留着 —— 它守的不再是
      「价格优先于时间」，而是**失效平仓这条路径本身是通的**
      （子循环只判止损/止盈，失效只能在粗周期块里判）。
      变异 `invalidation-close-ignored` 靠它。
    """
    th = T.Thesis(thesis_id="inv", kind="测试", direction=T.DOWN,
                  order_price=100.0, stop_price=130.0, target_price=30.0,
                  order_type=T.ORDER_LIMIT, family=T.FAMILY_TREND)
    bars = [Bar(T0 + i * M15, 60.0, 61.0, 59.0, 60.0, 1.0) for i in range(8)]
    bars[1] = Bar(T0 + M15, 60.0, 105.0, 59.0, 60.0, 1.0)      # 第 1 根被挂单价走到
    # ★ 成交发生在 5m 上：把第 1 根 15m 桶里的 5m K 抬到 105，才会被挂单价走到。
    b5 = [Bar(T0 + j * M5, 60.0, 61.0, 59.0, 60.0, 1.0) for j in range(60)]
    b5[3] = Bar(T0 + 3 * M5, 60.0, 105.0, 59.0, 60.0, 1.0)
    # ema30m：先在上方（做空合规）70，第 3 根才降到 50 → 那根起 VIOLATED。
    # 慢线（ema20@15m）一路 70，保证失效只可能来自快线，不掺 3 根确认。
    book = book_of(slow=[70.0] * 8)
    book.add({"line_key": T.FAST_LINE_KEY, "ts": T0 - M30,
              "line_price": 70.0, "line_ready_ms": T0})
    book.add({"line_key": T.FAST_LINE_KEY, "ts": T0 + M30,
              "line_price": 50.0, "line_ready_ms": T0 + 2 * M30})
    res = T.run(th, bars, book, entry_bars=b5, start_at=0)
    states = [t.to_state for t in res.transitions]
    assert T.FILLED in states, states
    assert res.state == T.CLOSED_THESIS, (res.state, states)
    assert T.CLOSED_SL not in states, states   # 失效平仓不是止损