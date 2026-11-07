# -*- coding: utf-8 -*-
"""等待预期必须按【真触发线】算，不是按裸均线算。

第九轮加了 0.2% 滞回带，进场条件从"收盘 > SMA"变成"收盘 > SMA*(1+h)"。
wait_stats 那张表原来是按"涨回裸均线要多久"标定的，
直接拿来回答"还要等多久"会偏乐观。

实测（14999 根 4h，只换目标、同样的历史样本）：
    (-2,0]      1.3 -> 1.7 天
    (-4,-2]     5.8 -> 6.3
    (-6,-4]     9.0 -> 9.8
    (-10,-6]   10.8 -> 11.2
    (-20,-10]  14.8 -> 14.8
    (-inf,-20] 20.5 -> 20.5
本表已按真触发线重新生成。

中途我自己踩过一次：第一版只把偏离度减去 h 再去查旧表。
那只修正"跨桶"的情形，换不出 (-6,-4] 桶那 0.8 天——
线上照样显示 9.0，而真值是 9.8。已改成重算整张表。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.trading.wait_stats import (
    expected_wait, CALIBRATED_HYST_PCT, SOURCE, _BUCKETS)


def test_table_is_calibrated_on_the_trigger():
    """表必须声明自己是按触发线算的，而不是按裸均线。"""
    assert CALIBRATED_HYST_PCT > 0, "标定值必须是正的滞回半宽"
    assert SOURCE["hyst_pct"] == pytest.approx(CALIBRATED_HYST_PCT)
    assert "SMA*(1+h)" in SOURCE["target"], (
        "出处必须写明目标是触发线，否则以后没人知道这张表怎么来的")
    assert SOURCE["bars"] >= 14000


def test_table_matches_measured_numbers():
    """钉住实测值。这些数来自服务器 14999 根 4h 重算，不是估的。

    如果有人改表却没过生成脚本，这里会红。
    """
    want = [1.7, 6.3, 9.8, 11.2, 14.8, 20.5]
    got = [b[2] for b in _BUCKETS]
    assert got == pytest.approx(want, abs=1e-9), (
        "表与实测不符：%s vs %s（请用 tools/gen_wait_hyst.py 重新生成）"
        % (got, want))


def test_table_is_monotone():
    """越低于均线等得越久。单调性是对口径的一个验证。"""
    meds = [b[2] for b in _BUCKETS]
    for a, b in zip(meds, meds[1:]):
        assert b >= a, "等待中位数不单调：%s" % (meds,)


def test_band_costs_more_wait_than_bare_sma():
    """钉住核心结论：带滞回之后，同一状态要等更久（或至少不更早）。

    对照的"裸均线"数值是本文件里写死的旧实测值。
    """
    bare = {0: 1.3, 1: 5.8, 2: 9.0, 3: 10.8, 4: 14.8, 5: 20.5}
    for k, b in enumerate(_BUCKETS):
        assert b[2] >= bare[k] - 1e-9, (
            "桶 %d 带滞回后反而更快了：%.1f -> %.1f" % (k, bare[k], b[2]))


def test_deeper_bucket_is_what_current_state_uses():
    """当前线上状态（约 -5.1%）必须落在 (-6,-4] 桶，报 9.8 天而不是 9.0。"""
    ew = expected_wait(-5.117)
    assert ew is not None
    assert ew["median_days"] == pytest.approx(9.8), (
        "当前状态应当报按触发线算的 9.8 天；报 9.0 说明还在用裸均线口径")


def test_upper_band_still_has_expectation():
    """价格在均线上方但没到触发线（0 <= dist < h）仍要给预期。"""
    ew = expected_wait(0.1)
    assert ew is not None, "上半带还没进场，必须给等待预期"
    assert ew["median_days"] == pytest.approx(1.7)


def test_past_trigger_returns_none():
    """反证：够到触发线之后返回 None。"""
    assert expected_wait(0.2) is None
    assert expected_wait(0.5) is None


def test_changing_h_is_flagged_as_extrapolation():
    """换掉 h 之后这张表就是外推，必须标出来，不能静默给数。

    这是防止"以后有人改 h、表没重算、大屏继续报偏乐观的天数"
    ——那种错不会抛异常，只会安静地错。
    """
    ew = expected_wait(-5.0, 0.5)
    assert ew is not None
    assert ew.get("extrapolated") is True, (
        "请求的 h=0.5 与标定的 %.1f 不同，必须标 extrapolated"
        % CALIBRATED_HYST_PCT)
    assert ew["calibrated_hyst_pct"] == pytest.approx(CALIBRATED_HYST_PCT)
    assert ew["requested_hyst_pct"] == pytest.approx(0.5)


def test_matching_h_is_not_flagged():
    """反证：用标定值时不打外推标记，否则看板上会一直挂着警告。"""
    ew = expected_wait(-5.0, CALIBRATED_HYST_PCT)
    assert not ew.get("extrapolated")
