# -*- coding: utf-8 -*-
"""第四阶段：为「高杠杆剥头皮」死磕。

上一阶段最大的对人不公之处：全部按 taker 0.05%/边 + 滑点 0.02%/边 计成本。
剥头皮真正该用挂单（maker）0.02%/边，来回 0.04% —— 差 3.5 倍。

本阶段四件事：
  A. 条件预测力：某个信号出现后，未来 k 根的期望收益到底是多少？
     这是剥头皮的地基。没有条件预测力，任何仓位管理都是赌。
  B. 成本情景：taker/taker vs maker/maker，同一策略差多少。
  C. 波动率闸门：只在 ATR/价格 够大时进场（让期望波动盖过成本）。
  D. 时段效应：加密市场有没有稳定的日内规律（亚洲/欧洲/美洲盘）。
  E. 样本外：前 70% 拟合、后 30% 验证，避免拿全期数字骗自己。
"""
import sqlite3
import sys
import time
from math import sqrt

DB = "/home/cryptobot/ai-trader/data/hist.db"
FUND_8H = 0.0001
START_EQ, LOT_BTC, MIN_NOTIONAL = 100.0, 0.001, 50.0


def load(iv):
    c = sqlite3.connect(DB)
    rows = c.execute("select open_time,o,h,l,c,v from k where interval=? order by open_time",
                     (iv,)).fetchall()
    c.close()
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows])


def atr(h, l, c, n=14):
    out = [None] * len(c)
    tr = [0.0] * len(c)
    for i in range(len(c)):
        tr[i] = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i-1]),
                                               abs(l[i] - c[i-1]))
    s = 0.0
    for i in range(len(c)):
        s += tr[i]
        if i >= n:
            s -= tr[i-n]
        if i >= n - 1:
            out[i] = s / n
    return out


def sma(x, n):
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i-n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n):
    out = [None] * len(x)
    k = 2.0 / (n + 1)
    p = None
    for i, v in enumerate(x):
        p = v if p is None else v * k + p * (1 - k)
        out[i] = p
    return out


def hr(x):
    print()
    print("=" * 108)
    print(x)
    print("=" * 108)


# ============================================================ A 条件预测力
def conditional_edge(c, sig, name, horizons, cost_maker, cost_taker):
    """信号为真的那些根，未来 h 根的收益均值。基线 = 全部根。"""
    n = len(c)
    tot = {h: [] for h in horizons}
    base = {h: [] for h in horizons}
    for i in range(n - max(horizons)):
        r = {h: (c[i+h] / c[i] - 1.0) for h in horizons}
        for h in horizons:
            base[h].append(r[h])
            if sig[i]:
                tot[h].append(r[h])
    print("  %-26s" % name, end="")
    for h in horizons:
        if not tot[h]:
            print(" h=%d: n=0" % h, end="")
            continue
        m = sum(tot[h]) / len(tot[h])
        b = sum(base[h]) / len(base[h])
        sd = sqrt(sum((x - m) ** 2 for x in tot[h]) / len(tot[h])) if len(tot[h]) > 1 else 0
        t = m / (sd / sqrt(len(tot[h]))) if sd > 0 else 0
        print("  h=%d: n=%6d 边=%+.4f%% t=%+5.1f" % (h, len(tot[h]), (m - b) * 100, t), end="")
    print()


