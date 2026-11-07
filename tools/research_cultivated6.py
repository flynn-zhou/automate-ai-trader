# -*- coding: utf-8 -*-
"""第五关（致命）：把 beta 剥掉，看还剩多少 alpha。

刚才那张表格有 12 个"扣成本后为正"的组合。但先做一个手算：

  BTC 5m, 7.15 年, 752126 根。2019 年 ~4000 美元 -> 2026 年 ~78000 美元，
  约 20 倍 = +1900%。平均每根 5m 收益 = 1900%/752126 ≈ 0.0025%。
  持有 48 根 = 0.12%。

而我测出来的"砸穿后买入 48 根"是 +0.130%。

**几乎完全相等。** 说明这个"边际"就是"长期持有"本身——
在多头的市场里买任何东西都赚，跟"收割模型"毫无关系。

所以必须无条件地先测：同一段行情、同一个持有期，什么都不筛的平均收益是多少。
  alpha = 条件均值 - 无条件均值
只有 alpha 显著为正，才谈得上策略。之前所有"看起来有效"的东西
死在这一关上，这次先跑它。
"""
import sqlite3, sys, math, time

DB = "/home/cryptobot/ai-trader/data/hist.db"
W = 100
TOL = 0.25
BREAK = 0.10
COST = 0.08


def load(sym, iv):
    c = sqlite3.connect(DB)
    r = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
                  (sym, iv)).fetchall()
    c.close()
    return r


def ms(xs):
    n = len(xs)
    if n < 3:
        return n, 0.0, 0.0
    m = sum(xs) / n
    var = sum(x * x for x in xs) / n - m * m
    return n, m, (0.0 if var <= 0 else m / math.sqrt(var / n))


def run(sym, iv, hold):
    rows = load(sym, iv)
    n = len(rows)
    if n < W + hold + 200:
        return None
    lo = [r[3] for r in rows]
    cl = [r[4] for r in rows]
    span = (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000)

    # 无条件基线：同一段行情里，每隔 hold 根取一个不重叠样本
    uncond = [(cl[i + hold] - cl[i]) / cl[i] * 100.0
              for i in range(W, n - hold, hold)]

    ev = [i for i in range(W, n - hold)
          if cl[i] < min(lo[i - W:i]) * (1 - BREAK / 100.0)]
    if len(ev) < 150:
        return None
    keep, last = [], -10**9
    for i in ev:
        if i - last < hold:
            continue
        keep.append(i)
        last = i
    cond = [(cl[i + hold] - cl[i]) / cl[i] * 100.0 for i in keep]

    nu, mu, tu = ms(uncond)
    nc, mc, tc = ms(cond)
    alpha = mc - mu
    # alpha 的 t：两组独立均值的差，用较大方差保守估
    vu = max(1e-12, sum((x - mu) ** 2 for x in uncond) / max(1, nu - 1))
    vc = max(1e-12, sum((x - mc) ** 2 for x in cond) / max(1, nc - 1))
    se = math.sqrt(vu / nu + vc / nc)
    ta = alpha / se if se > 0 else 0.0

    # 近三年同样的对比
    c23 = [(cl[i + hold] - cl[i]) / cl[i] * 100.0 for i in keep
           if time.gmtime(rows[i][0] / 1000.0).tm_year >= 2023]
    u23 = [(cl[i + hold] - cl[i]) / cl[i] * 100.0 for i in range(W, n - hold, hold)
           if time.gmtime(rows[i][0] / 1000.0).tm_year >= 2023]
    _, m23c, _ = ms(c23)
    _, m23u, _ = ms(u23)
    return dict(sym=sym, iv=iv, hold=hold, peryr=nc / span,
                mu=mu, mc=mc, alpha=alpha, ta=ta,
                a23=m23c - m23u, net=mc - COST)


if __name__ == "__main__":
    SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "BNBUSDT"]
    IVS = [("5m", 48), ("15m", 48), ("15m", 96), ("1h", 48), ("1h", 96)]
    print("%-9s %-8s %6s %11s %11s %11s %6s %11s" % (
        "品种", "周期/持有", "笔/年", "无条件(市场)", "砸穿后", "alpha", "t", "2023 alpha"))
    print("-" * 94)
    good = []
    for s in SYMS:
        for iv, h in IVS:
            try:
                r = run(s, iv, h)
            except Exception as e:
                print("%-9s %-8s ERR %s" % (s, iv, str(e)[:26]))
                continue
            if not r:
                continue
            flag = " <<<" if r["alpha"] > 0 and r["ta"] > 2.0 and r["a23"] > 0 else ""
            print("%-9s %-8s %6.0f %+11.3f%% %+11.3f%% %+11.3f%% %6.1f %+11.3f%%%s" % (
                r["sym"], r["iv"] + "/" + str(r["hold"]), r["peryr"],
                r["mu"], r["mc"], r["alpha"], r["ta"], r["a23"], flag))
            if flag:
                good.append(r)
    print()
    print("alpha 显著(t>2)且近三年仍为正的：%d 个" % len(good))
    for g in good:
        print("   %s %s/%d  alpha %+.3f%% (t=%.1f)  笔/年 %.0f"
              % (g["sym"], g["iv"], g["hold"], g["alpha"], g["ta"], g["peryr"]))
