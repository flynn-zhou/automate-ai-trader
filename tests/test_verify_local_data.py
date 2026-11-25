# -*- coding: utf-8 -*-
"""`tools/verify_local_data.py` 的测试：校验器自己必须能被校验。

为什么值得单独测：
    这个脚本唯一的用途是「发现数据缺口」。它自己要是漏报，
    后果比没有这个脚本更糟 —— 会给人一种「验过了，没问题」的错觉。
    本项目已经三次栽在「工具说成功，其实没做」上
    （`INSERT OR IGNORE` 报 written=N、攒着不写、空输入算成功）。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location(
    "verify_local_data", os.path.join(_ROOT, "tools", "verify_local_data.py"))
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)

M5 = 300_000
M15 = 900_000
H1 = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % M15)


# ---------------------------------------------------------------- 造库

def _mk_bars(path, rows, gaps=()):
    """rows: [(interval_ms, open_time)]；gaps: [(interval_ms, open_time)]"""
    c = sqlite3.connect(str(path))
    c.execute("create table bars(symbol text, interval_ms integer,"
              " open_time integer, o real, h real, l real, c real,"
              " v real, q real)")
    for iv, ts in rows:
        c.execute("insert into bars values('BTCUSDT',?,?,1,1,1,1,1,1)",
                  (iv, ts))
    c.execute("create table bar_gaps(symbol text, interval_ms integer,"
              " open_time integer, detected_at integer)")
    for iv, ts in gaps:
        c.execute("insert into bar_gaps values('BTCUSDT',?,?,?)",
                  (iv, ts, ts + 1000))
    c.commit()
    c.close()


def _mk_kbar(path, rows):
    c = sqlite3.connect(str(path))
    c.execute("create table kbar_evolution(id integer primary key,"
              " symbol text, interval_ms integer, ts integer, idx integer)")
    c.execute("create table kbar_line(id integer primary key, symbol text,"
              " interval_ms integer, ts integer, line_key text,"
              " line_kind text, line_period_ms integer, line_price real,"
              " bar_close real, line_ready_ms integer)")
    for iv, ts in rows:
        c.execute("insert into kbar_evolution(symbol,interval_ms,ts,idx)"
                  " values('BTCUSDT',?,?,0)", (iv, ts))
    c.commit()
    c.close()


def _mk_range(path, rows):
    c = sqlite3.connect(str(path))
    c.execute("create table bars_1h(id integer primary key, symbol text,"
              " ts integer, ts_h integer, open real, high real, low real,"
              " close real, volume real)")
    c.execute("create table range_evolution(id integer primary key,"
              " symbol text, ts integer, ts_h integer)")
    for ts in rows:
        c.execute("insert into bars_1h(symbol,ts,ts_h,open,high,low,close,"
                  "volume) values('BTCUSDT',?,?,1,1,1,1,1)", (ts, ts))
        c.execute("insert into range_evolution(symbol,ts,ts_h)"
                  " values('BTCUSDT',?,?)", (ts, ts))
    c.commit()
    c.close()


def _series(interval, n, start=T0):
    return [(interval, start + k * interval) for k in range(n)]


def _levels(checks):
    return {c.title: c.level for c in checks}


# ---------------------------------------------------------------- 连续性

def test_series_stats_reports_a_multi_bar_hole_with_its_size():
    """跳 5 格和跳 1 格都是断，但严重程度必须能看出来。

    数据：下标 0,1,2 之后直接跳到 8 —— 中间缺 3,4,5,6,7 共 **5 根**。
    """
    ts = [T0 + k * M5 for k in range(3)] + [T0 + (3 + 5) * M5]
    st = V.series_stats(ts, M5)
    assert st["rows"] == 4
    assert st["expect"] == 9
    assert st["missing"] == 5, st
    assert st["gaps"][0]["missing_bars"] == 5, st["gaps"]


def test_series_stats_refuses_a_zero_interval():
    """算不出节奏时必须硬报错，绝不能静默返回「0 缺失」。"""
    with pytest.raises(ValueError, match="interval"):
        V.series_stats([T0, T0 + M5], 0)


def test_contiguous_series_passes():
    st = V.series_stats([T0 + k * M15 for k in range(10)], M15)
    assert st["missing"] == 0
    assert st["gaps"] == []


# ---------------------------------------------------------------- 主流程

def _verify(tmp_path, bars_rows, kbar_rows, range_rows, now_ms,
            market_gaps=(), require_fresh=True):
    m = tmp_path / "market.db"
    k = tmp_path / "kbar.db"
    r = tmp_path / "range.db"
    _mk_bars(m, bars_rows, market_gaps)
    _mk_kbar(k, kbar_rows)
    _mk_range(r, range_rows)
    return V.verify(str(m), str(k), str(r), now_ms=now_ms,
                    require_fresh=require_fresh)


def test_a_complete_and_fresh_snapshot_has_no_fail(tmp_path):
    last = T0 + 9 * M15
    checks = _verify(
        tmp_path,
        _series(M15, 10) + _series(M5, 30),
        _series(M15, 10) + _series(M5, 30),
        [T0 + k * H1 for k in range(10)],
        now_ms=last + M15)          # 刚好收完最后一根
    fails = [c for c in checks if c.level == V.FAIL]
    assert not fails, [(c.title, c.detail) for c in fails]


def test_a_missing_bar_turns_continuity_red(tmp_path):
    """★ 这是这个脚本存在的理由：缺一根必须红。"""
    bars = _series(M15, 10)
    del bars[4]                      # 挖掉一根
    last = T0 + 9 * M15
    checks = _verify(tmp_path, bars, _series(M15, 10),
                     [T0 + k * H1 for k in range(10)],
                     now_ms=last + M15)
    bad = [c for c in checks if c.level == V.FAIL and "不连续" in c.title]
    assert bad, [(c.level, c.title, c.detail) for c in checks]
    assert "缺 1 根" in bad[0].detail, bad[0].detail


def test_stale_data_turns_freshness_red(tmp_path):
    """本地数据停在 11-01 却当成「最新」用，是最贵的错误。"""
    last = T0 + 9 * M5
    checks = _verify(tmp_path, _series(M5, 10), _series(M5, 10),
                     [T0 + k * H1 for k in range(10)],
                     now_ms=last + 48 * H1)      # 落后两天
    bad = [c for c in checks if c.level == V.FAIL and "不新鲜" in c.title]
    assert bad, [(c.level, c.title, c.detail) for c in checks]


def test_require_fresh_false_allows_an_old_snapshot(tmp_path):
    """拿历史快照做分析时可以关掉新鲜度，但连续性照样要查。"""
    bars = _series(M5, 10)
    del bars[3]
    checks = _verify(tmp_path, bars, _series(M5, 10),
                     [T0 + k * H1 for k in range(10)],
                     now_ms=T0 + 500 * H1, require_fresh=False)
    assert not [c for c in checks if "不新鲜" in c.title]
    assert [c for c in checks if "不连续" in c.title and c.level == V.FAIL]


def test_fine_interval_lagging_behind_the_coarse_one_is_a_fail(tmp_path):
    """5m 比 15m 落后 → 入场时钟会静默退回粗周期，必须红。"""
    checks = _verify(tmp_path,
                     _series(M15, 20) + _series(M5, 10),
                     _series(M15, 20) + _series(M5, 10),
                     [T0 + k * H1 for k in range(20)],
                     now_ms=T0 + 20 * M15, require_fresh=False)
    bad = [c for c in checks if c.level == V.FAIL and "周期跨越" in c.title]
    assert bad, [(c.level, c.title, c.detail) for c in checks]


def test_missing_file_is_a_fail_not_a_crash(tmp_path):
    checks = V.verify(str(tmp_path / "nope.db"), str(tmp_path / "nope2.db"),
                      str(tmp_path / "nope3.db"), now_ms=T0)
    assert checks
    assert all(c.level == V.FAIL for c in checks)


def test_recorded_gaps_are_surfaced_as_a_warning(tmp_path):
    """生产侧登记过断口 → 不判死，但必须让人看见。"""
    checks = _verify(tmp_path, _series(M15, 10), _series(M15, 10),
                     [T0 + k * H1 for k in range(10)],
                     now_ms=T0 + 10 * M15,
                     market_gaps=[(M15, T0 + 3 * M15)])
    got = [c for c in checks if c.title == "记录断口"]
    assert got and got[0].level == V.WARN, got
    assert "共 1 条" in got[0].detail


def test_line_ready_mismatch_is_a_warning(tmp_path):
    m = tmp_path / "market.db"
    k = tmp_path / "kbar.db"
    r = tmp_path / "range.db"
    _mk_bars(m, _series(M15, 10))
    _mk_kbar(k, _series(M15, 10))
    _mk_range(r, [T0 + k * H1 for k in range(10)])
    c = sqlite3.connect(str(k))
    # 一条符合 ready = ts+period，一条不符合
    c.execute("insert into kbar_line(symbol,interval_ms,ts,line_key,"
              "line_kind,line_period_ms,line_price,bar_close,line_ready_ms)"
              " values('BTCUSDT',900000,?,?, 'ema',900000,1,1,?)",
              (T0, "ema20@15m", T0 + 900_000))
    c.execute("insert into kbar_line(symbol,interval_ms,ts,line_key,"
              "line_kind,line_period_ms,line_price,bar_close,line_ready_ms)"
              " values('BTCUSDT',900000,?,?, 'ema',900000,1,1,?)",
              (T0, "range_soft_dn@1h", T0))       # ready 不等于 ts+period
    c.commit()
    c.close()
    checks = V.verify(str(m), str(k), str(r), now_ms=T0 + 10 * M15,
                      require_fresh=False)
    got = [x for x in checks if x.title == "线就绪时刻"]
    assert got and got[0].level == V.WARN, got


def test_main_returns_one_when_something_fails(tmp_path):
    m = tmp_path / "market.db"
    k = tmp_path / "kbar.db"
    r = tmp_path / "range.db"
    bars = _series(M5, 10)
    del bars[2]
    _mk_bars(m, bars)
    _mk_kbar(k, _series(M5, 10))
    _mk_range(r, [T0 + k * H1 for k in range(10)])
    rc = V.main(["--market-db", str(m), "--kbar-db", str(k),
                 "--range-db", str(r), "--no-fresh", "--json"])
    assert rc == 1


def test_summarize_counts_each_level():
    checks = [V.Check(V.OK, "a"), V.Check(V.WARN, "b"),
              V.Check(V.FAIL, "c"), V.Check(V.FAIL, "d")]
    assert V.summarize(checks) == {V.OK: 1, V.WARN: 1, V.FAIL: 2}
