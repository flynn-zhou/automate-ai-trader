# -*- coding: utf-8 -*-
"""重新生成等待表：目标改成【真触发线】SMA*(1+h)，不是裸均线。

上一版我只把偏离度减了 h 再去查旧表，那只能修正"跨桶"的情形。
实测证明还不够：从 (-6,-4] 桶出发，涨回裸均线中位 9.0 天，
涨到 SMA*1.002 要 9.8 天——差的这 0.8 天，减 0.2pp 偏离是换不出来的。

生成口径与已发布表完全一致（先复现验证过）：
  - 只取每段连续区间的【第一根】作为独立事件
  - 桶按距【裸均线】的偏离度划分（看板给的就是这个数）
  - 目标改成收盘 > SMA*(1+h)
"""
import datetime
import sqlite3

NL = 200
H = 0.002
BUCKETS = [(-2.0, 0.0), (-4.0, -2.0), (-6.0, -4.0),
           (-10.0, -6.0), (-20.0, -10.0), (-1e9, -20.0)]
NAMES = ["(-2,0]", "(-4,-2]", "(-6,-4]", "(-10,-6]", "(-20,-10]", "(-inf,-20]"]
PUBLISHED = [1.3, 6.0, 9.0, 10.5, 14.8, 20.8]

c = sqlite3.connect("data/backtest.db")
rows = c.execute(
    "SELECT open_time, c FROM klines WHERE interval='4h' ORDER BY open_time"
).fetchall()
c.close()
rows = [r for r in rows if r[1] and r[1] > 0]
T = [r[0] for r in rows]
C = [float(r[1]) for r in rows]
n = len(C)
sma = [None] * n
for i in range(NL, n):
    sma[i] = sum(C[i - NL:i]) / NL


def build(up):
    table = [None] * len(BUCKETS)
    for k, (lo, hi) in enumerate(BUCKETS):
        eps, prev_in = [], False
        for i in range(NL, n):
            if not sma[i]:
                continue
            d = (C[i] - sma[i]) / sma[i] * 100.0
            cur = (lo < d <= hi) if hi != 0.0 else (lo < d <= 0.0)
            if cur and not prev_in:
                j = i + 1
                while j < n:
                    if sma[j] and C[j] > sma[j] * up:
                        break
                    j += 1
                if j < n:
                    eps.append((T[j] - T[i]) / 86400000.0)
            prev_in = bool(cur)
        if not eps:
            continue
        eps.sort()

        def q(p):
            idx = max(0, min(len(eps) - 1, int(round(p * (len(eps) - 1)))))
            return round(eps[idx], 1)
        table[k] = (q(0.5), q(0.9), len(eps), round(eps[-1], 1))
    return table


raw = build(1.0)
hys = build(1.0 + H)

print("窗口 %d 根  %s ~ %s" % (
    n,
    datetime.datetime.utcfromtimestamp(T[0] / 1000).date(),
    datetime.datetime.utcfromtimestamp(T[-1] / 1000).date()))
print()
print("复现校验（裸均线 vs 已发布）：")
ok = True
for k, pub in enumerate(PUBLISHED):
    got = raw[k][0]
    good = abs(got - pub) < 0.6
    ok = ok and good
    print("  %-11s 发布 %5.1f  复现 %5.1f  %s"
          % (NAMES[k], pub, got, "OK" if good else "DIFF"))
assert ok, "口径对不上，不能继续"
print()
print("生成新表（目标 = SMA*%.1f%%）：" % (H * 100))
lines = []
for k, (lo, hi) in enumerate(BUCKETS):
    m, p90, cnt, mx = hys[k]
    lo_s = "-1e9" if lo <= -1e8 else ("%.1f" % lo)
    lines.append("    (%s, %.1f, %.1f, %.1f, %d, %.1f),"
                 % (lo_s, hi, m, p90, cnt, mx))
    print("  %-11s 中位 %5.1f  P90 %5.1f  样本 %4d  最长 %5.1f   (原 %5.1f, %+0.1f)"
          % (NAMES[k], m, p90, cnt, mx, raw[k][0], m - raw[k][0]))
print()
print("--- 代码块 ---")
for ln in lines:
    print(ln)
print("---")
