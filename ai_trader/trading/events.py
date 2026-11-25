# -*- coding: utf-8 -*-
"""决策 / 下单 / 持仓的**事件契约**（只放数据，不放逻辑）。

定位
----
用户 2026-11-24 描述的架构：

    「开仓决策像一个流水线广播，不关心下游，有就产出！有一个下单订阅方，
      去做真实下单！下不下单有一些拦截比如仓位、刚才说的手工单之类的，
      如果拦截通过，就走下单！有了持仓单之后，就发出信号，也像广播，
      那么 K 线演化等出场、止盈、止损等都有了勾子，就运转起来了！
      没有持仓这些都没意义！」

本模块就是那套广播的**消息定义**。为什么先定契约：

  这个项目最大的反复缺陷类型叫「同一件事两个来源」——
  同一个概念在多处各写一份，然后漂移。最近的例子就是平仓手续费：
  `_close` 记开+平、`_adopted_close` 只记平、`_pending_close` 记 0，
  同一笔仓算出三个净盈亏，而且漏记的方向永远让策略显得更赚钱。

  所以广播架构的第一块砖不是 Router，是**消息长什么样**。
  只要消息是 frozen 的、字段是显式的，下游就不需要"猜"上游给了什么。

设计上的两条硬约束
------------------
1. **`EntryIntent` 必须能在"无人订阅"时安全落地。**
   这是"决策不关心下游"的技术含义：产出方不该因为没人接单而抛异常、
   也不该因为拦下来就什么记录都不留。拒绝也要成为一条可回溯的事实。

2. **本模块不 import engine / 不 import 任何执行器。**
   只依赖标准库。这样 Web、回测、测试、守卫都能用同一份契约，
   不会因为 import 顺序或循环依赖被绕开。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

# ------------------------------------------------------------------ 闸门名称
# ★ 这些字符串会进 `decisions.gates` 和日志，改字面量等于改历史记录的可搜性。
GATE_UNATTENDED = "无人值守闸"
GATE_MANUAL_LOT = "手工仓闸"
GATE_SIZING = "仓位闸"
GATE_HALTED = "执行器熔断闸"
GATE_DUPLICATE = "同bar去重闸"
GATE_DRY_RUN = "纸面模式闸"
#: MGN-2（2026-11-25）：可用保证金够不够（不够时先撤外来占位单）。
GATE_MARGIN = "可用保证金闸"

# 路由结果的三种终局。用字符串而不是 bool，是因为"拦住"和"挂了"之间
# 还有第三种：**根本没有可执行的东西**（比如数量取整后为 0）。
ROUTE_PLACED = "placed"
ROUTE_REJECTED = "rejected"
ROUTE_EMPTY = "empty"


@dataclass(frozen=True)
class GateResult:
    """一道闸的判定。`ok=True` 表示放行。

    `detail` 必须是**人能读懂的原因**，因为拦截之后它会被原样写进
    `decisions.reason` 和 `risk_events.detail`。写"拒绝"等于没写。
    """

    gate: str
    ok: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.gate:
            raise ValueError("闸门必须有名字，否则拦截原因无法归因")


@dataclass(frozen=True)
class EntryIntent:
    """一条**开仓意图**。决策者的全部产出就是它。

    ★ 为什么带 `strategy` 和 `thesis_id`：决策者之间互不知道对方存在
      （挂单策略、趋势策略、剥头皮各产各的），但下游必须能归因到"谁想开的这单"。
      没有这两个字段，出了事只能靠日志时间戳猜。

    ★ `qty` 是**意图量**，不是最终成交量。执行器可以按交易所规格取整，
      但取整结果必须回到 `RouteResult`，不能回头改写 intent ——
      意图一旦发出就是事实，只能追加新的结果事件。
    """

    strategy: str
    side: str                  # "long" | "short"（引擎内部统一口径）
    entry: float
    stop: float
    target: float = 0.0
    qty: float = 0.0
    order_type: str = "LIMIT"
    client_id: str = ""
    reason: str = ""
    thesis_id: str = ""
    bar_ts: int = 0
    # meta 只放**证据**（特征值、阈值、算路），不放可变状态。
    # frozen 的 dict 仍是可变的，所以约定：调用方创建后不许再改。
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy:
            raise ValueError("开仓意图必须带 strategy")
        if self.side not in ("long", "short"):
            raise ValueError("开仓意图 side 必须是 long/short，收到 %r" % (self.side,))
        if self.entry <= 0:
            raise ValueError("开仓意图 entry 必须为正，收到 %r" % (self.entry,))
        # ★ 止损为 0 是合法的（唐奇安策略刻意不设硬停损），
        #   但**负的**止损说明上游算错了，宁可当场炸掉。
        if self.stop < 0:
            raise ValueError("开仓意图 stop 不能为负，收到 %r" % (self.stop,))


@dataclass(frozen=True)
class RouteResult:
    """一条 `EntryIntent` 的终局。

    `gates` 保留**所有**跑过的闸（包括放行的），不是只保留拦住的那一道。
    理由：事后回溯时最常问的是"它当时过了哪几关"，
    而只记最后一道失败，等于把通过的部分丢掉。
    """

    status: str
    intent: Optional[EntryIntent] = None
    gates: Tuple[GateResult, ...] = ()
    rejection: str = ""
    order: Any = None
    error: str = ""
    placed_qty: float = 0.0

    @property
    def placed(self) -> bool:
        return self.status == ROUTE_PLACED

    @property
    def failed_gate(self) -> str:
        for g in self.gates:
            if not g.ok:
                return g.gate
        return ""

    def as_decision_gates(self) -> list:
        """给 `decisions.gates` 列的 JSON 形状（引擎里那段已经有读者）。"""
        return [{"gate": g.gate, "ok": g.ok, "detail": g.detail} for g in self.gates]


@dataclass(frozen=True)
class ExitIntent:
    """一条**出场意图**（BUS-C/D 用，本轮只定义契约不接生产）。

    `slot` 是本轮拆仓的产物：`"system"` = 策略自己开的仓，
    `"adopted"` = 对账接管的人工仓。同一个品种在交易所是一个净持仓，
    但内部必须分开记 —— 否则任何规则都能对任何来源的仓动手
    （2026-11-24 事故就是人工仓被策略规则平掉的）。
    """

    slot: str
    action: str                # EXIT_STOP / EXIT_STRUCTURE / REDUCE / MOVE_STOP
    close_qty: float = 0.0
    new_stop: float = 0.0
    reason: str = ""
    bar_ts: int = 0
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.slot not in ("system", "adopted"):
            raise ValueError("出场意图 slot 必须是 system/adopted，收到 %r"
                             % (self.slot,))


@dataclass(frozen=True)
class PositionOpened:
    """持仓建立事件。**有了它，下游出场勾子才有意义**（用户原话）。"""

    slot: str
    side: str
    qty: float
    entry: float
    stop: float
    target: float = 0.0
    thesis_id: str = ""
    client_id: str = ""


@dataclass(frozen=True)
class PositionClosed:
    """持仓关闭事件。带毛/费/净三个数，口径由 `close_booking` 唯一决定。"""

    slot: str
    side: str
    qty: float
    entry: float
    exit_price: float
    gross: float
    fee: float
    net: float
    reason: str = ""
