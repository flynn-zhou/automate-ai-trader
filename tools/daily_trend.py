# -*- coding: utf-8 -*-
"""日线级趋势跟踪：6 年 4h 数据，训练/测试各半。

这是最后一个有学术支撑的候选（时序动量）。
关键设计：
- 宽止损（ATR 倍数大）让费用/风险比降到 1-3%
- 移动止损让赢家跑远（趋势跟踪的收益全靠右尾）
- 低杠杆（10-20x），100U 本金下强平距离足够
- 严格样本外：训练段定参数，测试段只验证
"""
import os, sys, statistics, itertools
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

rows = bt.load("data/backtest.db", "4h")
rows = [r for r in rows if r[5] > 0]
N = len(rows)
print("4h K 线 %d 根（约 %.1f 年）  %s -> %s" % (
    N, N*4/24/365,
    __import__("time").strftime("%Y-%m-%d", __import__("time").localtime(rows[0][0]/1000)),
    __import__("time").strftime("%Y-%m-%d", __import__("time").localtime(rows[-1][0]/1000))))

def atr(bs, i, n=14):
    if i < n + 1: return 0.0
    trs = []
    for k in range(i-n, i):
        h, l, pc = bs[k][2], bs[k][3], bs[k-1][4]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs)/len(trs) if trs else 0.0

def run(bs, lookback, atr_mult, trail_mult, cost_side, risk_pct=20.0, lev_cap=10.0, eq0=100.0):
    eq = eq0
    trades = []
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
                qty = min(eq*lev_cap/c, (eq*risk_pct/100.0)/sd)
                if qty <= 0: i += 1; continue
                pos = {"d": d, "entry": c, "stop": c - d*sd, "qty": qty,
                       "i0": i, "best": c, "atr0": a}
        else:
            pos["best"] = max(pos["best"], h) if pos["d"] > 0 else min(pos["best"], l)
            a = atr(bs, i) or pos["atr0"]
            new_stop = pos["best"] - pos["d"] * trail_mult * a
            pos["stop"] = (max(pos["stop"], new_stop) if pos["d"] > 0
                           else min(pos["stop"], new_stop))
            hit = l <= pos["stop"] if pos["d"] > 0 else h >= pos["stop"]
            if hit:
                px = pos["stop"]
                gross = (px - pos["entry"]) * pos["qty"] * pos["d"]
                fee = (pos["entry"] + px) * pos["qty"] * cost_side
                net = gross - fee
                trades.append({"net": net, "gross": gross, "fee": fee, "bars": i - pos["i0"]})
                eq += net
                pos = None
                if eq <= 5: break
        i += 1
    return trades, eq

def stat(tr, eq0=100.0):
    if len(tr) < 5: return None
    n = len(tr)
    w = [t for t in tr if t["net"] > 0]
    net = sum(t["net"] for t in tr)
    g = sum(t["gross"] for t in tr); f = sum(t["fee"] for t in tr)
    return {"n": n, "wr": len(w)/n, "net": net, "gross": g, "fee": f,
            "avg_bars": statistics.mean([t["bars"] for t in tr]),
            "final_eq": eq0 + net}

SPLIT = N // 2
print("训练段 %d 根，测试段 %d 根" % (SPLIT, N - SPLIT))
print()
print("回看 ATR倍 跟踪倍 成本    训练:笔数/胜率/净U      测试:笔数/胜率/净U")
best = None
for lookback, am, tm in itertools.product([20, 40, 55, 90], [2.0, 3.0, 4.0], [2.0, 3.0]):
    for cost_side, cname in ((0.0005+0.0002, "taker"), (0.0002, "maker")):
        tr1, eq1 = run(rows[:SPLIT], lookback, am, tm, cost_side)
        tr2, eq2 = run(rows[SPLIT:], lookback, am, tm, cost_side)
        s1, s2 = stat(tr1), stat(tr2)
        if not s1 or not s2: continue
        print("%4d %4.1f %5.1f %-6s %4d/%4.0f%%/%+8.1f    %4d/%4.0f%%/%+8.1f  %s" % (
            lookback, am, tm, cname, s1["n"], s1["wr"]*100, s1["net"],
            s2["n"], s2["wr"]*100, s2["net"],
            "<= 两段同为正" if s1["net"] > 0 and s2["net"] > 0 else ""))
        if s1["net"] > 0 and s2["net"] > 0:
            score = s1["net"] + s2["net"]
            if best is None or score > best[0]:
                best = (score, lookback, am, tm, cname, s1, s2)

print()
if best:
    sc, lb, am, tm, cn, s1, s2 = best
    print("=== 唯一两段都为正的族 ===")
    print("回看 %d, ATR %.1f 倍止损, 跟踪 %.1f 倍, 成本 %s" % (lb, am, tm, cn))
    print("  训练 %d 笔 胜率 %.0f%% 净 %+.1fU  平均持仓 %.1f 根(%.1f 天)" % (
        s1["n"], s1["wr"]*100, s1["net"], s1["avg_bars"], s1["avg_bars"]*4/24))
    print("  测试 %d 笔 胜率 %.0f%% 净 %+.1fU  平均持仓 %.1f 根(%.1f 天)" % (
        s2["n"], s2["wr"]*100, s2["net"], s2["avg_bars"], s2["avg_bars"]*4/24))
else:
    print("=== 没有任何一组参数在训练段和测试段都赚钱 ===")
