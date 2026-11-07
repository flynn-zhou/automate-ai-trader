# -*- coding: utf-8 -*-
"""上线前的最后三个决策：停损放哪、要不要做空、仓位怎么定。

不做这三个决策就写代码，等于把回测里没验过的东西塞进实盘。
"""
import sqlite3
import time
from math import sqrt

DB = "/home/cryptobot/ai-trader/data/hist.db"


def load(sym, iv="1h"):
    c = sqlite3.connect(DB)
    rows = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=?"
                     " order by open_time", (sym, iv)).fetchall()
    fr = c.execute("select ts, rate from funding where symbol=? order by ts", (sym,)).fetchall()
    c.close()
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows], fr)


def atr(h, l, c, n=14):
    out = [0.0]*len(c)
    for i in range(n, len(c)):
        s = 0.0
        for j in range(i-n+1, i+1):
            tr = max(h[j]-l[j], abs(h[j]-c[j-1]), abs(l[j]-c[j-1]))
            s += tr
        out[i] = s/n
    return out


def fb(fr, a, b):
    return sum(r for ts, r in fr if a <= ts < b)


def run(h, l, c, t, fr, N=150, M=75, at=None, allow_short=False,
        stop_k=0.0, max_hold=0):
    """stop_k>0 时，停损 = 入场价 -/+ stop_k * ATR(入场那根)。"""
    n = len(c)
    trs = []
    pos = 0
    ep = 0.0
    ei = 0
    sp = 0.0
    for i in range(max(N, M), n-1):
        nxt = c[i]
        # 先判停损（用当根的 low/high）
        if pos != 0 and stop_k > 0:
            if pos > 0 and l[i] <= sp:
                trs.append({"i": i, "ts": t[i], "g": (sp-ep)/ep, "why": "stop"})
                pos = 0
            elif pos < 0 and h[i] >= sp:
                trs.append({"i": i, "ts": t[i], "g": (ep-sp)/ep, "why": "stop"})
                pos = 0
        if pos != 0 and max_hold and (i - ei) >= max_hold:
            trs.append({"i": i, "ts": t[i], "g": pos*(nxt-ep)/ep if pos > 0 else (ep-nxt)/ep,
                        "why": "time"})
            pos = 0
        hi = max(h[i-N:i])
        lo = min(l[i-M:i])
        lo_s = min(l[i-N:i])
        hi_s = max(h[i-M:i])
        if pos == 0:
            if c[i] > hi:
                pos, ep, ei = 1, c[i+1], i+1
                sp = ep - stop_k*at[i] if stop_k > 0 else 0.0
            elif allow_short and c[i] < lo_s:
                pos, ep, ei = -1, c[i+1], i+1
                sp = ep + stop_k*at[i] if stop_k > 0 else 0.0
        elif pos > 0 and c[i] < lo:
            trs.append({"i": i+1, "ts": t[i+1], "g": (c[i+1]-ep)/ep, "why": "chan"})
            pos = 0
            if allow_short and c[i] < lo_s:
                pos, ep, ei = -1, c[i+1], i+1
                sp = ep + stop_k*at[i] if stop_k > 0 else 0.0
        elif pos < 0 and c[i] > hi_s:
            trs.append({"i": i+1, "ts": t[i+1], "g": (ep-c[i+1])/ep, "why": "chan"})
            pos = 0
    return trs


def account(trs, t, fr, fee=0.0005, lev=1.0, eq0=100.0, lot=0.001, mn=50.0, px0=None):
    eq = eq0
    pk = eq0
    mdd = 0.0
    for x in trs:
        notch = None
    eq = eq0
    pk = eq0
    mdd = 0.0
    lots = 0
    prev_i = 0
    for x in trs:
        if px0 is None:
            lp = 1.0
        else:
            lp = px0
        aff = int(eq*lev/(lot*lp))
        if aff < 1 or aff*lot*lp < mn:
            continue
        notion = aff*lot*lp
        pnl = x["g"]*notion - 2*(notion*fee)
        eq += pnl
        if eq <= 0:
            return {"final": eq, "mdd": 100.0, "n": len(trs)}
        pk = max(pk, eq)
        mdd = max(mdd, (pk-eq)/pk)
    return {"final": eq, "mdd": mdd*100, "n": len(trs)}


def summarize(trs, t, label, fr=None):
    if len(trs) < 20:
        print("  %-34s 交易不足 %d" % (label, len(trs)))
        return None
    n = len(trs)
    gs = [x["g"] for x in trs]
    avg = sum(gs)/n*100
    sd = sqrt(sum((g*100-avg)**2 for g in gs)/n)
    yrs = (t[-1]-t[0])/(365.25*24*3600*1000)
    win = sum(1 for g in gs if g > 0)/n*100
    stops = sum(1 for x in trs if x["why"] == "stop")
    # 复利（满仓）
    eq = 1.0
    pk = 1.0
    mdd = 0.0
    for g in gs:
        eq *= (1+g)
        pk = max(pk, eq)
        mdd = max(mdd, (pk-eq)/pk)
    print("  %-34s n=%3d %6.1f/年 ratio %+6.3f%% t=%+5.2f 胜率%4.0f%% 满仓复利%+9.1f%% 回撤%5.1f%% 止损%d"
          % (label, n, n/yrs, avg, avg/(sd/sqrt(n)) if sd > 0 else 0, win,
             (eq-1)*100, mdd*100, stops))
    return {"n": n, "avg": avg, "eq": eq, "mdd": mdd*100}


def main():
    t, o, h, l, c, fr = load("BTCUSDT")
    at = atr(h, l, c, 14)
    print("BTCUSDT 1h  %d 根  停损/做空/持仓上限 三维扫描" % len(c))
    print("（满仓复利口径，未含杠杆；rate = 每笔平均收益）")
    print()
    print("【1】硬停损该放多宽？（做多，用 ATR 倍数）")
    for k in (0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0):
        trs = run(h, l, c, t, fr, stop_k=k, at=at)
        summarize(trs, t, "不停损" if k == 0 else "停损 %.1f x ATR" % k)

    print()
    print("【2】要不要做空？")
    for sh in (False, True):
        trs = run(h, l, c, t, fr, allow_short=sh)
        summarize(trs, t, "双向(多+空)" if sh else "只做多")

    print()
    print("【3】持仓时间上限（防止一辈子不触发通道出场）")
    for mh in (0, 120, 240, 480):
        trs = run(h, l, c, t, fr, max_hold=mh)
        summarize(trs, t, "无上限" if mh == 0 else "最多 %d 小时" % mh)

    print()
    print("【4】组合：只做多 + 停损 + 持仓上限")
    for k in (1.5, 2.0, 2.5, 3.0):
        for mh in (0, 240):
            trs = run(h, l, c, t, fr, stop_k=k, at=at, max_hold=mh)
            summarize(trs, t, "%.1fATR + %s" % (k, "无上限" if mh == 0 else "%dh" % mh))

    print()
    print("【5】100U 真实账户（1 手 0.001 BTC = 78.33U，1x 满仓）")
    for k in (0.0, 2.0, 2.5, 3.0):
        trs = run(h, l, c, t, fr, stop_k=k, at=at)
        for lev in (1.0,):
            r = account(trs, t, fr, lev=lev, px0=78330.0)
            print("  %-20s 最终 %8.2f U  回撤 %5.1f%%  笔数 %d"
                  % ("不停损" if k == 0 else "%.1fATR 停损" % k, r["final"], r["mdd"], r["n"]))


if __name__ == "__main__":
    main()
