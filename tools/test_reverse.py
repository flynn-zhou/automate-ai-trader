# -*- coding: utf-8 -*-
"""方向检验：动量信号亏钱，是因为没边际，还是因为方向反了？

做法：同一批信号，把方向取反（追涨 -> 高抛）。如果反向后显著转正，
说明市场在这一尺度上是均值回归的，而不是趋势的。
"""
import os, sys
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

bars = bt.load("data/backtest.db", "1m")
orig_detect = bt.detect

def reversed_detect(b, i, cfg):
    r = orig_detect(b, i, cfg)
    if not r:
        return None
    side, hint, tag = r
    price = b[i-1][4]
    new_side = "short" if side == "long" else "long"
    # 反向的止损位也翻到另一边
    hi, lo = bt.swing(b, i, 20)
    new_hint = hi if new_side == "short" else lo
    return (new_side, new_hint, tag + "_rev")

def stat(tr, label):
    if not tr:
        print("%s: 无交易" % label); return
    n = len(tr)
    wins = [t for t in tr if t["net"] > 0]
    losses = [t for t in tr if t["net"] <= 0]
    net = sum(t["net"] for t in tr)
    gross = sum(t["gross"] for t in tr)
    fee = sum(t["fee"] for t in tr)
    aw = sum(t["net"] for t in wins)/len(wins) if wins else 0
    al = abs(sum(t["net"] for t in losses))/len(losses) if losses else 0
    wr = len(wins)/n
    be = al/(aw+al) if (aw+al) > 0 else None
    print("%-22s 笔数 %4d  胜率 %5.1f%%  赔率 %5.2f  毛 %8.1f  费 %7.1f  净 %8.1f  边际 %+6.1f%%" % (
        label, n, wr*100, (aw/al if al else 0), gross, fee, net, (wr-be)*100 if be else 0))

print()
print("=== 同一批信号，方向对照 ===")
for min_stop, rr, lev in [(0.4, 1.6, 60), (1.5, 1.6, 20), (2.5, 2.5, 20), (0.8, 1.2, 20), (1.5, 1.2, 20)]:
    bt.MIN_STOP = min_stop; bt.MAX_STOP = max(min_stop*2, 6.0)
    bt.RR = rr; bt.LEV_CAP = lev; bt.RISK_PCT = 35.0
    cfg = {"sig": "all", "micro_sigma": 3.0}
    bt.detect = orig_detect
    s1 = bt.run(bars, cfg)
    bt.detect = reversed_detect
    s2 = bt.run(bars, cfg)
    stat(s1, "顺势 止损%.1f%% RR%.1f" % (min_stop, rr))
    stat(s2, "  反向 止损%.1f%% RR%.1f" % (min_stop, rr))
    print()
bt.detect = orig_detect
