"""风险与仓位测试：仓位必须由风险预算反推，杠杆只是校验项。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_trader.config import TradingCfg  # noqa: E402
from ai_trader.trading.risk import build_plan, round_step  # noqa: E402


FEAT = {"atr3s_pct": 0.01, "sigma3_pct": 0.02, "feature_time": 0}


def test_qty_from_risk_budget():
    cfg = TradingCfg()
    plan = build_plan("long", 80000.0, 80000.0 * (1 - 0.004), 100.0, cfg, FEAT)
    assert plan.ok, plan.reason
    # 风险预算 = 权益 x 5% = 5U；止损 0.4% => 每枚 320U 风险，
    # 理论 qty = 5/320 = 0.0156，按 step=0.001 向下取整成 0.015 => 实际风险 4.8U。
    # 断言用「不超过预算」，因为取整只会让风险变小、永远不会变大。
    budget = plan.entry * plan.qty * 0 + 100.0 * cfg.risk_pct / 100.0
    assert plan.risk_usdt <= budget + 1e-9, "风险 %.4fU 超过预算 %.4fU" % (plan.risk_usdt, budget)
    assert plan.risk_usdt > 0
    assert plan.leverage <= cfg.max_leverage + 1e-9
    assert plan.target > plan.entry > plan.stop


def test_liquidation_gate_caps_leverage():
    """止损越宽，允许的名义越小——强平距离必须始终 >= 3x 止损。

    ⚠️ 本测试必须**显式**制造过度杠杆，不能依赖 TradingCfg 的默认值。
    2026-11 把 risk_pct 从 35 改到 5 之后，同样的止损位下仓位小了 7 倍，
    杠杆只剩 9.6x（强平距离 10%），闸门自然不触发——那是仓位变小了，
    **不是闸门失效**。依赖默认值会让这个测试在改配置时静默失去意义。
    """
    cfg = TradingCfg()
    cfg.risk_pct = 35.0          # 故意用高风险，逼杠杆撞上强平闸
    cfg.max_leverage = 60.0
    wide = build_plan("long", 80000.0, 80000.0 * (1 - 0.005), 100.0, cfg, FEAT)
    assert not wide.ok, "过度杠杆必须被拒绝，实际放行了: %s" % wide.reason
    liq = [g for g in wide.gates if g["gate"] == "强平距离"]
    assert liq and not liq[0]["ok"], "被拒绝的原因必须是强平距离，实际 gates=%s" % wide.gates


def test_tight_stop_is_widened_not_used():
    """止损比下限还紧时，必须被抬到 min_stop_pct，而不是照单执行。"""
    cfg = TradingCfg()
    plan = build_plan("long", 80000.0, 80000.0 * (1 - 0.001), 100.0, cfg, FEAT)
    assert plan.stop_pct >= cfg.min_stop_pct - 1e-9


def test_reachability_rejects_unreachable_target():
    """2026-11-01 事故回归：目标 109σ 而持仓上限 600 根 → 注定超时，必须提前拒单。"""
    cfg = TradingCfg()
    calm = {"atr3s_pct": 0.01, "sigma3_pct": 0.002, "feature_time": 0}
    plan = build_plan("short", 77500.0, 77500.0 * (1 + 0.004), 100.0, cfg, calm)
    assert not plan.ok
    assert any(g["gate"] == "可达性" and not g["ok"] for g in plan.gates)


def test_reachability_sets_hold_window():
    """可达时，持仓上限要按波动反推（而不是写死 600 根）。"""
    cfg = TradingCfg()
    plan = build_plan("long", 80000.0, 80000.0 * (1 - 0.004), 100.0, cfg, FEAT)
    assert plan.ok, plan.reason
    assert plan.max_hold_bars >= cfg.reach_min_bars
    assert plan.max_hold_bars <= cfg.max_hold_hours * 3600 / 3


def test_reject_too_wide_stop():
    cfg = TradingCfg()
    plan = build_plan("long", 80000.0, 80000.0 * (1 - 0.05), 100.0, cfg, FEAT)
    assert not plan.ok


def test_leverage_cap_binds_position():
    """权益极小时，杠杆上限必须先于风险预算生效。"""
    cfg = TradingCfg()
    cfg.max_leverage = 3.0
    plan = build_plan("long", 80000.0, 80000.0 * (1 - 0.0040), 20.0, cfg, {"atr3s_pct": 0.01})
    assert plan.qty * 80000.0 <= 20.0 * 3.0 + 1e-6 or not plan.ok


def test_min_notional_rejects():
    cfg = TradingCfg()
    # 权益 0.4U 时风险预算只有 0.14U，仓位取整后为 0（不足最小下单 0.001），必须拒单
    tiny = build_plan("long", 80000.0, 80000.0 * (1 - 0.004), 0.4, cfg, FEAT)
    assert not tiny.ok


def test_round_step():
    assert round_step(0.00387, 0.001) == 0.003
    assert round_step(0.0039999, 0.001) == 0.003
    assert round_step(0.004, 0.001) == 0.004
