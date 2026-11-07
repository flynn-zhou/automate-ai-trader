# -*- coding: utf-8 -*-
"""第三阶段：前两阶段没有止损，只有「信号反转才走」。

这可能是 15m/5m 全军覆没的真正原因——不是没有边际，而是
一次失败的突破要一路扛到反向信号，把小亏拖成大亏。

本阶段加三样东西：
  1. ATR 止损 + ATR 目标（逐根 K 线推进，用 high/low 判触发）
  2. 风险定量仓位：每笔亏 2%（或指定值）权益，而不是「满仓 × 杠杆」
  3. 双向（多和空）
"""
import sqlite3
import sys
import time

DB = "/home/cryptobot/ai-trader/data/hist.db"
FEE, SLIP, FUND_8H = 0.0005, 0.0002, 0.0001
START_EQ, LOT_BTC, MIN_NOTIONAL = 100.0, 0.001, 50.0


def load(iv):
    c = sqlite3.connect(DB)
    rows = c.execute("select open_time,o,h,l,c from k where interval=? order by open_time",
                     (iv,)).fetchall()
    c.close()
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows])


def atr(h, l, c, n):
    out = [None] * len(c)
    tr = [0.0] * len(c)
    for i in range(len(c)):
        tr[i] = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]),
                                               abs(l[i] - c[i - 1]))
    s = 0.0
    for i in range(len(c)):
        s += tr[i]
        if i >= n:
            s -= tr[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def simulate(o, h, l, c, t, bar_ms, N, M, atr_k, tp_k, risk_pct, max_lev,
             allow_short, use_stop=True):
    """逐根推进。第 i 根收盘决定，第 i+1 根开盘进场。"""
    n = len(c)
    a = atr(h, l, c, 14)
    eq = START_EQ
    peak = eq
    mdd = 0.0
    trades = []
    pos = 0
    lots = 0.0
    ep = 0.0
    stop = 0.0
    target = 0.0
    ei = 0
    # 前视窗口最小值（不含当前根）
    for i in range(max(N, M, 15), n - 1):
        if a[i] is None or a[i] <= 0:
            continue
        # ---- 持仓中：先判止损/止盈（用本根 high/low） ----
        if pos != 0:
            hi_bound, lo_bound = h[i], l[i]
            exited = False
            exit_px = 0.0
            why = ""
            if use_stop:
                if pos > 0:
                    if lo_bound <= stop:
                        exit_px, why, exited = stop, "stop", True
                    elif tp_k > 0 and hi_bound >= target:
                        exit_px, why, exited = target, "target", True
                else:
                    if hi_bound >= stop:
                        exit_px, why, exited = stop, "stop", True
                    elif tp_k > 0 and lo_bound <= target:
                        exit_px, why, exited = target, "target", True
            if exited:
                notion = lots * LOT_BTC * ep
                gross = pos * (exit_px - ep) / ep * notion
                hh = max(0.0, (t[i] - t[ei]) / 3600000.0)
                fund = notion * FUND_8H * hh / 8.0
                cost = (notion + lots * LOT_BTC * exit_px) * (FEE + SLIP)
                pnl = gross - fund - cost
                eq += pnl
                trades.append({"pnl": pnl, "notion": notion, "held_h": hh, "why": why})
                pos, lots = 0, 0.0
                if eq <= 0:
                    break
                peak = max(peak, eq)
                continue
        # ---- 平仓：反向突破 ----
        if pos != 0 and not use_stop:
            if pos > 0 and c[i] < min(l[i - M:i]):
                px = o[i + 1]
                notion = lots * LOT_BTC * ep
                gross = pos * (px - ep) / ep * notion
                hh = max(0.0, (t[i + 1] - t[ei]) / 3600000.0)
                pnl = gross - notion * FUND_8H * hh / 8.0 - (notion + lots * LOT_BTC * px) * (FEE + SLIP)
                eq += pnl
                trades.append({"pnl": pnl, "notion": notion, "held_h": hh, "why": "flip"})
                pos, lots = 0, 0.0
                if eq <= 0:
                    break
        if pos != 0:
            peak = max(peak, eq)
            continue
        # ---- 空仓：判入场 ----
        hh_n = max(h[i - N:i])
        ll_n = min(l[i - N:i])
        side = 0
        if c[i] > hh_n:
            side = 1
        elif allow_short and c[i] < ll_n:
            side = -1
        if side == 0:
            peak = max(peak, eq)
            continue
        px = o[i + 1]
        if use_stop:
            dist = atr_k * a[i]
            if dist <= 0:
                continue
            risk_u = eq * risk_pct / 100.0
            raw_lots = risk_u / (dist / px) / px / LOT_BTC
            raw_lots = min(raw_lots, eq * max_lev / px / LOT_BTC)
        else:
            raw_lots = eq * max_lev / px / LOT_BTC
        lots = float(int(raw_lots))
        notion = lots * LOT_BTC * px
        if lots < 1 or notion < MIN_NOTIONAL or notion > eq * 125:
            peak = max(peak, eq)
            continue
        pos = side
        ep = px
        ei = i + 1
        stop = px - side * atr_k * a[i] if use_stop else 0.0
        target = px + side * tp_k * a[i] if (use_stop and tp_k > 0) else 0.0
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    if not trades:
        return None
    n_t = len(trades)
    win = sum(1 for x in trades if x["pnl"] > 0) / n_t * 100
    yrs = (t[-1] - t[0]) / (365.25 * 24 * 3600 * 1000)
    pk = START_EQ
    m = 0.0
    run = START_EQ
    for x in trades:
        run += x["pnl"]
        pk = max(pk, run)
        m = max(m, (pk - run) / pk)
    return {"n": n_t, "final": eq, "ret": (eq / START_EQ - 1) * 100,
            "cagr": ((eq / START_EQ) ** (1 / yrs) - 1) * 100 if eq > 0 and yrs > 0 else -100.0,
            "mdd": m * 100, "win": win, "per_year": n_t / yrs,
            "avg_hold": sum(x["held_h"] for x in trades) / n_t}


def folds_ok(o, h, l, c, t, a1, a2, a3, a4, a5, a6, **kw):
    tot = []
    per = len(c) // 6
    for f in range(6):
        s = f * per
        e = (f + 1) * per if f < 5 else len(c)
        r = simulate(o[s:e], h[s:e], l[s:e], c[s:e], t[s:e], 1, a1, a2, a3, a4, a5, a6, **kw)
        if r:
            tot.append(r["ret"])
    return tot


def hr(x):
    print()
    print("-" * 112)
    print(x)
    print("-" * 112)


def main():
    iv = sys.argv[1] if len(sys.argv) > 1 else "1h"
    t, o, h, l, c = load(iv)
    print("%s  %d 根  %s ~ %s" % (iv, len(c),
          time.strftime("%Y-%m-%d", time.gmtime(t[0] / 1000)),
          time.strftime("%Y-%m-%d", time.gmtime(t[-1] / 1000))))

    hr("%s ATR 止损 + 风险定量仓位（每笔风险 2%%），双向，逐根推进" % iv)
    print("  %-30s %-7s %-8s %-9s %-8s %-8s %-8s %-8s"
          % ("参数 N/M/stop/tp", "方向", "笔/年", "最终权益", "年化", "最大回撤", "胜率", "均持仓"))
    grids = [
        (60, 30, 2.0, 0.0, True),
        (60, 30, 2.0, 4.0, True),
        (60, 30, 3.0, 0.0, True),
        (100, 50, 2.0, 0.0, True),
        (100, 50, 2.0, 4.0, True),
        (100, 50, 3.0, 6.0, True),
        (150, 75, 2.0, 0.0, True),
        (150, 75, 3.0, 0.0, True),
    ]
    for N, M, k, tp, both in grids:
        for short in (False, True):
            r = simulate(o, h, l, c, t, 0, N, M, k, tp, 2.0, 4.0, short, True)
            if not r:
                continue
            print("  N=%-3d M=%-3d k=%-3.1f tp=%-3.1f  %-8s %6.1f %9.2f %8.1f%% %7.1f%% %7.1f%% %7.1fh"
                  % (N, M, k, tp, "多空" if short else "只多", r["per_year"], r["final"],
                     r["cagr"], r["mdd"], r["win"], r["avg_hold"]))
        print()


if __name__ == "__main__":
    main()
