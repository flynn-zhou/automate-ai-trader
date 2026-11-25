# -*- coding: utf-8 -*-
"""第 5 步 · 事后层（表 3 `kbar_forward`）的测试。

守两类东西：

  1. **算术**：ret_k / held 的口径，尤其是「取错根」和「方向反了」这类不出错的错法。
  2. **隔离**：事后层禁止参与当下判定。这条靠扫源码验证 ——
     因为「不小心在 event.py 里 import 了 forward」不会让任何功能变红，
     只会让回测偷未来。
"""
from __future__ import annotations

import os
import re

import pytest

from ai_trader.kbar import forward as W
from ai_trader.kbar.store import EVO_FACT_COLS, FD_COLS, KBarStore

IV = 900_000
BASE = 1600000000000
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KBAR_DIR = os.path.join(ROOT, "ai_trader", "kbar")


def T(n):
    return BASE + n * IV


def _bar(n, c, o=None, h=None, l=None):
    o = c if o is None else o
    h = max(o, c) + 1.0 if h is None else h
    l = min(o, c) - 1.0 if l is None else l
    return {"ts": T(n), "o": float(o), "h": float(h), "l": float(l), "c": float(c)}


def _bars(closes):
    return [_bar(i, c) for i, c in enumerate(closes)]


# ================================================================== 收益口径
def test_ret_uses_the_bar_k_ahead_not_the_current_one():
    """ret1 必须是 c[t+1]/c[t]-1。

    这是本步最危险的错法：拿当根算，值恒等于 0，**不会报错**，
    只会让「之后涨了多少」这个数全部消失（而且看起来像「都没涨」）。
    """
    bars = _bars([100.0, 110.0, 121.0])
    r = W.forward_returns(bars, horizons=(1,))
    assert abs(r[T(0)][1] - 0.10) < 1e-12
    assert abs(r[T(1)][1] - 0.10) < 1e-12
    assert r[T(2)][1] is None, "最后一根后面没有 K 线，必须是 None"


def test_ret_is_not_the_previous_bar():
    """也不能取「上一根」—— 那等于把已经发生的事说成将要发生。"""
    bars = _bars([50.0, 100.0, 110.0])
    r = W.forward_returns(bars, horizons=(1,))
    # 用上一根会得到 100/50-1 = 1.0（翻倍），正确值是 0.10
    assert abs(r[T(1)][1] - 0.10) < 1e-12, "取成上一根了"


