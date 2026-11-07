"""风险与仓位：从风险预算反推仓位，杠杆只是结果。

这条是 k-trader 最致命的错误的反面：它用 lev = safety/stop_dist 让止损越紧杠杆越高，
等于用"更小的止损"换取"更大的仓位"，插针/滑点的绝对伤害反而放大。
这里永远是 qty = risk_budget / stop_dist，杠杆只做上限检查。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class RiskState:
    day: str = ""
    day_pnl: float = 0.0
    consecutive_losses: int = 0
    cooldown_until: int = 0        # ms
    trades_today: int = 0
    halted_reason: str = ""
    #: 实盘峰值基线的起算时刻（ms）。0 = 全历史。
    #: 资金被提走又注入后，旧峰值属于另一笔钱，不该继续压在风控上。
    baseline_ts: int = 0


@dataclass
class Plan:
    ok: bool
    reason: str
    side: str = ""
    qty: float = 0.0
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    stop_pct: float = 0.0
    tp_pct: float = 0.0
    notional: float = 0.0
    leverage: float = 0.0
    risk_usdt: float = 0.0
    liq_distance_pct: float = 0.0
    cost_pct: float = 0.0
    max_hold_bars: int = 0
    gates: List[dict] = field(default_factory=list)


def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step + 1e-9) * step


def build_plan(side: str, entry: float, stop_hint: float, equity: float,
               cfg, features: dict, account_mmr: float = 0.004) -> Plan:
    """把「信号 + 当前权益 + 结构止损位」变成一个可执行的计划，或明确拒绝原因。"""
    gates: List[dict] = []
    direction = 1 if side == "long" else -1

    # ---- 1) 止损距离：结构位 + 波动下限 ----
    sig_floor = entry * (features.get("sigma3_pct", 0.0) * cfg.stop_sigma_mult) / 100.0
    atr_floor = max(entry * (features.get("atr3s_pct", 0.0) * 3.0) / 100.0, sig_floor)
    stop_dist = abs(entry - stop_hint)
    stop_dist = max(stop_dist, atr_floor, entry * cfg.min_stop_pct / 100.0)
    stop = entry - direction * stop_dist
    stop_pct = stop_dist / entry * 100.0

    ok_stop = cfg.min_stop_pct <= stop_pct <= cfg.max_stop_pct
    gates.append({"gate": "止损距离", "ok": ok_stop,
                  "detail": "%.3f%% (允许 %.2f%%~%.2f%%)" % (stop_pct, cfg.min_stop_pct, cfg.max_stop_pct)})

    tp_dist = stop_dist * cfg.rr_target
    tp_pct = tp_dist / entry * 100.0
    target = entry + direction * tp_dist

    # ---- 2) 成本门：目标利润必须显著大于往返成本 ----
    #
    # 【这里只能用永续费率 cfg.taker_fee，不要按 exec_market 切】
    #
    # 我一度把它改成按市场取（现货 0.1%），结果本文件的两个测试立刻失败。
    # 查下来是我自己搞出的不一致：这个函数（qty_from_risk_budget）是
    # 剥头皮专用，它用的规格是 cfg.qty_step / cfg.min_notional（永续 0.001/50U）、
    # 杠杆 60x——全都是永续的；只有我新加的费率按现货算。
    # 同一张计划里混两套约定，会算出一个既不是现货也不是永续的成本。
    #
    # 剥头皮路径本来就是永续专用（趋势策略走 _step_trend，不经过这里），
    # 所以这里固定用永续费率是对的。
    cost_pct = (cfg.taker_fee * 2 + cfg.slippage_bps / 10000.0 * 2) * 100.0
    ok_cost = tp_pct >= cfg.cost_multiple * cost_pct
    gates.append({"gate": "成本门", "ok": ok_cost,
                  "detail": "目标 %.3f%% vs 往返成本 %.4f%% x%d" % (tp_pct, cost_pct, cfg.cost_multiple)})

    # ---- 2.5) 可达性门：目标必须是当前波动尺度下"够得到"的 ----
    # 2026-11-01 的真事故：止损 0.41%(=71σ)、目标 0.63%(=109σ)，而 max_hold 只有 600 根。
    # 随机漫步下够到 109σ 需要 (109)^2 ≈ 11895 根 ≈ 10 小时，600 根内必然超时。
    # 结果不是"止损被打"，而是被时间平掉，还付了双向手续费 —— 一笔注定负期望的单。
    sigma3 = features.get("sigma3_pct", 0.0)
    need_bars = int((tp_pct / sigma3) ** 2) if sigma3 > 0 else 0
    max_bars = int(cfg.max_hold_hours * 3600 / 3)
    if sigma3 <= 0:
        ok_reach = True
        reach_detail = "无 σ 数据，跳过可达性判定"
    elif need_bars <= cfg.reach_min_bars:
        ok_reach = True
        reach_detail = "目标 %.3f%% 需 %d 根（≤%d，几乎必达）" % (tp_pct, need_bars, cfg.reach_min_bars)
    else:
        ok_reach = need_bars <= max_bars
        reach_detail = ("目标 %.3f%% = %.0fσ，需约 %d 根(%.1f 小时)%s"
                        % (tp_pct, tp_pct / sigma3, need_bars, need_bars * 3 / 3600.0,
                           "" if ok_reach else "，超出 %.0f 小时上限 → 够不到" % cfg.max_hold_hours))
    gates.append({"gate": "可达性", "ok": ok_reach, "detail": reach_detail})

    # ---- 3) 波动门：太小没肉，太大滑点失控 ----
    atr = features.get("atr3s_pct", 0.0)
    ok_vol = cfg.min_atr_pct <= atr <= cfg.max_atr_pct
    gates.append({"gate": "波动门", "ok": ok_vol,
                  "detail": "atr3s %.4f%% (允许 %.3f%%~%.3f%%)" % (atr, cfg.min_atr_pct, cfg.max_atr_pct)})

    # ---- 4) 仓位：由风险预算反推 ----
    risk_budget = equity * cfg.risk_pct / 100.0
    raw_qty = risk_budget / stop_dist if stop_dist > 0 else 0.0
    qty = round_step(raw_qty, cfg.qty_step)

    # 杠杆上限约束（杠杆只是结果，不是旋钮）
    max_qty_by_lev = (equity * cfg.max_leverage) / entry if entry else 0.0
    qty = min(qty, round_step(max_qty_by_lev, cfg.qty_step))
    notional = qty * entry
    leverage = notional / equity if equity else 0.0

    ok_qty = qty >= cfg.qty_step and notional >= cfg.min_notional
    gates.append({"gate": "最小下单", "ok": ok_qty,
                  "detail": "qty=%.3f 名义=%.2fU (最小 %.3f/%.0fU) 杠杆=%.2fx"
                            % (qty, notional, cfg.qty_step, cfg.min_notional, leverage)})

    # ---- 5) 强平硬闸：强平价必须在止损之外足够远 ----
    liq_dist_pct = (1.0 / leverage - account_mmr) * 100.0 if leverage > 0 else 0.0
    ok_liq = leverage <= 0 or liq_dist_pct >= max(3 * stop_pct, 1.0)
    gates.append({"gate": "强平距离", "ok": ok_liq,
                  "detail": "%.3f%% (需 >= %.3f%%)" % (liq_dist_pct, max(3 * stop_pct, 1.0))})

    ok = all(g["ok"] for g in gates)
    if not ok:
        reason = next(g["detail"] for g in gates if not g["ok"])
    else:
        reason = ("%s qty=%.3f 名义%.2fU 杠杆%.2fx 止损%.3f%% 目标%.3f%% 风险%.2fU"
                  % (side, qty, notional, leverage, stop_pct, tp_pct, qty * stop_dist))

    plan = Plan(ok=ok, reason=reason, side=side, qty=qty, entry=entry, stop=stop,
                target=target, stop_pct=stop_pct, tp_pct=tp_pct, notional=notional,
                leverage=leverage, risk_usdt=qty * stop_dist,
                liq_distance_pct=liq_dist_pct, cost_pct=cost_pct, gates=gates)
    # 持仓上限按波动反推：随机漫步够到目标约需 (tp/sigma)^2 根，
    # 再留 2 倍余量给趋势行情，避免被时间平仓强行制造负期望。
    if sigma3 > 0:
        plan.max_hold_bars = int(min(cfg.max_hold_hours * 3600 / 3,
                                     max(cfg.reach_min_bars, need_bars * 2)))
    else:
        plan.max_hold_bars = cfg.max_hold_bars
    return plan


def check_risk_state(st: RiskState, cfg, now_ms: int, equity: float) -> Tuple[bool, str]:
    if st.halted_reason and st.day == _today(now_ms):
        return False, st.halted_reason
    if st.cooldown_until and now_ms < st.cooldown_until:
        left = (st.cooldown_until - now_ms) / 60000.0
        return False, "连亏冷却中，还剩 %.1f 分钟" % left
    limit = -cfg.daily_loss_pct * equity / 100.0
    if st.day_pnl <= limit:
        st.halted_reason = "当日亏损 %.2fU 已达上限 %.2fU" % (st.day_pnl, limit)
        return False, st.halted_reason
    return True, ""


def _today(now_ms: int) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(now_ms / 1000).strftime("%Y-%m-%d")


def roll_day(st: RiskState, now_ms: int) -> RiskState:
    d = _today(now_ms)
    if st.day != d:
        st.day = d
        st.day_pnl = 0.0
        st.trades_today = 0
        st.halted_reason = ""
        # 连亏计数跨日保留，但冷却解除
        if st.cooldown_until < now_ms:
            st.cooldown_until = 0
    return st