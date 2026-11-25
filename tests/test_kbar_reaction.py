# -*- coding: utf-8 -*-
"""第 3 步 · 反应层（kbar_reaction）的回归测试。

这些测试守的是「口径不许悄悄变」。有两条不变量，都是踩过才知道的：

  1. **反应只能用本根开盘前已就绪的线值。**
     同周期线（ema20@15m…）在 t 根的值由 t 的收盘算出，
     拿它判断「t 根有没有碰到」就是用收盘后才知道的数解释这根里的走势。
     实测错法会造出 1,723 次假触碰（占全部触碰 7%）。

  2. **上一根没有这条线，就是它不存在 —— 不许往前带。**
     表 0 对每条存在的线在每根 K 线上都无条件落行，所以「缺行」=「不存在」。
     第一版用「最近就绪值」，结果 2026-08-05 区间演化转 PRE 之后，
     05:15 的 K 线仍在拿 02:00 的线做反应。

两条都由「取上一根那一行」同时满足（15m 根 T-15m 的收盘正好是 T 的开盘）。

时间戳用 T(n) = BASE + n*900000，相邻根严格差一个周期 —— 否则 ready 的
边界关系没有意义，测出来的东西也不代表真实情形。
"""
from __future__ import annotations

import pytest

from ai_trader.kbar import reaction as R
from ai_trader.kbar.store import KBarStore, RX_COLS

BASE = 1_600_000_000_000
IV = 900_000


def T(n: int) -> int:
    """第 n 根 15m 的时间戳（UTC ms）。"""
    return BASE + n * IV


# ------------------------------------------------------------------ 夹具
def _bar(n, o, h, l, c, **kw):
    d = {"ts": T(n), "o": o, "h": h, "l": l, "c": c,
         "vol_rel": None, "taker_buy_ratio": None}
    d.update(kw)
    return d


def _lines(spec, period_ms=IV, kind=None):
    """{bar 序号: price} → {line_key: {bar_ts: (price, ready, src_ts)}}"""
    key = kind or "a@15m"
    if "@" not in key:
        key = "%s@%s" % (key, "15m" if period_ms == IV else "1h")
    return {key: {T(n): (float(p), T(n) + period_ms, T(n))
                  for n, p in spec.items()}}


def _run(bars, line_rows, meta=None, seg=None):
    return R.build_reactions("X", IV, bars, line_rows, meta, seg_by_ts=seg)


def _flat(n, o=1.0, h=2.0, l=0.5, c=1.5, **kw):
    return _bar(n, o, h, l, c, **kw)


# ------------------------------------------------------------------ 基础口径
def test_pen_depth_from_above_is_p_minus_low():
    assert R.pen_depth_pct(p=100.0, l=98.0, h=101.0, side=R.SIDE_FROM_ABOVE) == pytest.approx(0.02)


def test_pen_depth_from_below_is_high_minus_p():
    assert R.pen_depth_pct(p=100.0, l=99.0, h=102.0, side=R.SIDE_FROM_BELOW) == pytest.approx(0.02)


def test_recover_from_above_is_close_minus_low():
    assert R.recover_pct(p=100.0, l=98.0, h=101.0, c=99.5,
                         side=R.SIDE_FROM_ABOVE) == pytest.approx(0.015)


def test_recover_from_below_is_high_minus_close():
    assert R.recover_pct(p=100.0, l=99.0, h=102.0, c=101.0,
                         side=R.SIDE_FROM_BELOW) == pytest.approx(0.01)


def test_recover_can_exceed_pen_when_close_crosses_the_line():
    """穿进去又站到另一侧：收回可以大于穿透。这不是错误，是最值得看的情形。"""
    pen = R.pen_depth_pct(p=100.0, l=99.0, h=100.5, side=R.SIDE_FROM_ABOVE)
    rec = R.recover_pct(p=100.0, l=99.0, h=100.5, c=100.8, side=R.SIDE_FROM_ABOVE)
    assert pen == pytest.approx(0.01)
    assert rec == pytest.approx(0.018)
    assert rec > pen


