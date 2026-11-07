# -*- coding: utf-8 -*-
"""对手代理指标：在只有 OHLCV 的前提下，能不能造出「谁在被套、谁在追」的度量。

三个可测的代理：
  RSJ  已实现偏度跳变——用单根内的成交分配把「急涨急跌」和「温和上涨」分开。
       定义：r = log(C/O)，rv_self = r^2, rv_max = max(r_up^2, r_dn^2)
             RSJ = (rv_self - rv_max) / rv_self
       负得多 = 单根内单边急动 = 追涨杀跌的人被套。
  CLIMAX 爆量长影——成交量 z 分数高 + 影线占比大 = 有人被打穿后交出筹码。
  SWEEP 扫单——本根最低点刺穿过去 N 根最低点但收盘拉回 = 下方流动性被吃掉。

然后在 5m/15m 上测：这些形态出现后，未来 k 根的收益是否盖过 maker 成本 0.04%。
"""
import sqlite3
import time
from math import log, sqrt

DB = "/home/cryptobot/ai-trader/data/hist.db"
CM = 0.04


def load(sym, iv):
    c = sqlite3.connect(DB)
    rows = c.execute("select open_time,o,h,l,c,v from k2 where symbol=? and interval=?"
                     " order by open_time", (sym, iv)).fetchall()
    c.close()
    return rows


def pct(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * q))]


def analyze(sym, iv, hzs=(3, 6, 12, 24)):
    rows = load(sym, iv)
    n = len(rows)
    if n < 5000:
        print("%s %s 数据不足 (%d)" % (sym, iv, n))
        return
    t = [r[0] for r in rows]
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]
    v = [r[5] for r in rows]

    # 成交量 z 分数（滚动 200 根）
    vz = [0.0] * n
    for i in range(200, n):
        w = v[i-200:i]
        m = sum(w) / 200
        sd = sqrt(sum((x-m)**2 for x in w) / 200) or 1e-9
        vz[i] = (v[i] - m) / sd

    rsj = [0.0] * n
    for i in range(n):
        if o[i] <= 0 or c[i] <= 0:
            continue
        r = log(c[i] / o[i])
        r2 = r * r
        if r2 < 1e-12:
            continue
        ru = log(h[i] / o[i]) if h[i] > o[i] else 0.0
        rd = log(o[i] / l[i]) if l[i] < o[i] else 0.0
        rmax = max(ru * ru, rd * rd)
        rsj[i] = (r2 - rmax) / r2

    sweep_lo = [None] * n
    sweep_hi = [None] * n
    clim = [None] * n
    NB = 20
    for i in range(NB + 1, n):
        lo_n = min(l[i-NB:i])
        hi_n = max(h[i-NB:i])
        sweep_lo[i] = (l[i] < lo_n) and (c[i] > lo_n)
        sweep_hi[i] = (h[i] > hi_n) and (c[i] < hi_n)
        rng = h[i] - l[i]
        if rng > 0:
            body = abs(c[i] - o[i])
            wick = 1.0 - body / rng
            clim[i] = (vz[i] > 3.0) and (wick > 0.5)

    def edge(mask, hz, sign=1):
        tot, base = [], []
        for i in range(200, n - hz):
            r = c[i+hz] / c[i] - 1.0
            base.append(r)
            if mask[i]:
                tot.append(r * sign)
        if len(tot) < 100:
            return None
        m = sum(tot) / len(tot) * 100
        b = sum(base) / len(base) * 100 * sign
        sd = sqrt(sum((x*100 - m)**2 for x in tot) / len(tot))
        return {"n": len(tot), "m": m, "exc": m - b,
                "t": m/(sd/sqrt(len(tot))) if sd > 0 else 0}

    print()
    print("=" * 100)
    print("%s %s  %d 根  %.1f 年" % (sym, iv, n, (t[-1]-t[0])/(365.25*24*3600*1000)))
    print("=" * 100)
    print("  %-30s %-8s %-6s %-11s %-9s %-7s %s"
          % ("形态", "方向", "样本", "绝对收益/笔", "相对基线", "t值", "vs maker 0.04%"))
    _neg = sorted(x for x in rsj[200:] if x < 0)
    _rq = _neg[int(len(_neg) * 0.10)] if _neg else -1.0
    tests = [
        ("扫单：破20根低点拉回", [1 if sweep_lo[i] else 0 for i in range(n)], 1),
        ("扫单：破20根高点回落", [1 if sweep_hi[i] else 0 for i in range(n)], -1),
        ("爆量长影(量z>3且影>50%)", [1 if clim[i] else 0 for i in range(n)], 0),
        ("RSJ 极负(单边急动)", [1 if rsj[i] < _rq else 0 for i in range(n)], 1),
        ("RSJ 极负 反向做空", [1 if rsj[i] < _rq else 0 for i in range(n)], -1),
    ]
    for nm, mask, side in tests:
        for hz in hzs:
            if side == 0:
                # 爆量长影：按 K 线方向判断，长下影做多、长上影做空
                mm = [0]*n
                for i in range(n):
                    if mask[i]:
                        mm[i] = 1 if c[i] > o[i] else -1
                tot, base = [], []
                for i in range(200, n-hz):
                    r = c[i+hz]/c[i] - 1.0
                    base.append(r)
                    if mm[i]:
                        tot.append(r * mm[i])
                if len(tot) < 100:
                    continue
                m = sum(tot)/len(tot)*100
                sd = sqrt(sum((x*100-m)**2 for x in tot)/len(tot))
                b = sum(base)/len(base)*100
                flag = "盖过" if m >= CM else ("差 %.1f 倍" % (CM/m) if m > 0 else "负")
                print("  %-30s %-8s %-6d %+10.4f%% %+9.4f%% %+7.1f  %s"
                      % (nm, "顺影线", len(tot), m, m-b,
                         m/(sd/sqrt(len(tot))) if sd > 0 else 0, flag))
                break
            r = edge(mask, hz, side)
            if not r:
                continue
            flag = "盖过" if r["m"] >= CM else ("差 %.1f 倍" % (CM/r["m"]) if r["m"] > 0 else "负")
            print("  %-30s %-8s %-6d %+10.4f%% %+9.4f%% %+7.1f  %s"
                  % (nm, "做多" if side > 0 else "做空", r["n"], r["m"], r["exc"], r["t"], flag))


def main():
    print("成本线：maker 来回 %.2f%%" % CM)
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"):
        for iv in ("5m", "15m"):
            try:
                analyze(sym, iv)
            except Exception as e:
                print("%s %s 失败 %s" % (sym, iv, e))


if __name__ == "__main__":
    main()
