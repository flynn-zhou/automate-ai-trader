# -*- coding: utf-8 -*-
"""两个问题：
1) 资金费过滤器是不是过拟合？（训练/测试各半）
2) 资金费率能不能当"拥挤度信号"用在现货上——即使不付资金费，
   高费率意味着多头拥挤，回避它是否也能提升收益？

第 2 点是关键：如果现货 + 拥挤度过滤也更好，那说明过滤器抓到的是真实的市场结构，
而不是"少付了资金费"这个机械解释。
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


def fund_at(i, k=6):
    vals = []
    for j in range(i - k, i + 1):
        vals += by_ts.get(int(T[max(0, j)] // 14400000), [])
    return sum(vals) / len(vals) if vals else 0.0


def run(n, lev=1.0, funding=True, fmax=None, lo=0, hi=None):
    hi = N if hi is None else hi
    eq, pos, entry = 1.0, 0, 0.0
    peak, mdd = 1.0, 0.0
    nt = 0
    for i in range(max(n + 1, lo), hi):
        s_now = sum(C[i-n:i]) / n
        s_prev = sum(C[i-1-n:i-1]) / n if i-1-n >= 0 else s_now
        above, prev = C[i] > s_now, C[i-1] > s_prev
        if pos and funding:
            for r in by_ts.get(int(T[i] // 14400000), []):
                eq *= (1 - r * lev)
        if pos == 0:
            ok = above and not prev
            if ok and fmax is not None and fund_at(i) > fmax:
                ok = False
            if ok:
                pos, entry = 1, C[i]
                eq *= (1 - TAKER * lev)
                nt += 1
        elif (not above) and prev:
            eq *= (1 + lev * (C[i] - entry) / entry - TAKER * lev)
            pos = 0
        mark = eq * (1 + lev * (C[i] - entry) / entry) if pos else eq
        peak = max(peak, mark)
        mdd = max(mdd, (peak - mark) / peak if peak else 0)
    if pos:
        eq *= (1 + lev * (C[-1] - entry) / entry - TAKER * lev)
    return eq - 1, mdd, nt


mid = N // 2
print("=== 问题1：资金费过滤器，训练/测试各半（永续 1.5x）===")
print("%-12s %10s %10s" % ("阈值", "训练前半", "测试后半"))
for fmax in (None, 0.0002, 0.00015, 0.0001, 0.00005):
    e1, d1, _ = run(200, 1.5, True, fmax, lo=201, hi=mid)
    e2, d2, _ = run(200, 1.5, True, fmax, lo=mid, hi=N)
    tag = "不过滤" if fmax is None else "<%.3f%%" % (fmax * 100)
    print("%-12s %+9.0f%% %+9.0f%%" % (tag, e1 * 100, e2 * 100))

print()
print("=== 问题2：现货 1x + 同一过滤器（不付资金费，纯拥挤度信号）===")
print("%-12s %10s %10s %8s %6s" % ("阈值", "训练前半", "测试后半", "全期", "回撤"))
for fmax in (None, 0.0003, 0.0002, 0.00015, 0.0001):
    e1, _, _ = run(200, 1.0, False, fmax, lo=201, hi=mid)
    e2, _, _ = run(200, 1.0, False, fmax, lo=mid, hi=N)
    ea, da, nt = run(200, 1.0, False, fmax)
    tag = "不过滤" if fmax is None else "<%.3f%%" % (fmax * 100)
    print("%-12s %+9.0f%% %+9.0f%% %+7.0f%% %5.0f%%" % (tag, e1 * 100, e2 * 100, ea * 100, da * 100))
