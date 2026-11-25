# -*- coding: utf-8 -*-
"""`ai_trader/kbar/shape.py` 的测试。

这个模块是「把客观值变成判断」的那一层，所以测试必须钉住三件事：

  1. **判据本身**：每个条件单独不成立时，结论必须变。只测「全满足时成立」
     等于没测 —— 这个项目反复出现「条件根本没接线」（`and` 写成 `or`，
     或某个条件在短路后永远走不到）。
  2. **假严谨也要挡**：曾经有第三个条件 `close_pos >= 0.65`，数学上恒真，
     加进去等于没加。测试里有一条专门钉这个推导，防止有人再把它加回来。
  3. **口径不许漂**：`SCENARIO_TOL_PTS` 只在一处用，且不许碰
     `reaction.TOUCH_TOL_PCT`。

最后是**真数据锚点**：用户给的两根（北京 23:45 插针、05:15 稳住）是验收标准。
库不在就 skip，不假装通过。
"""
from __future__ import annotations

import datetime
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import shape as S  # noqa: E402


def bar(o, h, l, c, ts=None, vol_rel=None, taker_buy_ratio=None):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c,
            "vol_rel": vol_rel, "taker_buy_ratio": taker_buy_ratio}


def down_pin_bars(n_after=0, base=10000.0):
    """造一根下针：O=base H=base+5 L=base-100 C=base-20。针低 = base-100，针幅 105。"""
    out = [bar(base, base + 5, base - 100, base - 20)]
    for _k in range(n_after):
        out.append(bar(base - 18, base - 12, base - 22, base - 14))
    return out


def follow_bar(low, close, ts=None, wick_frac=0.05):
    """造一根「跟随根」：**开盘贴着最低价**，所以下影很短，本身不是针。

    真实数据里针后那根就是这样（05:15：O=75586.1 L=75575.9，下影只有 10.2 点）。
    如果开盘离最低很远，这根自己就会变成一根针，`after_pin` 会判 None ——
    构造测试夹具时这一点特别容易踩，所以单独提成一个函数并写清理由。
    """
    o = low + 5.0
    span = max(close - low, 100.0)
    h = max(close, o) + span * wick_frac
    return bar(o, h, low, close, ts=ts)


# ---------------------------------------------------------------- 几何
def test_geometry_wicks_and_close_pos_on_a_real_shaped_bar():
    """三个比例加起来必须等于 1 —— 这条挡住影线算错（比如用了错的 max/min）。"""
    g = S.geometry(100.0, 130.0, 90.0, 110.0)
    assert g.rng == 40.0
    assert g.body == 10.0
    assert g.up == 20.0      # 130 - max(100,110)
    assert g.dn == 10.0      # min(100,110) - 90
    assert g.up_pct == pytest.approx(0.5)
    assert g.dn_pct == pytest.approx(0.25)
    assert g.close_pos == pytest.approx(0.5)
    assert g.body / g.rng + g.up_pct + g.dn_pct == pytest.approx(1.0)


def test_geometry_survives_a_zero_range_bar():
    """一字板（h == l）不能除零，也不能把影线算成负数。"""
    g = S.geometry(100.0, 100.0, 100.0, 100.0)
    assert g.rng > 0
    assert g.up == 0.0 and g.dn == 0.0
    assert 0.0 <= g.close_pos <= 1.0


def test_geometry_clamps_negative_wicks_from_float_noise():
    """浮点残差会让 up/dn 掉到 -1e-13，必须钳回 0（否则 >=0 的判断会失效）。"""
    g = S.geometry(100.0, 100.0, 99.999999999999, 100.0)
    assert g.dn >= 0.0 and g.up >= 0.0
    # 脏数据（l 高于收盘）也不能算出负影线 —— 负影线会让占幅变成负数，
    # 后面所有 >= 比较都会静默通过。
    bad = S.geometry(100.0, 100.0, 100.5, 100.0)
    assert bad.dn == 0.0, bad
    bad2 = S.geometry(100.0, 99.0, 98.0, 100.0)     # h < c
    assert bad2.up == 0.0, bad2


# ---------------------------------------------------------------- 插针：两个条件
def test_pin_down_needs_both_ratio_and_points():
    """两个条件各自单独拉掉，结论都必须变。"""
    # 全满足：下影 100/105
    ok = S.geometry(10000.0, 10005.0, 9900.0, 9990.0)
    assert S.pin_side(ok) == S.SIDE_DOWN
    # 点数不够：比例很漂亮（下影占全幅 91%），但只有 1 点
    tiny = S.geometry(100.0, 100.1, 99.0, 100.0)
    assert tiny.dn_pct >= S.WICK_PCT_MIN
    assert tiny.dn < S.WICK_PTS_MIN
    assert S.pin_side(tiny) is None
    # 点数够，比例不够：下影 50 点，全幅 550 -> 9%
    wide = S.geometry(10000.0, 10500.0, 9950.0, 10400.0)
    assert wide.dn >= S.WICK_PTS_MIN
    assert wide.dn_pct < S.WICK_PCT_MIN
    assert S.pin_side(wide) is None
    # 点数与比例都不够
    flat = S.geometry(10020.0, 10030.0, 10000.0, 10010.0)   # 下影 10，全幅 30
    assert flat.dn_pct == pytest.approx(10.0 / 30.0)
    assert flat.dn < S.WICK_PTS_MIN
    assert S.pin_side(flat) is None


