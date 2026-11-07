# -*- coding: utf-8 -*-
"""参数扫描：在 120 天 1m 数据上找有没有任何一组参数是正期望的。

原则：一次只改一个维度，看边际怎么变；任何"选出来的最优"都要用另一段时间验证。
"""
import itertools, os, sys
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

bars = bt.load("data/backtest.db", "1m")
print("bars:", len(bars))

def run_variant(min_stop, max_stop, rr, micro_sigma, lev_cap, risk_pct):
    bt.MIN_STOP = min_stop
    bt.MAX_STOP = max_stop
    bt.RR = rr
    bt.LEV_CAP = lev_cap
    bt.RISK_PCT = risk_pct
    cfg = {"sig": "all", "micro_sigma": micro_sigma}
    return bt.run(bars, cfg)

def stat(tr):
    if not tr:
        return None
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
    return {"n": n, "wr": wr, "net": net, "fee": fee, "gross": gross,
            "odds": (aw/al if al else 0), "be": be, "edge": (wr-be) if be else None,
            "exp": net/n}

print()
print("止损宽   RR   杠杆上限  笔数  胜率   赔率   净U      费用U    边际")
best = None
for min_stop, rr, lev in itertools.product([0.4, 0.8, 1.5, 2.5], [1.2, 1.6, 2.5, 4.0], [20, 60]):
    tr = run_variant(min_stop, max(min_stop*2, 6.0), rr, 3.0, lev, 35.0)
    s = stat(tr)
    if not s:
        print("%5.1f%%  %4.1f  %6d   --     无交易" % (min_stop, rr, lev))
        continue
    print("%5.1f%%  %4.1f  %6d  %5d  %5.1f%%  %5.2f  %8.1f  %7.1f  %+6.1f%%" % (
        min_stop, rr, lev, s["n"], s["wr"]*100, s["odds"], s["net"], s["fee"], s["edge"]*100))
    if best is None or s["net"] > best[0]:
        best = (s["net"], min_stop, rr, lev, s)
print()
if best:
    print("最好的一组：止损>=%.1f%% RR=%.1f 杠杆<=%dx -> 净 %.1fU 胜率 %.1f%% 赔率 %.2f 边际 %+.1f%%" % (
        best[1], best[2], best[3], best[0], best[4]["wr"]*100, best[4]["odds"], best[4]["edge"]*100))
