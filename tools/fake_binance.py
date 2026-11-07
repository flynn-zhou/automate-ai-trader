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
        self.orders = []          # 收到的订单
        self.leverage = None      # 最后一次 set_leverage
        self.positions = {}       # perp: symbol -> {amt, entry}
        self.trades = []          # 现货成交历史（/api/v3/myTrades）
        self.calls = []           # 所有请求 (method, path)
        self.trades_available = True   # False = 模拟成交历史拿不到
        self.reject_next_order = None   # 注入失败用
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

    async def _perp_order(self, request):
        self.calls.append(("POST", "/fapi/v1/order"))
        raw = await request.text()
        err = self._verify(request, raw)
        if err:
            return web.json_response(err, status=400)
        p = dict(parse_qsl(raw))
        side = p["side"].upper()
        qty = float(p["quantity"])
        px = self.price
        self.orders.append({"market": "perp", "side": side, "qty": qty, "px": px})

        if self.reject_next_order:
            msg = self.reject_next_order
            self.reject_next_order = None
            return web.json_response({"code": -2010, "msg": msg}, status=400)

        pos = self.positions.setdefault("BTCUSDT", {"amt": 0.0, "entry": 0.0})
        signed = qty if side == "BUY" else -qty
        new_amt = pos["amt"] + signed
        if pos["amt"] == 0 or (pos["amt"] > 0) == (signed > 0):
            # 同向加仓：加权平均入场价
            total = abs(pos["amt"]) + abs(signed)
            pos["entry"] = ((pos["entry"] * abs(pos["amt"]) + px * abs(signed)) / total
                            if total else px)
        pos["amt"] = new_amt
        if abs(new_amt) < 1e-12:
            pos["amt"] = 0.0
            pos["entry"] = 0.0

        return web.json_response({
            "orderId": 2000 + len(self.orders), "symbol": "BTCUSDT",
            "status": "FILLED", "side": side, "type": "MARKET",
            "origQty": "%.3f" % qty, "executedQty": "%.3f" % qty,
            "avgPrice": "%.2f" % px, "cumQuote": "%.8f" % (qty * px),
            "updateTime": int(time.time() * 1000),
        })
