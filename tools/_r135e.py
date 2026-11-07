# -*- coding: utf-8 -*-
"""R135-E：干净房间复现，打印前若干笔成交的全部中间量，人眼核对。

另外做真正的因果对照：把未来路径换成本 seed 之外的独立随机数（
即 eps[1..T] 用 seed A，eps[T+1..] 用 seed B），若成交仍为正，
则偏置与未来路径无关，只能出在"选择"本身；若归零，则我上一版的因果测试漏了东西。
"""
import math
import random

W, HOLD, WAIT, OFF = 50, 48, 96, 0.05


def gen(n, sigma=0.0015, seed=1):
    """只生成对数收益，返回 eps 列表"""
    rnd = random.Random(seed)
    return [rnd.gauss(0, sigma) for _ in range(n)]


def build(eps):
    O, H, L, C = [], [], [], []
    px = 10000.0
    rnd = random.Random(424242)
    for e in eps:
        o = px
        c = o * math.exp(e)
        w1 = abs(rnd.gauss(0, 0.0006))
        w2 = abs(rnd.gauss(0, 0.0006))
        O.append(o); H.append(max(o, c) * (1 + w1)); L.append(min(o, c) * (1 - w2)); C.append(c)
        px = c
    return O, H, L, C


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


def trace(L, C, ntr=6):
    n = len(L)
    rn = rmin(L, W)
    i = W
    shown = 0
    fills = []
    while i < n - HOLD:
        lp = rn[i - 1] * (1 + OFF / 100.0)
        fi = -1
        for k in range(i, min(i + WAIT, n - HOLD)):
            if L[k] <= lp:
                fi = k; break
        if fi < 0:
            i += 1; continue
        fills.append(fi)
        if shown < ntr:
            print("   i=%-6d f=%-6d wait=%-3d lp=%.6f L[f]=%.6f C[f]=%.6f C[f+%d]=%.6f fwd=%+.4f%%  rn[i-1]=%.6f"
                  % (i, fi, fi - i, lp, L[fi], C[fi], HOLD, C[fi + HOLD],
                     math.log(C[fi + HOLD] / C[fi]) * 100, rn[i - 1]))
            shown += 1
        i = fi + HOLD + 1
    return fills


N = 60000
print("=" * 120)
print("A. 现象复现 + 前 6 笔明细")
epsA = gen(N, seed=1)
O, H, L, C = build(epsA)
fA = trace(L, C)
rA = [math.log(C[f + HOLD] / C[f]) * 100 for f in fA if f + HOLD < N]
print("   全部 %d 笔，前向均值 %+.4f%%" % (len(rA), sum(rA) / len(rA)))

print()
print("=" * 120)
print("B. 决定性对照：保持【同一份成交选择所依赖的价格】，但把未来路径换成独立随机数")
print("   做法：以后段 30000 根为界，前半用 epsA，后半用 epsB(独立 seed)。")
print("   若成交选择真是因果的，前半段的成交应与 A 完全一致，且其前向收益仍应显著>0。")
epsB = gen(N, seed=99)
hyb = epsA[:N // 2] + epsB[N // 2:]
O2, H2, L2, C2 = build(hyb)
fB = trace(L2, C2, ntr=0)
cut = N // 2 - HOLD - 1
print("   混合序列前半段成交 %d 笔（A 前半段 %d 笔）" % (len([f for f in fB if f <= cut]), len([f for f in fA if f <= cut])))
rB = [math.log(C2[f + HOLD] / C2[f]) * 100 for f in fB if f + HOLD < N and f <= cut]
print("   前半段（未受污染的路径）前向均值 %+.4f%%   n=%d" % (sum(rB) / len(rB), len(rB)))

print()
print("=" * 120)
print("C. 同一条序列，成交集合 vs 随机抽同样多 bar 的前向均值（重复 200 次取分布）")
rnd = random.Random(7)
allr = {k: math.log(C[k + HOLD] / C[k]) * 100 for k in range(W, N - HOLD)}
means = []
keys = sorted(allr)
for _ in range(200):
    s = rnd.sample(keys, len(rA))
    means.append(sum(allr[k] for k in s) / len(s))
means.sort()
print("   随机抽样 200 次的均值分布：5%%=%+.4f%%  中位=%+.4f%%  95%%=%+.4f%%" % (means[10], means[100], means[190]))
print("   实际成交集合均值 %+.4f%%  ->  位于随机分布的 %s"
      % (sum(rA) / len(rA), "外部（异常）" if sum(rA) / len(rA) > means[190] else "内部（正常）"))
