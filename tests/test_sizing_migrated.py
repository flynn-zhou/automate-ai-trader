# -*- coding: utf-8 -*-
"""仓位派生计算与开仓时段闸门（《下单与守卫_运行规则.md》§3 / §4 / §6）。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_trader.trading.sizing import (
    SessionConfig,
    SizingConfig,
    MAX_RISK_PER_SIGNAL_PER_100U,
    MIN_QTY_PER_100U,
    MIN_QTY_SINGLE_PER_100U,
    default_session_windows,
    derive,
    in_trade_window,
    max_risk_usd,
    min_order_qty,
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


# ================================================================ 手数兜底
# 用户 2026-11-26 口径：「每次挂单至少 0.01(改后)，也就是一次开单分三次至少
# 0.03，不分三次至少 0.03，这是 100U 总保证金的情况下，后面变成 200U 的时候
# 跟随倍数变化」+ 「我是能接受一次开仓信号最多损失 20U 的」。
#
# 这一组测试守两件事：
#   1. 兜底确实按本金比例缩放（改本金就跟着改，不用改代码）
#   2. 兜底**不能顶穿红线** —— 风险上限优先于兜底
# 第 2 条是这次改动里最容易做错的地方：兜底让手数变大 = 真亏更多，
# 如果不设上限，三引线 0.03 手会亏 28.3U，超过用户说的 20U。


def test_兜底按本金比例缩放():
    """100U -> 0.01/0.03；200U -> 0.02/0.06。『跟随倍数变化』就是这个。"""
    assert min_order_qty(100.0, 3) == pytest.approx(MIN_QTY_PER_100U)
    assert min_order_qty(100.0, 1) == pytest.approx(MIN_QTY_SINGLE_PER_100U)
    assert min_order_qty(200.0, 3) == pytest.approx(MIN_QTY_PER_100U * 2)
    assert min_order_qty(200.0, 1) == pytest.approx(MIN_QTY_SINGLE_PER_100U * 2)
    assert min_order_qty(50.0, 3) == pytest.approx(MIN_QTY_PER_100U * 0.5)


def test_兜底_三档合计等于单笔():
    """用户最终口径：分三次 0.03 = 不分三次 0.03。

    ★ 这两个数是**两条独立规则**碰到一起的巧合，不是 `3 * 0.01` 推出来的。
      所以这里断言的是「碰巧相等」这个事实；一旦有人把单笔兜底调成 0.02，
      这个断言会红，提醒他「用户口径变了，去确认」。
    """
    assert 3 * min_order_qty(100.0, 3) == pytest.approx(min_order_qty(100.0, 1))


def test_本金非正时兜底为零():
    assert min_order_qty(0.0, 3) == 0.0
    assert min_order_qty(-5.0, 3) == 0.0
    assert max_risk_usd(0.0) == 0.0


def test_风险红线按本金比例缩放():
    assert max_risk_usd(100.0) == pytest.approx(MAX_RISK_PER_SIGNAL_PER_100U)
    assert max_risk_usd(200.0) == pytest.approx(MAX_RISK_PER_SIGNAL_PER_100U * 2)


def test_不给价格时手数为零且行为与改前一致():
    """老调用方（只传止损距离）必须拿不到 qty，但 ok 判定不变。"""
    cfg = SizingConfig(capital=100.0, risk_pct=5.0, leverage=22.0,
                       margin_cap_pct=100.0)
    pp = plan_position(cfg, 1.0)
    assert pp.qty == 0.0
    assert pp.floor_applied is False
    assert pp.ok is True


def test_兜底会把小手数顶上来():
    """三引线实况：风险定仓给 0.0084，兜底 0.03，但被红线压到 0.021。"""
    cfg = SizingConfig(capital=100.0, risk_pct=8.0, leverage=22.0,
                       margin_cap_pct=100.0)
    # 不给红线：兜底原样生效
    raw = plan_position(cfg, 1.547, price=60880.8, qty_step=0.001,
                        min_qty=min_order_qty(100.0, 1))
    assert raw.qty == pytest.approx(0.030)
    assert raw.floor_applied is True
    assert raw.floor_capped is False
    # 给了红线：兜底被压下来，实际风险不超 20U
    capped = plan_position(cfg, 1.547, price=60880.8, qty_step=0.001,
                           min_qty=min_order_qty(100.0, 1),
                           max_risk=max_risk_usd(100.0))
    assert capped.qty == pytest.approx(0.021)
    assert capped.floor_capped is True
    assert capped.risk_at_floor <= max_risk_usd(100.0)


def test_兜底被红线压住时手数仍然大于纯风险定仓():
    """压下来的兜底不能压到比风险定仓还小 —— 那就成了「反向兜底」。"""
    cfg = SizingConfig(capital=100.0, risk_pct=8.0, leverage=22.0,
                       margin_cap_pct=100.0)
    pure = plan_position(cfg, 1.547, price=60880.8, qty_step=0.001)
    capped = plan_position(cfg, 1.547, price=60880.8, qty_step=0.001,
                           min_qty=min_order_qty(100.0, 1),
                           max_risk=max_risk_usd(100.0))
    assert capped.qty >= pure.qty


def test_风险定仓已够大时兜底不生效():
    """反包第 3 档实况：风险定仓 0.014 > 兜底 0.01，不该被兜底改小。"""
    cfg = SizingConfig(capital=100.0, risk_pct=8.0 / 3.0, leverage=22.0,
                       margin_cap_pct=100.0)
    pp = plan_position(cfg, 0.2263, price=60577.0, qty_step=0.001,
                       min_qty=min_order_qty(100.0, 3),
                       max_risk=max_risk_usd(100.0) / 3.0)
    assert pp.floor_applied is False
    assert pp.qty == pytest.approx(0.014)


def test_兜底不会把保证金上限顶掉():
    """兜底是体验要求，保证金是物理约束 —— 物理约束必须赢。

    ★ 构造要点：兜底**本身随本金缩放**，所以在 22x / 60000 下它只占本金
      约 82%（`0.0003 x 60000 / 22`），怎么调都顶不穿上限 100%。
      要真正压出冲突，必须让「同样的手数」需要更多保证金 —— 抬高价格。

    构造：本金 50U、价格 100000、止损 1%（风险定仓只给 0.002 手）、
    兜底 0.015 手 -> 保证金 0.015 x 100000 / 22 = 68.2U > 上限 50U。
    预期：拒单并写明「保证金」，而不是让兜底把上限顶掉。
    """
    cfg = SizingConfig(capital=50.0, risk_pct=5.0, leverage=22.0,
                       margin_cap_pct=100.0)
    pp = plan_position(cfg, 1.0, price=100000.0, qty_step=0.001,
                       min_qty=min_order_qty(50.0, 1))
    assert pp.floor_applied is True
    assert pp.margin > derive(cfg).margin_cap_amount
    assert pp.ok is False
    assert any("保证金" in r for r in pp.reasons)


def test_兜底的risk_at_floor是真亏的钱():
    """`risk_at_floor` 必须等于 手数 x 价 x 止损%，不是预算里的那个 R。"""
    cfg = SizingConfig(capital=100.0, risk_pct=8.0, leverage=22.0,
                       margin_cap_pct=100.0)
    pp = plan_position(cfg, 1.547, price=60880.8, qty_step=0.001,
                       min_qty=min_order_qty(100.0, 1),
                       max_risk=max_risk_usd(100.0))
    expect = pp.qty * 60880.8 * 0.01547
    assert pp.risk_at_floor == pytest.approx(expect, rel=1e-9)
    # 而且它确实比风险预算(8U)大 —— 这正是需要打 WARNING 的那个差
    assert pp.risk_at_floor > derive(cfg).risk_per_trade


def test_兜底向上取整到步长():
    """兜底落不到网格上时必须**向上**取 —— 往下取就失去兜底意义。"""
    cfg = SizingConfig(capital=100.0, risk_pct=8.0, leverage=22.0,
                       margin_cap_pct=100.0)
    # 兜底 0.01，步长 0.004 -> 向上到 0.012（不是 0.008）
    pp = plan_position(cfg, 1.547, price=60880.8, qty_step=0.004,
                       min_qty=0.01)
    assert pp.qty == pytest.approx(0.012)
    assert pp.floor_applied is True


def test_三档各十分之一本金时的红线也不超():
    """三档必须把红线按档摊，否则三档各 20U 就是 60U。"""
    per = max_risk_usd(100.0) / 3.0
    assert per == pytest.approx(20.0 / 3.0)
    cfg = SizingConfig(capital=100.0, risk_pct=8.0 / 3.0, leverage=22.0,
                       margin_cap_pct=100.0)
    pp = plan_position(cfg, 0.2263, price=60577.0, qty_step=0.001,
                       min_qty=min_order_qty(100.0, 3), max_risk=per)
    assert pp.risk_at_floor <= per + 1e-9


def test_红线是绝对上限_风险定仓也不许越():
    """红线不能只压兜底。risk_pct 调到 30U 时，红线必须赢。

    ★ 为什么必须有这条：如果红线只管兜底，那它就是「只在兜底时生效的
      装饰品」—— 用户把 risk_pct 写成 30，照样亏 30U，而
      「一次开仓信号最多损失 20U」变成了假承诺。
    """
    cfg = SizingConfig(capital=100.0, risk_pct=30.0, leverage=22.0,
                       margin_cap_pct=100.0)
    pp = plan_position(cfg, 1.547, price=60880.8, qty_step=0.001,
                       min_qty=0.0, max_risk=max_risk_usd(100.0))
    assert pp.risk_at_floor <= max_risk_usd(100.0)
    # 30U 预算下风险定仓本来要 0.031 手，被红线压到 0.021
    pure = plan_position(cfg, 1.547, price=60880.8, qty_step=0.001)
    assert pure.qty > pp.qty


