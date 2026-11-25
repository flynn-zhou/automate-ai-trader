# -*- coding: utf-8 -*-
"""三引线重写（2026-11-20 用户口径）+ 观察线拒绝出场 + 采集时钟。

用 11-20 09:50~10:10 那组真实 K 线的数字当金标准：

    09:50  O 61866.8  H 61908.0  L 61255.6  C 61474.1   ← 参考引线 A（长下影）
    09:55  O 61474.1  H 61701.6  L 61402.5  C 61544.7   ← A 的 inside bar
    10:00  O 61679.4  H 61737.0  L 61660.1  C 61700.3   ← A 的 inside bar
    10:05  O 61700.2  H 61700.2  L 61272.2  C 61357.5   ← 确认引线 B（下影回到 A 的影线区）
    10:10  O 61377.4  H 61517.4  L 61260.0  C 61509.7   ← 触发根

用户原话：「我们就会在 09:50 和 10:05 的低点 +1tick 做买单」「给与一定的
容忍度，比如 +-10U」→ 挂单价 = 61255.6 + 10 = **61265.60**。
"""
from collections import namedtuple

import pytest

from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T

Bar = namedtuple("Bar", "ts o h l c v")

M5 = 300_000
M15 = 900_000
M60 = 3_600_000
T0 = 1795137600000          # 11-20 09:20 CST（前面的历史根起点）


def _bar(ts, o, h, l, c, v=100.0):
    return Bar(ts, o, h, l, c, v)


def _golden_5m():
    """11-20 09:20 起、每根 5m 的 20 根：前 6 根造历史，后面是那组真实 K。"""
    out = []
    for k in range(6):                       # 09:20~09:45 造历史（振幅 ~100）
        out.append(_bar(T0 + k * M5, 62000.0 + k, 62050.0 + k, 61950.0 + k,
                        62010.0 + k))
    real = [
        (1795139400000, 61866.8, 61908.0, 61255.6, 61474.1),   # 09:50 A
        (1795139700000, 61474.1, 61701.6, 61402.5, 61544.7),   # 09:55 inside
        (1795140000000, 61679.4, 61737.0, 61660.1, 61700.3),   # 10:00 inside
        (1795140300000, 61700.2, 61700.2, 61272.2, 61357.5),   # 10:05 B
        (1795140600000, 61377.4, 61517.4, 61260.0, 61509.7),   # 10:10 触发
    ]
    for ts, o, h, l, c in real:
        out.append(_bar(ts, o, h, l, c))
    return out


def _h1(b5):
    out = []
    for b in b5:
        k = int(b.ts) - int(b.ts) % M60
        if out and out[-1].ts == k:
            p = out[-1]
            out.append(_bar(k, p.o, max(p.h, b.h), min(p.l, b.l), b.c))
        else:
            out.append(_bar(k, b.o, b.h, b.l, b.c))
    return out


#: 趋势背景闸要的那一对线（快线在下方 = 下跌背景位置正确；恒定 ⇒ weak）。
TREND_LINES = ((T.TREND_FAST_KEY, 61971.2 - 400.0),
               (T.TREND_SLOW_KEY, 61971.2))

#: 趋势线要**早就就绪**：`trend_context` 会回看 3 根 15m，
#: 线的 ready 晚于那个时刻就取不到 → `no_trend_line`。
TREND_TS = 1795137600000 - 10 * M15


def _ctx(b5, lines=()):
    book = T.LineBook([dict(line_key=k, ts=TREND_TS, line_price=px,
                            src_ts=TREND_TS, line_ready_ms=TREND_TS)
                       for k, px in TREND_LINES])
    for k, ts, px in lines:
        book.add(dict(line_key=k, ts=ts, line_price=px, src_ts=ts,
                      line_ready_ms=ts + M15))
    return P.PlanContext([], book, bars_1h=_h1(b5), bars_5m=b5, windows=None)


def _idx(ctx, ts):
    for i, b in enumerate(ctx.series_for(P.KIND_WICKS)):
        if int(b.ts) == ts:
            return i
    raise AssertionError("序列里没有 ts=%s" % ts)


# ---------------------------------------------------------------- 形状与定价

