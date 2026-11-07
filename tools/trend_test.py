# -*- coding: utf-8 -*-
"""趋势跟踪检验：把 1m 数据重采样到 5m/15m/1h/4h，测 Donchian 突破 + 移动止损。

为什么换这个方向：
1m 动量信号已证伪（预测力显著为负、幅度 0.02% 远小于成本 0.11%）。
唯一可能活下来的形态是"长尺度趋势 + 宽止损"——
因为宽止损把费用/风险比压到 2-5%，而趋势的盈亏比可以到 3-10 倍。

同时输出 taker(0.05%/边) 与 maker(0.02%/边) 两套成本，
用来判断"降成本"是不是能救活策略。
"""
import os, sys, itertools, statistics
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

rows = bt.load("data/backtest.db", "1m")

def resample(rs, k):
    out = []
    for i in range(0, len(rs) - k + 1, k):
        seg = rs[i:i+k]
        out.append((seg[0][0], seg[0][1],
                    max(b[2] for b in seg), min(b[3] for b in seg),
                    seg[-1][4], sum(b[5] for b in seg),
                    sum(b[8] for b in seg) if seg[0][8] is not None else 0))
    return out

def atr_of(bs, i, n):
    if i < n + 1: return 0.0
    trs = []
    for k in range(i - n, i):
        h, l, pc = bs[k][2], bs[k][3], bs[k-1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs)/len(trs) if trs else 0.0

def trend_run(bs, lookback, atr_mult, trail_r, fee_side, timeout, risk=35.0, lev_cap=20.0, eq=100.0):
    trades = []
    i = lookback + 20
    pos = None
    while i < len(bs) - 1:
        row = bs[i]
        h, l, c = row[2], row[3], row[4]
        if pos is None:
            hi = max(b[2] for b in bs[i-lookback:i])
            lo = min(b[3] for b in bs[i-lookback:i])
            a = atr_of(bs, i, 14)
            if a <= 0:
                i += 1; continue
            side = None
            if c > hi: side = "long"
            elif c < lo: side = "short"
            if side:
                d = 1 if side == "long" else -1
                stop_dist = atr_mult * a
                stop_pct = stop_dist / c * 100
                if not (0.3 <= stop_pct <= 15.0):
                    i += 1; continue
                qty = min(eq*lev_cap/c, (eq*risk/100.0)/stop_dist)
                pos = {"side": side, "d": d, "entry": c, "stop": c - d*stop_dist,
                       "risk": stop_dist, "qty": qty, "i0": i, "best": c, "trailed": False}
        else:
            pos["best"] = max(pos["best"], h) if pos["d"] > 0 else min(pos["best"], l)
            # 移动止损：盈利达到 trail_r 倍风险后，止损跟到 best - atr_mult*ATR
            gain = (pos["best"] - pos["entry"]) * pos["d"]
            if not pos["trailed"] and gain >= trail_r * pos["risk"]:
                pos["trailed"] = True
            if pos["trailed"]:
                a = atr_of(bs, i, 14)
                if a > 0:
                    new_stop = pos["best"] - pos["d"] * atr_mult * a
                    pos["stop"] = max(pos["stop"], new_stop) if pos["d"] > 0 else min(pos["stop"], new_stop)
            hit = l <= pos["stop"] if pos["d"] > 0 else h >= pos["stop"]
            reason = "stop" if hit else ("timeout" if i - pos["i0"] >= timeout else "")
            if reason:
                px = pos["stop"] if hit else c
                gross = (px - pos["entry"]) * pos["qty"] * pos["d"]
                fee = (pos["entry"] + px) * pos["qty"] * fee_side
                trades.append({"gross": gross, "fee": fee, "net": gross - fee, "reason": reason,
                               "bars": i - pos["i0"]})
                pos = None
        i += 1
    return trades

def stat(tr):
    if not tr: return None
    n = len(tr)
    w = [t for t in tr if t["net"] > 0]; lo_ = [t for t in tr if t["net"] <= 0]
    net = sum(t["net"] for t in tr); g = sum(t["gross"] for t in tr); f = sum(t["fee"] for t in tr)
    aw = sum(t["net"] for t in w)/len(w) if w else 0
    al = abs(sum(t["net"] for t in lo_))/len(lo_) if lo_ else 0
    wr = len(w)/n
    be = al/(aw+al) if (aw+al) > 0 else None
    return {"n": n, "wr": wr, "net": net, "gross": g, "fee": f, "odds": aw/al if al else 0,
            "edge": (wr-be) if be else 0, "bars": statistics.mean([t["bars"] for t in tr])}

SCALES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
print("尺度  回看  ATR倍 成本    笔数  胜率   赔率   平均持仓   毛U      费U     净U     边际")
results = []
for scale, k in SCALES.items():
    bs = resample(rows, k)
    if len(bs) < 200:
        continue
    for lookback, atr_mult in itertools.product([20, 55, 120], [2.0, 3.0, 4.0]):
        for fee_side, fname in ((0.0007, "taker"), (0.0003, "maker")):
            tr = trend_run(bs, lookback, atr_mult, 1.5, fee_side, timeout=10**9)
            s = stat(tr)
            if not s or s["n"] < 8:
                continue
            print("%-5s %4d  %4.1f %-6s %4d  %5.1f%% %5.2f %8.0f根 %8.1f %7.1f %8.1f  %+6.1f%%" % (
                scale, lookback, atr_mult, fname, s["n"], s["wr"]*100, s["odds"],
                s["bars"], s["gross"], s["fee"], s["net"], s["edge"]*100))
            results.append((s["net"], scale, lookback, atr_mult, fname, s))

print()
results.sort(reverse=True)
print("=== 净盈亏前 8 ===")
for net, sc, lb, am, fn, s in results[:8]:
    print("  %-4s 回看%3d ATR%.1f %-5s -> %3d笔 胜率%.0f%% 赔率%.2f 净%+8.1fU 边际%+.1f%%" % (
        sc, lb, am, fn, s["n"], s["wr"]*100, s["odds"], net, s["edge"]*100))
print()
pos = [r for r in results if r[0] > 0]
print("净为正的配置：%d / %d" % (len(pos), len(results)))
