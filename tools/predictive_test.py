# -*- coding: utf-8 -*-
"""最干净的检验：信号有没有预测力？

抛开止损/止盈结构，直接看：信号出现后，未来 5/15/30/60/120 分钟的
平均收益是多少、方向对不对、统计上显著吗（t 值）。

如果这里有信号，任何合理的止盈止损结构都能赚钱；
如果这里是 0，那么所有参数扫描出来的"最好一组"都是噪音。
"""
import os, sys, statistics, math
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

bars = bt.load("data/backtest.db", "1m")
CLOSE = [b[4] for b in bars]
n_bars = len(bars)

def forward_ret(i, h):
    if i + h >= n_bars:
        return None
    a, b = CLOSE[i], CLOSE[i + h]
    return (b - a) / a * 100.0 if a else None

HORIZONS = [5, 15, 30, 60, 120]

for sigma_min in (3.0, 5.0, 7.0):
    print()
    print("=== 信号强度 >= %.0f sigma ===" % sigma_min)
    acc = {h: [] for h in HORIZONS}
    cnt = 0
    for i in range(70, n_bars - 130):
        sig = bt.detect(bars, i, {"sig": "all", "micro_sigma": sigma_min})
        if not sig:
            continue
        side, hint, tag = sig
        d = 1 if side == "long" else -1
        cnt += 1
        for h in HORIZONS:
            r = forward_ret(i, h)
            if r is not None:
                acc[h].append(d * r)   # 按信号方向调整后的收益
    print("样本 %d 个信号" % cnt)
    for h in HORIZONS:
        v = acc[h]
        if len(v) < 10:
            continue
        m = statistics.mean(v)
        sd = statistics.pstdev(v)
        t = m / (sd / math.sqrt(len(v))) if sd else 0
        wr = sum(1 for x in v if x > 0) / len(v)
        print("  %4d 分钟后：平均 %+.4f%%  中位 %+.4f%%  胜率 %.1f%%  t=%.2f  %s"
              % (h, m, statistics.median(v), wr * 100, t,
                 "显著" if abs(t) > 2 else "不显著"))
    print("  成本线：往返 0.11%% -> 只有平均收益 > 0.11%% 才值得做")
