# -*- coding: utf-8 -*-
"""杀 B：那 448 倍是"限价等着"的功劳，还是我在暴跌那根偷了价？

B 的原始建模：entry = min(限价, 当根开盘)。跳空时会以更优价格成交。
这个逻辑在交易所层面成立，但它专门挑中最猛的暴跌那几根 —— 而那些
暴跌后的反弹，正是本项目测到的 alpha 来源。所以必须先排除偷价。

四个对照，全部用同一个信号、同一个持有期：
  B0 原始（entry = min(限价, 开盘)）              —— 基线，44838U
  B1 不许跳空改善（entry = 限价本身）              —— 最悲观
  B2 只在开盘高于限价时成交（跳空那类直接放弃）
  B3 剔除暴力插针（要求 low > 限价 x 0.998）
  B4 成交率打折（随机 50% 的成交判为没轮到）
另外补：成交后到出场前，最低价相对限价到底去了多深（插针深度分布）。
"""
import sqlite3, random
from math import log

HIST = "/home/cryptobot/ai-trader/data/hist.db"
LOT, MIN_NOTIONAL = 0.001, 50.0
FEE_TAKER, FEE_MAKER = 0.0005, 0.0002
W, HOLD, WAIT = 100, 48, 48


def load(sym, iv):
    c = sqlite3.connect(HIST)
    r = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
                  (sym, iv)).fetchall()
    fr = c.execute("select ts, rate from funding where symbol=? order by ts", (sym,)).fetchall()
    c.close()
    return r, fr


def fund_between(fr, t0, t1):
    return sum(x for ts, x in fr if t0 <= ts < t1)


def run(rows, fr, mode, keep_prob=1.0, seed=7, eq0=100.0):
    rnd = random.Random(seed)
    n = len(rows)
    t = [r[0] for r in rows]
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]
    eq, peak, mdd = eq0, eq0, 0.0
    trades = wins = placed = filled = 0
    depths = []
    i = W
    while i < n - HOLD:
        sup = min(l[i - W:i])
        limit_px = sup * 1.0002
        placed += 1
        fill_idx, entry = None, 0.0
        for k in range(i, min(i + WAIT, n - HOLD)):
            if l[k] > limit_px:
                continue
            if mode == "B1":
                entry = limit_px
            elif mode == "B2":
                if o[k] < limit_px:
                    continue                      # 跳空开盘在限价下方：放弃
                entry = limit_px
            elif mode == "B3":
                if l[k] < limit_px * 0.998:
                    continue                      # 插针太深：放弃
                entry = min(limit_px, o[k])
            else:
                entry = min(limit_px, o[k])
            if keep_prob < 1.0 and rnd.random() > keep_prob:
                continue                          # 排队没轮到
            fill_idx = k
            break
        if fill_idx is None:
            i += 1
            continue
        filled += 1
        qty = int(eq / entry / LOT) * LOT
        if qty < LOT or qty * entry < MIN_NOTIONAL:
            i = fill_idx + 1
            continue
        notional = qty * entry
        exit_idx = min(fill_idx + HOLD, n - 1)
        exit_px = c[exit_idx]
        pnl = qty * (exit_px - entry)
        f = fund_between(fr, t[fill_idx], t[exit_idx]) * notional
        fee = notional * FEE_MAKER + qty * exit_px * FEE_TAKER
        eq += pnl - f - fee
        trades += 1
        if pnl - f - fee > 0:
            wins += 1
        depths.append((min(l[fill_idx:exit_idx + 1]) - limit_px) / limit_px * 100.0)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
        i = exit_idx + 1
        if eq <= 0.5:
            break
    yrs = (t[-1] - t[0]) / (365.25 * 24 * 3600 * 1000)
    per = (log(eq / eq0) / trades * 100.0) if eq > 0 and trades else 0.0
    dp = sorted(depths)
    return dict(mode=mode, final=eq, trades=trades, mdd=mdd * 100,
                win=wins * 100.0 / max(1, trades), ypc=trades / yrs,
                placed=placed, filled=filled,
                fill_rate=filled * 100.0 / max(1, placed),
                per=per, d50=dp[len(dp) // 2] if dp else 0,
                d95=dp[int(len(dp) * 0.95)] if dp else 0)


def main():
    rows, fr = load("BTCUSDT", "5m")
    print("BTCUSDT 5m %d 根  持有 %d 根  等待 %d 根" % (len(rows), HOLD, WAIT))
    print()
    print("%-4s %-34s %11s %7s %7s %8s %8s"
          % ("编号", "建模", "终值U", "笔数", "胜率", "成交率", "单笔%"))
    print("-" * 92)
    cases = [
        ("B0", "原始（可跳空改善）", dict(mode="B0")),
        ("B1", "entry 强制=限价（不许改善）", dict(mode="B1")),
        ("B2", "跳空开盘一律放弃", dict(mode="B2")),
        ("B3", "剔除插针>0.2%的成交", dict(mode="B3")),
        ("B4", "成交率随机打 5 折", dict(mode="B0", keep_prob=0.5)),
        ("B5", "成交率随机打 2 折", dict(mode="B0", keep_prob=0.2)),
    ]
    out = []
    for tag, name, kw in cases:
        r = run(rows, fr, **kw)
        out.append(r)
        print("%-4s %-34s %11.2f %7d %6.1f%% %7.1f%% %+7.4f"
              % (tag, name, r["final"], r["trades"], r["win"], r["fill_rate"], r["per"]))
    print()
    print("插针深度（成交后到出场前最低价相对限价）：中位 %.3f%%  P95 %.3f%%"
          % (out[0]["d50"], out[0]["d95"]))
    print()
    b0 = out[0]["final"]
    print("相对原始 B0 的衰减：")
    for r in out[1:]:
        print("   %-4s %11.2fU   = 原始的 %6.2f%%" % (r["mode"], r["final"], r["final"] * 100 / b0))


if __name__ == "__main__":
    main()
