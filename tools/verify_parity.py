# -*- coding: utf-8 -*-
"""一致性验证：线上的 TrendStrategy 必须和回测口径逐笔一致。

这是防"上线的东西和验证过的东西不是同一个"的最后一道闸。
费用必须用真实执行方式的费率：现货 taker 0.1% 单边。
用 maker 费率去验证、用 taker 去实跑，就等于自己骗自己。
"""
import os
import sys

sys.path.insert(0, os.getcwd())
import tools.backtest as bt
from ai_trader.trading.trend import TrendStrategy
from ai_trader.config import TradingCfg

FEE = 0.001          # 现货 taker 单边
LOOKBACK = 200

rows = [r for r in bt.load("data/backtest.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
N = len(rows)
print("4h K 线：%d 根，费用模型：现货 taker %.2f%% 单边" % (N, FEE * 100))


def sma_at(i, n):
    if i < n:
        return None
    return sum(C[i - n:i]) / n


bt_trades = []
eq, pos, entry = 1.0, False, 0.0
for i in range(LOOKBACK + 1, N):
    s_now = sma_at(i, LOOKBACK)
    s_prev = sma_at(i - 1, LOOKBACK)
    above = C[i] > s_now
    prev = C[i - 1] > s_prev
    if not pos and above and not prev:
        pos, entry = True, C[i]
    elif pos and (not above) and prev:
        r = (C[i] - entry) / entry - 2 * FEE
        eq *= (1 + r)
        bt_trades.append((rows[i][0], entry, C[i], r))
        pos = False
if pos:
    r = (C[-1] - entry) / entry - 2 * FEE
    eq *= (1 + r)
    bt_trades.append((rows[-1][0], entry, C[-1], r))

tg = TrendStrategy(TradingCfg(strategy="trend", sma_lookback=LOOKBACK))
live_trades = []
eq2, pos2, entry2 = 1.0, False, 0.0
for i in range(LOOKBACK + 1, N):
    px = C[i]
    tg.bars = [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
               for r in rows[:i + 1]]
    if not pos2:
        if tg.want_entry(px):
            pos2, entry2 = True, px
    else:
        if tg.want_exit(px):
            r = (px - entry2) / entry2 - 2 * FEE
            eq2 *= (1 + r)
            live_trades.append((rows[i][0], entry2, px, r))
            pos2 = False
if pos2:
    r = (C[-1] - entry2) / entry2 - 2 * FEE
    eq2 *= (1 + r)
    live_trades.append((rows[-1][0], entry2, C[-1], r))

print("回测： %d 笔，总收益 %+.1f%%" % (len(bt_trades), (eq - 1) * 100))
print("线上： %d 笔，总收益 %+.1f%%" % (len(live_trades), (eq2 - 1) * 100))

same = len(bt_trades) == len(live_trades)
if same:
    for a, b in zip(bt_trades, live_trades):
        if a[0] != b[0] or abs(a[1] - b[1]) > 1e-6 or abs(a[3] - b[3]) > 1e-9:
            same = False
            break
print("逐笔一致：", "是" if same else "否")
if not same:
    sys.exit(1)
