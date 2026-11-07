# -*- coding: utf-8 -*-
"""被反复防守的位置 = 止损堆的所在地。收割者在这里下手。

用户 2026-11-02 指出的模型（我上一轮漏掉的关键一环）：

  "一波上涨后，在一个支撑，每次到了就拉起，像我们这种机器可能觉得
   这里是真支撑，入场，然后来一波大的，'我们'这样的机器就被收割了"

我上一轮测的"扫单"只是"跌破 20 根新低后收回"——但【刚创出来的新低】
和【被反复防守过 N 次的位置】完全是两件事。后者才积累了止损堆。
漏掉防守次数这个变量，等于没测这个模型。

本脚本把"防守次数 D"作为核心变量：
  base  = 过去 W 根的最低
  D     = 过去 W 根里"最低价摸到 base 附近、但收盘站回上方"的次数
          （真正被守住的次数；一旦破位 base 会下移，D 自然归零）

然后在 D 的分桶里看三类事件的未来收益：
  A 支撑确认（碰了、收回、但没插针）—— 这是"机器以为的真支撑"，应该亏
  B 插针收回（最低跌破 base 再收回）—— 跟着收割者买，应该赚
  C 决定性砸穿（收盘跌破 base）—— 跟着砸，应该赚

判据：如果用户模型成立，B 和 C 的收益应随 D 单调【上升】，A 应【下降】。
"""
import sqlite3, sys, math

DB = "/home/cryptobot/ai-trader/data/hist.db"
W = 100                 # 回看窗口（根）
TOL = 0.25              # "摸到 base 附近"的容差（%）
SWEEP = 0.35            # 插针深度（%）
BREAK = 0.10            # 收盘跌破幅度（%）
HORIZONS = (6, 12, 24, 48)
COST_MAKER_PCT = 0.04   # 单边 maker（%），永续+BNB 抵扣
COST_TAKER_PCT = 0.05

BUCKETS = ((1, 2), (3, 4), (5, 7), (8, 10**9))


def bname(d):
    for lo, hi in BUCKETS:
        if lo <= d <= hi:
            return "%d-%d" % (lo, hi) if hi < 10**9 else "%d+" % lo
    return "-"


def load(sym, iv):
    c = sqlite3.connect(DB)
    rows = c.execute(
        "select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
        (sym, iv)).fetchall()
    c.close()
    return rows


class Acc(object):
    __slots__ = ("n", "s", "s2", "big")

    def __init__(self):
        self.n = 0
        self.s = 0.0
        self.s2 = 0.0
        self.big = 0

    def add(self, x, is_big):
        self.n += 1
        self.s += x
        self.s2 += x * x
        if is_big:
            self.big += 1

    def mean(self):
        return self.s / self.n if self.n else 0.0

    def t(self):
        if self.n < 3:
            return 0.0
        m = self.mean()
        var = self.s2 / self.n - m * m
        if var <= 0:
            return 0.0
        return m / math.sqrt(var / self.n)


def analyze(sym, iv):
    rows = load(sym, iv)
    n = len(rows)
    if n < W + max(HORIZONS) + 100:
        print("%-9s %-4s 数据不足" % (sym, iv))
        return None
    op = [r[1] for r in rows]
    hi = [r[2] for r in rows]
    lo = [r[3] for r in rows]
    cl = [r[4] for r in rows]
    maxh = max(HORIZONS)

    # (kind, bucket) -> horizon -> Acc
    st = {}
    events = {}
    for kind in ("A", "B", "C"):
        for bkey in ("1-2", "3-4", "5-7", "8+"):
            events[(kind, bkey)] = 0
            for hz in HORIZONS:
                st[(kind, bkey, hz)] = Acc()

    for i in range(W, n - maxh):
        base = min(lo[i - W:i])
        edge = base * (1 + TOL / 100.0)
        d = 0
        for j in range(i - W, i):
            if lo[j] <= edge and cl[j] > edge:
                d += 1
        if d < 1:
            continue
        bk = bname(d)
        l_i = lo[i]
        c_i = cl[i]
        # 分类
        if c_i < base * (1 - BREAK / 100.0):
            kind = "C"
        elif l_i < base * (1 - SWEEP / 100.0) and c_i > edge:
            kind = "B"
        elif l_i <= edge and c_i > edge:
            kind = "A"
        else:
            continue
        events[(kind, bk)] += 1
        for hz in HORIZONS:
            r = (cl[i + hz] - c_i) / c_i * 100.0
            if kind == "C":
                r = -r                      # C 是做空，收益取反
            st[(kind, bk, hz)].add(r, abs(r) > 2.0)
    return st, events, n, rows


def report(sym, iv, res):
    if not res:
        return
    st, events, n, rows = res
    span_y = (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000)
    print("=" * 100)
    print("%s %s   跨度 %.2f 年   %d 根" % (sym, iv, span_y, n))
    print("=" * 100)
    names = {"A": "A 支撑确认(不插针)", "B": "B 插针收回(跟买)",
             "C": "C 收盘砸穿(跟空)"}
    for kind in ("A", "B", "C"):
        print("  %s" % names[kind])
        print("    %-6s %8s %s" % ("防守次", "样本", "  ".join(
            "%dh:%9s" % (hz, "收益/t") for hz in HORIZONS)))
        for bkey in ("1-2", "3-4", "5-7", "8+"):
            ev = events[(kind, bkey)]
            if ev == 0:
                continue
            cells = []
            for hz in HORIZONS:
                a = st[(kind, bkey, hz)]
                if a.n < 30:
                    cells.append("%12s" % "-")
                else:
                    cells.append("%7.3f%%/%.1f" % (a.mean(), a.t()))
            print("    %-6s %8d %s" % (bkey, ev, "  ".join(cells)))
        print()
    # 成本线提示
    print("  [成本] maker 往返 %.2f%% / taker 往返 %.2f%%"
          % (COST_MAKER_PCT * 2, COST_TAKER_PCT * 2))
    print()


if __name__ == "__main__":
    if len(sys.argv) > 2:
        jobs = [(sys.argv[1], sys.argv[2])]
    else:
        jobs = [("BTCUSDT", "5m")]
    for sym, iv in jobs:
        report(sym, iv, analyze(sym, iv))
