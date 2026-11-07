# -*- coding: utf-8 -*-
"""探测：这几个「对手仓位」接口能翻多深的历史？"""
import json, time, urllib.request, urllib.error

def get(url):
    for a in range(5):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(8 * (a + 1)); continue
            return None
        except Exception:
            time.sleep(3)
    return None

for name, path in (
    ("globalLongShortAccountRatio", "globalLongShortAccountRatio"),
    ("topLongShortPositionRatio", "topLongShortPositionRatio"),
    ("openInterestHist", "openInterestHist"),
    ("takerlongshortRatio", "takerlongshortRatio"),
):
    print("=" * 70)
    print(name)
    for period in ("1h", "4h"):
        for start in ("2021-01-01", "2019-09-01"):
            st = int(time.mktime(time.strptime(start, "%Y-%m-%d")) * 1000)
            url = ("https://fapi.binance.com/futures/data/%s?symbol=BTCUSDT&period=%s"
                   "&limit=500&startTime=%d" % (path, period, st))
            d = get(url)
            if not d:
                print("  %-4s 起于 %s : 空" % (period, start)); continue
            t0 = d[0].get("timestamp")
            print("  %-4s 起于 %s : 拿到 n=%d，最早 %s" % (
                period, start, len(d),
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(t0/1000)) if t0 else "?"))
            break
        time.sleep(0.5)
