# -*- coding: utf-8 -*-
"""那 3 笔"决定命运"的交易发生在什么时候？系统停机多久会错过？

既然前 3 笔贡献了 73% 的收益，就必须知道：
- 它们各自持续多久？
- 如果进程在入场那一刻挂了，多久之内恢复还能赶上？
"""
import sys
sys.path.insert(0, ".")
import tools.backtest as bt
from datetime import datetime, timezone

FEE = 0.001
NL = 200
rows = [b for b in bt.load("data/backtest.db", "4h") if b[5] > 0]
C = [b[4] for b in rows]; T = [b[0] for b in rows]; N = len(C)

trades = []
pos = False; entry = 0.0; entry_i = 0
for i in range(NL + 1, N):
    s_now = sum(C[i-NL:i]) / NL
    s_prev = sum(C[i-1-NL:i-1]) / NL
    above, prev = C[i] > s_now, C[i-1] > s_prev
    if not pos and above and not prev:
        pos, entry, entry_i = True, C[i], i
    elif pos and (not above) and prev:
        net = (1 + (C[i]-entry)/entry) * (1-FEE)**2 - 1
        trades.append({"i0": entry_i, "i1": i, "entry": entry, "exit": C[i],
                       "net": net, "days": (T[i]-T[entry_i])/86400000})
        pos = False

def fmt(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%Y-%m-%d")

srt = sorted(trades, key=lambda x: -x["net"])
print("决定命运的 5 笔:")
print("%-12s %-12s %10s %10s %8s" % ("入场", "出场", "入场价", "出场价", "收益"))
for t in srt[:5]:
    print("%-12s %-12s %10.0f %10.0f %+7.1f%%"
          % (fmt(T[t["i0"]]), fmt(T[t["i1"]]), t["entry"], t["exit"], t["net"]*100))

print()
print("这 5 笔的持仓时长: %s 天" % [round(t["days"], 1) for t in srt[:5]])

# 入场后第一根 4h 的涨幅：如果入场时挂了，晚多久就追不上
print()
print("入场那一刻没上车的话，代价有多大:")
for t in srt[:3]:
    e0 = t["entry"]
    row = rows[t["i0"]]
    print("  %s 入场价 %.0f -> 该根最高 %.0f (%.1f%%)"
          % (fmt(T[t["i0"]]), e0, row[2], (row[2]-e0)/e0*100))
