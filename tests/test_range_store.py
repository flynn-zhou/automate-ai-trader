# -*- coding: utf-8 -*-
"""区间演化存储层测试：三表落库、幂等、None 保真、方向映射。

重点不是「能写进去」，而是：
  1. 同一份数据跑两遍，行数不变（UNIQUE(symbol,ts) + upsert 幂等）
  2. None 边界落库后仍是 NULL，不能变成 0（那是伪造数据）
  3. DOWN/UP 两个方向的 S/R/P/K 映射各自正确，且与 api._row 口径一致
"""
from __future__ import annotations

import importlib
import os

import pytest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
REF_CSV = os.path.join(FIXTURES, "range_bars_15m_v11_ref.csv")


@pytest.fixture()
def engine(monkeypatch):
    if not os.path.exists(REF_CSV):
        pytest.skip("缺少 v11 夹具 CSV")
    monkeypatch.setenv("RANGE_BARS_CSV", REF_CSV)
    from ai_trader.range import engine as E
    importlib.reload(E)
    yield E
    monkeypatch.undo()
    importlib.reload(E)


@pytest.fixture()
def store(tmp_path):
    from ai_trader.range.store import RangeStore
    s = RangeStore(str(tmp_path / "r.db"))
    yield s
    s.close()


def test_persist_run_writes_three_tables(engine, store):
    from ai_trader.range.store import persist_run

    bars = engine.load_1h(verbose=False)
    events, states = engine.build(bars)
    got = persist_run(store, "BTCUSDT", bars, events, states, engine.ts8, engine.lab)

    assert got["range_evolution"] > 0
    assert got["bars_1h"] > 0
    n = store.counts("BTCUSDT")
    assert n["range_evolution"] == len(states)
    assert n["bars_1h"] == len(bars)


def test_persist_is_idempotent(engine, store):
    """同一份数据跑两遍，行数必须不变。"""
    from ai_trader.range.store import persist_run

    bars = engine.load_1h(verbose=False)
    events, states = engine.build(bars)
    persist_run(store, "BTCUSDT", bars, events, states, engine.ts8, engine.lab)
    first = store.counts("BTCUSDT")
    persist_run(store, "BTCUSDT", bars, events, states, engine.ts8, engine.lab)
    second = store.counts("BTCUSDT")
    assert first == second, "重复落库改变了行数: %s -> %s" % (first, second)


def test_none_boundaries_stay_null_not_zero(engine, store):
    """None 必须落成 NULL。填 0 会让下游把「没有这条线」读成「价格是 0」。"""
    from ai_trader.range.store import persist_run

    bars = engine.load_1h(verbose=False)
    events, states = engine.build(bars)
    persist_run(store, "BTCUSDT", bars, events, states, engine.ts8, engine.lab)

    rows = store.states("BTCUSDT")
    assert rows, "没有读到任何四线状态"
    zeros = [r for r in rows if any(v == 0 for v in r[2:7])]
    assert not zeros, "出现了 0 边界，说明 None 被填成了 0: %s" % zeros[:2]
    nulls = [r for r in rows if any(v is None for v in r[3:7])]
    assert nulls, "DOWN 区间应有 None 边界（例如 hard_dn），结果一个都没有"


def test_direction_mapping_down_vs_up(engine, store):
    """DOWN 与 UP 的映射必须各自正确，不能共用一套。"""
    from ai_trader.range.store import persist_run

    bars = engine.load_1h(verbose=False)
    events, states = engine.build(bars)
    persist_run(store, "BTCUSDT", bars, events, states, engine.ts8, engine.lab)

    rows = store.states("BTCUSDT")
    down = [r for r in rows if r[9] == "DOWN"]
    up = [r for r in rows if r[9] == "UP"]
    assert down and up, "两个方向都应出现，实际 DOWN=%d UP=%d" % (len(down), len(up))

    # DOWN: hard_up=R 有值, hard_dn 恒为 None
    d_formed = [r for r in down if r[4] is not None]
    assert d_formed, "DOWN 区间应有 hard_up(R)"
    assert all(r[5] is None for r in d_formed), "DOWN 区间的 hard_dn 必须为 None"

    # UP: hard_dn=R 有值, hard_up 恒为 None
    u_formed = [r for r in up if r[5] is not None]
    assert u_formed, "UP 区间应有 hard_dn(R)"
    assert all(r[4] is None for r in u_formed), "UP 区间的 hard_up 必须为 None"


def test_store_matches_api_row_semantics(engine, store):
    """存储层与 api._row 必须是同一套口径（同一个量只有一个名字）。"""
    from ai_trader.range import api
    from ai_trader.range.store import persist_run

    bars = engine.load_1h(verbose=False)
    events, states = engine.build(bars)
    persist_run(store, "BTCUSDT", bars, events, states, engine.ts8, engine.lab)
    stored = {r[0]: r for r in store.states("BTCUSDT")}
    hist = api.history(bars)

    checked = 0
    for row in hist:
        s = stored.get(row["ts"])
        if s is None:
            continue
        checked += 1
        # stored: ts, ts_h, soft_up, soft_dn, hard_up, hard_dn, v11_key, ...
        assert s[2] == row["soft_up"], "soft_up 口径不一致 @%s" % row["ts_h"]
        assert s[3] == row["soft_dn"]
        assert s[4] == row["hard_up"]
        assert s[5] == row["hard_dn"]
        assert s[6] == row["key"]
    assert checked > 1000, "比对根数过少: %d" % checked
