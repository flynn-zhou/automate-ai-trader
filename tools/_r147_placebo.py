# -*- coding: utf-8 -*-
"""R147：干净口径的安慰剂闸。

R146 在真实 BTC 上给出 +0.2920%/笔 t=16.84。必须先用随机游走验伪：
若纯 GBM 上也出现接近的数值，则 R146 的"干净"口径仍有泄漏。
"""
import math
import random
import bisect
import sqlite3
import time

HIST = "/home/cryptobot/ai-trader/data/hist.db"
W, WAIT, HOLD, OFF = 50, 96, 48, 0.05
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


def backtest(C, T, fts=None, cum=None, w=W, wait=WAIT, hold=HOLD, off=OFF):
    n = len(C)
    REF = [C[0]] + [C[i - 1] for i in range(1, n)]
    rn = rmin(REF, w)
    nets = []
    k = w
    while k < n - hold:
        lp = rn[k - 1] * (1 + off / 100.0)
        hit = -1
        for j in range(k, min(k + wait, n - hold)):
            if REF[j] <= lp:
                hit = j
                break
        if hit < 0:
            k += 1
            continue
        s = hit - 1
        e = s + hold
        if s < 0 or e >= n:
            k = hit + hold + 1
            continue
        entry, exit_px = C[s], C[e]
        if fts:
            a = bisect.bisect_left(fts, T[s]); b = bisect.bisect_left(fts, T[e])
            fund = (cum[b] - cum[a]) * 100.0
        else:
            fund = 0.0
        nets.append((exit_px - entry) / entry * 100.0 - fund - (F_IN + F_OUT) * 100.0)
        k = hit + hold + 1
    return nets


def st(v, tag):
    if len(v) < 3:
        print("   %-46s n=%d" % (tag, len(v)))
        return
    m = sum(v) / len(v)
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
    print("   %-46s n=%-5d 单笔 %+0.4f%%  t=%7.2f" % (tag, len(v), m, m / (sd / math.sqrt(len(v)))))


def synth(n, sigma=0.0015, seed=1, drift=0.0):
    rnd = random.Random(seed)
    C = [10000.0]
    T = [1567965300000]
    for i in range(n - 1):
        C.append(C[-1] * math.exp(rnd.gauss(drift, sigma)))
        T.append(T[-1] + 300000)
    return C, T


print("干净口径（判定=REF[j]=C[j-1]，起点=C[j-1]）的安慰剂闸")
print("=" * 100)
for seed in (1, 2, 3):
    C, T = synth(200000, seed=seed)
    st(backtest(C, T), "P1 纯 GBM seed=%d（零漂移零记忆，必须≈0）" % seed)

C, T = synth(200000, sigm=0.0015, seed=1, drift=0.00002) if False else synth(200000, seed=4, drift=0.00002)
st(backtest(C, T), "P1' GBM + 正漂移 0.002%/bar（应≈(48*0.002-0.07)=+0.026%）")

print()
print("真实数据")
c = sqlite3.connect(HIST)
rows = c.execute("select open_time,o,h,l,c from k2 where symbol='BTCUSDT' and interval='5m' order by open_time").fetchall()
fr = c.execute("select ts, rate from funding where symbol='BTCUSDT' order by ts").fetchall()
c.close()
C = [r[4] for r in rows]; T = [r[0] for r in rows]
fts = [x[0] for x in fr]
cum = [0.0]; acc = 0.0
for _, v in fr:
    acc += v; cum.append(acc)
st(backtest(C, T, fts, cum), "BTCUSDT 5m（R146 同口径）")
