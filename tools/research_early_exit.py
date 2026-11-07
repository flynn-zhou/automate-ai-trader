# -*- coding: utf-8 -*-
"""第 133 轮·第二条：用"被反复防守的支撑"改进唐奇安的出场。

用户 2026-11-02 的模型，套到唐奇安身上是这样一个反直觉命题：

    唐奇安在 1h 破 75 根新低时才出场 —— 那时收割已经发生完毕。
    但如果脚下那块支撑"看起来越安全"（被反复摸到又拉起），
    它坍塦的概率越高（第 132 轮实测：8 次以上 48h 砸穿率 18.1% vs 1-2 次 8.3%）。

    所以应该在【看起来最安全的时候】离场，而不是等它破了才走。

这在优化里叫"反脆弱选择"：把出场条件从"确认破位"改成"过度防守"。
如果它有效，它不但改善收益，还会把最大回撤压下来。

判据（和前几轮同一套，不接受任何一条不达标）：
  逐年为正 / 滚动 2 年为正 / 样本外后 30% 为正 / 参数邻域是高原不是针尖
"""
import sqlite3, time
from math import sqrt

DB = "/home/cryptobot/ai-trader/data/hist.db"
START_EQ, LOT_BTC, MIN_NOTIONAL = 100.0, 0.001, 50.0
N, M = 150, 75


def load(sym, iv):
    c = sqlite3.connect(DB)
    rows = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=?"
                     " order by open_time", (sym, iv)).fetchall()
    fr = c.execute("select ts, rate from funding where symbol=? order by ts", (sym,)).fetchall()
    c.close()
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows], fr)


def funding_between(fr, t0, t1):
    return sum(r for ts, r in fr if t0 <= ts < t1)


def signals(h, l, c, K=None, T=None):
    """唐奇安持仓序列；K/T 为 None 时就是原版（只在破 M 根新低时出场）。"""
    n = len(c)
    st = [0] * n
    pos = 0
    for i in range(max(N, M), n):
        hi = max(h[i - N:i])
        lo = min(l[i - M:i])
        if pos == 0:
            if c[i] > hi:
                pos = 1
        else:
            if c[i] < lo:
                pos = 0                       # 原版出场
            elif K and T:
                sup = min(l[i - K:i])
                if c[i] > sup:
                    edge = sup * 1.005
                    cnt = 0
                    for j in range(i - K, i):
                        if l[j] <= edge and c[j] > edge:
                            cnt += 1
                    if cnt >= T:
                        pos = 0               # 过度防守 -> 提前离场
        st[i] = pos
    return st


def real_account(st, o, t, fr, lev=1.0, fee=0.0005, eq0=START_EQ):
    eq = eq0
    pos, lots, ep, ei = 0, 0, 0.0, 0
    trades = []
    for i in range(len(st) - 1):
        if st[i] == pos:
            continue
        px = o[i + 1]
        if pos != 0:
            notion = lots * LOT_BTC * ep
            pnl = pos * (px - ep) / ep * notion
            f = funding_between(fr, t[ei], t[i + 1]) * notion
            fee_amt = (notion + lots * LOT_BTC * px) * fee
            eq += pnl - f - fee_amt
            trades.append({"pnl": pnl - f - fee_amt, "ts": t[i + 1], "eq": eq,
                           "gross": pos * (px - ep) / ep, "hold_h": (t[i+1]-t[ei])/3600000.0})
            if eq <= 0:
                break
        if st[i] != 0:
            raw = int(eq * lev / px / LOT_BTC)
            notion = raw * LOT_BTC * px
            if raw < 1 or notion < MIN_NOTIONAL:
                pos = 0
                continue
            lots, ep, ei = raw, px, i + 1
        pos = st[i] if lots else 0
    if not trades:
        return None
    run, pk, mdd = eq0, eq0, 0.0
    for x in trades:
        run = x["eq"]
        pk = max(pk, run)
        mdd = max(mdd, (pk - run) / pk if pk > 0 else 1.0)
    yrs = (t[-1] - t[0]) / (365.25 * 24 * 3600 * 1000)
    return dict(n=len(trades), final=eq, ret=(eq / eq0 - 1) * 100, mdd=mdd * 100,
                yrs=yrs, cagr=((eq / eq0) ** (1 / yrs) - 1) * 100 if eq > 0 else -100,
                per_year=len(trades) / yrs, trades=trades)


def yearly(res):
    ys = {}
    for x in res["trades"]:
        y = time.gmtime(x["ts"] / 1000.0).tm_year
        a = ys.setdefault(y, [0, 0.0])
        a[0] += 1
        a[1] += x["gross"]
    return ys


def main():
    sym, iv = "BTCUSDT", "1h"
    t, o, h, l, c, fr = load(sym, iv)
    print("%s %s %d 根  %s ~ %s" % (sym, iv, len(c),
          time.strftime("%Y-%m-%d", time.gmtime(t[0] / 1000)),
          time.strftime("%Y-%m-%d", time.gmtime(t[-1] / 1000))))

    base_st = signals(h, l, c)
    b = real_account(base_st, o, t, fr)
    print()
    print("=" * 104)
    print("基线：唐奇安 150/75 原版（破 75 根新低才出场）")
    print("=" * 104)
    print("  笔数 %d（%.1f/年）  终值 %.2fU  收益 %+.1f%%  年化 %+.1f%%  MDD %.1f%%"
          % (b["n"], b["per_year"], b["final"], b["ret"], b["cagr"], b["mdd"]))
    yb = yearly(b)
    print("  逐年: " + "  ".join("%d:%+.1f%%(%d)" % (y, yb[y][1], yb[y][0]) for y in sorted(yb)))
    per_b = sum(x["gross"] for x in b["trades"]) / b["n"] * 100
    print("  单笔：均值 %+.3f%%  持仓中位数 %.0f 小时"
          % (per_b, sorted(x["hold_h"] for x in b["trades"])[len(b["trades"]) // 2]))

    print()
    print("=" * 104)
    print("变体：脚下的支撑被摸过 T 次以上 -> 提前离场（不等破位）")
    print("=" * 104)
    print("%-6s %-6s %6s %10s %10s %8s %10s %10s"
          % ("K(根)", "T(次)", "笔数", "终值U", "收益%", "MDD%", "单笔%", "vs基线"))
    rows = []
    for K in (24, 48, 72):
        for T in (2, 3, 4, 6, 8, 12):
            st = signals(h, l, c, K, T)
            r = real_account(st, o, t, fr)
            if not r:
                continue
            per = sum(x["gross"] for x in r["trades"]) / r["n"] * 100
            rows.append(dict(K=K, T=T, r=r, per=per))
            print("%-6d %-6d %6d %10.2f %+10.1f %8.1f %+10.3f %+9.1f%%"
                  % (K, T, r["n"], r["final"], r["ret"], r["mdd"], per, r["ret"] - b["ret"]))
    print()
    best = max(rows, key=lambda x: x["r"]["final"])
    print("最好的格：K=%d T=%d  终值 %.2fU  收益 %+.1f%%  MDD %.1f%%  笔数 %d"
          % (best["K"], best["T"], best["r"]["final"], best["r"]["ret"],
             best["r"]["mdd"], best["r"]["n"]))
    yb2 = yearly(best["r"])
    print("  逐年: " + "  ".join("%d:%+.1f%%(%d)" % (y, yb2[y][1], yb2[y][0]) for y in sorted(yb2)))
    print("  （基线逐年对照）")
    print("        " + "  ".join("%d:%+.1f%%" % (y, yb[y][1]) for y in sorted(yb)))


if __name__ == "__main__":
    main()
