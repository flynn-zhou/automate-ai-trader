"""多周期 K 线聚合器（纯逻辑、无 IO、可单测）。

与 k-trader 的关键差别：
- k-trader 只把 3s 价聚成 3m。这里 3s 桶是原子事实，1m/3m/15m 全部由同一份 3s 桶聚合，
  所有周期天然一致，不存在两套数据源对不上的问题。
- 每个桶除 OHLC 外还带 buy_base/sell_base/trades：主动买卖力量来自 aggTrade 的 maker 标志，
  这是 REST 轮询永远拿不到的信息。
- 跨桶时返回上一条已收盘的桶，调用方据此落库 + 推送。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Bar:
    interval_ms: int
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    quote_volume: float = 0.0
    trades: int = 0
    buy_base: float = 0.0
    sell_base: float = 0.0
    closed: bool = False

    def as_dict(self) -> dict:
        return {
            "i": self.interval_ms,
            "t": self.open_time,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": round(self.volume, 6),
            "q": round(self.quote_volume, 2),
            "n": self.trades,
            "b": round(self.buy_base, 6),
            "s": round(self.sell_base, 6),
            "x": self.closed,
        }


def bucket_start(ts_ms: int, interval_ms: int) -> int:
    """时间戳归到桶起点，与交易所 UTC 边界一致。"""
    return (int(ts_ms) // int(interval_ms)) * int(interval_ms)


class MultiAggregator:
    """把成交喂进 N 个周期，返回本次跨桶收盘的 bar。"""

    def __init__(self, intervals: List[int]):
        assert 3000 in intervals, "3s 必须是原子周期"
        self._intervals = sorted(intervals)
        self._cur: Dict[int, Bar] = {}

    def _apply(self, bar: Bar, price: float, vol: float, quote: float,
               is_buy: Optional[bool], n_trades: int) -> None:
        if price > bar.high:
            bar.high = price
        if price < bar.low:
            bar.low = price
        bar.close = price
        bar.volume += vol
        bar.quote_volume += quote
        bar.trades += n_trades
        if is_buy is True:
            bar.buy_base += vol
        elif is_buy is False:
            bar.sell_base += vol

    def feed(self, price: float, ts_ms: int, vol: float = 0.0, quote: float = 0.0,
             is_buy: Optional[bool] = None, n_trades: int = 1
             ) -> Tuple[List[Bar], List[Bar]]:
        """喂一个成交，返回 (已收盘 bar 列表, 本轮更新的 bar 列表)。

        ★ 畸形数据必须在这里挡掉。2026-11-02 实测 @trade 流里有
          {"p":"0","q":"0","X":"NA","st":1} 这种报文（732 条里 2 条）。
          一旦它成为某个桶的第一笔，bar 会以 open=0/low=0 创建，
          而 low 再也回不来（真实价格都 > 0），于是"最低价"永久错误——
          止损、波动率、结构低点全都建立在它之上。
          线上实测 32338 根 3s bar 里已有 8 根 low=0。
        """
        price = float(price)
        ts_ms = int(ts_ms)
        vol = float(vol or 0.0)
        if not (price > 0.0) or vol < 0.0:
            logger.warning("丢弃畸形成交：price=%r vol=%r ts=%r", price, vol, ts_ms)
            return [], []
        closed: List[Bar] = []
        updated: List[Bar] = []
        for iv in self._intervals:
            start = bucket_start(ts_ms, iv)
            cur = self._cur.get(iv)
            if cur is None or cur.open_time != start:
                if cur is not None:
                    cur.closed = True
                    closed.append(cur)
                cur = Bar(interval_ms=iv, open_time=start, open=price,
                          high=price, low=price, close=price)
                self._cur[iv] = cur
            self._apply(cur, price, vol, quote, is_buy, n_trades)
            updated.append(cur)
        return closed, updated

    def feed_price_only(self, price: float, ts_ms: int):
        """兜底路径：只有价格没有成交量（REST 轮询 / 心跳）。"""
        return self.feed(price, ts_ms, 0.0, 0.0, None, 0)

    def current(self, interval_ms: int) -> Optional[Bar]:
        return self._cur.get(interval_ms)

    def current_price(self) -> Optional[float]:
        b = self._cur.get(3000)
        return b.close if b else None
