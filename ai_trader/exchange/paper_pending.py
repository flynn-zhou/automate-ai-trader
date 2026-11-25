"""纸面挂单簿：给定一根 5m K，判哪些单成交、按什么价成交、有没有同根歧义。

★ 为什么要单独一个模块，而不是塞进 `PaperExecutor`

  这是**唯一一处「纸面与实盘语义必须逐字对齐」的地方**：进场是提前挂的限价单，
  出场是止损市价单。如果这段逻辑和 `LiveExecutor.place_order` 的语义有偏差，
  纸面跑出来的样本就不是实盘会发生的样本 —— k-trader 的「回测赚、实盘亏」
  就是这么来的。

  所以成交判据写成一组纯函数（`_limit_fill` / `_stop_fill` / `_tp_fill`），
  不依赖行情源、不依赖时间、不依赖引擎，可以拿一根构造出来的 K 直接验。

★ 成交判据（用户 2026-11-17 拍板）

  「影线触及」：5m 的 high/low 碰到挂单价就成交，与真实挂单一致。
  不用「收盘穿越」—— 那会漏掉真实挂单能成交的单，让纸面样本偏乐观。

  但影线触及带来一个必须记账的副作用：**同一根 K 里，一张买单和一张卖单
  可能都够得到**。真实市场里这种事只可能先发生一边，模型分不清先后，
  所以这种根上所有成交都标 `ambiguous=True`，由调用方**单独记账**，
  不许当成正常成交混进统计（HANDOFF §23 的「同根歧义记账」同一条规矩）。

★ 与交易所语义对齐的细节

  1. **跳空**：限价买单挂在 P，若这根 K 开盘就低于 P，成交价是 `open`
     而不是 P（更优）。反过来止损单跳空会更差。这不是优化，是**必须**：
     用 P 成交等于凭空给了一个开盘不可能拿到的价格。
  2. **费率分两档**：挂单成交算 maker，触发式市价单算 taker。
     这和我给 `PricePlan` 建的成本模型是同一套口径
     （赢的一路走 maker、亏的一路走 taker + 滑点）。
  3. **reduce_only 单**不受方向限制：平多要 SELL，平空要 BUY。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .base import PendingOrder

#: 与 `LiveExecutor._PENDING_TYPES` 保持同一集合。
#: 两处不一致的话，纸面能挂的实盘挂不上 —— 那是最难查的一类分叉。
PENDING_TYPES = ("LIMIT", "STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET")

#: 触发式市价单（taker）；其余按 maker 收。
_TAKER_TYPES = ("STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET")


@dataclass
class PendingFill:
    """一根 K 上发生的一次成交。"""
    client_id: str
    side: str                # BUY / SELL
    qty: float
    price: float             # 实际成交价（已含跳空与滑点）
    fee: float
    order_type: str
    ts: int
    #: 同一根 K 里两个方向的单都够到了 → 先后分不清。
    #: ★ 这种成交必须单独记账，不许混进正常统计。
    ambiguous: bool = False
    #: gap = 跳空成交（成交价与挂单价不同）；touch = 正常触及。
    how: str = "touch"
    #: 这张单是不是只减仓 —— 账务那一侧要靠它做截断。
    reduce_only: bool = False


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class PendingBook:
    """内存里的挂单簿。只负责「挂 / 撤 / 查 / 用一根 K 结账」。

    不做仓位、不做账本、不碰数据库 —— 那些是 executor 与 trade_db 的事。
    这样它才能在单元测试里被一根构造出来的 K 直接驱动。
    """

    def __init__(self, fee_maker: float = 0.0002, fee_taker: float = 0.0005,
                 slippage_ticks: float = 0.0, symbol: str = "BTCUSDT"):
        self.fee_maker = float(fee_maker)
        self.fee_taker = float(fee_taker)
        self.slippage_ticks = float(slippage_ticks)
        self.symbol = symbol
        self._orders: Dict[str, PendingOrder] = {}
        self._seq = 0

    # ------------------------------------------------------------------ 写
    def place(self, side: str, qty: float, order_type: str = "LIMIT",
              price: Optional[float] = None,
              stop_price: Optional[float] = None,
              reduce_only: bool = False, client_id: str = "",
              working_type: str = "CONTRACT_PRICE",
              ts: int = 0) -> Tuple[PendingOrder, str]:
        """挂一张单。返回 `(订单, 错误说明)`，不抛异常（与 live 同契约）。"""
        order_type = (order_type or "").upper()
        side = (side or "").upper()
        if side not in ("BUY", "SELL"):
            return PendingOrder(status="REJECTED"), "方向非法：%r" % side
        if order_type not in PENDING_TYPES:
            return (PendingOrder(status="REJECTED"),
                    "不支持挂单类型 %r（只支持 %s）"
                    % (order_type, "/".join(PENDING_TYPES)))
        if order_type == "LIMIT" and not price:
            return PendingOrder(status="REJECTED"), "限价单必须给 price"
        if order_type != "LIMIT" and not stop_price:
            return PendingOrder(status="REJECTED"), "%s 必须给 stop_price" % order_type
        qty = _f(qty)
        if qty <= 0:
            return PendingOrder(status="REJECTED"), "数量必须为正：%r" % qty

        self._seq += 1
        cid = client_id or "paper-%d-%d" % (ts, self._seq)
        if cid in self._orders and self._orders[cid].working:
            # 同一个 client_id 不许挂两次：那会让「按 client_id 撤单」撤掉
            # 不是它想撤的那一张，而且对账时两边都认为自己有一张。
            return (PendingOrder(status="REJECTED"),
                    "client_id 已存在且还在挂着：%s" % cid)
        o = PendingOrder(
            order_id="P%d" % self._seq, client_id=cid, symbol=self.symbol,
            side=side, type=order_type, qty=qty,
            executed_qty=0.0,
            price=_f(price), stop_price=_f(stop_price),
            status="NEW", reduce_only=bool(reduce_only), update_time=int(ts))
        self._orders[cid] = o
        return o, ""

    def cancel(self, order_id: str = "", client_id: str = "") -> Tuple[bool, str]:
        o = self._find(order_id, client_id)
        if o is None:
            return False, "找不到这张单（order_id=%r client_id=%r）" % (
                order_id, client_id)
        if not o.working:
            return False, "这张单已经不在挂着（status=%s）" % o.status
        o.status = "CANCELED"
        return True, ""

    def query(self, order_id: str = "", client_id: str = "") -> Tuple[
            Optional[PendingOrder], str]:
        o = self._find(order_id, client_id)
        if o is None:
            return None, "找不到这张单（order_id=%r client_id=%r）" % (
                order_id, client_id)
        return o, ""

    def open_orders(self, symbol: Optional[str] = None) -> List[PendingOrder]:
        return [o for o in self._orders.values()
                if o.working and (symbol is None or o.symbol == symbol)]

    def all_orders(self) -> List[PendingOrder]:
        """含已终态的 —— 对账和审计要看「我挂过什么」，不只是「还挂着什么」。"""
        return list(self._orders.values())

    def _find(self, order_id: str, client_id: str) -> Optional[PendingOrder]:
        if client_id:
            return self._orders.get(client_id)
        for o in self._orders.values():
            if o.order_id == order_id:
                return o
        return None

    # ------------------------------------------------------------------ 结账
    def on_bar(self, bar: Any) -> List[PendingFill]:
        """用一根 5m K 给所有还挂着的单结账，返回这一根上的全部成交。

        `bar` 只需要有 `ts / o / h / l / c`。
        处理顺序：先把这一根里**会成交的单全部找出来**，判断是否存在
        买卖两侧同时成交（同根歧义），再统一置终态 —— 不许边判边改状态，
        否则「卖出平多」会把「买入开多」的那张单的先决条件改掉。
        """
        ts = int(_f(getattr(bar, "ts", 0)))
        o, h = _f(getattr(bar, "o", 0)), _f(getattr(bar, "h", 0))
        lo, c = _f(getattr(bar, "l", 0)), _f(getattr(bar, "c", 0))
        if h <= 0 or lo <= 0:
            return []
        if o <= 0:
            o = c

        hits: List[Tuple[PendingOrder, float, str]] = []
        for od in self.open_orders():
            got = self._judge(od, o=o, h=h, lo=lo)
            if got is not None:
                hits.append((od, got[0], got[1]))
        if not hits:
            return []

        sides = {od.side for od, _, _ in hits}
        ambiguous = len(sides) > 1

        fills: List[PendingFill] = []
        for od, px, how in hits:
            fill_px = self._apply_slippage(od, px)
            rate = self.fee_taker if od.type in _TAKER_TYPES else self.fee_maker
            already = _f(od.executed_qty)
            qty = max(0.0, _f(od.qty) - already)
            if qty <= 0:
                continue
            fills.append(PendingFill(
                client_id=od.client_id, side=od.side, qty=qty,
                price=fill_px, fee=fill_px * qty * rate,
                order_type=od.type, ts=ts, ambiguous=ambiguous, how=how,
                reduce_only=bool(od.reduce_only)))
            od.executed_qty = _f(od.qty)
            od.avg_price = fill_px
            od.status = "FILLED"
            od.update_time = ts
        return fills

    @staticmethod
    def _judge(od: PendingOrder, *, o: float, h: float,
               lo: float) -> Optional[Tuple[float, str]]:
        """这张单在这根 K 上成交吗？成交价与方式。返回 None = 不成交。"""
        typ, side = od.type, od.side

        if typ == "LIMIT":
            px = _f(od.price)
            if px <= 0:
                return None
            if side == "BUY" and lo <= px:
                # 跳空低开：开盘就比挂单价更优，按开盘价成交
                return (min(px, o), "touch" if o >= px else "gap")
            if side == "SELL" and h >= px:
                return (max(px, o), "touch" if o <= px else "gap")
            return None

        trig = _f(od.stop_price)
        if trig <= 0:
            return None
        take_profit = typ == "TAKE_PROFIT_MARKET"
        # ★ 触发方向不能按买卖看，要按「价格是往上穿还是往下穿」看：
        #
        #     买单止损 / 卖单止盈  → 价格**上破**触发 → 成交至少是触发价
        #     卖单止损 / 买单止盈  → 价格**下破**触发 → 成交至多是触发价
        #
        #   按买卖定方向是错的：卖单止盈是「持多、涨到目标卖出」，
        #   它是**上破**触发，成交价应为 `max(trig, open)`。
        #   第一版按买卖写，结果卖单止盈在 105 触发却成交在 100（对持仓更有利）
        #   —— 这种错不会让测试变红得很明显，只会让纸面样本静默变好看。
        up_trigger = (side == "BUY") != take_profit
        if up_trigger:
            if h < trig:
                return None
            return (max(trig, o), "gap" if o > trig else "touch")
        if lo > trig:
            return None
        return (min(trig, o), "gap" if o < trig else "touch")

    def _apply_slippage(self, od: PendingOrder, px: float) -> float:
        """触发式市价单滑点：**永远更差**，方向按买卖定。"""
        if od.type not in _TAKER_TYPES:
            return px
        s = max(0.0, self.slippage_ticks)
        if s <= 0:
            return px
        return px + s if od.side == "BUY" else px - s
