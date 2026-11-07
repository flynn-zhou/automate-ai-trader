"""REST 兜底：WS 断流时仍能拿到价格与历史 K 线。

k-trader 生产 WS 长期不通，靠的就是这条 REST 3s 轮询路径。新项目把它当"确定性底座"，
WS 只做加速——这个架构选择直接抄。
"""
from __future__ import annotations

import time
from typing import Optional

import aiohttp


class RestMarket:
    def __init__(self, base: str, symbol: str, proxy: str = ""):
        self._base = base.rstrip("/")
        self._symbol = symbol
        self._proxy = proxy or None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: Optional[dict] = None):
        s = await self._sess()
        async with s.get(self._base + path, params=params, proxy=self._proxy) as r:
            if r.status != 200:
                raise RuntimeError("HTTP %s %s" % (r.status, path))
            return await r.json()

    async def price(self) -> float:
        d = await self._get("/fapi/v1/ticker/price", {"symbol": self._symbol})
        return float(d["price"])

    async def klines(self, interval: str, limit: int = 500) -> list:
        return await self._get("/fapi/v1/klines",
                               {"symbol": self._symbol,
                                "interval": interval, "limit": limit})

    async def agg_trades(self, from_id: int = 0, limit: int = 1000) -> list:
        """拉 aggTrades。实测 BTCUSDT 约 5.6 笔/秒 => 3s 一轮 20 笔左右，
        1000 笔容量可覆盖 ~3 分钟断流，正常轮询永不丢笔。

        返回统一口径：[{id, ts, price, qty, is_buy}]，is_buy 由 m 标志换算
        （m=true 表示买方是 maker，即主动方为卖方）。
        """
        params = {"symbol": self._symbol, "limit": int(limit)}
        if from_id:
            params["fromId"] = int(from_id)
        rows = await self._get("/fapi/v1/aggTrades", params)
        return [{
            "id": int(r["a"]),
            "ts": int(r["T"]),
            "price": float(r["p"]),
            "qty": float(r["q"]),
            "is_buy": not bool(r["m"]),
        } for r in rows]

    async def server_time(self) -> int:
        d = await self._get("/fapi/v1/time")
        return int(d["serverTime"])

    async def latency_ms(self) -> int:
        t0 = time.time()
        await self.price()
        return int((time.time() - t0) * 1000)
