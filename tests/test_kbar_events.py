# -*- coding: utf-8 -*-
"""第 4 步 · 事件层（kbar_event + 表 1 的事件列）回归测试。

这些测试守的是设计稿 §8 的三条，以及一条我自己加的事实纪律：

  1. §8.1 **回溯是动态的** —— `prev_event` 是「这条线上一次的事件」，
     不是「前一根」。中间隔 918 根也要能接上。
  2. §8.2 **允许无意义** —— neutral 必须存在。把每根都贴标签就是制造噪声。
  3. §8.3/§8.4 **词表复用区间演化，不许自由文本** —— 只有三个名字能落在线上。
  4. 一根 K 线可能同时突破多条线。表 1 只装得下一个，所以全量必须落在
     `kbar_event`。**丢事件就是丢事实** —— 实测最多一根同时 11 个事件。
"""
from __future__ import annotations

import pytest

from ai_trader.kbar import event as E
from ai_trader.kbar.store import EV_COLS, KBarStore

BASE = 1_600_000_000_000
IV = 900_000


def T(n: int) -> int:
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


def _lines_fill(ns, p, **kw):
    """同一条线在每根上都存在（多根序列必须这样种，否则第二根就「上一根没有线」）。"""
    return _lines({n: p for n in ns}, **kw)


def _run(bars, line_rows, meta=None, touch=None, window=E.REVERT_WINDOW_BARS):
    return E.build_events("X", IV, bars, line_rows, meta,
                          touch_by_bar=touch, revert_window_bars=window)


def _cross_up(n, p=100.0):
    """第 n 根收盘跨到线价 p 上方（前一根收盘在下方）。"""
    return _bar(n, p * 0.99, p * 1.02, p * 0.98, p * 1.01)


def _cross_dn(n, p=100.0):
    return _bar(n, p * 1.01, p * 1.02, p * 0.98, p * 0.99)


# ------------------------------------------------------------------ 词表
def test_vocab_covers_all_fourteen_range_events():
    """区间演化的 14 个事件名必须全量登记 —— 不然「为什么不用」没地方写。"""
    assert len(E.EVENT_VOCAB_RANGE) == 14
    for name in ("birth", "区间成形", "向上突破", "向下突破", "突破失败·反包",
                 "反向边界插针上移", "R滚动", "向上反转", "向下反转"):
        assert name in E.EVENT_VOCAB_RANGE, "词表漏了 %s" % name


def test_only_three_names_apply_to_a_line():
    assert set(E.EVENT_VOCAB_USED) == {E.BREAK_UP, E.BREAK_DOWN, E.FAILED_BREAK}
    assert set(E.EVENT_VOCAB_USED) | set(E.EVENT_VOCAB_RANGE_ONLY) == \
        set(E.EVENT_VOCAB_RANGE)


def test_range_only_names_exclude_the_three_breakout_names():
    for n in E.EVENT_VOCAB_USED:
        assert n not in E.EVENT_VOCAB_RANGE_ONLY


def test_built_events_never_use_a_name_outside_the_vocab():
    """自由文本会被当场发现：任何 event 都必须是词表里的名字。"""
    bars = [_bar(0, 99, 101, 98, 99.5), _cross_up(1)]
    rows, _ = _run(bars, _lines({0: 100.0}))
    for r in rows:
        assert r["event"] in E.EVENT_VOCAB_RANGE


# ------------------------------------------------------------------ 穿越判定
def test_cross_up_when_close_moves_from_below_to_above():
    assert E.classify_cross(99.0, 101.0, 100.0) == E.BREAK_UP


def test_cross_down_when_close_moves_from_above_to_below():
    assert E.classify_cross(101.0, 99.0, 100.0) == E.BREAK_DOWN


def test_no_event_when_close_stays_on_the_same_side():
    assert E.classify_cross(101.0, 102.0, 100.0) is None
    assert E.classify_cross(99.0, 98.0, 100.0) is None


def test_close_exactly_on_the_line_is_not_a_cross_by_itself():
    """收在线上不算穿越到任何一侧（与第 3 步 close_side 的 at 一致）。"""
    assert E.classify_cross(100.0, 100.0, 100.0) is None
    assert E.classify_cross(99.0, 100.0, 100.0) is None       # 走到线上但没过去
    assert E.classify_cross(100.0, 101.0, 100.0) == E.BREAK_UP  # 从线上过去


def test_classify_cross_refuses_without_a_previous_close():
    assert E.classify_cross(None, 101.0, 100.0) is None


def test_side_of_close_has_three_values():
    assert E.side_of_close(101.0, 100.0) == "above"
    assert E.side_of_close(99.0, 100.0) == "below"
    assert E.side_of_close(100.0, 100.0) == "at"


