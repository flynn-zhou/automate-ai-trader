# -*- coding: utf-8 -*-
"""bars 表多品种化：结构升级 + 品种隔离。

两条必须钉死的性质
------------------
一、升级不得丢数据。生产库里已有 6 万多条 K 桶，重建表时少搬一行都是事故，
   而且不会报错——只是历史 K 线凭空少了一截。
二、查询必须按品种隔离。两个品种在同一个 (interval, open_time) 上各有自己的一根，
   这是**正常状态**（不同品种时间桶天然对齐）。查询漏掉 symbol 过滤时，
   返回的是两个品种交错的一堆 K 线，画面看着"像行情"，实际全是错的。
"""
from __future__ import annotations

import sqlite3

import pytest


class _Bar:
    """最小 K 线对象，字段与 feed 的 Bar 对齐。"""
    def __init__(self, interval_ms, open_time, o, h, l, c, v=1.0):
        self.interval_ms = interval_ms
        self.open_time = open_time
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v
        self.quote_volume = v * 10
        self.trades = 3
        self.buy_base = v / 2
        self.sell_base = v / 2


def _wait_rows(st, expect=1, timeout=5.0, **kw):
    """等单写线程落盘：轮询到够数为止，不靠固定 sleep。

    固定 sleep 的测试是"跑得快时通过、机器忙时红"，那种红会被人当成噪声忽略，
    久而久之就没人信测试了。
    """
    import time as _t
    end = _t.time() + timeout
    rows = []
    while _t.time() < end:
        rows = st.query_bars(60000, limit=10, **kw)
        if len(rows) >= expect:
            return rows
        _t.sleep(0.05)
    return rows


def _old_schema_db(path):
    """造一个升级前的库：bars 没有 symbol 列，主键是 (interval_ms, open_time)。"""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE bars (
            interval_ms INTEGER NOT NULL,
            open_time   INTEGER NOT NULL,
            o REAL, h REAL, l REAL, c REAL,
            v REAL, q REAL, n INTEGER, b REAL, s REAL,
            updated_at  INTEGER NOT NULL,
            PRIMARY KEY (interval_ms, open_time)
        );
        CREATE INDEX idx_bars_time ON bars(open_time);
        CREATE TABLE bar_gaps (
            interval_ms INTEGER NOT NULL,
            open_time   INTEGER NOT NULL,
            detected_at INTEGER NOT NULL,
            PRIMARY KEY (interval_ms, open_time)
        );
    """)
    rows = [(60000, 1000 + i * 60000, 1.0, 2.0, 0.5, 1.5, 9.0, 90.0, 7, 4.0, 5.0, 123)
            for i in range(50)]
    con.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO bar_gaps VALUES(?,?,?)",
                    [(60000, 2000 + i * 60000, 111) for i in range(5)])
    con.commit()
    con.close()


def test_migration_preserves_every_row(tmp_path):
    """升级前后行数必须一致——少搬一行不会报错，只会让历史凭空少一截。"""
    from ai_trader.store import DEFAULT_SYMBOL, Store

    path = str(tmp_path / "market.db")
    _old_schema_db(path)
    st = Store(path)
    st.start()
    try:
        con = sqlite3.connect(path)
        n = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM bars")]
        gaps = con.execute("SELECT COUNT(*) FROM bar_gaps").fetchone()[0]
        gap_syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM bar_gaps")]
        con.close()
    finally:
        st.stop()
    assert n == 50, "迁移丢了行：期望 50，实际 %d" % n
    assert syms == [DEFAULT_SYMBOL], "老数据应归到默认品种，实际 %r" % syms
    assert gaps == 5
    assert gap_syms == [DEFAULT_SYMBOL]


def test_migration_is_idempotent(tmp_path):
    """重复启动不得重复迁移，也不得报错。"""
    from ai_trader.store import Store

    path = str(tmp_path / "market.db")
    _old_schema_db(path)
    for _ in range(3):
        st = Store(path)
        st.start()
        st.stop()
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 50
    con.close()


def test_new_db_has_symbol_column(tmp_path):
    from ai_trader.store import Store

    st = Store(str(tmp_path / "fresh.db"))
    st.start()
    st.stop()
    con = sqlite3.connect(str(tmp_path / "fresh.db"))
    cols = [r[1] for r in con.execute("PRAGMA table_info(bars)")]
    con.close()
    assert "symbol" in cols


def test_two_symbols_coexist_and_are_isolated(tmp_path):
    """同一 (interval, open_time) 上两个品种各存一根，查询不得串味。"""
    from ai_trader.store import Store

    path = str(tmp_path / "m.db")
    st = Store(path, symbol="BTCUSDT")
    st.start()
    try:
        ts = 1700000000000
        st.put_bar(_Bar(60000, ts, 100, 110, 90, 105), symbol="BTCUSDT")
        st.put_bar(_Bar(60000, ts, 10, 11, 9, 10.5), symbol="ETHUSDT")
        # 注意：不能用 st._q.join()。queue.Queue.join() 要等 task_done()，
        # 而单写线程不调 task_done，会永久阻塞（写测试时踩过一次，600 秒挂死）。
        btc = _wait_rows(st)
        eth = _wait_rows(st, symbol="ETHUSDT")
    finally:
        st.stop()
    assert len(btc) == 1 and len(eth) == 1, "两个品种应各有一根，实际 %d/%d" % (len(btc), len(eth))
    assert btc[0]["c"] == 105, "BTC 查到了别的品种: %r" % (btc[0],)
    assert eth[0]["c"] == 10.5, "ETH 查到了别的品种: %r" % (eth[0],)


def test_default_symbol_keeps_old_callers_working(tmp_path):
    """不传 symbol 的老调用方必须仍然只看到自己的品种。"""
    from ai_trader.store import Store

    path = str(tmp_path / "m.db")
    st = Store(path)                       # 默认 BTCUSDT
    st.start()
    try:
        ts = 1700000000000
        st.put_bar(_Bar(60000, ts, 100, 110, 90, 105))
        st.put_bar(_Bar(60000, ts, 10, 11, 9, 10.5), symbol="ETHUSDT")
        rows = _wait_rows(st)                      # 不传 symbol
    finally:
        st.stop()
    assert len(rows) == 1, "默认查询把别的品种也带出来了: %d 根" % len(rows)
    assert rows[0]["c"] == 105
