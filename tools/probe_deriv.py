# -*- coding: utf-8 -*-
"""先探查：币安给了哪些「对手的仓位」数据？没有这个就谈不上揣摩对手。"""
import json, time, urllib.request, urllib.error

def get(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"__err": e.code, "body": e.read().decode()[:150]}
    except Exception as e:
        return {"__err": str(e)[:100]}

now = int(time.time() * 1000)
probes = {
  "OI 历史(1h, 30天)": "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=500",
  "OI 历史(5m, 30天)": "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=500",
  "全局多空账户比(1h)": "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=500",
  "大户多空账户比(1h)": "https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=500",
  "大户多空持仓量比(1h)": "https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=1h&limit=500",
  "吃单买卖量比(5m)": "https://fapi.binance.com/futures/data/takerlongshortRatio?period=5m&limit=500&symbol=BTCUSDT",
  "吃单买卖量比(1h)": "https://fapi.binance.com/futures/data/takerlongshortRatio?period=1h&limit=500&symbol=BTCUSDT",
  "当前OI": "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
  "资金费(近期)": "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=10",
}
for name, url in probes.items():
    d = get(url)
    if isinstance(d, dict) and "__err" in d:
        print("%-22s 失败 %s" % (name, d))
        continue
    if not d:
        print("%-22s 空" % name); continue
    k = d[0] if isinstance(d, list) else d
    t0 = k.get("timestamp") or k.get("fundingTime") or 0
    tn = d[-1].get("timestamp") if isinstance(d, list) else 0
    span = ""
    if t0 and tn:
        span = "  跨度 %.1f 天" % ((tn - t0) / 86400000.0)
    print("%-22s OK  n=%d%s" % (name, len(d) if isinstance(d, list) else 1, span))
    print("     keys=%s" % list(k.keys())[:9])