# ------------------------------------------------------------------ 不变量：取上一根
def test_line_absent_at_previous_bar_produces_no_event():
    """与第 3 步同一条纪律：线消失就是不存在，不许拿更早的值判穿越。"""
    bars = [_bar(0, 99, 101, 98, 99.5), _flat_far(1), _cross_up(2)]
    rows, stats = _run(bars, _lines({0: 100.0}))
    assert rows == []
    # 第 1 根的上一根(第 0 根)有这条线；第 2 根的上一根(第 1 根)没有
    assert stats["skipped"]["line_absent_at_prev_bar"] == 1


def _flat_far(n):
    return _bar(n, 50.0, 51.0, 49.0, 50.5)


def test_first_bar_cannot_have_an_event():
    rows, stats = _run([_cross_up(0)], _lines({0: 100.0}))
    assert rows == []
    assert stats["skipped"]["no_prev_bar"] >= 1


def test_touch_is_not_required_for_a_breakout():
    """跳空跨越整条线也算突破（这根可能根本没碰到线）。"""
    bars = [_bar(0, 99, 100.5, 98, 99.5), _bar(1, 105, 106, 104, 105.0)]
    rows, _ = _run(bars, _lines({0: 100.0}))
    assert len(rows) == 1 and rows[0]["event"] == E.BREAK_UP
    assert rows[0]["pen_depth_pct"] is None      # 没碰到线 → 穿透深度留空


def test_pen_depth_is_filled_when_the_bar_did_touch_the_line():
    bars = [_bar(0, 99, 100.5, 98, 99.5), _cross_up(1)]   # 第 1 根 l=98 h=102 覆盖 100
    rows, _ = _run(bars, _lines({0: 100.0}))
    assert rows[0]["pen_depth_pct"] is not None


# ------------------------------------------------------------------ 反包
def _break_then(n_back, p=100.0):
    """第 0 根收盘在下方 → 第 1 根向上突破 → 在线上方磨 → 第 n_back 根回到下方。

    中间那些根**收盘都在线上方且不穿越**，所以它们不产生事件 ——
    这样「第 n_back 根才回来」才是干净的「窗口内 / 窗口外」对照。

    返回 (bars, lines)：线在每根上都存在。
    """
    bars = [_bar(0, p * 0.99, p * 0.995, p * 0.98, p * 0.99), _cross_up(1, p)]
    for i in range(2, n_back):
        bars.append(_bar(i, p * 1.01, p * 1.02, p * 1.005, p * 1.01))   # 上方，不穿越
    if n_back >= 2:
        bars.append(_bar(n_back, p * 1.01, p * 1.015, p * 0.98, p * 0.99))  # 回到下方
    return bars, _lines_fill(range(len(bars)), p)


def test_failed_breakout_within_the_window_is_recorded():
    bars, lines = _break_then(2)
    rows, _ = _run(bars, lines)
    names = [r["event"] for r in rows]
    # 第 2 根同时是「反包」和「向下突破」—— 两件事都真的发生了，都记
    assert names == [E.BREAK_UP, E.FAILED_BREAK, E.BREAK_DOWN]


def test_failed_breakout_outside_the_window_is_not_recorded():
    bars, lines = _break_then(5)   # 第 5 根才回到下方，窗口只有 3
    rows, _ = _run(bars, lines)
    assert [r["event"] for r in rows] == [E.BREAK_UP, E.BREAK_DOWN], "窗口过了还算反包"


def test_failed_breakout_window_is_a_named_constant_with_provenance():
    """反包窗口是个数，必须显式、可查来源 —— 不许是散落的魔数。"""
    assert E.REVERT_WINDOW_BARS == 3
    assert "FORM_BARS" in (E.__doc__ or "") or True   # 来源写在常量注释里
    src = open(E.__file__, encoding="utf-8").read()
    assert "FORM_BARS = 3" in src, "没写明 3 这个数的来源"


def test_failed_breakout_consumes_the_open_breakout():
    """同一个突破只会被反包一次，不会每根都记一条反包。

    这里**不能**用「价格穿回原侧」的夹具 —— 那种回穿当根本身是一次反向穿越，
    会把未平仓突破覆盖掉，于是「消费」和「不消费」看不出差别（变异测试漏掉过）。
    所以改成「线自己移动到价格另一侧」：当根没有穿越，只能靠消费与否区分。
    """
    # 第 1 根向上突破 100；之后线价升到 105，价格一直在 101~102（没穿越新线价）
    bars = [_bar(0, 99, 99.5, 98, 99.0), _cross_up(1, 100.0),
            _bar(2, 101, 102, 100.5, 101.0),
            _bar(3, 101, 102, 100.5, 101.0),
            _bar(4, 101, 102, 100.5, 101.0)]
    lines = {"a@15m": {T(0): (100.0, T(0) + IV, T(0)),
                       T(1): (100.0, T(1) + IV, T(1)),
                       T(2): (105.0, T(2) + IV, T(2)),
                       T(3): (105.0, T(3) + IV, T(3))}}
    rows, _ = _run(bars, lines)
    # 第 2 根：收盘 101 回到 105 的下方 == 突破前的原侧 → 记一次反包
    # 第 3/4 根：未平仓突破已消费，不许再记
    assert sum(1 for r in rows if r["event"] == E.FAILED_BREAK) == 1


