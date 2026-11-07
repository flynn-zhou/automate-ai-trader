# -*- coding: utf-8 -*-
"""R135-A：把 P1 里那 +0.3490% 的来源钉死。

数学事实：在无漂移随机游走中，若成交 bar fi 的选择只依赖 fi 及之前的信息，
则 E[(c[fi+HOLD]-c[fi])/c[fi]] 必须 = 0。实测 +0.349% 说明三件事之一：
  (a) 我的成交选择里混进了未来信息（前视）
  (b) 该均值被极端值拖动（中位数应≈0）
  (c) 数学适用条件不成立（序列自身有记忆/有漂移）

逐条排除：
  T1 分位数 + 中位数 + 对数收益均值   -> 查 (b)
  T2 随机成交对照（同样本量抽随机 bar）-> 查基准
  T3 方向性对照：把成交 bar 换成"同一根但只看未来 48 根"的独立抽样
  T4 前视探针：把成交规则改成"必须用 t 时刻之后的信息才能知道"的版本，
     若收益跳到荒谬水平，说明规则本身用了未来信息
"""
import math
import random
import sys

sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R

T0, STEP = 1567965300000, 300000
W, HOLD, WAIT, OFF = 50, 48, 96, 0.05


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


def quant(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * q))]


def analyze(name, rows, rnd_seed=999):
    o = [r[1] for r in rows]
    lo = [r[3] for r in rows]
    c = [r[4] for r in rows]
    n = len(rows)
    rmin = R.window_min(lo, W)

    fills = []
    i = W
    while i < n - HOLD:
        lp = rmin[i - 1] * (1 + OFF / 100.0)
        fi = -1
        for k in range(i, min(i + WAIT, n - HOLD)):
            if lo[k] <= lp:
                fi = k
                break
        if fi < 0:
            i += 1
            continue
        fills.append(fi)
        i = fi + HOLD + 1

    def B(f):
        return (c[f + HOLD] - c[f]) / c[f] * 100.0

    def LB(f):
        return math.log(c[f + HOLD] / c[f]) * 100.0

    bs = [B(f) for f in fills]
    lb = [LB(f) for f in fills]
    uncond = [(c[k + HOLD] - c[k]) / c[k] * 100.0 for k in range(W, n - HOLD)]
    uncond_l = [math.log(c[k + HOLD] / c[k]) * 100.0 for k in range(W, n - HOLD)]

    rnd = random.Random(rnd_seed)
    rand_f = [rnd.randrange(W, n - HOLD) for _ in range(len(fills))]
    rnd_b = [B(f) for f in rand_f]

    print("\n### %s   n_fills=%d" % (name, len(fills)))
    print("  T1 成交后持有收益 B:   均值 %+0.4f%%  中位 %+0.4f%%  5%% %+0.3f%%  95%% %+0.3f%%"
          % (sum(bs) / len(bs), quant(bs, .5), quant(bs, .05), quant(bs, .95)))
    print("     对数收益 LB:       均值 %+0.4f%%  中位 %+0.4f%%" % (sum(lb) / len(lb), quant(lb, .5)))
    print("  T2 随机 bar 对照:      均值 %+0.4f%%  中位 %+0.4f%%   (应与无条件一致)"
          % (sum(rnd_b) / len(rnd_b), quant(rnd_b, .5)))
    print("     无条件全样本:      均值 %+0.4f%%  中位 %+0.4f%%  对数均值 %+0.4f%%"
          % (sum(uncond) / len(uncond), quant(uncond, .5), sum(uncond_l) / len(uncond_l)))
    share = [f for f in fills if f == _first_in_window(f, fills)]
    # 成交 bar 是否"创新低"：lo[f] <= min(lo[f-W:f])
    newlow = sum(1 for f in fills if lo[f] <= min(lo[f - W:f])) * 100.0 / len(fills)
    print("     成交 bar 创 W 根新低比例: %.1f%%   <- 若 ~100%%，成交基本=破位当根" % newlow)
    big = sum(1 for f in fills if B(f) > 2.0)
    print("     B > +2%% 的成交占比: %.2f%%  （尾部是否在拖动均值）" % (big * 100.0 / len(fills)))
    return fills, bs


def _first_in_window(f, fills):
    return f


rows = synth_gbm(150000, seed=1)
analyze("P1 合成 GBM seed=1", rows)
rows2, _ = R.load("BTCUSDT", "5m")
analyze("真实 BTCUSDT 5m", rows2)
