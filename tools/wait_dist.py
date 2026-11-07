# -*- coding: utf-8 -*-
"""空仓要等多久？这是用户最直接的体感，也是判断"卡住了"和"正常"的依据。

为什么必须量化：
  现在是空仓等待状态，价格比 SMA200 低 5.45%。
  如果正常等待期是两三个月，那用户看到"一直没交易"是正常的；
  如果正常是一两周，那说明有问题。
  没有这个分布，我无法区分「策略在等」和「策略坏了」。
"""
import os, sys, statistics
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

rows = [r for r in bt.load("data/spot.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
T = [r[0] for r in rows]
N = len(rows)
NL = 200
IV_H = 4.0

def sma(i):
    if i < NL:
        return None
    return sum(C[i - NL:i]) / NL

inm = []
for i in range(NL + 1, N):
    s = sma(i)
    inm.append(C[i] > s if s is not None else False)

# 游程：连续在市的长度 / 连续空仓的长度
runs_in, runs_out = [], []
cur = inm[0]; length = 1
for v in inm[1:]:
    if v == cur:
        length += 1
    else:
        (runs_in if cur else runs_out).append(length)
        cur = v; length = 1
(runs_in if cur else runs_out).append(length)

def stat(name, runs, unit_h=IV_H):
    if not runs:
        print("%s: 无样本" % name)
        return
    days = sorted(r * unit_h / 24.0 for r in runs)
    n = len(days)
    print("%s  样本 %d" % (name, n))
    print("     中位 %.1f 天   平均 %.1f 天" % (days[n//2], sum(days)/n))
    print("     最短 %.1f   最长 %.1f" % (days[0], days[-1]))
    print("     P75 %.1f   P90 %.1f   P95 %.1f"
          % (days[int(n*0.75)], days[int(n*0.90)], days[min(n-1, int(n*0.95))]))

print("4h 根数 %d  %.2f 年" % (N, N*IV_H/24/365))
print()
stat("【持仓段】", runs_in)
print()
stat("【空仓段】", runs_out)

print()
print("=== 当前这段空仓进行到哪了 ===")
# 从末尾往回数连续空仓
k = 0
for v in reversed(inm):
    if not v:
        k += 1
    else:
        break
print("     已空仓 %d 根 4h = %.1f 天" % (k, k * IV_H / 24))
if runs_out:
    days = sorted(r * IV_H / 24.0 for r in runs_out)
    pct = sum(1 for d in days if d <= k * IV_H / 24.0) / len(days) * 100
    print("     历史上 %.0f%% 的空仓段比当前更短（当前属于较长的那批时要注意）" % pct)
print()
print("     当前价 %.1f   SMA200 %.1f   差 %.2f%%"
      % (C[-1], sma(N-1), (C[-1]/sma(N-1)-1)*100))
if runs_out:
    longest = max(r * IV_H / 24.0 for r in runs_out)
    print("     历史最长空仓 %.0f 天；超过这个数就不是「正常等待」了" % longest)
