# -*- coding: utf-8 -*-
"""R135-B：不用任何外部模块，纯手写复现，把前视点钉死。

疑点：纯随机游走里，"成交后 48 根持有收益" = +0.35%，数学上必须 = 0。
做法：不复用 research_limit_neighbor，重新独立实现一遍，并加上
      前向 / 后向 / 随机对照 / 简单规则对照 四组，看偏到底出在哪一侧。
"""
import math
import random

T0, STEP = 1567965300000, 300000
W, HOLD, WAIT, OFF = 50, 48, 96, 0.05


def synth(n, sigma=0.0015, seed=1):
    rnd = random.Random(seed)
    O, H, L, C = [], [], [], []
    px = 10000.0
    for _ in range(n):
        o = px
        c = o * math.exp(rnd.gauss(0, sigma))
        w1 = abs(rnd.gauss(0, sigma * 0.4))
        w2 = abs(rnd.gauss(0, sigma * 0.4))
        O.append(o)
        H.append(max(o, c) * (1 + w1))
        L.append(min(o, c) * (1 - w2))
        C.append(c)
        px = c
    return O, H, L, C


def rmin(arr, w):
    """m[k] = min(arr[max(0,k-w+1)..k])"""
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


def report(name, O, H, L, C):
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

    def fwd(f):
        return math.log(C[f + HOLD] / C[f]) * 100.0

    def bwd(f):
        return math.log(C[f] / C[f - HOLD]) * 100.0 if f - HOLD >= 0 else 0.0

    fs = [fwd(f) for f in fills]
    bs = [bwd(f) for f in fills]
    allf = [math.log(C[k + HOLD] / C[k]) * 100.0 for k in range(W, n - HOLD)]
    rnd = random.Random(1)
    ctl = [math.log(C[k + HOLD] / C[k]) * 100.0
           for k in (rnd.randrange(W, n - HOLD) for _ in range(len(fills)))]

    # 简单规则对照：只要求"当根创 W 根新低"，不做等待窗口
    simple = [k for k in range(W, n - HOLD) if L[k] <= rn[k - 1]]
    sf = [math.log(C[k + HOLD] / C[k]) * 100.0 for k in simple]

    def m(x):
        return sum(x) / len(x) if x else 0.0

    print("\n### %s   n=%d  fills=%d  simple=%d" % (name, n, len(fills), len(simple)))
    print("  前向 48 根（成交 bar 收盘起算）: %+0.4f%%   <- 必须在 0 附近" % m(fs))
    print("  后向 48 根（成交 bar 之前）    : %+0.4f%%   <- 若是新低应显著为负" % m(bs))
    print("  全样本无条件                   : %+0.4f%%" % m(allf))
    print("  同量随机 bar 对照              : %+0.4f%%" % m(ctl))
    print("  简单规则（新 W 根低点）前向     : %+0.4f%%   <- 逐条对照，看是不是规则本身" % m(sf))
    return fills


print("=" * 96)
print("A. 纯合成随机游走（理论上一切都该是 0）")
O, H, L, C = synth(150000, seed=1)
report("GBM seed=1", O, H, L, C)

print()
print("=" * 96)
print("B. 对照：把价格序列换成【完全单调上升】，任何买入都应该赚 (beta)，用来确认脚本没写反")
n = 20000
O2, H2, L2, C2 = [], [], [], []
px = 10000.0
rr = random.Random(3)
for _ in range(n):
    o = px
    c = px * 1.0001
    O2.append(o); H2.append(max(o, c) * 1.0005); L2.append(min(o, c) * 0.9995); C2.append(c)
    px = c
report("纯上升趋势（beta 应显著为正）", O2, H2, L2, C2)
