# -*- coding: utf-8 -*-
"""扫单形态的宣判庭：杀掉剥头皮的那五道关，一道不少。

DOGE 5m 持有 2 小时每笔 +0.0733%、SOL 5m 持有 4 小时 +0.0744%，
看起来够付手续费了。但「全期平均」是最容易骗人的统计量。
按年拆、按样本外拆、按参数邻域拆，看它还站不站得住。
"""
import sqlite3
import time
from math import sqrt

DB = "/home/cryptobot/ai-trader/data/hist.db"
CM, CT = 0.04, 0.14


def load(sym, iv):
    c = sqlite3.connect(DB)
    rows = c.execute("select open_time,o,h,l,c,v from k2 where symbol=? and interval=?"
                     " order by open_time", (sym, iv)).fetchall()
    c.close()
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows])


def make_sig(h, l, c, NB, need_vol=None, v=None, vz_thr=0.0):
    n = len(c)
    lo_n = [None]*n
    for i in range(NB, n):
        lo_n[i] = min(l[i-NB:i])
    sig = [0]*n
    for i in range(NB, n):
        if lo_n[i] is None:
            continue
        ok = l[i] < lo_n[i] and c[i] > lo_n[i]
        if ok and need_vol is not None:
            ok = need_vol[i] >= vz_thr
        sig[i] = 1 if ok else 0
    return sig


def volz(v, w=200):
    n = len(v)
    out = [0.0]*n
    for i in range(w, n):
        win = v[i-w:i]
        m = sum(win)/w
        sd = sqrt(sum((x-m)**2 for x in win)/w) or 1e-9
        out[i] = (v[i]-m)/sd
    return out


def stat(c, sig, hz, s=0, e=None):
    n = len(c)
    if e is None:
        e = n
    tot = []
    for i in range(max(s, 1), min(e, n-hz)):
        if sig[i]:
            tot.append(c[i+hz]/c[i]-1.0)
    if len(tot) < 30:
        return None
    m = sum(tot)/len(tot)*100
    sd = sqrt(sum((x*100-m)**2 for x in tot)/len(tot))
    return {"n": len(tot), "m": m, "t": m/(sd/sqrt(len(tot))) if sd > 0 else 0,
            "sum": sum(tot)*100}


def main():
    cases = [("DOGEUSDT", "5m", 20, 24), ("DOGEUSDT", "15m", 20, 8),
             ("SOLUSDT", "5m", 20, 48), ("SOLUSDT", "15m", 20, 16),
             ("ETHUSDT", "15m", 20, 16)]
    print("五道关验收：能不能拿去跑实盘？")
    print("门槛：每笔 >= %.2f%%(maker) 或 %.2f%%(taker)" % (CM, CT))
    for sym, iv, NB, hz in cases:
        t, o, h, l, c, v = load(sym, iv)
        sig = make_sig(h, l, c, NB)
        print()
        print("=" * 96)
        print("%s %s  扫单(%d)  持有 %d 根" % (sym, iv, NB, hz))
        print("=" * 96)
        allst = stat(c, sig, hz)
        print("  全期：n=%d  平均 %+.4f%%  t=%+.1f" % (allst["n"], allst["m"], allst["t"]))

        # 逐年
        print("  逐年：")
        ok_y = 0
        tot_y = 0
        y0 = time.gmtime(t[0]/1000).tm_year
        y1 = time.gmtime(t[-1]/1000).tm_year
        for y in range(y0, y1+1):
            s = None
            for i, ts in enumerate(t):
                if time.gmtime(ts/1000).tm_year == y:
                    s = i; break
            e = None
            for i in range(len(t)-1, -1, -1):
                if time.gmtime(t[i]/1000).tm_year == y:
                    e = i+1; break
            if s is None or e is None or e-s < hz*3:
                continue
            r = stat(c, sig, hz, s, e)
            if not r or r["n"] < 20:
                continue
            tot_y += 1
            good = r["m"] >= CM
            if good:
                ok_y += 1
            print("    %d  n=%5d  %+8.4f%%  t=%+5.1f  %s"
                  % (y, r["n"], r["m"], r["t"], "OK" if good else "**不足**"))
        print("    -> 够成本的年份 %d/%d" % (ok_y, tot_y))

        # 样本外
        cut = int(len(c)*0.7)
        a = stat(c, sig, hz, 0, cut)
        b = stat(c, sig, hz, cut, len(c))
        print("  样本外：前70%% %+.4f%% (n=%d, t=%+.1f)  后30%% %+.4f%% (n=%d, t=%+.1f)  %s"
              % (a["m"], a["n"], a["t"], b["m"], b["n"], b["t"],
                 "两段都够" if (a["m"] >= CM and b["m"] >= CM) else "**至少一段不够**"))

        # 参数邻域
        print("  参数邻域：")
        sur = 0
        tot_p = 0
        for NB2 in (10, 15, 20, 30, 40):
            for hz2 in (hz, hz*2):
                sg2 = make_sig(h, l, c, NB2)
                r = stat(c, sg2, hz2)
                if not r:
                    continue
                tot_p += 1
                if r["m"] >= CM:
                    sur += 1
                print("    NB=%-3d hz=%-3d  n=%5d  %+8.4f%%  t=%+5.1f  %s"
                      % (NB2, hz2, r["n"], r["m"], r["t"],
                         "OK" if r["m"] >= CM else "不足"))
        print("    -> 够成本的参数格 %d/%d" % (sur, tot_p))


if __name__ == "__main__":
    main()
