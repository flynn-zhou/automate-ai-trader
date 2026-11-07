# -*- coding: utf-8 -*-
"""修正延迟模型，并且换一个正确的评价标准。

上一版的问题：
  lag 用 pending 单实现，但执行后又立刻重新判断信号，
  导致 lag=1 和 lag=2 走同一条路径（结果完全相同，暴露了 bug）。

更重要的问题：我一直在用"总收益"对比买入持有，
但这不是这个策略的价值所在。它的价值是【同样的钱、更小的回撤】。
100U 账户遇到 77% 回撤等于出局，31% 还能继续。
所以主指标应该是卡玛（收益/最大回撤），而不是绝对收益。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools.backtest as bt

rows = [r for r in bt.load("data/spot.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
N = len(rows)
N_SMA = 200


def sma(i):
    if i < N_SMA:
        return None
    return sum(C[i - N_SMA:i]) / N_SMA


def sim(cost, lag=0):
    """lag = 信号出现后第几根 K 线成交（0 = 当根收盘）。"""
    eq = 1.0
    curve = []
    pos = False
    entry = 0.0
    trades = 0
    queue = []          # [(执行下标, 方向)]
    for i in range(N_SMA + 1, N):
        s = sma(i)
        if s is None:
            curve.append(eq)
            continue
        price = C[i]
        # 执行到期的单（严格按排定的下标）
        for q in list(queue):
            if q[0] <= i:
                act = q[1]
                if act == "buy" and not pos:
                    pos = True; entry = price; trades += 1
                elif act == "sell" and pos:
                    r = (price - entry) / entry - 2 * cost
                    eq *= (1 + r); pos = False
                queue.remove(q)
        # 当根信号：只在没有待执行单时判断
        if not queue:
            if not pos and price > s and C[i - 1] <= sma(i - 1):
                if lag == 0:
                    pos = True; entry = price; trades += 1
                else:
                    queue.append((i + lag, "buy"))
            elif pos and price < s:
                if lag == 0:
                    r = (price - entry) / entry - 2 * cost
                    eq *= (1 + r); pos = False
                else:
                    queue.append((i + lag, "sell"))
        curve.append(eq)
    if pos:
        r = (C[-1] - entry) / entry - 2 * cost
        eq *= (1 + r)
    return {"ret": (eq - 1) * 100, "trades": trades, "curve": curve}


def maxdd(curve):
    peak = -1e9; dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd * 100


bh_ret = (C[-1] / C[0] - 1) * 100
bh_dd = maxdd([c / C[0] for c in C])
bh_calmar = bh_ret / bh_dd
print("4h %d 根  %.2f 年" % (N, N * 4 / 24 / 365))
print("买入持有：%+.0f%%  回撤 %.0f%%  卡玛 %.2f" % (bh_ret, bh_dd, bh_calmar))
print()

print("=== 手续费 x 延迟（主指标是卡玛，不是总收益）===")
print("费/边    延迟   总收益    回撤   卡玛   卡玛/买入持有   交易")
for cost_pct in (0.10, 0.12, 0.15, 0.20, 0.30):
    for lag in (0, 1, 2, 3):
        r = sim(cost_pct / 100.0, lag)
        dd = maxdd(r["curve"])
        cal = r["ret"] / dd if dd else 0
        print("%.2f%%    %d根  %+8.0f%%  %5.0f%%  %5.2f     %5.2fx      %d"
              % (cost_pct, lag, r["ret"], dd, cal, cal / bh_calmar, r["trades"]))
    print()
