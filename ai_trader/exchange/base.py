"""执行器接口。paper 与 live 必须返回同一种结构，否则策略层必然分叉。

k-trader 的教训：纸面/回测/实盘走了三条代码路径，最后"回测赚、实盘亏"无法归因。
这里强制同一个接口、同一份持仓表、同一个 PnL 公式。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FillResult:
    ok: bool
    price: float = 0.0
    qty: float = 0.0
    fee: float = 0.0
    order_id: str = ""
    status: str = "rejected"          # filled / rejected / unknown
    reason: str = ""


class Executor:
    name = "base"

    async def equity(self) -> dict:
        """返回 {balance, unrealized, equity, position_qty, position_side}"""
        raise NotImplementedError

    async def market_order(self, side: str, qty: float, reason: str = "",
                           reduce_only: bool = False) -> FillResult:
        raise NotImplementedError

    async def close(self) -> None:
        pass
