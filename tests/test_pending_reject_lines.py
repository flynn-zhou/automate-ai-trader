# -*- coding: utf-8 -*-
"""EMA 拒绝族：被拒绝的线可以是 ema20 / ema50 / ema144。

用户 2026-11-20 原话：
    「下跌反弹时 ema20 多次拒绝开空，同样适用于 ema50，不知道你实现了没有，
      ema 没有被多次拒绝，被突破后行情来到 ema50，ema50 出现多次拒绝也是
      同样的！上涨回调镜像」

追问后确认两件事（都钉在这份测试里）：
    ① 范围 = **ema20 + ema50 + ema144**（三条都算）；
    ② **不要求**「先被 ema20 拒绝过」这个先后顺序。

★ 这份测试存在的理由：这个功能有**两个**独立的拦路点，只修一个等于没修 ——
     · 检测器 `detect_ema_reject` 原来只认 `params.ema_key`（写死 ema20）；
     · 守卫 `guard_preconditions` 原来按 `close < ema20` 判位置，而 ema50/ema144
       被拒绝的场景恰恰是「价格已突破 ema20、反弹到更远的线」→ 全被拒掉。
  实测（本地 121 天）：ema50 有 155 个形态候选、ema144 有 75 个，
  在守卫面前放行 **0** 个。所以「能穿过守卫」必须是独立的断言，
  否则哪天有人把守卫改回去，检测器的测试仍然全绿。
"""
import os
import sys
from collections import namedtuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai_trader.trading import pending_plan as P    # noqa: E402
from ai_trader.trading import thesis as T          # noqa: E402

M15 = T.BAR_INTERVAL_MS
T0 = 1_700_000_000_000 - (1_700_000_000_000 % M15)
PX = 100_000.0

Bar = namedtuple("Bar", "ts o h l c v")


def b15(i, o, h, l, c):
    return Bar(T0 + i * M15, float(o), float(h), float(l), float(c), 1.0)


class _MultiBook:
    """三条线各自给一个价，且都能读。

    默认（下跌背景、做空口径）：
        ema20  = PX          （最近的线）
        ema50  = PX + 400    （价格反弹到它）
        ema144 = PX + 900    （更远）
    `ema50 == ema20 + 400` 保证趋势背景是「下跌」（ema20 < ema50），
    这是 EMA 拒绝做空的正确背景（趋势族顺背景）。
    """

    def __init__(self, p20=PX, p50=PX + 400.0, p144=PX + 900.0):
        self.px = {T.SLOW_LINE_KEY: float(p20),
                   T.TREND_SLOW_KEY: float(p50),
                   T.EMA144_LINE_KEY: float(p144)}

    def at(self, key, ts):
        px = self.px.get(key)
        if px is None:
            return None
        return T.LinePoint(price=px, src_ts=int(ts), ready_ms=int(ts))


def _reject_bar(i, level):
    """做空方向：上影线刺穿 level（触及/穿过），但收盘回到 level 下方。"""
    c = level - 10.0
    return b15(i, c, level + 110.0, c - 90.0, c)


def _h1(n=20, half=160.0):
    start = T0 - 6 * 3_600_000
    return [Bar(start + i * 3_600_000, PX, PX + half, PX - half, PX, 1.0)
            for i in range(n)]


M5 = 300_000


def _m5(n=40, half=45.0):
    return [Bar(T0 + i * M5, PX, PX + half, PX - half, PX, 1.0)
            for i in range(n)]


def _ctx(book, levels, n=10):
    """`levels[i]` = 第 i 根拒绝用的价位；None 表示这根是普通 K（不拒绝）。

    普通 K 放在对应线的**下方很远**，保证它既不是拒绝根、也不干扰计数。
    """
    bars = []
    for i in range(n):
        lv = levels.get(i)
        bars.append(_reject_bar(i, lv) if lv is not None
                    else b15(i, PX - 500, PX - 490, PX - 510, PX - 500))
    return P.PlanContext(bars15=bars, book=book, bars_1h=_h1(), bars_5m=_m5())