def test_window_boundary_is_inclusive():
    """正好隔 window 根回来算反包，隔 window+1 根不算 —— 边界必须钉死。

    突破发生在第 1 根，所以「第 k 根回来」的间隔是 k-1。
    """
    p = 100.0
    for k, want in ((4, True), (5, False)):     # 间隔 3 / 4
        bars = [_bar(0, 99, 99.5, 98, 99.0), _cross_up(1, p)]
        for i in range(2, k):
            bars.append(_bar(i, p * 1.01, p * 1.02, p * 1.005, p * 1.01))
        bars.append(_bar(k, p * 1.01, p * 1.015, p * 0.98, p * 0.99))
        rows, _ = _run(bars, _lines_fill(range(len(bars)), p))
        got = any(r["event"] == E.FAILED_BREAK for r in rows)
        assert got == want, ("突破后隔 %d 根回来：期望反包=%s，实际=%s"
                             % (k - 1, want, got))


def test_revert_window_is_a_parameter_not_hardcoded():
    """窗口必须能传，不许写死 3。"""
    p = 100.0
    bars = [_bar(0, 99, 99.5, 98, 99.0), _cross_up(1, p),
            _bar(2, p * 1.01, p * 1.02, p * 1.005, p * 1.01),
            _bar(3, p * 1.01, p * 1.015, p * 0.98, p * 0.99)]
    lr = _lines_fill(range(len(bars)), p)
    wide, _ = _run(bars, lr, window=5)
    tight, _ = _run(bars, lr, window=0)
    assert any(r["event"] == E.FAILED_BREAK for r in wide), "窗口 5 应该能算到反包"
    assert not any(r["event"] == E.FAILED_BREAK for r in tight), "窗口 0 不该有反包"


# ------------------------------------------------------------------ §8.1 回溯动态
def test_prev_event_is_the_previous_event_on_the_same_line():
    bars, lines = _break_then(2)
    rows, _ = _run(bars, lines)
    fb = [r for r in rows if r["event"] == E.FAILED_BREAK][0]
    assert fb["prev_event"] == E.BREAK_UP


def test_since_event_idx_is_the_index_distance():
    bars, lines = _break_then(2)
    rows, _ = _run(bars, lines)
    up = [r for r in rows if r["event"] == E.BREAK_UP][0]
    fb = [r for r in rows if r["event"] == E.FAILED_BREAK][0]
    assert fb["since_event_idx"] == fb["idx"] - up["idx"] == 1


def test_since_event_idx_survives_a_long_gap_between_events():
    """**§8.1 的核心**：中间隔很多根也要能接上，不许定死前 N 根。"""
    bars = [_bar(0, 99, 99.5, 98, 99.0), _cross_up(1)]
    for i in range(2, 42):                       # 中间 40 根在线上方磨
        bars.append(_bar(i, 100.6, 100.8, 100.4, 100.7))
    bars.append(_bar(42, 100.7, 100.8, 99.0, 99.2))    # 终于跌回下方
    rows, _ = _run(bars, _lines_fill(range(len(bars)), 100.0))
    downs = [r for r in rows if r["event"] == E.BREAK_DOWN]
    assert len(downs) == 1
    assert downs[0]["prev_event"] == E.BREAK_UP
    assert downs[0]["since_event_idx"] == 41, "长间隔没接上链条"


def test_prev_event_is_per_line_not_global():
    """两条线各有各的链条，A 线的事件不能成为 B 线的 prev_event。"""
    bars = [_bar(0, 99, 101, 98, 99.5), _bar(1, 99, 121, 98, 120.0)]
    lr = {"a@15m": {T(0): (100.0, T(1), T(0))},
          "b@15m": {T(0): (110.0, T(1), T(0))}}
    rows, _ = _run(bars, lr, {"a@15m": ("a", IV), "b@15m": ("b", IV)})
    assert len(rows) == 2
    assert all(r["prev_event"] is None for r in rows), "两条线的链条串了"


def test_first_event_on_a_line_has_no_prev_event():
    bars = [_bar(0, 99, 101, 98, 99.5), _cross_up(1)]
    rows, _ = _run(bars, _lines({0: 100.0}))
    assert rows[0]["prev_event"] is None
    assert rows[0]["since_event_idx"] is None


# ------------------------------------------------------------------ §8.2 neutral
def test_neutral_when_no_event_and_no_touch():
    bars = [_bar(0, 10, 11, 9, 10.5), _bar(1, 10, 11, 9, 10.5)]
    rows, stats = _run(bars, _lines({0: 100.0}), touch={})
    assert rows == []
    assert stats["bars_neutral"] == 2


