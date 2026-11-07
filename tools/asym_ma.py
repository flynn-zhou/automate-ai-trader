# -*- coding: utf-8 -*-
"""非对称均线：入场和出场用不同速度的均线。

动机（不是随机挖参）：单一均线的进出对称，容易在震荡里被反复扫；
用【慢均线进场、快均线出场】= 确认趋势后再进、转向时立刻走，
理论上能减少回吐。反过来【快进慢出】则是抢反转、但容易被假突破骗。

必须同时通过训练段和测试段才算数，否则一律否决（前面已经栽过两次）。
"""
import os, sys

sys.path.insert(0, os.getcwd())
import tools.backtest as bt

C = [b[4] for b in bt.load("data/backtest.db", "4h") if b[5] > 0]
T = [b[0] for b in bt.load("data/backtest.db", "4h") if b[5] > 0]
N, FEE = len(C), 0.001
YRS = (T[-1] - T[0]) / (365.25 * 24 * 3600 * 1000)


def stats(curve):
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0)
    tot = curve[-1] / curve[0] - 1
    cagr = (1 + tot) ** (1 / YRS) - 1
    return tot, cagr, mdd, (cagr / mdd if mdd else 0)


def run(lo, hi, n_in, n_out):
    need = max(n_in, n_out) + 2
    eq, pos, curve = 1.0, False, []
    for i in range(max(need, lo), hi):
        if pos:
            eq *= C[i] / C[i-1]                      # 先结算持仓收益
        si_now = sum(C[i-n_in:i]) / n_in
        si_prev = sum(C[i-1-n_in:i-1]) / n_in
        so_now = sum(C[i-n_out:i]) / n_out
        so_prev = sum(C[i-1-n_out:i-1]) / n_out
        up = C[i] > si_now and C[i-1] <= si_prev     # 慢均线上穿
        dn = C[i] < so_now and C[i-1] >= so_prev     # 快均线下穿
        if pos and dn:
            eq *= (1 - FEE); pos = False
        elif not pos and up:
            eq *= (1 - FEE); pos = True
        curve.append(eq)
    return curve


mid = N // 2
base_full = run(0, N, 200, 200)
bt_, bcg, bmd, bcm = stats(base_full)
print("基准（进出都 SMA200）：累计 %+.0f%%  年化 %.1f%%  回撤 %.0f%%  卡玛 %.2f"
      % (bt_*100, bcg*100, bmd*100, bcm))
print()
print("=== 非对称均线（训练 / 测试各半）===")
print("%-14s %10s %10s %9s %8s %7s" % ("进场/出场", "训练段", "测试段", "全期累计", "回撤", "卡玛"))
rows = []
for n_in in (100, 150, 200):
    for n_out in (30, 50, 100, 150, 200):
        c1 = run(0, mid, n_in, n_out)
        c2 = run(mid, N, n_in, n_out)
        ca = run(0, N, n_in, n_out)
        ta, cga, mda, cma = stats(ca)
        rows.append((n_in, n_out, c1[-1]/c1[0]-1, c2[-1]/c2[0]-1, ta, mda, cma))
        print("SMA%-3d/%-3d %+10.0f%% %+10.0f%% %+8.0f%% %7.0f%% %7.2f"
              % (n_in, n_out, (c1[-1]/c1[0]-1)*100, (c2[-1]/c2[0]-1)*100,
                 ta*100, mda*100, cma))

print()
print("=== 同时跑赢基准（训练和测试都要更好）的组合 ===")
best = None
for n_in, n_out, t1, t2, ta, mda, cma in rows:
    if t1 > (run(0, mid, 200, 200)[-1] - 1) and t2 > (run(mid, N, 200, 200)[-1] - 1):
        print("  SMA%d/%d  训练 %+.0f%%  测试 %+.0f%%  回撤 %.0f%%  卡玛 %.2f"
              % (n_in, n_out, t1*100, t2*100, mda*100, cma))
        if best is None or cma > best[2]:
            best = (n_in, n_out, cma)
print("  （无输出 = 没有一个组合能同时跑赢基准）" if best is None else "  最优：%s" % (best,))
