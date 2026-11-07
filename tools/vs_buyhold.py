# -*- coding: utf-8 -*-
"""诚实的对照：趋势策略 vs 买入并持有；并测波动率目标仓位。

记账顺序必须是：先用【本根持仓】结算收益，再判信号。
搞反了会漏掉每笔出场那根 bar 的下跌——出场是跌破均线时卖，那根 bar 恰恰是跌的，
181 笔累乘下来能虚增 17 倍。这个坑我踩过，写在注释里防止再犯。
"""
import math, os, sys

sys.path.insert(0, os.getcwd())
import tools.backtest as bt

rows = [b for b in bt.load("data/backtest.db", "4h") if b[5] > 0]
C = [b[4] for b in rows]
T = [b[0] for b in rows]
N, NL, FEE = len(C), 200, 0.001
YRS = (T[-1] - T[0]) / (365.25 * 24 * 3600 * 1000)


def stats(curve):
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0)
    tot = curve[-1] / curve[0] - 1
    cagr = (1 + tot) ** (1 / YRS) - 1
    return tot, cagr, mdd, (cagr / mdd if mdd else 0)


def run(lo, hi, vol_target=None, leverage=1.0):
    eq = 1.0
    curve = []
    pos = False
    w_prev = 0.0
    for i in range(max(NL + 1, lo), hi):
        s_now = sum(C[i-NL:i]) / NL
        s_prev = sum(C[i-1-NL:i-1]) / NL
        above, prev = C[i] > s_now, C[i-1] > s_prev

        # 1) 先结算本根持仓收益（进场那根不计，因为是在它的收盘价成交的）
        if pos:
            w = leverage
            if vol_target:
                rets = [math.log(C[k] / C[k-1]) for k in range(i-30, i) if C[k-1] > 0]
                sd = math.sqrt(sum(r*r for r in rets) / len(rets)) if rets else 0.0
                rv = sd * math.sqrt(365 * 6)
                w = leverage * (min(1.0, vol_target / rv) if rv > 1e-9 else 1.0)
                if abs(w - w_prev) > 1e-9:
                    eq *= (1 - abs(w - w_prev) * FEE)      # 调仓成本
                    w_prev = w
            eq *= (1 + w * (C[i] / C[i-1] - 1))

        # 2) 再判信号
        if pos and (not above) and prev:
            eq *= (1 - FEE)
            pos = False
            w_prev = 0.0
        elif not pos and above and not prev:
            eq *= (1 - FEE)
            pos = True
            w_prev = 0.0
        curve.append(eq)
    return curve


def bh_curve():
    c = [1.0]
    for i in range(1, N):
        c.append(c[-1] * (C[i] / C[i-1]))
    c[0] *= (1 - FEE)
    c[-1] *= (1 - FEE)
    return c


full = run(NL + 1, N)
t, cg, md, cm = stats(full)
bh = bh_curve()
bt_, bcg, bmd, bcm = stats(bh)

print("=== 对照（现货 taker 0.1%% 单边，%.2f 年，%d 根 4h）===" % (YRS, N))
print("%-18s %10s %8s %8s %7s" % ("方式", "累计", "年化", "回撤", "卡玛"))
print("%-18s %+9.0f%% %7.1f%% %7.0f%% %7.2f" % ("买入并持有", bt_*100, bcg*100, bmd*100, bcm))
print("%-18s %+9.0f%% %7.1f%% %7.0f%% %7.2f" % ("SMA200 只做多 1x", t*100, cg*100, md*100, cm))

mid = N // 2
print()
print("=== 波动率目标仓位（训练 / 测试各半）===")
print("%-12s %10s %10s %8s %8s" % ("目标波动", "训练段", "测试段", "全期回撤", "全期卡玛"))
for vt in (None, 0.30, 0.40, 0.50, 0.60):
    c1 = run(NL + 1, mid, vol_target=vt)
    c2 = run(mid, N, vol_target=vt)
    ca = run(NL + 1, N, vol_target=vt)
    ta, cga, mda, cma = stats(ca)
    tag = "不设" if vt is None else "%.0f%%" % (vt * 100)
    print("%-12s %+9.0f%% %+9.0f%% %7.0f%% %8.2f"
          % (tag, (c1[-1]/c1[0]-1)*100, (c2[-1]/c2[0]-1)*100, mda*100, cma))
