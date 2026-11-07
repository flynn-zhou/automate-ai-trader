# -*- coding: utf-8 -*-
"""收益因起点而异，那"回撤更小"这个说法稳不稳？

上一轮我说过：策略在收益和回撤上【同时】赢买入持有。
上面刚发现收益那一半取决于起点（从 2023 算买入持有赢 107pp）。
那回撤那一半呢？如果回撤优势也不稳，这个策略就没有可宣称的优势了。

必须按起点逐个验，而不是只看全期。
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

def maxdd(curve):
    peak = -1e18; dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd * 100

def run(n, i0, i1):
    eq = 1.0; pos = False; entry = 0.0; curve = []
    for i in range(max(n + 1, i0), min(i1, N)):
        s = sum(C[i - n:i]) / n
        sp = sum(C[i - 1 - n:i - 1]) / n if i - 1 - n >= 0 else s
        p, pp = C[i], C[i - 1]
        if not pos and p > s and pp <= sp:
            pos = True; entry = p
        elif pos and p < s:
            eq *= (1 + (p - entry) / entry - 2 * COST); pos = False
        curve.append(eq)
    if pos:
        eq *= (1 + (C[min(i1, N) - 1] - entry) / entry - 2 * COST)
        curve.append(eq)
    return (eq - 1) * 100, maxdd(curve)

years = sorted(set(YEARS))
print("=" * 76)
print("%-10s %8s %-16s %-16s %s" % ("起点", "年数", "策略 收益/回撤", "买入持有 收益/回撤", "结论"))
print("=" * 76)
s_win = 0; d_win = 0; c_win = 0; n_cmp = 0
for y in years[:-1]:
    r0 = idx_of_year(y)
    if not r0:
        continue
    yrs = (T[N - 1] - T[r0[0]]) / (365.25 * 86400000)
    if yrs < 0.5:
        continue
    s_ret, s_dd = run(200, r0[0], N)
    bh_curve = [c / C[r0[0]] for c in C[r0[0]:N]]
    bh_ret = (C[N - 1] / C[r0[0]] - 1) * 100
    bh_dd = maxdd(bh_curve)
    s_cal = s_ret / s_dd if s_dd else 0
    b_cal = bh_ret / bh_dd if bh_dd else 0
    verdict = []
    if s_ret > bh_ret: s_win += 1; verdict.append("收益赢")
    if s_dd < bh_dd: d_win += 1; verdict.append("回撤小")
    if s_cal > b_cal: c_win += 1; verdict.append("卡玛赢")
    n_cmp += 1
    print("%-10s %7.2f  %+7.0f%% / %3.0f%%   %+7.0f%% / %3.0f%%    %s"
          % ("%d-01" % y, yrs, s_ret, s_dd, bh_ret, bh_dd, " ".join(verdict)))

print()
print("在 %d 个起点里：" % n_cmp)
print("  收益赢 %d 次" % s_win)
print("  回撤更小 %d 次" % d_win)
print("  卡玛更高 %d 次" % c_win)
