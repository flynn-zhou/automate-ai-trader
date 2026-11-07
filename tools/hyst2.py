# -*- coding: utf-8 -*-
"""滞回带是不是真的？两问：
   1. 换一段数据（本地 13200 根，起点不同）排名还一样吗？
   2. 改善来自"赚得更多"还是"付得更少"？（把手续费设 0 看毛收益）
"""
import datetime
import sqlite3

NL, SP = 200, 25.0

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


def run(h, cost):
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    mtm, trades = [], 0
    up, dn = 1.0 + h, 1.0 - h
    for i in range(N):
        if i >= NL + 1:
            s = sum(C[i - NL:i]) / NL
            sp = sum(C[i - 1 - NL:i - 1]) / NL
            if not pos:
                if C[i] > s * up and C[i - 1] <= sp * up:
                    pos = True
                    entry = C[i]
                    stop = entry * (1 - SP / 100.0)
                    cash -= cash * cost
                    qty = (cash * (1 - cost)) / entry
                    cash -= qty * entry
                    trades += 1
            else:
                if L[i] <= stop:
                    cash += qty * stop
                    cash -= qty * stop * cost
                    qty, pos = 0.0, False
                elif C[i] < s * dn:
                    cash += qty * C[i]
                    cash -= qty * C[i] * cost
                    qty, pos = 0.0, False
        mtm.append(cash + qty * C[i])
    return mtm, trades


def cagr(cur):
    return (cur[-1] ** (1 / yrs) - 1) * 100


def mdd(cur):
    pk, w = cur[0], 0.0
    for v in cur:
        pk = max(pk, v)
        w = max(w, (pk - v) / pk)
    return w * 100


print("本地数据 %d 根 4h，%.2f 年（起点与服务器不同）" % (N, yrs))
print()
print("%-8s %7s %10s %10s %9s %9s"
      % ("滞回", "笔数", "CAGR(净)", "CAGR(毛)", "回撤(净)", "Calmar"))
print("-" * 68)
for h in (0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05):
    m_net, tr = run(h, 0.0010)
    m_gross, _ = run(h, 0.0)
    print("%-8s %7d %+9.2f%% %+9.2f%% %8.1f%% %8.2f"
          % ("%.1f%%" % (h * 100), tr, cagr(m_net), cagr(m_gross),
             mdd(m_net), cagr(m_net) / mdd(m_net)))

print()
print("=== 判据 ===")
print("  如果滞回带来的是【真边际】，毛收益应当同步变好；")
print("  如果只是省手续费，毛收益会变差、净收益才变好。")
m0n, t0 = run(0.0, 0.0010)
m0g, _ = run(0.0, 0.0)
m1n, t1 = run(0.01, 0.0010)
m1g, _ = run(0.01, 0.0)
print()
print("  h=0   : 毛 %.2f%%  净 %.2f%%  差 %.2fpp（手续费）"
      % (cagr(m0g), cagr(m0n), cagr(m0g) - cagr(m0n)))
print("  h=1%%  : 毛 %.2f%%  净 %.2f%%  差 %.2fpp（手续费）"
      % (cagr(m1g), cagr(m1n), cagr(m1g) - cagr(m1n)))
print()
print("  毛收益变化 %+.2fpp   净收益变化 %+.2fpp"
      % (cagr(m1g) - cagr(m0g), cagr(m1n) - cagr(m0n)))
if cagr(m1g) < cagr(m0g):
    print("  -> 毛收益【下降】：滞回少赚了行情，净收益的改善全部来自省手续费。")
    print("     这不是新边际，是用'少做几次'换'少付几次'——两边同源。")
else:
    print("  -> 毛收益上升，可能存在真实择时价值。")
