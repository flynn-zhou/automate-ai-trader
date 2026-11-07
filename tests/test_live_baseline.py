# -*- coding: utf-8 -*-
"""实盘峰值基线的回归防线。

背景是一条真实数据：2026-11-03 跑过约 14 分钟 live，账户 125.008778U，
一笔没做，钱之后被提走。切回 live 时峰值按 125.0088 算，而权益只有 0.007U，
回撤 -99.99% -> 立刻 60% 硬停。注资回 100U 也白白带着 -20%。

三条不变量：
  1. baseline_ts 只能把峰值起点【往后】推，不能往前拉；
  2. 旧数据一行都不能删（可追溯）；
  3. baseline_ts=0 等价于旧行为（全历史），保证没设置时行为不变。
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.trade_db import peak_equity_at  # noqa: E402


def _read(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _mkdb(rows):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE equity (ts INTEGER, balance REAL, unrealized REAL,"
              " equity REAL, position_qty REAL, leverage REAL, paper INTEGER)")
    c.executemany("INSERT INTO equity VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return path


OLD = 1700000000000        # 旧资金时代
NEWT = 1700100000000       # 新基线


def test_old_peak_ignored_after_baseline():
    """基线之前的峰值不能计入。这正是 125.0088 那条数据的处境。"""
    p = _mkdb([
        (OLD, 0, 0, 125.008778, 0, 0, 0),      # 旧资金：被提走的那笔
        (OLD + 1000, 0, 0, 125.008778, 0, 0, 0),
        (NEWT + 1000, 0, 0, 100.0, 0, 0, 0),   # 注资后
    ])
    try:
        assert abs(peak_equity_at(p, None, 0, 0) - 125.008778) < 1e-6
        assert abs(peak_equity_at(p, None, 0, NEWT) - 100.0) < 1e-6, \
            "基线之后的峰值没有排除旧数据"
    finally:
        os.unlink(p)


def test_baseline_never_moves_start_backwards():
    """基线只能往后推。往前拉等于把见过的回撤藏起来。"""
    p = _mkdb([
        (OLD, 0, 0, 900.0, 0, 0, 0),
        (NEWT + 1000, 0, 0, 100.0, 0, 0, 0),
    ])
    try:
        # 传一个比 OLD 还早的 since_ts，不应该把起点拉到 OLD 之前
        got = peak_equity_at(p, None, 0, OLD - 999999999)
        assert abs(got - 900.0) < 1e-6, "往前拉之后居然算出了不同的峰值"
    finally:
        os.unlink(p)


def test_baseline_zero_keeps_old_behaviour():
    """baseline_ts=0 必须等价于旧行为（全历史），保证没设置时不变。"""
    p = _mkdb([(OLD, 0, 0, 125.0, 0, 0, 0), (NEWT, 0, 0, 100.0, 0, 0, 0)])
    try:
        assert abs(peak_equity_at(p, None, 0, 0) - 125.0) < 1e-6
    finally:
        os.unlink(p)


def test_baseline_column_is_migrated():
    """guard_state 必须有 baseline_ts 列，且走的是 ALTER 迁移而不是只改 CREATE。

    线上那个库是老表，CREATE TABLE IF NOT EXISTS 对它完全无效 ——
    这个坑项目里已经踩过一次（第 127 轮），列全都不出现，读到的都是默认值。
    """
    src = _read("ai_trader", "trade_db.py")
    assert '("guard_state", "baseline_ts"' in src, "迁移里没有 baseline_ts"
    assert "ALTER TABLE guard_state ADD COLUMN baseline_ts" in src
    assert '"baseline_ts": int(d.get("baseline_ts") or 0)' in src, \
        "load_guard_state 没读出来"
    assert "baseline_ts" in src[src.index("def save_guard_state"):], \
        "save 路径没有 baseline_ts"


def test_guard_restores_peak_using_baseline():
    """Guard 恢复峰值时必须把 baseline_ts 传进去，否则等于没做。"""
    src = _read("ai_trader", "guard.py")
    assert "baseline_ts" in src, "Guard 不知道基线这个概念"
    i = src.index("def _restore_peak")
    body = src[i:i + 3000]
    assert "peak_equity(strategy, paper, since)" in body, \
        "恢复峰值时没带上基线时刻"


def test_reset_tool_requires_explicit_apply():
    """重置工具默认干跑。这种动风控基准的操作绝不能一条命令就生效。"""
    src = _read("tools", "reset_live_baseline.py")
    assert '"--apply" in sys.argv' in src
    assert "干跑" in src
    # 不许删 equity 历史
    assert "DELETE FROM equity" not in src, "重置工具在删历史行"

def test_reset_also_rebases_daily_start_equity():
    """重置峰值时必须同时重置当日基准权益。

    只改峰值不改当日基准，日亏损闸仍然按「基准 100U -> 现在 0.0071U
    = 今天亏了 100%」继续硬停到明天。表现出来就是"改了没用"。
    本金断层（提款/注资）不是当日亏损，不该计入日亏损闸。
    """
    src = _read("tools", "reset_live_baseline.py")
    assert '"day_start_equity": cur_eq' in src, "没重置当日基准权益"
    assert '"daily_halt_day": ""' in src, "没清当日硬停标记"
