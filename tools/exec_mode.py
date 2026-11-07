# -*- coding: utf-8 -*-
"""在真实资金费率的约束下，重新找最优执行方式。

核心问题：只做多的趋势策略，用永续还是用现货？
永续每 8 小时收 11.63%/年化的资金费；现货没有。多头的资金费是纯损耗，
所以对"只做多"这类策略，现货应该严格优于永续。这里把它量化出来。
"""
import json, os, sys, time

sys.path.insert(0, os.getcwd())
import tools.backtest as bt

fr = sorted([(int(r["fundingTime"]), float(r["fundingRate"]))
             for r in json.load(open("data/funding_btcusdt.json"))])
by_ts = {}
for t, r in fr:
    by_ts.setdefault(int(t // 14400000), []).append(r)

bars = [b for b in bt.load("data/backtest.db", "4h") if b[5] > 0]
C = [b[4] for b in bars]; T = [b[0] for b in bars]; N = len(bars)
TAKER = 0.0005
START_IDX_N = 201


def avg_funding_window(i, k=6):
    """过去 k 个 4h 桶的平均资金费率（约过去一天）。"""
    vals = []
    for j in range(i - k, i + 1):
        b = int(T[max(0, j)] // 14400000)
        vals += by_ts.get(b, [])
    return sum(vals) / len(vals) if vals else 0.0


def run(n, lev=1.0, funding=True, fmax=None, trades_out=False):
    eq, pos, entry, curve = 1.0, 0, 0.0, []
    trades = []
    for i in range(n + 1, N):
        s_now = sum(C[i-n:i]) / n
        s_prev = sum(C[i-1-n:i-1]) / n if i-1-n >= 0 else s_now
        above, prev = C[i] > s_now, C[i-1] > s_prev
        if pos and funding:
            for r in by_ts.get(int(T[i] // 14400000), []):
                eq *= (1 - r * lev)
        if pos == 0:
            ok = above and not prev
            if ok and fmax is not None and avg_funding_window(i) > fmax:
                ok = False
            if ok:
                pos, entry = 1, C[i]
                eq *= (1 - TAKER * lev)
        elif (not above) and prev:
            eq *= (1 + lev * (C[i] - entry) / entry - TAKER * lev)
            if trades_out:
                trades.append((C[i] - entry) / entry)
            pos = 0
        curve.append(eq * (1 + lev * (C[i] - entry) / entry) if pos else eq)
    if pos:
        r = (C[-1] - entry) / entry
        eq *= (1 + lev * r - TAKER * lev)
        if trades_out:
            trades.append(r)
    return eq - 1, curve, trades


def stats(curve):
    peak, mdd = 0, 0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0)
    return mdd


def by_year(curve, off=START_IDX_N + 1):
    res, prev, buckets = {}, 1.0, {}
    for k, v in enumerate(curve):
        y = time.localtime(T[off+k] / 1000).tm_year
        buckets[y] = v
    for y in sorted(buckets):
        res[y] = (buckets[y] / prev - 1) * 100
        prev = buckets[y]
    return res


print("=== 同一策略，不同执行方式（SMA200，六年）===")
print("%-22s %8s %8s %7s" % ("方式", "累计", "最大回撤", "年化"))
for name, lev, fund in [("现货 1x（无资金费）", 1.0, False),
                        ("现货 1x 复利", 1.0, False),
                        ("永续 1x（含资金费）", 1.0, True),
                        ("永续 1.5x（含资金费）", 1.5, True),
                        ("永续 2x（含资金费）", 2.0, True),
                        ("永续 3x（含资金费）", 3.0, True)]:
    e, cv, tr = run(200, lev=lev, funding=fund)
    yrs = 6.85
    cagr = ((1 + e) ** (1 / yrs) - 1) * 100
    print("%-22s %+7.0f%% %7.0f%% %+6.1f%%" % (name, e * 100, stats(cv) * 100, cagr))

print()
print("=== 分年对比 ===")
e_spot, cv_spot, _ = run(200, lev=1.0, funding=False)
e_perp, cv_perp, _ = run(200, lev=1.5, funding=True)
ys, yp = by_year(cv_spot, 201), by_year(cv_perp, 201)
years = sorted(ys)
print("年度        " + "  ".join("%7d" % y for y in years))
print("现货1x      " + "  ".join("%+6.0f%%" % ys[y] for y in years))
print("永续1.5x    " + "  ".join("%+6.0f%%" % yp[y] for y in years))

print()
print("=== 资金费过滤：只在费率不高时进场（永续 1.5x）===")
for fmax in (None, 0.0002, 0.0001, 0.00005, 0.0):
    e, cv, tr = run(200, lev=1.5, funding=True, fmax=fmax, trades_out=True)
    tag = "不过滤" if fmax is None else ("<%s%%" % (fmax * 100))
    print("  %-10s 累计 %+7.0f%%  回撤 %3.0f%%  交易 %d 笔  胜率 %.0f%%"
          % (tag, e * 100, stats(cv) * 100, len(tr),
             (sum(1 for x in tr if x > 0) / len(tr) * 100) if tr else 0))