def test_three_wicks_golden_case_prices_at_low_plus_tolerance():
    """金标准：10:05 出计划，价 61265.60（= 61255.6 + 10），一根不多一根不少。"""
    b5 = _golden_5m()
    ctx = _ctx(b5, lines=[("ema20@30m", 1795138200000, 61971.2)])
    params = P.PendingParams()
    plan, why = P.detect_wicks_false_break(_idx(ctx, 1795140300000), ctx,
                                           params, P.UP)
    assert why == "ok", "10:05 应该出计划，实际 %s" % why
    assert plan is not None
    assert plan.order_price == pytest.approx(61255.6 + params.entry_tolerance_pts,
                                             abs=0.01)
    assert plan.order_price == pytest.approx(61265.60, abs=0.01)
    # 证据里要能看出「参考的是哪一根、中间夹了几根」——排障靠它
    assert plan.evidence["ref_ts"] == 1795139400000
    assert plan.evidence["inside_bars"] == 2


def test_inside_bar_is_not_a_confirm_bar():
    """10:00 那根是 inside bar（low 61660 没回到 09:50 的影线区）→ 不能当确认根。"""
    b5 = _golden_5m()
    ctx = _ctx(b5)
    plan, why = P.detect_wicks_false_break(_idx(ctx, 1795140000000), ctx,
                                           P.PendingParams(), P.UP)
    assert plan is None
    assert why in ("ref_too_far", "not_inside_bar")


def test_next_bar_does_not_duplicate_the_plan():
    """10:10 不能再出一条（否则同一笔会挂两张单）。"""
    b5 = _golden_5m()
    ctx = _ctx(b5)
    plan, why = P.detect_wicks_false_break(_idx(ctx, 1795140600000), ctx,
                                           P.PendingParams(), P.UP)
    assert plan is None, "10:10 不该再出计划，实际 %s" % why


def test_tolerance_is_the_only_slack():
    """容差只作用在挂单价上：结构判据不跟着放宽（否则形态会自我复制）。"""
    p0 = P.PendingParams(entry_tolerance_pts=0.0)
    assert p0.wick_proximity_pts == 20.0, "口袋宽度是独立初值，不是容差" 
    b5 = _golden_5m()
    ctx = _ctx(b5, lines=[("ema20@30m", 1795138200000, 61971.2)])
    plan, why = P.detect_wicks_false_break(_idx(ctx, 1795140300000), ctx, p0, P.UP)
    assert why == "ok"
    assert plan.order_price == pytest.approx(61255.6, abs=0.01)


def test_tolerance_bars_caps_inside_run():
    """夹 2 根 inside bar 可以（默认），容忍上限为 1 时就该判不出来。"""
    b5 = _golden_5m()
    ctx = _ctx(b5, lines=[("ema20@30m", 1795138200000, 61971.2)])
    plan, why = P.detect_wicks_false_break(
        _idx(ctx, 1795140300000), ctx,
        P.PendingParams(wick_tolerance_bars=1), P.UP)
    assert plan is None


# ---------------------------------------------------------------- 前置条件闸

def test_guard_falls_back_to_ema15_when_30m_line_absent():
    """生产没有 ema20@30m —— 缺线时必须回退到 ema20@15m，而不是一律拒。"""
    b5 = _golden_5m()
    ts = 1795140300000
    ctx = _ctx(b5, lines=[("ema20@30m", 1795138200000, 61971.2)])
    bar = next(b for b in b5 if int(b.ts) == ts)
    ok, why = P.guard_preconditions(None, bar, ctx.book, direction=P.UP,
                                    kind=P._variant_name(P.KIND_WICKS, P.UP),
                                    close_ms=ts + M5)
    assert ok, "价格 61357 在 ema20@15m 61971 下方（下跌背景）→ 应放行，实际 %s" % why


def test_guard_rejects_wrong_side():
    """位置错了照样拒：做多背景要求价格在 ema 下方。"""
    b5 = _golden_5m()
    ts = 1795140300000
    ctx = _ctx(b5, lines=[("ema20@15m", 1795139100000, 61000.0)])   # 线在价格下方
    bar = next(b for b in b5 if int(b.ts) == ts)
    ok, why = P.guard_preconditions(None, bar, ctx.book, direction=P.UP,
                                    kind=P._variant_name(P.KIND_WICKS, P.UP),
                                    close_ms=ts + M5)
    assert not ok
    assert "not_below" in why


