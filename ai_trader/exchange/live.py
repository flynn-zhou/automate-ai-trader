"""实盘执行器（币安 USDT-M 永续）。

设计原则只有一条：**不信任下单返回值，只信任交易所的持仓状态。**

k-trader 的教训是"下单接口返回成功"和"仓真的开了"是两件事：网络超时、
精度被拒、只减仓标志生效，都会让回报看起来成功。这里的做法是：
下单 -> 立刻查真实持仓 -> 和期望比对 -> 不符就立刻反向平掉并熔断。
钱在交易所里，唯一可信的事实是持仓查询的结果。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp

from .base import Executor, FillResult

logger = logging.getLogger(__name__)


class LiveError(RuntimeError):
    pass


class LiveExecutor(Executor):
    name = "live"

    def __init__(self, feed, cfg, api_key: str, api_secret: str,
                 base: str = "https://fapi.binance.com", proxy: str = ""):
        self.feed = feed
        self.cfg = cfg
        self._key = api_key
        self._secret = api_secret
        self._base = base.rstrip("/")
        self._proxy = proxy or None
        self._session: Optional[aiohttp.ClientSession] = None
        self._spec_cache = None
        self._lev_set = 0.0
        self.halted: str = ""          # 非空 = 已熔断，不再开仓
        # 熔断置位时刻 + 自动过期时长，理由与 spot.py 的 _maybe_clear_halt 完全一致：
        # 熔断只该拦开仓，不该拦平仓；否则一次瞬态核对失败就让止损失效，
        # 而 halted 没人清除，等于 7x24 运行必须靠人工重启才能恢复。
        self.halted_ts: float = 0.0
        self.halt_ttl_s: float = 600.0
        self._transport = None         # 测试注入点：(method, path, params, signed) -> dict

    # ---------------- HTTP ----------------
    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, params: dict) -> str:
        qs = urlencode(params)
        sig = hmac.new(self._secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return qs + "&signature=" + sig

    async def _req(self, method: str, path: str, params: Optional[dict] = None,
                   signed: bool = True):
        p = dict(params or {})
        body = ""
        headers = {}
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            p["recvWindow"] = 5000
            body = self._sign(p)
            headers = {"X-MBX-APIKEY": self._key}
        elif p:
            body = urlencode(p)
        # 允许注入假传输层：实盘逻辑必须能在没有真钱的情况下被测试
        if self._transport is not None:
            return await self._transport(method, path, p, signed)
        try:
            s = await self._sess()
            url = self._base + path
            if method == "GET":
                async with s.get(url + ("?" + body if body else ""),
                                 headers=headers, proxy=self._proxy) as r:
                    txt = await r.text()
                    if r.status != 200:
                        raise LiveError("HTTP %s %s: %s" % (r.status, path, txt[:300]))
                    return json.loads(txt)
            async with s.post(url, data=body, headers=headers,
                              proxy=self._proxy) as r:
                txt = await r.text()
                if r.status != 200:
                    raise LiveError("HTTP %s %s: %s" % (r.status, path, txt[:300]))
                return json.loads(txt)
        except LiveError:
            raise
        except Exception as e:
            raise LiveError("网络失败 %s: %s" % (path, e))

    # ---------------- 交易规格 ----------------
    async def spec(self) -> dict:
        """数量步长/最小数量/最小名义/价格步长。下错精度的单会被直接拒绝。"""
        if self._spec_cache:
            return self._spec_cache
        d = await self._req("GET", "/fapi/v1/exchangeInfo", signed=False)
        sym = self.cfg.trading.symbol
        for x in d.get("symbols", []):
            if x.get("symbol") != sym:
                continue
            f = {y["filterType"]: y for y in x.get("filters", [])}
            lot = f.get("LOT_SIZE", {})
            mkt = f.get("MARKET_LOT_SIZE", {})
            prc = f.get("PRICE_FILTER", {})
            notional = f.get("MIN_NOTIONAL", {})
            self._spec_cache = {
                "step": float(lot.get("stepSize") or mkt.get("stepSize") or 0.001),
                "min_qty": float(lot.get("minQty") or 0.001),
                "tick": float(prc.get("tickSize") or 0.1),
                "min_notional": float(notional.get("notional") or notional.get("minNotional") or 5.0),
                "qty_precision": int(x.get("quantityPrecision", 3)),
                "price_precision": int(x.get("pricePrecision", 1)),
                # 与 SpotExecutor.spec() 保持同一套键。
                # 少这两个键的实际代价：tools/preflight_live.py 里
                # spec["base"] 直接 KeyError，体检跑到「资金」一节就崩，
                # 而前面每一节都显示 GO —— 看起来像「几乎通过」。
                "base": x.get("baseAsset", "BTC"),
                "quote": x.get("quoteAsset", "USDT"),
            }
            return self._spec_cache
        raise LiveError("找不到交易对规格 %s" % sym)

    def round_qty(self, qty: float, step: float) -> float:
        """向下取整到步长。宁可小一点，也不能超——超了就是拒单。"""
        if step <= 0:
            return qty
        n = int(round(qty / step, 9) // 1)
        return round(n * step, 10)

    # ---------------- 账户与持仓 ----------------
    async def position(self) -> dict:
        """真实持仓（唯一可信事实来源）。"""
        d = await self._req("GET", "/fapi/v2/positionRisk",
                            {"symbol": self.cfg.trading.symbol})
        for p in (d if isinstance(d, list) else [d]):
            amt = float(p.get("positionAmt", 0) or 0)
            if abs(amt) > 0:
                return {"qty": amt, "entry": float(p.get("entryPrice", 0) or 0),
                        "side": "long" if amt > 0 else "short"}
        return {"qty": 0.0, "entry": 0.0, "side": ""}

    async def mark_price(self) -> float:
        """交易所权威标记价（premiumIndex.markPrice）。

        为什么必须有：引擎的熔断止损判的是【本地行情缓存价】，
        行情断线时它是陈旧的，用陈旧价误判会把健康仓位按市价卖掉，
        卖掉后要等下一次金叉才能回场（可能几周）。
        引擎在触发止损时会用本方法复核一次。

        注意端点选择：用 /fapi/v1/premiumIndex 而不是 /fapi/v1/ticker/price。
        前者返回真正的 markPrice（与强平/风控同一口径），
        后者是最近成交价，剧烈波动时会与标记价偏离。
        """
        d = await self._req("GET", "/fapi/v1/premiumIndex",
                            {"symbol": self.cfg.trading.symbol}, signed=False)
        px = float((d or {}).get("markPrice", 0) or 0)
        if px > 0:
            return px
        return float(getattr(self.feed, "last_price", 0.0) or 0.0)

    async def equity(self) -> dict:
        d = await self._req("GET", "/fapi/v2/account")
        bal = float(d.get("totalWalletBalance", 0) or 0)
        unreal = float(d.get("totalUnrealizedProfit", 0) or 0)
        pos = await self.position()
        return {"balance": round(bal, 6), "unrealized": round(unreal, 6),
                "equity": round(bal + unreal, 6),
                "position_qty": pos["qty"], "position_side": pos["side"]}

    async def set_leverage(self, lev: float) -> None:
        lev = max(1.0, min(float(lev), float(self.cfg.trading.max_leverage)))
        if abs(lev - self._lev_set) < 1e-9:
            return
        await self._req("POST", "/fapi/v1/leverage",
                        {"symbol": self.cfg.trading.symbol, "leverage": int(round(lev))})
        self._lev_set = lev
        logger.info("[实盘] 杠杆已设置为 %dx", int(round(lev)))

    def _maybe_clear_halt(self) -> None:
        """让熔断自己过期（与 spot.py 同款，理由见那里的长注释）。"""
        if not self.halted:
            return
        age = time.time() - (self.halted_ts or 0.0)
        if self.halted_ts and age >= self.halt_ttl_s:
            logger.warning("[实盘] 熔断已持续 %.0f 秒，自动解除后重试；原原因：%s",
                           age, self.halted[:120])
            self.halted = ""
            self.halted_ts = 0.0

    # ---------------- 下单（verify-first） ----------------
    async def market_order(self, side: str, qty: float, reason: str = "",
                           reduce_only: bool = False) -> FillResult:
        self._maybe_clear_halt()
        if self.halted and not reduce_only:
            return FillResult(ok=False, reason="已熔断：" + self.halted)
        if self.halted and reduce_only:
            # 熔断期间放行减仓。拒绝平仓的后果是止损失效、敞口无上限，
            # 比「账目暂时不准」危险得多；而且永续下单前已按真实持仓截断，
            # 放行减仓不会造成反向开仓。
            logger.warning("[实盘] 熔断期间放行减仓（%s）：%s", side, self.halted[:80])
        spec = await self.spec()
        qty = self.round_qty(qty, spec["step"])
        if qty <= 0:
            return FillResult(ok=False, reason="数量取整后为 0（step=%.6f）" % spec["step"])

        before = await self.position()
        px = self.feed.last_price or 0.0
        if px and not reduce_only and qty * px < spec["min_notional"]:
            return FillResult(ok=False,
                              reason="名义 %.2fU < 最小 %.2fU" % (qty * px, spec["min_notional"]))

        params = {"symbol": self.cfg.trading.symbol, "side": side.upper(),
                  "type": "MARKET", "quantity": ("%.*f" % (spec["qty_precision"], qty))}
        if reduce_only:
            params["reduceOnly"] = "true"
        try:
            r = await self._req("POST", "/fapi/v1/order", params)
        except LiveError as e:
            logger.error("[实盘] 下单失败 %s %.6f: %s", side, qty, e)
            return FillResult(ok=False, reason=str(e)[:200])

        filled = float(r.get("executedQty", 0) or 0)
        avg = float(r.get("avgPrice", 0) or 0) or (px or 0.0)

        # 关键一步：不信返回值，用真实持仓核对
        await asyncio.sleep(0.35)
        after = await self.position()
        signed = qty if side.lower() == "buy" else -qty
        expected = before["qty"] + signed
        tol = max(spec["step"], abs(expected) * 0.02)
        if abs(after["qty"] - expected) > tol:
            msg = ("持仓核对失败：期望 %+.6f，实际 %+.6f（下单前 %+.6f，成交回报 %.6f）"
                   % (expected, after["qty"], before["qty"], filled))
            logger.critical("[实盘] %s", msg)
            # 立刻把仓位拉回下单前的状态，然后熔断
            try:
                back = -after["qty"]
                if abs(back) > 0:
                    await self._req("POST", "/fapi/v1/order", {
                        "symbol": self.cfg.trading.symbol,
                        "side": "SELL" if back < 0 else "BUY", "type": "MARKET",
                        "quantity": "%.*f" % (spec["qty_precision"], abs(back)),
                        "reduceOnly": "true"})
            except Exception:
                logger.critical("[实盘] 回滚平仓也失败，需人工介入", exc_info=True)
            self.halted = msg
            self.halted_ts = time.time()
            return FillResult(ok=False, reason=msg)

        fee = abs(avg * filled) * self.cfg.trading.taker_fee
        logger.info("[实盘] %s %.6f @ %.1f 持仓 %+.6f (%s)",
                    side, filled, avg, after["qty"], reason[:40])
        return FillResult(ok=True, price=avg, qty=filled, fee=fee,
                          order_id=str(r.get("orderId", "")),
                          status="filled", reason=reason)
