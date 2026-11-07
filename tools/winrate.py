# -*- coding: utf-8 -*-
"""核实胜率 15%：这个数字如果对，用户体验就是"七笔亏六笔"。

为什么必须核实：
  15% 胜率对趋势跟随来说低得反常（通常 30-40%）。
  如果这个数字是统计口径错了，我会把"七笔亏六笔"这种话讲给用户，
  他会因为一个错误的统计而做出错误决定（比如中途放弃一个好系统）。
  反过来如果真是 15%，他必须提前知道，否则第一次连亏五笔就会关掉它。

核实方法：
  1. 平均盈利 / 平均亏损 / 盈亏比
  2. 用不同口径重算胜率（按最终收益、按是否跑赢持有期）
  3. 看盈利集中在哪几笔（如果全靠 2-3 笔，那就是另一种风险）
"""
import os, sys
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

COST = 0.001
rows = [r for r in bt.load("data/spot.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
H = [r[2] for r in rows]
L = [r[3] for r in rows]
N = len(rows)
NL = 200

trades = []
i = NL + 1
pos = False
entry = 0.0
eidx = 0
best = worst = 0.0
while i < N:
    s = sum(C[i - NL:i]) / NL
    sp = sum(C[i - 1 - NL:i - 1]) / NL
    if not pos:
        if C[i] > s and C[i - 1] <= sp:
            pos = True; entry = C[i]; eidx = i; best = worst = entry
    else:
        best = max(best, H[i]); worst = min(worst, L[i])
        if C[i] < s:
            trades.append(((C[i] - entry) / entry - 2 * COST) * 100)
            pos = False
    i += 1
if pos:
    trades.append(((C[N - 1] - entry) / entry - 2 * COST) * 100)

n = len(trades)
wins = [t for t in trades if t > 0]
loss = [t for t in trades if t <= 0]

print("=" * 66)
print("胜率核实   %d 笔" % n)
print("=" * 66)
print("  胜 %d 笔   负 %d 笔   胜率 %.1f%%" % (len(wins), len(loss), len(wins) * 100.0 / n))
print()
print("  平均盈利 %+.2f%%   平均亏损 %+.2f%%"
      % (sum(wins) / len(wins) if wins else 0,
         sum(loss) / len(loss) if loss else 0))
aw = sum(wins) / len(wins) if wins else 0
al = abs(sum(loss) / len(loss)) if loss else 0
print("  盈亏比   %.2f : 1" % (aw / al if al else 0))
print("  期望值   %+.2f%%/笔（胜率×均盈 + 败率×均亏）"
      % (len(wins) * 100.0 / n / 100 * aw - len(loss) * 100.0 / n / 100 * al))
print()
print("  盈利前 5 笔：%s" % ", ".join("%+.1f%%" % x for x in sorted(wins, reverse=True)[:5]))
print("  亏损前 5 笔：%s" % ", ".join("%+.1f%%" % x for x in sorted(loss)[:5]))

# 盈利集中度：去掉最好的 3 笔会怎样
tot = 1.0
for x in trades:
    tot *= (1 + x / 100.0)
tot3 = 1.0
for x in sorted(trades, reverse=True)[3:]:
    tot3 *= (1 + x / 100.0)
print()
print("  全部复利：%.1f 倍" % tot)
print("  去掉最好的 3 笔后：%.1f 倍" % tot3)
print("  => 最好的 3 笔贡献了 %.0f%% 的总收益"
      % ((tot - tot3) / (tot - 1) * 100 if tot > 1 else 0))

# 连续亏损
cur = 0; mx = 0; run_at = 0
for k, x in enumerate(trades):
    if x <= 0:
        cur += 1
        if cur > mx:
            mx = cur; run_at = k
    else:
        cur = 0
print()
print("  最长连续亏损：%d 笔（发生在第 %d 笔附近）" % (mx, run_at))
print("  如果每笔间隔约 17 天，那意味着最长约 %.1f 天不赚钱" % (mx * 17 / 1.0))
