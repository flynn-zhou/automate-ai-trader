# -*- coding: utf-8 -*-
"""平仓记账口径统一 + 止损必须在爆仓价之内（2026-11-24 用户两条要求）。

背景（生产实测，22 倍杠杆，0.032 BTC @ 60880，权益 114U）：

   逐仓爆仓价 58356   → 距入场 4.15%
   旧止损     45660   → 距入场 25.00%（`trend_stop_pct` 被当成价格跌幅）
   止损比爆仓价还低 12696 点 = 0.17 倍爆仓距离
   ⇒ 价格永远先到 58356 爆仓，45660 一次都不可能触发

用户的原话：
  1.「统一口径！」
  2.「25% 的熔断可能是本金保证金损失的 25%，不是目标标的的涨跌幅！」

第 2 条查证结论：歧义来自 `trend_leverage = 1.0` 的历史设计 —— 1 倍杠杆下
「价格跌 25%」与「保证金亏 25%」是同一个数，所以从未暴露；上 22 倍才分家。
正确口径是 `爆仓距离 / 安全倍数`（`sizing.derive`，生产值 1.618%），
它天然满足「止损永远先于爆仓」。
"""
from __future__ import annotations

import types

import pytest

from ai_trader.trading import engine as E


# --------------------------------------------------------------- 测试脚手架
def _engine(**over):
    """造一个只带必要属性的引擎，绕开 __init__ 的 IO。"""
    eng = object.__new__(E.DecisionEngine)
    eng.tcfg = types.SimpleNamespace(
        symbol="BTCUSDT", trend_stop_pct=25.0, strategy="pending",
        is_spot=False, taker_fee_for=lambda is_spot: 0.0005,
        max_hold_bars=200, account_mmr=0.004)
    eng.db = types.SimpleNamespace(
        risks=[], log_risk=lambda k, m: eng.db.risks.append((k, m)))
    eng._sizing_params = lambda: {
        "sizing.capital": 100.0, "sizing.risk_pct": 5.0,
        "sizing.leverage": 22.0, "sizing.margin_cap_pct": 40.0}
    for k, v in over.items():
        setattr(eng, k, v)
    return eng


# ------------------------------------------------------- 1. 手续费口径统一
def test_close_booking_counts_both_sides_and_abs():
    """手续费 = |开仓费| + |平仓费|；净盈亏 = 毛 - 合计。

    ★ 三条平仓路径以前各写一套，同一笔仓算出三个 pnl_net：
        _close          开+平   ← 正确
        _adopted_close  只平    ← 漏开仓费
        _pending_close  0.0     ← 两边都漏
      漏记成本的方向是单向的：永远让亏损看起来更小、盈利看起来更大。
    """
    pos = {"entry": 60880.0, "fee": 0.98}   # 开仓已付 0.98U
    gross, fee, net = E.close_booking(pos, 61289.4, 1.20, 0.032, "long")
    assert gross == pytest.approx((61289.4 - 60880.0) * 0.032, rel=1e-12)
    assert fee == pytest.approx(0.98 + 1.20)
    assert net == pytest.approx(gross - (0.98 + 1.20))
    # 净盈亏必须严格小于毛盈亏（有成本就不可能相等）
    assert net < gross


def test_close_booking_short_sign_is_negative_when_price_rises():
    """空头：价格涨 → 毛亏损。方向不能反。"""
    pos = {"entry": 60000.0, "fee": 0.5}
    gross, fee, net = E.close_booking(pos, 60600.0, 0.5, 0.01, "short")
    assert gross == pytest.approx(-6.0)
    assert net == pytest.approx(-7.0)


def test_close_booking_accepts_buy_sell_spellings():
    """`side` 的三种写法必须同解 —— 这个项目里 buy/sell 与 long/short 混用。"""
    pos = {"entry": 100.0, "fee": 0.0}
    g_long, _, _ = E.close_booking(pos, 110.0, 0.0, 1.0, "long")
    g_buy, _, _ = E.close_booking(pos, 110.0, 0.0, 1.0, "buy")
    g_BUY, _, _ = E.close_booking(pos, 110.0, 0.0, 1.0, "BUY")
    assert g_long == g_buy == g_BUY == pytest.approx(10.0)


