# -*- coding: utf-8 -*-
"""K 线意图识别。

两类断言缺一不可：
  正向 —— 形态成立时真的判出来
  负向 —— 形态不成立时**不许**判出来（越界但没放量不算突破；长影线但不是插针不算恐慌盘）
只测正向会得到一个「什么都是突破」的识别器，而那比没有识别器更危险。
"""
from __future__ import annotations

import pytest

from ai_trader.trading import kline_intent as K


def mk(o, h, l, c, v=100.0, ts=0):
    return (ts, o, h, l, c, v)


def flat(n=20, v=100.0, span=1.0):
    """一段安静的历史：每根 h-l=span，量恒定。"""
    return [mk(100.0, 100.0 + span, 100.0 - span * 0.5, 100.0, v) for _ in range(n)]


# ---------------------------------------------------------------- 量能
def test_volume_heavy():
    bars = flat(20) + [mk(100, 101, 99, 100.5, v=200)]
    s, mult, ev = K.volume_regime(bars, len(bars) - 1)
    assert s == K.VOL_HEAVY_S
    assert mult == pytest.approx(2.0, rel=0.01)


def test_volume_thin():
    bars = flat(20) + [mk(100, 101, 99, 100.5, v=40)]
    s, mult, _ = K.volume_regime(bars, len(bars) - 1)
    assert s == K.VOL_THIN_S


def test_volume_normal():
    bars = flat(20) + [mk(100, 101, 99, 100.5, v=100)]
    s, _, _ = K.volume_regime(bars, len(bars) - 1)
    assert s == K.VOL_NORMAL_S


def test_volume_insufficient_data_is_normal_not_heavy():
    """只有一两根时不能凭单根判放量——那是把噪声当信号。"""
    bars = [mk(100, 101, 99, 100.5, v=9999), mk(100, 101, 99, 100.5, v=9999)]
    s, mult, ev = K.volume_regime(bars, 1)
    assert s == K.VOL_NORMAL_S and mult is None
    assert "数据不足" in ev


# ---------------------------------------------------------------- 恐慌盘
def test_panic_flush_long_lower_wick_with_volume():
    bars = flat(20) + [mk(100.0, 101.0, 95.0, 100.5, v=300)]
    r = K.read_kline(bars)
    assert r.panic_flush is True
    assert r.volume_regime == K.VOL_HEAVY_S
    assert r.low == 95.0
    assert any("下影线" in e for e in r.evidence)


def test_panic_flush_requires_volume():
    """长下影但没有放量 = 普通回抽，不是插针。"""
    bars = flat(20) + [mk(100.0, 101.0, 95.0, 100.5, v=100)]
    r = K.read_kline(bars)
    assert r.panic_flush is False


def test_panic_flush_requires_close_recovery():
    """长下影但收在最低附近 = 真跌，不是洗盘。"""
    bars = flat(20) + [mk(100.0, 101.0, 95.0, 95.2, v=300)]
    r = K.read_kline(bars)
    assert r.panic_flush is False


# ---------------------------------------------------------------- 突破
def test_breakout_above_p_with_volume():
    bars = flat(20) + [mk(100.0, 102.0, 99.8, 101.5, v=300)]
    st = {"direction": "UP", "p": 100.0}
    r = K.read_kline(bars, range_state=st)
    assert r.intent == K.INTENT_BREAKOUT
    assert any("越过上沿" in e for e in r.evidence)


def test_breakdown_below_p_with_volume():
    bars = flat(20) + [mk(100.0, 100.2, 98.0, 98.5, v=300)]
    st = {"direction": "DOWN", "p": 100.0}
    r = K.read_kline(bars, range_state=st)
    assert r.intent == K.INTENT_BREAKOUT
    assert any("跌破下沿" in e for e in r.evidence)


def test_breakout_without_volume_is_not_breakout():
    """关键的负向用例：越界但缩量/平量，是假突破，不许判 breakout。"""
    bars = flat(20) + [mk(100.0, 102.0, 99.8, 101.5, v=100)]
    r = K.read_kline(bars, range_state={"direction": "UP", "p": 100.0})
    assert r.intent != K.INTENT_BREAKOUT
    assert any("未获量能确认" in e for e in r.evidence)


def test_no_breakout_without_range_state():
    """没有区间背景时不许臆造突破——只能判形态。"""
    bars = flat(20) + [mk(100.0, 102.0, 99.8, 101.5, v=300)]
    r = K.read_kline(bars, range_state=None)
    assert r.intent != K.INTENT_BREAKOUT


def test_breakout_needs_buffer_not_hugging_line():
    """刚压线不算突破——缓冲是 0.1 ATR。"""
    bars = flat(20) + [mk(100.0, 101.02, 99.8, 100.02, v=300)]
    r = K.read_kline(bars, range_state={"direction": "UP", "p": 100.0})
    assert r.intent != K.INTENT_BREAKOUT


