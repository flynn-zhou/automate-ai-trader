# -*- coding: utf-8 -*-
"""最终判决：用【无偏】的信号构造（B：当根创新低才进场）重测，
配合【校准过的零基准】（GBM 振幅与真实 BTC 一致）。

为什么弃用 A（陈旧限价）：
  严格鞅（二项随机游走，E[c[t+H]]=c[t] 精确成立）上
    A 陈旧限价 -> +0.1633%  t=17.18   <- 在随机游走上"赚"钱，构造有偏
    B 当根破位 -> -0.0006%  t=-0.07   <- 正确读数为 0
  所以 A 的任何正数都不能作为证据。

判据（沿用用户设定）：真实均值 - 校准GBM均值 > 往返成本 0.04% 才算有路；
                      用户门槛是每笔 > 0.14%。
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


def runB(rows, W, HOLD, WAIT, OFF):
    """B：只在该 bar 自己创 W 根新低时进场（无偏）"""
    R.prepare(rows)
    l_, c_ = R.ARRS["l"], R.ARRS["c"]
    n = R.ARRS["n"]
    rmin = R.window_min(l_, W)
    mult = 1.0 + OFF / 100.0
    rets = []
    i = W
    while i < n - HOLD:
        hit = -1
        for k in range(i, min(i + WAIT, n - HOLD)):
            if l_[k] <= rmin[k - 1] * mult:
                hit = k
                break
        if hit < 0:
            i += 1
            continue
        ei = hit + HOLD
        rets.append((c_[ei] - c_[hit]) / c_[hit] * 100.0)
        i = ei + 1
    return rets


rows, _ = R.load("BTCUSDT", "5m")
target = med_range(rows)
lo_s, hi_s = 0.0004, 0.0020
for _ in range(22):
    mid = (lo_s + hi_s) / 2
    if med_range(synth(20000, 1, mid)) < target:
        lo_s = mid
    else:
        hi_s = mid
sig = (lo_s + hi_s) / 2
gb = synth(len(rows), 1, sig)

CELLS = [(50, 48, 96, 0.05), (50, 48, 48, 0.02), (100, 48, 48, 0.02),
         (100, 96, 96, 0.05), (200, 96, 96, 0.10), (200, 48, 96, 0.05),
         (150, 72, 96, 0.05), (50, 24, 24, 0.02)]

print("=" * 116)
print("B 构造（无偏）· 校准零基准  sigma=%.6f  振幅中位=%.4f%%" % (sig, target))
print("=" * 116)
print("%-26s %10s %7s %10s %7s %10s %9s" % ("格", "真实", "t", "校准GBM", "t", "残差", "判定"))
print("-" * 116)
good = 0
for (W, H, WA, O) in CELLS:
    n1, m1, t1 = stat(runB(rows, W, H, WA, O))
    n2, m2, t2 = stat(runB(gb, W, H, WA, O))
    r = m1 - m2
    v = "有路" if r > 0.14 else ("覆盖成本" if r > COST else "负")
    if r > COST:
        good += 1
    print("W=%-3d H=%-2d WAIT=%-2d off=%.2f %+9.4f%% %7.2f %+9.4f%% %7.2f %+9.4f%% %9s"
          % (W, H, WA, O, m1, t1, m2, t2, r, v))
print("-" * 116)
print("残差 > 成本 %.2f%% 的格: %d/%d ；残差 > 用户门槛 0.14%% 的格: %d"
      % (COST, good, len(CELLS),
         sum(1 for (W, H, WA, O) in CELLS
             if stat(runB(rows, W, H, WA, O))[1] - stat(runB(gb, W, H, WA, O))[1] > 0.14)))
