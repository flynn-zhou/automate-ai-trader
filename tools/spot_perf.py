# -*- coding: utf-8 -*-
"""在【现货】数据上重算 SMA200 的全部业绩指标。

为什么要重算：
  我此前一直引用「+1032%、最大回撤 31%、卡玛 33.15、213 笔」。
  但那些数字来自 data/backtest.db —— 【合约】K 线。
  而系统实际交易的是【现货】，费率也不同（现货 0.1%/边 vs 合约 0.05%）。

  上轮信号级 parity 已经证明：同一策略在现货数据上是 199 笔而不是 213 笔。
  所以那些业绩数字对应的不是我们真正在跑的东西。

  这轮用现货官方 K 线 + 现货真实费率重算，给出诚实的数字。

口径（与 tools/long_only.py 一致）：
  进场：C[i] > sma(i,n) 且 C[i-1] <= sma(i-1,n)
  出场：C[i] < sma(i,n)
  成本：每边 cost（买卖各一次）
  仓位：满仓（1x，现货无杠杆）
"""
import json
import os
import sys
import time
import urllib.request

BASE = "https://api.binance.com"
N = 200
IV = 14400000


def fetch_page(end_time):
    q = "symbol=BTCUSDT&interval=4h&limit=1000"
    if end_time:
        q += "&endTime=%d" % end_time
    req = urllib.request.Request(BASE + "/api/v3/klines?" + q,
                                 headers={"User-Agent": "ai-trader/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


all_rows, end, prev = [], None, None
for _ in range(14):
    rows = fetch_page(end)
    if not rows:
        break
    all_rows = rows + all_rows
    f = int(rows[0][0])
    if prev is not None and f >= prev:
        break
    prev = f
    end = f - 1

seen, uniq = set(), []
for r in all_rows:
    t = int(r[0])
    if t in seen:
        continue
    seen.add(t)
    uniq.append(r)
uniq.sort(key=lambda r: int(r[0]))
now_ms = int(time.time() * 1000)
rows = [r for r in uniq if int(r[0]) + IV <= now_ms]
C = [float(r[4]) for r in rows]
T = [int(r[0]) for r in rows]
t0 = time.strftime("%Y-%m-%d", time.gmtime(T[0] / 1000))
t1 = time.strftime("%Y-%m-%d", time.gmtime(T[-1] / 1000))
years = len(C) * IV / 86400000 / 365.0
print("现货 4h K 线 %d 根  %s ~ %s  (%.2f 年)" % (len(C), t0, t1, years))


def sma(i, n):
    if i < n:
        return None
    return sum(C[i - n:i]) / n


def sim(n, cost, eq0=100.0):
    eq = eq0
    curve = [eq]
    pos = False
    entry = 0.0
    bars_in = 0
    trades = []
    for i in range(n + 1, len(C)):
        s = sma(i, n)
        if s is None:
            continue
        price = C[i]
        if not pos and price > s and C[i - 1] <= sma(i - 1, n):
            pos = True
            entry = price
            trades.append({"entry_i": i, "entry": price})
            # 买入成本
            eq *= (1.0 - cost)
        elif pos and price < s:
            r = price / entry - 1.0
            eq *= (1.0 + r) * (1.0 - cost)
            trades[-1].update({"exit_i": i, "exit": price, "ret": r - 2 * cost})
            pos = False
        if pos:
            bars_in += 1
        # 盯市权益（含浮盈）
        if pos:
            curve.append(eq * (price / entry))
        else:
            curve.append(eq)
    if pos:
        r = C[-1] / entry - 1.0
        eq *= (1.0 + r) * (1.0 - cost)
        trades[-1].update({"exit_i": len(C) - 1, "exit": C[-1], "ret": r - 2 * cost})
    return {"ret": (eq / eq0 - 1) * 100, "eq": eq, "curve": curve,
            "trades": len(trades), "in_mkt": bars_in / max(1, len(C) - n - 1),
            "detail": trades}


def maxdd(curve):
    peak = -1e18
    dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd * 100


def cagr(ret, yrs):
    if yrs <= 0:
        return 0.0
    return ((1.0 + ret / 100.0) ** (1.0 / yrs) - 1.0) * 100.0


print()
print("=" * 74)
print("买入持有基准（现货）")
print("=" * 74)
bh = (C[-1] / C[0] - 1) * 100
bh_curve = [c / C[0] * 100.0 for c in C]
print("  总收益 %+.0f%%  CAGR %.1f%%  最大回撤 %.0f%%  卡玛 %.2f"
      % (bh, cagr(bh, years), maxdd(bh_curve), bh / maxdd(bh_curve)))

print()
print("=" * 74)
print("SMA200 只做多（现货真实费率）")
print("=" * 74)
print("  %-14s %10s %8s %8s %6s %8s" % ("费率(每边)", "总收益", "CAGR", "回撤", "卡玛", "笔数"))
out = {}
for cost, name in ((0.0, "0（理论）"), (0.0002, "0.02% maker"),
                   (0.0005, "0.05% 合约taker"), (0.0010, "0.10% 现货taker")):
    r = sim(N, cost)
    dd = maxdd(r["curve"])
    cal = r["ret"] / dd if dd else 0.0
    out[name] = {"ret": r["ret"], "cagr": cagr(r["ret"], years), "dd": dd,
                 "calmar": cal, "trades": r["trades"],
                 "in_mkt": r["in_mkt"] * 100}
    print("  %-14s %+9.0f%% %7.1f%% %7.0f%% %6.2f %8d"
          % (name, r["ret"], cagr(r["ret"], years), dd, cal, r["trades"]))

r = sim(N, 0.0010)
print()
print("  在场时间 %.0f%%" % (r["in_mkt"] * 100))
rets = [t["ret"] * 100 for t in r["detail"]]
if rets:
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    print("  胜率 %.1f%%（%d 胜 / %d 负）"
          % (100.0 * len(wins) / len(rets), len(wins), len(losses)))
    if wins:
        print("  平均盈利 %+.2f%%" % (sum(wins) / len(wins)))
    if losses:
        print("  平均亏损 %+.2f%%" % (sum(losses) / len(losses)))
    rets_sorted = sorted(rets, reverse=True)
    top3 = sum(rets_sorted[:3])
    total = sum(rets)
    print("  最好 3 笔合计 %+.1f 个百分点，占全部 %+.1f 的 %.0f%%"
          % (top3, total, 100.0 * top3 / total if total else 0))
    print("  单笔最大盈利 %+.2f%%   单笔最大亏损 %+.2f%%"
          % (rets_sorted[0], rets_sorted[-1]))

json.dump({"years": years, "bars": len(C), "from": t0, "to": t1,
           "buy_hold": {"ret": bh, "cagr": cagr(bh, years),
                        "dd": maxdd(bh_curve)}, "sma200": out},
          open("tools/spot_perf.json", "w"), ensure_ascii=False, indent=1)
print()
print("已写出 tools/spot_perf.json")
