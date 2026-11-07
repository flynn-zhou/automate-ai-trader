# -*- coding: utf-8 -*-
"""订单流不平衡的预测力检验 —— 与价格动量完全独立的信息源。

币安 K 线自带 taker 买入量(b)与总量(v)：imb = 2*b/v - 1 直接反映主动买卖压力。
如果这个维度有边际，它才是唯一可能活在成本线之上的东西。

同时给"中高波动 + 反向 + 长持有"这一格做显著性检验——
它之前显示 +0.0801%(480分钟)，需要知道那是不是噪音。
"""
import os, sys, statistics, math
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

bars = bt.load("data/backtest.db", "1m")
N = len(bars)
CLOSE = [b[4] for b in bars]

# 数据自检：taker 买量必须 <= 总量
bad = sum(1 for b in bars[:5000] if b[6] and b[5] and b[6] > b[5] * 1.001)
print("自检：前 5000 根里 taker买量>总量 的异常行数 =", bad)

def fwd(i, h):
    if i + h >= N: return None
    a, c = CLOSE[i], CLOSE[i+h]
    return (c - a) / a * 100.0 if a else None

def imb(i):
    v, b = bars[i][5], bars[i][6]
    if not v: return None
    return 2.0 * b / v - 1.0

HORIZONS = [5, 15, 60, 240]

print()
print("=== 订单流不平衡（当根 K 的 imb）与未来收益 ===")
print("%-14s %6s  " % ("imb 区间", "样本") + "  ".join("%10s" % ("%dm后" % h) for h in HORIZONS))
for lo, hi in ((-1.01, -0.4), (-0.4, -0.15), (-0.15, 0.15), (0.15, 0.4), (0.4, 1.01)):
    acc = {h: [] for h in HORIZONS}
    n = 0
    for i in range(70, N - 300):
        x = imb(i)
        if x is None or not (lo <= x < hi): continue
        n += 1
        for h in HORIZONS:
            r = fwd(i, h)
            if r is not None: acc[h].append(r)
    if n < 50:
        continue
    cells = []
    for h in HORIZONS:
        v = acc[h]
        if len(v) < 30: cells.append("%10s" % "-"); continue
        m = statistics.mean(v); sd = statistics.pstdev(v)
        t = m/(sd/math.sqrt(len(v))) if sd else 0
        cells.append("%8.4f%%%s" % (m, "*" if abs(t) > 2 else " "))
    print("%-14s %6d  " % ("[%.2f,%.2f)" % (lo, hi), n) + "  ".join(cells))
print("  (* = |t|>2 统计显著；成本线 taker 0.11% / maker 0.04%)")

print()
print("=== 显著性复核：中高波动 + 反向 + 480 分钟 ===")
sig_all = []
for i in range(70, N, 10):
    s = bt.sigma_ret(bars, i, 60)
    if s > 0: sig_all.append(s)
sig_all.sort()
q50, q75 = sig_all[len(sig_all)//2], sig_all[int(len(sig_all)*0.75)]
vals = []
for i in range(70, N - 500):
    sg = bt.sigma_ret(bars, i, 60)
    if sg <= 0 or not (q50 <= sg < q75): continue
    sig = bt.detect(bars, i, {"sig": "all", "micro_sigma": 3.0})
    if not sig: continue
    d = -(1 if sig[0] == "long" else -1)      # 反向
    r = fwd(i, 480)
    if r is not None: vals.append(d * r)
if len(vals) > 30:
    m = statistics.mean(vals); sd = statistics.pstdev(vals)
    t = m/(sd/math.sqrt(len(vals))) if sd else 0
    print("样本 %d  平均 %+.4f%%  标准差 %.4f%%  t=%.2f" % (len(vals), m, sd, t))
    print("taker 往返 0.11%% -> 净 %+.4f%%  %s" % (m-0.11, "可行" if m-0.11 > 0 else "不可行"))
    print("maker 往返 0.04%% -> 净 %+.4f%%  %s" % (m-0.04, "可行" if m-0.04 > 0 else "不可行"))
    print("需要的样本量（要 t=2 且净>0）：约 %d 笔" % max(0, int((2*sd/m)**2)) if m > 0 else "均值为负，无解")