def test_pen_and_recover_are_fractions_not_percent():
    """pen/recover 叫 _pct 但设计稿公式不带 ×100，是 0~1 的比例。

    这个不对称会静默坑下游（把 0.0008 当成 0.0008% 或 0.08%），专门钉住。
    """
    pen = R.pen_depth_pct(p=1000.0, l=999.0, h=1001.0, side=R.SIDE_FROM_ABOVE)
    assert pen == pytest.approx(0.001)                             # 千分之一，不是 0.1
    assert R.dist_pct(c=1010.0, p=1000.0) == pytest.approx(1.0)     # 这个才是百分数


def test_dist_pct_sign_is_positive_above_the_line():
    assert R.dist_pct(c=101.0, p=100.0) > 0
    assert R.dist_pct(c=99.0, p=100.0) < 0


def test_side_uses_open_price_and_below_wins_ties():
    assert R.side_of(101.0, 100.0) == R.SIDE_FROM_ABOVE
    assert R.side_of(99.0, 100.0) == R.SIDE_FROM_BELOW
    assert R.side_of(100.0, 100.0) == R.SIDE_FROM_BELOW        # o == p 归 from_below


def test_close_side_has_third_value_at():
    """收盘价正好等于线价是真实存在的（实测 7 行），不许硬塞进某一边。"""
    assert R.close_side_of(101.0, 100.0) == R.CLOSE_ABOVE
    assert R.close_side_of(99.0, 100.0) == R.CLOSE_BELOW
    assert R.close_side_of(100.0, 100.0) == R.CLOSE_AT


# ------------------------------------------------------------------ 触碰判定
def test_touch_is_inclusive_of_both_edges():
    assert R.is_touch(99.0, 101.0, 99.0) is True         # 正好等于最低
    assert R.is_touch(99.0, 101.0, 101.0) is True        # 正好等于最高
    assert R.is_touch(99.0, 101.0, 100.0) is True
    assert R.is_touch(99.0, 101.0, 98.999) is False
    assert R.is_touch(99.0, 101.0, 101.001) is False


def test_touch_tolerance_is_off_by_default():
    assert R.TOUCH_TOL_PCT == 0.0
    assert R.is_touch(100.0, 101.0, 99.99) is False


def test_touch_tolerance_widens_the_band_when_enabled():
    assert R.is_touch(100.0, 101.0, 99.99, tol_pct=0.02) is True     # 0.02% of 100 = 0.02
    assert R.is_touch(100.0, 101.0, 99.9, tol_pct=0.02) is False


# ------------------------------------------------------------------ as-of / pick_prev
def test_as_of_picks_last_value_ready_at_or_before_ts():
    seq = [(T(0), 0, 1.0), (T(1), 1, 2.0), (T(2), 2, 3.0)]
    assert R.as_of(seq, T(0)) == (T(0), 0, 1.0)      # 边界等于也算已知
    assert R.as_of(seq, T(1) - 1) == (T(0), 0, 1.0)
    assert R.as_of(seq, T(1)) == (T(1), 1, 2.0)
    assert R.as_of(seq, T(9)) == (T(2), 2, 3.0)


def test_as_of_returns_none_before_the_series_starts():
    assert R.as_of([(T(1), 0, 1.0)], T(0)) is None
    assert R.as_of([], T(0)) is None


def test_pick_prev_returns_only_exact_bar_match():
    rows = {T(0): (100.0, T(1), T(0))}
    assert R.pick_prev(rows, T(0)) == (100.0, T(1), T(0))
    assert R.pick_prev(rows, T(1)) is None      # 不许拿第 0 根的值顶第 1 根
    assert R.pick_prev(rows, T(-1)) is None


# ------------------------------------------------------------------ 不变量 1：防偷未来
def test_reaction_uses_previous_bar_value_not_the_current_bar_value():
    """错用本根收盘才出的线值会**漏掉真反应**。

    上一根(T0) 的线价 100 落在本根 [99,103] 里 → 碰得到；
    本根(T1) 收盘才出的 105 碰不到。正确行为必须判「碰到」。
    """
    bars = [_flat(0, 99.5, 100.5, 99.2, 100.2), _bar(1, 101.0, 103.0, 99.0, 102.0)]
    rows, stats = _run(bars, _lines({0: 100.0, 1: 105.0}))
    assert len(rows) == 1, "用了本根收盘才出的线值 → 真反应被漏掉"
    assert rows[0]["ts"] == T(1)
    assert rows[0]["line_price"] == 100.0
    assert stats["touch"] == 1


