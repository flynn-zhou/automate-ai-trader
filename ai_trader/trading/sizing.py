# -*- coding: utf-8 -*-
"""仓位与风险派生计算（《下单与守卫_运行规则.md》§3 / §4 / §6 的唯一实现）。

设计要点：
  1. 纯函数，无 IO、无全局状态 —— 可以被 Web、守卫、回测、测试同时调用，
     保证「配置页显示的数字」和「守卫实际用的数字」是同一份代码算出来的。
  2. 仓位由**风险定仓**（先有结构止损距离，再算名义仓位），
     保证金比例只是**上限校验**，不参与定仓。
     理由见文档 §3.1：把保证金比例当输入会让 40% 对应 0.57% 的止损，
     落在双边手续费+滑点（约 0.15%）的噪音里，物理上不可交易。
  3. 时区只在这里转换一次：用户口径北京时间(UTC+8) -> UTC。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

# 用户口径时区：北京时间 UTC+8（固定偏移，无夏令时，故不用 zoneinfo 依赖）
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")

# 维持保证金率近似（BTC/ETH 小仓位一档）。生产应改为按交易所档位表读取。
DEFAULT_MAINTENANCE_MARGIN_RATE = 0.005
# 安全倍数：爆仓距离必须是止损距离的这么多倍，保证「止损永远先于爆仓触发」
DEFAULT_SAFETY_MULTIPLE = 2.5

#: 手数兜底：**每 100U 本金**对应的最小挂单手数（BTC）。
#:
#: ★ 用户 2026-11-26 口径（两轮，取最终版）：
#:   先是「每次挂单至少 0.015，分三次至少 0.045，不分三次至少 0.03」，
#:   试算后发现 0.045 手光保证金就要 124U、而权益只有 114U，于是改成
#:   **「改成每份 0.01 吧，留几次亏损后第二次开不出仓的机会」** ——
#:   三档合计 0.03，与单笔不分档的 0.03 恰好一致。
#:
#:   用户同轮的底线（记在这里，省得下次再问）：
#:     「我是能接受一次开仓信号最多损失 20U 的！114U 亏完了都没事」
#:   即 20U 是**可接受上限**，不是每次都要打满 —— 所以保守取值是对的。
#:
#: 为什么需要它：风险定仓算出来的手数经常只有 0.0074 这种量级，
#: 用户「完全没感觉」。兜底是**体验**要求，不是风险要求。
#:
#: ★★ 兜底不能无条件生效 —— 它会与两个硬约束打架，见 `min_order_qty()`
#:    的文档与 `engine._pending_size()` 的处理：
#:      · 保证金上限（22x / 100U 本金只撑得起约 0.0366 BTC）
#:      · 单笔风险 R（手数变大 = 真亏更多）
MIN_QTY_PER_100U = 0.010
#: 不分档（单笔一次挂出）时的兜底，同样按每 100U 本金算。
#:
#: ★ 与「三档 × 0.01 = 0.03」数值相同是**巧合，不是同一个数** ——
#:   所以分两个常量写，不许合并成 `tranches * MIN_QTY_PER_100U`：
#:   将来单笔兜底调到 0.02 时，三档的每档兜底不该跟着变。
MIN_QTY_SINGLE_PER_100U = 0.030

#: 手数兜底的**风险上限**：每 100U 本金，一次开仓信号最多亏多少 U。
#:
#: ★ 用户 2026-11-26 原话：「我是能接受一次开仓信号最多损失 20U 的！
#:   114U 亏完了都没事」—— 这是**可接受上限**（红线），不是目标。
#:
#: ★★ 为什么必须有它：兜底会让实际风险脱离风险预算。
#:    实测三引线止损 1.547%、兜底 0.03 手 → 真亏 28.3U，
#:    比风险预算(8U)高 3.5 倍、比用户红线(20U)高 41%。
#:    没有这道闸，「最多丢 20U」就成了一句假的承诺。
#:
#: 与手数兜底一样按本金比例跟随（200U 本金 → 40U）。
MAX_RISK_PER_SIGNAL_PER_100U = 20.0


def max_risk_usd(capital: float) -> float:
    """一次开仓信号允许的最大亏损（U）。按本金比例跟随。

    ★ 与 `min_order_qty()` 同源同尺度：两者都随本金等比放大，
      所以 100U 上的「0.03 手 / 20U」到 200U 变成「0.06 手 / 40U」，
      不会出现「兜底放大了但风险上限没放大」的错位。
    """
    cap = float(capital or 0.0)
    if cap <= 0:
        return 0.0
    return float(MAX_RISK_PER_SIGNAL_PER_100U) * cap / 100.0



def min_order_qty(capital: float, tranches: int = 1) -> float:
    """这笔计划的最小手数（兜底）。按本金比例跟随。

    Args:
        capital: 账户本金（U）。
        tranches: 这次一共挂几档。1 = 单笔一次挂出；3 = 反包三档。

    Returns:
        最小手数（BTC）。本金 <= 0 时返回 0（调用方会在别处拒绝）。

    ★ 为什么按 `tranches` 分叉而不是按 `kind`：

      「分三次」是**手数分摊结构**，不是形态属性。将来任何多档策略都该走同一
      条规则，而写死 `if kind == KIND_ENGULF` 会让下一个多档策略悄悄沿用单笔
      的 0.03 兜底（总仓变成应有值的两倍）。

    ★ 口径对照（100U 本金）：

          单笔一次挂出       0.030
          三档（各 0.010）   0.030 合计

      200U 本金时全部翻倍：0.060 / 各 0.020（合计 0.060）。
      —— 「跟随倍数变化」就是这个意思，除数是本金而不是硬编码。

    ★ 为什么三档合计和单笔一样大：用户 2026-11-26 明确要求
      「留几次亏损后第二次开不出仓的机会」—— 单次信号最多亏 8~20U，
      三档摊开只是把同一笔风险切小，不是把总敞口做大。
    """
    cap = float(capital or 0.0)
    if cap <= 0:
        return 0.0
    per = (MIN_QTY_PER_100U if int(tranches) > 1 else MIN_QTY_SINGLE_PER_100U)
    return float(per) * cap / 100.0



@dataclass(frozen=True)
class SizingConfig:
    """三个可配置输入 + 两个物理常数。Web 配置页将来编辑的就是前三个。"""

    capital: float = 100.0          # 账户本金 U
    risk_pct: float = 5.0           # 每笔风险占本金 %
    leverage: float = 22.0          # 交易所杠杆
    margin_cap_pct: float = 40.0    # 单笔保证金占本金上限 %
    maintenance_margin_rate: float = DEFAULT_MAINTENANCE_MARGIN_RATE
    safety_multiple: float = DEFAULT_SAFETY_MULTIPLE

    def __post_init__(self) -> None:
        if self.capital <= 0:
            raise ValueError("本金必须为正: %r" % (self.capital,))
        if not 0 < self.risk_pct <= 100:
            raise ValueError("风险比例必须在 (0,100]: %r" % (self.risk_pct,))
        if self.leverage <= 1:
            raise ValueError("杠杆必须 > 1: %r" % (self.leverage,))
        if self.maintenance_margin_rate <= 0:
            raise ValueError("维持保证金率必须为正")
        if self.safety_multiple <= 0:
            raise ValueError("安全倍数必须为正")
        # 1/L - m 必须为正，否则「爆仓距离」为零或负，任何仓位都瞬间爆掉
        if 1.0 / self.leverage - self.maintenance_margin_rate <= 0:
            raise ValueError(
                "杠杆过高：1/%r - %r <= 0，爆仓距离非正（上限约 %.0f 倍）"
                % (self.leverage, self.maintenance_margin_rate,
                   1.0 / self.maintenance_margin_rate))


@dataclass(frozen=True)
class Derived:
    """配置页要显示的四行 + 若干辅助量。全部由 SizingConfig 推导。"""

    risk_per_trade: float        # 第1行 R（向下取整，U）
    liq_distance_pct: float      # 第2行 爆仓距离 %
    max_stop_distance_pct: float  # 第3行 止损距离上限 %
    margin_cap_amount: float     # 第4行 单笔保证金上限 U
    lives: int                   # 生命数 = floor(本金 / R)
    notional_at_max_stop: float  # 止损用满上限时的名义仓位（参考）

    def as_rows(self) -> list:
        """给 Web 配置页直接渲染的四行。"""
        return [
            {"label": "每笔风险 R", "value": "%g U" % self.risk_per_trade},
            {"label": "爆仓距离", "value": "%.3f%%" % self.liq_distance_pct},
            {"label": "止损距离上限", "value": "%.3f%%" % self.max_stop_distance_pct},
            {"label": "单笔保证金上限", "value": "%g U" % self.margin_cap_amount},
        ]


def derive(cfg: SizingConfig) -> Derived:
    """把配置算成四行派生结果。改任何输入都会立刻反映到这里。"""
    R = math.floor(cfg.capital * cfg.risk_pct / 100.0)
    if R < 1:
        R = 0  # 本金太小，连 1U 风险都分不出来 -> 0 表示不可交易
    liq_dist = (1.0 / cfg.leverage - cfg.maintenance_margin_rate) * 100.0
    max_stop = liq_dist / cfg.safety_multiple
    margin_cap = cfg.capital * cfg.margin_cap_pct / 100.0
    lives = int(cfg.capital // R) if R > 0 else 0
    notional_at_max = (R / (max_stop / 100.0)) if (R > 0 and max_stop > 0) else 0.0
    return Derived(
        risk_per_trade=float(R),
        liq_distance_pct=liq_dist,
        max_stop_distance_pct=max_stop,
        margin_cap_amount=margin_cap,
        lives=lives,
        notional_at_max_stop=notional_at_max,
    )


@dataclass(frozen=True)
class PositionPlan:
    """给定一个结构止损距离，算出来的仓位方案 + 是否允许开仓。

    ★ `qty` / `floor_applied` / `risk_at_floor` 是 2026-11-26 加的手数兜底产物。

      兜底必须在**这里**算，不能在引擎里另算一遍 —— 否则「配置页显示的名义仓位」
      和「实际挂出去的手数」会各说各话，正是这个项目反复吃亏的「同一件事
      两个来源」。引擎只负责把 `min_qty` 传进来、把结论拿出去。
    """

    stop_distance_pct: float
    notional: float
    margin: float
    margin_pct_of_capital: float
    liq_buffer: float          # 爆仓距离 / 止损距离，必须 >= safety_multiple
    ok: bool
    reasons: tuple = field(default=())
    #: 最终手数（已应用兜底并取整到步长）。未传价格/步长时为 0。
    qty: float = 0.0
    #: 兜底有没有真的把仓位放大。
    floor_applied: bool = False
    #: 兜底有没有被**风险上限**压下来（用户红线「最多损失 20U」）。
    floor_capped: bool = False
    #: 兜底之后的实际风险（U）。风控口径的 R 是 `derive().risk_per_trade`，
    #: 兜底生效后这个数会**大于**它 —— 必须让调用方看得见。
    risk_at_floor: float = 0.0

    def as_dict(self) -> dict:
        return {
            "stop_distance_pct": self.stop_distance_pct,
            "notional": self.notional,
            "margin": self.margin,
            "margin_pct_of_capital": self.margin_pct_of_capital,
            "liq_buffer": self.liq_buffer,
            "ok": self.ok,
            "reasons": list(self.reasons),
            "qty": self.qty,
            "floor_applied": self.floor_applied,
            "floor_capped": self.floor_capped,
            "risk_at_floor": self.risk_at_floor,
        }


def plan_position(cfg: SizingConfig, stop_distance_pct: float,
                  price: float = 0.0, qty_step: float = 0.0,
                  min_qty: float = 0.0,
                  max_risk: float = 0.0) -> PositionPlan:
    """按结构止损距离定仓（风险定仓），并逐条检查开仓前置门（文档 §4.2）。

    stop_distance_pct: 结构止损距离，正数，单位 %。例如 1.2 表示 1.2%。

    Args:
        price: 入场价。给 0 表示**不启用**手数换算与兜底（老行为）。
        qty_step: 交易所手数步长（如 0.001）。
        min_qty: 手数兜底（见 `min_order_qty`）。0 = 不兜底。
        max_risk: 兜底后允许的最大亏损（U），见 `max_risk_usd`。0 = 不限制。

    ★★ 兜底的优先级（安全闸不许被兜底绕过）：

        1. 风险定仓算出 `qty_risk` —— 「按风险预算该下多少」。
        2. 兜底 `max(qty_risk, min_qty)` —— 用户要「至少 0.01/0.03」。
        3. **风险上限再压一次**：兜底使实际风险超过 `max_risk` 时，
           把兜底压回「刚好亏 `max_risk`」的手数（向下取整到步长）。
           —— 这是用户红线（「最多损失 20U」），优先级高于兜底。
        4. **保证金上限**：兜底后的保证金超上限 → 拒单，而不是让上限让路。
           上限是物理约束（开不出来），兜底是体验要求，物理约束必须赢。
        5. 兜底使实际风险 > 风险**预算**但 <= `max_risk` 时不拒绝
           （那是用户的明确取舍），但把 `risk_at_floor` 带出去，让日志看得见。

    ★ 为什么第 3 步和第 4 步处理方式不同（一个压、一个拒）：
      风险上限可以靠「下小一点」满足，压得住；保证金上限也可以靠下小一点
      满足，但那样会让手数掉到兜底以下，兜底就失去意义了 —— 与其静默缩水，
      不如明确拒单并写清缺口，让用户知道「这笔没挂出去」。
    """
    if stop_distance_pct <= 0:
        raise ValueError("止损距离必须为正: %r" % (stop_distance_pct,))

    d = derive(cfg)
    reasons = []

    # 名义仓位由风险倒推：N = R / d
    notional = d.risk_per_trade / (stop_distance_pct / 100.0) if d.risk_per_trade else 0.0
    margin = notional / cfg.leverage
    margin_pct = (margin / cfg.capital * 100.0) if cfg.capital else 0.0
    liq_buffer = d.liq_distance_pct / stop_distance_pct

    if d.risk_per_trade <= 0:
        reasons.append("本金过小，无法分出 1U 风险")
    # ★ 边界必须用容差比较。止损按设计**恰好等于**上限（仓位就是按
    # d_max 定的），而 `(d + cost)/price*100` 在浮点下会大出 1e-14 ——
    # 严格 `>` 会把本来合规的计划拒掉（实测 4/11）。
    # 这不是放宽：3%% 的止损对 1.618%% 的上限照样被拒。
    if stop_distance_pct > d.max_stop_distance_pct + 1e-4:
        reasons.append(
            "止损 %.3f%% 超过上限 %.3f%%（爆仓会先于止损触发）"
            % (stop_distance_pct, d.max_stop_distance_pct))
    # ★ 同一个边界问题的第二处。
    #
    #   `liq_buffer = 爆仓距离 / 止损距离`，而止损上限本来就是
    #   `爆仓距离 / 安全倍数` 算出来的 —— 所以恰好贴着边界时
    #   liq_buffer **就是** safety_multiple（理论上相等）。
    #   浮点下它会变成 2.4999999999，于是“恰好合规”被判成不合规。
    #   用相对容差（1e-9 也行，但除法会放大误差，用相对更稳）。
    if liq_buffer < cfg.safety_multiple * (1.0 - 1e-5):
        reasons.append(
            "爆仓缓冲 %.2f 倍 < 要求 %.2f 倍" % (liq_buffer, cfg.safety_multiple))

    # ---- 手数 / 兜底 / 风险上限 ----
    qty = 0.0
    floor_applied = False
    floor_capped = False
    risk_at_floor = 0.0
    margin_checked = margin
    if price > 0 and qty_step > 0:
        step = float(qty_step)
        # 1 手亏多少钱：risk_per_unit = 价 x 止损%
        risk_per_unit = price * (stop_distance_pct / 100.0)
        # 红线折算成手数（向下取整到步长 = 宁可少亏，不可多亏）
        qty_cap = None
        if max_risk and max_risk > 0 and risk_per_unit > 0:
            qty_cap = math.floor(
                float(max_risk) / risk_per_unit / step + 1e-9) * step
        qty_risk = math.floor(notional / price / step) * step
        # ★ 红线是**绝对上限**：连风险定仓也不许越。
        #   正常配置下不会触发（风险预算 8U < 红线 20U），
        #   但如果有人把 risk_pct 调到 30U，红线必须赢 ——
        #   否则「一次开仓信号最多损失 20U」就成了一句空话。
        if qty_cap is not None and qty_risk > qty_cap:
            qty_risk = qty_cap
        qty = qty_risk
        if min_qty and min_qty > 0 and qty_risk < min_qty:
            # 兜底也要落在步长网格上，且**只能向上取整**（往下就失去兜底意义）
            qty_floor = math.ceil(float(min_qty) / step - 1e-9) * step
            # 兜底同样不许越红线
            if qty_cap is not None and qty_floor > qty_cap:
                qty_floor = qty_cap
                floor_capped = True
            if qty_floor > qty_risk:
                qty = qty_floor
                floor_applied = True
        qty = round(qty, 10)
        if qty > 0:
            margin_checked = qty * price / cfg.leverage
            risk_at_floor = qty * price * (stop_distance_pct / 100.0)

    if margin_checked > d.margin_cap_amount:
        reasons.append(
            "保证金 %.2fU 超过上限 %.2fU（占本金 %.1f%%）"
            % (margin_checked, d.margin_cap_amount,
               (margin_checked / cfg.capital * 100.0) if cfg.capital else 0.0))

    return PositionPlan(
        stop_distance_pct=stop_distance_pct,
        notional=notional,
        margin=margin_checked if price > 0 and qty_step > 0 else margin,
        margin_pct_of_capital=(margin_checked / cfg.capital * 100.0)
        if (price > 0 and qty_step > 0 and cfg.capital) else margin_pct,
        liq_buffer=liq_buffer,
        ok=not reasons,
        reasons=tuple(reasons),
        qty=qty,
        floor_applied=floor_applied,
        floor_capped=floor_capped,
        risk_at_floor=risk_at_floor,
    )


# ------------------------------------------------------------------ 开仓时段闸门
# 星期编号：周一=0 … 周日=6（与 datetime.weekday() 一致，全系统统一）
# 窗口用「一天内的分钟数」表示，[start, end)；end=1440 表示到当天 24:00
# 只用显式名字构造，禁止写裸数字当星期 —— 曾经把周日写成 5（那是周六），
# 会让闸门整体错一天且不报错。


def default_session_windows() -> dict:
    """默认开仓时段：周日 12:00 起，周一到周四全天，周五周六关闭。

    用户口径（北京时间）：故意跳过周五、周六与周日上午。
    """
    MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
    return {
        MON: [(0, 1440)],
        TUE: [(0, 1440)],
        WED: [(0, 1440)],
        THU: [(0, 1440)],
        FRI: [],
        SAT: [],
        SUN: [(12 * 60, 1440)],
    }


@dataclass(frozen=True)
class SessionConfig:
    windows: dict = field(default_factory=default_session_windows)
    enabled: bool = True

    def to_beijing(self, ts_utc: datetime) -> datetime:
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        return ts_utc.astimezone(BEIJING)


def in_trade_window(cfg: SessionConfig, ts_utc: datetime) -> bool:
    """该 UTC 时刻是否允许**开新仓**（北京时间口径判定）。"""
    if not cfg.enabled:
        return True
    bj = cfg.to_beijing(ts_utc)
    minute = bj.hour * 60 + bj.minute
    for start, end in (cfg.windows.get(bj.weekday()) or []):
        if start <= minute < end:
            return True
    return False


def session_label(cfg: SessionConfig) -> str:
    """人类可读的时段描述，供日志与将来的配置页使用。"""
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    parts = []
    for dow in range(7):
        wins = cfg.windows.get(dow) or []
        if not wins:
            parts.append("%s 关闭" % names[dow])
            continue
        seg = []
        for start, end in wins:
            seg.append("%02d:%02d-%02d:%02d" % (start // 60, start % 60, end // 60, end % 60))
        parts.append("%s %s" % (names[dow], "、".join(seg)))
    return "北京时间：" + "；".join(parts)
