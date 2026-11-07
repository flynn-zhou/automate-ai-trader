# -*- coding: utf-8 -*-
"""滞回带的稳健性：滚动子区间 + 零成本分解。

为什么需要（第 94 轮留下的问题）：
  全期表上 h=1% 比线上的 0.2% 更好（+1423% vs +1175%，回撤 40.9% vs 46.1%）。
  但那是同一份样本上挑参数 —— 样本内优化。要判断它是不是真的，得多问两句：

  问题 1：换个时间段还成立吗？（滚动子区间，成对比较）
  问题 2：为什么更好？是信号质量，还是单纯少交手续费？

  问题 2 有个干净的切法：把成本设成 0 再跑一遍。
    零成本下 h=1% 仍然更好 -> 是信号质量（进场更挑）
    零成本下 h=1% 不再更好 -> 纯粹是手续费（笔数少）

  这两个问题分开回答，才知道该不该动线上那个 0.2%。
"""
import sqlite3
import sys

DB = '/home/cryptobot/ai-trader/data/backtest.db'
NL, SP = 200, 25.0
HS = (0.0, 0.002, 0.005, 0.010)


def load():
    c = sqlite3.connect(DB)
    rows = c.execute("SELECT open_time,h,l,c FROM klines WHERE interval='4h'"
                     " ORDER BY open_time").fetchall()
    c.close()
    rows = [r for r in rows if r[3] and r[3] > 0]
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


def sma_all(C):
    """sma[i] = mean(C[i-NL:i])，不含第 i 根。预热段给 None。"""
    out = [None] * len(C)
    acc = 0.0
    for i in range(len(C)):
        if i >= NL:
            out[i] = acc / NL
        acc += C[i]
        if i >= NL:
            acc -= C[i - NL]
    return out


def sim(C, L, S, h, i0, i1, cost):
    """在 [i0, i1) 上跑，返回期末权益倍数。S 是预算好的 SMA。"""
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    trades = 0
    for i in range(max(i0, NL + 1), i1):
        s = S[i]
        sp = S[i - 1]
        if s is None or sp is None:
            continue
        if not pos:
            if C[i] > s * (1 + h) and C[i - 1] <= sp * (1 + h):
                pos = True; entry = C[i]; stop = entry * (1 - SP / 100.0)
                cash -= cash * cost
                qty = (cash * (1 - cost)) / entry
                cash -= qty * entry; trades += 1
        else:
            if L[i] <= stop:
                px = stop
            elif C[i] <= s * (1 - h):
                px = C[i]
            else:
                continue
            cash += qty * px; cash -= qty * px * cost
            qty = 0.0; pos = False
    return cash + qty * C[i1 - 1], trades


def mdd_of(C, L, S, h, i0, i1, cost):
    """逐根重估算窗口内最大回撤。"""
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    curve = []
    for i in range(max(i0, NL + 1), i1):
        s = S[i]; sp = S[i - 1]
        if s is not None and sp is not None:
            if not pos:
                if C[i] > s * (1 + h) and C[i - 1] <= sp * (1 + h):
                    pos = True; entry = C[i]; stop = entry * (1 - SP / 100.0)
                    cash -= cash * cost
                    qty = (cash * (1 - cost)) / entry
                    cash -= qty * entry
            else:
                if L[i] <= stop:
                    px = stop
                elif C[i] <= s * (1 - h):
                    px = C[i]
                else:
                    px = None
                if px is not None:
                    cash += qty * px; cash -= qty * px * cost
                    qty = 0.0; pos = False
        curve.append(cash + qty * C[i])
    if not curve:                 # 窗口太短（2019 年只有 3 根），跳过
        return float('nan'), float('nan')
    pk, w = curve[0], 0.0
    for v in curve:
        pk = max(pk, v); w = max(w, (pk - v) / pk)
    return w * 100, (curve[-1] - 1) * 100


T, H, L, C = load()
S = sma_all(C)
N = len(C)
print('权威库 %d 根 4h' % N)

