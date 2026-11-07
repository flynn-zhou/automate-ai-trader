# -*- coding: utf-8 -*-
"""100 倍杠杆的死亡时钟：逆行 0.96% 有多容易发生。

强平线：BTCUSDT 永续逐仓，维持保证金率 0.4% -> 约 -0.96%。
手续费占保证金：taker 5%/边（0.05% x 100），BNB maker 2%/边。

三个问题，一个脚本答完：
  ① 任意时刻进场，价格逆行 0.96% 需要多久（死亡时钟）
  ② 同一入场，先到 +X% 还是先到 -0.96%（先摸高还是先摸低）
  ③ 用 5m 破位买入（唯一过了 beta 关的信号）单独算存活率
"""
import sqlite3, time, math

HIST = "/home/cryptobot/ai-trader/data/hist.db"
LIQ = -0.96          # 强平线 %


def load(sym, iv):
    c = sqlite3.connect(HIST)
    r = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
                  (sym, iv)).fetchall()
    c.close()
    return r


def pct(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = int(len(s) * q)
    return s[min(k, len(s) - 1)]


def main():
    for iv, hold in (("5m", 48), ("15m", 16), ("15m", 48)):
        rows = load("BTCUSDT", iv)
        if not rows:
            continue
        n = len(rows)
        hi = [r[2] for r in rows]
        lo = [r[3] for r in rows]
        cl = [r[4] for r in rows]
        print("=" * 92)
        print("BTCUSDT %s   %d 根   持有窗口 %d 根" % (iv, n, hold))
        print("=" * 92)

        # ① 死亡时钟：从每根收盘进场，H 根内最低价跌了多少
        worst = []
        for i in range(0, n - hold, 5):        # 每 5 根取一个样本，够用且快
            w = min(lo[i + 1:i + 1 + hold])
            worst.append((w - cl[i]) / cl[i] * 100.0)
        print("  [逆行深度] 持有 %d 根内最大不利偏移（从收盘进场算）：" % hold)
        print("     中位数 %.3f%%   P10 %.3f%%   P5 %.3f%%   P1 %.3f%%   最差 %.3f%%"
              % (pct(worst, 0.5), pct(worst, 0.10), pct(worst, 0.05),
                 pct(worst, 0.01), min(worst)))
        hit = sum(1 for w in worst if w <= LIQ)
        print("     会触发强平(-0.96%%)的比例：%.2f%%  （即每 %.1f 次进场就有一次归零）"
              % (hit * 100.0 / len(worst), len(worst) / max(1, hit)))
        # 需要多少根才达到 -0.96%
        import bisect
        times = []
        for i in range(0, n - 200, 37):
            base_px = cl[i]
            t = None
            for j in range(i + 1, min(i + 200, n)):
                if lo[j] <= base_px * (1 + LIQ / 100.0):
                    t = j - i
                    break
            if t:
                times.append(t)
        if times:
            print("     从任意时刻进场到触及 -0.96%% 所需根数：中位 %d 根，P25 %d 根"
                  % (pct(times, 0.5), pct(times, 0.25)))

        # ② 先摸高还是先摸低
        for up in (0.3, 0.5, 1.0):
            win = lose = 0
            for i in range(0, n - hold, 7):
                p = cl[i]
                u = p * (1 + up / 100.0)
                d = p * (1 + LIQ / 100.0)
                res = 0
                for j in range(i + 1, i + 1 + hold):
                    if lo[j] <= d:
                        res = -1
                        break
                    if hi[j] >= u:
                        res = 1
                        break
                if res > 0:
                    win += 1
                elif res < 0:
                    lose += 1
            tot = win + lose
            if tot:
                print("  [先到哪边] 目标 +%.1f%% vs 强平 %.2f%%：先到目标 %.1f%% / 先爆 %.1f%%"
                      "（不计未触发的 %.0f%%）"
                      % (up, LIQ, win * 100.0 / tot, lose * 100.0 / tot,
                         (1 - tot * 1.0 / len(range(0, n - hold, 7))) * 100))
        print()


if __name__ == "__main__":
    main()
