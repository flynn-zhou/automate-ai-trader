# -*- coding: utf-8 -*-
"""出站路由层：**系统里唯一的真实下单出口**。

用户 2026-11-24 的架构原话：

    「有一个下单订阅方，去做真实下单！下不下单有一些拦截比如仓位、
      刚才说的手工单之类的，如果拦截通过，就走下单！」

本模块就是那个"下单订阅方"。它本身**不做任何交易判断** ——
判断全部由注册进来的闸门（interceptor）负责，它只负责三件事：

    1. **按声明顺序**跑闸门（顺序即优先级，代码里不写 if 链）
    2. 第一道拦住就停，并保留**所有**跑过的闸
    3. 全部通过才调 placer（真正的下单动作）

为什么要有这一层
----------------
改之前，拦截散在四条开仓路径上，各自实现：

    scalp  → `_step` 里手写 guard.can_open() + check_risk_state() + build_plan()
    trend  → `_step_trend` 里再写一遍 guard.can_open()
    pending→ `_pending_place` 里又写一遍 guard.can_open() + _pending_size()
    adopted→ 在 `_step_pending` 里散着判

后果不是"重复"，是**必然漂移**：2026-11-17 的缺陷就是挂单路径漏了
guard.can_open()，于是数据源 DOWN、日亏损上限、连亏冷却、熔断、
持仓不一致这些保护在最新那条策略上**全部静默失效**。

只要拦截是"每个调用方各写一遍"，就一定会有下一条路径漏掉某一关。
把它收成**一条有名字的、有序的链**，漏一关就得先删掉一个具名的闸门，
那就藏不住了。

契约（故意定得很窄）
------------------
- 闸门 ``async (intent, ctx) -> GateResult | None``
  ``None`` 表示"这道闸不适用于本次"，不计入结果、不影响后续。
- placer ``async (intent, ctx) -> (ok: bool, order: Any, error: str)``
- 闸门和 placer 抛出的异常**一律向上传播**，本模块不吞。

★ 为什么异常不在这里吞（这一点和"健壮性"直觉相反，是刻意的）：
  改之前，闸门里的异常会一路冒到 `_apply_pending_action` 的
  `except Exception`，被记成 `pending_action_error` 并让这次动作**不算已执行**
  （于是下一根 K 线会重试）。如果路由层在这里吞掉并当成"拒绝"，
  风险事件的种类就从 `pending_action_error` 变成别的，
  而且"重试"语义会丢掉 —— 那是**行为变化**，不是健壮性。
  BUS-A 的验收标准是行为零变化，所以这里只做编排。

  至于"拦截器异常是否应该 fail-closed（一律拒绝）"，那是个好主意，
  但它属于 BUS-B 的决策落库改造，不能混在这一步里偷渡。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable, List, Optional, Tuple

from .events import (ROUTE_EMPTY, ROUTE_PLACED, ROUTE_REJECTED, EntryIntent,
                     GateResult, RouteResult)

# 闸门 / 下单动作的类型别名（放在这里，调用方不必各写一遍 typing 导入）
Gate = Callable[[EntryIntent, Any], Awaitable[Optional[GateResult]]]
Placer = Callable[[EntryIntent, Any], Awaitable[Tuple[bool, Any, str]]]


class OrderRouter:
    """按顺序执行闸门，全部通过才下单。**无状态**，可复用、可并发调用。"""

    __slots__ = ("_gates", "_placer")

    def __init__(self, gates: Iterable[Gate], placer: Placer) -> None:
        self._gates: Tuple[Gate, ...] = tuple(gates)
        if not callable(placer):
            raise TypeError("OrderRouter 必须有一个可调用的 placer")
        self._placer = placer

    # ------------------------------------------------------------ 只读视图
    @property
    def gates(self) -> Tuple[Gate, ...]:
        return self._gates

    def gate_names(self) -> List[str]:
        """闸门名字（按执行顺序）。用于启动时打一行日志，
        让"这条链上到底有几关、什么顺序"从代码里跳到运维眼前。"""
        return [str(getattr(g, "__name__", "") or repr(g)) for g in self._gates]

    # ---------------------------------------------------------------- 主体
    async def route(self, intent: EntryIntent, ctx: Any = None) -> RouteResult:
        """跑完闸门，通过则下单。

        返回的 `gates` 包含**所有**跑过的闸（含放行的），
        `rejection` 是"哪一关 + 为什么"，可以直接写进决策原因。
        """
        passed: List[GateResult] = []
        for gate in self._gates:
            g = await gate(intent, ctx)
            if g is None:
                # 这道闸不适用于本次（例如纸面模式下的交易所规格闸）。
                continue
            passed.append(g)
            if not g.ok:
                return RouteResult(
                    status=ROUTE_REJECTED, intent=intent, gates=tuple(passed),
                    rejection=("%s：%s" % (g.gate, g.detail)) if g.detail else g.gate,
                )

        ok, order, err = await self._placer(intent, ctx)
        if not ok:
            # 下单失败也必须是**可观测的返回值**，不能只活在一行日志里。
            # 11-24 那 15 条幽灵动作就是因为失败只写日志、上层照样标记已执行。
            return RouteResult(
                status=ROUTE_EMPTY, intent=intent, gates=tuple(passed),
                rejection=("%s：%s" % ("下单失败", err)) if err else "下单失败",
                error=err,
            )
        return RouteResult(
            status=ROUTE_PLACED, intent=intent, gates=tuple(passed), order=order,
            placed_qty=float(getattr(order, "executed_qty", 0.0) or 0.0),
        )
