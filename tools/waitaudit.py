# -*- coding: utf-8 -*-
"""大屏说「历史同档等待 9.0 天」，与我第 66 轮算的「中位 0.8 天」差了十倍。
必有一个是错的——用同一份数据、同一个 SMA 口径，把两个量都算出来。

两个量不是一回事：
  A. 整段空窗长度：从"收盘跌破均线"那根起，到"重新站上"那根止，共几根。
  B. 入桶后剩余等待：对每个偏离度的第一根，从现在到下次上穿还有几根。
     （wait_stats 那张表量的就是这个）

B 必须 <= A（B 是 A 的一个后缀），所以 B 的中位数不可能远大于 A 的中位数。
如果算出来 B >> A，说明其中至少一个实现有 bug。

SMA 口径与策略一致：sma(i) = mean(C[i-N:i])，不含第 i 根。
"""
import json
import sqlite3
import statistics

N = 200
IV = 14400000
DAY = 86400000.0

c = sqlite3.connect("data/backtest.db")
rows = c.execute("SELECT open_time,c FROM klines WHERE interval='4h'"
                 " ORDER BY open_time").fetchall()
c.close()
rows = [r for r in rows if r[1] and r[1] > 0]
T = [r[0] for r in rows]
C = [r[1] for r in rows]
M = len(C)
print("数据 %d 根，%.2f 年" % (M, M * IV / DAY / 365.0))


def sma(i):
    if i < N:
        return None
    return sum(C[i - N:i]) / N


def dev(i):
    s = sma(i)
    return None if not s else (C[i] - s) / s * 100.0


# ---------- A. 整段空窗长度 ----------
runs = []
i = N
while i < M:
    s_prev, s_cur = sma(i - 1), sma(i)
    if s_prev is None or s_cur is None:
        i += 1
        continue
    if C[i - 1] > s_prev and C[i] <= s_cur:        # 这一根收盘跌破
        j = i + 1
        while j < M and sma(j) is not None and C[j] <= sma(j):
            j += 1
        if j < M and sma(j) is not None:
            runs.append(j - i)                      # 根数
        i = j + 1
    else:
        i += 1

runs.sort()
print()
print("=== A. 整段空窗长度（跌破->重新站上）===")
print("  样本 %d 段" % len(runs))
print("  中位 %d 根（%.1f 天）  P90 %d 根（%.1f 天）  最长 %d 根（%.1f 天）"
      % (runs[len(runs) // 2], runs[len(runs) // 2] * IV / DAY,
         runs[int(len(runs) * .9)], runs[int(len(runs) * .9)] * IV / DAY,
         runs[-1], runs[-1] * IV / DAY))


# ---------- B. 入桶后剩余等待（wait_stats 的算法）----------
BUCKETS = [("0 到 -2", -2.0, 0.0), ("-2 到 -4", -4.0, -2.0),
           ("-4 到 -6", -6.0, -4.0), ("-6 到 -10", -10.0, -6.0),
           ("-10 到 -20", -20.0, -10.0), ("低于 -20", -1000.0, -20.0)]


def days_to_cross(i):
    for j in range(i + 1, M):
        sj = sma(j)
        if sj is None:
            return None
        if C[j] > sj:
            return (j - i) * IV / DAY
    return None


print()
print("=== B. 入桶后剩余等待（wait_stats.py 的口径）===")
print("  %-12s %5s %8s %8s %8s" % ("偏离区间", "样本", "中位", "P90", "最长"))
table = {}
for name, lo, hi in BUCKETS:
    eps, prev_in = [], False
    for i in range(N, M):
        d = dev(i)
        if d is None:
            continue
        cur = (lo < d <= hi) if hi != 0.0 else (lo < d <= 0.0)
        if cur and not prev_in:
            w = days_to_cross(i)
            if w is not None:
                eps.append(w)
        prev_in = bool(cur)
    if not eps:
        print("  %-12s %5s" % (name, "-"))
        continue
    eps.sort()
    def q(p):
        k = max(0, min(len(eps) - 1, int(round(p * (len(eps) - 1)))))
        return eps[k]
    table[name] = {"n": len(eps), "median": round(q(.5), 1),
                   "p90": round(q(.9), 1), "max": round(eps[-1], 1)}
    print("  %-12s %5d %6.1f天 %6.1f天 %6.1f天"
          % (name, len(eps), q(.5), q(.9), eps[-1]))

# ---------- 自洽性 ----------
print()
print("=== 自洽性检查 ===")
allb = [v for v in table.values()]
tot = sum(v["n"] for v in allb)
med_b = statistics.median([v["median"] for v in allb])
print("  B 各桶中位：%s" % ", ".join("%.1f" % v["median"] for v in allb))
print("  B 总样本 %d；A 样本 %d" % (tot, len(runs)))
print("  A 中位 %.1f 天 / B 中位(桶中位的均值) %.1f 天"
      % (runs[len(runs) // 2] * IV / DAY, med_b))
print()
print("  判据：B 是 A 的后缀，所以每个桶的中位都不该超过 A 的 P90（%.1f 天）"
      % (runs[int(len(runs) * .9)] * IV / DAY))
bad = [n for n, v in table.items()
       if v["median"] > runs[int(len(runs) * .9)] * IV / DAY]
print("  超过 A 的 P90 的桶：%s" % (bad or "无"))
