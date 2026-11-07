# -*- coding: utf-8 -*-
"""第 133 轮·核心实验：把限价单挂在支撑位等着，到底能不能赢。

用户的思路：
  "你要么在他们入场之前，提前预判价格可能去一个地方，先 limit 单等着"

这个思路的赌注是：省下的手续费 + 更好的成交价 > 逆向选择的代价。
做市商靠这个赚钱，也死在这个上面。

设计三条路径，用【完全相同的信号（收盘跌破 100 根低点）】：
  A 市价追：跌破后下一根开盘市价买入，taker 0.05%/边
  B 限价等：在破位前就挂 limit buy 在支撑位，被插针打中才成交，maker 0.02%/边
  C 限价等 + 限价出：出场也挂 limit（maker 0.02%/边）

B/C 的关键：挂单是在【破位之前】就挂好的（用 i 之前的窗口算支撑），
这样才符合"提前预判、先等着"的语义。绝不能事后用当根数据决定挂在哪。

必须报告的三个数字：
  1. 挂单成交率（低 = 空转）
  2. 成交后的逆行（逆向选择的代价）
  3. 三种路径的净值与逐年
"""
import sqlite3, time

HIST = "/home/cryptobot/ai-trader/data/hist.db"
LOT, MIN_NOTIONAL = 0.001, 50.0
FEE_TAKER, FEE_MAKER = 0.0005, 0.0002
W, BREAK, HOLD = 100, 0.10, 48
WAIT = 48          # 限价单挂多久没成交就撤单重挂


def load(sym, iv):
    c = sqlite3.connect(HIST)
    r = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
                  (sym, iv)).fetchall()
    fr = c.execute("select ts, rate from funding where symbol=? order by ts", (sym,)).fetchall()
    c.close()
    return r, fr


def fund_between(fr, t0, t1):
    return sum(x for ts, x in fr if t0 <= ts < t1)


def run(rows, fr, mode, eq0=100.0):
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
    wins = 0
    placed = 0
    filled = 0
    adverse = []
    i = W

    while i < n - HOLD:
        sup = min(l[i - W:i])          # 支撑位：只用 i 之前的数据
        if mode == "A":
            # 市价追：必须等收盘确认跌破
            if c[i] >= sup * (1 - BREAK / 100.0):
                i += 1
                continue
            j = i + 1
            if j >= n:
                break
            entry = o[j]
            fill_idx = j
            fee_in = FEE_TAKER
        else:
            # 限价等：在支撑位挂买单（略高一点以提高成交率），等它被砸到
            placed += 1
            limit_px = sup * 1.0002     # 贴在支撑位上沿
            fill_idx = None
            entry = 0.0
            for k in range(i, min(i + WAIT, n - HOLD)):
                if l[k] <= limit_px:
                    entry = min(limit_px, o[k])
                    fill_idx = k
                    break
            if fill_idx is None:
                i += 1
                continue
            filled += 1
            fee_in = FEE_MAKER
        qty = int(eq / entry / LOT) * LOT
        if qty < LOT or qty * entry < MIN_NOTIONAL:
            i = fill_idx + 1
            continue
        notional = qty * entry
        exit_idx = min(fill_idx + HOLD, n - 1)
        if mode == "C":
            # 限价出：挂在 +0.3% 等着，等不到就市价收
            tgt = entry * 1.003
            exit_px = None
            for k in range(fill_idx + 1, exit_idx + 1):
                if h[k] >= tgt:
                    exit_px = tgt
                    break
            if exit_px is None:
                exit_px = c[exit_idx]
                fee_out = FEE_TAKER
            else:
                fee_out = FEE_MAKER
        else:
            exit_px = c[exit_idx]
            fee_out = FEE_TAKER if mode == "A" else FEE_MAKER
        pnl = qty * (exit_px - entry)
        f = fund_between(fr, t[fill_idx], t[exit_idx]) * notional
        fee = notional * fee_in + qty * exit_px * fee_out
        eq += pnl - f - fee
        trades += 1
        if pnl - f - fee > 0:
            wins += 1
        # 成交后的最大逆行（逆向选择度量）
        w = min(l[fill_idx + 1:exit_idx + 1]) if exit_idx > fill_idx else entry
        adverse.append((w - entry) / entry * 100.0)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
        i = exit_idx + 1
        if eq <= 0.5:
            break
    yrs = (t[-1] - t[0]) / (365.25 * 24 * 3600 * 1000)
    return dict(mode=mode, final=eq, trades=trades, mdd=mdd * 100,
                win=wins * 100.0 / max(1, trades), yrs=yrs,
                placed=placed, filled=filled,
                fill_rate=filled * 100.0 / max(1, placed),
                adv=sorted(adverse)[len(adverse) // 2] if adverse else 0.0)


def main():
    rows, fr = load("BTCUSDT", "5m")
    print("BTCUSDT 5m %d 根  %.2f 年   持有 %d 根，限价等待窗口 %d 根"
          % (len(rows), (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000), HOLD, WAIT))
    print()
    for mode, name in (("A", "市价追（跌破后 taker 进、taker 出）"),
                       ("B", "限价等（挂在支撑，maker 进、taker 出）"),
                       ("C", "限价等+限价出（maker 进、maker 出）")):
        r = run(rows, fr, mode)
        print("-" * 88)
        print("%s  [%s]" % (name, mode))
        print("  终值 %.2fU   笔数 %d   胜率 %.1f%%   回撤 %.1f%%"
              % (r["final"], r["trades"], r["win"], r["mdd"]))
        if mode != "A":
            print("  挂单 %d 次，成交 %d 次，成交率 %.1f%%  <-- 空转风险"
                  % (r["placed"], r["filled"], r["fill_rate"]))
        print("  成交后持有期内的最大逆行（中位数）：%.3f%%  <-- 逆向选择"
              % r["adv"])
    print("-" * 88)
    print()
    print("判据：B/C 终值超过 A 才算'限价等着'这条路成立。")


if __name__ == "__main__":
    main()
