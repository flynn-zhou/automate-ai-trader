# -*- coding: utf-8 -*-
"""仓位派生计算与开仓时段闸门（《下单与守卫_运行规则.md》§3 / §4 / §6）。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_trader.trading.sizing import (
    SessionConfig,
    SizingConfig,
    default_session_windows,
    derive,
    in_trade_window,
    plan_position,
    session_label,
)

CFG = SizingConfig()   # 默认 100U / 5% / 22x


# ------------------------------------------------------------------ 四行派生
def test_derive_defaults_match_documented_values():
    """文档 §3 的四行必须与代码逐位一致，否则配置页显示的就是假数字。"""
    d = derive(CFG)
    assert d.risk_per_trade == 5.0
    assert abs(d.liq_distance_pct - 4.045) < 1e-3
    assert abs(d.max_stop_distance_pct - 1.618) < 1e-3
    assert d.margin_cap_amount == 40.0
    assert d.lives == 20


def test_risk_is_floored_not_rounded():
    """『向下取整』：110U x 5% = 5.5 -> 5，不是 6（多给风险就是多亏钱）。"""
    assert derive(SizingConfig(capital=110.0)).risk_per_trade == 5.0
    assert derive(SizingConfig(capital=119.9)).risk_per_trade == 5.0
    assert derive(SizingConfig(capital=120.0)).risk_per_trade == 6.0


def test_changing_leverage_updates_all_three_rows():
    """改杠杆必须同时改变爆仓距离与止损上限——这是配置页实时反馈的核心。"""
    d30 = derive(SizingConfig(leverage=30.0))
    assert abs(d30.liq_distance_pct - 2.8333) < 1e-3
    assert abs(d30.max_stop_distance_pct - 1.1333) < 1e-3
    # 杠杆升高 -> R 不变，但可承受的止损被压缩
    assert d30.risk_per_trade == 5.0
    assert d30.max_stop_distance_pct < derive(CFG).max_stop_distance_pct


def test_leverage_above_ceiling_raises():
    """1/L - 维持保证金率 变成非正时，爆仓距离为零/负，任何仓位瞬间爆掉 -> 必须拒绝。

    边界是 1/0.005 = 200 倍。250 倍时 1/250 - 0.005 = -0.001 < 0。
    """
    with pytest.raises(ValueError):
        SizingConfig(leverage=250.0)


def test_leverage_at_199_is_still_valid_but_tiny_buffer():
    """199 倍仍然合法（爆仓距离仅 0.003%），但止损上限几乎为零 -> 实际不可开仓。"""
    d = derive(SizingConfig(leverage=199.0))
    assert 0 < d.liq_distance_pct < 0.01
    assert d.max_stop_distance_pct < 0.002
    assert not plan_position(SizingConfig(leverage=199.0), 0.5).ok


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        SizingConfig(capital=0)
    with pytest.raises(ValueError):
        SizingConfig(risk_pct=0)
    with pytest.raises(ValueError):
        SizingConfig(risk_pct=101)


def test_as_rows_returns_four_labelled_rows():
    rows = derive(CFG).as_rows()
    assert len(rows) == 4
    labels = [r["label"] for r in rows]
    assert labels == ["每笔风险 R", "爆仓距离", "止损距离上限", "单笔保证金上限"]
    assert rows[0]["value"] == "5 U"
    assert rows[1]["value"] == "4.045%"


# ------------------------------------------------------------------ 风险定仓
def test_plan_position_uses_risk_based_sizing():
    """止损 1.2% -> 名义 = 5/1.2% = 417U，保证金 = 417/22 = 18.9U。"""
    p = plan_position(CFG, 1.2)
    assert p.ok, p.reasons
    assert abs(p.notional - 416.67) < 0.5
    assert abs(p.margin - 18.94) < 0.1
    assert abs(p.liq_buffer - 3.37) < 0.02
    assert p.margin_pct_of_capital < 20


def test_stop_beyond_cap_is_refused():
    """止损 2% > 上限 1.618% -> 爆仓会先于止损触发，必须拒绝开仓。"""
    p = plan_position(CFG, 2.0)
    assert not p.ok
    assert any("超过上限" in r for r in p.reasons)


def test_stop_at_exactly_the_cap_passes():
    d = derive(CFG)
    p = plan_position(CFG, d.max_stop_distance_pct)
    assert p.ok, p.reasons
    assert abs(p.liq_buffer - 2.5) < 1e-6


def test_margin_cap_is_enforced():
    """高风险比例 + 紧止损 -> 保证金会冲破上限 40U。"""
    big = SizingConfig(capital=100.0, risk_pct=50.0)   # R = 50U
    p = plan_position(big, 1.0)                        # 名义 5000U -> 保证金 227U
    assert not p.ok
    assert any("保证金" in r and "上限" in r for r in p.reasons)


def test_margin_cap_never_binds_in_practical_stop_range():
    """文档 §3.1 的断言：止损 0.8-1.6% 时保证金自动落在 14-29%，40% 上限永不触发。"""
    for stop in (0.8, 1.0, 1.2, 1.5, 1.6):
        p = plan_position(CFG, stop)
        assert p.ok, (stop, p.reasons)
        assert p.margin_pct_of_capital < 40.0


def test_zero_or_negative_stop_raises():
    with pytest.raises(ValueError):
        plan_position(CFG, 0)
    with pytest.raises(ValueError):
        plan_position(CFG, -1.0)


# ------------------------------------------------------------------ 时段闸门
def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _bj(y, mo, d, h, mi=0):
    """构造一个北京时间时刻，转成 UTC 传给闸门。"""
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc) - __import__("datetime").timedelta(hours=8)


def test_default_windows_shape():
    w = default_session_windows()
    assert w[0] and w[1] and w[2] and w[3]          # 周一到周四开
    assert w[4] == [] and w[5] == []                # 周五、周六关
    assert w[6] == [(720, 1440)]                    # 周日只有 12:00 之后


def test_sunday_morning_is_closed_sunday_afternoon_is_open():
    cfg = SessionConfig()
    # 2026-11-08 是周日
    assert not in_trade_window(cfg, _bj(2026, 11, 8, 11, 59))
    assert in_trade_window(cfg, _bj(2026, 11, 8, 12, 0))
    assert in_trade_window(cfg, _bj(2026, 11, 8, 23, 59))


def test_friday_and_saturday_are_closed():
    cfg = SessionConfig()
    assert not in_trade_window(cfg, _bj(2026, 11, 6, 10, 0))    # 周五
    assert not in_trade_window(cfg, _bj(2026, 11, 7, 10, 0))    # 周六
    assert not in_trade_window(cfg, _bj(2026, 11, 6, 0, 0))
    assert not in_trade_window(cfg, _bj(2026, 11, 7, 23, 59))


def test_weekdays_are_open_all_day():
    cfg = SessionConfig()
    for day in (9, 10, 11, 12):            # 2026-11-09..12 = 周一..周四
        assert in_trade_window(cfg, _bj(2026, 11, day, 0, 0))
        assert in_trade_window(cfg, _bj(2026, 11, day, 12, 0))
        assert in_trade_window(cfg, _bj(2026, 11, day, 23, 59))


def test_disabled_session_always_allows():
    cfg = SessionConfig(enabled=False)
    assert in_trade_window(cfg, _bj(2026, 11, 7, 3, 0))         # 周六也放行


def test_naive_datetime_treated_as_utc():
    cfg = SessionConfig()
    # 周一 12:00 北京 = 周一 04:00 UTC
    assert in_trade_window(cfg, datetime(2026, 11, 9, 4, 0))


def test_session_label_mentions_sunday_noon():
    label = session_label(SessionConfig())
    assert "周日 12:00-24:00" in label
    assert "周五 关闭" in label
