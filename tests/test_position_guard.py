# -*- coding: utf-8 -*-
"""守卫状态机（《下单与守卫_运行规则.md》§7 / §8 / §10）。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from ai_trader.trading.position_guard import (
    ADD, EXIT_STOP, EXIT_STRUCTURE, HOLD, MOVE_STOP, PARTIAL_TP, REDUCE,
    ST_CLOSING, ST_FLAT,
    GuardConfig, GuardPosition, apply_decision, check_invariants, decide,
)

CFG = GuardConfig()          # R=5U, max_adds=1, safety=2.5, target_1_R=2


class FakeKline:
    """KlineRead 的最小替身：守卫只用这几个字段。"""

    def __init__(self, intent="none", panic_flush=False, volume_regime="常态",
                 low=None, atr=0.0, evidence=()):
        self.intent = intent
        self.panic_flush = panic_flush
        self.volume_regime = volume_regime
        self.low = low
        self.atr = atr
        self.evidence = tuple(evidence)


def long_pos(stop=98.8, liq=95.955, avg=100.0, qty=None):
    """做多基准仓：止损 1.2%，爆仓 4.045%，风险恰好 5U。"""
    qty = qty if qty is not None else 5.0 / abs(avg - stop)
    return GuardPosition(symbol="BTCUSDT", direction="long", avg_entry=avg,
                         qty=qty, stop_price=stop, liq_price=liq,
                         initial_risk=5.0)


# ------------------------------------------------------------ 基础几何
def test_structural_buffer_matches_document():
    p = long_pos()
    assert abs(p.structural_buffer() - 3.3708) < 1e-3
    assert abs(p.liq_buffer(100.0) - 3.3708) < 1e-3
    assert abs(p.risk_at_stop() - 5.0) < 1e-9


def test_liq_buffer_degrades_as_price_approaches_stop():
    """口径自证：价越靠近爆仓，缓冲必须**单调变小**（旧口径会虚高）。"""
    p = long_pos()
    vals = [p.liq_buffer(px) for px in (100.0, 99.5, 99.0, 98.8, 97.0)]
    assert all(vals[i] > vals[i + 1] for i in range(len(vals) - 1)), vals


def test_profit_R_is_in_units_of_trade_risk():
    p = long_pos()
    # 涨 1.2 元 = 1R（因为止损距离就是 1.2）
    assert abs(p.profit_R(101.2) - 1.0) < 1e-9
    assert abs(p.profit_R(102.4) - 2.0) < 1e-9


# ------------------------------------------------------------ P0 保命
def test_p0_reduces_when_structural_buffer_squeezed():
    """止损 1.2% 但爆仓只有 2.0% -> 结构性缓冲 1.67 倍 < 2.5，必须减仓 33.3%。"""
    p = long_pos(stop=98.8, liq=98.0)
    d = decide(p, 100.0, CFG)
    assert d.action == REDUCE, d
    assert abs(d.close_qty / p.qty - 0.3333) < 1e-3


def test_p0_reduce_actually_restores_buffer():
    p = long_pos(stop=98.8, liq=98.0)
    d = decide(p, 100.0, CFG)
    after = apply_decision(p, d)
    assert after.structural_buffer() >= 2.5 - 1e-9
    assert after.qty < p.qty


def test_p0_emergency_exit_when_liquidation_is_imminent():
    """距爆仓不到半个止损宽度 -> 减仓也来不及，直接离场。"""
    p = long_pos()
    d = decide(p, 96.0, CFG)      # (96-95.955)/1.2 = 0.04 个止损宽度
    assert d.action == EXIT_STOP
    assert "P0 紧急" in d.reason


def test_p0_cannot_be_vetoed_by_kline():
    """恐慌插针只对 P1 生效；P0 面前 K 线没有发言权。"""
    p = long_pos(stop=98.8, liq=98.0)
    k = FakeKline(panic_flush=True, low=97.0, atr=1.0)
    d = decide(p, 100.0, CFG, kline=k)
    assert d.action == REDUCE


# ------------------------------------------------------------ P1 止损
def test_p1_exits_when_price_crosses_stop():
    p = long_pos()
    d = decide(p, 98.5, CFG)
    assert d.action == EXIT_STOP
    assert "越过止损" in d.reason
    # 价格已滑过止损 0.3，审计必须写实际亏损 -1.25R 而不是计划 -1R
    assert "实际实现 -1.25R" in d.reason


def test_p1_reason_reports_actual_not_planned_loss():
    """恰好在止损处 -> 实际亏损就是 -1R。"""
    p = long_pos()
    d = decide(p, 98.8, CFG)
    assert d.action == EXIT_STOP
    assert "实际实现 -1.00R" in d.reason


def test_p1_delays_once_on_panic_flush():
    """长下影+带量+收回上半部，且距爆仓够远 -> 允许放宽止损一次。"""
    p = long_pos(stop=99.0, liq=95.955, qty=5.0)   # d_stop=1.0
    k = FakeKline(panic_flush=True, low=98.5, atr=1.0,
                  evidence=("lower_wick_ratio=0.62", "vol_z=2.1"))
    d = decide(p, 98.99, CFG, kline=k)
    assert d.action == MOVE_STOP
    assert "恐慌插针" in d.reason
    # K 线低点给的是 98.3，但会被不变量钳制到 100 - 4.045/2.5 = 98.382
    assert abs(d.new_stop - 98.382) < 1e-6
    assert d.new_stop < p.stop_price          # 确实放宽了（对做多=更低）
    assert d.evidence == ("lower_wick_ratio=0.62", "vol_z=2.1")
    # 放宽后结构性缓冲不得跌破 2.5 倍——这是它不被下一轮 P0 反咬的原因
    assert replace(p, stop_price=d.new_stop).structural_buffer() >= 2.5 - 1e-9


def test_p1_delay_only_once_in_its_lifetime():
    p = long_pos(stop=99.0, liq=95.955, qty=5.0)
    k = FakeKline(panic_flush=True, low=98.5, atr=1.0)
    d1 = decide(p, 98.99, CFG, kline=k)
    assert d1.action == MOVE_STOP
    p2 = apply_decision(p, d1)
    assert p2.stop_delay_used is True
    # 放宽后止损被钳到 98.382，价格 98.99 仍在止损之上 -> 仓位依然有效
    assert decide(p2, 98.99, CFG, kline=k).action == HOLD
    # 价格继续跌破新止损 -> 必须离场，且不得再修正
    d2 = decide(p2, 98.3, CFG, kline=k)
    assert d2.action == EXIT_STOP, "第二次触发止损不得再修正"


def test_p1_no_delay_when_liquidation_too_close():
    """恐慌插针也不行：距爆仓不足 3 个止损宽度时必须老老实实止损。"""
    p = long_pos(stop=99.0, liq=95.955, qty=5.0)
    k = FakeKline(panic_flush=True, low=98.5, atr=1.0)
    d = decide(p, 98.5, CFG, kline=k)      # buffer = 2.545 < 3
    assert d.action == EXIT_STOP


def test_p1_no_delay_without_panic_flush():
    p = long_pos(stop=99.0, liq=95.955, qty=5.0)
    k = FakeKline(panic_flush=False, low=98.5, atr=1.0)
    assert decide(p, 98.99, CFG, kline=k).action == EXIT_STOP


# ------------------------------------------------------------ P2 结构离场
def test_p2_exhaustion_exits_immediately():
    p = long_pos()
    k = FakeKline(intent="exhaustion")
    d = decide(p, 100.5, CFG, kline=k)
    assert d.action == EXIT_STRUCTURE


def test_p2_breakout_holds_instead_of_taking_target():
    """带量突破时不因到目标而平仓——这是「别死盯 R」的落点。"""
    p = long_pos()
    k = FakeKline(intent="breakout", volume_regime="放量")
    d = decide(p, 101.5, CFG, kline=k)     # 浮盈 1.25R
    assert d.action == HOLD
    assert "突破" in d.reason


def test_p2_distribution_takes_half_at_1R():
    p = long_pos()
    k = FakeKline(intent="distribution")
    d = decide(p, 101.5, CFG, kline=k)
    assert d.action == PARTIAL_TP
    assert abs(d.close_qty - p.qty / 2) < 1e-9
    assert abs(d.new_stop - 100.05) < 1e-6      # 抬到成本+手续费


# ------------------------------------------------------------ P3 分批止盈
def test_p3_partial_tp_at_target_1():
    p = long_pos()
    d = decide(p, 102.4, CFG)                  # 恰好 2R
    assert d.action == PARTIAL_TP
    assert abs(d.close_qty - p.qty / 2) < 1e-9
    # 减半后 qty=2.0833，+1R 的价格距离 = 5/2.0833 = 2.4（不是 1.2）
    assert abs(d.new_stop - 102.4) < 1e-6
    assert abs(d.new_target - 107.2) < 1e-6


def test_p3_levels_actually_carry_the_labelled_R():
    """缺陷 A 的回归测试：标注几 R，该价位就必须真值几 R。"""
    p = long_pos()
    d = decide(p, 102.4, CFG)
    after = apply_decision(p, d)
    assert abs(after.profit_R(d.new_stop) - 1.0) < 1e-9, "止损位应为 +1R"
    assert abs(after.profit_R(d.new_target) - 3.0) < 1e-9, "目标位应为 +3R"
    # 反面对照：旧的错误算法给出 0.5R / 1.5R
    assert abs(after.profit_R(101.2) - 0.5) < 1e-9


def test_partial_tp_records_realized_pnl_at_real_price():
    """缺陷 B 的回归测试：已实现盈亏必须用真实成交价，且只算平掉的那部分。"""
    p = long_pos()
    d = decide(p, 102.4, CFG)
    after = apply_decision(p, d)
    expected = (102.4 - 100.0) * (p.qty / 2)
    assert abs(after.realized_pnl - expected) < 1e-9
    assert after.realized_pnl > 0, "分批止盈的盈利不得为 0"


def test_partial_tp_scales_liquidation_price_with_remaining_qty():
    """缺陷 C 的回归测试：减半后爆仓价应按 1/qty 外移。"""
    p = long_pos()
    d = decide(p, 102.4, CFG)
    after = apply_decision(p, d)
    assert after.liq_price < p.liq_price          # 做多：爆仓价更远（更低）
    assert abs(after.d_liq() - p.d_liq() * 2.0) < 1e-6
    # 止损已抬到盈利侧，亏损风险归零 -> 结构性缓冲为 inf（协议约定的"无穷"）
    assert after.structural_buffer() == float("inf")


def test_profit_locked_position_is_not_flagged_as_invalid():
    """缺陷 D 的回归测试：止损抬到盈利侧是正常锁仓，不能被判成"错误一侧"。"""
    p = long_pos()
    after = apply_decision(p, decide(p, 102.4, CFG))
    assert after.stop_price > after.avg_entry      # 做多止损在成本之上
    assert check_invariants(after, CFG) == []


def test_p3_respects_max_partials():
    """分批次数受 max_partials 约束，不得对同一仓位连续密集切半。"""
    p = replace(long_pos(), partials_done=CFG.max_partials)
    d = decide(p, 102.4, CFG)
    assert d.action != PARTIAL_TP


def test_p2_distribution_respects_max_partials():
    p = replace(long_pos(), partials_done=CFG.max_partials)
    k = FakeKline(intent="distribution")
    assert decide(p, 101.5, CFG, kline=k).action != PARTIAL_TP


def test_repeated_partials_are_capped_by_config():
    """连跑多轮：模拟价格一路上涨，分批次数不得超过上限。"""
    p = long_pos()
    p = replace(p, target_price=None)
    for _ in range(6):
        d = decide(p, 120.0, CFG)
        if d.action != PARTIAL_TP:
            break
        p = apply_decision(p, d)
        p = replace(p, target_price=None)      # 强制每轮都看 profit_R
    assert p.partials_done <= CFG.max_partials


def test_p3_does_not_fire_below_target():
    p = long_pos()
    assert decide(p, 101.0, CFG).action != PARTIAL_TP


# ------------------------------------------------------------ 棘轮
def test_ratchet_moves_stop_to_breakeven_at_1R():
    p = long_pos()
    d = decide(p, 101.2, CFG)
    assert d.action == MOVE_STOP
    assert abs(d.new_stop - 100.05) < 1e-6     # 含双边手续费
    assert d.new_stop > p.stop_price


def test_ratchet_never_loosens_stop():
    """止损已在更高位时，棘轮不得把它拉回来。"""
    p = replace(long_pos(), stop_price=101.0)
    d = decide(p, 101.2, CFG)
    if d.action == MOVE_STOP:
        assert d.new_stop >= p.stop_price


# ------------------------------------------------------------ P4 加仓
def test_p4_adds_on_profit_with_volume_continuation():
    p = long_pos()
    k = FakeKline(intent="continuation", volume_regime="放量")
    d = decide(p, 100.7, CFG, kline=k)         # 浮盈 0.583R（低于 1R 棘轮线）
    assert d.action == ADD, d
    assert d.add_qty > 0
    assert d.new_stop > p.stop_price
    # 加仓后总风险必须仍 <= R
    assert check_invariants(p, CFG, decision=d, price=100.7) == []


def test_p4_blocked_when_max_adds_is_zero():
    cfg = replace(CFG, max_adds=0)
    k = FakeKline(intent="continuation", volume_regime="放量")
    assert decide(long_pos(), 100.7, cfg, kline=k).action == HOLD


def test_p4_blocked_on_low_volume():
    k = FakeKline(intent="continuation", volume_regime="缩量")
    assert decide(long_pos(), 100.7, CFG, kline=k).action == HOLD


def test_p4_blocked_when_adds_exhausted():
    p = replace(long_pos(), adds_done=1)
    k = FakeKline(intent="continuation", volume_regime="放量")
    assert decide(p, 100.7, CFG, kline=k).action == HOLD


def test_p4_blocked_outside_trade_window():
    cfg = replace(CFG, allow_new_risk=False)
    k = FakeKline(intent="continuation", volume_regime="放量")
    assert decide(long_pos(), 100.7, cfg, kline=k).action == HOLD


def test_p4_never_adds_to_losing_position():
    """已被套（浮亏）时绝不能加仓——这是与用户对齐的核心红线。"""
    k = FakeKline(intent="continuation", volume_regime="放量")
    for px in (99.9, 99.5, 99.0):
        assert decide(long_pos(), px, CFG, kline=k).action != ADD


# ------------------------------------------------------------ 做空镜像
def test_short_mirrors_long():
    p = GuardPosition(symbol="BTCUSDT", direction="short", avg_entry=100.0,
                      qty=5.0 / 1.2, stop_price=101.2, liq_price=104.045,
                      initial_risk=5.0)
    assert abs(p.structural_buffer() - 3.3708) < 1e-3
    assert abs(p.profit_R(98.8) - 1.0) < 1e-9
    # 止损在 101.2 之上，涨破即离场
    assert decide(p, 101.3, CFG).action == EXIT_STOP
    # 1R 棘轮把止损压到成本下方
    d = decide(p, 98.8, CFG)
    assert d.action == MOVE_STOP
    assert d.new_stop < 100.0


# ------------------------------------------------------------ 状态与不变量
def test_exit_zeroes_position_so_pnl_is_not_double_counted():
    """缺陷 E 的回归测试：离场后 qty 必须归零，否则已实现与浮动会被重复汇总。"""
    p = long_pos()
    d = decide(p, 102.0, CFG)                  # 2R 不到 -> 走棘轮
    assert d.action == MOVE_STOP
    p2 = apply_decision(p, d)
    d2 = decide(p2, 97.0, CFG)                 # 跌破止损（止损已被抬高）
    assert d2.action == EXIT_STOP
    p3 = apply_decision(p2, d2)
    assert p3.qty == 0.0, "离场后持仓必须归零"
    assert p3.state == ST_CLOSING
    # 归零后浮动盈亏为 0，总盈亏 = 已实现，不再重复计算
    assert p3.profit_u(97.0) == 0.0
    assert p3.realized_pnl + p3.profit_u(97.0) == p3.realized_pnl


def test_flat_and_closing_states_are_inert():
    for st in (ST_FLAT, ST_CLOSING):
        p = replace(long_pos(), state=st)
        assert decide(p, 90.0, CFG).action == HOLD


def test_invalid_price_is_ignored():
    assert decide(long_pos(), 0, CFG).action == HOLD


def test_invariants_flag_thin_liquidation_buffer():
    p = long_pos(stop=98.8, liq=98.0)          # 缓冲 1.67 倍 < 2.5
    bad = check_invariants(p, CFG)
    assert any("爆仓距离" in b for b in bad)


def test_invariants_flag_stop_beyond_liquidation():
    """做多止损 95.0 落在爆仓价 95.955 之下 -> 爆仓会比止损先到，必须报错。"""
    p = replace(long_pos(), stop_price=95.0)
    assert any("安全一侧" in b for b in check_invariants(p, CFG))


def test_invariants_accept_stop_above_entry_for_long():
    """做多止损高于入场价 = 已锁定利润，合法，不得报错。"""
    p = replace(long_pos(), stop_price=101.0)
    assert check_invariants(p, CFG) == []


def test_invariants_flag_risk_above_R():
    p = replace(long_pos(), qty=10.0)          # 止损处风险 12U > 5U
    assert any("超过 R" in b for b in check_invariants(p, CFG))


def test_config_validation():
    with pytest.raises(ValueError):
        GuardConfig(risk_per_trade=0)
    with pytest.raises(ValueError):
        GuardConfig(max_adds=-1)
    with pytest.raises(ValueError):
        GuardConfig(target_1_R=0)