def test_pin_up_uses_the_upper_wick_only():
    """上针只看上影。上影不够就不算，哪怕下影很长。"""
    up = S.geometry(10000.0, 10100.0, 9995.0, 10010.0)   # 上影 90，全幅 105
    assert S.pin_side(up) == S.SIDE_UP
    up_small = S.geometry(10000.0, 10020.0, 9900.0, 10010.0)  # 上影 10，下影 100
    assert up_small.up < S.WICK_PTS_MIN
    assert S.pin_side(up_small) == S.SIDE_DOWN           # 报下针，不是上针


def test_close_pos_condition_is_mathematically_redundant():
    """把 `close_pos >= X` 当第三个条件加进来，是假严谨。

    推导：c >= min(o,c) => c - l >= min(o,c) - l => 收位 >= 下影占幅。
    所以只要占幅条件成立，收位条件必然成立 —— 它永远不会把任何一根排除掉。
    这条测试**证明它恒真**，防止有人再把死条件加回去。
    """
    probes = [
        (10000.0, 10005.0, 9900.0, 9990.0),     # 收在下方
        (10000.0, 10100.0, 9995.0, 10010.0),    # 长上影
        (75800.0, 75800.0, 75562.0, 75728.0),   # 收在最低上方一点点
        (100.0, 101.0, 90.0, 99.9),
        (100.0, 101.0, 90.0, 90.1),             # 几乎收在最低
    ]
    for o, h, l, c in probes:
        g = S.geometry(o, h, l, c)
        assert g.close_pos >= g.dn_pct - 1e-12, (o, h, l, c)
        assert (1.0 - g.close_pos) >= g.up_pct - 1e-12, (o, h, l, c)


def test_pin_side_is_deterministic_for_any_shape():
    """同一根永远得到同一个答案；上下影不可能同时过线（三个比例加起来是 1）。"""
    g = S.geometry(10000.0, 10300.0, 9700.0, 10000.0)
    assert g.dn_pct + g.up_pct + g.body / g.rng == pytest.approx(1.0)
    assert S.pin_side(g) == S.pin_side(g)
    # 2 * 0.65 > 1，所以两边不可能同时 >= 0.65
    assert not (g.dn_pct >= S.WICK_PCT_MIN and g.up_pct >= S.WICK_PCT_MIN)


def test_pin_thresholds_are_inclusive_boundaries():
    """恰好等于阈值必须算过（>= 不是 >）。这条挡住「差一点点」的静默偏移。"""
    pts, pct = S.WICK_PTS_MIN, S.WICK_PCT_MIN
    rng = pts / pct                     # 让下影恰好 = 40
    l = 1000.0
    h = l + rng
    c = l + pct * rng                   # 收位恰好 = pct
    o = h                               # 开在最高，下影 = c - l
    g = S.geometry(o, h, l, c)
    assert g.dn == pytest.approx(pts, rel=1e-9)
    assert g.dn_pct == pytest.approx(pct, rel=1e-9)
    assert S.pin_side(g) == S.SIDE_DOWN


# ---------------------------------------------------------------- 线收回
def test_recovered_lines_needs_a_pierce_and_a_close_back():
    """三种情况必须分开：穿透后收回 / 穿透不收回 / 没穿透只接近。"""
    g = S.geometry(100.0, 105.0, 90.0, 101.0)   # 开盘 100
    lines = {"a@1h": 95.0, "b@1h": 103.0, "c@1h": 89.0}
    got = S.recovered_lines(g, lines)
    # a: o=100 > 95，90 < 95 < 101 -> 下穿后收回
    # b: o=100 < 103，101 < 103 < 105 -> 上穿后收回
    # c: 89 < 最低 90 -> 没碰到
    assert [(k, s) for k, _p, s in got] == [("a@1h", S.SIDE_DOWN),
                                            ("b@1h", S.SIDE_UP)]


def test_recovered_lines_rejects_a_pierce_that_closed_on_the_wrong_side():
    """穿下去了但收盘还在线下 = 没收回。这是最容易写成「都算收回」的地方。"""
    g = S.geometry(100.0, 105.0, 90.0, 94.0)     # 收盘 94 < 线价 95
    assert S.recovered_lines(g, {"a@1h": 95.0}) == []


def test_recovered_lines_requires_a_strict_pierce_not_a_touch():
    """线价正好等于最低/最高价 = 只是擦到，不算「穿透」。

    用闭区间的话，一根最低刚好压在线上的 K 会被记成「穿下去又收回」，
    而它其实连线下都没到过。
    """
    # 最低正好等于线价 90
    g = S.geometry(100.0, 105.0, 90.0, 101.0)
    assert S.recovered_lines(g, {"a@1h": 90.0}) == []
    # 最高正好等于线价 105
    g2 = S.geometry(100.0, 105.0, 90.0, 95.0)
    assert S.recovered_lines(g2, {"b@1h": 105.0}) == []


def test_recovered_lines_uses_open_price_to_decide_the_side():
    """从哪侧来用开盘价判（D-6 口径）。

    开盘就在线上（o == p）时不判 —— 没有「从哪侧来」可言，宁可漏也不编。
    若改用「本根穿没穿」而不看开盘价，一根跨线的 K 会两头都算。
    """
    # 开盘正好在线上
    g = S.geometry(95.0, 105.0, 90.0, 101.0)
    assert S.recovered_lines(g, {"a@1h": 95.0}) == []
    # 开盘在线上方、收盘也在上方，最低穿过 -> 下穿后收回
    g2 = S.geometry(99.0, 105.0, 90.0, 101.0)
    assert [s for _k, _p, s in S.recovered_lines(g2, {"a@1h": 95.0})] == [S.SIDE_DOWN]


