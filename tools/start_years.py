# -*- coding: utf-8 -*-
"""如果从某一年开始投，后面几年能拿到什么？

为什么必须算：
  全期 +1032% 是从 2019 年 12 月一路拿到 2026 年 11 月。
  但用户不是从 2019 年开始投——他是今天（2026 年 11 月）开始。
  中间那波 2020 年 +210% 不会重来。
  拿全期数字给一个今天才入场的人看，是误导。
"""
import os, sys, datetime
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

COST = 0.001
rows = [r for r in bt.load("data/spot.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
T = [r[0] for r in rows]
N = len(rows)
YEARS = [datetime.datetime.utcfromtimestamp(t / 1000).year for t in T]

def idx_of_year(y):
    idx = [i for i in range(N) if YEARS[i] == y]
    return (idx[0], idx[-1] + 1) if idx else None

def run(n, i0, i1):
    eq = 1.0; pos = False; entry = 0.0
    for i in range(max(n + 1, i0), min(i1, N)):
        s = sum(C[i - n:i]) / n
        sp = sum(C[i - 1 - n:i - 1]) / n if i - 1 - n >= 0 else s
        p, pp = C[i], C[i - 1]
        if not pos and p > s and pp <= sp:
            pos = True; entry = p
        elif pos and p < s:
            eq *= (1 + (p - entry) / entry - 2 * COST); pos = False
    if pos:
        eq *= (1 + (C[min(i1, N) - 1] - entry) / entry - 2 * COST)
    return (eq - 1) * 100

years = sorted(set(YEARS))
print("=" * 74)
print("从某年年初投进去，到 2026-11 的累计收益（现货 taker 0.10%/边）")
print("=" * 74)
print("%-12s %8s %12s %12s %10s" % ("起点", "年数", "策略累计", "买入持有", "策略年化"))
for y in years[:-1]:
    r0 = idx_of_year(y)
    if not r0:
        continue
    yrs = (T[N - 1] - T[r0[0]]) / (365.25 * 86400000)
    if yrs < 0.5:
        continue
    s_ret = run(200, r0[0], N)
    bh = (C[N - 1] / C[r0[0]] - 1) * 100
    cagr = ((1 + s_ret / 100) ** (1 / yrs) - 1) * 100 if s_ret > -100 else -100
    print("%-12s %7.2f %+11.0f%% %+11.0f%% %+9.1f%%"
          % ("%d-01" % y, yrs, s_ret, bh, cagr))

print()
print("=== 最坏情况：从任一年初投，持有满 2 年 ===")
worst, best = 999, -999
for y in years:
    r0 = idx_of_year(y)
    if not r0:
        continue
    end = None
    for y2 in years:
        if y2 == y + 2:
            end = idx_of_year(y2)
    if not end:
        continue
    s_ret = run(200, r0[0], end[0])
    worst = min(worst, s_ret); best = max(best, s_ret)
    print("  %d-01 起两年：%+8.0f%%" % (y, s_ret))
print()
print("  两年持有：最差 %+.0f%%   最好 %+.0f%%" % (worst, best))