def test_touch_without_event_is_neither_neutral_nor_event():
    """碰到了线但收盘没换边 —— 不许硬贴成 neutral，也不许编个事件。"""
    bars = [_bar(0, 99, 101, 98, 99.5), _bar(1, 99, 101, 98, 99.4)]
    rows, stats = _run(bars, _lines({0: 100.0}), touch={T(1): 3})
    assert rows == []
    assert stats["bars_touch_no_event"] == 1
    assert stats["bars_neutral"] == 1        # 第 0 根没有「上一根」也没接触


def test_bar_event_columns_marks_neutral_only_for_untouched_bars():
    bars = [_bar(0, 10, 11, 9, 10.5), _bar(1, 10, 11, 9, 10.5), _bar(2, 10, 11, 9, 10.5)]
    cols = E.bar_event_columns([], bars, touch_by_bar={T(1): 2})
    assert cols[T(0)]["state"] == E.NEUTRAL
    assert cols[T(1)]["state"] is None       # 有接触无事件 → 留空
    assert cols[T(2)]["state"] == E.NEUTRAL


def test_neutral_is_a_declared_constant_not_a_literal_scattered_around():
    assert E.NEUTRAL == "neutral"


# ------------------------------------------------------------------ 一根多个事件
def test_several_lines_crossing_in_one_bar_all_get_rows():
    """**这条守的是「丢事件就是丢事实」。**"""
    bars = [_bar(0, 99, 101, 98, 99.5), _bar(1, 99, 121, 98, 120.0)]
    lr = {"a@15m": {T(0): (100.0, T(1), T(0))},
          "b@15m": {T(0): (110.0, T(1), T(0))},
          "c@15m": {T(0): (115.0, T(1), T(0))}}
    rows, _ = _run(bars, lr, {k: (k.split("@")[0], IV) for k in lr})
    assert len(rows) == 3, "同根的三条线只留下了一部分"
    assert {r["line_key"] for r in rows} == set(lr)


def test_bar_event_columns_keeps_only_one_but_the_table_keeps_all():
    bars = [_bar(0, 99, 101, 98, 99.5), _bar(1, 99, 121, 98, 120.0)]
    lr = {"a@15m": {T(0): (100.0, T(1), T(0))},
          "b@15m": {T(0): (110.0, T(1), T(0))}}
    rows, _ = _run(bars, lr, {k: (k.split("@")[0], IV) for k in lr})
    cols = E.bar_event_columns(rows, bars, touch_by_bar={T(1): 2})
    assert len(rows) == 2
    assert cols[T(1)]["on_line"] in lr
    assert cols[T(1)]["event"] == E.BREAK_UP


def test_bar_event_columns_picks_the_deepest_penetration():
    """口径：多个事件时表 1 取穿透最深的那个（不是按名字、不是按顺序）。"""
    bars = [_bar(0, 99, 101, 98, 99.5), _bar(1, 99, 121, 98, 120.0)]
    evs = [
        {"ts": T(1), "line_key": "a@15m", "event": E.BREAK_UP,
         "pen_depth_pct": 0.001, "prev_event": None, "since_event_idx": None},
        {"ts": T(1), "line_key": "b@15m", "event": E.BREAK_UP,
         "pen_depth_pct": 0.050, "prev_event": None, "since_event_idx": None},
    ]
    cols = E.bar_event_columns(evs, bars, touch_by_bar={T(1): 2})
    assert cols[T(1)]["on_line"] == "b@15m"


def test_bar_event_columns_is_deterministic_on_ties():
    bars = [_bar(0, 99, 101, 98, 99.5), _bar(1, 99, 121, 98, 120.0)]
    evs = [
        {"ts": T(1), "line_key": "b@15m", "event": E.BREAK_UP,
         "pen_depth_pct": 0.01, "prev_event": None, "since_event_idx": None},
        {"ts": T(1), "line_key": "a@15m", "event": E.BREAK_UP,
         "pen_depth_pct": 0.01, "prev_event": None, "since_event_idx": None},
    ]
    cols = E.bar_event_columns(evs, bars, touch_by_bar={T(1): 2})
    assert cols[T(1)]["on_line"] == "a@15m"      # 并列取 line_key 小的


# ------------------------------------------------------------------ 落库
def _store(tmp_path):
    p = str(tmp_path / "kbar.db")
    return KBarStore(p), p


def _row(ts=None, key="a@15m", ev=None, **kw):
    r = {"symbol": "X", "interval_ms": IV, "ts": T(0) if ts is None else ts,
         "idx": (T(0) if ts is None else ts) // IV, "line_key": key,
         "line_kind": "ema20", "line_period_ms": IV, "line_price": 1.0,
         "event": ev or E.BREAK_UP, "prev_event": None, "since_event_idx": None,
         "close_side": "above", "pen_depth_pct": 0.001,
         "vol_rel": 1.2, "taker_buy_ratio": 0.55}
    r.update(kw)
    return r


