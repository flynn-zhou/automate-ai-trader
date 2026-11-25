
# -*- coding: utf-8 -*-
"""趋势背景闸 + 止盈按背景收紧（2026-11-20 用户口径）。

用户原话：

    「15m 的 ema20 和 ema50 是倾斜向下，说明预期不要太高，震荡偏下的行情，
      止盈目标不要太远。严格来说我们的三引线是 ema20 和 ema50 有同向趋势
      且相距越走越宽，这时反弹会相对强劲一些，像现在这种不是标准的三引线
      买法，反弹也会弱一些。」

    「下跌时 ema20 是快线，在 ema50 下面才是正常的，假如在 ema50 上面，
      说明在尝试反转，那就是别的策略开单，三引线和倒V不开单，上涨镜像。」

★ 两件事被显式分开，这正是本文件要钉的：
    **位置**（快线在慢线哪一侧）→ 开不开单，二值，没有中间地带；
    **斜率/开口**（同向？变宽？）→ 预期多强，决定止盈放多远。

★ 11-20 那组真实数字当金标准（生产 kbar_line 实测）：
    决策时刻 10:05 能看到的最后一根 15m 线是 09:45（ready 10:00）：
        ema20 = 62086.89   ema50 = 62453.29   （gap 366.40）
    3 根前（09:00，ready 09:15）：
        ema20 = 62144.92   ema50 = 62519.22   （gap 374.30）
    → 位置对（ema20 < ema50）、斜率同向向下、但开口 374.30 → 366.40 **收窄**
    → `weak`：不是标准三引线买法，反弹偏弱 → 目标收到最近的顺向关键线。
"""
from collections import namedtuple

import pytest

from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T

M5 = 300_000
M15 = 900_000
H1 = 3_600_000
T0 = 1795137600000          # 11-20 08:00 CST 附近，往前后都够铺

Bar = namedtuple("Bar", "ts o h l c v")


def _bar(ts, o, h, l, c, v=100.0):
    return Bar(ts, o, h, l, c, v)


def _book(rows):
    """rows: [(key, ts, price, ready)] 或 [(key, ts, price)]（ready = ts+15m）。"""
    b = T.LineBook()
    for r in rows:
        key, ts, px = r[0], int(r[1]), float(r[2])
        ready = int(r[3]) if len(r) > 3 else ts + M15
        b.add((key, T.LinePoint(price=px, src_ts=ts, ready_ms=ready)))
    return b


#: 金标准那组线的**最后一根** ts。所有夹具都以它为锚点往前铺，
#: 决策时刻固定为 `_LAST_TS + M15`（= 最后一根线就绪的那一刻）。
#:
#: ★ 为什幺要单独立一个锚点：第一版把线铺在 `T0 - 3h` 起、而决策时刻按
#:   `T0` 算，两者差 3 小时 —— 决策时刻落在所有线之后，`book.at()` 对
#:   「现在」和「3 根前」都返回同一根 → 斜率恒 0、开口恒等 →
#:   standard 被误判成 weak。夹具错位会伪装成判据失效，所以锚点只能有一个。
_LAST_TS = T0


def _golden_close_ms():
    """决策时刻 = 最后一根趋势线就绪的那一刻。"""
    return _LAST_TS + M15


def _ema_pair(fast_vals, slow_vals, last_ts=None):
    """按 15m 铺两条线，**最后一根**落在 `_LAST_TS`；ready = ts + 15m（不偷未来）。"""
    last = _LAST_TS if last_ts is None else int(last_ts)
    n = len(fast_vals)
    start = last - (n - 1) * M15
    rows = []
    for i, px in enumerate(fast_vals):
        rows.append((T.TREND_FAST_KEY, start + i * M15, px))
    for i, px in enumerate(slow_vals):
        rows.append((T.TREND_SLOW_KEY, start + i * M15, px))
    return _book(rows)


# ---------------------------------------------------------------- 11-20 金标准

#: 11-20 的真实线值，按 15m 递增排列，最后一项是「10:05 时已就绪的最新一根」。
#: 索引 -1 = 09:45（ready 10:00），索引 -4 = 09:00（ready 09:15）→ 正好 3 根前。
_GOLDEN_FAST = [62127.12, 62144.92, 62159.22, 62143.96, 62086.89]
_GOLDEN_SLOW = [62527.60, 62519.22, 62510.43, 62490.38, 62453.29]