def test_reaction_does_not_invent_a_touch_from_the_current_bar_value():
    """反过来：错用本根收盘才出的 100 会**造出假触碰**（实测 1,723 次）。"""
    bars = [_flat(0, 99.5, 100.5, 99.2, 100.2), _bar(1, 101.0, 103.0, 99.0, 102.0)]
    rows, stats = _run(bars, _lines({0: 104.0, 1: 100.0}))
    assert rows == [], "用了本根收盘才出的线值 → 造出假触碰"
    assert stats["bars_without_touch"] == 2


def test_first_bar_has_no_reaction_because_there_is_no_previous_bar():
    rows, stats = _run([_flat(0)], _lines({0: 1.0}))
    assert rows == []
    assert stats["skipped"]["line_not_ready"] == 1


# ------------------------------------------------------------------ 不变量 2：不许往前带
def test_line_absent_at_previous_bar_is_not_carried_forward():
    """**这条守的是实际发现的一个缺陷。**

    2026-08-05 01:00 起区间演化转 PRE（四线全空），表 0 就不再落 range 线行。
    第一版「取最近就绪值」会让 05:15 的 K 线继续拿 02:00 那条线判「碰到」——
    用一条消失了 3 小时的线做反应。正确行为：上一根没有就是不存在。
    """
    # 第 1 根的价格区间刻意避开线价，好让「不算触碰」与「线消失」两件事分开：
    # 第 1 根的上一根(第 0 根)有这条线 → 只是没碰到；
    # 第 2 根的上一根(第 1 根)已经没有这条线 → 不许拿第 0 根的值往前带。
    bars = [_flat(0), _bar(1, 95.0, 97.0, 94.0, 96.0), _bar(2, 99.0, 103.0, 98.0, 102.0)]
    rows, stats = _run(bars, _lines({0: 100.0}, period_ms=3_600_000, kind="range_key@1h"))
    assert rows == [], "拿消失了的值往前带了"
    assert stats["skipped"]["line_absent_at_prev_bar"] == 1


def test_line_reappearing_is_usable_again():
    bars = [_bar(0, 99.0, 101.0, 98.0, 100.0),
            _bar(1, 99.0, 101.0, 98.0, 100.0),
            _bar(2, 99.0, 103.0, 98.0, 102.0)]
    rows, _ = _run(bars, _lines({0: 100.0, 1: 100.0}))
    assert [r["ts"] for r in rows] == [T(1), T(2)]


def test_hourly_line_is_usable_once_its_hour_closes():
    """1h 线在 15m 根上：取上一根那一行，天然满足「该 1h 已收盘」。"""
    bars = [_bar(0, 99.0, 101.0, 98.0, 100.0), _bar(4, 99.0, 103.0, 98.0, 102.0)]
    rows, _ = _run(bars, _lines({0: 100.0}, period_ms=3_600_000, kind="range_key@1h"),
                   {"range_key@1h": ("range_key", 3_600_000)})
    assert len(rows) == 1
    assert rows[0]["ts"] == T(4)
    assert rows[0]["line_period_ms"] == 3_600_000


def test_assert_no_lookahead_raises_when_ready_is_after_the_bar():
    with pytest.raises(RuntimeError):
        R.assert_no_lookahead([{"ts": T(1), "line_key": "k", "_line_ready_ms": T(2)}], IV)


def test_assert_no_lookahead_accepts_boundary_equality():
    R.assert_no_lookahead([{"ts": T(1), "line_key": "k", "_line_ready_ms": T(1)}], IV)


def test_built_rows_pass_the_lookahead_assertion():
    bars = [_bar(0, 99.0, 101.0, 98.0, 100.0), _bar(1, 99.0, 103.0, 98.0, 102.0)]
    rows, _ = _run(bars, _lines({0: 100.0}))
    R.assert_no_lookahead(rows, IV)      # 不抛异常即通过


# ------------------------------------------------------------------ 计数与统计
def test_pairs_evaluated_counts_every_bar_times_every_line():
    lr = {"a@15m": {T(0): (1.0, T(1), T(0))}, "b@15m": {T(0): (100.0, T(1), T(0))}}
    _, stats = _run([_flat(0), _flat(1)], lr,
                    {"a@15m": ("a", IV), "b@15m": ("b", IV)})
    assert stats["pairs_evaluated"] == 4


