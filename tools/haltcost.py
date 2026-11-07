# -*- coding: utf-8 -*-
"""60% 硬停会不会在无人值守时把系统打死？

背景：guard.max_drawdown_pct = 60，触发后 halted_hard = True，
只有 reset() 能解除，而 reset() 只能人工调用。
用户明说"我不再提供策略"，所以一旦硬停 = 永远停机 = 目标失败。

而回测（tools/liveedge.py）测出的最大回撤是 47.8%——
只剩 12 个百分点余量。这到底是"足够安全"还是"随时会踩线"？

本脚本对【每一个可能的入场时点】重放，回答三件事：
  1. 有多少比例的起点会撞到 60%
  2. 撞到之后，永久停机 vs 允许继续，最终收益差多少
  3. 回撤分布长什么样（不只是全期一个数字）

口径与 liveedge.py 完全一致：SMA200 不含当根、上穿进场、跌破出场、
25% 熔断、现货 0.1% 双边、逐根 mark-to-market。
"""
import datetime
import sqlite3
import sys

DB = "data/backtest.db"
NL, SP, COST = 200, 25.0, 0.0010
HALT = 60.0

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
yrs = (T[-1] - T[0]) / (365.25 * 86400000)
print("数据 %d 根 4h  %.1f 年  %s ~ %s" % (
    N, yrs,
    datetime.datetime.utcfromtimestamp(T[0] / 1000).strftime("%Y-%m-%d"),
    datetime.datetime.utcfromtimestamp(T[-1] / 1000).strftime("%Y-%m-%d")))
print()


def run(start):
    """从 start 开始按真实规则跑，返回 (mtm 曲线, 首次撞 HALT 的索引)。"""
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    curve = []
    peak = 1.0
    hit = None
    for i in range(start, N):
        if i >= NL + 1:
            s = sum(C[i - NL:i]) / NL
            sp = sum(C[i - 1 - NL:i - 1]) / NL
            if not pos:
                if C[i] > s and C[i - 1] <= sp:
                    pos = True
                    entry = C[i]
                    stop = entry * (1 - SP / 100.0)
                    f = cash * COST
                    cash -= f
                    qty = (cash * (1 - COST)) / entry
                    cash -= qty * entry
            else:
                if L[i] <= stop:
                    px = stop
                    cash += qty * px
                    cash -= qty * px * COST
                    qty = 0.0
                    pos = False
                elif C[i] < s:
                    px = C[i]
                    cash += qty * px
                    cash -= qty * px * COST
                    qty = 0.0
                    pos = False
        v = cash + qty * C[i]
        curve.append(v)
        peak = max(peak, v)
        dd = (peak - v) / peak * 100.0
        if hit is None and dd >= HALT:
            hit = len(curve) - 1
    return curve, hit


# 起点：每 6 根（约一天）取一个，且留够 200 根预热
starts = list(range(NL + 1, N - 50, 6))
print("起点数 %d（每 6 根 4h 取一个，保证至少 200 根预热）" % len(starts))
print()

hits = []
final_halt = []
final_free = []
dds = []
for st in starts:
    curve, hit = run(st)
    if not curve:
        continue
    pk = curve[0]
    w = 0.0
    for v in curve:
        pk = max(pk, v)
        w = max(w, (pk - v) / pk)
    dds.append(w * 100)
    free = curve[-1]
    final_free.append(free)
    if hit is not None:
        # 硬停后永久停机：收益冻结在触发那一刻
        halted = curve[hit]
        final_halt.append(halted)
        hits.append((st, hit, w * 100, halted, free))
    else:
        final_halt.append(free)

print("=== 1. 60% 硬停触发比例 ===")
print("起点 %d 个，触发 %d 个（%.1f%%）"
      % (len(starts), len(hits), 100.0 * len(hits) / max(1, len(starts))))
print()

dds.sort()
if dds:
    def pct(p):
        k = int(len(dds) * p)
        return dds[min(k, len(dds) - 1)]
    print("=== 2. 各起点的最大回撤分布 ===")
    print("  最小 %.1f%%  25分位 %.1f%%  中位 %.1f%%  75分位 %.1f%%  最大 %.1f%%"
          % (dds[0], pct(0.25), pct(0.50), pct(0.75), dds[-1]))
    over = [d for d in dds if d >= HALT]
    print("  >= %.0f%% 的起点：%d 个（%.1f%%）"
          % (HALT, len(over), 100.0 * len(over) / len(dds)))
    over50 = [d for d in dds if d >= 50]
    print("  >= 50%% 的起点：%d 个（%.1f%%）  <- 距硬停只剩 10pp"
          % (len(over50), 100.0 * len(over50) / len(dds)))
print()

print("=== 3. 永久停机 vs 允许继续（同样起点）===")
if hits:
    tot_h = sum(final_halt)
    tot_f = sum(final_free)
    print("  平均最终权益：硬停 %.4f   不停 %.4f   差 %.4f（%.1f%%）"
          % (tot_h / len(starts), tot_f / len(starts),
             (tot_f - tot_h) / len(starts),
             100.0 * (tot_f - tot_h) / max(1e-9, tot_h)))
    print()
    print("  触发硬停的起点明细（最多 10 个）：")
    print("  %-12s %-8s %-8s %-12s %-12s" % ("起点日期", "回撤", "触发后", "硬停终值", "不停终值"))
    for st, hit, dd, hv, fv in hits[:10]:
        print("  %-12s %7.1f%% %7.1f%% %11.4f %11.4f"
              % (datetime.datetime.utcfromtimestamp(T[st] / 1000).strftime("%Y-%m-%d"),
                 dd, 100.0 * (1 - hv), hv, fv))
    print()
    better = sum(1 for _s, _h, _d, hv, fv in hits if fv > hv)
    print("  触发后「如果允许继续」能挽回的起点：%d / %d" % (better, len(hits)))
    winners = sorted(hits, key=lambda x: x[4] - x[3], reverse=True)[:3]
    print("  挽回最多的三个：")
    for st, hit, dd, hv, fv in winners:
        print("     %s  硬停 %.4f -> 继续 %.4f（%+.1f%%）"
              % (datetime.datetime.utcfromtimestamp(T[st] / 1000).strftime("%Y-%m-%d"),
                 hv, fv, (fv / hv - 1) * 100))
else:
    print("  没有任何起点触发 60% 硬停 —— 当前余量是够的")
