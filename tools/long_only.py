# -*- coding: utf-8 -*-
"""只做多 + 趋势过滤：最后一个有实证支撑的方向。

逻辑：BTC 6 年 +503%，长期上行漂移是唯一稳定的"边际"。
双向趋势跟踪会在这段历史里被空头拖死；只做多的趋势过滤
能把漂移留住、把崩盘躲掉。

评价标准不是"总收益最高"，而是：
  收益 / 最大回撤  与  卡玛比率  —— 因为它是能被杠杆安全放大的东西。
同时必须跑样本外。
"""
import os, sys, statistics, time, itertools
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

rows = [r for r in bt.load("data/backtest.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
N = len(rows)
print("4h %d 根  %.1f 年" % (N, N*4/24/365))

def sma(i, n):
    if i < n: return None
    return sum(C[i-n:i]) / n

def sim_sma(n, cost, split=None):
    """价格上穿 SMA 做多，下穿平仓。返回 (收益序列, 在场时间比例, 交易数)。"""
    eq = 1.0
    curve = []
    pos = False
    entry = 0.0
    bars_in = 0
    trades = 0
    rets = []
    for i in range(n+1, N):
        s = sma(i, n)
        if s is None: continue
        price = C[i]
        if not pos and price > s and C[i-1] <= sma(i-1, n):
            pos = True; entry = price; trades += 1
        elif pos and price < s:
            r = (price - entry)/entry - 2*cost
            rets.append(r); eq *= (1+r); pos = False
        if pos: bars_in += 1
        curve.append(eq)
    if pos:
        r = (C[-1]-entry)/entry - 2*cost
        rets.append(r); eq *= (1+r)
    return {"ret": (eq-1)*100, "trades": trades, "in_mkt": bars_in/max(1, N-n-1),
            "rets": rets, "curve": curve}

def maxdd(curve):
    peak = -1e9; dd = 0
    for v in curve:
        peak = max(peak, v)
        if peak > 0: dd = max(dd, (peak-v)/peak)
    return dd*100

print()
print("=== 买入持有基准 ===")
bh_curve = [c/C[0] for c in C]
print("总收益 %+.0f%%  最大回撤 %.0f%%  卡玛 %.2f" % (
    (C[-1]/C[0]-1)*100, maxdd(bh_curve),
    (C[-1]/C[0]-1)*100/maxdd(bh_curve) if maxdd(bh_curve) else 0))

print()
print("SMA周期 成本    总收益    最大回撤   卡玛    交易数  在场%")
best = []
for n in (50, 100, 150, 200, 300, 400):
    for cost, cn in ((0.0007,"taker"),(0.0002,"maker")):
        r = sim_sma(n, cost)
        dd = maxdd(r["curve"])
        calmar = r["ret"]/dd if dd else 0
        print("%5d  %-6s %+8.0f%%  %6.0f%%  %5.2f  %5d  %4.0f%%" % (
            n, cn, r["ret"], dd, calmar, r["trades"], r["in_mkt"]*100))
        best.append((calmar, n, cn, r))

print()
print("=== 样本外验证（前半段定周期，后半段验证）===")
SPLIT = N//2
tr_rows, te_rows = rows[:SPLIT], rows[SPLIT:]
def sim_slice(sl, n, cost):
    global C, N
    old_C, old_N = C, N
    C, N = [r[4] for r in sl], len(sl)
    try:
        return sim_sma(n, cost)
    finally:
        C, N = old_C, old_N

best.sort(reverse=True)
for calmar, n, cn, r in best[:3]:
    a = sim_slice(tr_rows, n, 0.0002)
    b = sim_slice(te_rows, n, 0.0002)
    print("SMA%d %s: 训练 %+.0f%% (回撤%.0f%%) | 测试 %+.0f%% (回撤%.0f%%)" % (
        n, cn, a["ret"], maxdd(a["curve"]), b["ret"], maxdd(b["curve"])))
print()
print("（对照）买入持有：训练 %+.0f%% | 测试 %+.0f%%" % (
    (tr_rows[-1][4]/tr_rows[0][4]-1)*100, (te_rows[-1][4]/te_rows[0][4]-1)*100))
