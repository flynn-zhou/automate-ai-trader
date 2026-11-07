# -*- coding: utf-8 -*-
"""R135-C：把 +0.34% 的来源钉死。四个互斥假设，逐个验。

H1 成交 bar 全是"下影线触摸"型：lp > C[f]，买入价高于收盘 -> 记账差
H2 成交 bar 与未来同一时段的暴涨行"恰好重合" -> 说明选择用了未来信息
H3 效应随时间衰减（短期反转） -> 是真实微观结构，不一定是伪影
H4 只在特定 horizon 出现 -> 是 horizon 与选择窗口 WAIT 的共谋

关键判别量：把"成交集合"的收益，与"同样后向跌幅的随机 bar"的收益对比。
若 matched control 也是 +0.34%，那这 0.34% 是"跌了之后会弹"的 beta，不是策略。
"""
import math
import random

T0, STEP = 1567965300000, 300000
W, HOLD, WAIT, OFF = 50, 48, 96, 0.05
SIG = 0.0015


def synth(n, sigma=SIG, seed=1):
    rnd = random.Random(seed)
    O, H, L, C = [], [], [], []
    px = 10000.0
    for _ in range(n):
        o = px
        c = o * math.exp(rnd.gauss(0, sigma))
        O.append(o)
        H.append(max(o, c) * (1 + abs(rnd.gauss(0, sigma * 0.4))))
        L.append(min(o, c) * (1 - abs(rnd.gauss(0, sigma * 0.4))))
        C.append(c)
        px = c
    return O, H, L, C


def rmin(arr, w):
    n = len(arr)
    m = [0.0] * n
    dq = []
    head = 0
    for k in range(n):
        v = arr[k]
        while len(dq) > head and arr[dq[-1]] >= v:
            dq.pop()
        dq.append(k)
        if dq[head] <= k - w:
            head += 1
        m[k] = arr[dq[head]]
    return m


def run(name, O, H, L, C):
    n = len(C)
    rn = rmin(L, W)
    fills = []
    i = W
    while i < n - HOLD:
        lp = rn[i - 1] * (1 + OFF / 100.0)
        fi = -1
        for k in range(i, min(i + WAIT, n - HOLD)):
            if L[k] <= lp:
                fi = k
                break
        if fi < 0:
            i += 1
            continue
        fills.append(fi)
        i = fi + HOLD + 1

    def lg(a, b):
        return math.log(C[b] / C[a]) * 100.0

    fwd = [lg(f, f + HOLD) for f in fills]
    bwd = [lg(f - HOLD, f) for f in fills]
    m = lambda x: sum(x) / len(x) if x else 0.0

    print("\n### %s   n=%d  fills=%d" % (name, n, len(fills)))
    print("  H4 不同 horizon 的前向对数收益（单位 %/笔）：")
    for hz in (6, 12, 24, 48, 96, 192):
        v = [lg(f, f + hz) for f in fills if f + hz < n]
        print("      horizon %3d 根: %+0.4f%%" % (hz, m(v)))

    # H1：成交时 lp 与收盘的关系
    lp_above = 0
    for f in fills:
        if C[f] < rn[f - 1] * (1 + OFF / 100.0):
            lp_above += 1
    print("  H1 成交时买入价高于当根收盘的比例: %.1f%%" % (lp_above * 100.0 / len(fills)))

    # 关键对照：按后向跌幅分桶，比较 fills 与"同样后向跌幅的全体 bar"
    b_all = {k: lg(k - HOLD, k) for k in range(W + HOLD, n - HOLD)}
    buckets = [(-99, -3), (-3, -2), (-2, -1), (-1, -0.5), (-0.5, 0), (0, 99)]
    print("  H2/H3 后向跌幅分桶对照（fills vs 全体 bar 的前向 48 根收益）：")
    print("      %-14s %-22s %-22s" % ("后向区间(%)", "fills 均值(n)", "全体同桶均值(n)"))
    for lo, hi in buckets:
        fs = [lg(f, f + HOLD) for f in fills if f + HOLD < n and lo <= lg(f - HOLD, f) < hi]
        as_ = [lg(k, k + HOLD) for k, b in b_all.items()
               if k + HOLD < n and lo <= b < hi]
        print("      [%5.1f,%5.1f)   %+0.4f%% (n=%-5d)  %+0.4f%% (n=%-6d)  %s"
              % (lo, hi, m(fs), len(fs), m(as_), len(as_),
                 "  <-- 差异 %.3f" % (m(fs) - m(as_)) if fs and as_ else ""))


print("=" * 104)
O, H, L, C = synth(150000, seed=1)
run("纯合成随机游走 seed=1", O, H, L, C)
print()
print("=" * 104)
O, H, L, C = synth(150000, seed=5)
run("纯合成随机游走 seed=5", O, H, L, C)
