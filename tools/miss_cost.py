# -*- coding: utf-8 -*-
"""漏掉一次进场有多大代价？

这个策略 85% 的收益来自最好的 3 笔交易，胜率只有 15%。
那么【漏掉一笔】就不是"少赚一点"，而是"整体收益腰斩"。

这直接决定可靠性该投在哪：
  如果漏 1 笔只损失 5%，那下单可靠性可以慢慢做；
  如果漏 1 笔损失一半，那"进场必须万无一失"就是最高优先级。

配合已知事实：
  每 4h 才有一次判断点，上穿事件必须被"看见"，
  否则要等下一轮完整周期（中位 9 天，最长 56 天）。
"""
import os, sys
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

COST = 0.001
rows = [r for r in bt.load("data/spot.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
N = len(rows)
NL = 200

def run(skip_idx=()):
    eq = 1.0
    pos = False
    entry = 0.0
    k = 0
    for i in range(NL + 1, N):
        s = sum(C[i - NL:i]) / NL
        sp = sum(C[i - 1 - NL:i - 1]) / NL
        if not pos:
            if C[i] > s and C[i - 1] <= sp:
                if k in skip_idx:
                    k += 1
                    continue        # 漏掉这次进场
                pos = True; entry = C[i]; k += 1
        else:
            if C[i] < s:
                eq *= (1 + (C[i] - entry) / entry - 2 * COST)
                pos = False
    if pos:
        eq *= (1 + (C[N - 1] - entry) / entry - 2 * COST)
    return eq

# 找出每笔交易的收益，定位最好的几笔
trades = []
pos = False; entry = 0.0; k = 0
for i in range(NL + 1, N):
    s = sum(C[i - NL:i]) / NL
    sp = sum(C[i - 1 - NL:i - 1]) / NL
    if not pos:
        if C[i] > s and C[i - 1] <= sp:
            pos = True; entry = C[i]
    else:
        if C[i] < s:
            trades.append(((C[i] - entry) / entry - 2 * COST) * 100)
            pos = False
if pos:
    trades.append(((C[N - 1] - entry) / entry - 2 * COST) * 100)

order = sorted(range(len(trades)), key=lambda x: trades[x], reverse=True)

base_eq = run()
print("=" * 62)
print("漏掉进场机会的代价")
print("=" * 62)
print("  完整执行          %.2f 倍" % base_eq)
print()
for k_ in (1, 2, 3, 5):
    skip = set(order[:k_])
    eq = run(skip)
    lost = (base_eq - eq) / (base_eq - 1) * 100
    print("  漏掉最好的 %d 笔    %.2f 倍   收益少了 %.0f%%" % (k_, eq, lost))
print()
# 漏掉最差的一笔会怎样（说明亏损单反而"有益"？不会，但值得看）
skip_worst = {order[-1]}
eq = run(skip_worst)
print("  漏掉最差 1 笔      %.2f 倍（对比完整 %.2f 倍）" % (eq, base_eq))
print()
print("  单笔最大收益 %.1f%%，占全部复利增长的绝大部分。" % trades[order[0]])
