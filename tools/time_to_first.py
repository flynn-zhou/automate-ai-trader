# -*- coding: utf-8 -*-
"""注资之后多久能开第一笔仓？

这直接决定用户该在什么时候把钱放进来。
因为入场规则认的是"上穿"这个事件：
  - 现在价格在均线【下方】-> 等一次上穿即可（我们的实际状态）
  - 现在价格在均线【上方】-> 上穿早已发生，要么靠启动对账补，要么等一整轮
"""
import os, sys, statistics
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

rows = [r for r in bt.load("data/spot.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
N = len(rows)
NL = 200
H = 4.0 / 24.0   # 每根 4h 折合天数

def sma(i):
    if i < NL:
        return None
    return sum(C[i - NL:i]) / NL

above = []
for i in range(N):
    s = sma(i)
    above.append(None if s is None else C[i] > s)

# 对每个位置，找下一次"上穿"（False -> True）要等几根
wait_below, wait_above = [], []   # 从均线下方起步 / 从均线上方起步
for i in range(NL + 1, N):
    if above[i] is None or above[i - 1] is None:
        continue
    j = i + 1
    found = None
    while j < N:
        if above[j - 1] is False and above[j] is True:
            found = j
            break
        j += 1
    if found is None:
        continue
    d = (found - i) * H
    (wait_above if above[i] else wait_below).append(d)

def rep(name, xs):
    if not xs:
        print(name, "无样本"); return
    xs = sorted(xs)
    n = len(xs)
    print("%s  样本 %d" % (name, n))
    print("     中位 %.1f 天   平均 %.1f 天" % (xs[n//2], sum(xs)/n))
    print("     P25 %.1f   P75 %.1f   P90 %.1f   最长 %.1f"
          % (xs[n//4], xs[int(n*0.75)], xs[int(n*0.9)], xs[-1]))
    print("     3 天内 %.0f%%   7 天内 %.0f%%   30 天内 %.0f%%"
          % (sum(1 for x in xs if x <= 3)/n*100,
             sum(1 for x in xs if x <= 7)/n*100,
             sum(1 for x in xs if x <= 30)/n*100))

print("4h 根数 %d  %.2f 年" % (N, N*4/24/365))
print()
rep("【从均线下方注资】", wait_below)
print()
rep("【从均线上方注资】", wait_above)
print()
print("=== 当前状态 ===")
s_last = sma(N - 1)
print("  现价 %.1f   SMA200 %.1f   差 %.2f%%  -> 在均线%s"
      % (C[-1], s_last, (C[-1]/s_last - 1)*100, "上方" if C[-1] > s_last else "下方"))
if C[-1] < s_last:
    print("  注资后等下一次上穿即可，参考上面第一组分布。")