def test_recovered_lines_without_prev_snapshot_says_nothing():
    """没有上一根的线快照时不许猜。空 dict 与 None 都要返回空。"""
    g = S.geometry(100.0, 105.0, 90.0, 101.0)
    assert S.recovered_lines(g, None) == []
    assert S.recovered_lines(g, {}) == []


def test_recovered_lines_ignores_none_prices():
    """表 0 里线价可能是 NULL（结构性缺值），不许让它把比较炸掉。"""
    g = S.geometry(100.0, 105.0, 90.0, 101.0)
    assert S.recovered_lines(g, {"a@1h": None, "b@1h": 95.0}) == \
        [("b@1h", 95.0, S.SIDE_DOWN)]


# ---------------------------------------------------------------- 恐慌盘
def test_panic_needs_volume_and_aggression_not_just_a_pin():
    """三个条件缺一不可：不是针 / 量不够 / 主动卖不占优，都要判 False。"""
    g = S.geometry(10000.0, 10005.0, 9900.0, 9990.0)
    assert S.pin_side(g) == S.SIDE_DOWN
    assert S.is_panic(g, 5.0, 0.40) is True
    assert S.is_panic(g, 2.9, 0.40) is False      # 量不够
    assert S.is_panic(g, 5.0, 0.46) is False      # 卖方不占优
    not_pin = S.geometry(10000.0, 10010.0, 9990.0, 10005.0)
    assert S.is_panic(not_pin, 9.9, 0.01) is False


def test_panic_returns_false_when_data_is_missing_but_judge_explains():
    """量能缺失时判 False，但证据里必须说明「缺失」而不是「量不够」。"""
    g = S.geometry(10000.0, 10005.0, 9900.0, 9990.0)
    assert S.is_panic(g, None, 0.40) is False
    assert S.is_panic(g, 5.0, None) is False
    j = S.judge([bar(10000, 10005, 9900, 9990)], 0)
    assert j.has(S.STATE_PIN)
    assert not j.has(S.STATE_PANIC)
    assert "缺失" in " ".join(j.evidence)


def test_panic_up_pin_uses_the_mirror_side():
    """上针的恐慌是「买方占优」，口径 = 1 - PANIC_TAKER_BUY_MAX。"""
    g = S.geometry(10000.0, 10100.0, 9995.0, 10010.0)
    assert S.pin_side(g) == S.SIDE_UP
    assert S.is_panic(g, 5.0, 0.60) is True
    assert S.is_panic(g, 5.0, 0.40) is False


# ---------------------------------------------------------------- 稳住
def test_find_pin_before_excludes_the_current_bar():
    """找针只看 i 之前。含 i 会让「针」和「稳住」重合，语义就废了。"""
    bars = down_pin_bars(n_after=2)
    assert S.find_pin_before(bars, 0) is None
    assert S.find_pin_before(bars, 1, look=3) == 0
    assert S.find_pin_before(bars, 0, look=3) is None
    # 关键：当 i 自己就是一根针时，也绝不能返回 i。
    # （只测 i=0 抓不到这个错 —— i=0 有 `i <= 0` 的早退兜着。）
    pin_here = down_pin_bars(n_after=1)      # bars[1] 不是针
    pin_here[1] = bar(10000.0, 10005.0, 9900.0, 9990.0)   # 把 bars[1] 也改成针
    assert S.find_pin_before(pin_here, 1, look=3) == 0    # 应返回前一根，不是自己


def test_find_pin_before_respects_lookback_window():
    """超出回看窗口就不算，窗口边界要精确（差一根就变）。"""
    bars = down_pin_bars(n_after=4)
    assert S.find_pin_before(bars, 3, look=3) == 0     # i-look = 0，含 0
    assert S.find_pin_before(bars, 4, look=3) is None  # i-look = 1，0 出窗


def test_find_pin_before_matches_direction():
    """找下针不能返回上针。方向错了整条判据都错。"""
    up_pin = bar(10000.0, 10100.0, 9995.0, 10010.0)
    assert S.find_pin_before([up_pin, bar(10010, 10015, 10005, 10012)], 1,
                             side=S.SIDE_DOWN) is None
    assert S.find_pin_before([up_pin, bar(10010, 10015, 10005, 10012)], 1,
                             side=S.SIDE_UP) == 0


def test_after_pin_rejects_a_bar_that_is_itself_a_pin():
    """自己是针就不参与「针后稳住」。否则同一根既是针又是稳住。"""
    bars = down_pin_bars(n_after=0)
    assert S.after_pin(bars, 0) is None


def test_after_pin_rejects_a_bar_that_breaks_the_pin_low():
    """破了针低就不是稳住，是继续走。"""
    bars = down_pin_bars(n_after=0)
    bars.append(bar(9982, 9988, 9890, 9980))      # 最低 9890 < 针低 9900
    assert S.after_pin(bars, 1) is None


def test_after_pin_tolerance_is_what_lets_a_near_miss_count():
    """容差就是这里的作用：差 10 点没破针低，容差 0 不算、容差 20 算。"""
    bars = down_pin_bars(n_after=0)          # 针低 = 9900
    bars.append(follow_bar(9890.0, 9920.0))  # 最低 9890，比针低低 10 点
    assert S.after_pin(bars, 1, tol_pts=0.0) is None
    got = S.after_pin(bars, 1, tol_pts=20.0)
    assert got is not None and got[0] == 0