def test_close_booking_missing_open_fee_is_zero_not_crash():
    """持仓 dict 没有 fee 键（旧数据）时按 0 算，不能抛。"""
    gross, fee, net = E.close_booking({"entry": 100.0}, 101.0, 0.3, 2.0, "long")
    assert fee == pytest.approx(0.3)
    assert net == pytest.approx(gross - 0.3)


# ------------------------------------------- 2. 止损距离：爆仓距离 / 安全倍数
def test_stop_distance_is_liq_distance_over_safety_multiple():
    """22x + 维持保证金 0.5% + 安全倍数 2.5 -> 止损距离 1.618%。"""
    eng = _engine()
    pct = eng._stop_distance_pct(60880.0)
    assert pct == pytest.approx((1.0 / 22.0 - 0.005) * 100.0 / 2.5, rel=1e-9)
    assert 1.6 < pct < 1.65


def test_stop_distance_is_always_inside_liquidation():
    """核心不变量：止损距离 < 爆仓距离（否则止损永远不触发）。

    这条断言就是 `position_guard.py:440` 那条不变量 —— 它早就写好了，
    但引擎从来没接过（check_invariants 零调用）。
    """
    eng = _engine()
    for lev in (3.0, 5.0, 10.0, 22.0, 50.0):
        eng._sizing_params = lambda lev=lev: {
            "sizing.capital": 100.0, "sizing.risk_pct": 5.0,
            "sizing.leverage": lev, "sizing.margin_cap_pct": 40.0}
        stop_pct = eng._stop_distance_pct(60000.0)
        liq_pct = eng._liq_distance_pct()
        assert stop_pct > 0, lev
        assert stop_pct < liq_pct, (lev, stop_pct, liq_pct)


def test_old_trend_stop_pct_is_unreachable_at_22x():
    """回归铁证：旧的 25% 价格止损在 22 倍下必然晚于爆仓。

    这条测试的作用是**把缺陷钉住**：一旦有人把 adopted 路径改回
    `trend_stop_pct`，上面那条不变量会立刻失败。
    """
    eng = _engine()
    entry = 60880.0
    liq_pct = eng._liq_distance_pct()
    old_stop = entry * (1 - 25.0 / 100.0)
    old_pct = (entry - old_stop) / entry * 100.0
    assert old_pct > liq_pct * 4      # 25% 是 4.15% 的 6 倍
    assert eng._warn_if_stop_unreachable("long", entry, old_stop) is True
    kinds = [k for k, _ in eng.db.risks]
    assert "stop_beyond_liquidation" in kinds


def test_safe_stop_does_not_warn():
    """按爆仓距离推导出来的止损不该告警（否则告警会变成噪声）。"""
    eng = _engine()
    entry = 60880.0
    pct = eng._stop_distance_pct(entry)
    stop = entry * (1 - pct / 100.0)
    assert eng._warn_if_stop_unreachable("long", entry, stop) is False
    assert not eng.db.risks


def test_fallback_stop_adopted_uses_liq_distance_but_system_keeps_trend_pct():
    """缺止损时两种仓不能用同一个口径。

    人工仓没有策略依据 -> 爆仓距离推导（可触发）；
    策略仓的 25% 是**已回测的策略参数**，这一轮无权悄悄改掉。
    """
    eng = _engine()
    eng._is_donchian = lambda: False
    entry = 60880.0
    a = eng._fallback_stop("long", entry, adopted=True)
    s = eng._fallback_stop("long", entry, adopted=False)
    expect_adopted = entry * (1 - eng._stop_distance_pct(entry) / 100.0)
    assert a == pytest.approx(expect_adopted)
    assert s == pytest.approx(entry * 0.75)
    assert a > s                            # 人工仓的保护位更近（能触发）
    kinds = [k for k, _ in eng.db.risks]
    # 策略仓那条 25% 会被标记为不可触发，人工仓那条不会
    assert kinds.count("stop_beyond_liquidation") == 1
    assert "stop_missing" in kinds


def test_fallback_stop_zero_entry_returns_zero():
    eng = _engine()
    assert eng._fallback_stop("long", 0.0, adopted=True) == 0.0
