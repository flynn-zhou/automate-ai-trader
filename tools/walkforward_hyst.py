# -*- coding: utf-8 -*-
"""走步验证：均线长度是「选出来的」还是「本来就该这么长」。

为什么要另起一个（第 99 轮）：
  已有的 tools/walkforward.py 是旧口径 —— 裸 SMA、不含滞回带、
  cost 用 0.0002（永续费率）。和 tools/liveedge.py 修之前是同一个毛病：
  量出来的不是线上那套规则的边际。所以那个工具只能当参考，不能下结论。

这轮问的问题是：
  如果每半年只用【当时之前】的数据挑均线长度和滞回带，然后往前跑半年，
  结果会比死守 200 / 0.2% 更好吗？

三种可能，含义完全不同：
  a) 走步挑出来的明显更好 -> 200 不是最优，应该做自适应
  b) 差不多              -> 200 是稳健的先验选择，参数不脆
  c) 走步更差            -> 挑参数本身在伤害收益

注意它证明不了「未来一定赚」。它只能回答「参数是不是明显过拟合的产物」。
"""
import sqlite3
from collections import Counter

DB = '/home/cryptobot/ai-trader/data/backtest.db'
SP = 25.0
COST = 0.0010                 # 现货单边 0.10%，与线上一致

SMA_CANDS = (100, 150, 200, 250, 300)
HYST_CANDS = (0.0, 0.002, 0.005)
FIXED = (200, 0.002)


def load():
    c = sqlite3.connect(DB)
    rows = c.execute("SELECT open_time,h,l,c FROM klines WHERE interval='4h'"
                     " ORDER BY open_time").fetchall()
    c.close()
    rows = [r for r in rows if r[3] and r[3] > 0]
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


def sma_all(C, n):
    out = [None] * len(C)
    acc = 0.0
    for i in range(len(C)):
        if i >= n:
            out[i] = acc / n
        acc += C[i]
        if i >= n:
            acc -= C[i - n]
    return out


def run(C, L, S, n, h, i0, i1):
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    for i in range(max(i0, n + 1), i1):
        s = S[i]; sp = S[i - 1]
        if s is None or sp is None:
            continue
        if not pos:
            if C[i] > s * (1 + h) and C[i - 1] <= sp * (1 + h):
                pos = True; entry = C[i]; stop = entry * (1 - SP / 100.0)
                cash -= cash * COST
                qty = (cash * (1 - COST)) / entry
                cash -= qty * entry
        else:
            if L[i] <= stop:
                px = stop
            elif C[i] <= s * (1 - h):
                px = C[i]
            else:
                continue
            cash += qty * px; cash -= qty * px * COST
            qty = 0.0; pos = False
    return cash + qty * C[i1 - 1]


T, H, L, C = load()
N = len(C)
SMAS = {n: sma_all(C, n) for n in SMA_CANDS}
BARS_PER_MONTH = 6 * 30
TRAIN = BARS_PER_MONTH * 24
STEP = BARS_PER_MONTH * 6

i = TRAIN
curve_wf, curve_fix = 1.0, 1.0
picks = []
while i + STEP <= N:
    best = None
    for n in SMA_CANDS:
        for h in HYST_CANDS:
            if i - TRAIN < n + 2:
                continue
            r = run(C, L, SMAS[n], n, h, i - TRAIN, i)
            if best is None or r > best[0]:
                best = (r, n, h)
    if best is None:
        i += STEP
        continue
    _r, n, h = best
    picks.append((i, n, h))
    curve_wf *= run(C, L, SMAS[n], n, h, i, i + STEP)
    curve_fix *= run(C, L, SMAS[FIXED[0]], FIXED[0], FIXED[1], i, i + STEP)
    i += STEP

print('走步：每 6 个月用【之前 2 年】挑参数，再往前跑 6 个月')
print('候选 SMA %s｜滞回 %s｜成本 %.4f/边'
      % (list(SMA_CANDS), [('%g%%' % (x * 100)) for x in HYST_CANDS], COST))
