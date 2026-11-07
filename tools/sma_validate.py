# -*- coding: utf-8 -*-
"""SMA 趋势过滤的严格验证：分年收益、参数敏感性、杠杆与回撤。

要排除三种自欺：
1. 收益全靠某一年（分年拆开看）
2. 参数只有特定值能赚（相邻周期必须也赚）
3. 回撤不可承受（给出各杠杆下的回撤与 CAGR）
"""
import os, sys, time, statistics
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

rows = [r for r in bt.load("data/backtest.db", "4h") if r[5] > 0]
N = len(rows)
C = [r[4] for r in rows]
T = [r[0] for r in rows]

def sma_at(i, n):
    if i < n: return None
    return sum(C[i-n:i])/n

def sim(n, cost_side, lev=1.0):
    """返回逐 bar 权益曲线（按 lev 放大收益，扣成本）。"""
    eq = 1.0
    curve = []
    pos = False
    entry = 0.0
    prev_above = None
    for i in range(n+1, N):
        s_now = sma_at(i, n); s_prev = sma_at(i-1, n)
        if s_now is None or s_prev is None:
            curve.append(eq); continue
        above = C[i] > s_now
        if prev_above is None: prev_above = above
        if not pos and above and not prev_above:
            pos = True; entry = C[i]; eq *= (1 - cost_side*lev)
        elif pos and not above and prev_above:
            gross = (C[i]-entry)/entry
            eq *= (1 + gross*lev - cost_side*lev)
            pos = False
        elif pos:
            # 逐 bar 标记市值
            pass
        # 记录按市值计的权益
        if pos:
            curve.append(eq * (1 + (C[i]-entry)/entry*lev))
        else:
            curve.append(eq)
        prev_above = above
    if pos:
        eq *= (1 + (C[-1]-entry)/entry*lev - cost_side*lev)
    return curve

def maxdd(curve):
    peak = -1e9; dd = 0
    for v in curve:
        peak = max(peak, v)
        if peak > 0: dd = max(dd, (peak-v)/peak)
    return dd*100

def yearly(curve, t0_idx):
    out = []
    start = 1.0
    cur_year = time.localtime(T[t0_idx]/1000).tm_year
    for k, v in enumerate(curve):
        y = time.localtime(T[t0_idx+k]/1000).tm_year
        if y != cur_year:
            out.append((cur_year, (v/start-1)*100))
            start = v; cur_year = y
    if curve:
        out.append((cur_year, (curve[-1]/start-1)*100))
    return out

print("=== 分年收益（SMA200，maker 成本，1x）===")
n = 200
cv = sim(n, 0.0002, 1.0)
for y, r in yearly(cv, n+1):
    print("  %d: %+7.1f%%" % (y, r))
print()
print("=== 参数敏感性（总收益 / 最大回撤）===")
print("周期   taker          maker")
for n in (140, 160, 180, 200, 220, 250, 280):
    a = sim(n, 0.0007, 1.0); b = sim(n, 0.0002, 1.0)
    print("%4d  %+7.0f%%/%3.0f%%   %+7.0f%%/%3.0f%%" % (
        n, (a[-1]-1)*100, maxdd(a), (b[-1]-1)*100, maxdd(b)))

print()
print("=== 杠杆选择（SMA200 maker，6 年）===")
print("杠杆  总收益    年化     最大回撤  卡玛")
for lev in (1.0, 1.5, 2.0, 2.5, 3.0):
    cv = sim(200, 0.0002, lev)
    tot = (cv[-1]-1)*100
    years = N*4/24/365
    cagr = ((cv[-1])**(1/years)-1)*100 if cv[-1] > 0 else -100
    dd = maxdd(cv)
    print("%4.1fx  %+8.0f%%  %+6.1f%%  %6.0f%%  %5.2f" % (lev, tot, cagr, dd, tot/dd if dd else 0))
print()
print("=== 对照：买入持有（1x）===")
bh = [c/C[0] for c in C]
print("  总收益 %+.0f%%  最大回撤 %.0f%%" % ((C[-1]/C[0]-1)*100, maxdd(bh)))
