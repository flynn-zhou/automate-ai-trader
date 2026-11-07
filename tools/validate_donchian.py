# -*- coding: utf-8 -*-
"""donchian(150/75) 上线前的严格验收——用杀掉剥头皮的那套标准来杀它。

五道关，缺一不可：
  1. 逐年拆：每一年都必须为正（不是靠某一年）
  2. 滚动窗口：每 2 年一段，段段为正
  3. 样本外：前 70% 拟合 / 后 30% 验证
  4. 参数邻域：整片高原，不是针尖
  5. 100U 真实账户：整数手(0.001 BTC)、最小名义 50U、资金费、1x

第 5 关最关键：一个在「满仓复利」里年化 30% 的策略，
放到 100U、只能下 1 手、还要付资金费的账户里，可能什么都剩不下。
"""
import sqlite3
import time
from math import sqrt

DB = "/home/cryptobot/ai-trader/data/hist.db"
START_EQ, LOT_BTC, MIN_NOTIONAL = 100.0, 0.001, 50.0


def load(sym, iv):
    c = sqlite3.connect(DB)
    tbl = "k2"
    rows = c.execute("select open_time,o,h,l,c from %s where symbol=? and interval=?"
                     " order by open_time" % tbl, (sym, iv)).fetchall()
    fr = c.execute("select ts, rate from funding where symbol=? order by ts", (sym,)).fetchall()
    c.close()
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows], fr)


def donchian_signal(h, l, c, N, M):
    st = [0] * len(c)
    pos = 0
    for i in range(max(N, M), len(c)):
        hi = max(h[i-N:i])
        lo = min(l[i-M:i])
        if pos == 0 and c[i] > hi:
            pos = 1
        elif pos == 1 and c[i] < lo:
            pos = 0
        st[i] = pos
    return st


def funding_between(fr, t0, t1):
    return sum(r for ts, r in fr if t0 <= ts < t1)


def simple_trades(st, o, t, fr, fee_side=0.0005):
    """每笔按「满仓百分比」计，用于逐年/滚动分析。"""
    out = []
    pos, ep, ei = 0, 0.0, 0
    for i in range(len(st) - 1):
        if st[i] == pos:
            continue
        px = o[i+1]
        if pos != 0:
            g = pos * (px - ep) / ep
            f = funding_between(fr, t[ei], t[i+1]) * pos
            cost = 2 * fee_side
            out.append({"i": i+1, "ts": t[i+1], "net": g - f - cost,
                        "gross": g, "fund": f, "cost": cost})
        if st[i] != 0:
            ep, ei = px, i+1
        pos = st[i]
    return out


def real_account(st, o, t, fr, lev=1.0, fee=0.0005, eq0=START_EQ):
    eq = eq0
    pos, lots, ep, ei = 0, 0, 0.0, 0
    trades = []
    for i in range(len(st) - 1):
        if st[i] == pos:
            continue
        px = o[i+1]
        if pos != 0:
            notion = lots * LOT_BTC * ep
            pnl = pos * (px - ep) / ep * notion
            f = funding_between(fr, t[ei], t[i+1]) * notion
            fee_amt = (notion + lots * LOT_BTC * px) * fee
            eq += pnl - f - fee_amt
            trades.append({"pnl": pnl - f - fee_amt, "ts": t[i+1], "eq": eq})
            if eq <= 0:
                break
        if st[i] != 0:
            raw = int(eq * lev / px / LOT_BTC)
            notion = raw * LOT_BTC * px
            if raw < 1 or notion < MIN_NOTIONAL:
                pos = 0
                continue
            lots, ep, ei = raw, px, i+1
        pos = st[i] if lots else 0
    if not trades:
        return None
    run, pk, mdd = eq0, eq0, 0.0
    for x in trades:
        run = x["eq"]
        pk = max(pk, run)
        mdd = max(mdd, (pk - run) / pk if pk > 0 else 1.0)
    yrs = (t[-1] - t[0]) / (365.25 * 24 * 3600 * 1000)
    return {"n": len(trades), "final": eq, "ret": (eq/eq0-1)*100, "mdd": mdd*100,
            "yrs": yrs, "cagr": ((eq/eq0)**(1/yrs)-1)*100 if eq > 0 and yrs > 0 else -100,
            "per_year": len(trades)/yrs}