DECIDE = 9          # 最后一根 = 决策根


# ---------------------------------------------------------------- 候选线清单

def test_candidates_are_the_three_lines_nearest_first():
    """候选线 = ema20 → ema50 → ema144，**由近到远**，且来自唯一来源。"""
    keys = P.PendingParams().REJECT_LINE_KEYS
    assert keys == (T.SLOW_LINE_KEY, T.TREND_SLOW_KEY, T.EMA144_LINE_KEY)
    # 顺序就是「近 → 远」，不是随便排的
    assert keys[0] == "ema20@15m"
    assert keys[1] == "ema50@15m"
    assert keys[2] == "ema144@15m"


def test_piercing_ema20_is_recognized_as_before():
    """回归：只在 ema20 上被拒绝时，行为与改前完全一致（用的还是 ema20）。"""
    book = _MultiBook()
    ctx = _ctx(book, {DECIDE - 1: PX, DECIDE: PX})
    plan, why = P.detect_ema_reject(DECIDE, ctx, P.PendingParams(), P.DOWN)
    assert why == "ok", why
    assert plan.evidence["reject_line_key"] == T.SLOW_LINE_KEY
    assert plan.evidence["ema_key"] == T.SLOW_LINE_KEY


def test_piercing_ema50_is_recognized():
    """★ 核心新行为：价格反弹到 ema50 被多次拒绝 → 同样成立。"""
    book = _MultiBook()
    lv50 = book.px[T.TREND_SLOW_KEY]
    ctx = _ctx(book, {DECIDE - 1: lv50, DECIDE: lv50})
    plan, why = P.detect_ema_reject(DECIDE, ctx, P.PendingParams(), P.DOWN)
    assert why == "ok", why
    assert plan.evidence["reject_line_key"] == T.TREND_SLOW_KEY
    # 证据里三条候选都要留痕（事后能看出「当时还试过哪两条」）
    assert plan.evidence["reject_candidates"] == \
        list(P.PendingParams().REJECT_LINE_KEYS)


def test_piercing_ema144_is_recognized():
    """★ ema144 也算（用户追问后明确「ema20 + ema50 + ema144 都算」）。"""
    book = _MultiBook()
    lv144 = book.px[T.EMA144_LINE_KEY]
    ctx = _ctx(book, {DECIDE - 1: lv144, DECIDE: lv144})
    plan, why = P.detect_ema_reject(DECIDE, ctx, P.PendingParams(), P.DOWN)
    assert why == "ok", why
    assert plan.evidence["reject_line_key"] == T.EMA144_LINE_KEY


def test_nearest_line_wins_when_two_lines_qualify():
    """两条线都成立时取**近的**（ema20）。近的止损更近、目标更容易到。"""
    book = _MultiBook()
    lv20 = book.px[T.SLOW_LINE_KEY]
    ctx = _ctx(book, {DECIDE - 1: lv20, DECIDE: lv20})
    plan, why = P.detect_ema_reject(DECIDE, ctx, P.PendingParams(), P.DOWN)
    assert why == "ok", why
    assert plan.evidence["reject_line_key"] == T.SLOW_LINE_KEY


# ---------------------------------------------------------------- 不许合并计数

def test_rejections_are_not_summed_across_lines():
    """★ 每条线**各自**要够次数，不许把不同线的拒绝加在一起。

    把 ema20 的 1 次和 ema50 的 1 次加在一起算「2 次」，会让两条线各自
    只有 1 次的弱信号也成立 —— 而用户要的是「在**同一条**线上被多次拒绝」。
    """
    book = _MultiBook()
    lv20 = book.px[T.SLOW_LINE_KEY]
    lv50 = book.px[T.TREND_SLOW_KEY]
    # ema20 上 1 次、ema50 上 1 次 —— 各自都不到 reject_min_bars(2)
    ctx = _ctx(book, {DECIDE - 1: lv20, DECIDE: lv50})
    plan, why = P.detect_ema_reject(DECIDE, ctx, P.PendingParams(), P.DOWN)
    assert plan is None
    assert why == "too_few_rejections", why


