# -*- coding: utf-8 -*-
"""`armed_at_sub_ts` 必须落库（成交守卫的主锚点）。

2026-11-25 查出来：这一列**根本不存在**。`armed_at_sub_ts` 只活在内存里，
`row_of` 不写它、`progress_of` 不读它，于是重启/重建 `Thesis` 之后永远
是 None —— 守卫的时间戳分支成了死代码，只剩 `armed_at_bar` 这个**数组
下标**在扛。

下标会漂：事故现场 `next_index` 被重算到 386，而 `armed_at_bar` 是 399，
ARMED 于是回头扫了挂单之前的 K。时间戳不会漂 —— 所以它必须落库。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trading import pending_thesis_store as PTS   # noqa: E402
from ai_trader.trading import thesis as T                   # noqa: E402


def _thesis():
    return T.Thesis(thesis_id="t-1", kind="x", direction=T.UP,
                    order_price=100.0, stop_price=90.0, target_price=120.0,
                    order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL)


def test_column_exists_after_open(tmp_path):
    """新库建表就该有这一列。"""
    st = PTS.PendingThesisStore(str(tmp_path / "p.db"))
    try:
        cols = {r[1] for r in st._conn.execute(
            "PRAGMA table_info(pending_theses)")}
        assert "armed_at_sub_ts" in cols, (
            "建表语句必须包含 armed_at_sub_ts，实际列：%s" % sorted(cols))
    finally:
        st.close()


def test_old_db_gets_the_column_migrated(tmp_path):
    """老库（没有这一列）打开后必须被补上，且历史行还在。"""
    path = str(tmp_path / "old.db")
    # 手工造一张「补列之前」的表：去掉 armed_at_sub_ts
    raw = sqlite3.connect(path)
    raw.executescript(PTS.SCHEMA.replace("    armed_at_sub_ts INTEGER,\n", ""))
    raw.execute("INSERT INTO pending_theses (thesis_id, state, next_index)"
                " VALUES ('legacy-1', 'ARMED', 7)")
    raw.commit()
    cols_before = {r[1] for r in raw.execute(
        "PRAGMA table_info(pending_theses)")}
    assert "armed_at_sub_ts" not in cols_before, "前置条件：老库没有这一列"
    raw.close()

    st = PTS.PendingThesisStore(path)
    try:
        cols = {r[1] for r in st._conn.execute(
            "PRAGMA table_info(pending_theses)")}
        assert "armed_at_sub_ts" in cols, "打开老库时必须补列"
        rows = st._conn.execute(
            "SELECT thesis_id, state FROM pending_theses").fetchall()
        assert len(rows) == 1 and rows[0]["thesis_id"] == "legacy-1", (
            "补列不能丢历史行（那些行是复盘和重放量具的输入）")
    finally:
        st.close()


def test_migration_is_idempotent(tmp_path):
    """重复打开同一个库不能报错（服务重启会反复调构造函数）。"""
    path = str(tmp_path / "p.db")
    for _ in range(3):
        st = PTS.PendingThesisStore(path)
        st.close()


def test_row_of_and_progress_of_round_trip_the_anchor(tmp_path):
    """写进去的时间戳必须能原样读回来 —— 否则等于没落库。"""
    st = PTS.PendingThesisStore(str(tmp_path / "p.db"))
    try:
        row = PTS.row_of(_thesis(), state=T.ARMED, next_index=5,
                         armed_at_bar=399, armed_at_sub_ts=1795593600000,
                         armed_ref_close=1.0, bar_ts=1795593600000)
        st.upsert_thesis(row)
        got = st._conn.execute(
            "SELECT armed_at_sub_ts FROM pending_theses WHERE thesis_id='t-1'"
        ).fetchone()
        assert got["armed_at_sub_ts"] == 1795593600000, (
            "时间戳没落库：%r" % (got["armed_at_sub_ts"],))

        loaded = dict(st._conn.execute(
            "SELECT * FROM pending_theses WHERE thesis_id='t-1'").fetchone())
        prog = PTS.progress_of(loaded)
        assert prog["armed_at_sub_ts"] == 1795593600000, (
            "progress_of 必须把它交回给状态机，否则守卫拿不到锚点")
    finally:
        st.close()


def test_legacy_row_without_anchor_reads_as_none(tmp_path):
    """老行（补列之前写的）读出来是 None，退回 `armed_at_bar` 判据。

    ★ 这是**向后兼容**，不是缺陷：None 时 `thesis.step` 走 elif 分支比下标，
      也就是补列之前的行为，不会把老预期判错。
    """
    st = PTS.PendingThesisStore(str(tmp_path / "p.db"))
    try:
        row = PTS.row_of(_thesis(), state=T.ARMED, next_index=5,
                         armed_at_bar=399, armed_ref_close=1.0,
                         bar_ts=1795593600000)
        assert row["armed_at_sub_ts"] is None
        st.upsert_thesis(row)
        loaded = dict(st._conn.execute(
            "SELECT * FROM pending_theses WHERE thesis_id='t-1'").fetchone())
        prog = PTS.progress_of(loaded)
        assert prog["armed_at_sub_ts"] is None
        assert prog["armed_at_bar"] == 399, "锚点缺失时必须还能用下标判据"
    finally:
        st.close()
