# -*- coding: utf-8 -*-
"""止损到底帮了还是害了？

我的实盘系统用 trend_stop_pct=25.0，也就是开仓后跌 25% 就止损离场。
但 long_only.py 的回测是【纯 SMA 进出，没有止损】——它给出 +1184%。

两个数字差 2.5 倍。必须搞清楚止损的影响，否则我在用无止损的回测
给一个有止损的系统背书。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools.backtest as bt

DB = os.environ.get("BT_DB", "data/spot.db")
rows = [r for r in bt.load(DB, "4h") if r[5] > 0]
C = [r[4] for r in rows]
N = len(rows)
print("4h %d 根  %.1f 年   %s" % (N, N * 4 / 24 / 365, DB))
print()


def sma(i, n):
    if i < n:
        return None
    return sum(C[i - n:i]) / n


def sim(n, cost, stop_pct=None):
    """SMA 上穿做多、下穿平仓；stop_pct 给定则加固定止损。"""
    eq = 1.0
    curve = []
    pos = False
    entry = 0.0
    bars = 0
    trades = 0
    stops = 0
    rets = []
    for i in range(n + 1, N):
        s = sma(i, n)
        if s is None:
            continue
        price = C[i]
        # 止损优先：先看这根有没有击穿止损价
        if pos and stop_pct is not None:
            stop_px = entry * (1 - stop_pct / 100.0)
            if price <= stop_px:
                r = (price - entry) / entry - 2 * cost
                rets.append(r); eq *= (1 + r)
                pos = False; stops += 1
                curve.append(eq)
                continue
        if not pos and price > s and C[i - 1] <= sma(i - 1, n):
            pos = True; entry = price; trades += 1
        elif pos and price < s:
            r = (price - entry) / entry - 2 * cost
            rets.append(r); eq *= (1 + r); pos = False
        if pos:
            bars += 1
        curve.append(eq)
    if pos:
        r = (C[-1] - entry) / entry - 2 * cost
        rets.append(r); eq *= (1 + r)
    return {"ret": (eq - 1) * 100, "trades": trades, "stops": stops,
            "in_mkt": bars / max(1, N - n - 1), "rets": rets, "curve": curve}


def maxdd(curve):
    peak = -1e9; dd = 0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd * 100


TAKER = 0.001   # 现货 taker 0.10%/边

print("SMA200 + 不同止损（现货 taker 0.10%/边）")
print("止损      总收益    最大回撤   卡玛    交易   触发止损   在场%")
for sp in (None, 10, 15, 20, 25, 30, 40):
    r = sim(200, TAKER, sp)
    dd = maxdd(r["curve"])
    print("%-8s %+8.0f%%  %6.0f%%  %5.2f  %5d  %7d  %4.0f%%" % (
        "-" if sp is None else "%d%%" % sp, r["ret"], dd,
        r["ret"] / dd if dd else 0, r["trades"], r["stops"], r["in_mkt"] * 100))