def test_11_20_is_weak_not_standard():
    """金标准：11-20 那组是 `weak` —— 同向但开口收窄 374.30 → 366.40。

    ★ 这条是整个改动的**原点**：用户看着那笔单说「不是标准的三引线买法，
      反弹也会弱一些」。数字必须能复现他的判断，否则判据是拍脑袋的。
    """
    book = _ema_pair(_GOLDEN_FAST, _GOLDEN_SLOW)
    got = P.trend_context(book, _golden_close_ms(), P.UP, family=T.FAMILY_REVERSAL)
    assert got is not None
    assert got["grade"] == P.TREND_WEAK
    assert got["fast"] == pytest.approx(62086.89, abs=0.01)
    assert got["slow"] == pytest.approx(62453.29, abs=0.01)
    assert got["gap_now"] == pytest.approx(366.40, abs=0.02)
    assert got["gap_prev"] == pytest.approx(374.30, abs=0.02)
    assert got["gap_now"] < got["gap_prev"], "开口在收窄"
    assert got["aligned"] is True, "位置是对的（快线在下方）"
    assert got["same_dir"] is True, "两根都在向下"


def test_standard_needs_both_aligned_and_widening():
    """同向 + 开口变宽 = standard（反弹强劲，止盈照 4R 放）。"""
    fast = [61000.0, 60800.0, 60600.0, 60400.0, 60200.0]     # 下降
    slow = [62000.0, 61950.0, 61900.0, 61850.0, 61800.0]     # 下降更慢
    book = _ema_pair(fast, slow)
    got = P.trend_context(book, _golden_close_ms(), P.UP, family=T.FAMILY_REVERSAL)
    assert got["grade"] == P.TREND_STANDARD
    assert got["gap_now"] > got["gap_prev"]
    assert got["same_dir"] is True and got["aligned"] is True


def test_same_direction_but_narrowing_is_weak():
    """同向但收敛 = weak（趋势在衰减，反弹弱）。

    ★ fast 越降越快 → 与 slow 的距离**缩小**：反弹的势能在衰减。
    """
    fast = [61200.0, 61100.0, 61000.0, 60900.0, 60800.0]     # 每根 -100
    slow = [62200.0, 62000.0, 61800.0, 61600.0, 61400.0]     # 每根 -200（降更快）
    book = _ema_pair(fast, slow)
    got = P.trend_context(book, _golden_close_ms(), P.UP, family=T.FAMILY_REVERSAL)
    assert got["aligned"] is True
    assert got["grade"] == P.TREND_WEAK


def test_fast_above_slow_is_wrong_side_for_long():
    """★ 位置判据（用户原话的核心）：下跌背景里 ema20 在**上方** = 在尝试反转。

    「假如在 ema50 上面，说明在尝试反转，那就是别的策略开单，
      三引线和倒V不开单」
    """
    fast = [63000.0, 62900.0, 62800.0, 62700.0, 62600.0]     # 在慢线上方
    slow = [62000.0, 61950.0, 61900.0, 61850.0, 61800.0]
    book = _ema_pair(fast, slow)
    got = P.trend_context(book, _golden_close_ms(), P.UP, family=T.FAMILY_REVERSAL)
    assert got["grade"] == P.TREND_WRONG_SIDE
    assert got["aligned"] is False


def test_mirror_swaps_the_sides():
    """上涨镜像：做空要求快线在**上方**。同一个 book，方向反过来结论就反过来。"""
    fast = [63000.0, 62900.0, 62800.0, 62700.0, 62600.0]
    slow = [62000.0, 61950.0, 61900.0, 61850.0, 61800.0]
    book = _ema_pair(fast, slow)
    up = P.trend_context(book, _golden_close_ms(), P.UP, family=T.FAMILY_REVERSAL)
    down = P.trend_context(book, _golden_close_ms(), P.DOWN, family=T.FAMILY_REVERSAL)
    assert up["grade"] == P.TREND_WRONG_SIDE
    assert down["grade"] != P.TREND_WRONG_SIDE
    assert down["aligned"] is True


def test_missing_lines_returns_none_not_a_pass():
    """缺线 = **判不了**，返回 None。调用方必须保守处理，不许当「没问题」。"""
    book = _book([(T.TREND_FAST_KEY, T0, 100.0)])       # 只有快线
    assert P.trend_context(book, T0 + 5 * M15, P.UP, family=T.FAMILY_REVERSAL) is None
    assert P.trend_context(None, T0, P.UP, family=T.FAMILY_REVERSAL) is None


def test_lines_not_yet_ready_returns_none():
    """线还没就绪（ready 晚于决策时刻）→ 判不了。不许偷未来。"""
    book = _book([(T.TREND_FAST_KEY, T0, 100.0, T0 + 99 * M15),
                  (T.TREND_SLOW_KEY, T0, 200.0, T0 + 99 * M15)])
    assert P.trend_context(book, T0 + M15, P.UP, family=T.FAMILY_REVERSAL) is None


