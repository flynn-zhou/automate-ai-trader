# -*- coding: utf-8 -*-
"""扩大样本：用 2000 根 4h（约 333 天）验现货/合约信号是否分歧。

并且专门统计「价格离 SMA200 很近」（<0.1%）的那些根 ——
那才是分歧真正可能发生的窗口。
"""
import json, os, sys, urllib.request, time
sys.path.insert(0, os.getcwd())
PROXY = os.environ.get("AI_HTTP_PROXY") or ""
opener = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
          if PROXY else urllib.request.build_opener())

def fetch(base, path, n):
    out = {}
    cur = int(time.time() * 1000) - (n + 5) * 4 * 3600 * 1000
    got = 0
    while got < n:
        lim = min(1000, n - got)
        q = "symbol=BTCUSDT&interval=4h&limit=%d&startTime=%d" % (lim, cur)
        req = urllib.request.Request(base + path + "?" + q, headers={"User-Agent": "p/1"})
        with opener.open(req, timeout=25) as r:
            rows = json.loads(r.read())
        if not rows:
            break
        for x in rows:
            out[int(x[0])] = float(x[4])
        got += len(rows)
        cur = int(rows[-1][0]) + 4 * 3600 * 1000
        if len(rows) < lim:
            break
    return out

spot = fetch("https://api.binance.com", "/api/v3/klines", 2000)
fut  = fetch("https://fapi.binance.com", "/fapi/v1/klines", 2000)
common = sorted(set(spot) & set(fut))
print("共同 4h 根数:", len(common), "=%.0f 天" % (len(common) * 4 / 24.0))

# 增量维护 SMA，避免 O(n^2)
def run(src):
    above, sma, px = [], [], []
    s = 0.0
    cl = [src[t] for t in common]
    for i, c in enumerate(cl):
        # SMA 不含当前根：用 cl[i-200:i]
        if i >= 201:
            s_now = sum(cl[i-201:i]) / 200.0
            above.append(cl[i] > s_now)
            sma.append(s_now)
            px.append(cl[i])
        else:
            above.append(None); sma.append(None); px.append(None)
    return above, sma, px

a_spot, s_spot, p_spot = run(spot)
a_fut,  s_fut,  p_fut  = run(fut)

mism = 0
tot = 0
near = 0
near_mism = 0
for i in range(len(common)):
    if a_spot[i] is None or a_fut[i] is None:
        continue
    tot += 1
    if a_spot[i] != a_fut[i]:
        mism += 1
    # 离均线多近（现货口径）
    if s_spot[i]:
        dist = abs(p_spot[i] - s_spot[i]) / s_spot[i] * 100
        if dist < 0.1:
            near += 1
            if a_spot[i] != a_fut[i]:
                near_mism += 1
print("above 判定分歧: %d / %d (%.4f%%)" % (mism, tot, mism * 100.0 / tot if tot else 0))
print("离均线 <0.1%% 的根数: %d，其中分歧 %d" % (near, near_mism))
