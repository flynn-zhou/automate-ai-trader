# -*- coding: utf-8 -*-
"""权益表是【节流写入】的：至少间隔 5 秒。

为什么要钉住这件事
------------------
不是因为这个行为会错，而是因为它会被**误读**。

排查"引擎循环是不是变慢了"时，我拿 equity 表的相邻 ts 间隔当循环周期，
量出稳定 6.0 秒（最小也是 6.0，从不出现 3 秒），
几乎要得出"3 秒实时系统实际跑 6 秒、丢掉一半决策"这个结论。

真相是这个 5 秒节流：3 秒粒度上取整后就是 6 秒。
真实循环是正常的——3s 桶 30 分钟 597/600 根严格连续。

所以这条测试同时钉两件事：
  1. 节流存在（5 秒内重复调用不落库）——别把它当 bug 删掉
  2. 节流之外的数据仍然完整——别把节流当成测量上游节奏的工具
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.trade_db import TradeDB


_seq = [0]


def _equity_row(ts=None):
    """每次给一个【不同的 ts】。

    为什么必须不同：equity 表是 ts INTEGER PRIMARY KEY，写入用 INSERT OR REPLACE。
    同一毫秒内的多次调用主键相同，会被合并成一行 —— 那样即使把节流删掉，
    行数也还是 1，测试会因为【主键合并】而通过，看起来像在验节流，
    实际什么都没验。这类"通过的理由是错的"比失败更危险。
    """
    if ts is None:
        _seq[0] += 1
        ts = int(time.time() * 1000) + _seq[0] * 1000
    return {"ts": ts, "balance": 100.0, "unrealized": 0.0, "equity": 100.0,
            "position_qty": 0.0, "leverage": 0.0, "paper": 1}


def _count(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM equity").fetchone()[0]
    finally:
        c.close()


def _wait_for(path, want, timeout=8.0):
    """等单写入队列把数据落盘。固定 sleep 不可靠（实测 0.4s 不够）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _count(path) >= want:
            return
        time.sleep(0.1)


def test_equity_writes_are_throttled():
    """5 秒内的重复写入必须被吞掉。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    try:
        tdb.log_equity(_equity_row())
        # 连续写 5 次，都应被节流吞掉
        for _ in range(5):
            tdb.log_equity(_equity_row())
        _wait_for(path, 1)
        time.sleep(0.6)          # 再多等一会儿，确认没有第二条
        n = _count(path)
        assert n == 1, (
            u"5 秒节流失效：写入了 %d 行（应为 1）。"
            u"如果节流被删掉，权益表会被 3 秒循环写爆；"
            u"但如果它被误认为循环节奏，又会得出错误结论。" % n)
    finally:
        tdb.stop()
        os.unlink(path)


def test_equity_written_again_after_gap():
    """超过间隔后必须继续写入，不能一直吞。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    try:
        tdb.log_equity(_equity_row())
        # 单写入队列：必须等它真的落盘再断言
        _wait_for(path, 1)
        assert _count(path) == 1, u"第一次写入没有落盘"
        # 把上次写入时间人为推早，模拟「已经过了 6 秒」
        tdb._last_equity_s -= 6
        tdb.log_equity(_equity_row())
        _wait_for(path, 2)
        n = _count(path)
        assert n == 2, u"过了节流窗口却不写了：%d 行（应为 2）" % n
    finally:
        tdb.stop()
        os.unlink(path)


def test_throttle_constant_matches_docstring():
    """常量必须和 docstring 说的一致：别一边写 5 秒一边说同秒。"""
    assert TradeDB.EQUITY_MIN_GAP_S == 5, (
        u"docstring 说 5 秒，常量是 %r" % TradeDB.EQUITY_MIN_GAP_S)
