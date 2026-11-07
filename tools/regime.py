# -*- coding: utf-8 -*-
"""这套策略最难熬的时候能难熬多久？

CAGR 41.7%、Calmar 0.87 是六年半的平均值。可人不是按平均值活的：
真金白银投进去之后，只要出现一段连亏，绝大多数人会在最坏的时刻把它关掉。
所以必须提前知道两件事——

  1. 多久不赚钱是正常的？（水下时长）
  2. 连续两年不赚钱，历史上出现过几次？

这轮把最近两年的逐年收益摆出来看，很扎眼：
  2025 -10.2%   2026 +7.9%
两年合计约 -3%。也就是说"现在这一刻"看起来是没边际的。
这不必然是坏事（买入持有同期 -17%），但必须量化"平淡期能有多长"。

口径与 liveedge 完全一致（同样的进场/出场/止损/费用），
只多算滚动窗口和水下时长，避免两套引擎各说各话。
"""
import datetime
import sqlite3

DB, NL, SP, COST = "data/backtest.db", 200, 25.0, 0.0010
IV_H = 4.0
YEAR_BARS = int(365.25 * 24 / IV_H)      # 2191

c = sqlite3.connect(DB)
rows = c.execute("SELECT open_time,h,l,c FROM klines WHERE interval='4h'"
                 " ORDER BY open_time").fetchall()
c.close()
rows = [r for r in rows if r[3] and r[3] > 0]
T = [r[0] for r in rows]
H = [r[1] for r in rows]
L = [r[2] for r in rows]
C = [r[3] for r in rows]
N = len(rows)
yr = [datetime.datetime.utcfromtimestamp(t / 1000).year for t in T]
yrs = (T[-1] - T[0]) / (365.25 * 86400000)

cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
mtm, trades, stopped = [], 0, 0
for i in range(N):
    if i >= NL + 1:
        s = sum(C[i - NL:i]) / NL
        sp = sum(C[i - 1 - NL:i - 1]) / NL
        if not pos:
            if C[i] > s and C[i - 1] <= sp:
                pos = True; entry = C[i]; stop = entry * (1 - SP / 100.0)
                f = cash * COST; cash -= f
                qty = (cash * (1 - COST)) / entry
                cash -= qty * entry; trades += 1
        else:
            if L[i] <= stop:
                cash += qty * stop; cash -= qty * stop * COST
                qty = 0.0; pos = False; stopped += 1
            elif C[i] < s:
                cash += qty * C[i]; cash -= qty * C[i] * COST
                qty = 0.0; pos = False
    mtm.append(cash + qty * C[i])
bhc = [C[i] / C[0] for i in range(N)]

print("数据 %d 根 4h  %.2f 年  %s ~ %s" % (
    N, yrs, datetime.datetime.utcfromtimestamp(T[0]/1000).date(),
    datetime.datetime.utcfromtimestamp(T[-1]/1000).date()))
print("策略 %.0f%%  CAGR  买入持有 %.0f%%" % (
    (mtm[-1]**(1/yrs)-1)*100, (bhc[-1]**(1/yrs)-1)*100))
print()

W = NL + 2


def roll(cur, bars):
    """滚动 bars 根的持有期收益，返回列表。"""
    return [(cur[i] / cur[i - bars] - 1) * 100 for i in range(bars, len(cur))]