def test_guard_prefers_30m_line_when_present():
    """有 @30m 就用 @30m（保持原设计），不能被回退逻辑覆盖。"""
    b5 = _golden_5m()
    ts = 1795140300000
    ctx = _ctx(b5, lines=[("ema20@30m", 1795139100000, 61000.0),      # 线上方 → 应拒
                          ("ema20@15m", 1795139100000, 69000.0)])     # 线下方 → 会放行
    bar = next(b for b in b5 if int(b.ts) == ts)
    ok, why = P.guard_preconditions(None, bar, ctx.book, direction=P.UP,
                                    kind=P._variant_name(P.KIND_WICKS, P.UP),
                                    close_ms=ts + M5)
    assert not ok and "not_below" in why


# ---------------------------------------------------------------- 观察线拒绝

def _thesis(direction="up", order_price=61265.6, stop=60837.6, target=62625.4):
    return T.Thesis(thesis_id="t1", kind="测试", direction=direction,
                    order_price=order_price, stop_price=stop,
                    target_price=target)


def test_observation_reject_exits_on_ema50_pierce_and_close_back():
    """11-20 11:00 那根：最高 62378.7 穿 ema50 62345.6，收回 62061.5 → 出场。"""
    book = T.LineBook([dict(line_key="ema50@15m", ts=1795142700000,
                            line_price=62345.6, src_ts=1795142700000,
                            line_ready_ms=1795143600000)])
    bar = _bar(1795143600000, 62247.6, 62378.7, 62052.2, 62061.5)
    got = T.observation_reject(_thesis(), bar, book, 1795143900000)
    assert got is not None
    assert got["key"] == "ema50@15m"
    assert got["exit_price"] == pytest.approx(62061.5)


def test_observation_reject_ignores_close_beyond_line():
    """收盘站上去了 = 那条线没拒绝，不能当出场（正是 ema20 的情形）。"""
    book = T.LineBook([dict(line_key="ema20@15m", ts=1795142700000,
                            line_price=61971.2, src_ts=1795142700000,
                            line_ready_ms=1795143600000)])
    bar = _bar(1795143600000, 62247.6, 62378.7, 62052.2, 62261.5)   # 收在线之上
    assert T.observation_reject(_thesis(), bar, book, 1795143900000) is None


def test_observation_reject_ignores_lines_below_entry_for_long():
    """做多只认上方的线；下方的线不算「到过目标区」。"""
    book = T.LineBook([dict(line_key="ema50@15m", ts=1795142700000,
                            line_price=61000.0, src_ts=1795142700000,
                            line_ready_ms=1795143600000)])
    bar = _bar(1795143600000, 61100.0, 61120.0, 60900.0, 60950.0)
    assert T.observation_reject(_thesis(), bar, book, 1795143900000) is None


def test_observation_reject_mirrors_for_short():
    """做空镜像：下穿下方的线又收回 → 出场。"""
    book = T.LineBook([dict(line_key="prior_low@15m", ts=1795142700000,
                            line_price=61000.0, src_ts=1795142700000,
                            line_ready_ms=1795143600000)])
    th = _thesis(direction="down", order_price=61265.6, stop=61700.0,
                 target=60400.0)
    bar = _bar(1795143600000, 61150.0, 61180.0, 60900.0, 61080.0)
    got = T.observation_reject(th, bar, book, 1795143900000)
    assert got is not None and got["key"] == "prior_low@15m"


# ---------------------------------------------------------------- 采集时钟

def test_harvest_clock_follows_plan_bar_space():
    """5m 口径的计划按 5m 时钟采、15m 口径按 15m 时钟采（引擎侧不变式）。

    这里直接钉住判据本身：`effective_claim_ts` 是「形态自己那根 K 的起始时刻」，
    采集必须用同一周期的「最后一根」去比 —— 用 15m 去比 5m 会永远不等。
    """
    b5 = _golden_5m()
    ctx = _ctx(b5, lines=[("ema20@30m", 1795138200000, 61971.2)])
    plan, why = P.detect_wicks_false_break(_idx(ctx, 1795140300000), ctx,
                                           P.PendingParams(), P.UP)
    assert why == "ok"
    assert plan.claim_on_entry is True
    assert plan.effective_claim_ts == 1795140300000      # 5m 起始时刻
    assert plan.effective_claim_ts % M15 != 0            # 不是 15m 边界 → 旧判据必失配
