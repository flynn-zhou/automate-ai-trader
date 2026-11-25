"""纸面执行器：本地撮合，价格取当前 3s 收盘价 + 滑点，费用按真实费率扣。

它的价值不是"模拟得像"，而是**让整条决策链路在没有真钱的情况下跑满**：
同样的信号、同样的风控、同样的持仓表、同样的 PnL 公式。
"""
from __future__ import annotations

import logging
import time

from .base import Executor, FillResult
from .paper_pending import PendingBook

logger = logging.getLogger(__name__)


class PaperExecutor(Executor):
    name = "paper"

    def __init__(self, feed, cfg):
        self.feed = feed
        self.cfg = cfg
        self.balance = float(cfg.trading.paper_balance)
        self.pos_qty = 0.0            # 正=多，负=空
        self.pos_entry = 0.0
        self._seq = 0

    def restore_from_db(self, tdb) -> None:
        """重启后自愈：余额与持仓必须从库里恢复，否则执行器与引擎会对不上。

        2026-11-01 的真事故：进程重启后 PaperExecutor 回到 100U 空仓，
        而引擎从 trades 表恢复出一笔持仓 —— 于是一边"持着仓"一边"权益恒为 100"，
        止损触发时会往一个实际不存在的仓位上发平仓单。实盘里这就是穿仓级错误。
        """
        self.tdb = tdb
        st = tdb.load_executor() if hasattr(tdb, "load_executor") else None
        if st:
            self.balance = float(st["balance"])
            self.pos_qty = float(st["pos_qty"])
            self.pos_entry = float(st["pos_entry"])
        else:
            # 老库没有 executor_state：用 trades 表重建。
            # 余额 = 起始资金 + 已实现净盈亏（pnl_net 里已经扣过手续费）。
            # 只算纸面交易的盈亏：纸面账户和真钱账户是两本账。
            # 不传 paper=1 会把实盘的盈亏也加进来，纸面余额直接失真。
            realized = (tdb.realized_pnl(paper=1)
                        if hasattr(tdb, "realized_pnl") else 0.0)
            self.balance = float(self.cfg.trading.paper_balance) + realized
            ot = tdb.open_trade() if hasattr(tdb, "open_trade") else None
            if ot:
                direction = 1 if ot["side"] == "long" else -1
                self.pos_qty = float(ot["qty"]) * direction
                self.pos_entry = float(ot["entry_price"])
        logger.info("[纸面] 状态恢复：余额=%.4f 持仓=%+.4f @ %.2f",
                    self.balance, self.pos_qty, self.pos_entry)
        self._save()          # 立刻落一条，下次重启就有确切状态可读

    def _save(self) -> None:
        tdb = getattr(self, "tdb", None)
        if tdb is None:
            return
        try:
            tdb.save_executor({"ts": int(time.time() * 1000), "balance": self.balance,
                               "pos_qty": self.pos_qty, "pos_entry": self.pos_entry})
        except Exception:
            logger.error("保存执行器状态失败", exc_info=True)

    def _price(self):
        b = self.feed.agg.current(3000)
        return b.close if b else self.feed.last_price

    def _slip(self, side: str, price: float) -> float:
        s = self.cfg.trading.slippage_bps / 10000.0
        return price * (1 + s) if side == "buy" else price * (1 - s)

    async def equity(self) -> dict:
        px = self._price() or 0.0
        unreal = 0.0
        if self.pos_qty and self.pos_entry:
            direction = 1 if self.pos_qty > 0 else -1
            unreal = (px - self.pos_entry) * self.pos_qty
            _ = direction
        return {"balance": round(self.balance, 6),
                "unrealized": round(unreal, 6),
                "equity": round(self.balance + unreal, 6),
                "position_qty": self.pos_qty,
                "position_side": "long" if self.pos_qty > 0 else ("short" if self.pos_qty < 0 else "")}

    async def market_order(self, side: str, qty: float, reason: str = "",
                           reduce_only: bool = False) -> FillResult:
        px = self._price()
        if not px:
            return FillResult(ok=False, reason="无价格")
        # reduce_only 必须真的执行【只减仓】，不能只是接住参数不用它。
        #
        # 第 109 轮实测缺陷：engine.py:_close 传的是 sell_qty = p.qty + dust
        # （1242 行），它敢多给一点，是因为注释里假定
        # 「执行器会再按 step 向下取整，所以这里多给一点是安全的，不会超卖」。
        # 这句话对 spot.py 成立（415 行按可用余额截断），对纸面不成立——
        # 于是纸面卖 0.00101 而只持 0.001 时，仓位翻成 -0.00001 的空仓，
        # 而且 pos_entry 还留着上一个多仓的均价。
        # 那是个真实交易所不可能出现的状态：equity() 会报「持有空仓」，
        # 止损按那个假均价算，决策日志写「持空」，整条样本被污染。
        #
        # ★ 2026-11-17：截断动作**下移进 `_apply_fill`**，因为挂单成交
        #   （平仓止损单）走的是另一条路径，那段路径同样需要它 ——
        #   两条路径各写一遍就是「同一件事两个来源」。
        fill_px = self._slip(side, px)
        # 费率必须按市场取：现货 0.10%，永续 0.05%。
        # 用单一 taker_fee 会让"配置是现货、纸面按永续收费"，
        # 于是纸面样本比现实便宜一半，不能用来印证回测。
        is_spot = getattr(self.cfg.trading, "exec_market", "spot") == "spot"
        fee = fill_px * qty * self.cfg.trading.taker_fee_for(is_spot)
        ok, qty, why = self._apply_fill(side, qty, fill_px, fee,
                                        reduce_only=reduce_only)
        if not ok:
            return FillResult(ok=False, reason=why)
        self._save()
        logger.info("[纸面] %s %.3f @ %.1f fee=%.4f 余额=%.4f 持仓=%.3f (%s)",
                    side, qty, fill_px, fee, self.balance, self.pos_qty, reason[:40])
        return FillResult(ok=True, price=fill_px, qty=qty, fee=fee,
                          order_id="PAPER-%d-%d" % (int(time.time()), self._seq),
                          status="filled", reason=reason)

    # ------------------------------------------------------------ 挂单
    #
    # ★ 2026-11-17：纸面以前**没有挂单能力**（见 `base.Executor` 顶部那段）。
    #   现在四个方法全部委托给 `PendingBook`，成交判据在那边用纯函数写死，
    #   引擎只在 5m 收盘时喂一根 K（`on_bar_5m`）。
    #
    #   ★ 成交后的记账**必须走 `_apply_fill`**，不许再抄一遍仓位/余额公式：
    #     「同一件事两个来源」是这个项目里反复出险的一类缺陷
    #     （HANDOFF §23 记了 7 次），挂单成交和市价成交更不能是两套。

    def _pending(self):
        if getattr(self, "_pbook", None) is None:
            t = self.cfg.trading
            is_spot = getattr(t, "exec_market", "spot") == "spot"
            self._pbook = PendingBook(
                fee_maker=t.fee_maker_for(is_spot),
                fee_taker=t.taker_fee_for(is_spot),
                slippage_ticks=0.0,
                symbol=getattr(t, "symbol", "BTCUSDT"))
        return self._pbook

    #: 纸面现在真的能挂单了（见 `paper_pending`）。
    supports_pending = True

    async def place_order(self, side: str, qty: float,
                          order_type: str = "LIMIT",
                          price=None, stop_price=None, reduce_only: bool = False,
                          client_id: str = "",
                          working_type: str = "CONTRACT_PRICE"):
        return self._pending().place(
            side=side, qty=qty, order_type=order_type, price=price,
            stop_price=stop_price, reduce_only=reduce_only,
            client_id=client_id, working_type=working_type,
            ts=int(time.time() * 1000))

    async def cancel_order(self, order_id: str = "", client_id: str = "",
                           symbol=None) -> bool:
        """★ 返回值跟实盘一致：`bool`，**不是** `(bool, 说明)` 元组。

        这里差点抄错：基类我第一版写成返回元组，而 `LiveExecutor` 返回
        `bool`，`engine._resolve_vanished` 直接 `if ok:` 用 —— 元组恒为真，
        「撤单失败」会被当成成功。所以对齐的是**实盘的签名**，不是我的想象。
        """
        ok, _why = self._pending().cancel(order_id=order_id,
                                          client_id=client_id)
        return ok

    async def query_order(self, order_id: str = "", client_id: str = "",
                          symbol=None):
        """返回 `PendingOrder | None` —— 与实盘同型（引擎判 `o.working`）。"""
        o, _why = self._pending().query(order_id=order_id,
                                        client_id=client_id)
        return o

    async def open_orders(self, symbol=None):
        """返回 `List[PendingOrder]` —— 与实盘同型（引擎直接 `len()` 它）。"""
        return self._pending().open_orders(symbol=symbol)

    def on_bar_5m(self, bar) -> list:
        """喂一根 5m K 给挂单簿结账，成交按同一套账务落地。

        返回 `[(PendingFill, FillResult)]`，调用方（引擎）拿它写 `working_orders`
        与成交记录。**同根歧义**（`fill.ambiguous`）一定要单独记账。
        """
        fills = []
        for f in self._pending().on_bar(bar):
            ok, qty, why = self._apply_fill(f.side.lower(), f.qty, f.price, f.fee,
                                            reduce_only=f.reduce_only)
            fills.append((f, FillResult(
                ok=ok, price=f.price, qty=qty if ok else 0.0,
                fee=f.fee if ok else 0.0, status="filled" if ok else "rejected",
                order_id=f.client_id, reason=why or f.order_type)))
        if fills:
            self._save()
        return fills

    def _apply_fill(self, side: str, qty: float, fill_px: float,
                    fee: float, reduce_only: bool = False):
        """把一笔成交落到仓位与余额上 —— **唯一的一份账务公式**。

        返回 `(成功?, 实际成交量, 说明)`。

        ★ `reduce_only` 的截断也放在这里（原来在 `market_order` 里）。
          挂单成交同样需要它：一张平多止损单不会自己知道仓位已经被别的路径
          平掉了，按原量成交就会把仓位翻到反方向 —— 这正是第 109 轮那个
          「纸面卖 0.00101 而只持 0.001」的真实形态。
        """
        if qty <= 0:
            return False, 0.0, "成交量必须为正"
        if reduce_only:
            held = abs(self.pos_qty)
            if held <= 1e-12:
                return False, 0.0, "reduce_only 但当前空仓，无仓可平"
            if qty > held:
                logger.warning("[纸面] reduce_only 截断：请求平 %.8f，实持 %.8f",
                               qty, held)
                qty = held
        self._seq += 1
        signed = qty if side == "buy" else -qty
        new_qty = self.pos_qty + signed
        # 开仓：记录均价；平仓：结算盈亏
        if self.pos_qty == 0 or (self.pos_qty > 0) == (signed > 0):
            # 同向加仓（v1 不支持，但保持公式正确）
            tot = abs(self.pos_qty) + qty
            self.pos_entry = (self.pos_entry * abs(self.pos_qty) + fill_px * qty) / tot if tot else 0.0
            self.pos_qty = new_qty
            self.balance -= fee
        else:
            close_qty = min(qty, abs(self.pos_qty))
            direction = 1 if self.pos_qty > 0 else -1
            pnl = (fill_px - self.pos_entry) * close_qty * direction
            self.balance += pnl - fee
            self.pos_qty = new_qty if abs(new_qty) > 1e-12 else 0.0
            if self.pos_qty == 0:
                self.pos_entry = 0.0
        return True, qty, ""
