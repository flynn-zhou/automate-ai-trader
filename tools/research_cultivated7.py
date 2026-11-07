# -*- coding: utf-8 -*-
"""最后一关：防守次数 D 是否放大砸穿后的 alpha（剥掉 beta 之后）。

已知：
  - P(砸穿) 随 D 单调上升（上涨段 48h: 8.3% -> 18.1%）
  - 5m 上"砸穿后买入"剥掉 beta 仍有 +0.11% alpha（4 个品种 t>2）
  - 但 +0.11% 对 0.08% 成本太薄

若 alpha 随 D 放大，就可以只用高 D 事件：频率降低、单笔变厚 -> 才可能成策略。
这是用户模型最核心的一环：**被养熟的支撑，砸穿后反弹更凶**（或更弱）。
"""
import sqlite3, sys, math

DB = "/home/cryptobot/ai-trader/data/hist.db"
W = 100
TOL = 0.25
BREAK = 0.10
HOLD = 48
COST = 0.08
NAMES = ("0", "1-2", "3-4", "5-7", "8+")


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


def bucket(d):
    if d <= 0:
        return 0
    if d <= 2:
        return 1
    if d <= 4:
        return 2
    if d <= 7:
        return 3
    return 4


def run(sym, iv):
    rows = load(sym, iv)
    n = len(rows)
    if n < W + HOLD + 300:
        return None
    lo = [r[3] for r in rows]
    cl = [r[4] for r in rows]
    span = (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000)
    uncond = [(cl[i + HOLD] - cl[i]) / cl[i] * 100.0
              for i in range(W, n - HOLD, HOLD)]
    _, mu, _ = ms(uncond)

    groups = {k: [] for k in range(5)}
    last = {k: -10**9 for k in range(5)}
    for i in range(W, n - HOLD):
        base = min(lo[i - W:i])
        if cl[i] >= base * (1 - BREAK / 100.0):
            continue
        edge = base * (1 + TOL / 100.0)
        d = 0
        for j in range(i - W, i):
            if lo[j] <= edge and cl[j] > edge:
                d += 1
        k = bucket(d)
        if i - last[k] < HOLD:          # 每桶内非重叠
            continue
        last[k] = i
        groups[k].append((cl[i + HOLD] - cl[i]) / cl[i] * 100.0)
    out = []
    for k in range(1, 5):
        g = groups[k]
        if len(g) < 100:
            continue
        nn, m, t = ms(g)
        out.append((NAMES[k], nn, nn / span, m, m - mu, m - COST))
    return dict(sym=sym, iv=iv, mu=mu, rows=out)


if __name__ == "__main__":
    print("%-9s %-6s %-5s %6s %10s %10s %11s %10s" % (
        "品种", "周期", "D桶", "笔/年", "砸穿后", "alpha", "扣成本净", "vs成本倍数"))
    print("-" * 82)
    for s in ["BTCUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT"]:
        r = run(s, "5m")
        if not r:
            continue
        print("  (无条件基准 %+.3f%%)" % r["mu"])
        for nm, nn, py_, m, a, net in r["rows"]:
            print("%-9s %-6s %-5s %6.0f %+10.3f%% %+10.3f%% %+11.3f%% %10.2fx" % (
                r["sym"], "5m", nm, py_, m, a, net, m / COST))
        print()
