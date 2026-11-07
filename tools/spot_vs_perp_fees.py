# -*- coding: utf-8 -*-
"""用【正确的手续费率】重算：现货 0.1% vs 永续 0.05%（都是 taker）。

刚才差点用永续费率去算现货，会把现货算得过于好看。
现货手续费是永续的两倍，181 笔交易下来是实打实的 36% 累计成本。
"""
import json, os, sys

sys.path.insert(0, os.getcwd())
import tools.backtest as bt

fr = sorted([(int(r["fundingTime"]), float(r["fundingRate"]))
             for r in json.load(open("data/funding_btcusdt.json"))])
by_ts = {}
for t, r in fr:
    by_ts.setdefault(int(t // 14400000), []).append(r)

bars = [b for b in bt.load("data/backtest.db", "4h") if b[5] > 0]
C = [b[4] for b in bars]; T = [b[0] for b in bars]; N = len(bars)


def run(n=200, lev=1.0, fee=0.001, funding=False):
    eq, pos, entry = 1.0, 0, 0.0
    peak, mdd = 1.0, 0.0
    nt = 0
    for i in range(n + 1, N):
        s_now = sum(C[i-n:i]) / n
        s_prev = sum(C[i-1-n:i-1]) / n if i-1-n >= 0 else s_now
        above, prev = C[i] > s_now, C[i-1] > s_prev
        if pos and funding:
            for r in by_ts.get(int(T[i] // 14400000), []):
                eq *= (1 - r * lev)
        if pos == 0 and above and not prev:
            pos, entry = 1, C[i]; eq *= (1 - fee * lev); nt += 1
        elif pos and (not above) and prev:
            eq *= (1 + lev * (C[i] - entry) / entry - fee * lev); pos = 0
        mark = eq * (1 + lev * (C[i] - entry) / entry) if pos else eq
        peak = max(peak, mark); mdd = max(mdd, (peak - mark) / peak if peak else 0)
    if pos:
        eq *= (1 + lev * (C[-1] - entry) / entry - fee * lev)
    return eq - 1, mdd, nt


YRS = 6.85
print("%-38s %8s %7s %7s %6s" % ("方式", "累计", "年化", "回撤", "卡玛"))
rows = [
    ("现货 1x，taker 0.10%", 1.0, 0.0010, False),
    ("现货 1x，maker 0.075%", 1.0, 0.00075, False),
    ("现货 1x，BNB 抵扣 0.075%", 1.0, 0.00075, False),
    ("永续 1x，taker 0.05%，含资金费", 1.0, 0.0005, True),
    ("永续 1.5x，taker 0.05%，含资金费", 1.5, 0.0005, True),
    ("永续 2x，taker 0.05%，含资金费", 2.0, 0.0005, True),
]
best = None
for name, lev, fee, fund in rows:
    e, d, nt = run(lev=lev, fee=fee, funding=fund)
    cagr = ((1 + e) ** (1 / YRS) - 1) * 100
    calmar = cagr / (d * 100) if d else 0
    print("%-38s %+7.0f%% %+6.1f%% %6.0f%% %6.2f" % (name, e * 100, cagr, d * 100, calmar))
    if best is None or calmar > best[1]:
        best = (name, calmar)
print()
print("风险调整后最优：", best[0])
