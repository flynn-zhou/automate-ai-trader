# -*- coding: utf-8 -*-
"""滚动清理必须真的会删，而且只删该删的。

为什么补这个（2026-11-02 实测）：
    线上日志里 滚动清理 0 行 出现 9 次，每一次都是 0。
    0 本身是对的（保留 168 小时，而库里只有约 1.5 天的数据），
    但它意味着【删除分支从来没有真正执行过】——
    SQL 对不对、会不会顺手删掉不该删的，线上完全没有证据。

    这个仓库以前踩过同类的坑：vol/volume 写错导致每 3 秒抛异常被吞掉，
    系统看着在跑、看板有数据，实际上从未评估过。"一直返回 0"和"一直失败"在日志里长得一样。

另外两个已修的隐患（见 feed._prune_loop 注释）：
    - sleep 写在第一次清理之前 -> 频繁重启时清理几乎不执行
    - 失败只记 debug -> INFO 级别下等于把故障吞掉
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.store import Store

IV3S, IV15S, IV1M, IV3M, IV15M, IV4H = 3000, 15000, 60000, 180000, 900000, 14400000


def _mk(tmp_path):
    p = str(tmp_path / 'm.db')
    s = Store(p)
    s.start()
    # 直接建表并按需插入：这里只测 prune 的 SQL 语义，不走写入队列，
    # 免得把"队列有没有把数据写进去"和"prune 删得对不对"混在一起。
    # 表由 Store.start() 自己建（SCHEMA 常量）。第一版我在测试里又写了一遍
    # CREATE TABLE IF NOT EXISTS，列名猜成 open/high/low/close/volume——
    # 真名是 o/h/l/c/v/q/n/b/s/updated_at，插入直接报 no column named open。
    # 教训：不要在测试里复述被测代码的 schema，用它建好的。
    conn = sqlite3.connect(p)
    return s, p, conn


def _fill(conn, iv, times):
    for t in times:
        conn.execute("INSERT OR REPLACE INTO bars (interval_ms, open_time, o, h, l, c,"
                     " v, q, n, b, s, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (iv, t, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1, 0.5, 0.5, t))
    conn.commit()


def test_prune_deletes_old_fast_bars(tmp_path):
    """3s/15s/1m 超过保留期必须删掉。"""
    s, p, conn = _mk(tmp_path)
    now = int(time.time() * 1000)
    old = now - 300 * 3600 * 1000          # 300 小时前（保留 168 小时）
    fresh = now - 3600 * 1000
    for iv in (IV3S, IV15S, IV1M):
        _fill(conn, iv, [old, fresh])
    n = s.prune(168)
    left = {iv: conn.execute("SELECT COUNT(*) FROM bars WHERE interval_ms=?", (iv,)).fetchone()[0]
            for iv in (IV3S, IV15S, IV1M)}
    assert left == {IV3S: 1, IV15S: 1, IV1M: 1}, (
        '超期快周期 bar 没有被删干净：%r' % (left,))
    assert n == 3, '返回的删除行数不对：%d（应为 3）' % n
    conn.close()
    s.stop()


def test_prune_keeps_slow_bars_forever(tmp_path):
    """3m/15m/4h 是图表历史，再老也不删——删了看板就缺历史。"""
    s, p, conn = _mk(tmp_path)
    now = int(time.time() * 1000)
    ancient = now - 1000 * 24 * 3600 * 1000     # 1000 天前
    for iv in (IV3M, IV15M, IV4H):
        _fill(conn, iv, [ancient])
    s.prune(168)
    left = {iv: conn.execute("SELECT COUNT(*) FROM bars WHERE interval_ms=?", (iv,)).fetchone()[0]
            for iv in (IV3M, IV15M, IV4H)}
    assert left == {IV3M: 1, IV15M: 1, IV4H: 1}, (
        '慢周期 bar 被误删了，看板会缺历史：%r' % (left,))
    conn.close()
    s.stop()


def test_prune_deletes_old_feed_events(tmp_path):
    """事件表同样会无限增长，必须一起清。"""
    s, p, conn = _mk(tmp_path)
    now = int(time.time() * 1000)
    conn.execute("INSERT INTO feed_events (ts, level, source, event, detail)"
                 ' VALUES (?,?,?,?,?)', (now - 400 * 3600 * 1000, 'INFO', 'x', 'old', ''))
    conn.execute("INSERT INTO feed_events (ts, level, source, event, detail)"
                 ' VALUES (?,?,?,?,?)', (now - 3600 * 1000, 'INFO', 'x', 'new', ''))
    conn.commit()
    s.prune(168)
    n = conn.execute("SELECT COUNT(*) FROM feed_events").fetchone()[0]
    assert n == 1, '旧事件没被清掉（剩 %d 行）' % n
    conn.close()
    s.stop()


def test_prune_is_a_noop_when_nothing_is_old(tmp_path):
    """对照：数据都还在保留期内时，必须一行都不删。

    这条和上面几条互为反例——只测"会删"的话，一个删光的实现也能全过。
    """
    s, p, conn = _mk(tmp_path)
    now = int(time.time() * 1000)
    for iv in (IV3S, IV15S, IV1M):
        _fill(conn, iv, [now - 3600 * 1000])
    n = s.prune(168)
    total = conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    assert n == 0 and total == 3, (
        '还有 3 行在保留期内，却删了 %d 行（剩 %d）' % (n, total))
    conn.close()
    s.stop()


class _Cfg:
    class store:
        tick_retention_hours = 168


class _Store:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def prune(self, keep_hours):
        self.calls.append(keep_hours)
        if self.fail:
            raise IOError('database is locked')
        return 0


def _make_feed(store):
    from ai_trader.feed import Feed
    f = object.__new__(Feed)
    f.store = store
    f.cfg = _Cfg()
    return f


def _run_loop_briefly(feed, ticks=3):
    """把 asyncio.sleep 换掉，让循环瞬间跑几轮，不真等 120 秒。"""
    sleeps = []

    async def fake_sleep(n):
        sleeps.append(n)
        if len(sleeps) > ticks:
            raise asyncio.CancelledError()

    orig = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        try:
            asyncio.get_event_loop().run_until_complete(feed._prune_loop())
        except asyncio.CancelledError:
            pass
    finally:
        asyncio.sleep = orig
    return sleeps


def test_prune_loop_runs_soon_after_start_not_after_an_hour():
    """启动后必须很快清一次。

    以前 sleep(3600) 在第一次清理【之前】，于是每次重启都要重新等一小时；
    线上 29 小时日志里只有 9 次清理就是这个原因。频繁重启时清理几乎不执行。
    """
    st = _Store()
    sleeps = _run_loop_briefly(_make_feed(st), ticks=2)
    assert st.calls, '整个循环一次 prune 都没调用'
    assert sleeps and sleeps[0] <= 300, (
        '第一次清理前等了 %r 秒；必须很快（<=300s），否则重启一次就推迟一小时'
        % (sleeps[0],))


def test_prune_loop_failure_is_warning_not_debug(caplog):
    """清理失败必须记 warning。

    线上 AI_LOG_LEVEL=INFO，debug 会被直接丢掉——
    于是"清理一直失败"在日志里完全不存在，直到磁盘写满。
    """
    import logging
    st = _Store(fail=True)
    with caplog.at_level(logging.INFO):
        _run_loop_briefly(_make_feed(st), ticks=1)
    levels = [r.levelno for r in caplog.records if '清理' in r.getMessage()]
    assert levels, '清理失败没有任何日志'
    assert min(levels) <= logging.WARNING, (
        '清理失败记的是 %s 级别，INFO 配置下看不见'
        % (logging.getLevelName(min(levels)),))
