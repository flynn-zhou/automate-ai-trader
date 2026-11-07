# -*- coding: utf-8 -*-
"""定位：在纯合成随机游走上，'成交 bar 之后 48 根'的收益为什么是正的。

中位 +0.376% 而理论应 = 0 —— 不是离群值，是整条分布右移了。
这只能说明【成交取样有偏】：被选中的 bar 系统性地位于上升段起点。

直接验：成交 bar 在前向窗口里是不是偏向"局部最低"，
以及成交 bar 的等权重 vs 时间加权是否不同。
"""
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

fills = []
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
    fills.append(fi)
    i = fi + HOLD

print("成交数 %d / 总 bar %d" % (len(fills), n))
# 1) 成交 bar 相对前向窗口的位置
pos = [(c[f + HOLD] - c[f]) / c[f] * 100 for f in fills]
print("\n[1] 成交 bar 之后 HOLD 根的收益 = %+0.4f%%  (应=0)" % (sum(pos) / len(pos)))

# 2) 同样的 bar 索引，但换成"前向窗口之前"的收益
pos_b = [(c[f] - c[f - HOLD]) / c[f - HOLD] * 100 for f in fills if f - HOLD >= 0]
print("[2] 成交 bar 之前 HOLD 根的收益 = %+0.4f%%  (若选样有偏，这里应为负)" % (sum(pos_b) / len(pos_b)))

# 3) 成交 bar 是不是局部最低？看它在 [f-48, f+48] 里的相对位置
rank = []
for f in fills:
    lo_i, hi_i = max(0, f - HOLD), min(n, f + HOLD + 1)
    seg = c[lo_i:hi_i]
    rank.append(sum(1 for x in seg if x < c[f]) * 1.0 / len(seg))
print("[3] 成交 bar 在 ±48 根里的分位（0=最低，1=最高）: %.3f" % (sum(rank) / len(rank)))

# 4) 全部 bar 的 HOLD 收益均值（无条件）
allr = [(c[k + HOLD] - c[k]) / c[k] * 100 for k in range(W, n - HOLD)]
print("[4] 全部 bar 的 HOLD 收益均值 = %+0.4f%%  (基准)" % (sum(allr) / len(allr)))

# 5) 关键对照：成交 bar 若均匀随机，均值应等于 [4]。差异来自选样偏。
print("\n结论提示：若 [1] 显著 > 0 而 [2] < 0，说明成交集中发生在局部低点——")
print("这本身是真实的（限价单确实在低点成交），但【持有期收益】应以此为条件重新计算。")
