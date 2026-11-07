# -*- coding: utf-8 -*-
"""多品种 K 线采集：把订阅清单里「非交易品种」的行情也收进库里。

为什么单独一个采集器，而不是把 Feed 复制多份
--------------------------------------------
Feed 不只是「收行情」，它同时持有：逐笔 WS 健康、REST 兜底健康、各周期 K 线
在途桶、缺口检测、snapshot() 的形状。复制 N 份等于把所有这些状态乘以 N，
而引擎只读交易品种那一个——多出来的全是没人看的负担和出错面。

所以分工是：
    Feed            -> 交易品种，全速（3s 轮询 + 逐笔 WS），行为一行不改
    MultiKline      -> 其他订阅品种，慢速（默认 5 分钟拉一次 1h K 线），只求「有历史」

这解决的是「我需要能研究其他品种」这个真实需求，不是「同时在多个品种上开仓」。
引擎仍然只交易 cfg.trading.symbol。

已知边界（写下来，避免被当成 bug）
----------------------------------
- 只拉 K 线，不收逐笔。非交易品种不需要逐笔（那是给撮合与守卫用的）。
- 不做缺口检测。Feed 那套缺口逻辑是给交易品种保证连续性的，
  观察品种缺一两根不影响研究结论，补上检测反而要维护两套游标。
- 拉取失败只记一笔降级，不退出循环：一个品种挂了不能拖死其他品种。
"""
from __future__ import annotations

import asyncio
import logging
from collections import namedtuple

logger = logging.getLogger(__name__)

# 与 Feed 的 Bar 字段对齐，store.put_bar 只读这些属性
Bar = namedtuple("Bar", "interval_ms open_time open high low close volume "
                        "quote_volume trades buy_base sell_base")

KLINE_MS = {"1m": 60_000, "3m": 180_000, "15m": 900_000,
            "1h": 3_600_000, "4h": 14_400_000}


def parse_kline(row, interval_ms: int) -> Bar:
    """币安原始 K 线数组 -> Bar。

    原始顺序（官方）：
      [0]openTime [1]open [2]high [3]low [4]close [5]volume
      [6]closeTime [7]quoteVolume [8]trades [9]takerBuyBase [10]takerBuyQuote
    下标 6 是 closeTime 不是 quoteVolume——错位一格会把成交额写成时间戳，
    而那个值看起来也"像个大数字"，不会报错。
    """
    return Bar(interval_ms=interval_ms, open_time=int(row[0]),
               open=float(row[1]), high=float(row[2]), low=float(row[3]),
               close=float(row[4]), volume=float(row[5]),
               quote_volume=float(row[7]), trades=int(row[8]),
               buy_base=float(row[9]), sell_base=float(row[5]) - float(row[9]))


class MultiKlineCollector:
    """按订阅清单收非交易品种的 K 线。"""

    def __init__(self, cfg, store, subs, interval: str = "1h",
                 limit: int = 500, period_s: float = 300.0):
        if interval not in KLINE_MS:
            raise ValueError("不支持的周期: %r（可选 %s）" % (interval, sorted(KLINE_MS)))
        self._cfg = cfg
        self._store = store
        self._subs = subs
        self._interval = interval
        self._interval_ms = KLINE_MS[interval]
        self._limit = limit
        self._period = period_s
        self._rest = {}              # symbol -> RestMarket（懒建，避免空订阅时开连接）
        self._fail = {}              # symbol -> 连续失败次数
        self.collected = 0
        self.skipped = 0

    def symbols(self):
        """要额外采集的品种 = 订阅清单 - 交易品种（交易品种由 Feed 负责）。"""
        trading = getattr(self._cfg.trading, "symbol", None) or \
                  getattr(self._cfg.market, "symbol", None)
        return [s for s in self._subs.symbols() if s != trading]

    def _client(self, symbol: str):
        if symbol not in self._rest:
            from .rest import RestMarket
            self._rest[symbol] = RestMarket(
                self._cfg.market.fapi_base, symbol,
                proxy=getattr(self._cfg.market, "http_proxy", "") or "")
        return self._rest[symbol]

    async def collect_once(self) -> dict:
        """拉一轮。返回 {symbol: 写入根数}，便于探针与日志。"""
        out = {}
        for sym in self.symbols():
            try:
                rows = await self._client(sym).klines(self._interval, self._limit)
            except Exception as e:
                self._fail[sym] = self._fail.get(sym, 0) + 1
                # 只记降级，不中断：一个品种挂了不能拖死其他品种
                logger.warning("多品种采集失败 %s（连续 %d 次）: %s",
                               sym, self._fail[sym], str(e)[:120])
                self.skipped += 1
                continue
            self._fail[sym] = 0
            n = 0
            for r in rows:
                try:
                    self._store.put_bar(parse_kline(r, self._interval_ms), symbol=sym)
                    n += 1
                except Exception as e:
                    logger.warning("多品种 K 线解析失败 %s: %s", sym, str(e)[:100])
            out[sym] = n
            self.collected += n
            logger.info("多品种采集 %s %s：%d 根", sym, self._interval, n)
        return out

    async def run_forever(self) -> None:
        # 启动时先睡一小会儿，别和 Feed 的预热挤在同一秒打交易所
        await asyncio.sleep(5)
        while True:
            try:
                got = await self.collect_once()
                if got:
                    logger.info("多品种采集完成: %s", got)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("多品种采集轮次异常", exc_info=True)
            await asyncio.sleep(self._period)

    async def close(self) -> None:
        for c in self._rest.values():
            try:
                await c.close()
            except Exception:
                pass
        self._rest.clear()
