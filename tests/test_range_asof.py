# -*- coding: utf-8 -*-
"""functional 入口：给指定时间，回答「当时区间是什么」。

两条实现路径必须口径一致：
    engine.as_of(bars, when)   —— 从 CSV 重算（研究/无库时用）
    store.state_at(ts)         —— 直接查库（生产/快路径）
如果两者对同一个 ts 给出不同答案，说明「引擎版本」和「库里那份」不是同一代，
那就是最危险的一类错误：页面显示的和实际算出来的不是一回事。
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


def test_as_of_returns_state_at_that_time(engine):
    from ai_trader.range import api

    bars = engine.load_1h(verbose=False)
    mid = engine.ts8(bars[len(bars) // 2][0])
    got = api.as_of(mid, bars=bars)
    assert got is not None
    assert got["ts_h"] == mid, "应当返回该时刻那一根，实际 %s" % got["ts_h"]
    assert got["direction"] in ("UP", "DOWN", "PRE", "")


def test_as_of_picks_last_bar_not_future(engine):
    """必须是「<= 该时刻的最后一根」，绝不能把未来的状态提前透露。"""
    from ai_trader.range import api

    bars = engine.load_1h(verbose=False)
    target = bars[len(bars) // 2]
    got = api.as_of(target[0], bars=bars)
    assert got["ts"] == target[0], "选错了根: 期望 %s 实际 %s" % (target[0], got["ts"])

    # 往前挪 1 毫秒必须落到前一根
    prev = api.as_of(target[0] - 1, bars=bars)
    if prev is not None:
        assert prev["ts"] < target[0], "差 1ms 却拿到了同一根，说明边界没守住"


def test_as_of_before_first_bar_returns_none(engine):
    """早于第一根返回 None，不是空 dict——调用方要能区分「没数据」和「未成形」。"""
    from ai_trader.range import api

    bars = engine.load_1h(verbose=False)
    assert api.as_of(bars[0][0] - 86400000, bars=bars) is None


def test_as_of_includes_the_bar_itself(engine):
    """用户要的是「那根 1h K 线对应的区间」，所以必须带上那根的价格。"""
    from ai_trader.range import api

    bars = engine.load_1h(verbose=False)
    got = api.as_of(bars[-1][0], bars=bars)
    assert got["bar"] is not None
    for k in ("open", "high", "low", "close"):
        assert got["bar"][k] is not None
    assert got["bar"]["high"] >= got["bar"]["low"]


def test_store_state_at_agrees_with_engine_as_of(engine, tmp_path):
    """库里查到的与引擎算出来的必须是同一份答案（同一代口径）。"""
    from ai_trader.range import api
    from ai_trader.range.store import RangeStore, persist_run

    bars = engine.load_1h(verbose=False)
    events, states = engine.build(bars)
    with RangeStore(str(tmp_path / "r.db")) as st:
        persist_run(st, "BTCUSDT", bars, events, states, engine.ts8, engine.lab)
        checked = 0
        for i in (len(bars) // 4, len(bars) // 2, len(bars) - 1):
            ts = bars[i][0]
            db = st.state_at_dict("BTCUSDT", ts)
            eng = api.as_of(ts, bars=bars)
            assert db is not None and eng is not None
            assert db["ts"] == eng["ts"], "库与引擎选到了不同的根 @%s" % ts
            for k in ("soft_up", "soft_dn", "hard_up", "hard_dn", "key",
                      "family", "segment", "direction"):
                assert db[k] == eng[k], "字段 %s 不一致 @%s: 库=%r 引擎=%r" % (
                    k, ts, db[k], eng[k])
            checked += 1
        assert checked == 3


def test_state_at_returns_none_when_no_data(tmp_path):
    from ai_trader.range.store import RangeStore

    with RangeStore(str(tmp_path / "empty.db")) as st:
        assert st.state_at_dict("BTCUSDT", 1234567890123) is None
