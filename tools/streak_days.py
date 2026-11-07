# -*- coding: utf-8 -*-
"""连续亏损换算成真实日历天数。

为什么不能乘平均持仓时间：
  交易之间还有"空仓等待"的日子，而等待的中位数就有 0.8 天、最长 56 天。
  13 连亏 × 17 天持仓是低估——真实经历的时间跨度要大得多。

用户真正感受到的是："我已经 X 个月没赚到钱了"。
这个数字必须用真实时间戳算，不能估算。
"""
import os, sys, datetime
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

COST = 0.001
rows = [r for r in bt.load("data/spot.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
T = [r[0] for r in rows]
N = len(rows)
NL = 200

def day(ms):
    return datetime.datetime.utcfromtimestamp(ms / 1000)

seq = []   # (exit_ts, ret_pct)
i = NL + 1
pos = False
entry = 0.0
while i < N:
    s = sum(C[i - NL:i]) / NL
    sp = sum(C[i - 1 - NL:i - 1]) / NL
    if not pos:
        if C[i] > s and C[i - 1] <= sp:
            pos = True; entry = C[i]
    else:
        if C[i] < s:
            seq.append((T[i], ((C[i] - entry) / entry - 2 * COST) * 100))
            pos = False
    i += 1
if pos:
    seq.append((T[N - 1], ((C[N - 1] - entry) / entry - 2 * COST) * 100))

print("=" * 72)
print("连续亏损的真实时间跨度   %d 笔" % len(seq))
print("=" * 72)

cur = 0
best_run = []
best_span = 0.0
start_ts = None
longest = []

for ts, ret in seq:
    if ret <= 0:
        if cur == 0:
            start_ts = ts
        cur += 1
    else:
        if cur > 0:
            span = (ts - start_ts) / 86400000.0
            if span > best_span:
                best_span = span
                longest = [(start_ts, ts, cur, span)]
            elif span == best_span:
                longest.append((start_ts, ts, cur, span))
        cur = 0

if cur > 0:
    span = (seq[-1][0] - start_ts) / 86400000.0
    if span > best_span:
        best_span = span
        longest = [(start_ts, seq[-1][0], cur, span)]

# 完整统计所有连亏段的日历跨度
runs = []
cur = 0
start_ts = None
for ts, ret in seq:
    if ret <= 0:
        if cur == 0:
            start_ts = ts
        cur += 1
    else:
        if cur > 0:
            runs.append((cur, (ts - start_ts) / 86400000.0))
        cur = 0
if cur > 0:
    runs.append((cur, (seq[-1][0] - start_ts) / 86400000.0))

print()
print("  最长连亏：%d 笔" % max(r[0] for r in runs))
for st, en, cnt, span in longest:
    print("     %s -> %s   %d 笔   %.0f 天（%.1f 个月）"
          % (day(st).strftime("%Y-%m-%d"), day(en).strftime("%Y-%m-%d"),
             cnt, span, span / 30.4))
print()
spans = sorted(r[1] for r in runs)
print("  所有连亏段（共 %d 段）的日历跨度：" % len(runs))
print("     中位 %.0f 天   平均 %.0f 天" % (spans[len(spans) // 2], sum(spans) / len(spans)))
print("     P75 %.0f 天   P90 %.0f 天   最长 %.0f 天"
      % (spans[int(len(spans) * 0.75)], spans[int(len(spans) * 0.9)], spans[-1]))
print()
print("  连亏超过 90 天的段数：%d / %d" %
      (sum(1 for s in spans if s > 90), len(spans)))