# ---------------------------------------------------------------- 最近的顺向关键线

def test_nearest_favorable_line_picks_the_closest_above_entry():
    book = _book([("ema50@15m", T0, 110.0),
                  ("ema144@15m", T0, 130.0),
                  ("prior_high@15m", T0, 120.0)])
    got = P.nearest_favorable_line(book, T0 + M15, 100.0, P.UP)
    assert got["key"] == "ema50@15m" and got["line"] == pytest.approx(110.0)


def test_nearest_favorable_line_ignores_the_wrong_side():
    """不利方向的线不是目标位（做多时下方的线）。"""
    book = _book([("ema50@15m", T0, 90.0)])
    assert P.nearest_favorable_line(book, T0 + M15, 100.0, P.UP) is None


def test_nearest_favorable_line_respects_min_dist():
    """★ `min_dist`：贴脸的线（远小于 1R）不能当目标 —— 那样扣掉费是负期望。"""
    book = _book([("ema50@15m", T0, 101.0),        # 只有 1 点，远小于 1R
                  ("ema144@15m", T0, 130.0)])
    got = P.nearest_favorable_line(book, T0 + M15, 100.0, P.UP, min_dist=20.0)
    assert got["key"] == "ema144@15m"
    assert P.nearest_favorable_line(book, T0 + M15, 100.0, P.UP,
                                    min_dist=500.0) is None


def test_nearest_favorable_line_mirrors_for_short():
    book = _book([("prior_low@15m", T0, 90.0), ("prior_low@15m", T0, 95.0)])
    got = P.nearest_favorable_line(book, T0 + M15, 100.0, P.DOWN)
    assert got["line"] == pytest.approx(95.0), "做空取最近的**下方**那条"


# ---------------------------------------------------------------- 止盈收紧

def test_target_is_capped_by_the_line_never_loosened():
    """★ 只收紧、不放宽：cap 比 4R 远时完全忽略。"""
    params = P.PendingParams(max_stop_pct=1.6181818, target_r_multiple=4.0)
    order = 61265.60
    base = P.plan_prices(order, P.UP, 560.0, 145.0, params)
    near = P.plan_prices(order, P.UP, 560.0, 145.0, params,
                         target_cap=order + 800.0)
    far = P.plan_prices(order, P.UP, 560.0, 145.0, params,
                        target_cap=order + 99999.0)
    assert near.target_price < base.target_price, "近的线要把目标收紧"
    assert far.target_price == pytest.approx(base.target_price), \
        "比 4R 还远的目标不许放宽"
    assert near.evidence["target_source"] == "nearest_favorable_line"
    assert far.evidence["target_source"] == "r_multiple"


def test_11_20_target_lands_on_ema50():
    """金标准的落点：目标从 4.19R 收到 ema50 的约 1.25R。

    ★ 注意这条线是**移动**的：决策时刻 ema50 = 62453.29，而持仓到 11:00 时
      ema50 已降到 62345.59，real max 只到 62378.7 —— 目标够不着，
      于是真正的出场是**观察线拒绝**（现实跟随）。这说明在弱背景里
      「固定目标价」只是上界，「观察线拒绝」才是主出场路径。
    """
    params = P.PendingParams(max_stop_pct=1.6181818, target_r_multiple=4.0)
    order = 61255.6 + 10.0
    book = _ema_pair(_GOLDEN_FAST, _GOLDEN_SLOW)
    close_ms = _golden_close_ms()
    base = P.plan_prices(order, P.UP, 560.0, 145.0, params)
    one_r = P._stop_distance(order, P.amp_stop_distance(560.0, 145.0, params),
                             params)
    cap = P.nearest_favorable_line(book, close_ms, order, P.UP, min_dist=one_r)
    assert cap is not None and cap["key"] == T.TREND_SLOW_KEY
    capped = P.plan_prices(order, P.UP, 560.0, 145.0, params,
                           target_cap=cap["line"])
    assert capped.target_price == pytest.approx(62453.29, abs=0.01)
    assert capped.evidence["gross_rr"] < base.evidence["gross_rr"]
    assert 1.0 < capped.evidence["gross_rr"] < 2.0


# ---------------------------------------------------------------- 接进 _build

def _plan_ctx(book, bars15, bars5, bars1h):
    return P.PlanContext(bars15=bars15, book=book, bars_1h=bars1h,
                         bars_5m=bars5)


