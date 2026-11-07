# -*- coding: utf-8 -*-
"""给新会话的一条独立诊断线索：合成 GBM 上 t=9.65 但真实随机游走该是 0，
说明不是"边际"而是计算伪影。先看收益分布的离散度与离群值。"""
import sys, math, random
sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R

T0, STEP = 1567965300000, 300000


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


rows = synth(150000)
R.prepare(rows)
R.ARRS["fund_ts"] = []
R.ARRS["fund_cum"] = [0.0]
c = R.ARRS["c"]; l = R.ARRS["l"]; n = R.ARRS["n"]
W, HOLD, WAIT, OFF = 50, 48, 96, 0.05
rmin = R.window_min(l, W)

rets = []
bad = 0
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
    ei = fi + HOLD
    r = (c[ei] - c[fi]) / c[fi] * 100.0
    if not (r == r) or abs(r) > 50:
        bad += 1
    rets.append(r)
    i = ei + 1

n = len(rets)
m = sum(rets) / n
sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (n - 1))
s = sorted(rets)
print("合成 GBM（无漂移、无记忆）上的'持有期收益'：")
print("  样本 %d   均值 %+0.4f%%   标准差 %.3f%%   t=%.2f" % (n, m, sd, m / (sd / math.sqrt(n))))
print("  分位: P1 %+0.3f%%  P25 %+0.3f%%  中位 %+0.3f%%  P75 %+0.3f%%  P99 %+0.3f%%"
      % (s[int(n*0.01)], s[int(n*0.25)], s[n//2], s[int(n*0.75)], s[int(n*0.99)]))
print("  最差 %+0.2f%%   最好 %+0.2f%%" % (s[0], s[-1]))
print("  |收益|>50%% 的样本数: %d  （>0 说明有离群值在拉动均值）" % bad)
# 去掉最大 1% 后
trim = s[:int(n*0.99)]
mt = sum(trim)/len(trim)
print("  去掉最好 1%% 后均值: %+0.4f%%" % mt)
