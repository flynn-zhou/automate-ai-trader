# -*- coding: utf-8 -*-
"""零基准法全表：每一格都同时跑【真实 BTC】和【同参数合成 GBM】，
残差 = 真实 - GBM 才是一笔交易的真实期望。

判据：残差 > 往返成本(0.04%)+资金费 才算有路；否则整格作废。
"""
import math, random, sys
sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R

T0, STEP = 1567965300000, 300000
COST = 0.04  # 挂单0.02 + 吃单0.02，百分比


def synth(n, seed=1, sigma=0.0015):
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


def stat(xs):
    n = len(xs)
    if n < 3:
        return 0, 0.0, 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return n, m, m / (sd / math.sqrt(n)) if sd > 0 else 0.0


def run(rows, W, HOLD, WAIT, OFF, entry_mode):
    R.prepare(rows)
    o_, l_, c_ = R.ARRS["o"], R.ARRS["l"], R.ARRS["c"]
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
        entry = c_[fi] if entry_mode == "close" else lp
        rets.append((c_[ei] - entry) / entry * 100.0)
        i = ei + 1
    return rets


CELLS = [(50, 48, 96, 0.05), (50, 48, 48, 0.02), (100, 48, 48, 0.02),
         (100, 96, 96, 0.05), (200, 96, 96, 0.10), (200, 48, 96, 0.05),
         (150, 72, 96, 0.05), (50, 24, 24, 0.02)]

rows, _ = R.load("BTCUSDT", "5m")
gb = synth(len(rows), seed=1)

print("=" * 118)
print("零基准法：真实 BTC 减去同参数合成 GBM，残差才是真实边际")
print("往返成本 = %.2f%%   门槛（用户设定） = 每笔 > 0.14%%" % COST)
print("=" * 118)
print("%-26s %10s %9s %10s %9s %10s %8s" % ("格", "真实均值", "t", "GBM均值", "t", "残差", "判定"))
print("-" * 118)
keep = 0
for (W, HOLD, WAIT, OFF) in CELLS:
    rr = run(rows, W, HOLD, WAIT, OFF, "close")
    gg = run(gb, W, HOLD, WAIT, OFF, "close")
    n1, m1, t1 = stat(rr)
    n2, m2, t2 = stat(gg)
    resid = m1 - m2
    verdict = "有路" if resid > COST else ("覆盖成本" if resid > 0 else "负")
    if resid > 0:
        keep += 1
    print("W=%-3d H=%-2d WAIT=%-2d off=%.2f %+9.4f%% %9.2f %+9.4f%% %9.2f %+9.4f%% %8s"
          % (W, HOLD, WAIT, OFF, m1, t1, m2, t2, resid, verdict))
print("-" * 118)
print("全部 %d 格中，残差 > 0 的: %d 格；残差 > 成本 %.2f%% 的: %d 格"
      % (len(CELLS), keep, COST, sum(1 for (W, H, WA, O) in CELLS
                                     if stat(run(rows, W, H, WA, O, "close"))[1]
                                      - stat(run(gb, W, H, WA, O, "close"))[1] > COST)))