def _flat(n, space_ms, start, px=61265.6, half=100.0):
    return [_bar(start + i * space_ms, px, px + half, px - half, px)
            for i in range(n)]


#: `_build_ctx` 里决策用的那根下标（`_build(i=10)`）。
_DECIDE_I = 10


def _build_ctx(fast_now, slow_now, fast_prev, slow_prev):
    """造一个能真正走到定价的上下文。

    ★ 三引线的 `BAR_SPACE` 是 **5m**，所以 `close_ms = bars5[10].ts + 5m`
      = T0+55min —— **不是 15m 整点**。按 15m 网格铺线（ready = ts+15m）
      会让 `at(close_ms)` 与 `at(close_ms-45m)` 一个命中一个落空
      → 直接 `no_trend_line`。

      所以这里只放两个点，用「查询时刻」当 ready：
          now : ts = close_ms - 15m,   ready = close_ms
          prev: ts = close_ms - 4×15m, ready = close_ms - 3×15m
      `book.at()` 取「ready 最大且不超过查询时刻」的那个点，
      于是查 `close_ms` 命中 now、查 `close_ms-45m` 命中 prev —— 与网格无关。

    ★ 另一条硬约束：前置闸要求「下跌背景里价格在 ema 下方」，
      所以两条线都得在价格**上方**；同时快线要仍在慢线下方。
    """
    start = T0
    close_ms = start + _DECIDE_I * M5 + M5
    now_ts = close_ms - M15
    prev_ts = close_ms - 4 * M15
    book = _book([
        (T.TREND_FAST_KEY, prev_ts, fast_prev, close_ms - 3 * M15),
        (T.TREND_SLOW_KEY, prev_ts, slow_prev, close_ms - 3 * M15),
        (T.TREND_FAST_KEY, now_ts, fast_now, close_ms),
        (T.TREND_SLOW_KEY, now_ts, slow_now, close_ms),
    ])
    bars15 = _flat(_DECIDE_I + 4, M15, start)
    bars5 = _flat(60, M5, start)
    bars1h = [_bar(start - (3 - k) * H1, 61265.6, 61265.6 + 80.0,
                   61265.6 - 80.0, 61265.6) for k in range(3)]
    return _plan_ctx(book, bars15, bars5, bars1h)


def test_build_rejects_when_fast_line_is_on_the_wrong_side():
    """位置错 → `trend_wrong_side`，三引线不出单（用户口径）。"""
    # 快线在慢线上方（做成「在尝试反转」），两根都还在价格上方
    ctx = _build_ctx(fast_now=62600.0, slow_now=61800.0,
                     fast_prev=62900.0, slow_prev=61950.0)
    plan, why = P._build(kind=P.KIND_WICKS, direction=P.UP,
                         order_type=T.ORDER_LIMIT, i=10, ctx=ctx,
                         params=P.PendingParams(),
                         order_price=61265.6 - 50.0, evidence={})
    assert plan is None
    assert why == "trend_wrong_side"


def test_build_rejects_when_trend_lines_are_missing():
    """缺线 → `no_trend_line`（判不了就不许放行）。"""
    ctx = _plan_ctx(T.LineBook(), _flat(_DECIDE_I + 4, M15, T0),
                    _flat(60, M5, T0),
                    [_bar(T0 - (3 - k) * H1, 61265.6, 61345.6, 61185.6,
                          61265.6) for k in range(3)])
    plan, why = P._build(kind=P.KIND_WICKS, direction=P.UP,
                         order_type=T.ORDER_LIMIT, i=10, ctx=ctx,
                         params=P.PendingParams(),
                         order_price=61265.6 - 50.0, evidence={})
    assert plan is None
    assert why == "no_trend_line"


def test_build_passes_and_records_the_context():
    """位置对 → 出单，并把背景结论写进 evidence（事后能查为什么）。"""
    # 两条线都在价格（61265.6）上方，快线仍在慢线下方，开口**变宽** → standard
    #   now : 62100 vs 62580 → gap 480
    #   prev: 62400 vs 62820 → gap 420
    ctx = _build_ctx(fast_now=62100.0, slow_now=62580.0,
                     fast_prev=62400.0, slow_prev=62820.0)
    plan, why = P._build(kind=P.KIND_WICKS, direction=P.UP,
                         order_type=T.ORDER_LIMIT, i=10, ctx=ctx,
                         params=P.PendingParams(),
                         order_price=61265.6 - 50.0, evidence={})
    assert why == "ok", why
    assert plan.evidence["trend_ctx"]["grade"] == P.TREND_STANDARD
    assert "trend_ctx" in plan.evidence


