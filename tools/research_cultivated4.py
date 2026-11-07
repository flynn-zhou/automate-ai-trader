# -*- coding: utf-8 -*-
"""收割模型第四关（决定性）：逐年 + 样本外。这是判生死的一关。

第三关看到"收盘砸穿 100 根低点后买入"在 BTC 5m 上 48h 给 +0.227%（t=7.0），
605.9 次/年，成本 0.08%。看起来很好。

但我上一轮已经有类似的东西死在这一关上：扫单、连跌3根做多，
全期数字漂亮，样本外归零。所以先别高兴。

这一关只做一件事：把这个效应按【年】拆开，再切成前 70% / 后 30%。
如果近 3 年是负的，那就是同一个死人，直接埋掉。

同时做非重叠采样（hold 48 根，事件间隔 < 48 根就跳过），
因为重叠样本会把 t 值吹大——606 次/年、持有 48 根，重叠度约 5 倍。
"""
import sqlite3, sys, math

DB = "/home/cryptobot/ai-trader/data/hist.db"
W = 100
TOL = 0.25
BREAK = 0.10
HOR = 48
COST = 0.08          # maker 往返 %


def load(sym, iv):
    c = sqlite3.connect(DB)
    r = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
                  (sym, iv)).fetchall()
    c.close()
    return r


def year_of(ts):
    import time
    return time.gmtime(ts / 1000.0).tm_year


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
        if self.n < 3:
            return 0.0
        m = self.mean()
        var = self.s2 / self.n - m * m
        return 0.0 if var <= 0 else m / math.sqrt(var / self.n)


def run(sym, iv):
    rows = load(sym, iv)
    n = len(rows)
    if n < W + HOR + 200:
        print("%-9s %-4s 数据不足" % (sym, iv))
        return
    lo = [r[3] for r in rows]
    cl = [r[4] for r in rows]

    events = []          # (idx, year, d)
    for i in range(W, n - HOR):
        base = min(lo[i - W:i])
        if cl[i] >= base * (1 - BREAK / 100.0):
            continue
        edge = base * (1 + TOL / 100.0)
        d = 0
        for j in range(i - W, i):
            if lo[j] <= edge and cl[j] > edge:
                d += 1
        events.append((i, year_of(rows[i][0]), d))

    if not events:
        print("%s %s 无事件" % (sym, iv))
        return

    # 非重叠采样
    keep = []
    last = -10**9
    for i, y, d in events:
        if i - last < HOR:
            continue
        keep.append((i, y, d))
        last = i

    print("=" * 92)
    print("%s %s  事件 %d，非重叠后 %d（%.1f 次/年）"
          % (sym, iv, len(events), len(keep),
             len(keep) / ((rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000))))
    # 逐年
    yrs = {}
    for i, y, d in keep:
        r = (cl[i + HOR] - cl[i]) / cl[i] * 100.0
        yrs.setdefault(y, Acc()).add(r)
    print("  逐年（持有 %d 根，非重叠）：" % HOR)
    line = []
    for y in sorted(yrs):
        a = yrs[y]
        if a.n < 5:
            continue
        line.append("%d:%+.3f%%(n=%d)" % (y, a.mean(), a.n))
    for k in range(0, len(line), 3):
        print("    " + "  ".join(line[k:k + 3]))
    pos = sum(1 for y in sorted(yrs) if yrs[y].n >= 5 and yrs[y].mean() > 0)
    tot = sum(1 for y in sorted(yrs) if yrs[y].n >= 5)
    print("    正年数 %d/%d" % (pos, tot))

    # 样本外
    cut = int(len(keep) * 0.7)
    for tag, part in (("前 70%%", keep[:cut]), ("后 30%%", keep[cut:])):
        a = Acc()
        for i, y, d in part:
            a.add((cl[i + HOR] - cl[i]) / cl[i] * 100.0)
        print("    样本外 %-8s n=%-5d 均值 %+0.3f%%  t=%4.1f  扣成本净 %+0.3f%%"
              % (tag, a.n, a.mean(), a.t(), a.mean() - COST))
    # 近三年
    a3 = Acc()
    for i, y, d in keep:
        if y >= 2023:
            a3.add((cl[i + HOR] - cl[i]) / cl[i] * 100.0)
    if a3.n:
        print("    2023 年起        n=%-5d 均值 %+0.3f%%  t=%4.1f  扣成本净 %+0.3f%%"
              % (a3.n, a3.mean(), a3.t(), a3.mean() - COST))
    print()


if __name__ == "__main__":
    jobs = [tuple(sys.argv[1].split(":"))] if len(sys.argv) > 1 else [("BTCUSDT", "5m")]
    for s, v in jobs:
        run(s, v)
