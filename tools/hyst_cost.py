# -*- coding: utf-8 -*-
"""只在【成本量级】上找 h，不往上搜。

一次往返手续费 0.2%。所以"收盘只比均线高一点点"的交叉，
赚的钱大概率不够付手续费——这是算术，不是拟合。
对应的带宽应当是 h ≈ 0.1%（上下各一半，合计 0.2%）。

如果 h 只有在 3%~5%（成本 15~25 倍）才有效，那多半是过拟合；
如果 h≈0.1~0.2% 就有效，那是真机制，可以用。

两个数据集同时跑：本地 13200 根、服务器 14999 根起点不同，
真机制应当两边同向。
"""
import datetime
import sqlite3
import sys

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


def run(h, cost=0.0010):
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    mtm, trades, held = [], 0, 0
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
                held += 1
                if L[i] <= stop:
                    cash += qty * stop
                    cash -= qty * stop * cost
                    qty, pos = 0.0, False
                elif C[i] < s * dn:
                    cash += qty * C[i]
                    cash -= qty * C[i] * cost
                    qty, pos = 0.0, False
        mtm.append(cash + qty * C[i])
    return mtm, trades, held


def cagr(cur):
    return (cur[-1] ** (1 / yrs) - 1) * 100


def mdd(cur):
    pk, w = cur[0], 0.0
    for v in cur:
        pk = max(pk, v)
        w = max(w, (pk - v) / pk)
    return w * 100


print("数据 %d 根，%.2f 年" % (N, yrs))
print()
print("%-9s %6s %8s %9s %9s %9s %8s"
      % ("滞回", "笔数", "在场%", "CAGR净", "CAGR毛", "回撤净", "Calmar"))
print("-" * 66)
base_net = base_gross = None
for h in (0.0, 0.001, 0.002, 0.003, 0.004, 0.005):
    m, tr, held = run(h, 0.0010)
    g, _, _ = run(h, 0.0)
    cn, cg = cagr(m), cagr(g)
    if h == 0.0:
        base_net, base_gross = cn, cg
    star = " ←成本量级" if h in (0.001, 0.002) else ""
    print("%-9s %6d %7.1f%% %+8.2f%% %+8.2f%% %8.1f%% %8.2f%s"
          % ("%.1f%%" % (h * 100), tr, 100.0 * held / N, cn, cg,
             mdd(m), cn / mdd(m), star))

print()
print("=== 与现行规则（h=0）逐项对比 ===")
for h in (0.001, 0.002):
    m, tr, held = run(h, 0.0010)
    g, _, _ = run(h, 0.0)
    print("  h=%.1f%%: 净 %+.2fpp   毛 %+.2fpp   笔数 %d->%d   回撤 %.1f%%->%.1f%%"
          % (h * 100, cagr(m) - base_net, cagr(g) - base_gross,
             run(0.0)[1], tr, mdd(run(0.0)[0]), mdd(m)))
print()
print("  判据：毛收益基本持平（差别在噪声内）+ 笔数下降 + 净收益上升")
print("        才算'只省成本、不改逻辑'。毛收益明显下降就是换了策略。")
