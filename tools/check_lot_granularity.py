# -*- coding: utf-8 -*-
"""致命检查：回测从 2019 年跑起，那时 BTC 才 1 万美元。

    一手 0.001 BTC = 10 U   -> 100U 账户有 10 个档位，粒度几乎不是约束
    今天 BTC 78330，一手 = 78.33 U -> 100U 账户只有 1 个档位

所以「100U 跑出 +718%」这个数字，可能是靠「早期 BTC 便宜、可以精细加仓」
撑起来的。今天起步的话完全不是同一件事。必须单独验。
"""
import sqlite3
import time
import json
from math import sqrt

DB = "/home/cryptobot/ai-trader/data/hist.db"
LOT_BTC = 0.001
MIN_NOTIONAL = 50.0
START_EQ = 100.0


def load(sym, iv):
    c = sqlite3.connect(DB)
    rows = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=?"
                     " order by open_time", (sym, iv)).fetchall()
    fr = c.execute("select ts, rate from funding where symbol=? order by ts", (sym,)).fetchall()
    c.close()
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows], fr)


def don(h, l, c, N, M):
    st = [0]*len(c)
    pos = 0
    for i in range(max(N, M), len(c)):
        if pos == 0 and c[i] > max(h[i-N:i]):
            pos = 1
        elif pos == 1 and c[i] < min(l[i-M:i]):
            pos = 0
        st[i] = pos
    return st


def fund_between(fr, a, b):
    return sum(r for ts, r in fr if a <= ts < b)


def sim(st, o, t, fr, lot_price, lev=1.0, fee=0.0005, eq0=START_EQ):
    """lot_price = 计算档位大小时假设的币价。用来模拟「今天起步」。"""
    eq = eq0
    pos, lots, ep, ei = 0, 0, 0.0, 0
    trades = []
    for i in range(len(st)-1):
        if st[i] == pos:
            continue
        px = o[i+1]
        if pos != 0:
            notion = lots * LOT_BTC * ep
            pnl = pos*(px-ep)/ep*notion
            f = fund_between(fr, t[ei], t[i+1])*notion
            fe = (notion + lots*LOT_BTC*px)*fee
            eq += pnl - f - fe
            trades.append({"pnl": pnl-f-fe, "eq": eq, "ret": pos*(px-ep)/ep,
                           "ts": t[i+1]})
            if eq <= 0:
                break
        if st[i] != 0:
            # 用「今天的币价」估一手要多少钱，看 100U 能买几手
            lot_notional = LOT_BTC * lot_price
            afford = int(eq * lev / lot_notional)
            if afford < 1:
                pos = 0
                lots = 0
                continue
            notion = afford * lot_notional
            if notion < MIN_NOTIONAL:
                pos = 0
                lots = 0
                continue
            lots, ep, ei = afford, px, i+1
        pos = st[i] if lots else 0
    return trades, eq


def analyze(trades, eq0=START_EQ):
    if not trades:
        return None
    pk = eq0
    mdd = 0.0
    for x in trades:
        pk = max(pk, x["eq"])
        mdd = max(mdd, (pk-x["eq"])/pk if pk > 0 else 1)
    rets = sorted(x["ret"] for x in trades)
    n = len(rets)
    return {"n": n, "final": trades[-1]["eq"], "mdd": mdd*100,
            "worst": rets[0]*100, "p5": rets[int(n*0.05)]*100,
            "median": rets[n//2]*100, "best": rets[-1]*100,
            "avg": sum(rets)/n*100,
            "win": sum(1 for r in rets if r > 0)/n*100}


def main():
    t, o, h, l, c, fr = load("BTCUSDT", "1h")
    st = don(h, l, c, 150, 75)
    print("donchian(150/75) 1h   每笔收益分布（满仓百分比）")
    tr0, _ = sim(st, o, t, fr, lot_price=10000.0)   # 早期廉价币价 = 无粒度约束
    a0 = analyze(tr0)
    print("  笔数 %d  平均 %+.2f%%  中位 %+.2f%%  最差 %.1f%%  P5 %.1f%%  最好 %+.1f%%  胜率 %.0f%%"
          % (a0["n"], a0["avg"], a0["median"], a0["worst"], a0["p5"], a0["best"], a0["win"]))

    print()
    print("=" * 92)
    print("关键对比：同样的策略、同样的历史，只改「一手值多少钱」")
    print("=" * 92)
    print("  %-28s %-7s %-9s %-9s %-9s %-8s" %
          ("假设的一手币价", "起始权益", "最终权益", "最大回撤", "笔数", "是否爆掉"))
    for lot_price, label in ((10000.0, "1 万（2019 真实）"), (30000.0, "3 万"),
                             (60000.0, "6 万"), (78330.0, "**78330（今天）**")):
        for eq0 in (100.0, 500.0, 1000.0):
            tr, fin = sim(st, o, t, fr, lot_price, eq0=eq0)
            a = analyze(tr, eq0)
            if not a:
                continue
            print("  %-28s %7.0f %9.2f %8.1f%% %8d  %s"
                  % (label if eq0 == 100.0 else "", eq0, fin, a["mdd"], a["n"],
                     "是" if fin < eq0 * 0.05 else ""))

    print()
    print("=" * 92)
    print("今天起步（一手 78.33U，100U 账户）：连续亏损会怎样")
    print("=" * 92)
    tr, fin = sim(st, o, t, fr, 78330.0, eq0=100.0)
    cum = 100.0
    seq = 0
    worst_run = 0
    cur = 0
    for x in tr:
        if x["pnl"] < 0:
            cur += 1
            worst_run = max(worst_run, cur)
        else:
            cur = 0
    print("  历史最长连亏 %.0f 笔" % worst_run)
    print("  都按「一手满仓」打：每笔亏 10%% 就是 %.2f U" % (78.33*0.10))
    eq = 100.0
    print("  逐笔推演最坏情况：")
    for k in range(1, 5):
        loss = 78.33 * 0.10
        eq -= loss
        can = "能" if eq >= 78.33 else "**开不出仓**"
        print("    第 %d 笔亏 10%% 后：%.2f U  -> %s" % (k, eq, can))
        if eq < 78.33:
            break


if __name__ == "__main__":
    main()
