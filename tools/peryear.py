# -*- coding: utf-8 -*-
"""最近两年是策略衰减，还是市场本来就没趋势？

这是注资前最该回答的问题：
  SMA200 逐年 2019:0% 2020:+210% 2021:+22% 2022:-25%
              2023:+101% 2024:+86% 2025:-10% 2026:+8%
  最近两年几乎是平的。如果市场本身也没涨，那是策略在正确地吃灰；
  如果市场大涨而策略没跟上，那是边际没了。

  两者含义完全不同，必须分清。
"""
import os, sys, datetime
sys.path.insert(0, os.getcwd())

# 【已作废，勿引用其结果】
# 1) 它读的 data/spot.db 已不存在（第 61 轮发现），数字无法复现。
# 2) 它的 strat() 里【没有熔断止损】，而线上 trend_stop_pct=25.0，
#    测的不是我们运行的策略。
# 现行口径请用 tools/liveedge.py（逐条对齐 trend.py / engine.py，
# 且带 25% 止损）。实测那段历史止损从未触发，所以两者收益相同，
# 但 liveedge.py 至少是可复现、可审计的。
raise SystemExit("peryear.py 已作废：数据缺失且无止损。请运行 tools/liveedge.py")

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

def strat(n, i0, i1):
    eq = 1.0; pos = False; entry = 0.0; bars = 0; trades = 0
    for i in range(max(n + 1, i0), min(i1, N)):
        s = sum(C[i - n:i]) / n
        sp = sum(C[i - 1 - n:i - 1]) / n if i - 1 - n >= 0 else s
        p, pp = C[i], C[i - 1]
        if not pos and p > s and pp <= sp:
            pos = True; entry = p; trades += 1
        elif pos and p < s:
            eq *= (1 + (p - entry) / entry - 2 * COST); pos = False
        if pos:
            bars += 1
    if pos:
        eq *= (1 + (C[min(i1, N) - 1] - entry) / entry - 2 * COST)
    return (eq - 1) * 100, trades, bars / max(1, min(i1, N) - max(n + 1, i0))

print("=" * 78)
print("%-6s %12s %12s %10s %10s %8s" % ("年份", "策略", "买入持有", "差额", "BTC 区间", "在场%"))
print("=" * 78)
for y in sorted(set(YEARS)):
    r = idx_of_year(y)
    if not r:
        continue
    s_ret, tr, inmk = strat(200, r[0], r[1])
    bh = (C[r[1] - 1] / C[r[0]] - 1) * 100
    lo, hi = min(C[r[0]:r[1]]), max(C[r[0]:r[1]])
    print("%-6d %+11.0f%% %+11.0f%% %+9.0f%%   %6.0f-%-6.0f %7.0f%%"
          % (y, s_ret, bh, s_ret - bh, lo, hi, inmk * 100))

print()
print("=== 最近两年细看 ===")
for y in (2025, 2026):
    r = idx_of_year(y)
    if not r:
        continue
    s_ret, tr, inmk = strat(200, r[0], r[1])
    bh = (C[r[1] - 1] / C[r[0]] - 1) * 100
    print("%d  策略 %+.0f%%   买入持有 %+.0f%%   交易 %d 笔   在场 %.0f%%"
          % (y, s_ret, bh, tr, inmk * 100))
