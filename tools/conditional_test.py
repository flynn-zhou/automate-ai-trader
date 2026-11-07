# -*- coding: utf-8 -*-
"""决定性检验：在什么条件下，信号后的收益幅度能超过 0.11% 的成本？

已知：3σ 动量信号在 15-120 分钟后平均收益 -0.008%~-0.018%（显著负，即均值回归），
幅度远小于成本 0.11%。所以要找的是"幅度足够大"的时刻。

考察三个维度：
  A. 波动率状态（用过去 60 根 sigma 分位数分组）
  B. 更长持有期（2h / 4h / 8h）
  C. 反向（均值回归）是否比正向更好
"""
import os, sys, statistics, math
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

bars = bt.load("data/backtest.db", "1m")
CLOSE = [b[4] for b in bars]
N = len(bars)

def fwd(i, h):
    if i + h >= N: return None
    a, b = CLOSE[i], CLOSE[i+h]
    return (b - a) / a * 100.0 if a else None

# 先算全样本 sigma 分布，用来分档
sig_all = []
for i in range(70, N, 10):
    s = bt.sigma_ret(bars, i, 60)
    if s > 0: sig_all.append(s)
sig_all.sort()
qs = [sig_all[int(len(sig_all)*p)] for p in (0.25, 0.5, 0.75, 0.9)]
print("sigma 分位: p25=%.4f%% p50=%.4f%% p75=%.4f%% p90=%.4f%%" % tuple(qs))
print()

HORIZONS = [15, 60, 240, 480]
BUCKETS = [("低波动", 0, qs[0]), ("中低", qs[0], qs[1]), ("中高", qs[1], qs[2]), ("高波动", qs[2], 99)]

for direction, dname in ((1, "顺势(动量)"), (-1, "反向(回归)")):
    print("=" * 78)
    print("方向：%s" % dname)
    print("%-8s %6s  " % ("波动档", "样本") + "  ".join("%10s" % ("%dm后" % h) for h in HORIZONS))
    for name, lo, hi in BUCKETS:
        acc = {h: [] for h in HORIZONS}
        cnt = 0
        for i in range(70, N - 500):
            sg = bt.sigma_ret(bars, i, 60)
            if sg <= 0 or not (lo <= sg < hi):
                continue
            sig = bt.detect(bars, i, {"sig": "all", "micro_sigma": 3.0})
            if not sig:
                continue
            side = sig[0]
            d = (1 if side == "long" else -1) * direction
            cnt += 1
            for h in HORIZONS:
                r = fwd(i, h)
                if r is not None:
                    acc[h].append(d * r)
        if cnt < 30:
            print("%-8s %6d  (样本不足)" % (name, cnt)); continue
        cells = []
        for h in HORIZONS:
            v = acc[h]
            if len(v) < 20:
                cells.append("%10s" % "-"); continue
            m = statistics.mean(v)
            sd = statistics.pstdev(v)
            t = m / (sd / math.sqrt(len(v))) if sd else 0
            mark = "*" if abs(m) > 0.11 else " "
            cells.append("%9.4f%%%s" % (m, mark))
        print("%-8s %6d  " % (name, cnt) + "  ".join(cells))
    print("  (* = 幅度超过往返成本 0.11%)")
    print()
