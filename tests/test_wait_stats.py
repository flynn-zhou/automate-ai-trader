# -*- coding: utf-8 -*-
"""等待预期模块：边界与单调性。"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.trading.wait_stats import expected_wait, _BUCKETS, SOURCE


def test_above_sma_returns_none():
    """够到【触发线】之后不该给等待预期——那时策略不是在等上穿。

    ★ 这条用例在第九轮改过判据，理由必须写清楚，不能只是把断言改顺。

    旧语义（无滞回）：边界是裸均线，dist >= 0 就返回 None。
    新语义（有 0.2% 滞回带）：边界是 SMA*(1+h)，dist >= 0.2 才返回 None。
      价格恰好等于均线（dist=0）时，系统【仍然是空仓在等】——
      因为进场要求收盘高出均线 0.2%。这时不给预期，
      就是把"在等"说成"没什么可等的"，大屏会少掉最关键的一句话。

    所以 0.0 现在应当有预期；1.5 仍然应当为 None（已远超触发线）。
    """
    assert expected_wait(1.5) is None, "远超触发线，不该再报等待"
    ew = expected_wait(0.0)
    assert ew is not None, (
        "价格恰在均线上时，离触发线还差一个滞回带，仍然在等待")
    assert ew["median_days"] > 0


def test_none_boundary_is_the_trigger_not_the_sma():
    """边界必须落在触发线（0.2%）上，而不是裸均线（0）上。"""
    from ai_trader.trading.wait_stats import CALIBRATED_HYST_PCT
    h = CALIBRATED_HYST_PCT
    assert expected_wait(h - 1e-6) is not None, "还差一点点，仍在等待"
    assert expected_wait(h) is None, "刚好够到触发线，不再等待"
    assert expected_wait(h + 0.1) is None


def test_bad_input_returns_none():
    assert expected_wait(None) is None
    assert expected_wait("abc") is None


def test_current_state_has_expectation():
    """当前线上偏离约 -5.3%，必须能查到。"""
    ew = expected_wait(-5.32)
    assert ew is not None, u"当前状态查不到等待预期"
    assert ew["median_days"] > 0
    assert ew["p90_days"] >= ew["median_days"]
    assert ew["samples"] >= 100, u"样本太少，结论不可信"
    # 必须带上出处，看板要标明这是历史频率而不是预测
    assert ew["source"]["bars"] > 10000
    assert "method" in ew["source"]


def test_empty_wait_is_monotone_in_deviation():
    """越低于均线，等得越久。单调性是对口径的一个验证：
    如果统计口径错了，通常不会留下这种结构。"""
    devs = [-1.0, -3.0, -5.0, -8.0, -15.0, -30.0]
    meds = []
    for d in devs:
        ew = expected_wait(d)
        assert ew is not None, u"偏离 %.1f%% 查不到" % d
        meds.append(ew["median_days"])
    for a, b in zip(meds, meds[1:]):
        assert b >= a, (
            u"等待天数随偏离加深不单调：%s（偏离 %s）。"
            u"单调性被破坏说明分桶或口径有问题。" % (meds, devs))


def test_buckets_cover_all_negatives():
    """任何负偏离都必须落进某个桶，不能有查不到的空洞。"""
    for d in (-0.01, -0.1, -1.99, -2.0, -3.99, -4.0, -5.99, -6.0,
              -9.99, -10.0, -19.99, -20.0, -50.0, -99.0):
        assert expected_wait(d) is not None, u"偏离 %.2f%% 落在桶外" % d


def test_source_is_declared():
    """出处必须可查：历史频率和预测是两件事，看板要能标明。"""
    assert SOURCE["bars"] > 10000
    assert SOURCE["from"] and SOURCE["to"]
    assert "独立事件" in SOURCE["method"]
