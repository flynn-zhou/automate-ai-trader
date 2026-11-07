# -*- coding: utf-8 -*-
"""滞回带能不能省下那 6.22%/年的手续费？

现状：收盘上穿 SMA200 进场、收盘跌破出场。均线附近的震荡会来回抽打，
每抽一次付 0.2% 往返。6.84 年 213 笔 = 31 笔/年 = 6.22%/年手续费。

滞回（hysteresis）是最经典的去抖手段：
  进场门槛抬到 SMA*(1+h)，出场门槛压到 SMA*(1-h)。
带子里的价格既不算上穿也不算跌破，于是同一段震荡只做一次而不是三次。

这不是已否决的那几种（斜率过滤 / 连续 N 根确认 / 非对称均线）：
它不改均线、不改信号判定，只改"触发线相对均线的位置"。

风险：这是在搜参数，很容易过拟合。所以判据不能只看总收益——
必须逐年看，而且要问"改善来自少数几年还是多数年份"。
上一轮刚学到：总量改善可以掩盖某一时代的矛盾。
"""
import datetime
import sqlite3

NL, SP, COST = 200, 25.0, 0.0010

c = sqlite3.connect("data/backtest.db")
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


def run(h, stop_pct=SP):
    """h = 滞回半宽（0 表示现行规则）。返回逐根 mark-to-market 净值。"""
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    mtm, trades, fees = [], 0, 0.0
    up, dn = 1.0 + h, 1.0 - h
    for i in range(N):
        if i >= NL + 1:
            s = sum(C[i - NL:i]) / NL
            sp = sum(C[i - 1 - NL:i - 1]) / NL
            if not pos:
                if C[i] > s * up and C[i - 1] <= sp * up:
                    pos = True
                    entry = C[i]
                    stop = entry * (1 - stop_pct / 100.0)
                    f = cash * COST
                    cash -= f
                    fees += f
                    qty = (cash * (1 - COST)) / entry
                    cash -= qty * entry
                    trades += 1
            else:
                if L[i] <= stop:
                    px = stop
                    cash += qty * px
                    f = qty * px * COST
                    cash -= f
                    fees += f
                    qty, pos = 0.0, False
                elif C[i] < s * dn:
                    px = C[i]
                    cash += qty * px
                    f = qty * px * COST
                    cash -= f
                    fees += f
                    qty, pos = 0.0, False
        mtm.append(cash + qty * C[i])
    return mtm, trades, fees


def mdd(cur):
    pk, w = cur[0], 0.0
    for v in cur:
        pk = max(pk, v)
        w = max(w, (pk - v) / pk)
    return w * 100


def cagr(cur):
    return (cur[-1] ** (1 / yrs) - 1) * 100


print("数据 %d 根 4h，%.2f 年" % (N, yrs))
print()
print("%-8s %7s %10s %9s %9s %8s %9s"
      % ("滞回半宽", "笔数", "总收益", "CAGR", "最大回撤", "Calmar", "手续费%权益"))
print("-" * 74)
res = {}
for h in (0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05):
    mtm, tr, fees = run(h)
    res[h] = (mtm, tr, fees)
    print("%-8s %7d %+9.0f%% %+8.2f%% %8.1f%% %8.2f %8.1f%%"
          % ("%.1f%%" % (h * 100), tr, (mtm[-1] - 1) * 100, cagr(mtm),
             mdd(mtm), cagr(mtm) / mdd(mtm), fees * 100))

print()
print("=== 逐年看：改善是普遍的，还是只来自少数几年？ ===")
bounds = {}
for i, y in enumerate(yr):
    bounds.setdefault(y, [i, i])
    bounds[y][1] = i
years = sorted(bounds)
hdr = "%-6s" % "年份"
for h in (0.0, 0.01, 0.02, 0.03):
    hdr += "%10s" % ("h=%.0f%%" % (h * 100))
print(hdr)


def yearly(mtm):
    out, prev = {}, 1.0
    for y in years:
        i0, i1 = bounds[y]
        out[y] = (mtm[i1] / prev - 1) * 100
        prev = mtm[i1]
    return out


yt = {h: yearly(res[h][0]) for h in (0.0, 0.01, 0.02, 0.03)}
for y in years:
    line = "%-6d" % y
    for h in (0.0, 0.01, 0.02, 0.03):
        line += "%+9.0f%%" % yt[h][y]
    print(line)

print()
print("=== 判据：h>0 在多少个年份里优于 h=0 ===")
for h in (0.01, 0.02, 0.03):
    win = sum(1 for y in years if yt[h][y] > yt[0.0][y])
    lose = sum(1 for y in years if yt[h][y] < yt[0.0][y])
    print("  h=%.0f%%: 优于现行 %d 年，劣于 %d 年（共 %d 年）"
          % (h * 100, win, lose, len(years)))
