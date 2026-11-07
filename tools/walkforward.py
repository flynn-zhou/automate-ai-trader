# -*- coding: utf-8 -*-
"""滚动验证：参数在每一年是否都站得住，而不是只靠某一段行情。

比"训练/测试各半"更严的地方：单次切分只能验一次，
按年拆开能看到【每一种市况】下的表现，暴露出"只在牛市有效"这类问题。
"""
import os, sys, time, statistics
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

rows = [r for r in bt.load("data/backtest.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
T = [r[0] for r in rows]
N = len(rows)

def sim(n, cost=0.0002, slope=False):
    eq = 1.0; pos = False; entry = 0.0; curve = []
    for i in range(n+1, N):
        s_now = sum(C[i-n:i])/n
        s_prev = sum(C[i-1-n:i-1])/n if i-1-n >= 0 else s_now
        above = C[i] > s_now
        prev = C[i-1] > s_prev
        ok = (s_now > s_prev) if slope else True
        if not pos and above and not prev and ok:
            pos = True; entry = C[i]
        elif pos and (not above) and prev:
            eq *= (1 + (C[i]-entry)/entry - 2*cost); pos = False
        curve.append(eq*(1+(C[i]-entry)/entry) if pos else eq)
    if pos:
        eq *= (1 + (C[-1]-entry)/entry - 2*cost)
    return curve

def by_year(curve, off):
    out = {}; start = 1.0
    for k, v in enumerate(curve):
        y = time.localtime(T[off+k]/1000).tm_year
        out.setdefault(y, [start, start])
        out[y][1] = v
    res = {}
    prev = 1.0
    for y in sorted(out):
        res[y] = (out[y][1]/prev - 1)*100
        prev = out[y][1]
    return res

bh = [c/C[0] for c in C]
bhy = by_year(bh, 0)
years = sorted(bhy)
print("年度      " + "  ".join("%7d" % y for y in years) + "   正年数  最差年")
print("买入持有  " + "  ".join("%+6.0f%%" % bhy[y] for y in years))

results = []
for n in (120, 160, 200, 240, 280):
    yr = by_year(sim(n), n+1)
    good = sum(1 for y in years if yr.get(y, 0) > 0)
    worst = min(yr.get(y, 0) for y in years)
    print("SMA%-4d  " % n + "  ".join("%+6.0f%%" % yr.get(y, 0) for y in years)
          + "   %d/%d   %+.0f%%" % (good, len(years), worst))
    results.append((good, worst, n, yr))

print()
print("=== 加上『只在上行时做多』过滤（SMA 斜率 > 0）===")
for n in (160, 200, 240):
    yr = by_year(sim(n, slope=True), n+1)
    good = sum(1 for y in years if yr.get(y, 0) > 0)
    worst = min(yr.get(y, 0) for y in years)
    tot = 1.0
    for y in years: tot *= (1 + yr.get(y, 0)/100)
    print("SMA%-4d+斜率  " % n + "  ".join("%+6.0f%%" % yr.get(y, 0) for y in years)
          + "   %d/%d   %+.0f%%   累计 %+.0f%%" % (good, len(years), worst, (tot-1)*100))

print()
print("=== 日线尺度（4h 合成 1d，每 6 根）===")
d = [rows[i] for i in range(0, N, 6)]
saved_C, saved_N = C[:], N
C[:] = [r[4] for r in d]; N = len(d)
for n in (30, 50, 100):
    if N < n + 50: continue
    yr = by_year(sim(n), n+1)
    ys = sorted(yr)
    good = sum(1 for y in ys if yr[y] > 0)
    print("日线 SMA%-4d  " % n + "  ".join("%+6.0f%%" % yr[y] for y in ys) + "   %d/%d" % (good, len(ys)))
C[:] = saved_C; N = saved_N
