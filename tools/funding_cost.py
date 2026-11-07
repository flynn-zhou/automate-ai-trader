# -*- coding: utf-8 -*-
"""把真实资金费率算进回测。

之前的 +666% 有个致命遗漏：永续合约每 8 小时收一次资金费，BTC 长期正费率（多头付钱），
年化约 10%。1.5x 杠杆下这等于每年 15% 的权益损耗，六年下来能把结论整个翻掉。

不能靠假设，必须拉币安真实的 fundingRate 历史来算。
"""
import json, os, sys, time, urllib.request
from datetime import datetime

sys.path.insert(0, os.getcwd())
import tools.backtest as bt

CACHE = "data/funding_btcusdt.json"
PROXY = os.environ.get("AI_HTTP_PROXY", "")


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    if PROXY:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        return json.loads(op.open(req, timeout=25).read())
    return json.loads(urllib.request.urlopen(req, timeout=25).read())


def download():
    """分页拉全量资金费率历史。8 小时一次，六年约 6600 条。"""
    out, start = [], int(datetime(2020, 1, 1).timestamp() * 1000)
    while True:
        url = ("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT"
               "&startTime=%d&limit=1000" % start)
        d = _fetch(url)
        if not d:
            break
        out += d
        start = d[-1]["fundingTime"] + 1
        print("  拉到 %d 条，截至 %s" % (len(out),
              datetime.utcfromtimestamp(d[-1]["fundingTime"] / 1000).date()))
        if len(d) < 1000:
            break
        time.sleep(0.25)
    return out


if os.path.exists(CACHE):
    rows = json.load(open(CACHE))
    print("资金费率缓存：%d 条" % len(rows))
else:
    print("下载资金费率历史…")
    rows = download()
    json.dump(rows, open(CACHE, "w"))
    print("已缓存 %d 条" % len(rows))

fr = sorted([(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows])
print("区间 %s ~ %s，共 %d 次结算"
      % (datetime.utcfromtimestamp(fr[0][0] / 1000).date(),
         datetime.utcfromtimestamp(fr[-1][0] / 1000).date(), len(fr)))
avg = sum(x[1] for x in fr) / len(fr)
print("平均单次费率 %.5f%%（年化约 %.2f%%）"
      % (avg * 100, avg * 3 * 365 * 100))

# 按 4h 桶索引
by_ts = {}
for t, r in fr:
    by_ts.setdefault(int(t // 14400000), []).append(r)

bars = [b for b in bt.load("data/backtest.db", "4h") if b[5] > 0]
C = [b[4] for b in bars]
T = [b[0] for b in bars]
N = len(bars)
TAKER = 0.0005


def run(n, funding=True, lev=1.5, short_side=False):
    """返回 (总收益, 交易数, 年化资金费占比, 每年收益dict)。"""
    eq = 1.0
    pos = 0                    # 0 空, 1 多, -1 空
    entry = 0.0
    curve = []
    freq = 0.0                 # 累计资金费（占权益）
    for i in range(n + 1, N):
        s_now = sum(C[i-n:i]) / n
        s_prev = sum(C[i-1-n:i-1]) / n if i-1-n >= 0 else s_now
        above, prev = C[i] > s_now, C[i-1] > s_prev

        # 资金费：每个 4h 桶结一次
        if pos != 0 and funding:
            bucket = int(T[i] // 14400000)
            for r in by_ts.get(bucket, []):
                pay = r * lev * (1 if pos > 0 else -1)   # 多头费率为正时付钱
                eq *= (1 - pay)
                freq += pay

        if pos == 0:
            if above and not prev:
                pos, entry = 1, C[i]
                eq *= (1 - TAKER * lev)
            elif short_side and (not above) and prev:
                pos, entry = -1, C[i]
                eq *= (1 - TAKER * lev)
        elif pos > 0 and (not above) and prev:
            eq *= (1 + (C[i] - entry) / entry - TAKER * lev)
            pos = 0
        elif pos < 0 and above and (not prev):
            eq *= (1 + (entry - C[i]) / entry - TAKER * lev)
            pos = 0
        mark = eq
        if pos > 0:
            mark = eq * (1 + (C[i] - entry) / entry)
        elif pos < 0:
            mark = eq * (1 + (entry - C[i]) / entry)
        curve.append(mark)
    if pos != 0:
        r = (C[-1] - entry) / entry if pos > 0 else (entry - C[-1]) / entry
        eq *= (1 + r - TAKER * lev)

    def by_year(cv, off):
        res, prev = {}, 1.0
        buckets = {}
        for k, v in enumerate(cv):
            y = time.localtime(T[off+k] / 1000).tm_year
            buckets[y] = v
        for y in sorted(buckets):
            res[y] = (buckets[y] / prev - 1) * 100
            prev = buckets[y]
        return res
    return eq - 1, by_year(curve, n + 1), freq


print()
print("=== 只做多 SMA200，1.5x：资金费的影响 ===")
for n in (160, 200, 240):
    e_no, _, _ = run(n, funding=False)
    e_f, yr, freq = run(n, funding=True)
    print("SMA%-4d 不计资金费 %+7.0f%%   计入 %+7.0f%%   （资金费吃掉 %.1f%% 权益）"
          % (n, e_no * 100, e_f * 100, (e_no - e_f) * 100))

print()
print("=== 分年（SMA200，计资金费）===")
e_f, yr, freq = run(200, funding=True)
bh = [c / C[0] for c in C]
_, bhy, _ = run(200, funding=False)
print("年度        " + "  ".join("%7d" % y for y in sorted(yr)) + "   累计  资金费")
print("策略        " + "  ".join("%+6.0f%%" % yr[y] for y in sorted(yr))
      + "  %+5.0f%%  %.0f%%" % (e_f * 100, freq * 100))

print()
print("=== 双向（上多下空，计资金费）===")
for n in (160, 200, 240):
    e_b, yb, fb = run(n, funding=True, short_side=True)
    print("SMA%-4d 双向 %+7.0f%%   " % (n, e_b * 100)
          + "  ".join("%+5.0f%%" % yb.get(y, 0) for y in sorted(yb)))
