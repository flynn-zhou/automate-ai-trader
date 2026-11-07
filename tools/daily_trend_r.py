# -*- coding: utf-8 -*-
"""日线级趋势跟踪（R 倍数记账版）。

上一版用"20% 风险复利"，几百笔必然破产，所有配置都 -95U，
那测的是破产概率，不是边际。改成每笔固定风险 1R=1U、不复利，
盈亏直接以 R 计，才能公平比较不同参数。

同时给买入持有基准，用来判断"策略是不是还不如躺着不动"。
"""
import os, sys, statistics, math, itertools, time
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

rows = [r for r in bt.load("data/backtest.db", "4h") if r[5] > 0]
N = len(rows)
print("4h K 线 %d 根（%.1f 年）" % (N, N*4/24/365))

def atr(bs, i, n=14):
    if i < n+1: return 0.0
    trs = []
    for k in range(i-n, i):
        h, l, pc = bs[k][2], bs[k][3], bs[k-1][4]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs)/len(trs) if trs else 0.0

def run(bs, lookback, atr_mult, trail_mult, cost_side, risk_u=1.0):
    """每笔固定风险 1U（不复利），返回 R 序列。"""
    out = []
    i = lookback + 20
    pos = None
    while i < len(bs)-1:
        h, l, c = bs[i][2], bs[i][3], bs[i][4]
        if pos is None:
            hi = max(b[2] for b in bs[i-lookback:i])
            lo = min(b[3] for b in bs[i-lookback:i])
            a = atr(bs, i)
            if a <= 0: i += 1; continue
            side = "long" if c > hi else ("short" if c < lo else None)
            if side:
                d = 1 if side == "long" else -1
                sd = atr_mult * a
                if sd <= 0 or sd/c > 0.20: i += 1; continue
                pos = {"d": d, "entry": c, "stop": c-d*sd, "qty": risk_u/sd,
                       "i0": i, "best": c, "atr0": a}
        else:
            pos["best"] = max(pos["best"], h) if pos["d"] > 0 else min(pos["best"], l)
            a = atr(bs, i) or pos["atr0"]
            ns = pos["best"] - pos["d"]*trail_mult*a
            pos["stop"] = (max(pos["stop"], ns) if pos["d"] > 0 else min(pos["stop"], ns))
            hit = l <= pos["stop"] if pos["d"] > 0 else h >= pos["stop"]
            if hit:
                px = pos["stop"]
                r = ((px-pos["entry"])*pos["qty"]*pos["d"]
                     - (pos["entry"]+px)*pos["qty"]*cost_side)
                out.append(r)
                pos = None
        i += 1
    return out

def stats(v):
    if len(v) < 5: return None
    m = statistics.mean(v); sd = statistics.pstdev(v)
    t = m/(sd/math.sqrt(len(v))) if sd else 0
    return {"n": len(v), "mean": m, "sd": sd, "t": t, "sum": sum(v),
            "wr": sum(1 for x in v if x > 0)/len(v)}

# 基准：买入持有
bh = (rows[-1][4]-rows[0][4])/rows[0][4]*100
print("基准：6 年买入持有 %+.0f%%" % bh)
print()

SPLIT = N//2
rows_tr, rows_te = rows[:SPLIT], rows[SPLIT:]
res = []
for lb, am, tm in itertools.product([20,40,55,90],[2.0,3.0,4.0],[2.0,3.0]):
    for cs, cn in ((0.0007,"taker"),(0.0002,"maker")):
        s1 = stats(run(rows_tr, lb, am, tm, cs))
        s2 = stats(run(rows_te, lb, am, tm, cs))
        if not s1 or not s2: continue
        res.append((lb, am, tm, cn, s1, s2))
        print("回看%3d ATR%.0f 跟踪%.0f %-5s | 训练 %3d笔 期望%+.2fR t=%5.2f | 测试 %3d笔 期望%+.2fR t=%5.2f %s"
              % (lb, am, tm, cn, s1["n"], s1["mean"], s1["t"], s2["n"], s2["mean"], s2["t"],
                 "<== 两段同为正" if s1["mean"]>0 and s2["mean"]>0 else ""))

print()
both = [r for r in res if r[4]["mean"]>0 and r[5]["mean"]>0]
print("训练+测试都为正的配置：%d / %d" % (len(both), len(res)))
sig = [r for r in both if r[5]["t"] > 1.5]
print("其中测试段 t>1.5 的：%d" % len(sig))
for r in both[:6]:
    print("  回看%d ATR%.0f 跟踪%.0f %s -> 训练%+.2fR(t=%.2f) 测试%+.2fR(t=%.2f)" % (
        r[0], r[1], r[2], r[3], r[4]["mean"], r[4]["t"], r[5]["mean"], r[5]["t"]))