def test_after_pin_rejects_a_weak_close():
    """没破针低但收在全幅最低部，就不是「稳住」。"""
    bars = down_pin_bars(n_after=0)
    # 最低 9905（没破 9900），收 9906 = 全幅最低，且收阴
    bars.append(bar(9950.0, 9952.0, 9905.0, 9906.0))
    assert S.after_pin(bars, 1) is None


def test_after_pin_rejects_a_bar_with_no_pin_before_it():
    """前面没有针，「稳住」没有主语。"""
    bars = [bar(10000, 10020, 9990, 10015), bar(10010, 10030, 10000, 10025)]
    assert S.after_pin(bars, 1) is None


def test_stable_vs_far_from_pin_uses_the_pin_range_as_the_ruler():
    """「稳住」允许离针 1 倍针幅，超出改判「针后远离」。

    针：O=10000 H=10005 L=9885 C=10010 -> 全幅 120，针低 9885。
    """
    pin = bar(10000.0, 10005.0, 9885.0, 10010.0)
    near = follow_bar(9945.0, 10060.0)   # 距针 60  = 0.5 倍针幅
    far = follow_bar(10035.0, 10150.0)   # 距针 150 = 1.25 倍针幅
    assert S.after_pin([pin, near], 1) == (0, S.STATE_STABLE)
    assert S.after_pin([pin, far], 1) == (0, S.STATE_NEAR_PIN)


def test_stable_of_only_returns_the_stable_case():
    """`stable_of` 是「只要稳住」时的窄接口：针后远离必须返回 None。"""
    pin = bar(10000.0, 10005.0, 9885.0, 10010.0)
    far = follow_bar(10035.0, 10150.0)
    assert S.stable_of([pin, far], 1) is None
    near = follow_bar(9945.0, 10060.0)
    assert S.stable_of([pin, near], 1) == 0


def test_the_pin_range_ruler_scales_with_the_pin_not_with_the_price():
    """同样的跟随幅度，跟着大针算稳住、跟着小针算远离 —— 证明尺是针幅不是固定点数。"""
    big = bar(10000.0, 10005.0, 9800.0, 10000.0)     # 针幅 205，针低 9800
    small = bar(10000.0, 10005.0, 9950.0, 10000.0)   # 针幅 55，针低 9950
    # 距大针低 220 点 = 1.07 倍针幅 -> 远离
    follow_big = follow_bar(10020.0, 10120.0)
    assert S.after_pin([big, follow_big], 1)[1] == S.STATE_NEAR_PIN
    # 距小针低 40 点 = 0.73 倍针幅 -> 稳住
    follow_small = follow_bar(9990.0, 10040.0)
    assert S.after_pin([small, follow_small], 1)[1] == S.STATE_STABLE


def test_a_medium_distance_is_far_from_a_small_pin_but_near_a_big_pin():
    """同一根跟随根，配小针判「远离」、配大针判「稳住」。

    两根针的**针低相同（9950）**，只有全幅不同：小针 55、大针 210。
    跟随根最低 10030，距针低 80 点 ——
        小针：80 > 55   -> 针后远离
        大针：80 <= 210 -> 插针后稳住
    这条专门挡住「用固定点数代替针幅」的实现：上限若写死 100 点，两个都会判稳住。
    """
    small = bar(10000.0, 10005.0, 9950.0, 10000.0)    # 下影 50，全幅 55
    big = bar(10120.0, 10160.0, 9950.0, 10150.0)      # 下影 170，全幅 210
    assert S.pin_side(S.geometry(10000.0, 10005.0, 9950.0, 10000.0)) == S.SIDE_DOWN
    assert S.pin_side(S.geometry(10120.0, 10160.0, 9950.0, 10150.0)) == S.SIDE_DOWN
    follow = follow_bar(10030.0, 10090.0)             # 距针低 80 点
    assert S.after_pin([small, follow], 1)[1] == S.STATE_NEAR_PIN
    assert S.after_pin([big, follow], 1)[1] == S.STATE_STABLE


# ---------------------------------------------------------------- judge 合成
def test_judge_label_follows_the_priority_order():
    """一根既是插针收回又是恐慌盘时，label 报恐慌盘（优先级最高）。"""
    bars = [bar(10000.0, 10005.0, 9900.0, 9990.0, vol_rel=5.0,
                taker_buy_ratio=0.40)]
    j = S.judge(bars, 0, prev_lines={"a@1h": 9950.0})
    assert j.has(S.STATE_PIN) and j.has(S.STATE_PIN_RECOVERED)
    assert j.has(S.STATE_PANIC)
    assert j.label == S.STATE_PANIC


def test_judge_reports_states_in_the_canonical_priority_order():
    """`states` 的顺序是固定的展示契约：按 `STATE_ORDER` 排，不按追加顺序。

    `show_kbar` 会原样打印「同时成立：恐慌盘、插针收回、插针」，
    顺序乱掉后人读到的重点就变了；且任何按 states 做 diff 的地方都会假报警。
    """
    bars = [bar(10000.0, 10005.0, 9900.0, 9990.0, vol_rel=5.0,
                taker_buy_ratio=0.40)]
    j = S.judge(bars, 0, prev_lines={"a@1h": 9950.0})
    idx = [S.STATE_ORDER.index(s) for s in j.states]
    assert idx == sorted(idx), j.states
    assert j.states == (S.STATE_PANIC, S.STATE_PIN_RECOVERED, S.STATE_PIN)