def test_no_touch_rows_do_not_enter_the_table_but_are_counted():
    rows, stats = _run([_flat(0), _flat(1)], _lines({0: 99.0}))
    assert rows == []
    assert stats["bars_without_touch"] == 2


def test_touch_counts_by_bar_counts_rows_per_bar():
    assert R.touch_counts_by_bar([{"ts": T(0)}, {"ts": T(0)}, {"ts": T(1)}]) == \
        {T(0): 2, T(1): 1}


def test_empty_inputs_are_handled():
    rows, stats = R.build_reactions("X", IV, [], {"a@15m": {T(0): (1.0, T(0), T(0))}})
    assert rows == [] and stats["rows"] == 0
    rows2, stats2 = R.build_reactions("X", IV, [_flat(0)], {})
    assert rows2 == [] and stats2["lines"] == 0


def test_by_kind_counts_touch_rows():
    lr = {"a@15m": {T(0): (1.0, T(1), T(0))}, "b@15m": {T(0): (1.2, T(1), T(0))}}
    _, stats = _run([_flat(0), _flat(1)], lr,
                    {"a@15m": ("a", IV), "b@15m": ("b", IV)})
    assert stats["by_kind"] == {"a": 1, "b": 1}


def test_row_carries_line_identity_and_bar_volume_fields():
    bars = [_flat(0), _flat(1, vol_rel=2.5, taker_buy_ratio=0.61)]
    lr = {"ema200@1h": {T(0): (1.0, T(0) + 3_600_000, T(0))}}
    rows, _ = _run(bars, lr, {"ema200@1h": ("ema200", 3_600_000)})
    r = rows[0]
    assert r["line_key"] == "ema200@1h"
    assert r["line_kind"] == "ema200"
    assert r["line_period_ms"] == 3_600_000
    assert r["vol_rel"] == 2.5
    assert r["taker_buy_ratio"] == 0.61


def test_undefined_fields_are_left_null():
    """dwell_bars 口径没定，必须留空 —— 不许塞一个看起来合理的默认值。"""
    rows, _ = _run([_flat(0), _flat(1)], _lines({0: 1.0}))
    assert rows[0]["dwell_bars"] is None


def test_internal_evidence_fields_are_present_but_underscored():
    rows, _ = _run([_flat(0), _flat(1)], _lines({0: 1.0}))
    assert rows[0]["_line_ready_ms"] == T(0) + IV
    assert rows[0]["_src_ts"] == T(0)


# ------------------------------------------------------------------ nth_touch
def test_nth_touch_is_null_when_no_segment_info_supplied():
    rows, stats = _run([_flat(0), _flat(1)], _lines({0: 1.0}))
    assert rows[0]["nth_touch"] is None
    assert stats["nth_touch"] == "not_computed"


def test_nth_touch_counts_from_one_within_a_segment():
    bars = [_flat(n) for n in range(4)]
    seg = {T(n): (1, 1) for n in range(4)}
    rows, _ = _run(bars, _lines({0: 1.0, 1: 1.0, 2: 1.0}), seg=seg)
    assert [r["nth_touch"] for r in rows] == [1, 2, 3]


def test_nth_touch_resets_when_segment_changes():
    """设计稿 §13 第 3 步验收：`nth_touch` 在 (fam, seg) 变化时必须归零。

    反应发生在第 1/2/3 根（第 0 根没有「上一根」）。
    让第 1、2 根同段、第 3 根换段 → 期望 1, 2, 1。
    """
    bars = [_flat(n) for n in range(4)]
    seg = {T(0): (1, 1), T(1): (1, 1), T(2): (1, 1), T(3): (1, 2)}
    rows, _ = _run(bars, _lines({0: 1.0, 1: 1.0, 2: 1.0}), seg=seg)
    assert [r["nth_touch"] for r in rows] == [1, 2, 1], "换段没归零"


def test_nth_touch_resets_when_family_changes():
    seg = {T(0): (1, 1), T(1): (2, 1)}
    rows, _ = _run([_flat(0), _flat(1)], _lines({0: 1.0}), seg=seg)
    assert [r["nth_touch"] for r in rows] == [1]