print('段数 %d' % len(picks))
print()
print('  走步自适应    期末 %.2fx' % curve_wf)
print('  死守 200/0.2%% 期末 %.2fx' % curve_fix)
print()
if picks:
    print('每段挑了谁：')
    for k, (idx, n, h) in enumerate(picks):
        if k < 26:
            print('  第%2d段  SMA%-4d 滞回 %g%%' % (k + 1, n, h * 100))
    cn = Counter(p[1] for p in picks)
    ch = Counter(p[2] for p in picks)
    print()
    print('均线长度被选中次数:', dict(sorted(cn.items())))
    print('滞回带被选中次数  :', dict(sorted(ch.items())))

# ---- 参数曲面：不依赖自适应机制，直接看「附近好参数有邻居吗」 ----
print()
print('=== 参数曲面（全期定参，成本 %.4f/边）===' % COST)
print('    每格是期末倍数。好的参数如果周围也都是好的，才不脆。')
print()
ALLS = (50, 100, 150, 200, 250, 300, 400)
ALLH = (0.0, 0.002, 0.005, 0.010)
SM2 = {n: sma_all(C, n) for n in ALLS}
hdr = 'SMA/滞回'
print('%-10s' % hdr + ''.join('%10s' % ('%g%%' % (h * 100)) for h in ALLH))
print('-' * (10 + 10 * len(ALLH)))
grid = {}
for n in ALLS:
    row = ''
    for h in ALLH:
        if N < n + 2:
            row += '%10s' % '-'
            continue
        r = run(C, L, SM2[n], n, h, 0, N)
        grid[(n, h)] = r
        row += '%9.2fx' % r
    print('%-10d' % n + row)
print()
best = max(grid.items(), key=lambda kv: kv[1])
print('最强格: SMA%d 滞回 %g%% -> %.2fx'
      % (best[0][0], best[0][1] * 100, best[1]))
cur = grid.get((200, 0.002))
print('现役格: SMA200 滞回 0.2%% -> %.2fx' % cur)
nb = []
for (n, h) in [(150, 0.002), (250, 0.002), (200, 0.0), (200, 0.005)]:
    if (n, h) in grid:
        nb.append(grid[(n, h)])
print('现役格的 4 个邻居: %s' % ', '.join('%.2fx' % x for x in nb))
verdict = '现役格不是孤峰（邻居也都不差）' if min(nb) > cur * 0.6 else '现役格周围有明显深坑'
print('邻居最差 %.2fx / 现役 %.2fx -> %s' % (min(nb), cur, verdict))

# ---- SMA 长度的成对稳健性：250 是真的更好，还是几个年份撑起来的？ ----
print()
print('=== 滚动 2 年窗口（步长 1 月）：SMA250 对 SMA200 的成对比较 ===')
W = BARS_PER_MONTH * 24
STEP2 = BARS_PER_MONTH
S200 = SM2[200]
S250 = SM2[250]
wins = 0
nw = 0
diffs = []
i = 0
while i + W <= N:
    a = run(C, L, S200, 200, 0.002, i, i + W)
    b = run(C, L, S250, 250, 0.002, i, i + W)
    nw += 1
    if b > a:
        wins += 1
    diffs.append((b - a) * 100)
    i += STEP2
diffs.sort()
print('窗口数 %d' % nw)
print('  SMA250 跑赢 SMA200 的窗口: %d (%.0f%%)' % (wins, 100.0 * wins / nw))
print('  差值(百分点) 中位 %+.1f  25%% %+.1f  75%% %+.1f  最差 %+.1f  最好 %+.1f'
      % (diffs[len(diffs) // 2], diffs[len(diffs) // 4], diffs[3 * len(diffs) // 4],
         diffs[0], diffs[-1]))
print()
print('  注意：窗口重叠，不是独立样本，只看方向。')

# ---- 逐年：250 的领先集中在哪几年 ----
print()
print('=== 逐年：SMA200 vs SMA250（成本 %.4f/边）===' % COST)
import datetime
yr = [datetime.datetime.utcfromtimestamp(t / 1000).year for t in T]
print('%6s %12s %12s %10s' % ('年', 'SMA200', 'SMA250', '谁赢'))
for y in sorted(set(yr)):
    idx = [k for k in range(N) if yr[k] == y]
    if len(idx) < 300:
        continue
    i0, i1 = idx[0], idx[-1] + 1
    a = run(C, L, S200, 200, 0.002, i0, i1)
    b = run(C, L, S250, 250, 0.002, i0, i1)
    print('%6d %11.2fx %11.2fx %10s' % (y, a, b, '250' if b > a else '200'))