def test_judge_says_none_for_an_ordinary_bar():
    """普通 K 线必须老实报「无」。设计稿 §8.2：强行贴标签就是在制造噪声。"""
    bars = [bar(10000.0, 10020.0, 9990.0, 10015.0, vol_rel=1.0,
                taker_buy_ratio=0.5)]
    j = S.judge(bars, 0)
    assert j.label == S.STATE_NONE
    # 「无」不是一种形态，不该出现在 states 里 —— 它只出现在 label。
    # 早先 states=(STATE_NONE,) 会让人以为「无」是六种形态之一，
    # 落库后 states 与 label 也对不上（表 5 存的是空串）。
    assert j.states == ()
    assert not j.has(S.STATE_NONE)
    assert j.side is None


def test_judge_accepts_negative_index_and_rejects_out_of_range():
    bars = down_pin_bars(n_after=1)
    assert S.judge(bars, -1).label in (S.STATE_STABLE, S.STATE_NEAR_PIN)
    with pytest.raises(IndexError):
        S.judge(bars, 99)
    with pytest.raises(IndexError):
        S.judge(bars, -99)


def test_judge_on_empty_bars_does_not_crash():
    j = S.judge([], 0)
    assert j.label == S.STATE_NONE
    assert "没有 K 线数据" in j.evidence


def test_judge_is_pure():
    """同一个输入跑两次必须完全一样（不许藏状态）。"""
    bars = [bar(10000.0, 10005.0, 9900.0, 9990.0, vol_rel=5.0,
                taker_buy_ratio=0.40)]
    a = S.judge(bars, 0, prev_lines={"a@1h": 9950.0})
    b = S.judge(bars, 0, prev_lines={"a@1h": 9950.0})
    assert a == b


def test_judge_evidence_is_never_empty():
    """每种 label 都要有证据串。空证据 = 读者只能相信结论。"""
    cases = [
        ([bar(10000, 10005, 9900, 9990, vol_rel=5.0, taker_buy_ratio=0.4)], {}),
        ([bar(10000, 10005, 9900, 9990)], {"a@1h": 9950.0}),
        ([bar(10000, 10005, 9900, 9990)], {}),
        (down_pin_bars(n_after=1), {}),
        ([bar(10000, 10020, 9990, 10015)], {}),
    ]
    for bars, lines in cases:
        j = S.judge(bars, len(bars) - 1, prev_lines=lines)
        assert j.evidence, j.label


def test_judge_reports_pin_ts_only_for_stable_or_near():
    """`pin_ts` 只在这根「在针之后」时才有值，针自己是 None。"""
    pin = bar(10000.0, 10005.0, 9885.0, 10010.0, ts=1)
    near = follow_bar(9945.0, 10060.0, ts=2)
    assert S.judge([pin], 0).pin_ts is None
    assert S.judge([pin, near], 1).pin_ts == 1


def test_as_dict_round_trips_every_field():
    """`as_dict` 是给工具/人看的输出格式，字段不许漏。"""
    j = S.judge([bar(10000.0, 10005.0, 9900.0, 9990.0)], 0)
    d = j.as_dict()
    # 「本根的形态」与「它稳的那根针的形态」是两个字段，不许合并回一个；
    # 「参照针的方向」也必须单列 —— 佐证层要靠它判「守住没」，而表 5 的
    # side 列只在「本根自己是针」时才有值（v3 补的，理由同表 3 的 held_side）。
    assert set(d) == {"states", "label", "side", "evidence", "pin_ts",
                      "pin_kind", "ref_pin_kind", "ref_pin_side"}, d.keys()
    assert d["states"] == list(j.states)
    assert d["label"] == j.label
    # as_dict 必须与 NamedTuple 逐字段一致 —— 加了字段忘了加进 dict，工具就读不到
    assert set(d) == set(j._fields)


def test_counts_by_label_tallies_each_judgement_once():
    bars = [bar(10000, 10005, 9900, 9990), bar(10000, 10020, 9990, 10015)]
    js = [S.judge(bars, 0), S.judge(bars, 1)]
    c = S.counts_by_label(js)
    assert sum(c.values()) == 2


# ---------------------------------------------------------------- 口径隔离
def test_the_scenario_tolerance_does_not_touch_reaction_touch_tol():
    """用户原话：「我设置的这个容差只在这一个场景生效！别扩散！」

    表 2 的入表条件是 `reaction.TOUCH_TOL_PCT`，它是 0.0。
    shape 的容差是另一个数，两者不许被写成同一个值。
    """
    from ai_trader.kbar import reaction
    assert reaction.TOUCH_TOL_PCT == 0.0
    assert S.SCENARIO_TOL_PTS == 20.0
    assert S.SCENARIO_TOL_PTS != reaction.TOUCH_TOL_PCT


