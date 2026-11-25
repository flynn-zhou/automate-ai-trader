# -*- coding: utf-8 -*-
"""只读探针：确认 /fapi/v2/positionRisk 真实返回里 `liquidationPrice`
字段存在、且 `position()` 那套解析能取到数（不下任何单）。"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

env = {}
for line in open("/etc/ai-trader/env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

key = env.get("AI_API_KEY", "")
sec = env.get("AI_API_SECRET", "")
sym = env.get("AI_SYMBOL", "BTCUSDT")
proxy = env.get("AI_HTTP_PROXY", "") or None
base = env.get("AI_FAPI_BASE", "https://fapi.binance.com")

q = "symbol=%s&timestamp=%d&recvWindow=5000" % (sym, int(time.time() * 1000))
sig = hmac.new(sec.encode(), q.encode(), hashlib.sha256).hexdigest()
url = "%s/fapi/v2/positionRisk?%s&signature=%s" % (base, q, sig)

opener = urllib.request.build_opener()
if proxy:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
try:
    data = json.loads(opener.open(req, timeout=15).read().decode())
except Exception as e:
    print("请求失败（不影响结论，字段名可静态核对）：%s" % e)
    data = None

if data:
    row = data[0] if isinstance(data, list) else data
    print("=== 真实返回里我们关心的字段 ===")
    for k in ("symbol", "positionAmt", "entryPrice", "liquidationPrice",
              "maintMargin", "unRealizedProfit", "leverage", "marginType"):
        print("  %-20s = %r" % (k, row.get(k, "<字段不存在>")))
    print("\n  liquidationPrice 存在？", "liquidationPrice" in row)
    print("  maintMargin 存在？     ", "maintMargin" in row)
    print("  unRealizedProfit 存在？", "unRealizedProfit" in row)