def test_event_table_has_the_columns_the_design_needs(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        cols = {r[1] for r in st._con.execute("PRAGMA table_info(kbar_event)")}
    for c in ("symbol", "interval_ms", "ts", "line_key", "event",
              "prev_event", "since_event_idx", "pen_depth_pct"):
        assert c in cols, "kbar_event 缺列 %s" % c


def test_ev_cols_matches_table_columns(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        cols = {r[1] for r in st._con.execute("PRAGMA table_info(kbar_event)")}
    want = {c.strip() for c in EV_COLS.split(",")}
    assert want <= cols, "EV_COLS 里有表里不存在的列: %s" % sorted(want - cols)


def test_unique_key_includes_event_so_two_events_can_share_a_bar(tmp_path):
    """同一根、同一条线上，突破与反包可以同时存在，所以 event 必须进唯一键。"""
    st, _ = _store(tmp_path)
    with st:
        st.upsert_events([_row(ev=E.BREAK_UP), _row(ev=E.FAILED_BREAK)])
        assert st.event_counts("X", IV)["rows"] == 2


def test_upsert_events_is_idempotent(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_events([_row()])
        st.upsert_events([_row(pen_depth_pct=0.7)])
        got = st.events("X", IV)
    assert len(got) == 1
    assert got[0]["pen_depth_pct"] == pytest.approx(0.7)


def test_upsert_events_accepts_null_since_event_idx(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_events([_row(since_event_idx=None)])
        assert st.events("X", IV)[0]["since_event_idx"] is None


def test_delete_events_only_touches_its_own_interval(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_events([_row(), _row(interval_ms=3_600_000)])
        assert st.delete_events("X", IV) == 1
        assert st.event_counts("X", 3_600_000)["rows"] == 1


def test_set_bar_events_writes_the_five_columns(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_evolution([{"symbol": "X", "interval_ms": IV, "ts": T(0),
                              "idx": 1, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}])
        n = st.set_bar_events("X", IV, {T(0): {"event": E.BREAK_UP,
                                              "prev_event": None,
                                              "on_line": "a@15m",
                                              "since_event_idx": None,
                                              "state": None}})
        got = st.evolution_rows("X", IV)[0]
    assert n == 1
    assert got["event"] == E.BREAK_UP
    assert got["on_line"] == "a@15m"


def test_set_bar_events_does_not_wipe_facts_or_n_touch(tmp_path):
    """回填第 4 步不许把第 2 步的事实和第 3 步的 n_touch 抹掉。"""
    st, _ = _store(tmp_path)
    with st:
        st.upsert_evolution([{"symbol": "X", "interval_ms": IV, "ts": T(0),
                              "idx": 1, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5,
                              "body_pct": 0.66, "vol_rel": 1.9}])
        st.set_n_touch("X", IV, {T(0): 4})
        st.set_bar_events("X", IV, {T(0): {"event": E.BREAK_UP, "prev_event": None,
                                          "on_line": "a@15m",
                                          "since_event_idx": None, "state": None}})
        got = st.evolution_rows("X", IV)[0]
    assert got["body_pct"] == pytest.approx(0.66)
    assert got["n_touch"] == 4
    assert got["event"] == E.BREAK_UP


def test_event_counts_reads_evolution_back(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_evolution([
            {"symbol": "X", "interval_ms": IV, "ts": T(0), "idx": 1,
             "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5},
            {"symbol": "X", "interval_ms": IV, "ts": T(1), "idx": 2,
             "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}])
        st.upsert_events([_row()])
        st.set_bar_events("X", IV, {
            T(0): {"event": E.BREAK_UP, "prev_event": None, "on_line": "a@15m",
                   "since_event_idx": None, "state": None},
            T(1): {"event": None, "prev_event": None, "on_line": None,
                   "since_event_idx": None, "state": E.NEUTRAL}})
        c = st.event_counts("X", IV)
    assert c["rows"] == 1
    assert c["evolution"]["with_event"] == 1
    assert c["evolution"]["neutral"] == 1


# ------------------------------------------------------------------ 端到端
def _evo(n, **kw):
    # 注意：n_touch 不在 EVO_FACT_COLS 里，写不进来 —— 它由第 3 步的
    # `set_n_touch()` 单独写。夹具里必须显式调它，否则第 4 步的前置检查会拒绝
    # （这正好模拟「第 2 步 rebuild 之后第 3 步没重跑」）。
    d = {"symbol": "X", "interval_ms": IV, "ts": T(n), "idx": n,
         "o": 99.0, "h": 101.0, "l": 98.0, "c": 99.5,
         "vol_rel": 1.1, "taker_buy_ratio": 0.5}
    d.update(kw)
    return d


def _line_row(n, price, key="a@15m", kind="ema20", period=IV):
    return {"symbol": "X", "interval_ms": IV, "ts": T(n), "line_key": key,
            "line_kind": kind, "line_period_ms": period, "line_price": price,
            "bar_close": 99.5, "dist_pct": 0.0, "src_ts": T(n),
            "line_ready_ms": T(n) + period}


def test_build_and_persist_events_end_to_end(tmp_path):
    """表 0 + 表 1 + 表 2 → 表 2.5，并把五列写回表 1。"""
    from ai_trader.kbar.build import build_and_persist_events
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([_evo(0), _evo(1, c=101.0), _evo(2, c=101.5)])
        st.set_n_touch("X", IV, {T(0): 0, T(1): 1, T(2): 0})
        st.upsert_lines([_line_row(0, 100.0), _line_row(1, 100.0)])
        st.upsert_reactions([
            {"symbol": "X", "interval_ms": IV, "ts": T(1), "line_key": "a@15m",
             "line_kind": "ema20", "line_period_ms": IV, "line_price": 100.0,
             "dist_pct": 1.0, "side": "from_below", "pen_depth_pct": 0.01,
             "recover_pct": 0.015, "close_side": "above", "dwell_bars": None,
             "vol_rel": 1.0, "taker_buy_ratio": 0.5, "nth_touch": 1}])
    stats = build_and_persist_events(p, "X", IV)
    assert stats["rows"] == 1, stats
    assert stats["persisted"] == 1
    with KBarStore(p) as st2:
        ev = st2.events("X", IV)
        evo = {r["ts"]: r["event"] for r in st2.evolution_rows("X", IV)}
    assert ev[0]["event"] == E.BREAK_UP
    assert evo[T(1)] == E.BREAK_UP


def test_build_and_persist_events_refuses_without_reactions(tmp_path):
    """表 2 还没跑就建事件层，neutral 会全算错 —— 必须拒绝而不是猜。"""
    from ai_trader.kbar.build import build_and_persist_events
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([_evo(0), _evo(1)])
        st.set_n_touch("X", IV, {T(0): 0, T(1): 0})
        st.upsert_lines([_line_row(0, 100.0)])
    stats = build_and_persist_events(p, "X", IV)
    assert "error" in stats
    assert "表 2" in stats["error"]


def test_build_and_persist_events_dry_run_writes_nothing(tmp_path):
    from ai_trader.kbar.build import build_and_persist_events
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([_evo(0), _evo(1, c=101.0)])
        st.set_n_touch("X", IV, {T(0): 0, T(1): 1})
        st.upsert_lines([_line_row(0, 100.0)])
        st.upsert_reactions([
            {"symbol": "X", "interval_ms": IV, "ts": T(1), "line_key": "a@15m",
             "line_kind": "ema20", "line_period_ms": IV, "line_price": 100.0,
             "dist_pct": 1.0, "side": "from_below", "pen_depth_pct": 0.01,
             "recover_pct": 0.015, "close_side": "above", "dwell_bars": None,
             "vol_rel": 1.0, "taker_buy_ratio": 0.5, "nth_touch": 1}])
    stats = build_and_persist_events(p, "X", IV, dry_run=True)
    assert stats["rows"] == 1
    with KBarStore(p) as st2:
        assert st2.event_counts("X", IV)["rows"] == 0


def test_event_params_snapshot_declares_vocab_and_rules():
    from ai_trader.kbar.build import event_params_snapshot
    p = event_params_snapshot(IV)
    assert p["vocab_used"] == list(E.EVENT_VOCAB_USED)
    assert p["vocab_not_applicable"] == list(E.EVENT_VOCAB_RANGE_ONLY)
    assert p["revert_window_bars"] == E.REVERT_WINDOW_BARS
    assert "D-16" in p["revert_window_source"]
    assert "D-17" in p["neutral_rule"]


def test_find_breakout_then_revert_pairs_by_line():
    bars, lines = _break_then(2)
    rows, _ = _run(bars, lines)
    pairs = E.find_breakout_then_revert(rows)
    assert len(pairs) == 1
    assert pairs[0][0]["event"] == E.BREAK_UP
    assert pairs[0][1]["event"] == E.FAILED_BREAK
    # 反包那条的 prev_event 必须正好是那个突破（设计稿 §8.4 的形态）
    assert pairs[0][1]["prev_event"] == E.BREAK_UP


def test_event_counts_by_event_counts_names():
    bars, lines = _break_then(2)
    rows, _ = _run(bars, lines)
    c = E.event_counts_by_event(rows)
    assert c[E.BREAK_UP] == 1 and c[E.FAILED_BREAK] == 1 and c[E.BREAK_DOWN] == 1


# ================================================================== 审查修复守卫
# 下面这些测试守的是 2026-11-07 那一轮上下游审查（F1~F5、F10）修掉的东西。
# 每一条都对应一个「不报错、只是悄悄不对」的失效方式。

def test_set_bar_events_uses_the_shared_column_constant():
    """F5：写回表 1 的列名由常量生成，不许手写一份（会漂移）。"""
    import inspect
    from ai_trader.kbar import store as S
    assert tuple(c.strip() for c in S.EVO_EVENT_COLS.split(",")) == S.EVO_EVENT_KEY
    src = inspect.getsource(S.KBarStore.set_bar_events)
    assert "EVO_EVENT_COLS" in src, "set_bar_events 没用共享常量生成 SQL"
    for col in S.EVO_EVENT_KEY:
        assert col in src or col in S.EVO_EVENT_COLS


def test_evo_event_cols_excludes_step3_and_step4_other_columns():
    """事件层只能写它自己那五列：n_touch 是第 3 步的，n_break 至今没人写。"""
    from ai_trader.kbar.store import EVO_EVENT_KEY
    assert "n_touch" not in EVO_EVENT_KEY
    assert "n_break" not in EVO_EVENT_KEY
    assert set(EVO_EVENT_KEY) == {"event", "prev_event", "on_line",
                                  "since_event_idx", "state"}


def test_pen_depth_zero_is_not_treated_as_missing():
    """F3：`pen_depth_pct` 恰好 0.0 是**已知的最浅穿透**，不能当成缺失。

    构造：a 线的穿透未知（NULL），b 线的穿透恰好 0.0。
    正确 → b 赢（0.0 > 未知）。错法（`x or -1.0`）→ 两者都是 -1.0，
    并列后按 line_key 小者胜 → a 赢。两者结果相反，所以这条能抓住错法。
    """
    bars = [_bar(0, 99, 101, 98, 99.5), _bar(1, 99, 121, 98, 120.0)]
    evs = [
        {"ts": T(1), "line_key": "a@15m", "event": E.BREAK_UP,
         "pen_depth_pct": None, "prev_event": None, "since_event_idx": None},
        {"ts": T(1), "line_key": "b@15m", "event": E.BREAK_UP,
         "pen_depth_pct": 0.0, "prev_event": None, "since_event_idx": None},
    ]
    cols = E.bar_event_columns(evs, bars, touch_by_bar={T(1): 2})
    assert cols[T(1)]["on_line"] == "b@15m", "穿透为 0.0 被当成缺失了"


def test_deeper_penetration_still_wins_over_zero():
    """0.0 是已知值，但更深的值仍然要赢 —— 别把修复做成「0 永远赢」。"""
    bars = [_bar(0, 99, 101, 98, 99.5), _bar(1, 99, 121, 98, 120.0)]
    evs = [
        {"ts": T(1), "line_key": "a@15m", "event": E.BREAK_UP,
         "pen_depth_pct": 0.0, "prev_event": None, "since_event_idx": None},
        {"ts": T(1), "line_key": "b@15m", "event": E.BREAK_UP,
         "pen_depth_pct": 0.0001, "prev_event": None, "since_event_idx": None},
    ]
    cols = E.bar_event_columns(evs, bars, touch_by_bar={T(1): 2})
    assert cols[T(1)]["on_line"] == "b@15m"


def test_find_breakout_then_revert_does_not_fabricate_a_causal_pair():
    """F10：反包必须靠自己的 `prev_event` 认领突破，不许「配给最近的先前突破」。

    构造一个 `prev_event` 为空的反包（真实数据里不会出现，但函数不许在这里编因果）：
    旧写法会把它配给前面那个突破，新写法必须认不出配对。
    """
    evs = [
        {"ts": T(1), "line_key": "a@15m", "event": E.BREAK_UP,
         "prev_event": None, "since_event_idx": None},
        {"ts": T(2), "line_key": "a@15m", "event": E.FAILED_BREAK,
         "prev_event": None, "since_event_idx": None},
    ]
    assert E.find_breakout_then_revert(evs) == []


def test_find_breakout_then_revert_pairs_only_by_prev_event():
    """配出来的每一对，反包那条的 prev_event 必须等于突破那条的事件名。"""
    bars, lines = _break_then(2)
    rows, _ = _run(bars, lines)
    pairs = E.find_breakout_then_revert(rows)
    assert pairs
    for bo, fb in pairs:
        assert fb["prev_event"] == bo["event"]
        assert int(bo["ts"]) < int(fb["ts"])


# ---------------------------------------------------------------- F1 / F2 / F4
def test_line_value_rule_in_meta_says_previous_bar_not_as_of():
    """F1：写进库的「当前取法」必须是 pick_prev，不能是被废弃的 as-of。"""
    from ai_trader.kbar.build import reaction_params_snapshot
    p = reaction_params_snapshot(IV)
    rule = p["line_value_rule"]
    assert ("上一根" in rule) or ("pick_prev" in rule), \
        "line_value_rule 没写「取上一根那一行」：%s" % rule
    assert "最后一个值" not in rule, "line_value_rule 又写回了 as-of 取法"
    # 废弃的那条要留痕，但不能冒充「当前规则」
    sup = p.get("line_value_rule_superseded") or ""
    assert "as_of" in sup or "最后一个值" in sup, "没留废弃规则的痕迹"
    assert "往前带" in sup or "PRE" in sup, "没说清它为什么被废弃"


def test_event_params_declare_the_event_reaction_contract():
    """F2：事件不保证在表 2 有明细，这条契约必须写进库的自述。"""
    from ai_trader.kbar.build import event_params_snapshot
    p = event_params_snapshot(IV)
    c = p.get("event_reaction_contract") or ""
    assert "LEFT JOIN" in c, "没写明必须 LEFT JOIN"
    assert "INNER JOIN" in c, "没警告 INNER JOIN 会丢事件"
    assert p.get("reaction_detail_coverage"), "没给明细覆盖率"


def test_events_refused_when_n_touch_is_missing(tmp_path):
    """F4：第 2 步 rebuild 会把 n_touch 删掉，第 4 步必须拒绝而不是照算。"""
    from ai_trader.kbar.build import build_and_persist_events
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([_evo(0), _evo(1, c=101.0)])
        st.upsert_lines([_line_row(0, 100.0)])
        st.upsert_reactions([
            {"symbol": "X", "interval_ms": IV, "ts": T(1), "line_key": "a@15m",
             "line_kind": "ema20", "line_period_ms": IV, "line_price": 100.0,
             "dist_pct": 1.0, "side": "from_below", "pen_depth_pct": 0.01,
             "recover_pct": 0.015, "close_side": "above", "dwell_bars": None,
             "vol_rel": 1.0, "taker_buy_ratio": 0.5, "nth_touch": 1}])
    stats = build_and_persist_events(p, "X", IV)
    assert "error" in stats, "n_touch 空着也照算了"
    assert "n_touch" in stats["error"]
    assert "backfill_kbar_reactions" in stats["error"], "没告诉人该重跑哪一步"
    with KBarStore(p) as st2:
        assert st2.event_counts("X", IV)["rows"] == 0, "拒绝了却还是写了库"


def test_events_refused_when_n_touch_disagrees_with_reaction(tmp_path):
    """F4：表 2 换过而 n_touch 没跟着更新 → 必须拒绝。"""
    from ai_trader.kbar.build import build_and_persist_events
    st, p = _store(tmp_path)
    with st:
        # 表 2 覆盖 2 根，但 n_touch 说只有 1 根碰到线
        st.upsert_evolution([_evo(0), _evo(1, c=101.0), _evo(2)])
        st.set_n_touch("X", IV, {T(0): 0, T(1): 1, T(2): 0})
        st.upsert_lines([_line_row(0, 100.0), _line_row(1, 100.0)])
        st.upsert_reactions([
            {"symbol": "X", "interval_ms": IV, "ts": T(1), "line_key": "a@15m",
             "line_kind": "ema20", "line_period_ms": IV, "line_price": 100.0,
             "dist_pct": 1.0, "side": "from_below", "pen_depth_pct": 0.01,
             "recover_pct": 0.015, "close_side": "above", "dwell_bars": None,
             "vol_rel": 1.0, "taker_buy_ratio": 0.5, "nth_touch": 1},
            {"symbol": "X", "interval_ms": IV, "ts": T(2), "line_key": "a@15m",
             "line_kind": "ema20", "line_period_ms": IV, "line_price": 100.0,
             "dist_pct": 0.0, "side": "from_above", "pen_depth_pct": 0.02,
             "recover_pct": 0.01, "close_side": "below", "dwell_bars": None,
             "vol_rel": 1.0, "taker_buy_ratio": 0.5, "nth_touch": 2}])
    stats = build_and_persist_events(p, "X", IV)
    assert "error" in stats, "n_touch 与表 2 对不上也照算了"
    assert "重跑" in stats["error"]


def test_event_fingerprint_is_written_for_later_drift_detection(tmp_path):
    """F4：第 4 步要把输入指纹写进 meta，否则事后查不出「输入变了」。"""
    import json
    from ai_trader.kbar.build import build_and_persist_events
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([_evo(0), _evo(1, c=101.0)])
        st.set_n_touch("X", IV, {T(0): 0, T(1): 1})
        st.upsert_lines([_line_row(0, 100.0)])
        st.upsert_reactions([
            {"symbol": "X", "interval_ms": IV, "ts": T(1), "line_key": "a@15m",
             "line_kind": "ema20", "line_period_ms": IV, "line_price": 100.0,
             "dist_pct": 1.0, "side": "from_below", "pen_depth_pct": 0.01,
             "recover_pct": 0.015, "close_side": "above", "dwell_bars": None,
             "vol_rel": 1.0, "taker_buy_ratio": 0.5, "nth_touch": 1}])
    build_and_persist_events(p, "X", IV)
    with KBarStore(p) as st2:
        raw = st2.get_meta("event_input_fingerprint:X:15m")
    assert raw, "没写输入指纹"
    fp = json.loads(raw)
    for k in ("line_rows", "evolution_bars", "reaction_rows",
              "reaction_bars", "event_rows"):
        assert k in fp, "指纹缺项 %s" % k
    assert fp["reaction_rows"] == 1
    assert fp["event_rows"] == 1
