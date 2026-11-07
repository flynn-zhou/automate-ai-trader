# -*- coding: utf-8 -*-
"""策略持仓期间的资金费，用真实结算数据算。

关键区别：全局平均资金费包含了大量空仓时段。
但策略只在价格上穿 SMA200 时做多——那正是牛市、也是资金费最高的时候。
用全局平均会低估策略实际付出的资金费。
"""
import json, os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools.backtest as bt

F = "data/funding_btcusdt.json"
if not os.path.exists(F):
    print("缺资金费缓存，先跑 tools/funding_true_cost.py")
    sys.exit(1)
fund = json.load(open(F))
fmap = {}
for x in fund:
    fmap[int(x["fundingTime"])] = float(x["fundingRate"])

rows = [r for r in bt.load("data/spot.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
T = [r[0] for r in rows]
N = len(rows)
n = 200

def sma(i):
    if i < n:
        return None
    return sum(C[i - n:i]) / n

# 逐根 4h 判断是否持仓（和回测口径一致：收盘价在均线上方即在市）
in_mkt = [False] * N
for i in range(n + 1, N):
    s = sma(i)
    if s is not None:
        in_mkt[i] = C[i] > s

MS8H = 8 * 3600 * 1000
held, allr = [], []
for i in range(N):
    t = T[i]
    span = (T[i + 1] - t) if i + 1 < N else MS8H
    # 这根 4h 覆盖 1 次资金费结算（8h 三班倒，4h 覆盖 1~2 次，取最近的）
    # 用"最接近这根 K 线中点"的结算时刻
    mid = t + span // 2
    best = min(fmap.keys(), key=lambda x: abs(x - mid))
    if abs(best - mid) <= span:
        allr.append(fmap[best])
        if in_mkt[i]:
            held.append(fmap[best])

frac = sum(in_mkt) / N
print("4h 根数 %d  %.2f 年" % (N, N * 4 / 24 / 365))
print()
if allr:
    print("【全局】   样本 %d   均值 %.6f%%/8h   年化 %.2f%%"
          % (len(allr), sum(allr) / len(allr) * 100,
             sum(allr) / len(allr) * 3 * 365 * 100))
if held:
    a = sum(held) / len(held)
    print("【持仓期】 样本 %d   均值 %.6f%%/8h   持有年化 %.2f%%"
          % (len(held), a * 100, a * 3 * 365 * 100))
    print("           按在场 %.1f%% 折算到权益：%.2f%%/年"
          % (frac * 100, a * 3 * 365 * frac * 100))
    print()
    print("对比：现货手续费 0.20%%/往返 x 213 笔 / 6.84 年 = %.2f%%/年（按权益）"
          % (0.002 * 213 / 6.84 * 100))
    print("      永续手续费 0.10%%/往返 x 213 笔 / 6.84 年 = %.2f%%/年（按权益）"
          % (0.001 * 213 / 6.84 * 100))