# ============================================================ B/C/E 逐笔模拟
def simulate(o, h, l, c, t, N, M, cost_rt, atr_k=0.0, tp_k=0.0,
             risk_pct=2.0, max_lev=3.0, allow_short=False, vol_gate=0.0,
             hours_ok=None, bar_ms=300000):
    n = len(c)
    a = atr(h, l, c, 14)
    eq = START_EQ
    trades = []
    pos = 0
    lots = 0.0
    ep = stop = target = 0.0
    ei = 0
    for i in range(max(N, M, 15), n - 1):
        if a[i] is None or a[i] <= 0:
            continue
        if pos != 0:
            hi_b, lo_b = h[i], l[i]
            ex = None
            if stop > 0:
                if pos > 0:
                    if lo_b <= stop:
                        ex = stop
                    elif target > 0 and hi_b >= target:
                        ex = target
                else:
                    if hi_b >= stop:
                        ex = stop
                    elif target > 0 and lo_b <= target:
                        ex = target
            if ex is None and pos > 0 and c[i] < min(l[i-M:i]):
                ex = o[i+1]
            elif ex is None and pos < 0 and c[i] > max(h[i-M:i]):
                ex = o[i+1]
            if ex is not None:
                notion = lots * LOT_BTC * ep
                gross = pos * (ex - ep) / ep * notion
                hh = max(0.0, (t[i] - t[ei]) / 3600000.0)
                pnl = gross - notion * FUND_8H * hh / 8.0 - cost_rt * notion / 2.0 - cost_rt * lots * LOT_BTC * ex / 2.0
                eq += pnl
                trades.append({"pnl": pnl, "notion": notion, "held_h": hh})
                pos, lots = 0, 0.0
                if eq <= 0:
                    break
                continue
            continue
        # 波动率闸门
        if vol_gate > 0 and a[i] / c[i] < vol_gate:
            continue
        side = 0
        if c[i] > max(h[i-N:i]):
            side = 1
        elif allow_short and c[i] < min(l[i-N:i]):
            side = -1
        if side == 0:
            continue
        if hours_ok is not None:
            hh = (t[i+1] // 3600000) % 24
            if hh not in hours_ok:
                continue
        px = o[i+1]
        if atr_k > 0:
            dist = atr_k * a[i]
            raw = (eq * risk_pct / 100.0) / (dist / px) / px / LOT_BTC
            raw = min(raw, eq * max_lev / px / LOT_BTC)
        else:
            raw = eq * max_lev / px / LOT_BTC
        lots = float(int(raw))
        notion = lots * LOT_BTC * px
        if lots < 1 or notion < MIN_NOTIONAL or notion > eq * 125:
            continue
        pos, ep, ei = side, px, i + 1
        stop = px - side * atr_k * a[i] if atr_k > 0 else 0.0
        target = px + side * tp_k * a[i] if (atr_k > 0 and tp_k > 0) else 0.0
    if not trades:
        return None
    nt = len(trades)
    pk = START_EQ
    m = 0.0
    run = START_EQ
    for x in trades:
        run += x["pnl"]
        pk = max(pk, run)
        m = max(m, (pk - run) / pk)
    yrs = (t[-1] - t[0]) / (365.25 * 24 * 3600 * 1000)
    return {"n": nt, "final": eq, "ret": (eq/START_EQ - 1) * 100,
            "cagr": ((eq/START_EQ) ** (1/yrs) - 1) * 100 if eq > 0 and yrs > 0 else -100.0,
            "mdd": m * 100, "win": sum(1 for x in trades if x["pnl"] > 0)/nt*100,
            "per_year": nt/yrs, "avg_hold": sum(x["held_h"] for x in trades)/nt}


def oos_split(o, h, l, c, t, frac=0.7, **kw):
    cut = int(len(c) * frac)
    kw2 = dict(kw)
    kw2["bar_ms"] = kw.get("bar_ms", 300000)
    a = simulate(o[:cut], h[:cut], l[:cut], c[:cut], t[:cut], **kw)
    b = simulate(o[cut:], h[cut:], l[cut:], c[cut:], t[cut:], **kw)
    return a, b


def main():
    iv = sys.argv[1] if len(sys.argv) > 1 else "5m"
    t, o, h, l, c, v = load(iv)
    a14 = atr(h, l, c, 14)
    print("%s  %d 根  %s ~ %s" % (iv, len(c),
          time.strftime("%Y-%m-%d", time.gmtime(t[0]/1000)),
          time.strftime("%Y-%m-%d", time.gmtime(t[-1]/1000))))

    hr("A. 条件预测力：信号出现后，未来 k 根的平均超额收益（相对无条件基线）")
    print("   cost_maker=%.2f%%/来回  cost_taker=%.2f%%/来回" % (0.04, 0.14))
    print()
    HZ = [1, 3, 6, 12, 24]
    sm20, sm50, sm200 = sma(c, 20), sma(c, 50), sma(c, 200)
    ef12, es48 = ema(c, 12), ema(c, 48)
    sigs = []
    sigs.append(("价格 < SMA20 (超卖)", [1 if (sm20[i] and c[i] < sm20[i]) else 0 for i in range(len(c))]))
    sigs.append(("价格 > SMA20 (超买)", [1 if (sm20[i] and c[i] > sm20[i]) else 0 for i in range(len(c))]))
    sigs.append(("连跌 3 根", [1 if i >= 3 and c[i] < c[i-1] < c[i-2] < c[i-3] else 0 for i in range(len(c))]))
    sigs.append(("连涨 3 根", [1 if i >= 3 and c[i] > c[i-1] > c[i-2] > c[i-3] else 0 for i in range(len(c))]))
    sigs.append(("单根跌幅 > 1 ATR", [1 if (a14[i] and i >= 1 and c[i-1] - c[i] > a14[i]) else 0 for i in range(len(c))]))
    sigs.append(("单根涨幅 > 1 ATR", [1 if (a14[i] and i >= 1 and c[i] - c[i-1] > a14[i]) else 0 for i in range(len(c))]))
    sigs.append(("EMA12 > EMA48", [1 if (ef12[i] and es48[i] and ef12[i] > es48[i]) else 0 for i in range(len(c))]))
    sigs.append(("ATR/价格 > 中位数的2倍", [1 if (a14[i] and a14[i]/c[i] > 0.02) else 0 for i in range(len(c))]))
    for nm, s in sigs:
        conditional_edge(c, s, nm, HZ, 0.0004, 0.0014)

    hr("B. 成本情景 + 样本外：同一个策略，taker 与 maker 差多少")
    print("  %-22s %-9s %-8s %-6s %-10s %-10s %-8s"
          % ("配置", "成本", "样本", "笔数", "年化", "最大回撤", "笔/年"))
    for label, cost in (("taker 0.14%", 0.0014), ("maker 0.04%", 0.0004)):
        for N, M in ((20, 10), (55, 20)):
            a, b = oos_split(o, h, l, c, t, 0.7, N=N, M=M, cost_rt=cost,
                             max_lev=3.0, bar_ms=300000)
            for tag, r in (("前70%", a), ("后30%", b)):
                if r:
                    print("  donchian(%d/%d) %-9s %-8s %6d %8.1f%% %8.1f%% %7.1f"
                          % (N, M, label, tag, r["n"], r["cagr"], r["mdd"], r["per_year"]))
        print()

    hr("C. 波动率闸门：只在 ATR/价格 够大时进场（maker 成本）")
    print("  %-22s %-9s %-8s %-6s %-10s %-10s %-8s"
          % ("配置", "闸门", "样本", "笔数", "年化", "最大回撤", "笔/年"))
    for vg in (0.0, 0.001, 0.002, 0.004, 0.006):
        a, b = oos_split(o, h, l, c, t, 0.7, N=20, M=10, cost_rt=0.0004,
                         max_lev=3.0, vol_gate=vg, bar_ms=300000)
        for tag, r in (("前70%", a), ("后30%", b)):
            if r:
                print("  donchian(20/10) ATR>%.1f%% %-8s %6d %8.1f%% %8.1f%% %7.1f"
                      % (vg*100, tag, r["n"], r["cagr"], r["mdd"], r["per_year"]))
        print()

    hr("D. 时段效应：按 UTC 小时看 5m 平均绝对波动 和 平均收益方向")
    if iv == "5m":
        buckets = {hh: [] for hh in range(24)}
        dirs = {hh: [] for hh in range(24)}
        for i in range(1, len(c)):
            hh = (t[i] // 3600000) % 24
            buckets[hh].append(abs(c[i]/c[i-1] - 1.0))
            dirs[hh].append(c[i]/c[i-1] - 1.0)
        best = sorted(range(24), key=lambda x: -sum(buckets[x])/len(buckets[x]))[:8]
        print("  波动最大的 8 个小时（UTC）：")
        for hh in best:
            m = sum(buckets[hh])/len(buckets[hh])*100
            d = sum(dirs[hh])/len(dirs[hh])*100
            print("    %02d:00  平均|涨跌| = %.4f%%   平均方向 = %+.5f%%   n=%d"
                  % (hh, m, d, len(buckets[hh])))
        print()
        print("  波动最小的 4 个小时：")
        worst = sorted(range(24), key=lambda x: sum(buckets[x])/len(buckets[x]))[:4]
        for hh in worst:
            m = sum(buckets[hh])/len(buckets[hh])*100
            print("    %02d:00  平均|涨跌| = %.4f%%" % (hh, m))


if __name__ == "__main__":
    main()
