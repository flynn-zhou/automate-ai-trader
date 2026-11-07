# -*- coding: utf-8 -*-
"""收割模型第三关：砸穿那一刻之后，到底该做哪边；以及扫品种/周期。

前两关的结论：
  - P(砸穿) 随"被防守次数 D"单调上升，上涨段 8.3%->18.1%（48h），2.2 倍
  - "买支撑"的收益 D=3-4 最好，D>=8 在下跌段显著为负（t=-10.0）
  - 但"砸穿后跟着做空"全线亏钱 —— 说明砸穿后价格反弹

这一关只问一件事：**砸穿（收盘跌破 base）之后，未来收益是哪边。**
按 D 分桶、按行情分段，并给出与成本线的对比。
顺带扫多品种/多周期，看边际在哪儿最大。
"""
import sqlite3, sys, math

DB = "/home/cryptobot/ai-trader/data/hist.db"
W = 100
TOL = 0.25
BREAK = 0.10
MA_N = 200
HOR = (24, 48, 96)
BUCKETS = ((1, 2), (3, 4), (5, 7), (8, 10**9))
NAMES = ("1-2", "3-4", "5-7", "8+")
COST = 0.04          # 单边 maker %


class Acc(object):
    __slots__ = ("n", "s", "s2")

    def __init__(self):
        self.n = 0
        self.s = 0.0
        self.s2 = 0.0

    def add(self, x):
        self.n += 1
        self.s += x
        self.s2 += x * x

    def mean(self):
        return self.s / self.n if self.n else 0.0

    def t(self):
        if self.n < 5:
            return 0.0
        m = self.mean()
        var = self.s2 / self.n - m * m
        return 0.0 if var <= 0 else m / math.sqrt(var / self.n)


def load(sym, iv):
    c = sqlite3.connect(DB)
    r = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
                  (sym, iv)).fetchall()
    c.close()
    return r


def run(sym, iv):
    rows = load(sym, iv)
    n = len(rows)
    if n < W + MA_N + max(HOR) + 10:
        print("%-9s %-4s 数据不足" % (sym, iv))
        return
    hi = [r[2] for r in rows]
    lo = [r[3] for r in rows]
    cl = [r[4] for r in rows]
    pre = [0.0] * (n + 1)
    for i in range(n):
        pre[i + 1] = pre[i] + cl[i]

    acc = {}
    cnt = {}
    for reg in ("all", "up", "down"):
        for bi, bn in enumerate(NAMES):
            cnt[(reg, bn)] = 0
            for hz in HOR:
                acc[(reg, bn, hz)] = Acc()
    # 基线：所有砸穿事件混在一起
    base_acc = {hz: Acc() for hz in HOR}
    base_n = 0

    for i in range(max(W, MA_N), n - max(HOR)):
        ma = (pre[i] - pre[i - MA_N]) / MA_N
        reg = "up" if cl[i] > ma else "down"
        base = min(lo[i - W:i])
        if cl[i] >= base * (1 - BREAK / 100.0):
            continue                      # 只看真的砸穿了的那一根
        edge = base * (1 + TOL / 100.0)
        d = 0
        for j in range(i - W, i):
            if lo[j] <= edge and cl[j] > edge:
                d += 1
        bn = None
        for k, (a, b) in enumerate(BUCKETS):
            if a <= d <= b:
                bn = NAMES[k]
                break
        if bn is None:
            continue
        c_i = cl[i]
        base_n += 1
        for hz in HOR:
            r = (cl[i + hz] - c_i) / c_i * 100.0
            base_acc[hz].add(r)
            acc[(reg, bn, hz)].add(r)
            acc[("all", bn, hz)].add(r)
        cnt[(reg, bn)] += 1
        cnt[("all", bn)] += 1

    span = (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000)
    print("=" * 96)
    print("%s %s  %.2f 年  砸穿事件总数 %d（%.1f 次/年）" % (sym, iv, span, base_n, base_n / span))
    print("  基线（不分 D）：" + "  ".join(
        "%dh %+0.3f%%/t%.1f" % (hz, base_acc[hz].mean(), base_acc[hz].t()) for hz in HOR))
    print("  注：收益为【正=做多赚】，成本 maker 往返 %.2f%%" % (COST * 2))
    for reg, tag in (("up", "上涨段"), ("down", "下跌段")):
        print("  [%s]" % tag)
        for bn in NAMES:
            if cnt[(reg, bn)] < 100:
                continue
            cells = []
            for hz in HOR:
                a = acc[(reg, bn, hz)]
                cells.append("%+7.3f%%/t%4.1f" % (a.mean(), a.t()))
            print("     D=%-4s n=%-6d %s" % (bn, cnt[(reg, bn)], "  ".join(cells)))
    print()


if __name__ == "__main__":
    jobs = [(sys.argv[1], sys.argv[2])] if len(sys.argv) > 2 else [("BTCUSDT", "5m")]
    for s, v in jobs:
        run(s, v)