def test_ret_multi_horizon_is_each_its_own_distance():
    bars = _bars([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    r = W.forward_returns(bars, horizons=(1, 3, 5))
    assert abs(r[T(0)][1] - 0.01) < 1e-12
    assert abs(r[T(0)][3] - 0.03) < 1e-12
    assert abs(r[T(0)][5] - 0.05) < 1e-12


def test_ret_leaves_the_tail_empty_instead_of_using_what_it_has():
    """末尾不足 k 根时留空，不许拿已经走过的凑。"""
    bars = _bars([100.0, 101.0, 102.0])
    r = W.forward_returns(bars, horizons=(1, 3, 5))
    assert r[T(1)][1] is not None
    assert r[T(2)][1] is None
    assert r[T(0)][3] is None, "只有 3 根却给了 ret3"
    assert r[T(0)][5] is None


def test_ret_handles_missing_close():
    bars = _bars([100.0, 101.0])
    bars[1]["c"] = None
    r = W.forward_returns(bars, horizons=(1,))
    assert r[T(0)][1] is None


def test_ret_fee_is_zero_by_default_so_the_number_is_pure_price_ratio():
    assert W.RET_FEE == 0.0
    bars = _bars([100.0, 110.0])
    r = W.forward_returns(bars, horizons=(1,))
    assert abs(r[T(0)][1] - 0.10) < 1e-12


# ================================================================== 守住口径
def test_held_one_when_price_stays_on_the_same_side():
    bars = _bars([100.0, 101.0, 102.0, 103.0, 104.0])
    # 参照价 99.5，之后 3 根收盘都在上方
    assert W.held_of(bars, T(0), 99.5, W.SIDE_ABOVE, held_bars=3) == 1


def test_held_zero_when_price_comes_back_across():
    bars = _bars([100.0, 101.0, 98.0, 99.0, 100.0])
    assert W.held_of(bars, T(0), 99.5, W.SIDE_ABOVE, held_bars=3) == 0


def test_held_checks_the_last_of_the_k_bars_too():
    """边界必须在第 k 根上，不能只看前 k-1 根。

    只查前 k-1 根的话，「第 k 根才跌回来」会被判成守住 —— 而第 k 根恰好是
    这条口径真正关心的那一根（「3 根之内守没守住」）。
    """
    bars = _bars([100.0, 101.0, 102.0, 98.0, 100.0])
    # held_bars=3：t+1=101, t+2=102 都守住，t+3=98 跌破 → 必须 0
    assert W.held_of(bars, T(0), 99.5, W.SIDE_ABOVE, held_bars=3) == 0
    # 只看到 t+2 就会误判成 1 —— 用 held_bars=2 确认那一版是 1（证明这条夹具能区分）
    assert W.held_of(bars, T(0), 99.5, W.SIDE_ABOVE, held_bars=2) == 1


def test_held_none_when_not_enough_future_bars():
    bars = _bars([100.0, 101.0])
    assert W.held_of(bars, T(0), 99.5, W.SIDE_ABOVE, held_bars=3) is None


def test_held_none_for_unknown_ts_or_no_side():
    bars = _bars([100.0, 101.0, 102.0, 103.0])
    assert W.held_of(bars, T(99), 99.5, W.SIDE_ABOVE) is None
    assert W.held_of(bars, T(0), None, W.SIDE_ABOVE) is None
    assert W.held_of(bars, T(0), 99.5, W.SIDE_AT) is None


def test_held_uses_the_fixed_reference_price_not_a_moving_one():
    """参照价固定：价格回到**这条水平线**的另一侧才算没守住。

    这里线价如果跟着某种移动均线往下走，用移动价会得到 1，用固定价得到 0。
    两种问法都合理，但必须是同一个（D-20），不能在不同根上悄悄换。
    """
    bars = _bars([100.0, 90.0, 90.0, 90.0, 90.0])
    # 固定参照价 95：t+1 就跌破了 → 0
    assert W.held_of(bars, T(0), 95.0, W.SIDE_ABOVE, held_bars=3) == 0
    # 若参照价是 89（宽松），则全都在上方 → 1
    assert W.held_of(bars, T(0), 89.0, W.SIDE_ABOVE, held_bars=3) == 1


def test_held_tolerance_widens_the_neutral_band_not_the_side():
    """容差是「中性带」：它让**擦着线**的收盘算作哪一侧，而不是把线上的说成上方。

    容差 0：收盘正好等于线价 → at（既不算上方也不算下方）。
    容差 0.1%：100 的线价有 ±0.1 的带子，收盘 99.95 就落进带子里 → 仍是 at。
    要让它算「上方」，收盘得高于 100.1；要让它算「下方」，得低于 99.9。
    """
    at_line = [_bar(0, 100.0), _bar(1, 100.0), _bar(2, 101.0), _bar(3, 102.0)]
    # 容差 0：t+1 正好压在线上 → 不算「在上方」
    assert W.held_of(at_line, T(0), 100.0, W.SIDE_ABOVE, held_bars=3, tol_pct=0.0) == 0

    near = [_bar(0, 100.0), _bar(1, 99.95), _bar(2, 101.0), _bar(3, 102.0)]
    # 容差 0：99.95 < 100 → 下方 → 没守住
    assert W.held_of(near, T(0), 100.0, W.SIDE_ABOVE, held_bars=3, tol_pct=0.0) == 0
    # 容差 0.1%（±0.1）：99.95 在带子里 → 仍是 at → 还是没守住（at 不算 above）
    assert W.held_of(near, T(0), 100.0, W.SIDE_ABOVE, held_bars=3,
                     tol_pct=0.001) == 0
    # 容差 1%（±1）：99.95 在带子里；把带子放宽到 2% 也一样是 at
    # 真正能变的是「低于带子下限但高于容差下限」—— 这里用 0.05% 的带子验证边界
    inside = [_bar(0, 100.0), _bar(1, 100.05), _bar(2, 101.0), _bar(3, 102.0)]
    # 容差 0：100.05 > 100 → 上方 → 守住
    assert W.held_of(inside, T(0), 100.0, W.SIDE_ABOVE, held_bars=3, tol_pct=0.0) == 1
    # 容差 0.1%（±0.1）：100.05 落进带子 → at → 不守住
    assert W.held_of(inside, T(0), 100.0, W.SIDE_ABOVE, held_bars=3,
                     tol_pct=0.001) == 0


def test_held_side_for_breakout_follows_the_event_direction():
    # 向上突破判的是「站上上方」；向下突破判的是「跌到下方」
    assert W.held_side_for(W.BREAK_UP, 100.0, 99.0) == W.SIDE_ABOVE
    assert W.held_side_for(W.BREAK_DOWN, 98.0, 99.0) == W.SIDE_BELOW


def test_held_side_for_revert_uses_the_close_side_of_that_bar():
    """反包没有自己的穿越方向，用当根收盘所在侧，不临时发明一个方向。"""
    assert W.held_side_for(W.FAILED_BREAK, 101.0, 100.0) == W.SIDE_ABOVE
    assert W.held_side_for(W.FAILED_BREAK, 99.0, 100.0) == W.SIDE_BELOW
    assert W.held_side_for(W.FAILED_BREAK, 100.0, 100.0) is None, "压在线上没有方向"


# ================================================================== 整表
def test_build_forward_one_row_per_bar_and_neutral_bars_have_no_held():
    bars = _bars([100.0] * 6)
    # 只有第 1 根有事件；第 2 根是 neutral（state 非空但 event 空）
    bar_events = {T(1): {"event": W.BREAK_UP, "on_line": "a@15m"}}
    ev_rows = [{"ts": T(1), "line_key": "a@15m", "line_price": 99.0}]
    rows, st = W.build_forward("X", IV, bars, bar_events, event_rows=ev_rows,
                              held_bars=3)
    assert len(rows) == len(bars)
    held = {r["ts"]: r["held"] for r in rows}
    assert held[T(1)] == 1
    for ts in (T(0), T(2), T(3), T(4), T(5)):
        assert held[ts] is None, "无事件的根不许填 held"
    assert st["bars_with_event"] == 1
    assert st["held_1"] == 1 and st["held_0"] == 0


def test_build_forward_marks_ref_missing_when_no_event_row():
    """表 1 有事件但表 2.5 没有那条明细 → held 空，并计进 held_ref_missing。"""
    bars = _bars([100.0] * 6)
    bar_events = {T(1): {"event": W.BREAK_UP, "on_line": "a@15m"}}
    rows, st = W.build_forward("X", IV, bars, bar_events, event_rows=[])
    assert {r["ts"]: r["held"] for r in rows}[T(1)] is None
    assert st["held_ref_missing"] == 1


def test_build_forward_writes_the_audit_columns():
    """held 必须能回答「是哪条线的哪一侧、以什么价为准」。"""
    bars = _bars([100.0] * 6)
    bar_events = {T(1): {"event": W.BREAK_DOWN, "on_line": "b@15m"}}
    ev_rows = [{"ts": T(1), "line_key": "b@15m", "line_price": 105.0}]
    rows, _ = W.build_forward("X", IV, bars, bar_events, event_rows=ev_rows,
                              held_bars=3)
    r = {x["ts"]: x for x in rows}[T(1)]
    assert r["held_line"] == "b@15m"
    assert r["held_ref_price"] == 105.0
    assert r["held_side"] == W.SIDE_BELOW


# ================================================================== 自查断言
def test_assert_no_lookahead_catches_a_ret_computed_on_the_wrong_bar():
    """如果 ret 被算成别的根，assert 必须炸 —— 否则这层没有任何自保能力。"""
    bars = _bars([100.0, 110.0, 121.0])
    rows, _ = W.build_forward("X", IV, bars, {}, event_rows=[])
    rows[0]["ret1"] = 0.0            # 假装用当根算的
    with pytest.raises(W.ForwardLookaheadError):
        W.assert_no_lookahead(rows, bars)


def test_assert_no_lookahead_catches_a_missing_tail_value():
    bars = _bars([100.0, 110.0])
    rows, _ = W.build_forward("X", IV, bars, {}, event_rows=[])
    rows[1]["ret1"] = 0.5            # 最后一根不该有 ret1
    with pytest.raises(W.ForwardLookaheadError):
        W.assert_no_lookahead(rows, bars)


def test_assert_no_lookahead_catches_a_mid_table_null():
    """中间某根的 ret 该有值却空了 —— 这是「静默丢数」，必须报。

    只查「尾部有没有多给值」是不够的：把中间某根置空不会让任何下游报错，
    只会让统计的分母莫名变小。
    """
    bars = _bars([100.0, 110.0, 121.0, 133.1])
    rows, _ = W.build_forward("X", IV, bars, {}, event_rows=[])
    assert rows[0]["ret1"] is not None
    rows[0]["ret1"] = None
    with pytest.raises(W.ForwardLookaheadError):
        W.assert_no_lookahead(rows, bars)


def test_assert_no_lookahead_catches_row_count_mismatch():
    bars = _bars([100.0, 110.0])
    rows, _ = W.build_forward("X", IV, bars, {}, event_rows=[])
    with pytest.raises(W.ForwardLookaheadError):
        W.assert_no_lookahead(rows[:-1], bars)


def test_assert_no_lookahead_passes_on_a_clean_table():
    bars = _bars([100.0, 110.0, 121.0, 133.1, 146.41, 161.05])
    rows, _ = W.build_forward("X", IV, bars, {}, event_rows=[])
    W.assert_no_lookahead(rows, bars)


# ================================================================== 隔离红线
def test_current_judgement_path_never_imports_forward():
    """**这条是本步最重要的一条。**

    事后层允许读之后的 K 线（那是它的工作）。所以一旦当下判定路径 import 了它，
    回测就会偷未来，而且**不会有任何功能变红**。只能靠扫源码守着。
    """
    offenders = []
    for fn in ("lines.py", "facts.py", "reaction.py", "event.py"):
        src = open(os.path.join(KBAR_DIR, fn), encoding="utf-8").read()
        if _imports_forward(src):
            offenders.append(fn)
    assert not offenders, "这些当下判定模块 import 了事后层：%s" % offenders


# 四种写法都要认：
#   from .forward import x        from . import forward
#   from ai_trader.kbar.forward   import ai_trader.kbar.forward
#   from ai_trader.kbar import forward
_FORWARD_IMPORT_PATTERNS = (
    r"^\s*from\s+\.+\s*forward\b",
    r"^\s*from\s+\.+\s+import\s+forward\b",
    r"^\s*from\s+ai_trader\.kbar\.forward\b",
    r"^\s*from\s+ai_trader\.kbar\s+import\s+[^\n]*\bforward\b",
    r"^\s*import\s+ai_trader\.kbar\.forward\b",
)


def _imports_forward(src: str) -> bool:
    return any(re.search(p, src, re.M) for p in _FORWARD_IMPORT_PATTERNS)


def test_the_isolation_scan_itself_catches_every_import_form():
    """守卫的守卫：扫描器必须认得每一种 import 写法。

    这条是给 M19 那个漏掉补的 —— `from . import forward` 这种写法
    原来扫不出来，于是「红线破了」也全绿。
    """
    bad_samples = [
        "from .forward import held_of",
        "from . import forward as _fw",
        "from ai_trader.kbar.forward import FORWARD_HORIZONS",
        "from ai_trader.kbar import forward",
        "import ai_trader.kbar.forward",
    ]
    for smp in bad_samples:
        assert _imports_forward(smp + "\n"), "扫描器漏掉了这种写法：%r" % smp
    good_samples = [
        "from .reaction import pick_prev",
        "# 事后层禁止参与当下判定：不 import forward",
        "from .build import build_and_persist_forward",  # 这是 build 里的定义处
    ]
    for smp in good_samples:
        assert not _imports_forward(smp + "\n"), "扫描器误报：%r" % smp


def test_judgement_entry_points_do_not_call_the_forward_builder():
    """第 1~4 步的入口不许调 build_and_persist_forward。"""
    src = open(os.path.join(KBAR_DIR, "build.py"), encoding="utf-8").read()
    calls = len(re.findall(r"build_and_persist_forward\(", src))
    assert calls == 1, ("build.py 里出现了 %d 次 build_and_persist_forward —— "
                        "只许定义（1 次），不许被别的步骤调用" % calls)


def test_forward_module_does_not_import_the_judgement_modules():
    """反向也要干净：事后层不该反过来依赖事件的判定实现（避免口径被悄悄共享）。"""
    src = open(os.path.join(KBAR_DIR, "forward.py"), encoding="utf-8").read()
    for mod in ("reaction", "event"):
        assert not re.search(r"^\s*from\s+\.\s*%s\b" % mod, src, re.M), \
            "forward.py import 了 %s —— 两层的代码路径要分开" % mod


# ================================================================== 表结构
def _store(tmp_path):
    p = str(tmp_path / "t.db")
    return KBarStore(p), p


def test_table3_exists_with_the_design_columns(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        cols = {r[1] for r in st._con.execute("PRAGMA table_info(kbar_forward)")}
    for c in ("symbol", "interval_ms", "ts", "ret1", "ret3", "ret5", "held"):
        assert c in cols, "表 3 缺设计稿要求的列 %s" % c
    for c in ("held_line", "held_ref_price", "held_side"):
        assert c in cols, "表 3 缺审计列 %s" % c


def test_fd_cols_matches_the_table(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        cols = {r[1] for r in st._con.execute("PRAGMA table_info(kbar_forward)")}
    want = {c.strip() for c in FD_COLS.split(",")}
    assert want <= cols, "FD_COLS 里有表里不存在的列：%s" % sorted(want - cols)


def test_table3_unique_key_is_per_bar_not_per_line(tmp_path):
    """表的粒度是「根」，不是「根 × 线」—— 所以唯一键里不许出现 line_key。"""
    st, _ = _store(tmp_path)
    with st:
        st.upsert_forward([{"symbol": "X", "interval_ms": IV, "ts": T(0),
                            "ret1": 0.01, "ret3": None, "ret5": None,
                            "held": 1, "held_line": "a@15m",
                            "held_ref_price": 99.0, "held_side": "above"}])
        st.upsert_forward([{"symbol": "X", "interval_ms": IV, "ts": T(0),
                            "ret1": 0.02, "ret3": None, "ret5": None,
                            "held": 0, "held_line": "b@15m",
                            "held_ref_price": 101.0, "held_side": "below"}])
        rows = st.forward_rows("X", IV)
    assert len(rows) == 1, "同一根写了两行 —— 唯一键没生效"
    assert rows[0]["ret1"] == 0.02 and rows[0]["held"] == 0, "后写的没覆盖先写的"


def test_forward_upsert_does_not_touch_other_tables(tmp_path):
    """写表 3 不该影响表 1 的任何列（尤其第 3/4 步的成果）。"""
    st, _ = _store(tmp_path)
    before = {c.strip() for c in EVO_FACT_COLS.split(",")}
    with st:
        st.upsert_evolution([{"symbol": "X", "interval_ms": IV, "ts": T(0),
                              "idx": 0, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}])
        st.set_n_touch("X", IV, {T(0): 1})
        st.upsert_forward([{"symbol": "X", "interval_ms": IV, "ts": T(0),
                            "ret1": 0.01}])
        ev = st.evolution_rows("X", IV)
    assert ev[0]["o"] == 1.0
    assert ev[0]["n_touch"] == 1, "写事后层把 n_touch 抹掉了"
    assert "kbar_forward" not in before


def test_forward_counts_reports_fill_and_distribution(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_forward([
            {"symbol": "X", "interval_ms": IV, "ts": T(0), "ret1": 0.01,
             "ret3": None, "ret5": None, "held": 1, "held_line": "a@15m",
             "held_ref_price": 1.0, "held_side": "above"},
            {"symbol": "X", "interval_ms": IV, "ts": T(1), "ret1": 0.02,
             "ret3": None, "ret5": None, "held": 0, "held_line": "a@15m",
             "held_ref_price": 1.0, "held_side": "above"},
            {"symbol": "X", "interval_ms": IV, "ts": T(2), "ret1": None},
        ])
        c = st.forward_counts("X", IV)
    assert c["rows"] == 3
    assert c["ret1"] == 2
    assert c["held"] == 2
    assert c["by_held"] == {1: 1, 0: 1, "null": 1}


def test_delete_forward_only_removes_that_symbol_and_interval(tmp_path):
    st, _ = _store(tmp_path)
    with st:
        st.upsert_forward([
            {"symbol": "X", "interval_ms": IV, "ts": T(0), "ret1": 0.1},
            {"symbol": "Y", "interval_ms": IV, "ts": T(0), "ret1": 0.2},
        ])
        n = st.delete_forward("X", IV)
        left_x = st.forward_counts("X", IV)["rows"]
        left_y = st.forward_counts("Y", IV)["rows"]
    assert n == 1 and left_x == 0 and left_y == 1


# ================================================================== 端到端
def test_build_and_persist_forward_end_to_end(tmp_path):
    from ai_trader.kbar.build import build_and_persist_forward
    st, p = _store(tmp_path)
    closes = [100.0, 110.0, 121.0, 133.1, 146.41, 161.05]
    with st:
        st.upsert_evolution([
            {"symbol": "X", "interval_ms": IV, "ts": T(i), "idx": i,
             "o": c, "h": c + 1, "l": c - 1, "c": c} for i, c in enumerate(closes)])
        st.set_n_touch("X", IV, {T(i): 0 for i in range(len(closes))})
        # 给第 1 根一个事件（表 1 的五列 + 表 2.5 的明细）
        st.set_bar_events("X", IV, {T(1): {"event": "向上突破",
                                           "prev_event": None,
                                           "on_line": "a@15m",
                                           "since_event_idx": None,
                                           "state": None}})
        st.upsert_events([{"symbol": "X", "interval_ms": IV, "ts": T(1), "idx": 1,
                           "line_key": "a@15m", "line_kind": "ema20",
                           "line_period_ms": IV, "line_price": 109.0,
                           "event": "向上突破", "prev_event": None,
                           "since_event_idx": None, "close_side": "above",
                           "pen_depth_pct": 0.01, "vol_rel": 1.0,
                           "taker_buy_ratio": 0.5}])
    stats = build_and_persist_forward(p, "X", IV)
    assert "error" not in stats, stats.get("error")
    assert stats["rows"] == len(closes)
    assert stats["held_1"] == 1, stats.get("held_1")
    with KBarStore(p) as st2:
        rows = st2.forward_rows("X", IV)
        c = st2.forward_counts("X", IV)
    assert len(rows) == len(closes)
    assert abs(rows[0]["ret1"] - 0.10) < 1e-12
    assert rows[-1]["ret1"] is None
    assert rows[1]["held"] == 1
    assert rows[1]["held_line"] == "a@15m"
    assert c["ret5"] == len(closes) - 5


def test_build_and_persist_forward_ignores_neutral_bars(tmp_path):
    """`state='neutral'` 的根 event 是空的，**不许**被当成「有事件」。

    这是一个真实发生过的错法：把判定条件写成 `event is not None or state is not None`
    会把 6,067 根 neutral 一起收进来，然后因为它们没有 on_line 而全部报成
    「有事件但拿不到参照价」—— 真值被 6,067 条噪声埋掉，而且不会报错。
    """
    from ai_trader.kbar.build import build_and_persist_forward
    st, p = _store(tmp_path)
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    with st:
        st.upsert_evolution([
            {"symbol": "X", "interval_ms": IV, "ts": T(i), "idx": i,
             "o": c, "h": c + 1, "l": c - 1, "c": c} for i, c in enumerate(closes)])
        st.set_n_touch("X", IV, {T(i): 0 for i in range(len(closes))})
        # 第 1 根：真事件；第 2、3 根：neutral（event 空、state='neutral'）
        st.set_bar_events("X", IV, {
            T(1): {"event": "向上突破", "prev_event": None, "on_line": "a@15m",
                   "since_event_idx": None, "state": None},
            T(2): {"event": None, "prev_event": None, "on_line": None,
                   "since_event_idx": None, "state": "neutral"},
            T(3): {"event": None, "prev_event": None, "on_line": None,
                   "since_event_idx": None, "state": "neutral"},
        })
        st.upsert_events([{"symbol": "X", "interval_ms": IV, "ts": T(1), "idx": 1,
                           "line_key": "a@15m", "line_kind": "ema20",
                           "line_period_ms": IV, "line_price": 100.5,
                           "event": "向上突破", "prev_event": None,
                           "since_event_idx": None, "close_side": "above",
                           "pen_depth_pct": 0.01, "vol_rel": 1.0,
                           "taker_buy_ratio": 0.5}])
    stats = build_and_persist_forward(p, "X", IV)
    assert "error" not in stats, stats.get("error")
    # 只有 1 根真有事件
    assert stats["bars_with_event"] == 1, \
        "把 neutral 的根也算成有事件了（会污染 held 的分母）"
    # 没有一根「拿不到参照价」—— neutral 根本不该进来找参照价
    assert stats["held_ref_missing"] == 0, \
        "neutral 的根被当成「有事件但缺参照价」了"
    assert stats["held_1"] + stats["held_0"] + stats["held_null"] == 1
    with KBarStore(p) as st2:
        rows = {r["ts"]: r for r in st2.forward_rows("X", IV)}
    assert rows[T(1)]["held"] in (0, 1)
    for ts in (T(0), T(2), T(3), T(4), T(5), T(6)):
        assert rows[ts]["held"] is None, "neutral 的根填了 held"


def test_build_and_persist_forward_refuses_ragged_bars(tmp_path):
    """不是等间隔就拒绝 —— ret_k 的前提是 t+k 根就是 t+k*interval。"""
    from ai_trader.kbar.build import build_and_persist_forward
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([
            {"symbol": "X", "interval_ms": IV, "ts": T(0), "idx": 0,
             "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5},
            # 跳过 T(1)，直接 T(2)
            {"symbol": "X", "interval_ms": IV, "ts": T(2), "idx": 2,
             "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.6}])
    stats = build_and_persist_forward(p, "X", IV)
    assert "error" in stats
    assert "等间隔" in stats["error"]


def test_build_and_persist_forward_refuses_event_without_on_line(tmp_path):
    from ai_trader.kbar.build import build_and_persist_forward
    st, p = _store(tmp_path)
    with st:
        st.upsert_evolution([
            {"symbol": "X", "interval_ms": IV, "ts": T(i), "idx": i,
             "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5} for i in range(3)])
        st.set_bar_events("X", IV, {T(1): {"event": "向上突破",
                                           "prev_event": None,
                                           "on_line": None,
                                           "since_event_idx": None,
                                           "state": None}})
    stats = build_and_persist_forward(p, "X", IV)
    assert "error" in stats
    assert "on_line" in stats["error"]


def test_forward_params_snapshot_declares_the_undecided_parts():
    """占位口径必须写在库的自述里，不能只活在代码里。"""
    from ai_trader.kbar.build import forward_params_snapshot
    p = forward_params_snapshot(IV)
    assert p["horizons"] == list(W.FORWARD_HORIZONS)
    assert p["held_bars"] == W.HELD_BARS
    assert "D-12" in (p.get("ret_fee_note") or ""), "没写扣费口径待定"
    assert "D-20" in (p.get("held_ref_price_rule") or ""), "没写参照价口径待定"
    assert "isolation" in p, "没声明事后层的隔离约束"
    assert "held(无事件的根)" in p["undefined_by_design"]