def test_scenario_tolerance_is_wired_to_tol_pts_only():
    """扫描源码：`SCENARIO_TOL_PTS` 只许出现在常量定义、`tol_pts` 默认参数、
    以及 `params()` 里那一个**只用于上报**的 dict 键。

    一旦它被塞进 `pin_side` / `recovered_lines` / `is_panic`，
    容差就扩散了 —— 那三个判据的文档明确说它们不带容差。

    `"scenario_tol_pts": SCENARIO_TOL_PTS` 是允许的：它的用途是把口径写进
    kbar_meta 让人能读，不参与任何判断。但必须钉住「上报的值就是常量本身」，
    否则上报一份、判断用另一份，比不报还糟。
    """
    path = os.path.join(ROOT, "ai_trader", "kbar", "shape.py")
    with open(path, encoding="utf-8") as f:
        src = f.read().split("\n")
    # 只看代码行（跳过 docstring / 注释里的提及）
    in_doc = False
    report_lines = 0
    for n, ln in enumerate(src, 1):
        if ln.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or ln.strip().startswith("#"):
            continue
        if "SCENARIO_TOL_PTS" not in ln:
            continue
        s = ln.strip()
        if s.startswith('"scenario_tol_pts":'):
            report_lines += 1
            continue
        ok = (s.startswith("SCENARIO_TOL_PTS")
              or s.startswith("tol_pts")
              or s.startswith("if tol_pts"))
        assert ok, "第 %d 行把容差接到了别处：%s" % (n, s)
    assert report_lines == 1, (
        "只该有 params() 里那一个上报键，实际 %d 处" % report_lines)
    # 上报的值必须就是判断用的那个常量 —— 两处各写一个数就会漂
    assert S.params()["scenario_tol_pts"] == S.SCENARIO_TOL_PTS


def test_shape_does_not_import_forward():
    """第 5 步的事后信息不许混进当下读数（设计稿 §六：回测会偷未来）。"""
    path = os.path.join(ROOT, "ai_trader", "kbar", "shape.py")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    code = [ln.strip() for ln in lines
            if ln.strip() and not ln.strip().startswith("#")]
    # 只检查真正的 import 语句，不检查 docstring 里的名字
    imports = [ln for ln in code
               if ln.startswith("import ") or ln.startswith("from ")]
    for ln in imports:
        assert "forward" not in ln, "shape.py 不许 import forward：%s" % ln
    assert not hasattr(S, "forward")


# ---------------------------------------------------------------- 真数据锚点
DB = os.path.join(ROOT, "data", "kbar.db")
_CACHE = {}


def _load_real():
    """从 data/kbar.db 读真数据。库不在就 skip（不是失败）。"""
    if "bars" in _CACHE:
        return _CACHE["bars"], _CACHE["lines"]
    if not os.path.exists(DB):
        pytest.skip("没有 data/kbar.db")
    import sqlite3
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    rows = list(con.execute(
        "select ts,o,h,l,c,vol_rel,taker_buy_ratio from kbar_evolution "
        "where symbol='BTCUSDT' and interval_ms=900000 order by ts"))
    lines = {}
    for ts, lk, p in con.execute(
            "select ts,line_key,line_price from kbar_line "
            "where symbol='BTCUSDT' and interval_ms=900000"):
        lines.setdefault(ts, {})[lk] = float(p)
    con.close()
    _CACHE["bars"] = [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                       "vol_rel": r[5], "taker_buy_ratio": r[6]} for r in rows]
    _CACHE["lines"] = lines
    return _CACHE["bars"], _CACHE["lines"]


def _judge_at(ts_bj):
    """给北京时间（UTC+8），返回 (judgement, ts)。"""
    dt = datetime.datetime.strptime(ts_bj, "%Y-%m-%d %H:%M")
    ms = int((dt - datetime.datetime(1970, 1, 1)).total_seconds() - 8 * 3600) * 1000
    bars, lines = _load_real()
    ts_list = [b["ts"] for b in bars]
    if ms not in ts_list:
        pytest.skip("库里没有 %s 这根" % ts_bj)
    i = ts_list.index(ms)
    prev = lines.get(ts_list[i - 1], {}) if i else None
    return S.judge(bars, i, prev_lines=prev), ms


def test_real_2345_beijing_is_a_panic_pin_with_line_recovery():
    """用户的第 1 个例子：北京 2026-11-07 23:45 = 插针 + 收回 3 条线 + 恐慌盘。"""
    j, _ = _judge_at("2026-11-07 23:45")
    assert j.has(S.STATE_PIN) and j.side == S.SIDE_DOWN
    assert j.has(S.STATE_PIN_RECOVERED)
    assert j.has(S.STATE_PANIC)
    assert j.label == S.STATE_PANIC
    joined = " ".join(j.evidence)
    for key in ("donchian_lo@15m", "range_p@1h", "range_soft_dn@1h"):
        assert key in joined


