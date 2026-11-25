# -*- coding: utf-8 -*-
"""出场执行层：把守卫的决定翻译成**可下发的动作**，并在这里守手数门槛。

为什么需要单独一层
------------------
`position_guard.decide()` 是纯函数，它只回答「该怎么办」，不管「能不能办」。
它会把一个仓位算出 `REDUCE` 并且给出 `close_qty` —— 那个数量是**数学上**
正确的，但未必是**交易上**可执行的。

★ 2026-11-24 观察期实测到的具体病例（用户裁决用「执行侧要求手数」修）：

    max_stop_distance_pct = liq_distance_pct / safety_multiple
    → 4.045454545454546 / 2.5 = 1.6181818181818184

  这两个数被设计成**刚好相等**，而 P0-b 的判据是严格小于。
  于是"止损正好顶到上限"这个点上，浮点表示误差 7.5e-15 就翻转了结论：

    structural_buffer() = 2.4999999999999925
    decide() → REDUCE，理由「结构性缓冲 2.50 倍 < 2.50 倍，减仓 0.0%」
    要求平掉 8.99e-17 BTC

  交易所步长是 0.001 BTC。`8.99e-17` 取整后是 0 —— 一张**注定失败**的指令。
  如果执行层照单全收，就会每 15 分钟发一次这种空指令：
  要么被交易所拒（噪声、可能触发限频），要么在下游被悄悄吞掉（更糟：
  日志上看起来"减过仓"，实际一股没动）。

★ 为什么修在执行侧而不是改守卫的判据（用户的选择 ②）：

  加一个容差（`< safety * (1-1e-9)`）只让**这个算式**不再产生那个数；
  而"下发一个小于步长的数量"是**任何来源**都可能犯的错 ——
  仓位取整、部分成交、外部改仓、将来新增的规则。
  门槛放在唯一的出口上，所有来源一次覆盖。

这也正是 `order_router.py` 对开仓做的事（拦截收敛到唯一出口）。
出场侧同样只能有一个出口，否则"某条路径忘了取整"必然会再发生一次。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .position_guard import (ADD, EXIT_STOP, HOLD, MOVE_STOP, PARTIAL_TP,
                             REDUCE, GuardDecision)

# 执行层自己产生的两种"不动作"结论（不是守卫的 HOLD，是执行层的否决）
SKIP_TOO_SMALL = "too_small"      # 数量取整后不足一个交易所步长
SKIP_NOTHING = "nothing"          # 守卫本来就没要求动作


@dataclass(frozen=True)
class ExitPlan:
    """一条**可执行**的出场计划。`action == HOLD` 表示什么都不该做。

    `why_not` 说明为什么不动作：空 = 守卫要求持有；
    `SKIP_TOO_SMALL` / `SKIP_NOTHING` = 执行层否决了守卫的动作。
    这个区分很重要 —— 前者是"守卫认为没事"，后者是"守卫要求做但做不到"，
    混在一起会把执行层的缺陷伪装成守卫的判断。
    """

    action: str
    qty: float = 0.0
    new_stop: float = 0.0
    reason: str = ""
    why_not: str = ""
    guard_action: str = ""

    @property
    def is_noop(self) -> bool:
        return self.action == HOLD

    @property
    def is_full_exit(self) -> bool:
        return self.action == EXIT_STOP


def round_down_to_step(qty: float, step: float) -> float:
    """向下取整到步长。宁可小一点也不能超：超了就是拒单。

    与 `LiveExecutor.round_qty` 同一口径（那边是交易接口的最后一道取整）。
    这里提前算一次，是为了能在**决定是否动作**之前就知道结果。
    """
    if qty <= 0:
        return 0.0
    if step <= 0:
        return qty
    n = int(round(qty / step, 9) // 1)
    return round(n * step, 10)


def plan_exit(decision: GuardDecision, qty: float, step: float,
              price: float = 0.0) -> ExitPlan:
    """把守卫决定翻译成可执行计划。**小于一个步长的动作一律否决为 HOLD。**

    Args:
        decision: `position_guard.decide()` 的返回值
        qty:      当前持仓数量（绝对值）
        step:     交易所步长；<= 0 表示未知，此时不做手数否决
        price:    当前价（仅用于日志/理由）

    ★ 反向约定：`step <= 0`（未知）时**放行**而不是拦死。
      理由：未知步长是配置缺失，此时拦掉所有出场会让止损彻底失效 ——
      "因为不知道能不能卖，所以不卖"是最坏的一种安全。这与
      "拦截器异常一律拒绝"不同：那是保护开仓，这是保护**逃生**。
    """
    a = str(getattr(decision, "action", "") or "")
    why = str(getattr(decision, "reason", "") or "")

    if a == HOLD:
        return ExitPlan(action=HOLD, reason=why, why_not=SKIP_NOTHING,
                        guard_action=a)

    if a == MOVE_STOP:
        ns = float(getattr(decision, "new_stop", 0.0) or 0.0)
        if ns <= 0:
            return ExitPlan(action=HOLD, reason=why, why_not=SKIP_NOTHING,
                            guard_action=a)
        return ExitPlan(action=MOVE_STOP, new_stop=ns, reason=why,
                        guard_action=a)

    if a in (EXIT_STOP, REDUCE, PARTIAL_TP):
        # EXIT_STOP = 全部离场；REDUCE / PARTIAL_TP 只平一部分
        want = (float(qty) if a == EXIT_STOP
                else float(getattr(decision, "close_qty", 0.0) or 0.0))
        if want <= 0:
            return ExitPlan(action=HOLD, reason=why, why_not=SKIP_NOTHING,
                            guard_action=a)
        # ★ 不需要单独钳位 `want <= qty`：下面「取整后 >= 全部持仓就走全平」
        #   那一支天然把上限压在 qty 上（超卖在合约上等于反向开仓）。
        #   原本这里有一段 `if want > qty: want = qty`，与那一支完全等价；
        #   变异测试把钳位删掉后测试依旧全绿，证明它是冗余。
        #   不写冗余的价值是：只有一个地方决定"最多能卖多少"。
        rounded = round_down_to_step(want, float(step or 0.0))

        # ★★ 用户裁决 ②（2026-11-24）：执行侧要求好手数 >= 交易所步长，
        #    不足视为 HOLD。这就是 `8.99e-17 BTC` 那道坎的位置。
        #
        #    只留这一条：`round_down_to_step` 已经把 0 < qty < step 压成 0，
        #    所以 `<= 0` 同时覆盖"数量为 0"和"不足一步"。
        #    （原本这里还有一条 `if step > 0 and rounded <= 0`，与这条完全
        #      重复；变异测试把那条改成 False 后测试依旧全绿，
        #      说明它只是一行看着有用的死代码。）
        if rounded <= 0:
            return ExitPlan(action=HOLD, reason=why, why_not=SKIP_TOO_SMALL,
                            guard_action=a)

        # 取整后正好等于全部持仓 → 直接按全平处理，别走减仓路径。
        # 否则会调"减仓"接口去平掉 100%，语义错了（部分成交/手续费归因也会歪）。
        if float(qty) > 0 and rounded >= float(qty):
            return ExitPlan(action=EXIT_STOP, qty=float(qty), reason=why,
                            guard_action=a)
        return ExitPlan(action=REDUCE, qty=rounded, reason=why,
                        guard_action=a)

    if a == ADD:
        # 用户裁决「不要加仓」→ 执行层直接拒绝，即使有人把 max_adds 调开了。
        # 这条不变量的位置很有讲究：放在**执行侧**而不是只靠配置，
        # 与 BUS-A 把拦截收敛到唯一出口是同一个道理。
        return ExitPlan(action=HOLD, reason=why, why_not=SKIP_NOTHING,
                        guard_action=a)

    # 未知动作：宁可不动，也不猜它的意思
    return ExitPlan(action=HOLD, reason=why, why_not=SKIP_NOTHING,
                    guard_action=a)


def describe(plan: ExitPlan) -> str:
    """一句话说清执行层的结论（进日志/决策用）。"""
    if plan.action == EXIT_STOP:
        return "全部离场 %.6f（%s）" % (plan.qty, plan.reason)
    if plan.action == REDUCE:
        return "减仓 %.6f（%s）" % (plan.qty, plan.reason)
    if plan.action == MOVE_STOP:
        return "移动止损到 %.6f（%s）" % (plan.new_stop, plan.reason)
    if plan.why_not == SKIP_TOO_SMALL:
        return "按兵不动：守卫要求 %s，但取整后不足一个交易所步长（%s）" % (
            plan.guard_action, plan.reason)
    return "按兵不动（%s）" % (plan.reason or plan.guard_action or "无动作")
