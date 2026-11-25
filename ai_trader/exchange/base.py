"""执行器接口。paper 与 live 必须返回同一种结构，否则策略层必然分叉。

k-trader 的教训：纸面/回测/实盘走了三条代码路径，最后"回测赚、实盘亏"无法归因。
这里强制同一个接口、同一份持仓表、同一个 PnL 公式。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class FillResult:
    ok: bool
    price: float = 0.0
    qty: float = 0.0
    fee: float = 0.0
    order_id: str = ""
    status: str = "rejected"          # filled / rejected / unknown
    reason: str = ""


@dataclass
class PendingOrder:
    """挂在交易所里、还没成交完的订单。

    ★ 为什么必须和 `FillResult` 分开：`FillResult` 表达的是「这一下成交了，
    成交价/量/费是多少」，调用方拿它直接记账；而挂单**没有成交**，
    持仓一分没动。两者混用会让「挂单成功」被当成「已建仓」——
    账面上有仓位、交易所里没有，或者反过来。

    `status` 用交易所原生的四个值：
        NEW / PARTIALLY_FILLED / FILLED / CANCELED
    （另外 `EXPIRED`、`REJECTED` 原样透传，不做归一化。）
    """
    order_id: str = ""
    client_id: str = ""
    symbol: str = ""
    side: str = ""
    type: str = ""
    qty: float = 0.0                 # 委托量
    executed_qty: float = 0.0        # 已成交量
    price: float = 0.0               # 限价（STOP_MARKET 时为 0）
    stop_price: float = 0.0          # 触发价
    avg_price: float = 0.0
    status: str = ""
    reduce_only: bool = False
    update_time: int = 0

    @property
    def working(self) -> bool:
        """还挂着、还能成交。"""
        return self.status in ("NEW", "PARTIALLY_FILLED")

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.executed_qty)


class Executor:
    name = "base"

    async def equity(self) -> dict:
        """返回 {balance, unrealized, equity, position_qty, position_side}"""
        raise NotImplementedError

    async def market_order(self, side: str, qty: float, reason: str = "",
                           reduce_only: bool = False) -> FillResult:
        raise NotImplementedError

    # ---------------------------------------------------------------- 挂单
    #
    # ★ 2026-11-17 为什么把这三个方法提到基类：
    #
    #   `place_order / cancel_order / query_order / open_orders` 以前**只写在
    #   LiveExecutor 上**，基类里一个字都没有，`PaperExecutor` 自然也就没有。
    #   后果不是「纸面少个功能」，而是**整条「提前挂单」的策略在生产里跑不起来**：
    #   `pending_plan` + `thesis` 的全部语义就是「先挂一张单，等价格来」，
    #   纸面模式（默认 `AI_TRADE_MODE=paper`）连挂单这个动作都表达不了。
    #
    #   同一个坑记过一次（HANDOFF §十七「假交易所忽略 type」），那次是
    #   「有方法但忽略参数」，这次更彻底：**方法根本不存在**。
    #   提到基类之后，`isinstance(ex, Executor)` 就能保证四个方法可调用，
    #   谁没实现谁在 import 期就暴露，而不是等到生产第一笔挂单。

    async def place_order(self, side: str, qty: float,
                          order_type: str = "LIMIT",
                          price: Optional[float] = None,
                          stop_price: Optional[float] = None,
                          reduce_only: bool = False,
                          client_id: str = "",
                          working_type: str = "CONTRACT_PRICE",
                          ) -> "Tuple[PendingOrder, str]":
        """挂一张单。返回 `(PendingOrder, 错误说明)`，**不抛异常**。

        为什么返回元组而不是抛：策略层必须能区分「挂上了」和「没挂上」，
        而挂单失败不该让主循环崩 —— 崩了等于策略停了但仓位还在。
        """
        raise NotImplementedError("%s 不支持挂单" % type(self).__name__)

    async def cancel_order(self, order_id: str = "", client_id: str = "",
                           symbol: Optional[str] = None) -> bool:
        """撤单。`True` = 已不在挂着（含「本来就不在了」），`False` = 明确失败。"""
        raise NotImplementedError("%s 不支持撤单" % type(self).__name__)

    async def query_order(self, order_id: str = "", client_id: str = "",
                          symbol: Optional[str] = None
                          ) -> Optional[PendingOrder]:
        """查一张单的当前状态。查不到返回 None（不抛）。"""
        raise NotImplementedError("%s 不支持查单" % type(self).__name__)

    async def open_orders(self, symbol: Optional[str] = None
                          ) -> "List[PendingOrder]":
        """当前所有还挂着的单。

        `_sync_working_orders` 靠它做跨进程对账：不知道「我挂过什么」，
        重启后就会再挂一张 → 同一笔仓两个止损 → 后触发的那张把剩仓反向开出去。
        """
        raise NotImplementedError("%s 不支持查挂单列表" % type(self).__name__)

    #: ★ 这个执行器有没有挂单能力。
    #
    #   单独一个标记，而不是靠 `hasattr` 猜：基类现在**声明了**这四个方法
    #   （否则「纸面没有挂单」这种事只能等生产发现），于是 `hasattr` 恒为真，
    #   引擎就再也分不清「支持」和「继承了但没实现」——
    #   实测后果：现货执行器被当成支持挂单，启动对账抛
    #   `SpotExecutor 不支持查挂单列表`，写出一条假的 `orders_fetch_failed`，
    #   而哨兵正是靠「risk_events 为空」判断健康的（测试当场变红）。
    supports_pending: bool = False

    #: ★ 这个执行器能不能查成交历史（带 clientOrderId）。
    #
    #   只有实盘能查。判「用户手工开的仓」和「我们自己下的单丢了成交回报」
    #   必须靠它：两者在数据库里长得一模一样（库空、交易所有仓），
    #   唯一分得清的地方是交易所的 clientOrderId —— 我们下的单一律以
    #   `AI-` 开头，用户手机上下的是空字符串。
    #
    #   和 supports_pending 一样用**显式标记**而不是 `hasattr`：
    #   基类一旦声明了方法，hasattr 就恒为真，引擎再也分不清
    #   「支持」和「继承了但没实现」。
    supports_fill_history: bool = False

    async def recent_fills(self, hours: int = 168) -> list:
        """最近成交（每笔带 ``ts/side/qty/price/client_id``）。

        默认 168 小时（7 天）是币安 ``/fapi/v1/userTrades`` 的硬上限：
        ``startTime``/``endTime`` 跨度超过 7 天会直接 400。
        """
        raise NotImplementedError("%s 不支持查成交历史" % type(self).__name__)

    async def close(self) -> None:
        pass
