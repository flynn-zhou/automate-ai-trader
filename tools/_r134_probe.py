# -*- coding: utf-8 -*-
"""定位 B 为什么在纯随机游走里也是正的：逐项对照。"""
import math, random, sys
sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R

T0, STEP = 1567965300000, 300000
def synth_gbm(n, sigma=0.0015, seed=1):
    rnd = random.Random(seed); rows=[]; px=10000.0
    for i in range(n):
        o=px; c=o*math.exp(rnd.gauss(0,sigma))
        hi=max(o,c)*(1+abs(rnd.gauss(0,sigma*0.4))); lo=min(o,c)*(1-abs(rnd.gauss(0,sigma*0.4)))
        rows.append((T0+i*STEP,o,hi,lo,c)); px=c
    return rows

def probe(name, rows, W=50, HOLD=48, WAIT=96, OFF=0.05):
    R.prepare(rows)
    o,l,c = R.ARRS["o"], R.ARRS["l"], R.ARRS["c"]
    n = R.ARRS["n"]
    rmin = R.window_min(l, W)
    fills=[]
    i = W
    while i < n - HOLD:
        lp = rmin[i-1]*(1+OFF/100.0)
        fi=-1
        for k in range(i, min(i+WAIT, n-HOLD)):
            if l[k] <= lp: fi=k; break
        if fi<0: i+=1; continue
        fills.append(fi); i = fi+HOLD+1
    def m(x): return sum(x)/len(x)
    # 1) 无条件：所有 bar 的 HOLD 根收益
    uncond = [(c[k+HOLD]-c[k])/c[k]*100 for k in range(W, n-HOLD)]
    # 2) fill bar 的条件收益
    cond = [(c[f+HOLD]-c[f])/c[f]*100 for f in fills]
    # 3) 从 fill bar 的最低价买、收盘卖（A）与 从最低价买、持有 HOLD（A+B）
    A = [(c[f]-l[f])/l[f]*100 for f in fills]
    AB = [(c[f+HOLD]-l[f])/l[f]*100 for f in fills]
    # 4) 但 l[f] 是当根最低价 = 事后才知道"能被填在那"，它永远 >= 真实挂单价 lp
    #    统计 l[f] 相对 lp 的位置、以及 f 在扫描窗口里的位置
    pos = []
    for f in fills:
        # 找它属于哪个扫描窗口（近似：上一个 fill 之后）
        pass
    print("  %-28s n=%-5d 无条件 %+0.4f%% | fill条件 %+0.4f%% | A(低点->收盘) %+0.4f%% | 低点->HOLD后 %+0.4f%%"
          % (name, len(fills), m(uncond), m(cond), m(A), m(AB)))
    # 5) 关键：fill bar 的收盘相对"当根开盘"的位移（是否系统性收在低点）
    disp = [(c[f]-o[f])/o[f]*100 for f in fills]
    print("     fill bar 当根振幅（收-开）均值 %+0.4f%%  <- 若显著为负，说明被选中的都是下跌 bar" % m(disp))

print("W=50 HOLD=48 WAIT=96 off=0.05，全部单位 %/笔")
print("="*120)
probe("P1 合成 GBM seed=1", synth_gbm(150000, seed=1))
probe("P1 合成 GBM seed=2", synth_gbm(150000, seed=2))
rows, fr = R.load("BTCUSDT", "5m")
probe("真实 BTCUSDT", rows)
rows2, _ = R.load("ETHUSDT", "5m")
probe("真实 ETHUSDT", rows2)