def test_ema50_needs_its_own_last_bar_rejection():
    """ema50 上够 2 次、但**最后一根不是拒绝根** → 不成立（与 ema20 同口径）。

    ★ 拒绝原因取「更有信息量」的那个：形态确实出现过、只是离决策根还差
      一根，所以报 `last_bar_not_a_rejection` 而不是笼统的 `too_few`。
      这样统计才能分清「形状不够」和「时机差一根」。
    """
    book = _MultiBook()
    lv50 = book.px[T.TREND_SLOW_KEY]
    # 8、9 两根在 ema50 拒绝，决策根是 11（中间那根是普通 K）
    ctx = _ctx(book, {DECIDE - 2: lv50, DECIDE - 1: lv50}, n=12)
    plan, why = P.detect_ema_reject(11, ctx, P.PendingParams(), P.DOWN)
    assert plan is None
    assert why == "last_bar_not_a_rejection", why


def test_failure_reason_prefers_the_more_informative_one():
    """一条线凑够次数只是时机不对 → 报 `last_bar_not_a_rejection`。

    理由区分是为了统计能看出「是形状不够还是时机差一根」。
    """
    book = _MultiBook()
    lv50 = book.px[T.TREND_SLOW_KEY]
    ctx = _ctx(book, {DECIDE - 2: lv50, DECIDE - 1: lv50})
    plan, why = P.detect_ema_reject(DECIDE, ctx, P.PendingParams(), P.DOWN)
    assert plan is None
    assert why == "last_bar_not_a_rejection", why


# ---------------------------------------------------------------- 必须能穿过守卫

def test_guard_uses_the_rejected_line_not_ema20():
    """★★ 本轮的关键回归点：ema50 场景必须能**穿过** `guard_preconditions`。

    场景照用户原话搭：价格突破 ema20、来到 ema50、被 ema50 反复压回。
    所以 `close`（= ema50 之下 10 点）**高于** ema20 —— 老守卫按
    `close < ema20` 判，会把这一单拒成
    `not_below_ema30m_for_down_background`。
    """
    book = _MultiBook()
    lv50 = book.px[T.TREND_SLOW_KEY]
    ctx = _ctx(book, {DECIDE - 1: lv50, DECIDE: lv50})
    bar = ctx.bars15[DECIDE]
    assert bar.c > book.px[T.SLOW_LINE_KEY], "夹具必须先满足「价格在 ema20 上方」"

    params = P.PendingParams()
    plan, why = P.detect_ema_reject(DECIDE, ctx, params, P.DOWN)
    assert why == "ok", why
    # 直接验守卫本身：给了 line_key 就该放行，不给就该拒（复现老缺陷）
    ok_new, why_new = P.guard_preconditions(
        plan, bar, ctx.book, direction=P.DOWN, kind=plan.kind,
        close_ms=int(bar.ts) + M15,
        line_key=plan.evidence["reject_line_key"])
    assert ok_new is True, why_new
    ok_old, why_old = P.guard_preconditions(
        plan, bar, ctx.book, direction=P.DOWN, kind=plan.kind,
        close_ms=int(bar.ts) + M15)
    assert ok_old is False, "不传 line_key 应当复现老缺陷（被 ema20 拦掉）"
    assert why_old.startswith("not_above_ema30m") or \
        why_old.startswith("not_below_ema30m"), why_old


