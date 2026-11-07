# -*- coding: utf-8 -*-
"""关键发现验证：毛盈亏是正的，净盈亏被手续费吃光。

那答案就只有一个：减少交易频率 + 放大单笔目标。
这里扫描"信号阈值 x 目标倍数"，看毛/费比怎么变。
"""
import os, sys, itertools
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

bars = bt.load("data/backtest.db", "1m")

def stat(tr):
    if not tr: return None
    n = len(tr)
    wins = [t for t in tr if t["net"] > 0]
    losses = [t for t in tr if t["net"] <= 0]
    net = sum(t["net"] for t in tr); gross = sum(t["gross"] for t in tr); fee = sum(t["fee"] for t in tr)
    aw = sum(t["net"] for t in wins)/len(wins) if wins else 0
    al = abs(sum(t["net"] for t in losses))/len(losses) if losses else 0
    wr = len(wins)/n
    be = al/(aw+al) if (aw+al) > 0 else None
    return {"n": n, "wr": wr, "net": net, "gross": gross, "fee": fee,
            "ratio": gross/fee if fee else 0, "edge": (wr-be) if be else 0}

print("止损%%  RR   信号σ  笔数   胜率   毛U      费U     毛/费   净U      边际")
rows = []
for min_stop, rr, ms in itertools.product([0.8, 1.5], [1.2, 1.6, 2.5], [3.0, 5.0, 7.0, 9.0]):
    bt.MIN_STOP = min_stop; bt.MAX_STOP = max(min_stop*2, 6.0)
    bt.RR = rr; bt.LEV_CAP = 20.0; bt.RISK_PCT = 35.0
    tr = bt.run(bars, {"sig": "all", "micro_sigma": ms})
    s = stat(tr)
    if not s:
        print("%4.1f  %4.1f  %5.1f  --  无交易" % (min_stop, rr, ms)); continue
    print("%4.1f  %4.1f  %5.1f  %5d  %5.1f%%  %8.1f  %7.1f  %5.2f  %8.1f  %+6.1f%%" % (
        min_stop, rr, ms, s["n"], s["wr"]*100, s["gross"], s["fee"], s["ratio"], s["net"], s["edge"]*100))
    rows.append((s["net"], min_stop, rr, ms, s))

print()
rows.sort(reverse=True)
print("=== 净盈亏最好的 5 组 ===")
for net, ms_, rr_, mss, s in rows[:5]:
    print("  止损>=%.1f%% RR=%.1f 信号>=%.0fσ -> %d笔 毛%.0f 费%.0f 净%.0fU 边际%+.1f%%" % (
        ms_, rr_, mss, s["n"], s["gross"], s["fee"], net, s["edge"]*100))
print()
pos = [r for r in rows if r[0] > 0]
print("净为正的配置数：%d / %d" % (len(pos), len(rows)))
