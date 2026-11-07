# -*- coding: utf-8 -*-
"""扫品种/周期：找"砸穿后接止损"这个原料在哪儿兑现窗口够宽（扣成本为正）。

BTC 5m 已判：机制真、但近三年 +0.061% < 成本 0.08%，净 -0.019%。
用户明确说：品种是手段、周期是手段。所以把所有能拿到的组合扫一遍。

输出每条一行，只留决策需要的数字：
  n23/mean23/net23 = 2023 年起的样本、均值、扣成本净
  后30%净 = 样本外后半段扣成本净
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


def stats(xs):
    n = len(xs)
    if n < 3:
        return n, 0.0, 0.0
    m = sum(xs) / n
    var = sum(x * x for x in xs) / n - m * m
    t = 0.0 if var <= 0 else m / math.sqrt(var / n)
    return n, m, t


def run(sym, iv, hold):
    rows = load(sym, iv)
    n = len(rows)
    if n < W + hold + 200:
        return None
    lo = [r[3] for r in rows]
    cl = [r[4] for r in rows]
    ev = []
    for i in range(W, n - hold):
        base = min(lo[i - W:i])
        if cl[i] >= base * (1 - BREAK / 100.0):
            continue
        ev.append(i)
    if len(ev) < 200:
        return None
    keep = []
    last = -10**9
    for i in ev:
        if i - last < hold:
            continue
        keep.append(i)
        last = i
    allr = [(cl[i + hold] - cl[i]) / cl[i] * 100.0 for i in keep]
    i23 = [i for i in keep if time.gmtime(rows[i][0] / 1000.0).tm_year >= 2023]
    r23 = [(cl[i + hold] - cl[i]) / cl[i] * 100.0 for i in i23]
    cut = int(len(keep) * 0.7)
    r_back = [(cl[i + hold] - cl[i]) / cl[i] * 100.0 for i in keep[cut:]]
    n_all, m_all, t_all = stats(allr)
    n23, m23, t23 = stats(r23)
    nb, mb, tb = stats(r_back)
    span = (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000)
    return dict(sym=sym, iv=iv, hold=hold, n=n_all, peryr=n_all / span,
                mall=m_all, tall=t_all, n23=n23, m23=m23, t23=t23,
                mb=mb, tb=tb, net23=m23 - COST, netb=mb - COST)


if __name__ == "__main__":
    SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "BNBUSDT"]
    IVS = [("5m", 48), ("15m", 48), ("1h", 48), ("15m", 96), ("1h", 96)]
    print("%-9s %-4s %5s %7s %4s %13s %13s %13s" % (
        "品种", "周期", "笔/年", "全期", "t", "2023起均值", "2023净", "后30%净"))
    print("-" * 84)
    hits = []
    for s in SYMS:
        for iv, hold in IVS:
            r = None
            try:
                r = run(s, iv, hold)
            except Exception as e:
                print("%-9s %-4s 错误 %s" % (s, iv, str(e)[:30]))
                continue
            if not r:
                continue
            tag = " <<<" if r["net23"] > 0 and r["netb"] > 0 else ""
            print("%-9s %-4s %5.0f %+7.3f%% %4.1f %+13.3f%% %+13.3f%% %+13.3f%%%s" % (
                r["sym"], r["iv"] + "/" + str(hold), r["peryr"], r["mall"], r["tall"],
                r["m23"], r["net23"], r["netb"], tag))
            if r["net23"] > 0 and r["netb"] > 0:
                hits.append(r)
    print()
    print("扣成本后近三年与样本外都为正的：%d 个" % len(hits))
    for h in hits:
        print("   %s %s/%d  净 %+.3f%% (t=%.1f)  笔/年 %.0f"
              % (h["sym"], h["iv"], h["hold"], h["net23"], h["t23"], h["peryr"]))
