# -*- coding: utf-8 -*-
"""持仓守卫 · 状态机与决策（《下单与守卫_运行规则.md》§7 / §8 的实现）。

设计原则
--------
1. **纯函数**：decide() 不做 IO、不写库、不下单。给定同一个 (持仓, 行情, K线读数)
   永远给出同一个决策。这样它可以在回测里被复用，规则也能被测试逐条钉死。
2. **优先级即顺序**：P0 保命 -> P1 止损 -> P2 结构离场 -> P3 分批止盈 -> P4 加仓 -> P5 持有。
   先命中的先执行，**不允许并列判断**（并列意味着两套结论互相打架）。
3. **止盈止损价格只朝有利方向移动**（棘轮）。任何情况下 guard 都不会把止损放宽，
   唯一例外是 P1 的「恐慌插针修正」，且一生只用一次、且受 liq_buffer 硬约束。

方向约定
--------
sign = +1 表示做多，-1 表示做空。所有几何量都用 sign 归一化，避免双份分支代码。
做多：liq < stop < entry < target
做空：liq > stop > entry > target
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Optional

# 决策动作
HOLD = "HOLD"
REDUCE = "REDUCE"              # P0 保命减仓
MOVE_STOP = "MOVE_STOP"        # 抬高止损（棘轮）/ P1 恐慌插针修正
EXIT_STOP = "EXIT_STOP"        # P1 止损离场
EXIT_STRUCTURE = "EXIT_STRUCTURE"  # P2 结构离场
PARTIAL_TP = "PARTIAL_TP"      # P3 分批止盈
ADD = "ADD"                    # P4 加仓

# 状态机
ST_FLAT = "FLAT"
ST_OPEN = "OPEN"
ST_CLOSING = "CLOSING"


@dataclass(frozen=True)
class GuardConfig:
    """守卫参数。前四项来自 Web 配置（§2），其余是行为常数。"""

    risk_per_trade: float = 5.0        # R（U），来自 sizing.derive()
    max_adds: int = 1                  # 加仓次数上限，0 = 禁止加仓
    max_partials: int = 2              # 分批止盈次数上限（防同一仓位被连续密集切半）
    safety_multiple: float = 2.5       # 爆仓缓冲要求：liq_buffer >= 此值
    target_1_R: float = 2.0            # 第一目标（R 倍数），达到后分批止盈
    fee_bps: float = 5.0               # 单边手续费（基点），用于算保本价
    allow_new_risk: bool = True        # 是否允许增加风险（时段闸门 + 非交易日）

    def __post_init__(self) -> None:
        if self.risk_per_trade <= 0:
            raise ValueError("risk_per_trade 必须为正")
        if self.max_adds < 0:
            raise ValueError("max_adds 不能为负")
        if self.max_partials < 0:
            raise ValueError("max_partials 不能为负")
        if self.safety_multiple <= 0:
            raise ValueError("safety_multiple 必须为正")
        if self.target_1_R <= 0:
            raise ValueError("target_1_R 必须为正")


@dataclass
class GuardPosition:
    """守卫眼里的持仓。金额单位 U，价格单位同标的。"""

    symbol: str
    direction: str              # "long" | "short"
    avg_entry: float
    qty: float                  # 持仓数量（绝对值）
    stop_price: float
    liq_price: float
    target_price: Optional[float] = None
    initial_risk: float = 0.0   # 入场时的风险金额（U）= |avg-stop| * qty
    adds_done: int = 0
    partials_done: int = 0          # 已分批止盈次数（受 max_partials 约束）
    stop_delay_used: bool = False   # P1 恐慌插针修正是否已用（一生一次）
    realized_pnl: float = 0.0
    state: str = ST_OPEN

    @property
    def sign(self) -> int:
        return 1 if self.direction == "long" else -1

    # ---- 几何量（全部用 sign 归一化，做多做空共用一套公式）----
    def d_stop(self) -> float:
        """入场到止损的距离（正数）。"""
        return self.sign * (self.avg_entry - self.stop_price)

    def d_liq(self) -> float:
        """入场到爆仓价的距离（正数）。"""
        return self.sign * (self.avg_entry - self.liq_price)

    def adverse(self, price: float) -> float:
        """价格相对入场的不利移动量，正数=在亏损方向。"""
        return self.sign * (self.avg_entry - price)

    def remaining_to_stop(self, price: float) -> float:
        """价格距止损还剩多少。<=0 表示已越过止损。"""
        return self.d_stop() - self.adverse(price)

    def liq_buffer(self, price: float) -> float:
        """距爆仓还有几个「止损宽度」的距离（以 d_stop 为单位）。

        为什么不用「距爆仓 / 距止损」作分母：那个量在价格逼近止损时会虚高
        （分母趋零 -> 比值无穷大），越接近止损反而显示越安全，且在越过止损后
        直接失去定义。用固定的止损宽度做单位，含义稳定、可跨时刻比较。
        """
        d = self.d_stop()
        dist_liq = max(0.0, self.sign * (price - self.liq_price))
        if d <= 0:
            # 止损已在盈利侧（锁利润状态）：爆仓不可能先于止损发生，缓冲视为无穷
            return float("inf") if dist_liq > 0 else 0.0
        return dist_liq / d

    def structural_buffer(self) -> float:
        """结构性的爆仓缓冲（与当前价无关）：入场距爆仓 / 止损宽度。

        这是真正的不变量。只有「加仓」或「保证金被抽走」会让它恶化，
        价格移动不会——价格移动到止损时，止损本来就会先触发。
        """
        d = self.d_stop()
        if d <= 0:
            # 止损在盈利侧：本仓已无亏损风险，爆仓无从谈起
            return float("inf")
        return self.d_liq() / d

    def profit_u(self, price: float) -> float:
        """浮动盈亏（U）。"""
        return self.sign * (price - self.avg_entry) * self.qty

    def profit_R(self, price: float) -> float:
        if self.initial_risk <= 0:
            return 0.0
        return self.profit_u(price) / self.initial_risk

    def risk_at_stop(self, stop_price: Optional[float] = None) -> float:
        """若在给定止损处离场，将亏损多少 U（用于不变量校验）。"""
        stop = self.stop_price if stop_price is None else stop_price
        return abs(self.avg_entry - stop) * self.qty


@dataclass(frozen=True)
class GuardDecision:
    action: str
    reason: str
    metrics: dict = field(default_factory=dict)
    new_stop: Optional[float] = None
    new_qty: Optional[float] = None
    new_target: Optional[float] = None
    close_qty: Optional[float] = None       # PARTIAL_TP / REDUCE 时平掉的数量
    add_qty: Optional[float] = None         # ADD 时增加的数量
    evidence: tuple = field(default=())

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "metrics": dict(self.metrics),
            "new_stop": self.new_stop,
            "new_qty": self.new_qty,
            "new_target": self.new_target,
            "close_qty": self.close_qty,
            "add_qty": self.add_qty,
            "evidence": list(self.evidence),
        }


def _finite_round(x: float, ndigits: int = 4):
    """inf 不是合法 JSON，审计与配置页要走 JSON，这里统一落成 None。"""
    if x is None or x != x or x in (float("inf"), float("-inf")):
        return None
    return round(x, ndigits)


def _level_for_R(pos: GuardPosition, k: float, qty: float) -> Optional[float]:
    """返回「浮盈恰好为 k 个 R」对应的价格。

    关键：R 是**金额**概念（initial_risk），不是价格距离。减仓后 qty 变小，
    同样的价格距离只兑现更少的钱，所以必须除以**减仓后**的 qty。
    历史缺陷：曾用减仓前的 qty 计算，导致标注 +1R 的位置实际只有 0.5R。
    """
    if qty <= 0 or pos.initial_risk <= 0:
        return None
    return pos.avg_entry + pos.sign * (k * pos.initial_risk / qty)


def _scale_liq_for_qty(pos: GuardPosition, new_qty: float) -> float:
    """减仓后重算爆仓价：隔离保证金下 d_liq 与 qty 成反比。

    平仓让爆仓价外移是物理性质；REDUCE 与 PARTIAL_TP 必须用同一套算法，
    否则守卫会对同一个仓位给出两套互相矛盾的缓冲读数。
    """
    if new_qty <= 0 or pos.qty <= 0:
        return pos.liq_price
    scale = pos.qty / new_qty
    return pos.avg_entry - pos.sign * (pos.d_liq() * scale)


def _metrics(pos: GuardPosition, price: float) -> dict:
    return {
        "price": price,
        "avg_entry": pos.avg_entry,
        "qty": pos.qty,
        "stop_price": pos.stop_price,
        "liq_price": pos.liq_price,
        "d_stop": round(pos.d_stop(), 6),
        "d_liq": round(pos.d_liq(), 6),
        "remaining_to_stop": round(pos.remaining_to_stop(price), 6),
        "liq_buffer": _finite_round(pos.liq_buffer(price)),
        "structural_buffer": _finite_round(pos.structural_buffer()),
        "profit_R": round(pos.profit_R(price), 4),
        "profit_u": round(pos.profit_u(price), 4),
        # 已消耗的风险预算比例（0=刚开仓 1=正好到止损）
        "risk_consumed_ratio": round(
            1.0 - (pos.remaining_to_stop(price) / pos.d_stop()), 4
        ) if pos.d_stop() > 0 else 1.0,
        "state": pos.state,
    }


def _reduce_fraction_for_buffer(pos: GuardPosition, target: float) -> float:
    """算出要把仓位减掉多少比例，才能让**结构性缓冲**回到 target。

    推导：爆仓距离与持仓数量成反比（隔离保证金下 loss_at_liq = qty * d_liq，
    而可亏的保证金不变，故 d_liq ∝ 1/qty）：
        d_liq' = d_liq / (1 - f)            f = 平掉的持仓比例
    要求 d_liq' >= target * d_stop：
        1 - f <= d_liq / (target * d_stop)
        f >= 1 - d_liq / (target * d_stop)
    """
    d_stop = pos.d_stop()
    if d_stop <= 0:
        return 1.0
    denom = target * d_stop
    if denom <= 0:
        return 1.0
    f = 1.0 - pos.d_liq() / denom
    return min(1.0, max(0.0, f))


# --------------------------------------------------------------------- 决策主体
def decide(pos: GuardPosition, price: float, cfg: GuardConfig,
           kline: Optional[Any] = None) -> GuardDecision:
    """对当前持仓做出一个决定。优先级 P0 -> P5，命中即返回。"""
    m = _metrics(pos, price)

    if pos.state in (ST_FLAT, ST_CLOSING):
        return GuardDecision(HOLD, "状态 %s，守卫不动作" % pos.state, m)

    if price <= 0:
        return GuardDecision(HOLD, "价格无效（%r），跳过本轮" % (price,), m)

    # ------------------------------- P0 保命减仓（最高优先级，禁止被 K 线否决）
    buffer_now = pos.liq_buffer(price)
    past_stop = pos.remaining_to_stop(price) <= 0

    # P0-a 紧急：价格已经逼近爆仓（不足半个止损宽度），减仓也来不及 -> 直接离场
    if buffer_now < 0.5:
        return GuardDecision(
            EXIT_STOP,
            "P0 紧急：距爆仓仅 %.2f 个止损宽度 -> 立即全部离场" % buffer_now, m)

    # P0-b 结构性缓冲被压薄（加仓过多 / 保证金被抽走）-> 减仓修复
    if not past_stop and pos.structural_buffer() < cfg.safety_multiple:
        f = _reduce_fraction_for_buffer(pos, cfg.safety_multiple)
        if f >= 1.0:
            return GuardDecision(
                EXIT_STOP, "P0 结构性缓冲 %.2f 倍且无法通过减仓修复 -> 全部离场"
                % pos.structural_buffer(), m)
        if f > 0:
            cut = pos.qty * f
            return GuardDecision(
                REDUCE,
                "P0 保命：结构性缓冲 %.2f 倍 < %.2f 倍，减仓 %.1f%% 恢复缓冲"
                % (pos.structural_buffer(), cfg.safety_multiple, f * 100),
                m, new_qty=pos.qty - cut, close_qty=cut)

    # ------------------------------- P1 止损（含一次性恐慌插针修正）
    if past_stop:
        if (kline is not None and getattr(kline, "panic_flush", False)
                and not pos.stop_delay_used and buffer_now >= 3.0):
            atr = getattr(kline, "atr", 0.0) or 0.0
            low = getattr(kline, "low", None)
            if low is not None and atr > 0:
                new_stop = low - 0.2 * atr * pos.sign
                # 放宽必须留在不变量 #1（d_liq >= safety * d_stop）的预算内。
                # 否则下一轮 P0 会判定结构性缓冲被压薄而去减仓 ——
                # 放宽止损反而触发减仓，是自相矛盾的。这里直接钳制到预算上限。
                max_d_stop = pos.d_liq() / cfg.safety_multiple
                if abs(pos.avg_entry - new_stop) > max_d_stop:
                    new_stop = pos.avg_entry - pos.sign * max_d_stop
                widened = replace(pos, stop_price=new_stop)
                dist_new = abs(price - new_stop)
                dist_liq = abs(price - pos.liq_price)
                if widened.d_stop() > pos.d_stop() and dist_new * 1.5 <= dist_liq:
                    return GuardDecision(
                        MOVE_STOP,
                        "P1 恐慌插针修正：长下影+带量+收回上半部，延迟止损一次",
                        m, new_stop=new_stop,
                        evidence=tuple(getattr(kline, "evidence", ()) or ()))
        # 审计必须写**实际**亏损而不是计划亏损 1R：价格可能已滑过止损（跳空/滑点），
        # 此时 realized = profit_R（负）。写 "1R" 会让事后复盘误判亏损幅度。
        return GuardDecision(
            EXIT_STOP,
            "P1 价格越过止损 %.6g -> 全部离场，实际实现 %.2fR（计划 -1R）"
            % (pos.stop_price, pos.profit_R(price)), m)

    profit_R = pos.profit_R(price)

    # ------------------------------- P2 结构离场（替代死盯固定目标）
    if kline is not None:
        intent = (getattr(kline, "intent", None) or "none")
        evidence = tuple(getattr(kline, "evidence", ()) or ())

        if intent == "exhaustion":
            return GuardDecision(
                EXIT_STRUCTURE,
                "P2 衰竭形态（长上影+放量+收在下沿）-> 结构离场", m, evidence=evidence)

        if (intent == "distribution" and profit_R >= 1.0
                and pos.partials_done < cfg.max_partials):
            half = pos.qty / 2.0
            new_stop = pos.avg_entry + (cfg.fee_bps / 10000.0) * pos.avg_entry * pos.sign
            return GuardDecision(
                PARTIAL_TP,
                "P2 派发形态且浮盈 %.2fR -> 减半，剩余止损抬到成本上方" % profit_R,
                m, close_qty=half, new_qty=pos.qty - half, new_stop=new_stop,
                evidence=evidence)

        if intent == "breakout" and profit_R >= 1.0:
            return GuardDecision(
                HOLD,
                "P2 带量突破且浮盈 %.2fR -> 不因到目标而平仓，继续持有" % profit_R,
                m, evidence=evidence)

    # ------------------------------- P3 分批止盈
    if pos.target_price is not None:
        reached = pos.sign * (price - pos.target_price) >= 0
    else:
        reached = profit_R >= cfg.target_1_R

    if (reached and profit_R >= cfg.target_1_R
            and pos.partials_done < cfg.max_partials):
        half = pos.qty / 2.0
        qty_after = pos.qty - half
        # 剩余仓位止损抬到 +1R、目标设为 +3R —— 一律按**减仓后**的 qty 折算，
        # 保证「标注几 R」与「该价位实际几 R」数值恒等。
        new_stop = _level_for_R(pos, 1.0, qty_after)
        new_target = _level_for_R(pos, 3.0, qty_after)
        return GuardDecision(
            PARTIAL_TP,
            "P3 达到第一目标 %.2fR -> 平一半，剩余止损抬到 +1R（价位 %.4g）"
            % (profit_R, new_stop),
            m, close_qty=half, new_qty=qty_after,
            new_stop=new_stop, new_target=new_target)

    # ------------------------------- 止损棘轮（只朝有利方向）
    ratchet = _ratchet_stop(pos, price, cfg, profit_R)
    if ratchet is not None:
        return ratchet

    # ------------------------------- P4 加仓（仅浮盈方向）
    add = _maybe_add(pos, price, cfg, kline, m, profit_R)
    if add is not None:
        return add

    return GuardDecision(HOLD, "P5 无动作条件成立，继续持有", m)


def _ratchet_stop(pos: GuardPosition, price: float, cfg: GuardConfig,
                  profit_R: float) -> Optional[GuardDecision]:
    """浮盈达标就抬止损：>=1R 抬到保本（含手续费），>=2R 抬到 +1R。只抬不降。"""
    fee = cfg.fee_bps / 10000.0
    candidates = []
    if profit_R >= 1.0:
        candidates.append(("1R 抬到保本", pos.avg_entry * (1 + fee * pos.sign)))
    if profit_R >= 2.0:
        per_unit_R = pos.initial_risk / pos.qty
        candidates.append(("2R 抬到 +1R", pos.avg_entry + pos.sign * per_unit_R))
    if not candidates:
        return None
    # 取最有利的那个，且必须严格优于现有止损
    best_label, best_stop = candidates[-1]
    if pos.sign * (best_stop - pos.stop_price) <= 0:
        return None
    return GuardDecision(MOVE_STOP, "止损棘轮：%s" % best_label,
                         _metrics(pos, price), new_stop=best_stop)


def _maybe_add(pos: GuardPosition, price: float, cfg: GuardConfig,
               kline: Optional[Any], m: dict,
               profit_R: float) -> Optional[GuardDecision]:
    """P4 加仓：浮盈 >= 0.5R + K线带量延续 + 还有次数 + 允许增加风险 + 加后总风险不变。"""
    if cfg.max_adds <= 0:
        return None
    if pos.adds_done >= cfg.max_adds:
        return None
    if not cfg.allow_new_risk:
        return None
    if profit_R < 0.5:
        return None
    if kline is None:
        return None
    if (getattr(kline, "volume_regime", None) != "放量"
            or (getattr(kline, "intent", None) or "") not in ("breakout", "continuation")):
        return None

    # 加仓量：目标总风险仍 <= R。加仓后止损同步抬到当前价下方 1 个原止损距离，
    # 使 (avg' - stop') * qty' <= R。
    add_qty = min(pos.qty / 2.0, pos.initial_risk / max(1e-9, pos.d_stop()) / 2.0)
    if add_qty <= 0:
        return None
    new_qty = pos.qty + add_qty
    new_avg = (pos.avg_entry * pos.qty + price * add_qty) / new_qty
    # 抬止损到「新均价 - 1个原止损距离」之上，保证不增加总风险
    proposed_stop = new_avg - pos.sign * (pos.initial_risk / new_qty)
    # 且必须严格优于现有止损（棘轮）
    if pos.sign * (proposed_stop - pos.stop_price) <= 0:
        proposed_stop = pos.stop_price
    return GuardDecision(
        ADD,
        "P4 浮盈 %.2fR + 带量延续 -> 加仓 %.6g，同步抬止损保持总风险不变"
        % (profit_R, add_qty),
        m, add_qty=add_qty, new_stop=proposed_stop, new_qty=new_qty,
        evidence=tuple(getattr(kline, "evidence", ()) or ()))


# --------------------------------------------------------------------- 不变量
def check_invariants(pos: GuardPosition, cfg: GuardConfig,
                     decision: Optional[GuardDecision] = None,
                     price: Optional[float] = None) -> list:
    """返回违反的不变量列表（空 = 通过）。实现验收用，文档 §10。"""
    bad = []
    if pos.d_liq() <= 0:
        bad.append("爆仓价在入场价的错误一侧（d_liq=%.6g）" % pos.d_liq())
    # 止损必须落在爆仓价的安全一侧，否则止损永远不会先触发
    if pos.d_liq() > 0 and pos.sign * (pos.stop_price - pos.liq_price) <= 0:
        bad.append("止损 %.6g 未在爆仓价 %.6g 的安全一侧"
                   % (pos.stop_price, pos.liq_price))
    # 以下只在止损仍在亏损侧（d_stop>0）时有意义。
    # 止损抬到盈利侧是正常的止盈锁仓，不是错误。
    if pos.d_stop() > 0:
        if pos.d_liq() < cfg.safety_multiple * pos.d_stop():
            bad.append("爆仓距离 %.6g < %.2f 倍止损距离 %.6g"
                       % (pos.d_liq(), cfg.safety_multiple, pos.d_stop()))
        risk = pos.risk_at_stop()
        if risk > cfg.risk_per_trade + 1e-9:
            bad.append("止损处风险 %.4fU 超过 R=%.4fU" % (risk, cfg.risk_per_trade))

    if decision is not None:
        if decision.new_stop is not None:
            if pos.sign * (decision.new_stop - pos.stop_price) < 0 and decision.action == MOVE_STOP:
                if not (decision.action == MOVE_STOP and "恐慌插针" in decision.reason):
                    bad.append("止损被朝不利方向移动（棘轮被破坏）")
        if decision.close_qty is not None and decision.close_qty > pos.qty + 1e-9:
            bad.append("平仓量 %.6g 超过持仓 %.6g" % (decision.close_qty, pos.qty))
        if decision.action == ADD and decision.new_stop is not None:
            new_qty = decision.new_qty or pos.qty
            new_avg = (pos.avg_entry * pos.qty + (price or pos.avg_entry) * (decision.add_qty or 0)) / new_qty
            after = abs(new_avg - decision.new_stop) * new_qty
            if after > cfg.risk_per_trade + 1e-6:
                bad.append("加仓后总风险 %.4fU 超过 R=%.4fU" % (after, cfg.risk_per_trade))
    return bad


def apply_decision(pos: GuardPosition, decision: GuardDecision) -> GuardPosition:
    """把决策落到持仓上（不涉及下单，仅供回测与测试）。"""
    if decision.action in (HOLD,):
        return pos
    out = replace(pos)
    # 真实成交价：优先用决策里记录的现价，绝不臆造（历史缺陷：曾用 target_price，
    # 该字段为 None 时回退到均价，导致分批止盈的已实现盈亏恒为 0）。
    exec_price = decision.metrics.get("price")

    if decision.action == REDUCE:
        out.qty = decision.new_qty if decision.new_qty is not None else pos.qty
        out.liq_price = _scale_liq_for_qty(pos, out.qty)
    elif decision.action in (EXIT_STOP, EXIT_STRUCTURE):
        out.state = ST_CLOSING
        px = exec_price if exec_price is not None else pos.stop_price
        # 离场必须把 qty 归零：否则已实现盈亏与残余浮动盈亏会重复计算，
        # 汇总时凭空多出一笔并不存在的浮动损益。
        out.realized_pnl = pos.realized_pnl + pos.sign * (px - pos.avg_entry) * pos.qty
        out.qty = 0.0
    elif decision.action == PARTIAL_TP:
        out.qty = decision.new_qty if decision.new_qty is not None else pos.qty
        # 只对**平掉的那部分**记盈亏，用真实成交价
        close_qty = decision.close_qty or 0.0
        px = exec_price if exec_price is not None else pos.avg_entry
        out.realized_pnl = pos.realized_pnl + pos.sign * (px - pos.avg_entry) * close_qty
        out.partials_done = pos.partials_done + 1
        out.liq_price = _scale_liq_for_qty(pos, out.qty)
        if decision.new_stop is not None:
            out.stop_price = decision.new_stop
        if decision.new_target is not None:
            out.target_price = decision.new_target
    elif decision.action == MOVE_STOP:
        if decision.new_stop is not None:
            out.stop_price = decision.new_stop
        if "恐慌插针" in decision.reason:
            out.stop_delay_used = True
    elif decision.action == ADD:
        add_qty = decision.add_qty or 0.0
        if add_qty > 0:
            new_qty = pos.qty + add_qty
            px = decision.metrics.get("price", pos.avg_entry)
            out.avg_entry = (pos.avg_entry * pos.qty + px * add_qty) / new_qty
            out.qty = new_qty
            out.adds_done = pos.adds_done + 1
            if decision.new_stop is not None:
                out.stop_price = decision.new_stop
    return out
