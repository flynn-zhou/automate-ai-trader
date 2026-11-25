# -*- coding: utf-8 -*-
"""探针 ③：把「Algo 单怎么查活/怎么查死/成交价在哪」一次问清。

前面已知：
  · POST /fapi/v1/algoOrder                      可用（algoType=CONDITIONAL）
  · DELETE /fapi/v1/algoOrder?algoId=            可用
  · GET /fapi/v1/openAlgoOrders?symbol=          可用（列表）
  · GET /fapi/v1/algoOrder?algoId=（不带 symbol）→ -2013

本轮要定的是：单查到底要怎么发？列表能不能替代单查？
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"


def _env():
    key = os.environ.get("AI_API_KEY", "")
    sec = os.environ.get("AI_API_SECRET", "")
    if not key or not sec:
        p = "/etc/ai-trader/env"
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line.startswith("AI_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("AI_API_SECRET="):
                    sec = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key, sec


KEY, SEC = _env()


def req(method, path, params=None):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    q = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(SEC.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = "%s%s?%s&signature=%s" % (BASE, path, q, sig)
    rq = urllib.request.Request(url, method=method,
                                headers={"X-MBX-APIKEY": KEY})
    try:
        with urllib.request.urlopen(rq, timeout=15) as r:
            raw, code = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:                                   # noqa: BLE001
        return 0, {"exc": str(e)[:200]}
    try:
        return code, json.loads(raw)
    except Exception:                                        # noqa: BLE001
        return code, raw[:300]


def show(tag, code, body):
    print("\n--- %s  (HTTP %s)" % (tag, code))
    print("  %s" % (json.dumps(body, ensure_ascii=False)[:460]
                    if isinstance(body, (dict, list)) else body))


async def main():
    code, pos = req("GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL})
    qty = 0.0
    if isinstance(pos, list) and pos:
        qty = abs(float(pos[0].get("positionAmt") or 0.0))
    code, tk = req("GET", "/fapi/v1/ticker/price", {"symbol": SYMBOL})
    px = float(tk.get("price") or 0.0)
    code, spec = req("GET", "/fapi/v1/exchangeInfo")
    tick = 0.1
    for s in (spec.get("symbols") or []):
        if s["symbol"] == SYMBOL:
            for f in s["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
    trig = round((px * 0.80) // tick * tick, 8)
    qty = qty or 0.002
    cid = "AI-pr3-%d" % int(time.time())

    code, body = req("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL", "symbol": SYMBOL, "side": "SELL",
        "type": "STOP_MARKET", "quantity": qty, "triggerPrice": trig,
        "reduceOnly": "true", "workingType": "CONTRACT_PRICE",
        "clientAlgoId": cid})
    show("① 挂", code, body)
    algo_id = str((body or {}).get("algoId") or "")
    if not algo_id:
        return 1
    print("  algoId=%s cid=%s" % (algo_id, cid))

    # 单查的各种形状
    show("② GET algoOrder 带 symbol",
         *req("GET", "/fapi/v1/algoOrder", {"algoId": algo_id,
                                            "symbol": SYMBOL}))
    show("③ GET algoOrder 用 clientAlgoId + symbol",
         *req("GET", "/fapi/v1/algoOrder", {"clientAlgoId": cid,
                                            "symbol": SYMBOL}))
    show("④ GET openAlgoOrders（列表，按 cid 找）",
         *req("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYMBOL}))
    show("⑤ GET historyAlgoOrders 形状",
         *req("GET", "/fapi/v1/historyAlgoOrders", {"symbol": SYMBOL,
                                                    "limit": 5}))
    show("⑥ 撤", *req("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}))
    show("⑦ 撤后 historyAlgoOrders（看 FINISHED/CANCELLED 长什么样）",
         *req("GET", "/fapi/v1/historyAlgoOrders", {"symbol": SYMBOL,
                                                    "limit": 5}))
    show("⑧ openAlgoOrders 剩余",
         *req("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYMBOL}))
    return 0


sys.exit(asyncio.get_event_loop().run_until_complete(main()))
