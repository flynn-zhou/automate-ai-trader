# -*- coding: utf-8 -*-
"""探针 ②：确认 Algo 单的查询/撤销端点与字段（不猜，全部实测）。

要回答四个问题（引擎要靠它们判"还没挂 / 已挂 / 已成交"）：
  1. 单张查询走哪条路径？GET /fapi/v1/algoOrder?algoId= ？
  2. 按 clientAlgoId 能不能查回来？（重启认回同一张单靠它）
  3. 触发之后成交价在哪？algoStatus？actualOrderId？
  4. 撤销：DELETE /fapi/v1/algoOrder?algoId= 已确认可用

★ 探针单：触发价离现价 -20%，reduceOnly=true，跑完立刻撤掉。
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
    print("  %s" % (json.dumps(body, ensure_ascii=False)[:500]
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
    cid = "AI-pr2-%d" % int(time.time())
    print("现价 %.2f 手数 %.6f 触发 %.2f clientAlgoId=%s"
          % (px, qty, trig, cid))

    code, body = req("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL", "symbol": SYMBOL, "side": "SELL",
        "type": "STOP_MARKET", "quantity": qty, "triggerPrice": trig,
        "reduceOnly": "true", "workingType": "CONTRACT_PRICE",
        "clientAlgoId": cid})
    show("① 挂", code, body)
    algo_id = str((body or {}).get("algoId") or "") if isinstance(body, dict) else ""
    if not algo_id:
        print("!! 挂失败，后续跳过")
        return 1

    show("② 按 algoId 查 GET /fapi/v1/algoOrder",
         *req("GET", "/fapi/v1/algoOrder", {"algoId": algo_id}))
    show("③ 按 clientAlgoId 查 GET /fapi/v1/algoOrder",
         *req("GET", "/fapi/v1/algoOrder", {"clientAlgoId": cid}))
    show("④ clientAlgoId 放到 /fapi/v1/order（旧端点，预期失败）",
         *req("GET", "/fapi/v1/order", {"origClientOrderId": cid}))
    show("⑤ 撤 DELETE /fapi/v1/algoOrder",
         *req("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}))
    show("⑥ 撤销后再查（看状态字段变什么）",
         *req("GET", "/fapi/v1/algoOrder", {"algoId": algo_id}))
    show("⑦ openAlgoOrders 剩余", *req("GET", "/fapi/v1/openAlgoOrders",
                                        {"symbol": SYMBOL}))
    return 0


sys.exit(asyncio.get_event_loop().run_until_complete(main()))
