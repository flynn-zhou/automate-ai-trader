# -*- coding: utf-8 -*-
"""决定性验证：把 fill 价从 lp(限价) 换成 c[fi](成交当根收盘)，看 alpha 还剩多少。

原理（上一会话定位）：
  脚本用 entry = lp 记账，但判定成交用的是 bar 的【最低价】。
  被 lp 打中的 bar，收盘价普遍远高于 lp（低点在下、收盘在上），
  等于默认"买在当根最低点、按当根收盘估值"——这段差价是 K 线颗粒度伪影。

  随机游走上真正的"收盘买、收盘卖"应该 ≈ 0（本地实测 +0.0275%）。
  而脚本报 +0.29~+0.35%。差的 12 倍就是这个伪影。

本脚本对同一批成交做四种记账，全部同一信号、同一持有期：
  M0  entry = lp          （原脚本，已知有偏）
  M1  entry = c[fi]       （收盘买，最保守）
  M2  entry = min(lp, o[fi])  （限价或开盘，取更优）
  M3  entry = o[fi]       （次日开盘市价）
并在【合成 GBM】和【真实 BTC】上同时跑，看哪个模型的 GBM 读数为 0。
"""
import math, random, sys
sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R

T0, STEP = 1567965300000, 300000
CELLS = [(50, 48, 96, 0.05), (100, 48, 48, 0.02), (200, 96, 96, 0.10)]
W_, HOLD_, WAIT_, OFF_ = 50, 48, 96, 0.05


def synth(n, sigma=0.0015, seed=1):
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


def stats(xs):
    n = len(xs)
    if n < 3:
        return n, 0.0, 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return n, m, m / (sd / math.sqrt(n))


def run(rows, W, HOLD, WAIT, OFF, mode):
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
        ei = fi + HOLD
        if ei > n - 1:
            ei = n - 1
        if ei <= fi:
            i = fi + 1
            continue
        if mode == "M0":
            entry = lp
        elif mode == "M1":
            entry = c_[fi]
        elif mode == "M2":
            entry = min(lp, o_[fi])
        else:
            entry = o_[fi]
        rets.append((c_[ei] - entry) / entry * 100.0)
        i = ei + 1
    return stats(rets)


def report(name, rows):
    print("### %s" % name)
    print("   %-6s %-28s %8s %11s %8s" % ("模型", "格", "样本", "单笔均值", "t"))
    for (W, HOLD, WAIT, OFF) in CELLS:
        tag = "W=%d H=%d WAIT=%d off=%.2f" % (W, HOLD, WAIT, OFF)
        for mode, mname in (("M0", "限价成交(lp) 原脚本"),
                            ("M1", "收盘成交(c[fi])"),
                            ("M2", "min(限价,开盘)"),
                            ("M3", "开盘成交(o[fi])")):
            n, m, t = run(rows, W, HOLD, WAIT, OFF, mode)
            print("   %-6s %-28s %8d %+10.4f%% %8.2f" % (mode, tag, n, m, t))
        print()


report("合成 GBM seed=1（无漂移无记忆，正确模型应读出 ≈ 0）", synth(150000))

rows, fr = R.load("BTCUSDT", "5m")
report("真实 BTCUSDT 5m（7.15 年）", rows)
