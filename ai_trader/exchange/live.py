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
from typing import List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp

from .base import Executor, FillResult, PendingOrder

logger = logging.getLogger(__name__)


class LiveError(RuntimeError):
    pass


class LiveExecutor(Executor):
    supports_pending = True
    supports_fill_history = True

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
            # 撤单必须用 DELETE。原来这里只有 GET / 其它，所有非 GET 请求都
            # 走 POST —— 撤单会被发成 "POST /fapi/v1/order?...&orderId=..."
            # 而币安对这个组合的解读是「新建一张市价单」，**不是撤单**。
            # 结果就是：想撤单却开了个反向仓，而且 HTTP 200 看不出来。
            fn = s.delete if method == "DELETE" else s.post
            async with fn(url + ("?" + body if method == "DELETE" and body else ""),
                          data=(None if method == "DELETE" else body),
                          headers=headers, proxy=self._proxy) as r:
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
        """真实持仓（唯一可信事实来源）。

        ★ 2026-11-24 BUS-D：补上 `liquidationPrice`（用户裁决「要」）。

        为什么这一行很重要：`position_guard` 的整套 P0 保命逻辑
        （"离爆仓还有几个止损宽度"）唯一的事实来源就是爆仓价。
        以前这里把它**读出来后丢掉**，于是守卫只能用配置推算出的
        `(1/杠杆 - 维持保证金)` 近似值 —— 而真实爆仓价还受逐仓/全仓、
        维持保证金率档位、未实现盈亏的影响，在极端行情下能和推算值差好几个点。

        推算值与真实值的差别不是学术问题：11-24 那次事故里，
        推算 4.0455% 而实测逐仓 4.15%、全仓 5.48%。
        用推算值判"还安全"的仓位，可能已经在真实爆仓线的危险一侧。

        `liquidationPrice` 在无仓时返回 "0"，这里统一成 float 0.0，
        调用方用 `> 0` 判有效性，不要用真假值判。

        ★ 字段是**实测**过的，不是照文档抄的。2026-11-24 用只读探针
          打了一次 `/fapi/v2/positionRisk`（真实账户，22x 逐仓），
          返回里确认存在的字段：
              symbol / positionAmt / entryPrice / liquidationPrice /
              unRealizedProfit / leverage / marginType
          而 **`maintMargin` 不存在** —— 所以这里不取它。
          （一度写过 `p.get("maintMargin", 0)`，那是一行永远返回 0 的
            死字段：看起来"守卫可以核对维持保证金"，实际上是凭空捏造。
            这类"装成数据源"的假字段比缺字段更危险。）
        """
        d = await self._req("GET", "/fapi/v2/positionRisk",
                            {"symbol": self.cfg.trading.symbol})
        for p in (d if isinstance(d, list) else [d]):
            amt = float(p.get("positionAmt", 0) or 0)
            if abs(amt) > 0:
                return {"qty": amt,
                        "entry": float(p.get("entryPrice", 0) or 0),
                        "side": "long" if amt > 0 else "short",
                        # ★ 真实爆仓价（标记价口径）。0 = 交易所未给出。
                        "liq_price": float(p.get("liquidationPrice", 0) or 0),
                        # 未实现盈亏：逐仓下它会影响真实爆仓价随浮盈移动
                        "unrealized": float(
                            p.get("unRealizedProfit", 0) or 0)}
        return {"qty": 0.0, "entry": 0.0, "side": "", "liq_price": 0.0,
                "unrealized": 0.0}

    async def mark_price(self) -> float:
        """交易所权威标记价（premiumIndex.markPrice）。

        **用途：记账与风控口径，不是止损复核。**

        ★ A2（2026-11-25 交易域审查）：这个 docstring 以前写着
          "引擎在触发止损时会用本方法复核一次" —— 那句话现在**是错的**，
          止损复核已经改用 `last_trade_price()`（见那个方法的说明）。
          为什么必须改：币安 STOP/STOP_MARKET 的触发口径是
          `workingType=CONTRACT_PRICE`（最新成交价），不是标记价；
          而本地触发价也来自成交。拿标记价去复核一张按成交价触发的止损，
          在快跌时会得出"本地已触发、交易所未触发"，于是判成"行情陈旧"
          并且**拒绝平仓** —— 恰好在最该平的那一秒。

          标记价仍是这里该用的东西：平仓记账要用权威价，
          不能让一个偏掉的成交价污染账本（`_adopted_close` 的兜底价用它）。

        端点选择：用 /fapi/v1/premiumIndex 而不是 /fapi/v1/ticker/price ——
        前者与强平/风控同一口径，剧烈波动时不会与真实结算价偏离。
        """
        d = await self._req("GET", "/fapi/v1/premiumIndex",
                            {"symbol": self.cfg.trading.symbol}, signed=False)
        px = float((d or {}).get("markPrice", 0) or 0)
        if px > 0:
            return px
        return float(getattr(self.feed, "last_price", 0.0) or 0.0)

    async def equity(self) -> dict:
        """账户权益快照。**顺带把真实爆仓价带回来。**

        ★ A3（2026-11-25 交易域审查）：这里本来就已经调了一次
          `position()`（为了拿 `position_qty` 做按账截断），而
          `position()` 会返回 `liquidationPrice`。也就是说：
          **每个 tick 都已经付过这次 HTTP 的钱了**，但爆仓价被丢掉，
          于是 P0（离爆仓还有几个止损宽度，唯一的保命判断）只能去
          读一个 5 分钟 TTL 的缓存。

          22 倍杠杆下这笔账很清楚：4.05% 的爆仓距离、1.6% 的止损，
          「爆仓价」在 5 分钟里能因为总权益变动而挪动好几个百分点
          （11-24 实测同一笔仓逐仓 4.15% / 全仓 5.48%）。
          最该新鲜的一个数字，反而是链上最旧的一个。

          所以这里把 `liq_price` 一起返回，调用方（`_step`）用它喂缓存，
          正常运行时再也不需要为爆仓价单独发一次请求。
        """
        d = await self._req("GET", "/fapi/v2/account")
        bal = float(d.get("totalWalletBalance", 0) or 0)
        unreal = float(d.get("totalUnrealizedProfit", 0) or 0)
        pos = await self.position()
        return {"balance": round(bal, 6), "unrealized": round(unreal, 6),
                "equity": round(bal + unreal, 6),
                "position_qty": pos["qty"], "position_side": pos["side"],
                # ★ 与 `position()["liq_price"]` 同一个来源、同一次请求。
                #   0 表示交易所未给出（无仓或该字段为空），调用方用 > 0 判有效。
                "liq_price": float(pos.get("liq_price") or 0.0)}

    async def last_trade_price(self) -> float:
        """交易所的**最新成交价**（`/fapi/v1/ticker/price`）。

        ★ A2（2026-11-25 交易域审查）：止损复核必须与触发价**同源**。

          触发用的是本地行情（`feed.agg.current(3000).close`，成交驱动）；
          复核原来用 `mark_price()`（`/fapi/v1/premiumIndex` 的
          markPrice —— 指数价 + 基差平滑）。两个不是同一个东西：

              快跌时 last 先到、mark 慢半拍，
              于是「本地触发、交易所未触发」→ 判成「行情陈旧」→
              **拒绝平仓**。而那正是最该平的那一秒。

          更关键的是：币安 STOP/STOP_MARKET 的默认触发口径就是
          `workingType=CONTRACT_PRICE` = **最新成交价**，不是标记价。
          用标记价去复核一张按最新成交价触发的止损，本身就对不上。

          所以复核也取最新成交价：同源之后，"两个价格不同"才真正
          等于"其中一个是陈旧的"。`mark_price()` 继续保留给记账兜底用
          （那里要的是权威价，不是可成交价）。
        """
        d = await self._req("GET", "/fapi/v1/ticker/price",
                            {"symbol": self.cfg.trading.symbol}, signed=False)
        px = float((d or {}).get("price", 0) or 0)
        if px > 0:
            return px
        # 端点拿不到就退回本地最后价 —— 调用方靠 0 判"无法确认"，
        # 所以这里只在本地也拿不到时才返回 0。
        return float(getattr(self.feed, "last_price", 0.0) or 0.0)

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
    async def _commission_of(self, order_id: str) -> float:
        """一笔订单的真实手续费合计（计价币）。拿不到返回 0，由调用方退回估算。

        为什么不能拿 avg*qty*费率代替：永续的 maker 是 0.02%、taker 是 0.05%，
        差 2.5 倍。用户手工挂的限价单成交是 maker，我们自己的市价单是 taker，
        同一笔持仓的开、平两侧费率不同，用单一费率估必然错。
        /fapi/v1/userTrades 是唯一给出真实 commission 的地方。

        手续费被折成别的币（BNB 抵扣）时返回 0 —— 我们不做汇率换算，
        宁可退回"按 taker 估算"（偏保守），也不编一个数。
        """
        try:
            rows = await self._req("GET", "/fapi/v1/userTrades",
                                   {"symbol": self.cfg.trading.symbol,
                                    "orderId": order_id})
        except Exception as e:
            logger.warning("[实盘] 查订单 %s 的手续费失败：%s", order_id, e)
            return 0.0
        if not isinstance(rows, list) or not rows:
            return 0.0
        quote = str((self._spec_cache or {}).get("quote") or "USDT")
        total = 0.0
        for t in rows:
            asset = str(t.get("commissionAsset") or "")
            if asset and asset != quote:
                logger.warning("[实盘] 订单 %s 的手续费以 %s 计价，不换算，退回估算",
                               order_id, asset)
                return 0.0
            try:
                total += float(t.get("commission", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        return abs(total)

    async def recent_fills(self, hours: int = 168) -> list:
        """最近成交，按**时间倒序**返回：``ts/side/qty/price/client_id``。

        为什么需要它（2026-11-19，硬停调查）：`reconcile_live()` 发现
        「交易所有仓、库里没有」时，有两种完全不同的成因，而在数据库里
        长得一模一样：

          1. **用户手工开的仓** —— §13.2 的约定是：接管、不判策略出场、
             只保留熔断止损。**不许硬停**，否则用户每在手机上开一次仓，
             系统就把自己停一次（mismatch 硬停永不自动解除）。
          2. **我们自己下的单，成交回报丢了** —— 这是真故障，
             我们不知道自己的敞口，必须硬停等人看。

        唯一分得清的地方是交易所的 `clientOrderId`：我们下的单一律以
        ``AI-`` 开头（见 ``_open``/``_close`` 的 ``AI-`` 前缀），
        用户在手机 App 上下的单这一栏是空字符串。所以这里把原始成交
        交回给引擎，由引擎按**错配差额**去归类（只看差额，不看整笔持仓，
        否则系统自己正常的成交会把人工仓一起污染成「我们的」）。

        ★ 7 天上限：币安 ``/fapi/v1/userTrades`` 的 ``startTime``/``endTime``
        跨度超过 7 天直接 400，所以 hours 被夹在 168 以内。
        代价是「开了超过 7 天、一直没被对账发现」的仓归类不出来，
        会落到 conservative 的 unknown → 仍然硬停。
        """
        hours = max(1, min(int(hours or 168), 168))
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - hours * 3600 * 1000
        rows = await self._req("GET", "/fapi/v1/userTrades",
                               {"symbol": self.cfg.trading.symbol,
                                "startTime": start_ms, "endTime": end_ms,
                                "limit": 1000})
        if not isinstance(rows, list):
            raise ValueError("userTrades 返回的不是列表：%r" % type(rows))
        out = []
        for t in rows:
            try:
                out.append({
                    "ts": int(t.get("time") or 0),
                    "side": str(t.get("side") or "").upper(),
                    "qty": float(t.get("qty") or 0),
                    "price": float(t.get("price") or 0),
                    "client_id": str(t.get("clientOrderId") or ""),
                })
            except (TypeError, ValueError):
                # 单笔字段异常不能让整次对账失明：丢掉这一笔，
                # 归类会因为「凑不出差额」落到 unknown，仍是保守方向。
                continue
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out

    async def _fill_truth(self, r: dict, px: float, want_qty: float):
        """取真实成交量 / 成交价 / 手续费；取不到就按保守口径补齐。

        为什么必须有这一步（第 129 轮，真金白银的 bug）：
        POST /fapi/v1/order 不指定 newOrderRespType 时，币安永续返回 ACK ——
        只有 orderId/symbol/status，executedQty 和 avgPrice 都是 "0"。于是

            filled = 0  ->  fee = abs(avg * 0) * taker_fee = 0

        【每一笔】永续成交的手续费都被记成 0，方向恒定地让亏损看起来更小。
        而 qty 侥幸没错，是因为 engine._close() 里有 `fill.qty > 0` 的兜底 ——
        fee 这一侧没有任何兜底。

        生产实证（11-12 两笔，账户 292U）：
            日志 "[实盘] sell 0.000000 @ 72479.4"        <- filled=0
            trades 里 fee=0.0，两笔记成 +0.33 / +3.63
            交易所 userTrades 真实 commission 合计 6.305
            真实净额 -3.4826 / +1.1251，合计 -2.3575
            账户权益变化 294.951699 -> 292.594233 = -2.357466，完全吻合
        库里凭手续费多记了 6.32U 的"利润"，占本金 2.2%。

        三步补齐，前一步拿不到就走下一步：
          1) POST 回报（已让 newOrderRespType=RESULT，正常情况下够了）
          2) GET /fapi/v1/order?orderId=   —— 权威成交结果
          3) GET /fapi/v1/userTrades?orderId= —— 真实 commission
        第 3 步拿不到才退回"按 taker 费率估算"。估算的方向是偏保守的
        （maker 0.02% 会被按 0.05% 记）：宁可把成本记高，也绝不能记成 0。
        """
        r = r or {}
        filled = float(r.get("executedQty", 0) or 0)
        avg = float(r.get("avgPrice", 0) or 0)
        cum = float(r.get("cumQuote", 0) or 0)
        oid = str(r.get("orderId", "") or "")

        if (filled <= 0 or avg <= 0) and oid:
            try:
                q = await self._req("GET", "/fapi/v1/order",
                                    {"symbol": self.cfg.trading.symbol, "orderId": oid})
                q = q or {}
                filled = float(q.get("executedQty", 0) or 0) or filled
                avg = float(q.get("avgPrice", 0) or 0) or avg
                cum = float(q.get("cumQuote", 0) or 0) or cum
            except Exception as e:
                logger.warning("[实盘] 补查订单 %s 的成交结果失败：%s", oid, e)

        if avg <= 0 and cum > 0 and filled > 0:
            avg = cum / filled
        if avg <= 0:
            avg = float(px or 0.0)

        fee = 0.0
        if oid and filled > 0:
            fee = await self._commission_of(oid)
        if fee <= 0:
            # 成交量也拿不到时，用【请求量】当基数。这是"这次成交大概付了多少"
            # 的估算，比记 0 强得多——记 0 等于假装交易免费。
            basis = filled if filled > 0 else float(want_qty or 0)
            fee = abs(avg * basis) * float(self.cfg.trading.taker_fee)
            if filled > 0:
                logger.warning("[实盘] 订单 %s 拿不到真实手续费，按 taker %.3f%% 估算 %.6f"
                               "（maker 成交会被高估，方向偏保守）",
                               oid, float(self.cfg.trading.taker_fee) * 100, fee)
        return filled, avg, fee

    # ---------------- 真实成交 / 资金费 / K 线（第 130 轮） ----------------
    #
    # 三个接口只为一件事：把「真实持仓那一单」还原出来。
    #
    # 为什么必须走交易所，不能用本地 trades 表
    # ----------------------------------------
    # 生产实证（11-11 09:04 → 11-12 22:57）：
    #   userTrades 25 笔，实现盈亏 +109.80，手续费 -16.92，资金费 -0.30 → 净 ≈ +92.59
    #   trades 表同区间 4 条记录                                      → 净 -20.46
    # 差 113U。trades 只记「引擎经手过的」仓，用户手工来回买卖的
    # 大多数根本没进库。所以真账只能从交易所拉。

    async def user_trades(self, days_back: int = 30,
                          max_pages: int = 10) -> list:
        """交易所真实成交（逐笔含 commission），按时间升序返回。

        为什么要分页往回收：不带 startTime 时币安只返回最近 7 天。
        只取 7 天会让跨越 7 天边界的回合缺掉前半段，
        还原出来的入场价是错的——而错得很安静，不会报错。

        每页最多 7 天（币安单次时间跨度上限），最多翻 max_pages 页。
        """
        sym = self.cfg.trading.symbol
        end = int(time.time() * 1000)
        floor = end - int(days_back) * 86400 * 1000
        out: list = []
        seen: set = set()
        for _ in range(max_pages):
            start = max(floor, end - 7 * 86400 * 1000)
            rows = await self._req("GET", "/fapi/v1/userTrades",
                                   {"symbol": sym, "startTime": start,
                                    "endTime": end, "limit": 1000})
            fresh = [r for r in (rows or []) if r.get("id") not in seen]
            for r in fresh:
                seen.add(r.get("id"))
            out = fresh + out
            if not fresh or start <= floor:
                break
            end = start - 1
        out.sort(key=lambda r: (int(r.get("time") or 0), int(r.get("id") or 0)))
        return [{
            "id": int(r.get("id") or 0),
            "order_id": str(r.get("orderId") or ""),
            "ts": int(r.get("time") or 0),
            "side": str(r.get("side") or "").upper(),
            "qty": abs(float(r.get("qty") or 0.0)),
            "price": float(r.get("price") or 0.0),
            "realized_pnl": float(r.get("realizedPnl") or 0.0),
            "commission": abs(float(r.get("commission") or 0.0)),
            "asset": r.get("commissionAsset") or "",
            "maker": bool(r.get("maker")),
        } for r in out]

    async def funding_income(self, days_back: int = 30,
                             max_pages: int = 10) -> list:
        """资金费（永续持仓每 8 小时一次）。负数表示付出。

        单独拉是因为它【不在】 userTrades 里：手续费是每笔成交的，
        资金费是按持仓时长扣的。只看手续费会漏掉这部分持仓成本。
        """
        sym = self.cfg.trading.symbol
        end = int(time.time() * 1000)
        floor = end - int(days_back) * 86400 * 1000
        out: list = []
        seen: set = set()
        for _ in range(max_pages):
            start = max(floor, end - 7 * 86400 * 1000)
            rows = await self._req("GET", "/fapi/v1/income",
                                   {"symbol": sym,
                                    "incomeType": "FUNDING_FEE",
                                    "startTime": start, "endTime": end,
                                    "limit": 1000})
            fresh = [r for r in (rows or [])
                     if (r.get("tranId"), r.get("time")) not in seen]
            for r in fresh:
                seen.add((r.get("tranId"), r.get("time")))
            out = fresh + out
            if not fresh or start <= floor:
                break
            end = start - 1
        return [{"ts": int(r.get("time") or 0),
                 "income": float(r.get("income") or 0.0)} for r in out]

    async def klines_1m(self, start_ms: int, end_ms: int,
                        max_pages: int = 4) -> list:
        """1m K 线，只用来算持仓期间的 MFE/MAE。

        公开端点、不需要签名。单次上限 1500 根，所以按 1500 分钟翻页——
        一单持仓通常几分钟到几小时，一般一次就够。
        """
        sym = self.cfg.trading.symbol
        out: list = []
        # ★ 起点必须【向下取整到分钟】，否则包含入场那一刻的那根 K 线会被漏掉。
        #
        # 币安 /fapi/v1/klines 的 startTime 是"只返回 openTime >= startTime 的"，
        # 而 openTime 永远落在整分钟上。所以只要入场时刻不是整分整秒，
        # 装着入场价的那根 K 线 openTime < startTime，就被排除了。
        #
        # 后果不是"精度差一点"：持仓 20 秒的单，窗口内一根整分钟 K 线都没有，
        # 接口返回空 → MFE/MAE 算不出来 → 那一单永远没有分数。
        # 而"持仓几十秒"正是用户手工来回买卖的典型形态，等于把最该被看见的
        # 那批单排除在分析之外。长单也系统性丢掉首尾两根。
        #
        # 生产实证（第 130 轮部署后）：30 单里唯一缺 K 线的就是那笔 20 秒的单。
        cur = (int(start_ms) // 60_000) * 60_000
        end_ms = int(end_ms)
        for _ in range(max_pages):
            if cur > end_ms:
                break
            rows = await self._req("GET", "/fapi/v1/klines",
                                   {"symbol": sym, "interval": "1m",
                                    "startTime": cur, "endTime": end_ms,
                                    "limit": 1500}, signed=False)
            if not rows:
                break
            out.extend(rows)
            last = int(rows[-1][0])
            if last <= cur:
                break
            cur = last + 60_000
        return [[int(r[0]), float(r[2]), float(r[3])] for r in out]

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
                  "type": "MARKET", "quantity": ("%.*f" % (spec["qty_precision"], qty)),
                  # ★ 不传这个，币安永续默认返回 ACK：executedQty/avgPrice 都是 "0"。
                  #   RESULT 才会带回真实成交量与成交均价（见 _fill_truth 的长注释）。
                  "newOrderRespType": "RESULT"}
        if reduce_only:
            params["reduceOnly"] = "true"
        try:
            r = await self._req("POST", "/fapi/v1/order", params)
        except LiveError as e:
            logger.error("[实盘] 下单失败 %s %.6f: %s", side, qty, e)
            return FillResult(ok=False, reason=str(e)[:200])

        filled, avg, fee = await self._fill_truth(r, px, qty)

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

        # fee 已在 _fill_truth 里取到【真实 commission】或保守估算。
        # 原来这里是 abs(avg * filled) * taker_fee —— filled 为 0 时直接归零，
        # 这就是 11-12 那两笔手续费整体消失的原因。
        logger.info("[实盘] %s %.6f @ %.1f 费 %.6f 持仓 %+.6f (%s)",
                    side, filled, avg, fee, after["qty"], reason[:40])
        return FillResult(ok=True, price=avg, qty=filled, fee=fee,
                          order_id=str(r.get("orderId", "")),
                          status="filled", reason=reason)

    # ---------------- 挂单（限价 / 止损） ----------------
    #
    # ★ 这一组方法**刻意不**复用 market_order 那条「发单 → sleep 0.35s →
    #   核对真实持仓 → 对不上就回滚平仓 + 熔断」的路。原因是判据不成立：
    #   未成交的挂单，持仓**本来就不该变**。用「持仓必须变」去核对，
    #   每一张正常挂单都会被判成异常，然后回滚 + 熔断 ——
    #   一个保护机制直接变成「一挂单就停机」，而且是永久停机。
    #
    #   挂单成交之后持仓怎么对账，属于启动对账（working_orders）的职责：
    #   重启时拉 openOrders + 真实持仓，两个方向都补齐。
    #
    #   仍然保留熔断语义：熔断期间不许用挂单开仓（否则熔断可被绕过），
    #   但放行 reduceOnly（止损单必须能挂上去，不然敞口没有上限）。

    _PENDING_TYPES = ("LIMIT", "STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET")

    @staticmethod
    def _as_pending(d: dict, fallback_client: str = "") -> PendingOrder:
        return PendingOrder(
            order_id=str(d.get("orderId", "") or ""),
            client_id=str(d.get("clientOrderId", "") or fallback_client),
            symbol=str(d.get("symbol", "") or ""),
            side=str(d.get("side", "") or ""),
            type=str(d.get("type", "") or ""),
            qty=float(d.get("origQty", 0) or 0),
            executed_qty=float(d.get("executedQty", 0) or 0),
            price=float(d.get("price", 0) or 0),
            stop_price=float(d.get("stopPrice", 0) or 0),
            avg_price=float(d.get("avgPrice", 0) or 0),
            status=str(d.get("status", "") or ""),
            reduce_only=bool(d.get("reduceOnly", False)),
            update_time=int(d.get("updateTime", 0) or 0),
        )

    def _round_px(self, px: float, spec: dict) -> float:
        tick = spec["tick"]
        if tick <= 0:
            return px
        return round(round(px / tick) * tick, spec["price_precision"])

    async def place_order(self, side: str, qty: float,
                          order_type: str = "LIMIT",
                          price: Optional[float] = None,
                          stop_price: Optional[float] = None,
                          reduce_only: bool = False,
                          client_id: str = "",
                          working_type: str = "CONTRACT_PRICE",
                          ) -> Tuple[PendingOrder, str]:
        """挂一张单。返回 (订单, 错误说明)。成功时错误说明为空串。

        不抛异常：策略层在「挂不上」和「挂上了」之间必须能区分处理，
        而且挂单失败不该让主循环崩 —— 崩了就等于策略停了但仓位还在。
        """
        self._maybe_clear_halt()
        if self.halted and not reduce_only:
            return PendingOrder(status="REJECTED"), "已熔断：" + self.halted
        if self.halted and reduce_only:
            logger.warning("[实盘] 熔断期间放行挂止损单：%s", self.halted[:80])

        order_type = (order_type or "").upper()
        side = (side or "").upper()
        if side not in ("BUY", "SELL"):
            return PendingOrder(status="REJECTED"), "方向非法：%r" % side
        if order_type not in self._PENDING_TYPES:
            return (PendingOrder(status="REJECTED"),
                    "不支持挂单类型 %r（只支持 %s）"
                    % (order_type, "/".join(self._PENDING_TYPES)))
        if order_type == "LIMIT" and not price:
            return PendingOrder(status="REJECTED"), "限价单必须给 price"
        if order_type != "LIMIT" and not stop_price:
            return PendingOrder(status="REJECTED"), "%s 必须给 stop_price" % order_type

        spec = await self.spec()
        qty = self.round_qty(qty, spec["step"])
        if qty <= 0:
            return (PendingOrder(status="REJECTED"),
                    "数量取整后为 0（step=%.6f）" % spec["step"])

        px_now = self.feed.last_price or 0.0
        if px_now and not reduce_only and qty * px_now < spec["min_notional"]:
            return (PendingOrder(status="REJECTED"),
                    "名义 %.2fU < 最小 %.2fU" % (qty * px_now, spec["min_notional"]))

        params = {"symbol": self.cfg.trading.symbol, "side": side,
                  "type": order_type,
                  "quantity": "%.*f" % (spec["qty_precision"], qty),
                  "newOrderRespType": "RESULT"}
        if order_type == "LIMIT":
            params["price"] = "%.*f" % (spec["price_precision"],
                                        self._round_px(float(price), spec))
            params["timeInForce"] = "GTC"
        if stop_price:
            params["stopPrice"] = "%.*f" % (spec["price_precision"],
                                            self._round_px(float(stop_price), spec))
            params["workingType"] = working_type
        if reduce_only:
            params["reduceOnly"] = "true"
        if client_id:
            params["newClientOrderId"] = client_id

        try:
            r = await self._req("POST", "/fapi/v1/order", params)
        except LiveError as e:
            logger.error("[实盘] 挂单失败 %s %s %.6f: %s", order_type, side, qty, e)
            return PendingOrder(status="REJECTED"), str(e)[:200]

        o = self._as_pending(r or {}, client_id)
        logger.info("[实盘] 挂单 #%s %s %s %.6f 价 %s 触发 %s → %s",
                    o.order_id, o.side, o.type, o.qty,
                    params.get("price", "-"), params.get("stopPrice", "-"),
                    o.status)
        return o, ""

    async def cancel_order(self, order_id: str = "", client_id: str = "",
                           symbol: Optional[str] = None) -> bool:
        """撤单。true = 撤掉了（或本来就不在了），false = 明确失败。

        「已经不在了」按成功处理：撤单的目的状态是「这张单不再挂着」，
        而已成交/已撤都满足它。为此要先查一次状态，否则分不清
        「本来就没成交在挂着」和「已经成交了」—— 后者绝不能当撤单成功。
        """
        if not order_id and not client_id:
            logger.error("[实盘] 撤单既没给 order_id 也没给 client_id")
            return False
        sym = symbol or self.cfg.trading.symbol
        params = {"symbol": sym}
        if order_id:
            params["orderId"] = str(order_id)
        else:
            params["origClientOrderId"] = client_id
        try:
            await self._req("DELETE", "/fapi/v1/order", params)
        except LiveError as e:
            cur = await self.query_order(order_id=order_id,
                                         client_id=client_id, symbol=sym)
            if cur is None or not cur.working:
                # 已经不挂了（成交/已撤）：目的达成
                logger.info("[实盘] 撤单时订单已不在挂单簿（%s），按成功处理",
                            order_id or client_id)
                return True
            logger.error("[实盘] 撤单失败 %s: %s", order_id or client_id, e)
            return False
        logger.info("[实盘] 已撤单 %s", order_id or client_id)
        return True

    async def open_orders(self, symbol: Optional[str] = None
                          ) -> List[PendingOrder]:
        """交易所里还挂着的单。启动对账靠它。"""
        sym = symbol or self.cfg.trading.symbol
        try:
            rows = await self._req("GET", "/fapi/v1/openOrders", {"symbol": sym})
        except LiveError as e:
            logger.error("[实盘] 拉挂单列表失败：%s", e)
            raise
        return [self._as_pending(d) for d in (rows or [])]

    async def query_order(self, order_id: str = "", client_id: str = "",
                          symbol: Optional[str] = None
                          ) -> Optional[PendingOrder]:
        """查一张单的当前状态。查不到返回 None（不抛）。"""
        if not order_id and not client_id:
            return None
        sym = symbol or self.cfg.trading.symbol
        params = {"symbol": sym}
        if order_id:
            params["orderId"] = str(order_id)
        else:
            params["origClientOrderId"] = client_id
        try:
            d = await self._req("GET", "/fapi/v1/order", params)
        except LiveError as e:
            logger.warning("[实盘] 查订单 %s 失败：%s", order_id or client_id, e)
            return None
        return self._as_pending(d or {}, client_id)
