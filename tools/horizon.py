# -*- coding: utf-8 -*-
"""看板说"还需涨 5.5%"，这个数字诚实吗？

看板上的 need_pct = (SMA200 - price) / price，是把 SMA 当成静止的。
但 SMA200 是滚动的：每过一根 4h，最老的一根就被挤出去。
如果被挤出去的那根比现在的价格高，SMA 自己就会往下走；
如果比现在低，SMA 反而会往上走。

对只做多的策略，这个差别是决定性的：
  - SMA 在跌 -> 可能不用等价格上涨，等均线下来就自然上穿
  - SMA 在涨 -> 哪怕价格不动，也永远追不上

所以本脚本按"价格冻结在当前值"这个中性假设，逐根推算
SMA200 与上穿时点。同时给出敏感性：价格再跌 5%/10% 会怎样。
"""
import datetime
import sqlite3

DB = "data/backtest.db"
NL = 200
IV = 14400000          # 4h

c = sqlite3.connect(DB)
rows = c.execute("SELECT open_time,c FROM klines WHERE interval='4h'"
                 " ORDER BY open_time").fetchall()
c.close()
rows = [r for r in rows if r[1] and r[1] > 0]
T = [r[0] for r in rows]
C = [r[1] for r in rows]
N = len(rows)
print("历史 4h K 线 %d 根，最后 %s"
      % (N, datetime.datetime.utcfromtimestamp(T[-1] / 1000).strftime("%Y-%m-%d %H:%M")))

px = C[-1]
sma_now = sum(C[-NL:]) / NL
print("末根收盘 %.2f" % px)
print("当前 SMA%d %.2f   现价%s SMA，差 %+.2f%%"
      % (NL, sma_now, "高于" if px > sma_now else "低于",
         (px - sma_now) / sma_now * 100))
print()

# 价格冻结，逐根滚动推算 SMA
window = list(C[-NL:])
cur = px
path = []
for k in range(1, 601):          # 最多推 600 根 4h = 100 天
    window.pop(0)
    window.append(cur)
    s = sum(window) / NL
    path.append(s)

print("=== 假设价格冻结在 %.0f，SMA%d 会怎么走 ===" % (px, NL))
for k in (1, 6, 30, 90, 180, 360, 600):
    if k <= len(path):
        s = path[k - 1]
        gap = (px - s) / s * 100
        print("  +%3d 根 (%5.1f 天): SMA %8.2f   差 %+6.2f%%"
              % (k, k * 4 / 24.0, s, gap))

cross = None
for k, s in enumerate(path, 1):
    if px > s:
        # 上穿还需要"上一根也在下方"，这里只看首次超过
        cross = k
        break
print()
if cross:
    print(">>> 价格不动的话，%d 根 4h 后（约 %.1f 天）现价就会高于 SMA"
          % (cross, cross * 4 / 24.0))
    print("    也就是说看板上的「还需涨 5.5%」高估了门槛——均线自己在下来。")
else:
    print(">>> 价格不动的话，600 根（100 天）内 SMA 都不会被追上")

print()
print("=== 敏感性：如果价格继续跌 ===")
for drop in (0, 5, 10, 15, 20):
    p2 = px * (1 - drop / 100.0)
    window = list(C[-NL:])
    got = None
    for k in range(1, 601):
        window.pop(0)
        window.append(p2)
        s = sum(window) / NL
        if p2 > s:
            got = k
            break
    if got:
        print("  再跌 %2d%% -> 价 %8.0f，%3d 根后上穿（%.0f 天）"
              % (drop, p2, got, got * 4 / 24.0))
    else:
        print("  再跌 %2d%% -> 价 %8.0f，600 根内不上穿" % (drop, p2))

print()
print("=== 参考：跌破 SMA 之后，要空等多久才会再上穿？ ===")
# 语义必须说清楚：这里量的是【出场之后到下一次进场】的空窗，
# 不是"从进场那一刻开始等"。上一版写成了从进场点开始找下一次跌破，
# 于是每次都得 0（进场那一刻就在均线上方），样本 181 次全是 0 根——
# 一个所有样本都等于 0 的统计量不是结论，是量错了。
waits = []
i = NL
while i < N:
    above = C[i] > sum(C[i - NL:i]) / NL
    prev_above = C[i - 1] > sum(C[i - 1 - NL:i - 1]) / NL
    if prev_above and not above:
        # i 这根收官在均线下方：从下一根开始数，直到重新站上
        j = i + 1
        while j < N and C[j] <= sum(C[j - NL:j]) / NL:
            j += 1
        if j < N:
            waits.append(j - i)
        i = j + 1
    else:
        i += 1
if waits:
    waits.sort()
    print("  样本 %d 次" % len(waits))
    print("  中位 %d 根（%.1f 天）  最长 %d 根（%.0f 天）  最短 %d 根"
          % (waits[len(waits) // 2], waits[len(waits) // 2] * 4 / 24.0,
             waits[-1], waits[-1] * 4 / 24.0, waits[0]))
else:
    print("  没有样本")