print("=" * 70)
print("一、滚动持有期：任意时点进场，拿这么久，赚还是亏")
print("=" * 70)
print("%-10s %6s %9s %9s %9s %9s" % ("窗口", "样本", "亏钱占比", "中位", "最差", "最好"))
for label, bars in (("6 个月", YEAR_BARS // 2), ("1 年", YEAR_BARS),
                    ("2 年", YEAR_BARS * 2), ("3 年", YEAR_BARS * 3)):
    r = roll(mtm, bars)
    if not r:
        continue
    neg = sum(1 for v in r if v < 0) / float(len(r)) * 100
    r.sort()
    print("%-10s %6d %8.1f%% %+8.1f%% %+8.1f%% %+8.1f%%"
          % (label, len(r), neg, r[len(r)//2], r[0], r[-1]))

print()
print("=" * 70)
print("二、水下时长：从净值新高到再次新高，最长等了多久")
print("=" * 70)
peak, peak_i, worst, worst_span = mtm[0], 0, 0.0, 0
cur_peak, cur_pi = mtm[0], 0
underwater = []
start = None
for i, v in enumerate(mtm):
    # ★ v >= cur_peak 是错的：空仓时现金不变，每根 K 线都被当成"创新高"，
    # 于是量出 465 段"中位 0 天"的水下段——那不是行情，是标量不变被当成了新高。
    if v > cur_peak:
        if start is not None:
            underwater.append((i - start) * IV_H / 24.0)
            start = None
        cur_peak, cur_pi = v, i
    elif v < cur_peak and start is None:
        start = i
    dd = (cur_peak - v) / cur_peak * 100 if cur_peak else 0.0
    if dd > worst:
        worst = dd
underwater.sort()
tail = (N - 1 - cur_pi) * IV_H / 24.0
print("  已结束的水下段：%d 段" % len(underwater))
if underwater:
    print("    中位 %.0f 天｜P90 %.0f 天｜最长 %.0f 天（%.1f 年）"
          % (underwater[len(underwater)//2], underwater[int(len(underwater)*.9)],
             underwater[-1], underwater[-1] / 365.0))
print("  当前仍在水中：%.0f 天（%.1f 年），距峰值回撤 %.1f%%"
      % (tail, tail / 365.0, (cur_peak - mtm[-1]) / cur_peak * 100))
print("  峰值出现在 %s（净值 %.2f，现值 %.2f）"
      % (datetime.datetime.utcfromtimestamp(T[cur_pi] / 1000).date(),
         cur_peak, mtm[-1]))
print("  历史最深回撤 %.1f%%" % worst)

print()
print("=" * 70)
print("三、连续亏损年：历史上出现过几次")
print("=" * 70)
bounds = {}
for i, y in enumerate(yr):
    bounds.setdefault(y, [i, i]); bounds[y][1] = i
years = sorted(bounds)
ret, prev = [], 1.0
for y in years:
    i0, i1 = bounds[y]
    ret.append((y, mtm[i1] / prev - 1)); prev = mtm[i1]
streak, best_streak, cur_neg = [], 0, 0
for y, r in ret:
    if r < 0:
        cur_neg += 1
        if cur_neg > best_streak:
            best_streak = cur_neg
    else:
        cur_neg = 0
print("  年度收益：%s" % "  ".join("%d %+.0f%%" % (y, r * 100) for y, r in ret))
print("  最长连续亏损年：%d 年" % best_streak)
neg_years = sum(1 for _, r in ret if r < 0)
print("  亏损年 %d / %d（%.0f%%）" % (neg_years, len(ret), 100.0 * neg_years / len(ret)))

print()
print("=" * 70)
print("四、如果只看最近两年（每个时点的「当下两年」）")
print("=" * 70)
r2 = roll(mtm, YEAR_BARS * 2)
if r2:
    neg2 = sum(1 for v in r2 if v < 0)
    print("  滚动两年窗口 %d 个，其中亏钱 %d 个（%.0f%%）"
          % (len(r2), neg2, 100.0 * neg2 / len(r2)))
    last = (mtm[-1] / mtm[-1 - YEAR_BARS * 2] - 1) * 100
    print("  最近一个两年窗口：%+.1f%%（买入持有 %+.1f%%）"
          % (last, (bhc[-1] / bhc[-1 - YEAR_BARS * 2] - 1) * 100))
print()
print("  注：这不是在唱衰。是把「最坏能坏多久」提前说清楚——")
print("      因为唯一能保证亏钱的做法，就是在最坏的那一刻把它关掉。")
