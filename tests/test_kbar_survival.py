# -*- coding: utf-8 -*-
"""存活层（表 6b）的测试：一句话「活了多久」。

这一层存在的理由，是两个已经量出来的事实：
    1. 固定 N 是错的表达。实测「N=1 就定型」只占 45.5%，p99 = 11 根。
       只报 N=1/3 的时候，一大半判断还只是临时答案。
    2. 两类预言的短期裁决**不对称**：
         守住型（不该破）在短 N 下偏松 —— 还没来得及破就算成立
         会破型（该破）  在短 N 下偏严 —— 还没来得及破就算推翻
       放在同一个 N 上比命中率本来就不成立。

三态必须分开，这是本文件最要紧的地方：
    已破 / 活到最后 / **未成熟**
「未成熟」与「活到最后」都表现为 break_n IS NULL。混起来就是拿表尾冒充结论。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B          # noqa: E402
from ai_trader.kbar import verify as V         # noqa: E402
from ai_trader.kbar import shape as S          # noqa: E402

DB = os.path.join(ROOT, "data", "kbar.db")
needs_db = pytest.mark.skipif(not os.path.exists(DB), reason="没有 data/kbar.db")

DOWN = S.SIDE_DOWN
UP = S.SIDE_UP


def mk(o, h, l, c, ts=0):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c,
            "vol_rel": 1.0, "taker_buy_ratio": 0.5}


def held_down(ref=100.0):
    """守住型：后面不该跌破 ref。"""
    return V.Prediction(V.PRED_PIN_LOW_HELD, "不该跌破 %.1f" % ref,
                        ref, DOWN, True)


def broke_down(ref=100.0):
    """会破型：后面该跌破 ref。"""
    return V.Prediction(V.PRED_PIN_LOW_BROKE, "该跌破 %.1f" % ref,
                        ref, DOWN, False)


BASE = mk(101, 102, 100.5, 101)


# ---------------------------------------------------------------- 三态
def test_breaks_at_the_bar_it_first_crosses():
    bars = [BASE, mk(101, 101, 99, 99.5), mk(98, 98, 90, 91)]
    sv = V.first_break(bars, 0, held_down())
    assert sv.status == V.SURV_BROKE
    assert sv.break_n == 1          # 第 1 根就破了，不是第 2 根
    assert sv.actual_at_break == 99


def test_exact_touch_counts_as_broken():
    """容差 0 时「正好打到」就算破。

    这条钉的是比较符号：`<=` 会破，`<` 不会。写成 `<` 等于对被验的那个
    判断网开一面 —— 打到线上都不算破，佐证层就没意义了。
    与本项目 reaction 层 TOUCH_TOL_PCT=0「正好等于线价算触碰」同一个口径。
    """
    bars = [BASE, mk(101, 101, 100.0, 100.2)]   # 最低正好 100.0 == ref
    sv = V.first_break(bars, 0, held_down(100.0))
    assert sv.status == V.SURV_BROKE, "打平不算破 —— 比较符号写成了 <"
    assert sv.break_n == 1


def test_lasted_means_looked_at_the_full_window():
    bars = [BASE] + [mk(101, 101, 100.8, 100.9) for _ in range(V.SURVIVAL_MAX_N)]
    sv = V.first_break(bars, 0, held_down())
    assert sv.status == V.SURV_LASTED
    assert sv.break_n is None
    assert sv.n_seen == V.SURVIVAL_MAX_N   # 看满了


def test_pending_means_we_ran_out_of_bars():
    """**最容易写错的一条。**

    只给 3 根后续，它是 `未成熟`，不是 `活到最后`。把这两件事混起来，
    表尾那几根「还没验」就会被算进「验完了没破」，命中率凭空变好。
    """
    bars = [BASE] + [mk(101, 101, 100.8, 100.9) for _ in range(3)]
    sv = V.first_break(bars, 0, held_down())
    assert sv.status == V.SURV_PENDING
    assert sv.break_n is None
    assert sv.n_seen == 3
    assert sv.n_seen < sv.max_n


def test_pending_and_lasted_both_have_no_break_n():
    """这两态的 break_n 都是 None —— 所以才必须有 status 列。"""
    short = [BASE] + [mk(101, 101, 100.8, 100.9) for _ in range(3)]
    full = [BASE] + [mk(101, 101, 100.8, 100.9) for _ in range(V.SURVIVAL_MAX_N)]
    a = V.first_break(short, 0, held_down())
    b = V.first_break(full, 0, held_down())
    assert a.break_n is None and b.break_n is None
    assert a.status != b.status, "两态被合并了"


def test_broken_is_final_even_if_the_window_is_not_full():
    """已经破了就是定了，不用等满窗口 —— 所以表尾 12 根里
    提前破的那些应当算「已破」，不该算「未成熟」。"""
    bars = [BASE, mk(101, 101, 99, 99.5)]
    sv = V.first_break(bars, 0, held_down())
    assert sv.status == V.SURV_BROKE     # 只有 1 根后续，但已经破了
    assert sv.n_seen == 1 < sv.max_n


# ---------------------------------------------------------------- 会破型
def test_broke_type_is_confirmed_when_it_breaks():
    bars = [BASE] + [mk(101, 101, 100.8, 100.9) for _ in range(4)] \
        + [mk(100, 100, 98, 98.5)]
    sv = V.first_break(bars, 0, broke_down())
    assert sv.status == V.SURV_BROKE
    assert sv.break_n == 5


def test_broke_type_that_never_breaks_lasts_out_the_window():
    bars = [BASE] + [mk(101, 101, 100.8, 100.9) for _ in range(V.SURVIVAL_MAX_N)]
    sv = V.first_break(bars, 0, broke_down())
    assert sv.status == V.SURV_LASTED


# ---------------------------------------------------------------- 全幅型
def test_quiet_breaks_when_excursion_reaches_one_range():
    """全幅型（「无」）的尺是 1 倍本根全幅，不是价位。"""
    g = S.geometry(BASE["o"], BASE["h"], BASE["l"], BASE["c"])
    amp = max(g.rng, S.WICK_PTS_MIN)
    pred = V.Prediction(V.PRED_QUIET, "后无大波动", None, None, True, amp=amp)
    # 没到 1 倍全幅
    small = [BASE] + [mk(101, 101, 100.9, 101) for _ in range(V.SURVIVAL_MAX_N)]
    assert V.first_break(small, 0, pred).status == V.SURV_LASTED
    # 走到 3 倍全幅 → 第 1 根就破
    big_l = BASE["c"] - 3 * amp
    big = [BASE, mk(101, 101, big_l, 101)]
    sv = V.first_break(big, 0, pred)
    assert sv.status == V.SURV_BROKE and sv.break_n == 1


def test_quiet_result_has_no_ref_price():
    """全幅型的 ref_price 必须是 None —— 它没有价位，别编一个出来。"""
    g = S.geometry(BASE["o"], BASE["h"], BASE["l"], BASE["c"])
    pred = V.Prediction(V.PRED_QUIET, "后无大波动", None, None, True,
                        amp=max(g.rng, S.WICK_PTS_MIN))
    bars = [BASE] + [mk(101, 101, 100.9, 101)]
    assert V.first_break(bars, 0, pred).ref_price is None


# ---------------------------------------------------------------- 无前视 / 增量安全
def test_break_n_does_not_change_when_more_bars_arrive():
    """**增量更新的逻辑基础。**

    一旦破了，后面再来多少根都不该改变 break_n。如果这条不成立，
    「只重算尾部」就是错的，历史会被静默改写。
    """
    head = [BASE, mk(101, 101, 100.8, 100.9), mk(101, 101, 99, 99.5)]
    short = head + [mk(101, 101, 100.8, 100.9) for _ in range(3)]
    long_ = head + [mk(101, 101, 80, 80) for _ in range(9)]
    a = V.first_break(short, 0, held_down())
    b = V.first_break(long_, 0, held_down())
    assert a.status == V.SURV_BROKE and b.status == V.SURV_BROKE
    assert a.break_n == b.break_n == 2


def test_max_n_cuts_the_window():
    bars = [BASE] + [mk(101, 101, 100.8, 100.9) for _ in range(20)]
    sv = V.first_break(bars, 0, held_down(), max_n=5)
    assert sv.max_n == 5 and sv.n_seen == 5
    assert sv.status == V.SURV_LASTED


# ---------------------------------------------------------------- 口径指纹
def test_survival_hash_is_independent_of_verify_hash():
    """存活层的 max_n 若并进 verify_params()，表 6 的指纹会变、全库要多算一遍。

    两层各自一个指纹，互不牵连 —— 这条钉住它。
    """
    assert V.survival_hash() != V.verify_hash()
    assert V.survival_hash() == V.survival_hash()
    assert "max_n" in V.survival_params()
    assert "max_n" not in V.verify_params()


def test_survival_max_n_is_big_enough_to_cover_measured_tail():
    """实测 p99 = 11 根。截断取小了，尾部会有大批判断永远停在「未成熟」。"""
    assert V.SURVIVAL_MAX_N >= 12


# ---------------------------------------------------------------- 落库 + 增量一致
@pytest.fixture()
def small_db():
    """把生产库拷一份、只留前 400 根，得到一个**真结构的小库**。

    不手搓 fixture 建表：那样测的是我编的 schema，不是真的那一张。
    """
    if not os.path.exists(DB):
        pytest.skip("没有 data/kbar.db")
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "kbar_small.db")
    shutil.copy2(DB, dst)
    con = sqlite3.connect(dst)
    try:
        ts_all = [r[0] for r in con.execute(
            "SELECT ts FROM kbar_evolution WHERE symbol='BTCUSDT' "
            "AND interval_ms=900000 AND o IS NOT NULL ORDER BY ts LIMIT 400")]
        keep = ts_all[-1]
        for tbl in ("kbar_evolution", "kbar_shape", "kbar_verify",
                    "kbar_verify_survival", "kbar_line", "kbar_reaction",
                    "kbar_event", "kbar_forward"):
            con.execute("DELETE FROM %s WHERE ts > ?" % tbl, (keep,))
        con.commit()
    finally:
        con.close()
    yield dst
    shutil.rmtree(tmp, ignore_errors=True)


def _snap(db):
    from ai_trader.kbar.store import KBarStore
    with KBarStore(db) as st:
        v = sorted((r["ts"], r["horizon"], r["verdict"], r["actual_price"],
                    r["diff_pts"]) for r in st.verify_rows("BTCUSDT", 900000))
        s = sorted((r["ts"], r["status"], r["break_n"], r["n_seen"],
                    r["actual_at_break"])
                   for r in st.survival_rows("BTCUSDT", 900000))
    return v, s


def test_incremental_equals_full(small_db):
    """**「实时跟随」的验收条件。** 只算尾部，结果必须和全量逐行相同。"""
    B.build_and_persist_verify(small_db, "BTCUSDT", 900000)
    B.build_and_persist_survival(small_db, "BTCUSDT", 900000)
    v0, s0 = _snap(small_db)
    out = B.update_verify_tail(small_db, "BTCUSDT", 900000)
    assert not out["verify"].get("error"), out["verify"].get("error")
    assert not out["survival"].get("error"), out["survival"].get("error")
    v1, s1 = _snap(small_db)
    assert v0 == v1, "表 6 增量与全量不一致"
    assert s0 == s1, "表 6b 增量与全量不一致"


def test_incremental_only_recomputes_the_tail(small_db):
    """它必须**正好**只算尾部，不是多算也不算少算。

    少算一根的后果很隐蔽：那一根本来只有 11 根后续（未成熟），
    少算一次看起来结果一样；但新 K 线进来后它就该定型了，
    而边界一直把它排除在外 —— 它会**永远停在未成熟**。
    """
    B.build_and_persist_survival(small_db, "BTCUSDT", 900000)
    out = B.update_verify_tail(small_db, "BTCUSDT", 900000)
    tail = out["tail_bars"]
    # 必须重算 tail+1 根：最后 tail 根还没定型，再加上「刚刚定型的那一根」
    # （i = n-1-tail）。只算 tail 根就会把刚定型的那根漏掉。
    assert out["recompute_bars"] == tail + 1, (
        "应正好重算 %d 根，实际 %d 根" % (tail + 1, out["recompute_bars"]))
    assert out["recompute_bars"] < out["bars"], "明明是几百根却重算了全部"


def test_new_bar_makes_the_oldest_pending_bar_settle(small_db):
    """**「实时跟随」的验收条件。**

    表尾那几根之所以是「未成熟」，是因为后面的根不够 max_n。
    来一根新 K 线，最早的那个未成熟根就凑满了窗口，必须**当场定型**。
    如果增量边界把它排除在外，它就永远定不了型 —— 表尾会一直挂着一串
    「未成熟」，而这个层的结论永远差最后几根。
    """
    from ai_trader.kbar.store import KBarStore
    B.build_and_persist_survival(small_db, "BTCUSDT", 900000)

    con = sqlite3.connect(small_db)
    try:
        # 找出现在还「未成熟」的那些根，以及最早那根**还差几根**才凑满窗口。
        # 不能假设「来一根就定型」：它可能只看到 10 根，还差 2 根。
        pend = list(con.execute(
            "SELECT ts, n_seen, max_n FROM kbar_verify_survival "
            "WHERE symbol='BTCUSDT' AND interval_ms=900000 AND status=? "
            "ORDER BY ts", (V.SURV_PENDING,)))
        assert pend, "表尾没有未成熟根，测不到这件事"
        oldest, seen, mx = pend[0]
        need = int(mx) - int(seen)
        assert need >= 1
        # 补 need 根新 K 线（照抄最后一根的形态，只换 ts）
        cols_evo = ("o", "h", "l", "c", "vol_rel", "taker_buy_ratio")
        cols_shp = [d[1] for d in con.execute("PRAGMA table_info(kbar_shape)")]
        last_evo = con.execute(
            "SELECT o,h,l,c,vol_rel,taker_buy_ratio FROM kbar_evolution "
            "WHERE symbol='BTCUSDT' AND interval_ms=900000 "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        last_shp = dict(zip(cols_shp, con.execute(
            "SELECT * FROM kbar_shape WHERE symbol='BTCUSDT' "
            "AND interval_ms=900000 ORDER BY ts DESC LIMIT 1").fetchone()))
        base_ts = con.execute(
            "SELECT MAX(ts) FROM kbar_evolution WHERE symbol='BTCUSDT' "
            "AND interval_ms=900000").fetchone()[0]
        for k in range(1, need + 1):
            ts = base_ts + k * 900000
            con.execute(
                "INSERT INTO kbar_evolution (symbol,interval_ms,ts,idx,%s) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)" % ",".join(cols_evo),
                ("BTCUSDT", 900000, ts, 90000 + k) + tuple(last_evo))
            r = dict(last_shp)
            r["ts"] = ts
            con.execute(
                "INSERT INTO kbar_shape (%s) VALUES (%s)"
                % (",".join(cols_shp), ",".join("?" * len(cols_shp))),
                [r[c] for c in cols_shp])
        con.commit()
    finally:
        con.close()

    out = B.update_verify_tail(small_db, "BTCUSDT", 900000)
    assert not out["survival"].get("error"), out["survival"].get("error")
    with KBarStore(small_db) as st:
        after = {r["ts"]: r["status"] for r in st.survival_rows("BTCUSDT",
                                                                900000)}
    assert after[oldest] != V.SURV_PENDING, (
        "补足 %d 根新 K 线后，最早的那个未成熟根（%s）还是未成熟 —— "
        "增量边界把它漏掉了" % (need, oldest))


def test_pending_is_not_counted_as_settled(small_db):
    from ai_trader.kbar.store import KBarStore
    B.build_and_persist_survival(small_db, "BTCUSDT", 900000)
    with KBarStore(small_db) as st:
        c = st.survival_counts("BTCUSDT", 900000)
    assert c["settled"] + c["pending"] == c["rows"]
    assert c["settled"] == (c["by_status"].get("已破", 0)
                            + c["by_status"].get("活到最后", 0))
    assert c["pending"] == c["by_status"].get("未成熟", 0)


def test_survival_rows_have_exactly_one_hash(small_db):
    from ai_trader.kbar.store import KBarStore
    B.build_and_persist_survival(small_db, "BTCUSDT", 900000)
    with KBarStore(small_db) as st:
        c = st.survival_counts("BTCUSDT", 900000)
    assert c["survival_hashes"] == [V.survival_hash()]


def test_all_three_states_actually_occur(small_db):
    """三态在这个数据集里都出现，测试才测到了东西。"""
    from ai_trader.kbar.store import KBarStore
    B.build_and_persist_survival(small_db, "BTCUSDT", 900000)
    with KBarStore(small_db) as st:
        c = st.survival_counts("BTCUSDT", 900000)
    assert c["by_status"].get("已破", 0) > 0
    assert c["by_status"].get("活到最后", 0) > 0
    assert c["by_status"].get("未成熟", 0) > 0, "表尾没留未成熟，测不到边界"


# ---------------------------------------------------------------- 校验器本身要有效
def test_row_count_guard_fires_when_a_bar_is_missing(small_db):
    """**校验器不能是同义反复。**

    第一版拿 `len(rows)` 当基准去比 `count(表6)`，而 rows 就是刚写进去的东西
    —— 拿自己比自己永远相等，少写一根根本拦不住（变异测试抓出来的）。

    注意下面删的是**表 1** 的行、不是表 5 的：删表 5 会让「应有根数」跟着变少，
    两边仍然自洽（那本来就该通过）。真正会漏写的是「表 5 有结论、表 1 没这根」
    —— 遍历走的是表 1，于是这一根被跳过，而应有根数还按表 5 数着。
    """
    B.build_and_persist_verify(small_db, "BTCUSDT", 900000)
    con = sqlite3.connect(small_db)
    try:
        victim = con.execute(
            "SELECT ts FROM kbar_shape WHERE symbol='BTCUSDT' "
            "AND interval_ms=900000 ORDER BY ts DESC LIMIT 1 OFFSET 5"
        ).fetchone()[0]
        con.execute("DELETE FROM kbar_evolution WHERE symbol='BTCUSDT' "
                    "AND interval_ms=900000 AND ts=?", (victim,))
        con.commit()
    finally:
        con.close()
    out = B.build_and_persist_verify(small_db, "BTCUSDT", 900000)
    assert out.get("error"), "表 1 少一根、表 5 还有结论，却没报错 —— 校验拦不住"
    assert "写完对不上" in out["error"]


def test_survival_row_count_guard_fires_when_a_bar_is_missing(small_db):
    import sqlite3 as sq
    B.build_and_persist_survival(small_db, "BTCUSDT", 900000)
    con = sq.connect(small_db)
    try:
        victim = con.execute(
            "SELECT ts FROM kbar_shape WHERE symbol='BTCUSDT' "
            "AND interval_ms=900000 ORDER BY ts DESC LIMIT 1 OFFSET 5"
        ).fetchone()[0]
        con.execute("DELETE FROM kbar_evolution WHERE symbol='BTCUSDT' "
                    "AND interval_ms=900000 AND ts=?", (victim,))
        con.commit()
    finally:
        con.close()
    out = B.build_and_persist_survival(small_db, "BTCUSDT", 900000)
    assert out.get("error"), "表 1 少一根、表 5 还有结论，却没报错 —— 校验拦不住"
    assert "写完对不上" in out["error"]