def test_nth_touch_is_counted_per_line_key():
    lr = {"a@15m": {T(0): (1.0, T(1), T(0))}, "b@15m": {T(0): (1.2, T(1), T(0))}}
    rows, _ = _run([_flat(0), _flat(1)], lr,
                   {"a@15m": ("a", IV), "b@15m": ("b", IV)},
                   seg={T(0): (1, 1), T(1): (1, 1)})
    got = {(r["ts"], r["line_key"]): r["nth_touch"] for r in rows}
    assert got == {(T(1), "a@15m"): 1, (T(1), "b@15m"): 1}


def test_nth_touch_is_null_and_counted_when_bar_has_no_segment():
    """段缺失的根不许假装从 1 开始 —— 那不是「第一次」，是「不知道第几次」。"""
    bars = [_flat(n) for n in range(3)]
    rows, stats = _run(bars, _lines({0: 1.0, 1: 1.0}), seg={T(1): (1, 1)})
    by_ts = {r["ts"]: r["nth_touch"] for r in rows}
    assert by_ts[T(1)] == 1
    assert by_ts[T(2)] is None
    assert stats["skipped"]["nth_touch:no_segment"] == 1


# ------------------------------------------------------------------ 落库
def _store(tmp_path):
    p = str(tmp_path / "kbar.db")
    return KBarStore(p), p


def _row(ts=None, key="ema20@15m", price=1.0, **kw):
    r = {"symbol": "X", "interval_ms": IV, "ts": T(0) if ts is None else ts,
         "line_key": key, "line_kind": "ema20", "line_period_ms": IV,
         "line_price": price, "dist_pct": 0.5, "side": "from_above",
         "pen_depth_pct": 0.001, "recover_pct": 0.002, "close_side": "above",
         "dwell_bars": None, "vol_rel": 1.2, "taker_buy_ratio": 0.55,
         "nth_touch": None}
    r.update(kw)
    return r


def test_reaction_table_has_all_design_columns(tmp_path):
    """表 2 的列必须与设计稿 §6 表 2 逐项对上（少一列就是设计稿没实现）。"""
    st, _ = _store(tmp_path)
    with st:
        cols = {r[1] for r in st._con.execute("PRAGMA table_info(kbar_reaction)")}
    design = {"symbol", "interval_ms", "ts", "line_key", "line_kind",
              "line_period_ms", "line_price", "dist_pct", "side",
              "pen_depth_pct", "recover_pct", "close_side", "dwell_bars",
              "vol_rel", "taker_buy_ratio", "nth_touch"}
    assert design <= cols, "表 2 缺列: %s" % sorted(design - cols)
    assert "touch" not in cols, "能进表的必然触及，不该有 touch 列"


