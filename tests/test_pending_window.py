# -*- coding: utf-8 -*-
"""开仓窗口（用户 2026-11-17 口径）的专项测试。

口径逐字：「就是向下顺向带量突破区间后，下一个区间形成过程中出的开仓预期」

这一组测试钉四件事：
  1. 窗口 = 顺向带量突破那根 + 紧跟的 forming 段
  2. 不带量 → 不开窗口（「带量」是硬条件，不是修饰）
  3. 突破后没有 forming 段 → 不开窗口（还在原区间里晃，不算）
  4. 方向必须配对（DOWN 的突破不能给 UP 镜像用）
  5. 窗口是**定义域**不是过滤器：窗口外连探测都不该跑
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai_trader.trading import pending_plan as P      # noqa: E402
from ai_trader.trading import thesis as T            # noqa: E402

HOUR = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % HOUR)
SOFT_DN = "range_soft_dn@1h"
SOFT_UP = "range_soft_up@1h"


class H(object):
    """1h bar。"""

    __slots__ = ("ts", "o", "h", "l", "c", "v")

    def __init__(self, ts, c, v=100.0):
        self.ts, self.o, self.h, self.l, self.c, self.v = \
            int(ts), float(c), float(c), float(c), float(c), float(v)


def hours(closes, vols=None):
    vols = vols or [100.0] * len(closes)
    return [H(T0 + k * HOUR, c, vols[k]) for k, c in enumerate(closes)]


def tagger(spec):
    """spec: {下标: (tag, direction)}；direction 用本模块常量（小写）。

    ★ 这里故意做一次「小写 → 大写」的转换，因为**真实库
      `range_evolution.direction` 就是大写**（`'DOWN'`/`'UP'`）。
      测试 helper 必须模拟库，不能直接把模块常量喂进去 ——
      否则这个大小写不匹配的 bug 在测试里永远看不见（实测踩过一次：
      窗口 7 段全部错过、计划数静默变 0、没有任何报错）。
    """
    _UP = {P.DOWN: "DOWN", P.UP: "UP"}

    def tag_of(ts):
        k = (int(ts) - T0) // HOUR
        tag, d = spec.get(k, ("", P.DOWN))
        return tag, _UP[d]
    return tag_of


def liner(spec):
    """spec: {下标: price}；返回只在该小时收盘时给值的 line_of。"""
    def line_of(ts, key):
        k = (int(ts) - T0) // HOUR
        return spec.get(k)
    return line_of


# ---------------------------------------------------------------- 窗口构建

def test_window_is_the_breakout_plus_the_forming_stretch():
    """突破那根 + 后面跟着的 forming 段 = 一段窗口。"""
    # 突破那根必须**带量**，否则窗口根本不开（见下一条测试）
    h = hours([1000.0, 1000.0, 900.0, 880.0, 870.0, 870.0],
              [1000.0, 1000.0, 9000.0, 1000.0, 1000.0, 1000.0])
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("forming", P.DOWN),
                     4: ("forming", P.DOWN)})
    wins = P.build_open_windows(h, tag_of, liner({2: 950.0}))
    assert len(wins) == 1, wins
    w = wins[0]
    assert w.direction == P.DOWN
    assert w.start_ms == T0 + 2 * HOUR, "窗口必须从突破那根开始"
    assert w.end_ms == T0 + 5 * HOUR, "窗口到 forming 段结束（右开）"
    assert w.contains(T0 + 4 * HOUR)


def test_a_breakout_without_volume_opens_no_window():
    """★「带量」是硬条件。缩量穿刺不开窗口。"""
    vols = [1000.0] * 6
    vols[2] = 50.0                    # 突破那根严重缩量
    h = hours([1000.0, 1000.0, 900.0, 880.0, 870.0, 870.0], vols)
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("forming", P.DOWN)})
    assert P.build_open_windows(h, tag_of, liner({2: 950.0})) == []


def test_a_breakout_with_volume_opens_a_window():
    """反证：同样的形状，带量就必须开窗口（否则上一条会假通过）。"""
    vols = [1000.0] * 6
    vols[2] = 5000.0
    h = hours([1000.0, 1000.0, 900.0, 880.0, 870.0, 870.0], vols)
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("forming", P.DOWN)})
    assert len(P.build_open_windows(h, tag_of, liner({2: 950.0}))) == 1


def test_no_forming_after_the_breakout_means_no_window():
    """突破了但没开始长新区间 → 还在原区间里晃，不算开仓窗口。"""
    h = hours([1000.0, 1000.0, 900.0, 880.0, 870.0, 870.0], [1000.0] * 2 + [9000.0] + [1000.0] * 3)
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("", P.DOWN), 4: ("", P.DOWN)})
    assert P.build_open_windows(h, tag_of, liner({2: 950.0})) == []


def test_a_close_that_did_not_cross_the_forward_boundary_is_not_a_breakout():
    """tag 说突破、但收盘没真的越过顺向边界 → 不开窗口（防数据不一致）。"""
    h = hours([1000.0, 1000.0, 960.0, 950.0, 940.0, 940.0], [1000.0] * 2 + [9000.0] + [1000.0] * 3)
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("forming", P.DOWN)})
    # 收回 950 上方：前收 1000 >= 950，但 close 960 不低于 950
    assert P.build_open_windows(h, tag_of, liner({2: 950.0})) == []


def test_direction_must_match_the_breakout_side():
    """UP 的突破不能给 DOWN 用 —— 方向不配对会把镜像混进来。"""
    h = hours([900.0, 900.0, 950.0, 960.0, 970.0, 970.0],
              [1000.0] * 2 + [9000.0] + [1000.0] * 3)
    tag_of = tagger({2: ("突破", P.UP), 3: ("forming", P.UP)})
    up = P.build_open_windows(h, tag_of, liner({2: 920.0}),
                              directions=(P.UP,))
    down = P.build_open_windows(h, tag_of, liner({2: 920.0}),
                                directions=(P.DOWN,))
    assert len(up) == 1 and up[0].direction == P.UP
    assert down == []


def test_both_directions_are_built_by_default():
    """用户要求镜像一起做：默认两个方向都算。"""
    h = hours([1000.0, 1000.0, 900.0, 880.0, 1050.0, 1080.0],
              [1000.0, 1000.0, 9000.0, 1000.0, 9000.0, 1000.0])
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("forming", P.DOWN),
                     4: ("突破", P.UP), 5: ("forming", P.UP)})
    wins = P.build_open_windows(h, tag_of,
                                liner({2: 950.0, 4: 1000.0}))
    assert [w.direction for w in wins] == [P.DOWN, P.UP], wins


def test_window_direction_uses_the_module_constants_not_the_db_casing():
    """★ 仓库里有两套方向词汇，必须在一个地方对应起来。

    * `range_evolution.direction`（引擎写库）= 大写 `'DOWN'` / `'UP'`
    * `thesis` / `pending_plan` 的方向常量      = 小写 `DOWN` / `UP`

    这两套直接比较**永远不相等**，而且不报错 —— 实测后果是窗口 7 段
    全部错过、计划数静默变成 0。所以这里同时钉住：
      · `build_open_windows` 接受**大写**的 tag 方向（模拟库）
      · 产出的 `OpenWindow.direction` 是**小写**模块常量（给 detect_all 用）
    """
    h = hours([1000.0, 1000.0, 900.0, 880.0],
              [1000.0, 1000.0, 9000.0, 1000.0])
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("forming", P.DOWN)})
    wins = P.build_open_windows(h, tag_of, liner({2: 950.0}))
    assert wins, "大写 tag 方向必须能被识别（库就是这么存的）"
    assert wins[0].direction == P.DOWN, (
        "窗口方向必须转成本模块常量（小写），否则 detect_all 的 "
        "in_window(ts, bg) 永远不匹配；实际 %r" % (wins[0].direction,))


# ---------------------------------------------------------------- 追加尾巴

def test_extra_bars_extend_the_window_tail():
    """★ 用户 2026-11-17：「forming 是 3 根，我想再追加 3 根」。

    追加的是**窗口尾巴**，不是引擎的 FORM_BARS。3 根 → 6 根。
    """
    # 突破 1 根 + forming 2 根，后面还有 4 根 (空)
    h = hours([1000.0, 1000.0, 900.0, 880.0, 870.0, 860.0, 850.0, 840.0],
              [1000.0, 1000.0, 9000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0])
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("forming", P.DOWN),
                     4: ("forming", P.DOWN)})
    base = P.build_open_windows(h, tag_of, liner({2: 950.0}), extra_bars=0)
    ext = P.build_open_windows(h, tag_of, liner({2: 950.0}), extra_bars=3)
    assert len(base) == 1 and len(ext) == 1
    assert base[0].end_ms == T0 + 5 * HOUR, base[0]
    assert ext[0].end_ms == T0 + 8 * HOUR, (
        "追加 3 根后应到第 8 小时；实际 %s" % (ext[0],))
    assert ext[0].start_ms == base[0].start_ms, "追加不许动窗口起点"


def test_extra_bars_never_swallow_the_next_breakout():
    """★ 追加的尾巴不许越过下一个「突破/反转」。

    越过了相邻两段窗口就重叠，而 in_window 只取第一个命中的窗口 ——
    另一段会被静默吞掉，两个方向的窗口还可能同时盖住同一根 K。
    """
    h = hours([1000.0, 1000.0, 900.0, 880.0, 870.0, 860.0, 850.0],
              [1000.0, 1000.0, 9000.0, 1000.0, 1000.0, 9000.0, 1000.0])
    # 第 5 根是新的「反转」→ 追加必须停在第 5 根之前
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("forming", P.DOWN),
                     4: ("", P.DOWN), 5: ("反转", P.UP)})
    got = P.build_open_windows(h, tag_of, liner({2: 950.0}),
                               directions=(P.DOWN,), extra_bars=5)
    assert len(got) == 1
    assert got[0].end_ms <= T0 + 5 * HOUR, (
        "窗口越过了下一个反转（会重叠）；实际 %s" % (got[0],))


def test_extra_bars_rejects_a_negative_value():
    """负数要硬报错，不许静默当成 0。"""
    with pytest.raises(ValueError, match="extra_bars"):
        P.build_open_windows(hours([1000.0, 900.0]), tagger({}), liner({}),
                             extra_bars=-1)


def test_period_ms_must_be_positive():
    with pytest.raises(ValueError, match="period_ms"):
        P.build_open_windows(hours([1000.0, 900.0]), tagger({}), liner({}),
                             period_ms=0)


def test_windows_do_not_overlap_with_extra_bars():
    """多段突破 + 追加时，任意两段窗口都不许重叠。"""
    closes = [1000.0] * 3 + [900.0, 880.0, 870.0] + [1000.0] * 3 + \
             [1100.0, 1120.0, 1130.0] + [1000.0] * 3
    vols = [1000.0] * len(closes)
    for i in (3, 9):
        vols[i] = 9000.0
    h = hours(closes, vols)
    tag_of = tagger({3: ("突破", P.DOWN), 4: ("forming", P.DOWN),
                     5: ("forming", P.DOWN),
                     9: ("突破", P.UP), 10: ("forming", P.UP),
                     11: ("forming", P.UP)})
    got = P.build_open_windows(h, tag_of,
                               liner({3: 950.0, 9: 1050.0}), extra_bars=3)
    assert len(got) == 2, got
    a, b = got
    assert a.end_ms <= b.start_ms, (
        "两段窗口重叠了：%s / %s —— in_window 会静默吞掉后一段" % (a, b))


# ---------------------------------------------------------------- 上下文

def test_context_with_no_windows_given_does_not_filter():
    """没提供窗口 = 只验形状（单元测试用），不做过滤。"""
    ctx = P.PlanContext([], None, windows=None)
    assert ctx.in_window(123456) is True


def test_context_with_empty_windows_filters_everything_out():
    """★ [] 和 None 语义不同：[] 表示「这段时间真的没有开仓机会」。

    合并成一个默认值，会让「窗口算成了空」被读成「不需要窗口」，
    于是又回到在任意位置出计划的状态 —— 正是本轮要修的 bug。
    """
    ctx = P.PlanContext([], None, windows=[])
    assert ctx.in_window(123456) is False


def test_detect_all_refuses_to_run_without_windows_when_required():
    """生产路径必须硬失败，而不是静默在任意位置出计划。"""
    ctx = P.PlanContext([], None, windows=None)
    with pytest.raises(ValueError, match="开仓窗口"):
        P.detect_all(ctx, require_window=True)


def test_a_breakout_tag_of_the_opposite_direction_opens_no_window():
    """★ `tag='突破'` 必须**连同 direction 一起**匹配。

    只判 `tag == TAG_BREAKOUT` 不判方向，等于把涨破（镜像的摸顶）当成
    下跌突破 —— 实测倒V 距 ema20@15m 的中位会从 +0.124% 变成
    +0.278%（更差），因为它把两种相反的打法混成了一个形态。

    ★ 数据必须这样搭才能真的区分：突破那根 tag 记 **UP**（方向不对），
      但后面的 forming 段方向是 **DOWN**（方向对）。
      这样：
        · 正确实现 → 突破方向不匹配 → 不开窗口；
        · 去掉方向检查 → 突破通过，forming 也通过 → 就会错误地开窗口。
      第一版把两处都写成 UP，于是两种实现结果都是空，测不出区别。
    """
    h = hours([1000.0, 1000.0, 900.0, 880.0, 870.0],
              [1000.0, 1000.0, 9000.0, 1000.0, 1000.0])
    tag_of = tagger({2: ("突破", P.UP), 3: ("forming", P.DOWN)})
    got = P.build_open_windows(h, tag_of, liner({2: 950.0}),
                               directions=(P.DOWN,))
    assert got == [], (
        "方向不匹配的突破不该给 DOWN 开窗口；实际 %r" % (got,))
    """★ 窗口的 forming 段必须**同方向**。

    只判 `tag == 'forming'` 不判方向，会让「下跌突破」的窗口被一段
    上涨方向的 forming 延长 —— 那等于把镜像的区间算进抄底的窗口里。
    """
    h = hours([1000.0, 1000.0, 900.0, 880.0, 870.0],
              [1000.0, 1000.0, 9000.0, 1000.0, 1000.0])
    # 突破是 DOWN，但后面那段 forming 是 UP
    tag_of = tagger({2: ("突破", P.DOWN), 3: ("forming", P.UP)})
    assert P.build_open_windows(h, tag_of, liner({2: 950.0})) == []


# ---------------------------------------------------------------- 镜像

def _breakout_ctx(window_dirs):
    """造一个最小上下文：15m 序列 + 指定方向的窗口。"""
    bars = [H(T0 + k * 900_000, 100.0) for k in range(40)]
    wins = [P.OpenWindow(T0, T0 + 8 * HOUR, d) for d in window_dirs]
    return P.PlanContext(bars, T.LineBook(), windows=wins)


def test_include_mirror_runs_both_directions():
    """★ 用户口径「上涨时是镜像的」：必须显式跑镜像版。

    不打开 include_mirror 只会跑基准版（三条基准版都在**下跌**背景），
    于是上涨行情里永远不出计划 —— 而且不会报错。
    """
    ctx = _breakout_ctx([P.DOWN, P.UP])
    _, base = P.detect_all(ctx, require_window=True, include_mirror=False)
    _, both = P.detect_all(ctx, require_window=True, include_mirror=True)
    base_labels = {k.split(":")[0] for k in base}
    both_labels = {k.split(":")[0] for k in both}
    assert both_labels > base_labels, (
        "include_mirror=True 没有多跑任何组合：base=%r both=%r"
        % (base_labels, both_labels))
    # 每一对「下跌…」/「上涨…」都该出现
    for label in both_labels:
        assert ("下跌" in label or "上涨" in label), label
    assert any("下跌" in x for x in both_labels)
    assert any("上涨" in x for x in both_labels)


def test_background_direction_is_down_for_the_base_version():
    """★ 基准版必须在**下跌**背景下检测（抄底与反弹做空的背景都是下跌）。

    把 bg 算反了，基准版会去上涨窗口里找形态 —— 结果是一条都不出，
    但 stats 里全是 outside_window，看起来像「这段时间没机会」。
    """
    bars = [H(T0 + k * 900_000, 100.0) for k in range(40)]
    only_down = P.PlanContext(
        bars, T.LineBook(),
        windows=[P.OpenWindow(T0, T0 + 8 * HOUR, P.DOWN)])
    only_up = P.PlanContext(
        bars, T.LineBook(),
        windows=[P.OpenWindow(T0, T0 + 8 * HOUR, P.UP)])
    _, sd = P.detect_all(only_down, require_window=True)
    _, su = P.detect_all(only_up, require_window=True)
    # 基准版（下跌…）在 DOWN 窗口里会被**真正检测**（有各种形态原因计数），
    # 在 UP 窗口里则只会有 outside_window。
    down_reasons = {k.split(":")[1] for k in sd if "下跌" in k}
    up_reasons = {k.split(":")[1] for k in su if "下跌" in k}
    assert down_reasons - {"outside_window"}, (
        "基准版在 DOWN 窗口里没有被检测：%r" % (down_reasons,))
    assert up_reasons == {"outside_window"}, (
        "基准版竟然在 UP 窗口里被检测了（背景方向算反）：%r" % (up_reasons,))


# ---------------------------------------------------------------- 极性

def test_guard_uses_the_pattern_position_not_just_the_direction():
    """★ 抄底做多要求价格在 ema **下方**；旧的「只看方向」恰好写反。

    这个 bug 的后果：位置对的做多全被拦死、位置错的放行，而且不报错。
    """
    from ai_trader.trading import thesis as T
    import collections
    Bar = collections.namedtuple("Bar", "ts o h l c v")
    # 价格 100，快线 200 → 价格在线**下方**
    # ★ ema20@30m 的 ts 要按 30m 桶对齐：它的 ready_ms = ts + 30m，
    #   而 guard 取的是 bar 收盘（T0+15m）时「已就绪」的值。
    #   第一版按 15m 步长放线，ready_ms = T0+15m+30m > T0+15m → 判成
    #   no_fast_line，测到的是「线没准备好」而不是极性。
    M30 = 1_800_000
    book = T.LineBook()
    base = T0 - (T0 % M30)
    for k in range(6):
        ts = base - (k + 2) * M30          # 全部在 bar 之前就已收盘
        book.add({"line_key": T.FAST_LINE_KEY, "ts": ts,
                  "line_price": 200.0,
                  "line_ready_ms": ts + M30})
    bar = Bar(T0, 100.0, 101.0, 99.0, 100.0, 1.0)
    kind = P._variant_name(P.KIND_WICKS, P.UP)      # 下跌三引线（抄底做多）
    ok, why = P.guard_preconditions(None, bar, book, direction=P.UP, kind=kind)
    assert ok, (
        "抄底做多在 ema20@30m 下方是对的（§23.2），却被拦成 %r —— "
        "这是把极性写反了" % (why,))
    """窗口外的跳过必须**记账**（禁止静默过滤）。"""
    bars = [H(T0 + k * 900_000, 100.0) for k in range(8)]
    ctx = P.PlanContext(bars, None, windows=[])
    plans, stats = P.detect_all(ctx, require_window=True)
    assert plans == []
    assert any("outside_window" in k for k in stats), stats

