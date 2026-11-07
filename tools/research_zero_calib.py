# -*- coding: utf-8 -*-
"""校准后的零基准：GBM 的 bar 几何必须与真实 BTC 匹配，伪影才可比。

实测（最近 60000 根 5m）：
  真实 BTC  振幅中位 0.1118%   上影 0.271  下影 0.232
  原 GBM    振幅中位 0.2017%   上影 0.286  下影 0.263   <- 振幅大 1.8 倍

振幅大 -> "买在最低点"的伪影更大 -> 残差被低估。
这里把 sigma 校准到振幅中位一致，再重算残差。
"""
import math, random, sys, statistics
sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R

T0, STEP = 1567965300000, 300000
COST = 0.04


def synth(n, seed, sigma):
    rnd = random.Random(seed)
    rows = []
    px = 10000.0
    for i in range(n):
        o = px
        c = o * math.exp(rnd.gauss(0.0, sigma))
        hi = max(o, c) * (1 + abs(rnd.gauss(0, sigma * 0.4)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, sigma * 0.4)))
        rows.append((T0 + i * STEP, o, hi, lo, c))
        px = c
    return rows


def med_range(rows):
    seg = rows[-60000:]
    v = sorted((r[2] - r[3]) / r[3] * 100 for r in seg)
    return v[len(v) // 2]


def stat(xs):
    n = len(xs)
    if n < 3:
        return 0, 0.0, 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return n, m, m / (sd / math.sqrt(n)) if sd > 0 else 0.0


def run(rows, W, HOLD, WAIT, OFF):
    R.prepare(rows)
    l_, c_ = R.ARRS["l"], R.ARRS["c"]
    n = R.ARRS["n"]
    rmin = R.window_min(l_, W)
    mult = 1.0 + OFF / 100.0
    rets = []
    i = W
    end = n - HOLD
    while i < end:
        lp = rmin[i - 1] * mult
        fi = -1
        for k in range(i, min(i + WAIT, end)):
            if l_[k] <= lp:
                fi = k
                break
        if fi < 0:
            i += 1
            continue
        ei = min(fi + HOLD, n - 1)
        if ei <= fi:
            i = fi + 1
            continue
        rets.append((c_[ei] - c_[fi]) / c_[fi] * 100.0)   # 收盘进、收盘出（无伪影）
        i = ei + 1
    return rets


rows, _ = R.load("BTCUSDT", "5m")
target = med_range(rows)
print("真实 BTC 振幅中位 = %.4f%%" % target)
lo_s, hi_s = 0.0004, 0.0020
for _ in range(22):
    mid = (lo_s + hi_s) / 2
    if med_range(synth(20000, 1, mid)) < target:
        lo_s = mid
    else:
        hi_s = mid
sig = (lo_s + hi_s) / 2
print("校准后 sigma = %.6f  -> GBM 振幅中位 = %.4f%%" % (sig, med_range(synth(20000, 1, sig))))
gb = synth(len(rows), 1, sig)

CELLS = [(50, 48, 96, 0.05), (100, 48, 48, 0.02), (200, 96, 96, 0.10),
         (200, 48, 96, 0.05), (150, 72, 96, 0.05), (50, 24, 24, 0.02)]
print()
print("%-26s %10s %7s %10s %7s %10s %10s" % ("格", "真实", "t", "校准GBM", "t", "残差", "判定"))
print("-" * 100)
for (W, H, WA, O) in CELLS:
    n1, m1, t1 = stat(run(rows, W, H, WA, O))
    n2, m2, t2 = stat(run(gb, W, H, WA, O))
    r = m1 - m2
    v = "有路" if r > COST else ("仅覆盖成本" if r > 0 else "负")
    print("W=%-3d H=%-2d WAIT=%-2d off=%.2f %+9.4f%% %7.2f %+9.4f%% %7.2f %+9.4f%% %10s"
          % (W, H, WA, O, m1, t1, m2, t2, r, v))
print("-" * 100)
print("门槛：每笔 > 0.14%%（用户设定）；往返成本 0.04%%")
