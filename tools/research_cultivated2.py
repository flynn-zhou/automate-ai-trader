# -*- coding: utf-8 -*-
"""收割模型第二关：直接验"养熟了再杀"，并排除趋势混淆。

上一关（research_cultivated.py）在 BTC 5m 上看到：
  买支撑的收益随"被防守次数 D"单调下滑，D>=5 时 t=-3.7（显著为负）。

但那可能只是同义反复——被防守 8 次的位置多半是下跌趋势里的震荡底，
"买它亏钱"就等于"下跌趋势里别做多"。所以在下结论前必须做两件事：

  ① 直接测【砸穿概率】P(未来 K 根内收盘跌破 base) 随 D 的变化。
     这才是"养熟了再杀"的直接证据；收益是间接的。
  ② 把样本按【200 根均线的上下】切成上涨段/下跌段，看 D 的效应是否两段都在。
     只在下跌段存在 -> 我的结论是假的。

同时放宽插针定义（上一版 D>=3 的样本只有 40/32/14，太少）。
"""
import sqlite3, sys, math

DB = "/home/cryptobot/ai-trader/data/hist.db"
W = 100
TOL = 0.25
SWEEP = 0.20          # 放宽（原来 0.35 太严，样本只剩 40）
BREAK = 0.10
HBARS = (24, 48, 96, 192)
KPROB = (12, 24, 48)
MA_N = 200
BUCKETS = ((1, 2), (3, 4), (5, 7), (8, 10**9))


def bname(d):
    for lo, hi in BUCKETS:
        if lo <= d <= hi:
            return "%d-%d" % (lo, hi) if hi < 10**9 else "%d+" % lo
    return None


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
        if var <= 0:
            return 0.0
        return m / math.sqrt(var / self.n)


def load(sym, iv):
    c = sqlite3.connect(DB)
    rows = c.execute(
        "select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
        (sym, iv)).fetchall()
    c.close()
    return rows


def analyze(sym, iv):
    rows = load(sym, iv)
    n = len(rows)
    need = W + max(max(HBARS), max(KPROB)) + MA_N + 10
    if n < need:
        print("%-9s %-4s 数据不足 %d < %d" % (sym, iv, n, need))
        return None
    hi = [r[2] for r in rows]
    lo = [r[3] for r in rows]
    cl = [r[4] for r in rows]

    # 累积和求 200 均线（快）
    pre = [0.0] * (n + 1)
    for i in range(n):
        pre[i + 1] = pre[i] + cl[i]

    fwd = {}
    brk = {}
    sweepv = {}
    for reg in ("up", "down"):
        for bkey in ("1-2", "3-4", "5-7", "8+"):
            sweepv[(reg, bkey)] = 0
            for hz in HBARS:
                fwd[(reg, bkey, hz)] = Acc()
            for kk in KPROB:
                brk[(reg, bkey, kk)] = [0, 0]      # [命中, 总]
    # 无条件基线
    base_brk = {kk: [0, 0] for kk in KPROB}

    rng = range(max(W, MA_N) , n - max(max(HBARS), max(KPROB)))
    for i in rng:
        ma = (pre[i] - pre[i - MA_N]) / MA_N
        reg = "up" if cl[i] > ma else "down"
        base = min(lo[i - W:i])
        edge = base * (1 + TOL / 100.0)
        d = 0
        for j in range(i - W, i):
            if lo[j] <= edge and cl[j] > edge:
                d += 1
        if d < 1:
            continue
        bk = bname(d)
        if bk is None:
            continue
        # 只统计"站在支撑上方"的处境——这才是"买支撑"的人所在的状态
        if cl[i] <= edge:
            continue
        c_i = cl[i]
        sweepv[(reg, bk)] += 1
        for kk in KPROB:
            hit = 0
            for t in range(i + 1, i + 1 + kk):
                if cl[t] < base * (1 - BREAK / 100.0):
                    hit = 1
                    break
            brk[(reg, bk, kk)][0] += hit
            brk[(reg, bk, kk)][1] += 1
            base_brk[kk][0] += hit
            base_brk[kk][1] += 1
        for hz in HBARS:
            fwd[(reg, bk, hz)].add((cl[i + hz] - c_i) / c_i * 100.0)
    return fwd, brk, base_brk, sweepv, n, rows


def report(sym, iv, res):
    if not res:
        return
    fwd, brk, base_brk, sweepv, n, rows = res
    span = (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000)
    print("=" * 104)
    print("%s %s   跨度 %.2f 年   %d 根" % (sym, iv, span, n))
    print("=" * 104)

    print("① 砸穿概率 P(未来 K 根内收盘跌破支撑) —— 养熟了再杀的直接证据")
    print("   基线(无条件): " + "  ".join(
        "%dh %.1f%%" % (kk, base_brk[kk][0] * 100.0 / max(1, base_brk[kk][1])) for kk in KPROB))
    for reg, tag in (("up", "上涨段"), ("down", "下跌段")):
        print("   [%s]  n=%d" % (tag, sum(sweepv[(reg, b)] for b in ("1-2", "3-4", "5-7", "8+"))))
        for bkey in ("1-2", "3-4", "5-7", "8+"):
            parts = []
            for kk in KPROB:
                hit, tot = brk[(reg, bkey, kk)]
                parts.append("%6.1f%%" % (hit * 100.0 / tot) if tot >= 50 else "     -")
            print("     D=%-4s %s" % (bkey, "  ".join(parts)))
    print()

    print("② 买支撑的远期收益（%，含两段行情分别看）")
    print("   %-8s %s" % ("", "  ".join("%dh: 均值/t值" % hz for hz in HBARS)))
    for reg, tag in (("up", "上涨段"), ("down", "下跌段")):
        for bkey in ("1-2", "3-4", "5-7", "8+"):
            a0 = fwd[(reg, bkey, HBARS[-1])]
            if a0.n < 100:
                continue
            cells = []
            for hz in HBARS:
                a = fwd[(reg, bkey, hz)]
                cells.append("%7.3f%%/%4.1f" % (a.mean(), a.t()))
            print("   %s D=%-4s %s" % (tag, bkey, "  ".join(cells)))
    print()


if __name__ == "__main__":
    jobs = [(sys.argv[1], sys.argv[2])] if len(sys.argv) > 2 else [("BTCUSDT", "5m")]
    for sym, iv in jobs:
        report(sym, iv, analyze(sym, iv))