# ---- 问题 1：逐年 ----
print()
print('=== 逐年收益（成本 0.10%） ===')
print('%-8s' % '年' + ''.join('%12s' % ('h=%g%%' % (h * 100)) for h in HS))
import datetime
yr = [datetime.datetime.utcfromtimestamp(t / 1000).year for t in T]
years = sorted(set(yr))
for y in years:
    idx = [i for i in range(N) if yr[i] == y]
    i0, i1 = idx[0], idx[-1] + 1
    row = ''
    for h in HS:
        _, ret = mdd_of(C, L, S, h, i0, i1, 0.0010)
        row += '%+11.1f%%' % ret
    print('%-8d' % y + row)

# ---- 问题 2：零成本分解（机制）----
print()
print('=== 零成本 vs 有成本（全期），看差距是信号还是手续费 ===')
print('%-8s %14s %14s %12s' % ('带子', '成本0.10%', '成本0', '笔数'))
for h in HS:
    a, na = sim(C, L, S, h, 0, N, 0.0010)
    b, nb = sim(C, L, S, h, 0, N, 0.0)
    print('h=%-6g %+13.0f%% %+13.0f%% %12d' % (h * 100, (a - 1) * 100, (b - 1) * 100, na))
print()
print('读法：如果零成本下 h=1% 不再赢 h=0.2%，那它的优势就只是少交手续费。')

# ---- 问题 1b：滚动子区间（成对比较）----
print()
print('=== 滚动 2 年窗口（步长 1 个月），h=1% 对 h=0.2% 的成对比较 ===')
W = 6 * 365 * 2 // 1           # 2 年 4h 根数 = 4380
STEP = 180                     # 1 个月
wins = {'1v0.2': 0, '1v0': 0}
diffs = []
n_win = 0
i0 = 0
while i0 + W <= N:
    r02, _ = sim(C, L, S, 0.002, i0, i0 + W, 0.0010)
    r10, _ = sim(C, L, S, 0.010, i0, i0 + W, 0.0010)
    r00, _ = sim(C, L, S, 0.000, i0, i0 + W, 0.0010)
    n_win += 1
    if r10 > r02:
        wins['1v0.2'] += 1
    if r10 > r00:
        wins['1v0'] += 1
    diffs.append((r10 - r02) * 100)
    i0 += STEP
diffs.sort()
print('窗口数 %d' % n_win)
print('  h=1%% 跑赢 h=0.2%% 的窗口: %d (%.0f%%)' % (wins['1v0.2'], 100.0 * wins['1v0.2'] / n_win))
print('  h=1%% 跑赢 h=0%%   的窗口: %d (%.0f%%)' % (wins['1v0'], 100.0 * wins['1v0'] / n_win))
print('  差值(百分点) 中位 %+.1f  25%% %+.1f  75%% %+.1f  最差 %+.1f  最好 %+.1f'
      % (diffs[len(diffs)//2], diffs[len(diffs)//4],
         diffs[3*len(diffs)//4], diffs[0], diffs[-1]))
print()
print('注意：窗口互相重叠，不是独立样本，只能当方向参考，不能当显著性检验。')
print()
print('=== 关键复核：线上 0.2% 相对裸均线 0% 稳健吗？ ===')
print('（我一直在假设 0.2% 是好的，得验一下这个假设本身）')
W2 = 6 * 365 * 2
STEP2 = 180
pairs = [(0.002, 0.0), (0.002, 0.005), (0.002, 0.010)]
res = {p: 0 for p in pairs}
d2 = {p: [] for p in pairs}
nw = 0
i = 0
while i + W2 <= N:
    vals = {}
    for h in (0.0, 0.002, 0.005, 0.010):
        vals[h], _ = sim(C, L, S, h, i, i + W2, 0.0010)
    nw += 1
    for p in pairs:
        if vals[p[0]] > vals[p[1]]:
            res[p] += 1
        d2[p].append((vals[p[0]] - vals[p[1]]) * 100)
    i += STEP2
print('窗口数 %d（滚动 2 年，步长 1 月）' % nw)
for p in pairs:
    v = sorted(d2[p])
    print('  0.2%% vs %-5g%%  胜率 %.0f%%   差值 中位 %+6.1f  P25 %+6.1f  P75 %+6.1f  最差 %+6.1f'
          % (p[1] * 100, 100.0 * res[p] / nw, v[len(v)//2], v[len(v)//4],
             v[3*len(v)//4], v[0]))
