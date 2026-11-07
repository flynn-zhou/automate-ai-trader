# -*- coding: utf-8 -*-
"""杠杆生死线：同一个信号，1x 到 100x，带真实强平与手续费。

上一轮算清了：100x 的强平线是 -0.6%（逐仓、维持保证金率 0.4%）。
一个无漂移随机游走在这个屏障下 EV 恒为 0，再扣 4%/笔的手续费就是必亏。
但 5m 破位信号有 +0.11% 的 alpha（=100x 下本金的 +11%），理论上可能盖过 4%。 

所以唯一能回答问题的办法：跑真实的、带强平的资金曲线。
  逐仓强平：权益 <= 名义额 x 0.4% 时归零
  手续费：maker 0.02%/边（BNB 抵扣），按名义额收
  资金费：按 funding 表逐段累计
  仓位：每次用全部可用权益按 L 倍开仓，向下取整到 0.001 BTC
"""
import sqlite3, time

HIST = "/home/cryptobot/ai-trader/data/hist.db"
LOT, MIN_NOTIONAL, MMR = 0.001, 50.0, 0.004
FEE = 0.0002          # maker 单边（BNB 抵扣）
W, BREAK, HOLD = 100, 0.10, 48


def load(sym, iv):
    c = sqlite3.connect(HIST)
    r = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
                  (sym, iv)).fetchall()
    fr = c.execute("select ts, rate from funding where symbol=? order by ts", (sym,)).fetchall()
    c.close()
    return r, fr


def fund_between(fr, t0, t1):
    return sum(x for ts, x in fr if t0 <= ts < t1)


def simulate(rows, fr, lev, eq0=100.0):
    n = len(rows)
    t = [r[0] for r in rows]
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]

    eq = eq0
    peak = eq0
    mdd = 0.0
    trades = 0
    liqs = 0
    wins = 0
    i = W
    while i < n - HOLD:
        base_ = min(l[i - W:i])
        if c[i] >= base_ * (1 - BREAK / 100.0):
            i += 1
            continue
        # 进场：下一根开盘
        j = i + 1
        entry = o[j]
        raw = int(eq * lev / entry / LOT)
        if raw < 1 or raw * LOT * entry < MIN_NOTIONAL:
            i += 1
            continue
        qty = raw * LOT
        notional = qty * entry
        margin_used = notional / lev
        liq_px = entry * (1.0 - (margin_used - notional * MMR) / notional)
        eq_start = eq
        closed = False
        for k in range(j, min(j + HOLD, n)):
            if l[k] <= liq_px:                      # 爆仓
                eq = max(0.0, eq_start - margin_used)
                liqs += 1
                trades += 1
                closed = True
                i = k + 1
                break
        if closed:
            if eq <= 0.5:
                break
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak)
            continue
        k = min(j + HOLD, n - 1)
        exit_px = c[k]
        pnl = qty * (exit_px - entry)
        f = fund_between(fr, t[j], t[k]) * notional
        fee = (notional + qty * exit_px) * FEE
        eq = eq_start + pnl - f - fee
        trades += 1
        if pnl - f - fee > 0:
            wins += 1
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
        i = k + 1
        if eq <= 0.5:
            break
    yrs = (t[-1] - t[0]) / (365.25 * 24 * 3600 * 1000)
    return dict(lev=lev, final=eq, trades=trades, liqs=liqs, mdd=mdd * 100,
                win=wins * 100.0 / max(1, trades), yrs=yrs)


def main():
    rows, fr = load("BTCUSDT", "5m")
    print("BTCUSDT 5m %d 根  %.2f 年   信号：收盘跌破 100 根低点后买入，持有 %d 根"
          % (len(rows), (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000), HOLD))
    print("强平规则：逐仓，维持保证金率 %.1f%%，权益 <= 名义额x%.1f%% 时归零" % (MMR * 100, MMR * 100))
    print()
    print("%-6s %12s %8s %8s %8s %8s" % ("杠杆", "终值U", "笔数", "爆仓", "回撤%", "胜率%"))
    print("-" * 60)
    for lev in (1, 2, 5, 10, 20, 50, 100):
        r = simulate(rows, fr, lev)
        note = ""
        if r["final"] < 0.5:
            note = "  <-- 归零"
        print("%-6d %12.2f %8d %8d %8.1f %8.1f%s"
              % (lev, r["final"], r["trades"], r["liqs"], r["mdd"], r["win"], note))


if __name__ == "__main__":
    main()