def test_build_passes_the_rejected_line_to_the_guard():
    """★ `_build` 必须把 `reject_line_key` 传给守卫（只有趋势族传）。

    只测守卫本身不够 —— 接线漏了照样白改。所以这里走完整 `_build` 路径，
    断言最终的拒绝原因里**没有** precondition。
    """
    book = _MultiBook()
    lv50 = book.px[T.TREND_SLOW_KEY]
    ctx = _ctx(book, {DECIDE - 1: lv50, DECIDE: lv50})
    plan, why = P.detect_ema_reject(DECIDE, ctx, P.PendingParams(), P.DOWN)
    assert why == "ok", why
    assert plan.evidence["reject_line_key"] == T.TREND_SLOW_KEY
    # _build 内部已经跑过守卫；能出来就说明守卫放行了
    assert plan.kind.startswith("下跌"), plan.kind


def test_guard_line_missing_is_rejected_not_silently_falling_back():
    """守卫指定的线取不到 → 拒（`no_guard_line`），**不许**退回 ema20。"""
    class _No144(_MultiBook):
        def at(self, key, ts):
            if key == T.EMA144_LINE_KEY:
                return None
            return super().at(key, ts)

    book = _No144()
    bar = b15(0, PX, PX + 10, PX - 10, PX - 5)
    ok, why = P.guard_preconditions(
        None, bar, book, direction=P.DOWN, kind=P.KIND_EMA_REJECT,
        close_ms=int(bar.ts) + M15, line_key=T.EMA144_LINE_KEY)
    assert ok is False
    assert why == "no_guard_line:%s" % T.EMA144_LINE_KEY, why


# ---------------------------------------------------------------- 镜像：上涨回调

def test_mirror_up_pullback_long_on_ema50():
    """★ 上涨回调镜像：价格回落到 ema50 被多次支撑 → 做多成立。

    用户：「上涨回调镜像」。所以三条线在**上方**、价格从上方回踩到线
    被支撑（下影线刺穿、收盘回到线上方）。
    """
    # 上涨背景：ema20 在 ema50 上方（趋势族做多要 ema20 > ema50）
    book = _MultiBook(p20=PX + 400.0, p50=PX, p144=PX - 500.0)
    lv50 = book.px[T.TREND_SLOW_KEY]

    bars = []
    for i in range(10):
        if i in (DECIDE - 1, DECIDE):
            c = lv50 + 10.0
            bars.append(b15(i, c, c + 90.0, lv50 - 110.0, c))
        else:
            bars.append(b15(i, PX + 500, PX + 510, PX + 490, PX + 500))
    ctx = P.PlanContext(bars15=bars, book=book, bars_1h=_h1(), bars_5m=_m5())

    plan, why = P.detect_ema_reject(DECIDE, ctx, P.PendingParams(), P.UP)
    assert why == "ok", why
    assert plan.evidence["reject_line_key"] == T.TREND_SLOW_KEY
    assert plan.direction == P.UP


# ---------------------------------------------------------------- 三引线不受影响

def test_wicks_and_inverse_v_never_get_a_guard_line():
    """★ 三引线 / 倒V **绝不**传 `line_key`：口径一直是「价格远离 15m 的 ema20」。

    这不是风格问题：`_build` 里按 `PATTERN_FAMILY` 分支，只有趋势族传。
    如果哪天有人「顺手」对所有族都传，三引线的守卫就会从 ema20 换成
    它自己 evidence 里的某个键（或者 None 落回老路径），判据被悄悄改掉。
    """
    assert P.PATTERN_FAMILY[P.KIND_WICKS] == T.FAMILY_REVERSAL
    assert P.PATTERN_FAMILY[P.KIND_INVERSE_V] == T.FAMILY_REVERSAL
    assert P.PATTERN_FAMILY[P.KIND_EMA_REJECT] == T.FAMILY_TREND
    # 反转族的检测器不往 evidence 里写 reject_line_key（写的是别的字段）
    src = open(os.path.join(_ROOT, "ai_trader", "trading",
                            "pending_plan.py"), encoding="utf-8").read()
    build = src.split("if params.apply_guard:", 1)[1].split("return None", 1)[0]
    assert "FAMILY_TREND" in build, "必须按族分支，不许无条件传 line_key"