def test_rx_cols_matches_table_columns(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        cols = {r[1] for r in st._con.execute("PRAGMA table_info(kbar_reaction)")}
    want = {c.strip() for c in RX_COLS.split(",")}
    assert want <= cols, "RX_COLS 里有表里不存在的列: %s" % sorted(want - cols)


def test_upsert_reactions_is_idempotent(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_reactions([_row(), _row(key="ema50@15m", price=2.0)])
        assert st.reaction_counts("X", IV)["rows"] == 2
        st.upsert_reactions([_row(price=1.5)])
        assert st.reaction_counts("X", IV)["rows"] == 2


def test_upsert_reactions_updates_values_on_conflict(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_reactions([_row(price=1.0, side="from_above")])
        st.upsert_reactions([_row(price=9.9, side="from_below")])
        got = st.reactions("X", IV)[0]
    assert got["line_price"] == 9.9
    assert got["side"] == "from_below"


def test_upsert_reactions_accepts_null_late_fields(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_reactions([_row(dwell_bars=None, nth_touch=None)])
        got = st.reactions("X", IV)[0]
    assert got["dwell_bars"] is None
    assert got["nth_touch"] is None


def test_reaction_counts_reports_fill_rates(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_reactions([_row(), _row(ts=T(1), vol_rel=None)])
        c = st.reaction_counts("X", IV)
    assert c["rows"] == 2
    assert c["filled"]["vol_rel"] == 1
    assert c["filled"]["dwell_bars"] == 0


def test_delete_reactions_only_touches_its_own_interval(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_reactions([_row(), _row(interval_ms=3_600_000)])
        assert st.delete_reactions("X", IV) == 1
        assert st.reaction_counts("X", 3_600_000)["rows"] == 1


def test_rerunning_facts_does_not_wipe_reactions(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_reactions([_row()])
        st.upsert_evolution([{"symbol": "X", "interval_ms": IV, "ts": T(0),
                              "idx": 1, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}])
        assert st.reaction_counts("X", IV)["rows"] == 1


def test_line_series_is_keyed_by_bar_ts(tmp_path):
    """反应层必须能按 bar_ts 取到「上一根那一行」，所以索引键是 ts 而不是 ready。"""
    st, _ = _store(tmp_path)
    with st:
        st.upsert_lines([
            {"symbol": "X", "interval_ms": IV, "ts": T(1), "line_key": "a@15m",
             "line_kind": "ema20", "line_period_ms": IV, "line_price": 2.0,
             "bar_close": 2.0, "dist_pct": 0.0, "src_ts": T(1),
             "line_ready_ms": T(2)},
            {"symbol": "X", "interval_ms": IV, "ts": T(0), "line_key": "a@15m",
             "line_kind": "ema20", "line_period_ms": IV, "line_price": 1.0,
             "bar_close": 1.0, "dist_pct": 0.0, "src_ts": T(0),
             "line_ready_ms": T(1)},
        ])
        series = st.line_series("X", IV)
    assert set(series["a@15m"]) == {T(0), T(1)}
    assert series["a@15m"][T(0)] == (1.0, T(1), T(0))
    assert series["a@15m"][T(1)] == (2.0, T(2), T(1))


def test_line_meta_maps_key_to_kind_and_period(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_lines([
            {"symbol": "X", "interval_ms": IV, "ts": T(0), "line_key": "ema200@1h",
             "line_kind": "ema200", "line_period_ms": 3_600_000, "line_price": 1.0,
             "bar_close": 1.0, "dist_pct": 0.0, "src_ts": T(0),
             "line_ready_ms": T(0) + 3_600_000},
        ])
        meta = st.line_meta("X", IV)
    assert meta["ema200@1h"] == ("ema200", 3_600_000)


def test_set_n_touch_writes_zero_for_evaluated_bars(tmp_path):
    """碰到 0 条要显式写 0 —— 留 NULL 会跟「没评估」长得一样。"""
    st, _ = _store(tmp_path)
    with st:
        for n in (0, 1):
            st.upsert_evolution([{"symbol": "X", "interval_ms": IV, "ts": T(n),
                                  "idx": n, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}])
        st.set_n_touch("X", IV, {T(0): 2, T(1): 0})
        got = {r["ts"]: r["n_touch"] for r in st.evolution_rows("X", IV)}
    assert got[T(0)] == 2 and got[T(1)] == 0


def test_set_n_touch_leaves_unevaluated_bars_alone(tmp_path):
    """没评估过的根不许被写成 0（那是把「不知道」写成「没有」）。"""
    st, _ = _store(tmp_path)
    with st:
        st.upsert_evolution([{"symbol": "X", "interval_ms": IV, "ts": T(0),
                              "idx": 1, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}])
        st.set_n_touch("X", IV, {})
        assert st.evolution_rows("X", IV)[0]["n_touch"] is None


def test_reactions_query_filters_by_ts_and_key(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_reactions([_row(), _row(key="b@15m"), _row(ts=T(1))])
        assert len(st.reactions("X", IV, ts=T(0))) == 2
        assert len(st.reactions("X", IV, line_key="b@15m")) == 1


# ------------------------------------------------------------------ 端到端
def _line_row(n, price, key="ema20@15m", kind="ema20", period=IV):
    return {"symbol": "X", "interval_ms": IV, "ts": T(n), "line_key": key,
            "line_kind": kind, "line_period_ms": period, "line_price": price,
            "bar_close": 1.5, "dist_pct": 0.0, "src_ts": T(n),
            "line_ready_ms": T(n) + period}


def _evo(n, **kw):
    d = {"symbol": "X", "interval_ms": IV, "ts": T(n), "idx": n,
         "o": 101.0, "h": 103.0, "l": 99.0, "c": 102.0,
         "vol_rel": 1.1, "taker_buy_ratio": 0.5}
    d.update(kw)
    return d


def test_build_and_persist_reactions_end_to_end(tmp_path):
    """表 0 + 表 1 → 表 2，并且 n_touch 从反应层数出来写回表 1。"""
    from ai_trader.kbar.build import build_and_persist_reactions
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([_evo(n) for n in range(3)])
        st.upsert_lines([_line_row(0, 100.0), _line_row(1, 100.0)])
    stats = build_and_persist_reactions(p, "X", IV)
    assert stats["rows"] == 2, stats            # 第 1、2 根各有一次
    with KBarStore(p) as st2:
        assert len(st2.reactions("X", IV)) == 2
        evo = {r["ts"]: r["n_touch"] for r in st2.evolution_rows("X", IV)}
    assert evo == {T(0): 0, T(1): 1, T(2): 1}


def test_build_and_persist_reactions_refuses_without_source_tables(tmp_path):
    from ai_trader.kbar.build import build_and_persist_reactions
    st, p = _store(tmp_path)
    with st:
        pass
    stats = build_and_persist_reactions(p, "X", IV)
    assert "error" in stats
    assert "表 0" in stats["error"]


def test_build_and_persist_reactions_dry_run_writes_nothing(tmp_path):
    from ai_trader.kbar.build import build_and_persist_reactions
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([_evo(n, o=1.0, h=2.0, l=0.5, c=1.5) for n in (0, 1)])
        st.upsert_lines([_line_row(0, 1.0)])
    stats = build_and_persist_reactions(p, "X", IV, dry_run=True)
    assert stats["rows"] == 1
    with KBarStore(p) as st2:
        assert st2.reaction_counts("X", IV)["rows"] == 0


def test_build_and_persist_reactions_rebuild_is_idempotent(tmp_path):
    from ai_trader.kbar.build import build_and_persist_reactions
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([_evo(n, o=1.0, h=2.0, l=0.5, c=1.5) for n in (0, 1)])
        st.upsert_lines([_line_row(0, 1.0)])
    a = build_and_persist_reactions(p, "X", IV, rebuild=True)
    b = build_and_persist_reactions(p, "X", IV, rebuild=True)
    assert a["rows"] == b["rows"] == 1
    with KBarStore(p) as st2:
        assert st2.reaction_counts("X", IV)["rows"] == 1


def test_reaction_params_snapshot_declares_undefined_fields():
    """口径快照必须自报「哪个字段留空、为什么」，否则库解释不了自己。"""
    from ai_trader.kbar.build import reaction_params_snapshot
    p = reaction_params_snapshot(IV)
    assert p["undefined_by_design"] == ["dwell_bars"]
    assert "D-8" in p["undefined_reason"]["dwell_bars"]
    assert p["touch_tol_pct"] == 0.0
    assert "fam" in p["nth_touch_rule"] and "seg" in p["nth_touch_rule"]


def test_end_to_end_nth_touch_uses_segment_from_table1(tmp_path):
    """端到端：nth_touch 必须真的从表 1 的 (fam, seg) 取段并换段归零。

    反应发生在第 1/2/3 根（第 0 根没有「上一根」）。
    第 1、2 根同段、第 3 根换段 → 期望 1, 2, 1。
    """
    from ai_trader.kbar.build import build_and_persist_reactions
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([
            _evo(0, o=1.0, h=2.0, l=0.5, c=1.5, fam=1, seg=1),
            _evo(1, o=1.0, h=2.0, l=0.5, c=1.5, fam=1, seg=1),
            _evo(2, o=1.0, h=2.0, l=0.5, c=1.5, fam=1, seg=1),
            _evo(3, o=1.0, h=2.0, l=0.5, c=1.5, fam=1, seg=2),
        ])
        st.upsert_lines([_line_row(0, 1.0), _line_row(1, 1.0), _line_row(2, 1.0)])
    build_and_persist_reactions(p, "X", IV)
    with KBarStore(p) as st2:
        got = [(r["ts"], r["nth_touch"]) for r in st2.reactions("X", IV)]
    assert got == [(T(1), 1), (T(2), 2), (T(3), 1)], "换段没归零"
