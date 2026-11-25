# -*- coding: utf-8 -*-
"""假币安：用真实响应格式跑通实盘代码路径。

动机：SpotExecutor / LiveExecutor 至今【一次都没有真正执行过】——
所有交易都是纸面的。等用户注资之后，第一次真实下单才会第一次
走到签名、下单、成交解析、余额核对这条链路。

而这个项目里所有致命 bug 都出现在"从未执行过的代码路径"上：
  attach() 漏了 stop_price 兜底（只在重启时执行）
  seed 失败导致静默 33 天（只在启动失败时执行）
  body 在每次 4h 收盘时拿到 bar.vol（只在收盘时执行）

所以我不能用真钱去试。做法是：起一个本地 HTTP 服务，
按币安【真实的响应字段】回应，让执行器走完整的 HTTP + 签名 + 解析链路。

关键点：签名必须真的校验。否则测的只是一个假传输层，
而"签名算错"正是那种上线才会暴露、且错误信息完全误导的故障。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, urlencode

from aiohttp import web

SPOT_SYMBOL = {
    "symbol": "BTCUSDT",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "baseAssetPrecision": 8,   # 实测值：币安现货 BTCUSDT 是 8，不是 6
    "filters": [
        {"filterType": "LOT_SIZE", "stepSize": "0.00001",
         "minQty": "0.00001", "maxQty": "9000.00000000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
    ],
}

PERP_SYMBOL = {
    "symbol": "BTCUSDT",
    "quantityPrecision": 3,
    "pricePrecision": 2,
    "filters": [
        {"filterType": "LOT_SIZE", "stepSize": "0.001",
         "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001"},
        {"filterType": "MIN_NOTIONAL", "notional": "50"},
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
    ],
}


class FakeBinance:
    """一个会真的算 HMAC、真的扣余额、真的改持仓的假交易所。"""

    def __init__(self, api_key="testkey", api_secret="testsecret",
                 usdt=100.0, btc=0.0, price=75000.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.usdt = usdt
        self.btc = btc
        self.price = price
        self.orders = []          # 收到的订单（追加日志，所有类型都进这里）
        self.leverage = None      # 最后一次 set_leverage
        self.positions = {}       # perp: symbol -> {amt, entry}
        self.trades = []          # 现货成交历史（/api/v3/myTrades）
        # ★ 永续成交历史（/fapi/v1/userTrades）。
        #   这个接口之前**根本不存在**，而 `LiveExecutor._commission_of`
        #   和 `user_trades()` 都要用它；缺了就是 404，而 404 会被引擎当成
        #   “真账同步失败”记进 risk_events —— 测试里看起来像真实故障。
        self.perp_trades = []
        self.calls = []           # 所有请求 (method, path)
        self.trades_available = True   # False = 模拟成交历史拿不到
        self.reject_next_order = None   # 注入失败用
        # ---- 挂单（限价 / 止损）----
        # 这一块原来完全没有：`_perp_order` 忽略 type、永远返回 FILLED 并立刻
        # 改持仓，所以「挂单」在测试里根本无法表达。没有它，
        # 「提前挂单、等价格来」这个机制一行都验不了。
        self.order_book = {}      # orderId(int) -> 订单状态
        self.fills = []           # 每一笔成交（含部分成交）
        self._seq = 0
        # 设了它，每次撮合只成交「剩余量 × 这个比例」，用来造 PARTIALLY_FILLED
        self.partial_fill_ratio = None
        self._runner = None
        self._site = None
        self.port = 0

    # ---------- 生命周期 ----------
    async def start(self):
        app = web.Application()
        app.router.add_get("/api/v3/exchangeInfo", self._spot_exchange_info)
        app.router.add_get("/api/v3/ticker/price", self._spot_price)
        app.router.add_get("/api/v3/account", self._spot_account)
        app.router.add_post("/api/v3/order", self._spot_order)
        app.router.add_get("/api/v3/myTrades", self._spot_my_trades)
        app.router.add_get("/fapi/v1/exchangeInfo", self._perp_exchange_info)
        app.router.add_get("/fapi/v1/premiumIndex", self._perp_mark)
        app.router.add_get("/fapi/v1/ticker/price", self._perp_price)
        app.router.add_get("/fapi/v2/account", self._perp_account)
        app.router.add_get("/fapi/v2/positionRisk", self._perp_position)
        app.router.add_post("/fapi/v1/order", self._perp_order)
        app.router.add_get("/fapi/v1/openOrders", self._perp_open_orders)
        app.router.add_get("/fapi/v1/userTrades", self._perp_user_trades)
        app.router.add_get("/fapi/v1/order", self._perp_query_order)
        app.router.add_delete("/fapi/v1/order", self._perp_cancel_order)
        app.router.add_delete("/fapi/v1/allOpenOrders",
                              self._perp_cancel_all)
        app.router.add_post("/fapi/v1/leverage", self._perp_leverage)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        for s in self._runner.addresses:
            self.port = s[1]
            break
        return self

    def base(self, spot=True):
        return "http://127.0.0.1:%d" % self.port

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    # ---------- 签名校验 ----------
    def _verify(self, request, raw_qs):
        # 真实币安对 POST 也接受把参数放在 query string 里。这里 body 为空时
        # 回落到 query —— 否则「POST 用 query 传参」会被误判成签名错误，
        # 那是个和签名完全无关的假故障，排查起来非常费劲。
        if not raw_qs:
            raw_qs = request.query_string or ""
        key = request.headers.get("X-MBX-APIKEY", "")
        if key != self.api_key:
            return {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
        parts = parse_qsl(raw_qs, keep_blank_values=True)
        signed_parts = [(k, v) for k, v in parts if k != "signature"]
        sig = dict(parts).get("signature")
        payload = urlencode(signed_parts)
        expect = hmac.new(self.api_secret.encode(), payload.encode(),
                          hashlib.sha256).hexdigest()
        if sig != expect:
            return {"code": -1022, "msg": "Signature for this request is not valid."}
        ts = dict(parts).get("timestamp")
        if ts and abs(int(time.time() * 1000) - int(ts)) > 5000:
            return {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}
        return None

    # ---------- 现货 ----------
    async def _spot_exchange_info(self, request):
        self.calls.append(("GET", "/api/v3/exchangeInfo"))
        return web.json_response({"timezone": "UTC", "serverTime": int(time.time() * 1000),
                                  "symbols": [SPOT_SYMBOL]})

    async def _spot_price(self, request):
        self.calls.append(("GET", "/api/v3/ticker/price"))
        return web.json_response({"symbol": "BTCUSDT", "price": "%.2f" % self.price})

    async def _spot_account(self, request):
        self.calls.append(("GET", "/api/v3/account"))
        err = self._verify(request, request.query_string)
        if err:
            return web.json_response(err, status=400)
        return web.json_response({
            "makerCommission": 10, "takerCommission": 10,
            "canTrade": True, "accountType": "SPOT",
            "balances": [
                {"asset": "BTC", "free": "%.8f" % self.btc, "locked": "0.00000000"},
                {"asset": "USDT", "free": "%.8f" % self.usdt, "locked": "0.00000000"},
            ],
        })

    async def _spot_order(self, request):
        self.calls.append(("POST", "/api/v3/order"))
        raw = await request.text()
        err = self._verify(request, raw)
        if err:
            return web.json_response(err, status=400)
        p = dict(parse_qsl(raw))
        side = p["side"].upper()
        qty = float(p["quantity"])
        px = self.price
        cost = qty * px
        fee = cost * 0.001
        self.orders.append({"market": "spot", "side": side, "qty": qty, "px": px})

        # --- 真实币安会拒的三种订单，这里也必须拒 ---
        #
        # 假交易所比现实宽容，验证的就是一个不存在的世界。
        # 这三种拒绝以前没实现，于是：
        #   - 下单量不是 step 整数倍 -> 真盘拒单，假盘照过
        #   - 名义 < 5 USDT          -> 真盘拒单，假盘照过
        #   - 数量 < minQty          -> 真盘拒单，假盘照过
        # 任何一条被漏掉，测试都会在"真盘上永远不会成交"的订单上显示绿灯。
        spec = SPOT_SYMBOL
        step = 0.0
        min_qty = 0.0
        min_notional = 0.0
        for f in spec["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                min_qty = float(f["minQty"])
            elif f["filterType"] == "NOTIONAL":
                min_notional = float(f["minNotional"])

        if step > 0:
            # 用整数比较避免浮点误差（0.00333 / 0.00001 = 332.999...）
            n_steps = qty / step
            if abs(n_steps - round(n_steps)) > 1e-6:
                return web.json_response(
                    {"code": -1111,
                     "msg": "Quantity %s is not a multiple of stepSize %s"
                            % (p["quantity"], step)},
                    status=400)
        if min_qty > 0 and qty < min_qty - 1e-12:
            return web.json_response(
                {"code": -1013,
                 "msg": "Quantity less than minQty %s" % min_qty},
                status=400)
        if min_notional > 0 and cost < min_notional - 1e-9:
            return web.json_response(
                {"code": -1013,
                 "msg": "Filter failure: NOTIONAL. minNotional is %s, order value %s"
                        % (min_notional, cost)},
                status=400)

        if self.reject_next_order:
            msg = self.reject_next_order
            self.reject_next_order = None
            return web.json_response({"code": -2010, "msg": msg}, status=400)

        # 真实币安：买入的手续费扣在【得到的币】上，卖出扣在收到的 USDT 上。
        # 旧版这里一律记成 USDT，比现实宽容——于是"拿扣费前数量当持仓"
        # 这个 bug 在假交易所上永远测不出来，真盘上一买就卖不掉。
        if side == "BUY":
            comm_amt, comm_asset = fee / px, "BTC"
        else:
            comm_amt, comm_asset = fee, "USDT"
        # ★ 成交记录必须在【余额检查通过之后】才写。
        #
        # 这里原来就是在余额检查之前写，于是一张被 -2010 拒掉的卖单
        # 也会出现在 /api/v3/myTrades 里。后果很隐蔽：
        #   tests/test_cost_basis.py 里"全部卖出后成本基准归零"那条用例，
        #   卖出其实【被拒了】，但 cost_basis 回放时看到了那张不存在的成交，
        #   于是算出净持仓 0 —— 测试是绿的，但通过的理由是错的。
        #   它验的不是"卖出后归零"，而是"假交易所帮我记了一笔没发生的成交"。
        #
        # 任何依赖 myTrades 的测试都会被这个宽容骗过去（对账、成本基准、
        # 边际统计）。假交易所比现实宽容，验证的就是一个不存在的世界。
        if side == "BUY":
            # 买入：USDT 付成交额（手续费不在这里扣），BTC 到账扣掉手续费
            if cost > self.usdt + 1e-9:
                return web.json_response(
                    {"code": -2010,
                     "msg": "Account has insufficient balance for requested action."},
                    status=400)
            self.usdt -= cost
            self.btc += qty - (fee / px)
        else:
            if qty > self.btc + 1e-9:
                return web.json_response(
                    {"code": -2010,
                     "msg": "Account has insufficient balance for requested action."},
                    status=400)
            self.btc -= qty
            self.usdt += cost - fee

        # 到这里才算真的成交，此时才写成交明细（见上面那段说明）
        self.trades.append({"symbol": "BTCUSDT", "id": 9000 + len(self.trades),
                            "orderId": 1000 + len(self.orders), "price": "%.8f" % px,
                            "qty": "%.8f" % qty, "quoteQty": "%.8f" % cost,
                            "commission": "%.8f" % comm_amt,
                            "commissionAsset": comm_asset,
                            "time": int(time.time() * 1000),
                            "isBuyer": side == "BUY", "isMaker": False})

        return web.json_response({
            "symbol": "BTCUSDT", "orderId": 1000 + len(self.orders),
            "clientOrderId": "fake", "transactTime": int(time.time() * 1000),
            "price": "0.00000000", "origQty": "%.8f" % qty,
            "executedQty": "%.8f" % qty,
            "cummulativeQuoteQty": "%.8f" % cost,
            "status": "FILLED", "timeInForce": "GTC", "type": "MARKET",
            "side": side, "fills": [{"price": "%.2f" % px, "qty": "%.8f" % qty,
                                     "commission": "%.8f" % (
                                         fee / px if side == "BUY" else fee),
                                     "commissionAsset":
                                         "BTC" if side == "BUY" else "USDT"}],
        })

    async def _spot_my_trades(self, request):
        self.calls.append(("GET", "/api/v3/myTrades"))
        err = self._verify(request, request.query_string)
        if err:
            return web.json_response(err, status=400)
        if not self.trades_available:
            return web.json_response({"code": -2014, "msg": "API-key format invalid."},
                                     status=400)
        return web.json_response(self.trades)

    # ---------- 永续 ----------
    async def _perp_exchange_info(self, request):
        self.calls.append(("GET", "/fapi/v1/exchangeInfo"))
        return web.json_response({"symbols": [PERP_SYMBOL]})

    async def _perp_mark(self, request):
        self.calls.append(("GET", "/fapi/v1/premiumIndex"))
        return web.json_response({"symbol": "BTCUSDT", "markPrice": "%.2f" % self.price,
                                  "indexPrice": "%.2f" % self.price,
                                  "lastFundingRate": "0.00010000"})

    async def _perp_price(self, request):
        self.calls.append(("GET", "/fapi/v1/ticker/price"))
        return web.json_response({"symbol": "BTCUSDT", "price": "%.2f" % self.price})

    async def _perp_account(self, request):
        self.calls.append(("GET", "/fapi/v2/account"))
        err = self._verify(request, request.query_string)
        if err:
            return web.json_response(err, status=400)
        pos = self.positions.get("BTCUSDT", {"amt": 0.0, "entry": 0.0})
        unreal = (self.price - pos["entry"]) * pos["amt"] if pos["amt"] else 0.0
        return web.json_response({
            "totalWalletBalance": "%.8f" % self.usdt,
            "totalUnrealizedProfit": "%.8f" % unreal,
            "totalMarginBalance": "%.8f" % (self.usdt + unreal),
            "availableBalance": "%.8f" % self.usdt,
        })

    async def _perp_position(self, request):
        self.calls.append(("GET", "/fapi/v2/positionRisk"))
        err = self._verify(request, request.query_string)
        if err:
            return web.json_response(err, status=400)
        pos = self.positions.get("BTCUSDT", {"amt": 0.0, "entry": 0.0})
        return web.json_response([{
            "symbol": "BTCUSDT", "positionAmt": "%.3f" % pos["amt"],
            "entryPrice": "%.2f" % pos["entry"],
            "markPrice": "%.2f" % self.price,
            "unRealizedProfit": "%.8f" % ((self.price - pos["entry"]) * pos["amt"]
                                          if pos["amt"] else 0.0),
            "leverage": str(self.leverage or 20),
        }])

    async def _perp_leverage(self, request):
        self.calls.append(("POST", "/fapi/v1/leverage"))
        raw = await request.text()
        err = self._verify(request, raw)
        if err:
            return web.json_response(err, status=400)
        p = dict(parse_qsl(raw))
        self.leverage = int(p["leverage"])
        return web.json_response({"symbol": "BTCUSDT", "leverage": self.leverage,
                                  "maxNotionalValue": "1000000"})

    # ---------- 挂单账本 ----------
    # 设计取舍：
    # * LIMIT 成交价取**挂单价**，不取当前价。价格穿过挂单价时真实成交只会
    #   更好或相等，取挂单价是保守下界 —— 回放不应该靠「假设成交在有利位置」
    #   来赚分。
    # * STOP_MARKET 触发后按**当前价**成交（它就是市价单）。
    # * 一根 bar 里如果有多次价格变动，撮合只在 set_price() 时发生，
    #   不试图还原 bar 内路径 —— 那是 K 线数据本身给不了的信息。

    def _new_order_id(self):
        self._seq += 1
        return 2000 + self._seq

    @staticmethod
    def _now_ms():
        return int(time.time() * 1000)

    def _order_json(self, o):
        return {
            "orderId": o["orderId"],
            "clientOrderId": o["clientOrderId"],
            "symbol": o["symbol"],
            "status": o["status"],
            "price": "%.2f" % o["price"],
            "avgPrice": "%.2f" % o["avgPrice"],
            "stopPrice": "%.2f" % o["stopPrice"],
            "origQty": "%.3f" % o["origQty"],
            "executedQty": "%.3f" % o["executedQty"],
            "cumQuote": "%.8f" % o["cumQuote"],
            "type": o["type"],
            "side": o["side"],
            "timeInForce": o["timeInForce"],
            "reduceOnly": o["reduceOnly"],
            "workingType": o["workingType"],
            "updateTime": o["updateTime"],
        }

    def _apply_fill(self, side, qty, px):
        """按成交改持仓（和原来 MARKET 那段完全同一个算法）。"""
        pos = self.positions.setdefault("BTCUSDT", {"amt": 0.0, "entry": 0.0})
        signed = qty if side == "BUY" else -qty
        if pos["amt"] == 0 or ((pos["amt"] > 0) == (signed > 0)):
            # 同向加仓：加权平均入场价
            total = abs(pos["amt"]) + abs(signed)
            if total:
                pos["entry"] = ((pos["entry"] * abs(pos["amt"])
                                 + px * abs(signed)) / total)
        pos["amt"] = pos["amt"] + signed
        if abs(pos["amt"]) < 1e-12:
            pos["amt"] = 0.0
            pos["entry"] = 0.0

    def _is_triggered(self, o, px):
        """当前价格有没有让这张单该成交。"""
        t = o["type"]
        if t == "LIMIT":
            return px <= o["price"] if o["side"] == "BUY" else px >= o["price"]
        if t in ("STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET"):
            sp = o["stopPrice"]
            if o["side"] == "BUY":
                return px >= sp          # 买入止损：价格涨到触发价
            return px <= sp              # 卖出止损：价格跌到触发价
        return False

    def _fill_order(self, o, px):
        remaining = o["origQty"] - o["executedQty"]
        ratio = self.partial_fill_ratio
        # 每次成交「原量 × 比例」，不是「剩余量 × 比例」。
        # 后者是几何衰减（0.5 → 0.25 → 0.125…），永远差一点填不满，
        # 而这个旋钮的用途只是造出 PARTIALLY_FILLED，不该有收敛问题。
        qty = remaining if ratio is None else min(remaining, o["origQty"] * ratio)
        if qty <= 1e-9:
            qty = remaining
        fill_px = o["price"] if o["type"] == "LIMIT" else px
        o["executedQty"] += qty
        o["cumQuote"] += qty * fill_px
        o["avgPrice"] = o["cumQuote"] / o["executedQty"]
        o["updateTime"] = self._now_ms()
        o["status"] = ("FILLED"
                       if o["executedQty"] >= o["origQty"] - 1e-12
                       else "PARTIALLY_FILLED")
        self._apply_fill(o["side"], qty, fill_px)
        self.fills.append({"orderId": o["orderId"], "side": o["side"],
                           "qty": qty, "px": fill_px,
                           "reduceOnly": o["reduceOnly"]})
        return qty

    def _match_orders(self):
        """按当前价撮合所有挂着的单。价格每次变动都该调一次。"""
        for o in list(self.order_book.values()):
            if o["status"] not in ("NEW", "PARTIALLY_FILLED"):
                continue
            if self._is_triggered(o, self.price):
                self._fill_order(o, self.price)

    def set_price(self, px, match=True):
        """推进价格；默认顺手撮合挂单。

        测试必须能推价格，否则「限价挂着不成交 → 价格走过去了才成交」
        这条最基本的路径没法验。
        """
        self.price = float(px)
        if match:
            self._match_orders()
        return self.price

    def _find_order(self, q):
        if "orderId" in q:
            try:
                return self.order_book.get(int(q["orderId"]))
            except (TypeError, ValueError):
                return None
        cid = q.get("origClientOrderId") or q.get("clientOrderId")
        if cid:
            for o in self.order_book.values():
                if o["clientOrderId"] == cid:
                    return o
        return None

    def _place_pending(self, p, side, otype, qty):
        """建一张挂单，返回 (json响应, http状态)。"""
        price = float(p.get("price") or 0.0)
        stop = float(p.get("stopPrice") or p.get("stop") or 0.0)
        if otype == "LIMIT" and price <= 0:
            return {"code": -1102, "msg": "Mandatory parameter 'price' was not sent."}, 400
        if otype in ("STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET") and stop <= 0:
            return ({"code": -1102,
                     "msg": "Mandatory parameter 'stopPrice' was not sent."}, 400)
        cid = p.get("newClientOrderId") or ""
        if cid:
            for o in self.order_book.values():
                if o["clientOrderId"] == cid:
                    return ({"code": -2010,
                             "msg": "Duplicate client order id: %s" % cid}, 400)
        o = {
            "orderId": self._new_order_id(),
            "clientOrderId": cid or ("fake-%d" % self._seq),
            "symbol": p.get("symbol", "BTCUSDT"),
            "side": side,
            "type": otype,
            "origQty": qty,
            "executedQty": 0.0,
            "price": price,
            "stopPrice": stop,
            "avgPrice": 0.0,
            "cumQuote": 0.0,
            "status": "NEW",
            "timeInForce": p.get("timeInForce", "GTC"),
            "reduceOnly": str(p.get("reduceOnly", "")).lower() == "true",
            "workingType": p.get("workingType", "CONTRACT_PRICE"),
            "updateTime": self._now_ms(),
        }
        self.order_book[o["orderId"]] = o
        # 已经能成交的（限价单挂进来时就是价内）立刻成交，和真实交易所一致
        if self._is_triggered(o, self.price):
            self._fill_order(o, self.price)
        return self._order_json(o), 200

    async def _perp_order(self, request):
        self.calls.append(("POST", "/fapi/v1/order"))
        raw = await request.text()
        err = self._verify(request, raw)
        if err:
            return web.json_response(err, status=400)
        p = dict(parse_qsl(raw))
        side = p["side"].upper()
        otype = (p.get("type") or "MARKET").upper()
        qty = float(p["quantity"])
        px = self.price
        self.orders.append({"market": "perp", "side": side, "qty": qty,
                            "px": px, "type": otype})

        if self.reject_next_order:
            msg = self.reject_next_order
            self.reject_next_order = None
            return web.json_response({"code": -2010, "msg": msg}, status=400)

        if otype != "MARKET":
            body, code = self._place_pending(p, side, otype, qty)
            return web.json_response(body, status=code)

        oid = self._new_order_id()
        self._apply_fill(side, qty, px)
        return web.json_response({
            "orderId": oid, "symbol": "BTCUSDT",
            "status": "FILLED", "side": side, "type": "MARKET",
            "origQty": "%.3f" % qty, "executedQty": "%.3f" % qty,
            "avgPrice": "%.2f" % px, "cumQuote": "%.8f" % (qty * px),
            "updateTime": self._now_ms(),
        })

    async def _perp_user_trades(self, request):
        """永续成交历史。

        支持 `orderId` 过滤（`_commission_of` 用它查单笔手续费）；
        不带 `orderId` 时返回全部（`user_trades()` 分页拉真账用）。
        """
        self.calls.append(("GET", "/fapi/v1/userTrades"))
        err = self._verify(request, request.query_string)
        if err:
            return web.json_response(err, status=400)
        if not self.trades_available:
            return web.json_response({"code": -2014, "msg": "API-key format invalid."},
                                     status=400)
        q = dict(parse_qsl(request.query_string))
        rows = list(self.perp_trades)
        if q.get("orderId"):
            rows = [r for r in rows if str(r.get("orderId")) == str(q["orderId"])]
        if q.get("startTime"):
            rows = [r for r in rows if int(r.get("time") or 0) >= int(q["startTime"])]
        if q.get("endTime"):
            rows = [r for r in rows if int(r.get("time") or 0) <= int(q["endTime"])]
        return web.json_response(rows)

    async def _perp_open_orders(self, request):
        self.calls.append(("GET", "/fapi/v1/openOrders"))
        err = self._verify(request, request.query_string)
        if err:
            return web.json_response(err, status=400)
        out = [self._order_json(o) for o in self.order_book.values()
               if o["status"] in ("NEW", "PARTIALLY_FILLED")]
        out.sort(key=lambda x: x["orderId"])
        return web.json_response(out)

    async def _perp_query_order(self, request):
        self.calls.append(("GET", "/fapi/v1/order"))
        err = self._verify(request, request.query_string)
        if err:
            return web.json_response(err, status=400)
        o = self._find_order(dict(parse_qsl(request.query_string)))
        if o is None:
            return web.json_response(
                {"code": -2013, "msg": "Order does not exist."}, status=400)
        return web.json_response(self._order_json(o))

    async def _perp_cancel_order(self, request):
        self.calls.append(("DELETE", "/fapi/v1/order"))
        err = self._verify(request, request.query_string)
        if err:
            return web.json_response(err, status=400)
        o = self._find_order(dict(parse_qsl(request.query_string)))
        # 真交易所：不存在的单、以及已成交/已撤的单，都回 -2011
        if o is None or o["status"] in ("FILLED", "CANCELED"):
            return web.json_response(
                {"code": -2011, "msg": "Unknown order sent."}, status=400)
        o["status"] = "CANCELED"
        o["updateTime"] = self._now_ms()
        return web.json_response(self._order_json(o))

    async def _perp_cancel_all(self, request):
        self.calls.append(("DELETE", "/fapi/v1/allOpenOrders"))
        err = self._verify(request, request.query_string)
        if err:
            return web.json_response(err, status=400)
        n = 0
        for o in self.order_book.values():
            if o["status"] in ("NEW", "PARTIALLY_FILLED"):
                o["status"] = "CANCELED"
                o["updateTime"] = self._now_ms()
                n += 1
        return web.json_response({"code": 200, "msg": "success", "n": n})