def show(title):
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def main():
    sym, iv, N, M = "BTCUSDT", "1h", 150, 75
    t, o, h, l, c, fr = load(sym, iv)
    print("%s %s  %d 根  %s ~ %s  资金费 %d 条"
          % (sym, iv, len(c), time.strftime("%Y-%m-%d", time.gmtime(t[0]/1000)),
             time.strftime("%Y-%m-%d", time.gmtime(t[-1]/1000)), len(fr)))
    st = donchian_signal(h, l, c, N, M)
    tr = simple_trades(st, o, t, fr)
    print("信号/交易：%d 笔，%.1f 笔/年" % (len(tr), len(tr) / ((t[-1]-t[0])/(365.25*24*3600*1000))))

    show("第 1 关：逐年拆解（每笔满仓百分比，含资金费与双边费）")
    by = {}
    for x in tr:
        by.setdefault(time.gmtime(x["ts"]/1000).tm_year, []).append(x["net"])
    all_pos = True
    for y in sorted(by):
        v = by[y]
        s = sum(v)
        print("  %d  n=%3d  年度合计 %+8.2f%%  平均/笔 %+7.3f%%  %s"
              % (y, len(v), s*100, sum(v)/len(v)*100, "正" if s > 0 else "**负**"))
        if s <= 0:
            all_pos = False
    print("  -> 每一年都为正：%s" % ("是" if all_pos else "否"))

    show("第 2 关：滚动 2 年窗口（每段独立求和）")
    import datetime as dt
    y0 = time.gmtime(t[0]/1000).tm_year
    y1 = time.gmtime(t[-1]/1000).tm_year
    ok = 0
    tot = 0
    for ys in range(y0, y1):
        seg = [x for x in tr if ys <= time.gmtime(x["ts"]/1000).tm_year < ys+2]
        if len(seg) < 3:
            continue
        s = sum(x["net"] for x in seg)
        tot += 1
        if s > 0:
            ok += 1
        print("  %d-%d  笔数 %3d  合计 %+8.2f%%  %s" % (ys, ys+1, len(seg), s*100,
                                                      "正" if s > 0 else "**负**"))
    print("  -> 正的滚动窗口 %d/%d" % (ok, tot))

    show("第 3 关：样本外（前 70% / 后 30%）")
    cut = int(len(c) * 0.7)
    for tag, s, e in (("前70%", 0, cut), ("后30%", cut, len(c))):
        sub = [x for x in tr if s <= x["i"] < e]
        if not sub:
            continue
        nn = len(sub)
        avg = sum(x["net"] for x in sub) / nn
        sd = sqrt(sum((x["net"]-avg)**2 for x in sub)/nn) if nn > 1 else 0
        print("  %s %s ~ %s  笔数 %3d  平均/笔 %+7.3f%%  t=%+5.2f  合计 %+8.2f%%"
              % (tag, time.strftime("%Y-%m-%d", time.gmtime(t[s]/1000)),
                 time.strftime("%Y-%m-%d", time.gmtime(t[e-1]/1000)),
                 nn, avg*100, (avg/(sd/sqrt(nn))) if sd > 0 else 0,
                 sum(x["net"] for x in sub)*100))

    show("第 4 关：参数邻域")
    print("  %-18s %-7s %-9s %-11s %-9s" % ("参数", "笔/年", "平均/笔", "逐年是否全正", "滚动正窗"))
    for NN, MM in ((100, 50), (120, 60), (140, 70), (150, 75), (160, 80), (180, 90), (200, 100)):
        s2 = donchian_signal(h, l, c, NN, MM)
        t2 = simple_trades(s2, o, t, fr)
        if not t2:
            continue
        yrs = (t[-1]-t[0])/(365.25*24*3600*1000)
        byy = {}
        for x in t2:
            byy.setdefault(time.gmtime(x["ts"]/1000).tm_year, []).append(x["net"])
        allyr = all(sum(v) > 0 for v in byy.values())
        okc = 0
        tt = 0
        for ys in range(y0, y1):
            seg = [x for x in t2 if ys <= time.gmtime(x["ts"]/1000).tm_year < ys+2]
            if len(seg) < 3:
                continue
            tt += 1
            if sum(x["net"] for x in seg) > 0:
                okc += 1
        print("  donchian(%d/%d)%-5s %6.1f %+9.3f%%  %-11s %d/%d"
              % (NN, MM, "", len(t2)/yrs, sum(x["net"] for x in t2)/len(t2)*100,
                 "全正" if allyr else "**有负年**", okc, tt))

    show("第 5 关：100U 真实账户（整数手 0.001 BTC / 最小名义 50U / 资金费 / 1x）")
    for lev in (1.0, 2.0):
        r = real_account(st, o, t, fr, lev=lev)
        if r:
            print("  %.0fx  %.1f 年  最终 %7.2f U  总收益 %+8.1f%%  年化 %+7.2f%%  最大回撤 %5.1f%%  %.1f 笔/年"
                  % (lev, r["yrs"], r["final"], r["ret"], r["cagr"], r["mdd"], r["per_year"]))

    show("成本拆解：策略赚的钱里，多少被费用和资金费吃掉")
    gross = sum(x["gross"] for x in tr)
    cost = sum(x["cost"] for x in tr)
    fund = sum(x["fund"] for x in tr)
    print("  毛收益合计  %+9.2f%%" % (gross*100))
    print("  手续费      %+9.2f%%  (占毛收益 %.1f%%)" % (-cost*100, cost/gross*100))
    print("  资金费      %+9.2f%%  (占毛收益 %.1f%%)" % (-fund*100, fund/gross*100))
    print("  净收益      %+9.2f%%" % ((gross-cost-fund)*100))


if __name__ == "__main__":
    main()