def test_real_0515_beijing_is_stable_after_the_0445_pin():
    """用户的第 2 个例子：北京 2026-11-08 05:15 = 插针后的稳住。

    关键：它稳的是 **04:45** 那根，不是 23:45。
    """
    j, _ = _judge_at("2026-11-08 05:15")
    assert j.label == S.STATE_STABLE
    assert not j.has(S.STATE_PIN)             # 它本身不是针
    pin_bj = (datetime.datetime.utcfromtimestamp(j.pin_ts / 1000)
              + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    assert pin_bj == "2026-11-08 04:45"


def test_real_0445_beijing_is_the_pin_itself():
    """被稳住的那根针自己，必须也是插针（否则「稳住」没有主语）。"""
    j, _ = _judge_at("2026-11-08 04:45")
    assert j.has(S.STATE_PIN) and j.side == S.SIDE_DOWN
    assert j.has(S.STATE_PANIC)


def test_real_0500_beijing_is_not_a_pin_and_not_stable():
    """05:00 收位只有 10%，是「针后又被杀一刀」，不能算稳住。"""
    j, _ = _judge_at("2026-11-08 05:00")
    assert not j.has(S.STATE_PIN)
    assert not j.has(S.STATE_STABLE)


def test_real_2345_bar_has_no_stable_label():
    """针自己不许同时被标成稳住。"""
    j, _ = _judge_at("2026-11-07 23:45")
    assert not j.has(S.STATE_STABLE)
    assert not j.has(S.STATE_NEAR_PIN)

# ================================================================ v2 判据
# 这一节对应 docs/K线演化-实现记录.md §三十八 的两个漏洞。
# 用户的原话：「我要求的恐慌盘以及插针收回等都没有判断」—— 那两根就是下面这两类。

def bar2(o, h, l, c, vol_rel=None, tbr=None, ts=None):
    d = {"o": float(o), "h": float(h), "l": float(l), "c": float(c)}
    if vol_rel is not None:
        d["vol_rel"] = vol_rel
    if tbr is not None:
        d["taker_buy_ratio"] = tbr
    if ts is not None:
        d["ts"] = ts
    return d


def test_panic_does_not_require_the_bar_to_be_a_pin():
    """v1 漏洞一：`close_pos = 0` 时下影恒等于 0，于是「恐慌盘要以插针为前提」
    让「收在最低点的大阴线」永远判不出来。全库 181 根这种根，v1 一根都没抓到。

    2026-11-04 10:45（北京）就是这形态：开 76642 收 76127，收在全幅最低，
    量 3.09 倍、主动买 0.311 —— 一根影线都没有，但价格是被按住了。
    """
    b = bar2(76642.2, 76654.3, 76127.4, 76127.8, vol_rel=3.09, tbr=0.311)
    g = S.geometry(b["o"], b["h"], b["l"], b["c"])
    assert S.pin_side(g) is None, "这根本来就不该是针（下影 0.4 点）"
    j = S.judge([b], 0)
    assert j.label == S.STATE_PANIC, j.label
    assert j.side is None, "它自己不是针，side 就该是 None"
    assert any("不要求本根是针" in e for e in j.evidence), j.evidence


def test_panic_still_requires_volume_and_aggression():
    """去掉的是「必须是针」这个**形态**前提（**不是**去掉量能与主动性。

    这两条是恐慌盘的实质：没量、主动性不占优，收在最低点也只是普通下跌。
    """
    # 收位 0% 但量只有 1.2 倍 -> 不是恐慌盘
    j = S.judge([bar2(100, 100.5, 99, 99, vol_rel=1.2, tbr=0.4)], 0)
    assert not j.has(S.STATE_PANIC), j.evidence
    assert any("量只" in e for e in j.evidence), j.evidence
    # 量够但主动买 0.6（卖方不占优）-> 也不是
    j2 = S.judge([bar2(100, 100.5, 99, 99, vol_rel=5.0, tbr=0.60)], 0)
    assert not j2.has(S.STATE_PANIC), j2.evidence
    assert any("主动买" in e for e in j2.evidence), j2.evidence


def test_panic_side_prefers_the_wick_over_close_position():
    """2026-11-07 23:45：下影 166 点 = 77%，收位也是 77%。

    收位高是因为「打下去又被买回来」，它是结果、不是方向。
    只看收位会把下压判成上冲 —— 这一条钉住「有影线听影线的」。
    """
    # 开盘价在**高位**才是下影：下影 = min(o, c) - l。开在低位就变成上影了。
    b = bar2(75688.0, 75689.1, 75522.9, 75689.0, vol_rel=7.35, tbr=0.388)
    g = S.geometry(b["o"], b["h"], b["l"], b["c"])
    assert g.close_pos > 0.7, g.close_pos      # 收在高位
    assert S.pin_side(g) == S.SIDE_DOWN        # 但是根下针
    assert S.panic_side(g) == S.SIDE_DOWN, "方向被收位带跑了"
    j = S.judge([b], 0)
    assert j.has(S.STATE_PANIC), j.evidence
    assert any("影线方向" in e for e in j.evidence), j.evidence


def test_swing_pin_is_confirmed_only_with_data_up_to_today():
    """局部极值只许用「到今天为止」的数据。

    写成「后面几根没破它」就是偷未来：第 j 根收盘时不可能知道后面破没破。
    这里把同一根放在两个不同的 `end` 下判，结论必须随 end 变 ——
    说明它真的在看窗口，而不是回头扫描整段序列。
    """
    # 价差必须 > WICK_PTS_MIN（40），否则「冲进极值的落差」这道门槛会先把它们挡掉，
    # 测出来的是 min_depth 而不是 end 的作用。
    bars = [bar2(1000, 1010, 990, 1000),   # 0
            bar2(1000, 1005, 950, 960),    # 1  这一段的最低（落差 1010-950=60）
            bar2(960, 970, 955, 965),      # 2  没破 950
            bar2(965, 970, 940, 950)]      # 3  破了 950 -> 1 不再是极值
    assert S.swing_pin_kind(bars, 1, 1) == S.SIDE_DOWN
    assert S.swing_pin_kind(bars, 1, 2) == S.SIDE_DOWN, "2 没破，1 还算极值"
    assert S.swing_pin_kind(bars, 1, 3) != S.SIDE_DOWN, "3 破了 95，1 就不该再算极值"
    assert S.swing_pin_kind(bars, 3, 3) == S.SIDE_DOWN


def test_swing_pin_needs_a_minimum_drop():
    """抖 3 个点不算针 —— 与长影线要求 `WICK_PTS_MIN` 对称。"""
    flat = [bar2(100, 100.2, 99.9, 100), bar2(100, 100.1, 99.8, 99.9)]
    assert S.swing_pin_kind(flat, 1, 1) is None, "0.4 点的抖动被当成针了"
    deep = [bar2(1000, 1010, 990, 1000), bar2(1000, 1005, 950, 960)]
    assert S.swing_pin_kind(deep, 1, 1) == S.SIDE_DOWN


def test_multi_bar_downtrend_low_is_reachable_as_a_pin():
    """v1 漏洞二：2026-11-04 11:00 打出全波最低 75,690.2，下影只占 36% ——
    它不是任何单根的长影线，但它是这波跌下来的极值。

    v1 只认长影线，所以 11:15 的「收回 + 稳住」根本判不出来（判成「无」）。
    v2 用局部极值当针，这里验证「找不到长影线也能靠极值找到针」。
    """
    bars = [bar2(77000, 77010, 76600, 76610),
            bar2(76610, 76620, 76200, 76210),
            bar2(76210, 76250, 75690.2, 75870),   # 缺口逐渐收窄，影线占比都 < 65%
            bar2(75870, 76353.5, 75868.0, 76319.5)]  # 收回并收高位
    for b in bars[:3]:
        g = S.geometry(b["o"], b["h"], b["l"], b["c"])
        assert S.pin_side(g) is None, "这三根都不该是长影线针，用例前提不成立"
    j = S.judge(bars, 3)
    assert j.has(S.STATE_STABLE) or j.has(S.STATE_NEAR_PIN), j.evidence
    assert j.ref_pin_kind == S.PIN_KIND_SWING, j.ref_pin_kind
    assert j.pin_ts is None or True   # bars 没给 ts，不许崩


def test_pin_amplitude_is_the_pin_bars_own_range_not_the_widest_one():
    """针幅必须是**极值那根自己的全幅**，不是窗口里最宽那根。

    拿窗口最宽根当尺会把尺撑大、于是「稳住」虚增、「针后远离」几乎消失。
    实测（全库 17,654 根，各自成立口径）：
        极值那根自己的全幅   稳住 3,992 / 远离 736   ← 现在用的
        窗口内最宽根         稳住 4,220 / 远离 508   ← 远离少 228 根
    更早还试过「冲进极值的落差」：2026-11-04 11:00 那根算出 1,618 点，
    177.8 点只占 0.11 倍，几乎全判「稳住」，等于这把尺失效。

    这里造一个「窗口里有更宽根」的场景：极值那根只有 120 点，另一根 500 点。
    """
    bars = [bar2(1000, 1500, 1000, 1200),    # 0  全幅 500（窗口里最宽）
            bar2(1000, 1120, 950, 1000),     # 1  极值低点，全幅 170
            bar2(1000, 1060, 990, 1050)]     # 2  极值之后
    own = 170.0                                   # 1120 - 950
    widest = 500.0                                # 1500 - 1000
    got = S.pin_amp(bars, 1, S.PIN_KIND_SWING)
    assert got == max(own, S.WICK_PTS_MIN), (
        "针幅应取极值那根自己的全幅 %.1f，实际 %.1f" % (own, got))
    assert got != widest, "用成了窗口最宽根（%.1f）" % widest


def test_pin_amplitude_has_a_floor_for_a_doji_extreme():
    """极值那根是十字星（全幅 2 点）时，尺不能小到除出天文数字。"""
    bars = [bar2(1000, 1010, 990, 1000), bar2(1000, 1001, 999, 1000.5)]
    assert S.pin_amp(bars, 1, S.PIN_KIND_WICK) == S.WICK_PTS_MIN
    assert S.pin_amp(bars, 1, S.PIN_KIND_SWING) == S.WICK_PTS_MIN


def test_no_state_means_empty_states_not_the_word_wu():
    """「无」不是一种形态。放进 states 会让「label=无 但 states=['无']」看起来
    像六种形态之一，落库后与 states='' 对不上（早先就是这样错的）。"""
    j = S.judge([bar2(100, 100.1, 99.9, 100.0)], 0)
    assert j.label == S.STATE_NONE
    assert j.states == ()
    assert not j.has(S.STATE_NONE)


def test_params_hash_changes_when_a_threshold_changes():
    """表 5 每行带指纹，就是为了「改阈值没重跑」能被发现。

    这条测试改一个常量再还原，验证指纹真的跟着动 ——
    否则指纹是个常数，等于没装安全阀。
    """
    before = S.params_hash()
    old = S.WICK_PTS_MIN
    try:
        S.WICK_PTS_MIN = old + 1.0
        assert S.params_hash() != before, "改了阈值指纹没变，安全阀失效"
    finally:
        S.WICK_PTS_MIN = old
    assert S.params_hash() == before, "还原后指纹必须回到原值"


def test_params_covers_every_threshold_that_affects_the_verdict():
    """指纹只覆盖它列出来的键。漏一个键 = 改它不会让指纹失效。

    这里钉住键的**集合**：新增阈值而忘了加进 params()，等于新增了一个
    改了也不报警的常量。
    """
    assert set(S.params()) == {
        "wick_pct_min", "wick_pts_min", "panic_vol_rel_min",
        "panic_taker_buy_max", "panic_close_pos_max", "stable_lookback_bars",
        "scenario_tol_pts", "stable_max_pin_range_mult", "swing_lookback_bars",
    }


def test_cross_bar_recovery_is_off_by_default():
    """跨根收回默认关。开着时全库 37% 的根都会被判「插针收回」——
    那不是少数事件，说明它测到的是「相邻根收在线的另一侧」，不是「针被收回」。
    功能留着（可传 allow_cross_bar=True），但默认不许开着影响标签。
    """
    import inspect
    sig = inspect.signature(S.judge)
    assert sig.parameters["allow_cross_bar"].default is False
    assert sig.parameters["allow_swing"].default is True