# ---------------------------------------------------------------- 派发 / 衰竭 / 延续
def test_distribution_heavy_volume_small_body_at_boundary():
    bars = flat(20) + [mk(100.0, 100.6, 99.9, 100.2, v=300)]
    r = K.read_kline(bars, range_state={"direction": "UP", "p": 100.2})
    assert r.intent == K.INTENT_DISTRIBUTION
    assert any("派发" in e for e in r.evidence)


def test_exhaustion_long_counter_wick_far_from_p():
    bars = flat(20) + [mk(100.0, 104.0, 99.8, 100.2, v=100)]
    r = K.read_kline(bars, range_state={"direction": "UP", "p": 95.0})
    assert r.intent == K.INTENT_EXHAUSTION
    assert any("衰竭" in e for e in r.evidence)


def test_continuation_strong_body_aligned_with_direction():
    bars = flat(20) + [mk(100.0, 100.9, 99.95, 100.8, v=100)]
    r = K.read_kline(bars, range_state={"direction": "UP", "p": 95.0})
    assert r.intent == K.INTENT_CONTINUATION


def test_continuation_requires_alignment():
    """强实体但方向与区间相反，不叫延续。"""
    bars = flat(20) + [mk(100.8, 100.9, 99.95, 100.0, v=100)]
    r = K.read_kline(bars, range_state={"direction": "UP", "p": 95.0})
    assert r.intent != K.INTENT_CONTINUATION


def test_none_when_nothing_matches():
    bars = flat(20) + [mk(100.0, 100.4, 99.6, 100.1, v=100)]
    r = K.read_kline(bars)
    assert r.intent == K.INTENT_NONE
    assert r.evidence, "即使无结论也要给证据，不能是空"


# ---------------------------------------------------------------- 契约
def test_every_read_carries_evidence():
    """任何结论都必须能解释——这是可复核的前提。"""
    bars = flat(20) + [mk(100.0, 102.0, 99.8, 101.5, v=300)]
    for st in (None, {"direction": "UP", "p": 100.0}):
        r = K.read_kline(bars, range_state=st)
        assert r.evidence, "缺少证据串"


def test_read_kline_is_pure(monkeypatch):
    """纯函数：不修改入参，重复调用结果相同。"""
    bars = flat(20) + [mk(100.0, 102.0, 99.8, 101.5, v=300)]
    snapshot = [tuple(b) for b in bars]
    a = K.read_kline(bars, range_state={"direction": "UP", "p": 100.0})
    b = K.read_kline(bars, range_state={"direction": "UP", "p": 100.0})
    assert [tuple(x) for x in bars] == snapshot, "入参被修改了"
    assert a == b, "同输入必须同输出"


def test_index_out_of_range_raises():
    with pytest.raises(IndexError):
        K.read_kline(flat(5), i=99)


def test_empty_bars_returns_none_intent():
    r = K.read_kline([])
    assert r.intent == K.INTENT_NONE
    assert r.evidence


def test_output_feeds_the_guard_contract():
    """与 position_guard.decide(kline=) 的字段契约必须对得上。"""
    from ai_trader.trading.position_guard import (GuardConfig, GuardPosition,
                                                  HOLD, decide)
    bars = flat(20) + [mk(100.0, 102.0, 99.8, 101.5, v=300)]
    read = K.read_kline(bars, range_state={"direction": "UP", "p": 100.0})
    pos = GuardPosition("BTCUSDT", "long", 100.0, 5.0 / 1.2, 98.8, 95.955,
                        initial_risk=5.0)
    d = decide(pos, 101.4, GuardConfig(), kline=read)   # 不应抛异常
    assert d.action in (HOLD, "MOVE_STOP", "PARTIAL_TP", "ADD", "EXIT_STOP",
                        "EXIT_STRUCTURE", "REDUCE")


def test_p0_never_consults_kline(monkeypatch):
    """P0 保命分支禁止调用 K 线判断。

    做法：让 read_kline 一旦被调用就炸。P0 场景（缓冲 < 0.5）下必须照常离场。
    """
    from ai_trader.trading.position_guard import (GuardConfig, GuardPosition,
                                                  EXIT_STOP, decide)

    def boom(*a, **k):
        raise AssertionError("P0 分支调用了 K 线判断——这会让亏损仓位被留在场上")

    monkeypatch.setattr(K, "read_kline", boom)
    pos = GuardPosition("BTCUSDT", "long", 100.0, 5.0 / 1.2, 98.8, 95.6,
                        initial_risk=5.0)   # 缓冲极小
    d = decide(pos, 96.0, GuardConfig(), kline=None)
    assert d.action == EXIT_STOP