def test_all_three_patterns_are_gated():
    """★ **三族都过闸**（用户 2026-11-20 把三族放在同一句里点名）。

    上一版这里断言的是「EMA 拒绝不过闸」，理由是「用户只点名了三引线和倒V」，
    而那句原话的后半句正是「反弹时 ema20 拒绝开空单」—— 读窄了。
    """
    assert P.TREND_GATE.get(P.KIND_WICKS) is True
    assert P.TREND_GATE.get(P.KIND_INVERSE_V) is True
    assert P.TREND_GATE.get(P.KIND_EMA_REJECT) is True


# ---------------------------------------------------------------- 极性按族定

def test_reversal_family_goes_against_the_regime():
    """反转族**逆**背景：三下引线（做多）要下跌背景，三上引线（做空）要上涨背景。

    用户：「下跌趋势，ema20 在 ema50 下面，三下引线和倒V开单」。
    """
    down = _ema_pair([61000.0, 60800.0, 60600.0, 60400.0, 60200.0],
                     [62000.0, 61900.0, 61800.0, 61700.0, 61600.0])
    up = _ema_pair([62000.0, 62100.0, 62200.0, 62300.0, 62400.0],
                   [61000.0, 61100.0, 61200.0, 61300.0, 61400.0])
    assert P.trend_context(down, _golden_close_ms(), P.UP,
                           family=T.FAMILY_REVERSAL)["grade"] != P.TREND_WRONG_SIDE
    assert P.trend_context(up, _golden_close_ms(), P.DOWN,
                           family=T.FAMILY_REVERSAL)["grade"] != P.TREND_WRONG_SIDE
    # 反过来（做多但背景在涨）= 位置错
    assert P.trend_context(up, _golden_close_ms(), P.UP,
                           family=T.FAMILY_REVERSAL)["grade"] == P.TREND_WRONG_SIDE
    assert P.trend_context(down, _golden_close_ms(), P.DOWN,
                           family=T.FAMILY_REVERSAL)["grade"] == P.TREND_WRONG_SIDE


def test_trend_family_goes_with_the_regime():
    """★ 趋势族**顺**背景：EMA 拒绝做空要下跌背景、做多要上涨背景。

    用户：「下跌趋势…反弹时 ema20 拒绝**开空单**；上涨趋势…回调时 ema20
    拒绝**开多单**」。

    ★ 这条是上一版判反的地方：当时按「做空 ⇒ 要 ema20 > ema50」推，
      对 EMA 拒绝正好相反 —— 美元会把所有真正的反弹做空全拒掉。
    """
    down = _ema_pair([61000.0, 60800.0, 60600.0, 60400.0, 60200.0],
                     [62000.0, 61900.0, 61800.0, 61700.0, 61600.0])
    up = _ema_pair([62000.0, 62100.0, 62200.0, 62300.0, 62400.0],
                   [61000.0, 61100.0, 61200.0, 61300.0, 61400.0])
    got = P.trend_context(down, _golden_close_ms(), P.DOWN,
                          family=T.FAMILY_TREND)
    assert got["grade"] != P.TREND_WRONG_SIDE, "下跌趋势里 EMA 拒绝开空单必须放行"
    assert got["regime"] == "down" and got["required_regime"] == "down"
    got_long = P.trend_context(up, _golden_close_ms(), P.UP,
                               family=T.FAMILY_TREND)
    assert got_long["grade"] != P.TREND_WRONG_SIDE, "上涨趋势里 EMA 拒绝开多单必须放行"
    # 反例：下跌趋势里想做多（不是 EMA 拒绝该做的事）
    assert P.trend_context(down, _golden_close_ms(), P.UP,
                           family=T.FAMILY_TREND)["grade"] == P.TREND_WRONG_SIDE


def test_flat_lines_are_undecidable():
    """两线重合 → `flat`（判不了就不做），不许默认放行。"""
    book = _ema_pair([60000.0] * 5, [60000.0] * 5)
    got = P.trend_context(book, _golden_close_ms(), P.UP,
                          family=T.FAMILY_REVERSAL)
    assert got["grade"] == P.TREND_FLAT
    assert got["regime"] == "flat" and got["aligned"] is False


def test_unknown_family_returns_none():
    """族没登记 → None（不许回退成默认极性）。"""
    book = _ema_pair([61000.0, 60800.0, 60600.0, 60400.0, 60200.0],
                     [62000.0, 61900.0, 61800.0, 61700.0, 61600.0])
    assert P.trend_context(book, _golden_close_ms(), P.UP,
                           family="不存在的族") is None
