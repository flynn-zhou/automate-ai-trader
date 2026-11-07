# -*- coding: utf-8 -*-
"""安全边际：成本涨到多少，这个策略就没边际了？

为什么必须测：
  现货 +1032%、回撤 31% 好得让人不安。好消息如果太便宜，通常是算错了。
  真正该问的不是"能赚多少"，而是"我算错多少它还能活"。

要测三件事：
  1. 手续费敏感度 —— 0.05% 到 0.50% 每边，边际在哪消失
  2. 延迟敏感度   —— 信号出现后晚一根 4h 才成交，还剩多少
  3. 两者叠加     —— 真实世界是"费用高 + 有延迟"
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools.backtest as bt

rows = [r for r in bt.load("data/spot.db", "4h") if r[5] > 0]
C = [r[4] for r in rows]
N = len(rows)
print("4h %d 根  %.2f 年" % (N, N * 4 / 24 / 365))
print()

N_SMA = 200


def sma(i):
    if i < N_SMA:
        return None
    return sum(C[i - N_SMA:i]) / N_SMA


def sim(cost, lag=0):
    """lag=0 用信号那根的收盘价成交；lag=1 晚一根。"""
    eq = 1.0
    curve = []
    pos = False
    entry = 0.0
    trades = 0
    pending = None       # (方向, 触发价) 等待下一根执行
    for i in range(N_SMA + 1, N):
        s = sma(i)
        if s is None:
            continue
        price = C[i]
        # 先执行上一根排队的单
        if pending is not None:
            act, _ = pending
            fill = price
            if act == "buy" and not pos:
                pos = True; entry = fill; trades += 1
            elif act == "sell" and pos:
                r = (fill - entry) / entry - 2 * cost
                eq *= (1 + r); pos = False
            pending = None
        if lag > 0:
            # 信号延迟一根执行
            if not pos and price > s and C[i - 1] <= sma(i - 1):
                pending = ("buy", price)
            elif pos and price < s:
                pending = ("sell", price)
        else:
            if not pos and price > s and C[i - 1] <= sma(i - 1):
                pos = True; entry = price; trades += 1
            elif pos and price < s:
                r = (price - entry) / entry - 2 * cost
                eq *= (1 + r); pos = False
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


bh = (C[-1] / C[0] - 1) * 100
bh_dd = maxdd([c / C[0] for c in C])
print("基准：买入持有 %+.0f%%  回撤 %.0f%%" % (bh, bh_dd))
print()

print("=== 1) 手续费敏感度（每边）===")
print("费/边     总收益    回撤    卡玛    交易   对比买入持有")
for cost_pct in (0.05, 0.075, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50):
    r = sim(cost_pct / 100.0)
    dd = maxdd(r["curve"])
    calmar = r["ret"] / dd if dd else 0
    diff = r["ret"] - bh
    print("%.3f%%   %+8.0f%%  %5.0f%%  %5.2f  %5d   %+8.0f%%%s"
          % (cost_pct, r["ret"], dd, calmar, r["trades"], diff,
             "  <- 输给买入持有" if diff < 0 else ""))
print()

print("=== 2) 延迟敏感度（信号后晚一根 4h 成交，费 0.10%/边）===")
for lag in (0, 1, 2):
    r = sim(0.001, lag=lag)
    dd = maxdd(r["curve"])
    print("晚 %d 根   %+8.0f%%   回撤 %3.0f%%   卡玛 %5.2f   交易 %d"
          % (lag, r["ret"], dd, r["ret"] / dd if dd else 0, r["trades"]))
print()

print("=== 3) 叠加：真实世界（费 0.12%/边 + 晚一根）===")
r = sim(0.0012, lag=1)
dd = maxdd(r["curve"])
print("  %+.0f%%   回撤 %.0f%%   卡玛 %.2f" % (r["ret"], dd, r["ret"] / dd if dd else 0))
print()

print("=== 结论 ===")
print("  现货实际成本约 0.10%%/边（taker），滑点再加 0.01-0.02%%。")
print("  上表看边际在哪个成本处消失，就知道安全边际有多厚。")
