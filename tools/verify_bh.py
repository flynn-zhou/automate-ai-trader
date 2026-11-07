# -*- coding: utf-8 -*-
"""核对：+967% 买入持有这个数对不对？

用最朴素的方式验算——起点价、终点价、倍数。
如果买入持有对不上，那策略的 +1032% 也就不用信。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools.backtest as bt
import datetime

for db in ("data/spot.db", "data/backtest.db"):
    if not os.path.exists(db):
        continue
    rows = [r for r in bt.load(db, "4h") if r[5] > 0]
    if not rows:
        continue
    t0, c0 = rows[0][0], rows[0][4]
    t1, c1 = rows[-1][0], rows[-1][4]
    f = lambda ms: datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    mult = c1 / c0
    yrs = (t1 - t0) / (365.25 * 86400000)
    print("%-20s %d 根  %.2f 年" % (db, len(rows), yrs))
    print("  %s  开 %.2f  ->  %s  收 %.2f" % (f(t0), c0, f(t1), c1))
    print("  倍数 %.2fx   买入持有 %.0f%%   年化 %.1f%%"
          % (mult, (mult - 1) * 100, (mult ** (1 / yrs) - 1) * 100))
    print()
