# -*- coding: utf-8 -*-
"""那个 25% 熔断档 6.84 年触发 0 次，该设多少？

背景：
  趋势策略每笔都挂一个 trend_stop_pct=25 的硬止损，
  实测 213 笔里 0 次触发——因为 SMA 出场总在它之前发生。
  所以 25% 是个从没工作过的档位，真正的风控是均线。

问题：
  如果收紧止损，砍掉的是亏损单的尾巴，还是赢家的起点？
  ——只能靠实测，不能靠直觉。

方法：
  在 4h 线上给每笔加硬止损，止损价 = 开仓价 × (1 - p)。
  同一根 K 线内，只要 low <= 止损价 就按止损价出场（保守：
  假设先打止损再上涨，不给"先涨后跌"的便宜）。

判据不能只看样本内最优 —— 那必然过拟合。
要看：整条曲线是不是一段平台，以及训练/测试两半是否同向。
"""
import os, sys
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

COST = 0.001
DB = os.environ.get("BT_DB", "data/spot.db")
rows = [r for r in bt.load(DB, "4h") if r[5] > 0]
C = [r[4] for r in rows]
H = [r[2] for r in rows]
L = [r[3] for r in rows]
N = len(rows)
NL = 200


def sim(stop_pct, i0, i1):
    """stop_pct=None 表示不设硬止损。"""
    eq = 1.0
    pos = False
    entry = 0.0
    peak = 1.0
    dd = 0.0
    trades = 0
    stops = 0
    for i in range(max(NL + 1, i0), min(i1, N)):
        s = sum(C[i - NL:i]) / NL
        sp = sum(C[i - 1 - NL:i - 1]) / NL if i - 1 - NL >= 0 else s
        p, pp = C[i], C[i - 1]
        if not pos:
            if p > s and pp <= sp:
                pos = True
                entry = p
                trades += 1
        else:
            exit_px = None
            if stop_pct is not None:
                stop_px = entry * (1 - stop_pct / 100.0)
                if L[i] <= stop_px:
                    exit_px = stop_px
                    stops += 1
            if exit_px is None and p < s:
                exit_px = p
            if exit_px is not None:
                eq *= (1 + (exit_px - entry) / entry - 2 * COST)
                pos = False
                peak = max(peak, eq)
                if peak > 0:
                    dd = max(dd, (peak - eq) / peak)
    if pos:
        eq *= (1 + (C[min(i1, N) - 1] - entry) / entry - 2 * COST)
        peak = max(peak, eq)
        if peak > 0:
            dd = max(dd, (peak - eq) / peak)
    return (eq - 1) * 100, dd * 100, trades, stops


print("=" * 78)
print("硬止损档位实验   数据 %s  %d 根  成本 %.2f%%/边" % (DB, N, COST * 100))
print("=" * 78)
print("%-10s %12s %8s %9s %8s %10s" % ("止损档", "累计收益", "最大回撤", "卡玛", "笔数", "止损次数"))
print("-" * 78)

for sp in (None, 40, 30, 25, 20, 15, 12, 10, 8, 6):
    ret, dd, tr, st = sim(sp, 0, N)
    cal = ret / dd if dd > 0 else 0
    label = "无" if sp is None else "%d%%" % sp
    print("%-10s %+11.0f%% %7.0f%% %9.1f %8d %10d"
          % (label, ret, dd, cal, tr, st))
