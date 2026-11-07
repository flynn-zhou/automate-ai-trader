"""Binance 永续 aggTrade 逐笔 WebSocket（主路径）。

抄 k-trader 的两条铁律：
1) 自愈：断线 -> 指数退避 -> 重连 -> 重新订阅，全程有生命周期事件可观测。
2) 不裸奔：WS 只做加速，REST 3s 永远在跑，WS 断流自动降级且在看板上可见。

用 aiohttp 的 ws_connect（而非 websockets 库）以便统一 proxy 支持。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

OnTrade = Callable[[dict], Awaitable[None]]
OnLifecycle = Callable[[str, str], Awaitable[None]]


class AggTradeWS:
    """单品种 aggTrade 订阅，断线自愈。"""

    # 接受这两种事件：字段完全兼容（p 价、q 量、T 时间、m 是否买方为 maker）。
    # 为什么要两种：2026-11-02 实测这个环境下永续 @aggTrade 连得上但一条不推
    # （45 秒 0 条，同时 @trade 45 秒 195 条，用原生 aiohttp 直连复核过）。
    # 只认 aggTrade 的话，换流名会静默丢弃全部消息——表现和"WS 没数据"一模一样。
    TRADE_EVENTS = ("aggTrade", "trade")

    def __init__(self, base_url: str, symbol: str, on_trade: OnTrade,
                 on_lifecycle: Optional[OnLifecycle] = None,
                 proxy: str = "", max_backoff: float = 30.0,
                 stream: str = "aggTrade"):
        self._symbol = symbol.lower()
        self._stream = stream or "aggTrade"
        self._url = "%s/%s@%s" % (base_url.rstrip("/"), self._symbol, self._stream)
        self._on_trade = on_trade
        self._on_lifecycle = on_lifecycle
        self._proxy = proxy or None
        self._max_backoff = max_backoff
        self.last_msg_ts: int = 0        # 最后一次收到消息的本地时间（ms）
        self.connected: bool = False
        self.reconnects: int = 0
        # 被丢弃的畸形成交数。必须可观测：否则"流很干净"和
        # "脏数据被静默丢掉"看起来一模一样。
        self.dropped: int = 0
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def _emit(self, event: str, detail: str = "") -> None:
        if self._on_lifecycle:
            try:
                await self._on_lifecycle(event, detail)
            except Exception:
                logger.debug("lifecycle handler failed", exc_info=True)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="aggTradeWS")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def url(self) -> str:
        return self._url

    def _is_trade_event(self, d: dict) -> bool:
        """是不是一条可用的逐笔成交。两种事件名都收。"""
        return isinstance(d, dict) and d.get("e") in self.TRADE_EVENTS

    def silent_seconds(self) -> float:
        """距离上次收到消息（或连上）过去了多久。"""
        if not self.last_msg_ts:
            return 999.0
        return (int(time.time() * 1000) - self.last_msg_ts) / 1000.0

    def is_silent(self, threshold_s: float = 60.0) -> bool:
        """连线还在，但已经超过 threshold_s 没有数据。

        只看 connected 会把"连着但不推"当成健康——2026-11-02 的坑就是它：
        看板报 ws_connected=true，而那条流一个 tick 都没送过。
        断线是另一种故障，这里不算静默。
        """
        return bool(self.connected) and self.silent_seconds() > threshold_s

    def seconds_since_last_msg(self) -> float:
        return self.silent_seconds()

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._emit("connecting", self._url)
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(self._url, proxy=self._proxy,
                                               heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_receive=None)) as ws:
                        self.connected = True
                        self.last_msg_ts = int(time.time() * 1000)
                        backoff = 1.0
                        await self._emit("connected", "")
                        # 日志别写死 "aggTrade"：流名可配，日志要跟着走。
                        # 2026-11-02 出现过「日志说 aggTrade、实际连的是 @trade」。
                        logger.info("逐笔 WS 已连接 %s", self._url)
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                # last_msg_ts 必须在【确认这是一条有效成交之后】才刷新。
                                #
                                # 原来是收到任何报文就刷新，于是流里那些
                                # {"p":"0","q":"0","X":"NA"} 的环境噪声也算"活着"：
                                #   - is_silent() 永远返回 False；
                                #   - 真正该报的"连着但不推"被噪声盖住。
                                # 这正是本文件 L87 那段注释警告过的 2026-11-02 的坑
                                # （看板报 ws_connected=true，实际一个 tick 都没送），
                                # 当时的修复没有把这一处包含进去。
                                #
                                # 实测噪声比例约 1.4%（20 秒 137 条中含 2 条），
                                # 而它们每次都会让"静默"归零 —— 一分钟只要来一条，
                                # 静默检测就永久失效。
                                try:
                                    d = json.loads(msg.data)
                                except Exception:
                                    continue
                                if not self._is_trade_event(d):
                                    continue
                                # 畸形报文必须在边界丢弃：实测流里有
                                # {"p":"0","q":"0","X":"NA"} 这种环境噪声。
                                # 放进聚合器会造出 low=0 的 bar，
                                # 放进 last_price 会让决策看到价格 0。
                                try:
                                    _p = float(d["p"])
                                    _q = float(d["q"])
                                except (KeyError, TypeError, ValueError):
                                    continue
                                if not (_p > 0.0) or _q < 0.0:
                                    self.dropped += 1
                                    if self.dropped <= 5 or self.dropped % 100 == 0:
                                        logger.warning(
                                            "丢弃畸形成交(第 %d 条): p=%s q=%s X=%s",
                                            self.dropped, d.get("p"), d.get("q"),
                                            d.get("X"))
                                    continue
                                # 到这里才算"确实收到一条可用于行情的成交"。
                                # 静默检测只认这个时间戳，不认"收到过任何报文"。
                                self.last_msg_ts = int(time.time() * 1000)
                                # m=true 表示买方是 maker => 主动方是卖方
                                is_buy = not bool(d.get("m", False))
                                await self._on_trade({
                                    "ts": int(d.get("T") or d.get("E") or 0),
                                    "price": float(d["p"]),
                                    "qty": float(d["q"]),
                                    "is_buy": is_buy,
                                })
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("逐笔 WS 异常(%s): %s", self._stream, e)
            finally:
                was_connected = self.connected
                self.connected = False
                if was_connected:
                    self.reconnects += 1
                    await self._emit("disconnected", "")
                    logger.warning("逐笔 WS 断开(%s)，准备重连", self._stream)
            if self._stop.is_set():
                break
            await self._emit("reconnecting", "%.1fs" % backoff)
            await asyncio.sleep(backoff)
            backoff = min(self._max_backoff, backoff * 2)
