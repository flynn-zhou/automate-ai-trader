# -*- coding: utf-8 -*-
"""策略体检：这 +474% 到底靠什么赚来的？

用户只知道"100U 想赚钱"。我有责任说清楚：
这笔钱是靠多少次交易、多大胜率、多少集中度赚来的。
如果收益全靠两三笔，那这个策略就是脆弱的，不能当成稳定收益来期待。
"""
import sys
sys.path.insert(0, ".")
import tools.backtest as bt

FEE = 0.001
NL = 200
rows = [b for b in bt.load("data/backtest.db", "4h") if b[5] > 0]
C = [b[4] for b in rows]
T = [b[0] for b in rows]
N = len(C)

trades = []
pos = False
entry = 0.0
entry_i = 0
for i in range(NL + 1, N):
    s_now = sum(C[i-NL:i]) / NL
    s_prev = sum(C[i-1-NL:i-1]) / NL
    above, prev = C[i] > s_now, C[i-1] > s_prev
    if not pos and above and not prev:
        pos, entry, entry_i = True, C[i], i
    elif pos and (not above) and prev:
        gross = (C[i] - entry) / entry
        net = (1 + gross) * (1 - FEE) ** 2 - 1
        trades.append({"i0": entry_i, "i1": i, "entry": entry, "exit": C[i],
                       "net": net, "days": (T[i]-T[entry_i])/86400000})
        pos = False

print("交易笔数: %d" % len(trades))
wins = [t for t in trades if t["net"] > 0]
losses = [t for t in trades if t["net"] <= 0]
print("胜率: %.1f%% (%d 胜 / %d 负)" % (len(wins)/len(trades)*100, len(wins), len(losses)))
print("平均盈利: %+.2f%%   平均亏损: %+.2f%%"
      % (sum(t["net"] for t in wins)/len(wins)*100,
         sum(t["net"] for t in losses)/len(losses)*100))
avg_w = sum(t["net"] for t in wins)/len(wins)
avg_l = sum(t["net"] for t in losses)/len(losses)
print("盈亏比: %.2f" % (avg_w/abs(avg_l)))
print("平均持仓: %.1f 天（中位 %.1f）" % (
    sum(t["days"] for t in trades)/len(trades),
    sorted(t["days"] for t in trades)[len(trades)//2]))

# 集中度：靠多少笔赚来的
srt = sorted(trades, key=lambda x: -x["net"])
print()
print("收益集中度（按单笔净收益排序）:")
tot = sum(t["net"] for t in trades)
for k in (1, 3, 5, 10):
    s = sum(t["net"] for t in srt[:k])
    print("   前 %2d 笔合计 %+.0f%%  （占全部收益的 %.0f%%）"
          % (k, s*100, s/tot*100))
print()
print("单笔最大盈利: %+.1f%%   最大亏损: %+.1f%%"
      % (srt[0]["net"]*100, srt[-1]["net"]*100))
print("最好 3 笔若全部错过，总收益会变成: %+.0f%%"
      % ((tot - sum(t["net"] for t in srt[:3]))*100))
