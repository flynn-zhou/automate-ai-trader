# -*- coding: utf-8 -*-
"""决定性检验：样本外验证「中高波动里反向动量」这条唯一活着的线索。

严谨性要求：
1. 波动分位只用训练段算，再套到测试段（防止前视）。
2. 训练/测试各占一半，看两边期望是否同号。
3. 成本三档：吃单进出(0.10%)、挂单进吃单出(0.07%)、全挂单(0.04%)。
4. 报告集中度：利润是不是靠极少数几笔（那说明是运气不是边际）。
"""
import os, sys, statistics, math
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

bars = bt.load("data/backtest.db", "1m")
N = len(bars)
CLOSE = [b[4] for b in bars]
SPLIT = 70 + int((N - 500 - 70) * 0.5)

def sigma_at(i):
    return bt.sigma_ret(bars, i, 60)

def sig_at(i):
    return bt.detect(bars, i, {"sig": "all", "micro_sigma": 3.0})

def collect(a, b):
    """在 [a,b) 区间的信号：反向 + 对应的 sigma。"""
    out = []
    for i in range(a, b):
        s = sig_at(i)
        if not s: continue
        sg = sigma_at(i)
        if sg <= 0: continue
        d = -1 if s[0] == "long" else 1     # 反向
        out.append((i, d, sg))
    return out

tr_ = collect(70, SPLIT)
te_ = collect(SPLIT, N - 500)
sig_tr = sorted(x[2] for x in tr_)
vlo = sig_tr[int(len(sig_tr)*0.50)]
vhi = sig_tr[int(len(sig_tr)*0.75)]
print("训练段 %d 个信号，波动分位 p50=%.4f%% p75=%.4f%%（只用训练段算）" % (len(tr_), vlo, vhi))
print("测试段 %d 个信号" % len(te_))
print()

def sim(sigs, hold, stop_pct, cost, v_lo, v_hi):
    """反向入场 -> 固定持有 hold 分钟 / 止损 stop_pct。返回每笔净收益(%)。"""
    res = []
    for i, d, sg in sigs:
        if not (v_lo <= sg < v_hi): continue
        if i + hold >= N: continue
        entry = CLOSE[i]
        if not entry: continue
        stop = entry * (1 - d * stop_pct / 100.0)
        px = None
        for k in range(i + 1, min(i + hold + 1, N)):
            lo, hi = bars[k][3], bars[k][2]
            if (d > 0 and lo <= stop) or (d < 0 and hi >= stop):
                px = stop; break
        if px is None:
            px = CLOSE[min(i + hold, N - 1)]
        gross = (px - entry) / entry * 100.0 * d
        res.append(gross - cost)
    return res

def report(name, v):
    if len(v) < 20:
        print("%-30s 样本 %d（不足）" % (name, len(v))); return None
    m = statistics.mean(v); sd = statistics.pstdev(v)
    t = m/(sd/math.sqrt(len(v))) if sd else 0
    # 集中度：去掉最好的 5% 之后还剩多少
    sv = sorted(v)
    trim = sv[:int(len(sv)*0.95)]
    m_trim = statistics.mean(trim) if trim else 0
    print("%-30s n=%4d  均值 %+.4f%%  t=%5.2f  胜率 %4.1f%%  去最优5%%后 %+.4f%%" % (
        name, len(v), m, t, sum(1 for x in v if x > 0)/len(v)*100, m_trim))
    return m, t, m_trim

for hold in (480, 1440):
    print("=== 持有 %d 分钟 ===" % hold)
    for cost, cname in ((0.10, "吃单进出"), (0.07, "挂单进/吃单出"), (0.04, "全挂单")):
        print(" 成本 %s (%.2f%%)" % (cname, cost))
        report("   训练段", sim(tr_, hold, 1.5, cost, vlo, vhi))
        report("   测试段", sim(te_, hold, 1.5, cost, vlo, vhi))
    print()
