# -*- coding: utf-8 -*-
"""根因定位：拆解单笔收益，看它到底来自哪一步。

net = A + B
  A = (成交当根收盘 - 限价) / 限价   <-- "买在当根最低点、按当根收盘估值"的记账差
  B = (持有期末收盘 - 成交当根收盘) / 成交当根收盘  <-- 真正的持有期收益（随机游走下应为 0）
如果 A 占绝对主导，说明这个"边际"是行情颗粒度造成的记账伪影，不是预测能力。
"""
import math, random, sys
sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R


def decompose(name, rows):
    R.prepare(rows)
    R.ARRS["fund_ts"] = []
    R.ARRS["fund_cum"] = [0.0]
    c, l = R.ARRS["c"], R.ARRS["l"]
    n = R.ARRS["n"]
    W, HOLD, WAIT, OFF = 50, 48, 96, 0.05
    rmin = R.window_min(l, W)
    A, B = [], []
    i = W
    while i < n - HOLD:
        lp = rmin[i - 1] * (1 + OFF / 100.0)
        fi = -1
        for k in range(i, min(i + WAIT, n - HOLD)):
            if l[k] <= lp:
                fi = k
                break
        if fi < 0:
            i += 1
            continue
        ex = fi + HOLD
        a = (c[fi] - lp) / lp * 100.0
        b = (c[ex] - c[fi]) / c[fi] * 100.0
        A.append(a); B.append(b)
        i = ex + 1

    def m(x):
        return sum(x) / len(x) if x else 0.0

    def med(x):
        s = sorted(x)
        return s[len(s) // 2] if s else 0.0
    print("  %-40s n=%-5d  A(当根最低价->当根收盘) %+0.4f%% (中位 %+0.4f%%)   B(持有期本身) %+0.4f%%   A 占比 %.0f%%"
          % (name, len(A), m(A), med(A), m(B), 100 * m(A) / (m(A) + m(B)) if (m(A) + m(B)) else 0))


T0, STEP = 1567965300000, 300000


def synth_gbm(n, sigma=0.0015, seed=1):
    rnd = random.Random(seed)
    rows, px = [], 10000.0
    for i in range(n):
        o = px
        c = o * math.exp(rnd.gauss(0, sigma))
        hi = max(o, c) * (1 + abs(rnd.gauss(0, sigma * 0.4)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, sigma * 0.4)))
        rows.append((T0 + i * STEP, o, hi, lo, c))
        px = c
    return rows


print("单笔收益拆解（W=50 HOLD=48 WAIT=96 off=0.05）")
print("=" * 130)
rows, fr = R.load("BTCUSDT", "5m")
decompose("真实 BTCUSDT 5m", rows)
decompose("P1 合成 GBM seed=1", synth_gbm(150000, seed=1))
c_arr = [x[4] for x in rows]
rets = [math.log(c_arr[i + 1] / c_arr[i]) for i in range(len(c_arr) - 1)]
rr = list(rets); random.Random(7).shuffle(rr)
from_arr = []
px = 10000.0
so = random.Random(107)
for i, r in enumerate(rr):
    o = px; c = o * math.exp(r)
    w1 = abs(so.gauss(0, abs(r) + 1e-6)); w2 = abs(so.gauss(0, abs(r) + 1e-6))
    from_arr.append((T0 + i * STEP, o, max(o, c) * (1 + w1), min(o, c) * (1 - w2), c))
    px = c
decompose("P2 打乱收益 seed=7", from_arr)
print()
print("判读：B 是可交易的真实持有收益，A 是「按当根最低价成交、按当根收盘估值」的记账差。")
print("      A 占大头 -> 所谓边际来自 bar 颗粒度，不来自预测。")
