# -*- coding: utf-8 -*-
"""R149：通过穷举闸 + 安慰剂闸的最终口径，在真实 BTC 上重测。

判定：low[j] <= lp，low 用真实最低价 l[j]（盘中触及，成交瞬间确定，无前视）
起点：C[j+1]（成交根之后一根收盘，严格在决策之后）
出场：C[j+1+HOLD]
成本：maker 进 / taker 出 + 资金费
"""
import bisect
import math
import random
import sqlite3
import time

HIST = "/home/cryptobot/ai-trader/data/hist.db"
F_IN, F_OUT = 0.0002, 0.0005


def rmin(arr, w):
    n = len(arr); m = [0.0] * n; dq = []; head = 0
    for k in range(n):
        v = arr[k]
        while len(dq) > head and arr[dq[-1]] >= v:
            dq.pop()
        dq.append(k)
        if dq[head] <= k - w:
            head += 1
        m[k] = arr[dq[head]]
    return m


def backtest(C, T, LO, fts=None, cum=None, w=50, wait=96, hold=48, off=0.05):
    n = len(C)
    rn = rmin(LO, w)
    out = []
    k = w
    while k < n - hold - 1:
        lp = rn[k - 1] * (1 + off / 100.0)
        hit = -1
        for j in range(k, min(k + wait, n - hold - 1)):
            if LO[j] <= lp:
                hit = j
                break
        if hit < 0:
            k += 1
            continue
        s, e = hit + 1, hit + 1 + hold
        if e >= n:
            k = hit + hold + 2
            continue
        entry, exit_px = C[s], C[e]
        if fts:
            a = bisect.bisect_left(fts, T[s]); b = bisect.bisect_left(fts, T[e])
            fund = (cum[b] - cum[a]) * 100.0
        else:
            fund = 0.0
        net = (exit_px - entry) / entry * 100.0 - fund - (F_IN + F_OUT) * 100.0
        out.append((T[hit], net))
        k = hit + hold + 2
    return out


def st(out, tag, show=False):
    if len(out) < 3:
        print("   %-44s n=%d" % (tag, len(out)))
        return
    v = [x[1] for x in out]
    m = sum(v) / len(v)
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
    t = m / (sd / math.sqrt(len(v)))
    ys = {}
    for ts, x in out:
        ys.setdefault(time.gmtime(ts / 1000.0).tm_year, []).append(x)
    my = {y: sum(a) / len(a) for y, a in ys.items()}
    pos = sum(1 for y, a in my.items() if len(ys[y]) > 20 and a > 0)
    tot = sum(1 for y, a in my.items() if len(ys[y]) > 20)
    cut = int(len(v) * 0.7)
    mo = sum(v[cut:]) / len(v[cut:])
    print("   %-44s n=%-5d 单笔 %+0.4f%%  t=%6.2f  年 %d/%d  后30%% %+0.4f%%"
          % (tag, len(v), m, t, pos, tot, mo))
    if show:
        print("      逐年: " + "  ".join("%s:%+0.3f" % (y, my[y]) for y in sorted(my) if len(ys[y]) > 20))
    return m, t


def synth(n, sigma=0.0015, seed=1):
    rnd = random.Random(seed)
    C = [10000.0]; T = [1567965300000]
    LO = [C[0]]
    for i in range(n - 1):
        o = C[-1]
        c = o * math.exp(rnd.gauss(0, sigma))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, sigma * 0.4)))
        C.append(c); LO.append(lo); T.append(T[-1] + 300000)
    return C, T, LO


print("最终口径：判定 l[j]<=lp（盘中触及），起点 C[j+1]，持有 hold 根")
print("=" * 104)
print("安慰剂闸（必须≈0）")
for seed in (1, 2, 3):
    C, T, LO = synth(150000, seed=seed)
    st(backtest(C, T, LO), "  P1 纯 GBM seed=%d" % seed)

c = sqlite3.connect(HIST)
rows = c.execute("select open_time,o,h,l,c from k2 where symbol='BTCUSDT' and interval='5m' order by open_time").fetchall()
fr = c.execute("select ts, rate from funding where symbol='BTCUSDT' order by ts").fetchall()
c.close()
C = [r[4] for r in rows]; T = [r[0] for r in rows]; LO = [r[3] for r in rows]
fts = [x[0] for x in fr]
cum = [0.0]; acc = 0.0
for _, v in fr:
    acc += v; cum.append(acc)

print()
print("真实数据")
st(backtest(C, T, LO, fts, cum), "  BTCUSDT 5m W=50 WAIT=96 HOLD=48 off=0.05", show=True)
print()
print("参数敏感性（hold=48）")
for w in (50, 100, 200):
    for wait in (24, 48, 96):
        for off in (0.0, 0.05, 0.2):
            st(backtest(C, T, LO, fts, cum, w, wait, 48, off),
               "  W=%d WAIT=%d off=%.2f" % (w, wait, off))
print()
print("不同持有期（W=50 WAIT=96 off=0.05）")
for hold in (12, 24, 48, 96, 192):
    st(backtest(C, T, LO, fts, cum, 50, 96, hold, 0.05), "  HOLD=%d" % hold)
