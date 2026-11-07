# -*- coding: utf-8 -*-
"""诊断：为什么 1m 数据上一个信号都不出。"""
import sqlite3, statistics, sys, os
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

bars = bt.load("data/backtest.db", "1m")
print("bars:", len(bars))
cnt = {"micro_sigma_ok": 0, "vol_ok": 0, "break_hi": 0, "break_lo": 0, "sig": 0}
samples = []
for i in range(70, len(bars), 37):
    price = bars[i-1][4]
    sg = bt.sigma_ret(bars, i, 60)
    if sg <= 0:
        continue
    seg = bars[i-6:i]
    ref = seg[0][4]
    mv = (price - ref) / ref * 100.0
    n = abs(mv) / sg
    if n >= 3.0:
        cnt["micro_sigma_ok"] += 1
        recent_v = sum(b[5] for b in seg) / 6.0
        base_v = sum(b[5] for b in bars[max(0, i-60):i-6]) / max(1, len(bars[max(0, i-60):i-6]))
        vx = recent_v / base_v if base_v > 0 else 0
        if vx >= 1.0:
            cnt["vol_ok"] += 1
            hi, lo = bt.swing(bars, i, 20)
            if mv > 0 and price > hi:
                cnt["break_hi"] += 1
                samples.append((i, "long", mv, n, vx))
            if mv < 0 and price < lo:
                cnt["break_lo"] += 1
                samples.append((i, "short", mv, n, vx))
print("统计（每 37 根抽样）:", cnt)
print("样本位移/σ 分布：")
sgs = [bt.sigma_ret(bars, i, 60) for i in range(70, len(bars), 37)]
sgs = [s for s in sgs if s > 0]
print("  sigma 中位数 %.4f%%  最大 %.4f%%  最小 %.4f%%" % (
    statistics.median(sgs), max(sgs), min(sgs)))
nvals = []
for i in range(70, len(bars), 37):
    sg = bt.sigma_ret(bars, i, 60)
    if sg <= 0: continue
    seg = bars[i-6:i]; ref = seg[0][4]
    if not ref: continue
    mv = (bars[i-1][4] - ref)/ref*100
    nvals.append(abs(mv)/sg)
nvals.sort()
print("  位移/σ 分位: p50=%.2f p90=%.2f p99=%.2f max=%.2f" % (
    nvals[len(nvals)//2], nvals[int(len(nvals)*.9)], nvals[int(len(nvals)*.99)], nvals[-1]))
print("  >=3σ 的占比: %.2f%%" % (sum(1 for v in nvals if v>=3)/len(nvals)*100))
