# -*- coding: utf-8 -*-
"""滚动前向验证：参数在每一段都站得住，还是只靠某一段行情？

为什么必须做（而且要重做）：
  之前的参数分析是在旧数据上做的，而旧数据上一轮刚被证明是错的
  （backtest.db 曾变成 0 字节，重建后结论从 +473% 变成 +1032%）。
  支撑"选 SMA200"的理由必须在【修正后的数据】上重新成立。

  更关键的是：样本内 SMA300 明显更高（+1976% vs +1184%），
  如果只挑样本内最优，应该选 300。选 200 的理由是"样本外更稳"——
  这个理由必须被数据证明，不能只是一句话。

做法：
  1. 每个参数在每一年单独算收益 -> 看是不是只靠某一年
  2. 滚动：用前 N 年选参数，在接下来一年用这个参数 -> 真正的样本外
  3. 对比"每次都用样本内最优"与"固定用 200"
"""
import os, sys, datetime
sys.path.insert(0, os.getcwd())
import tools.backtest as bt

DB = os.environ.get("BT_DB", "data/spot.db")
COST = 0.001          # 现货 taker 0.10%/边，这是实际成本，不是乐观假设
rows = [r for r in bt.load(DB, "4h") if r[5] > 0]
C = [r[4] for r in rows]
T = [r[0] for r in rows]
N = len(rows)

def year_of(ms):
    return datetime.datetime.utcfromtimestamp(ms / 1000).year

YEARS = [year_of(t) for t in T]

def sim_range(n, i0, i1, cost=COST):
    """在 [i0, i1) 区间内跑策略，返回该区间收益率(%)。

    用前 n 根算均线，但收益只统计区间内的。
    """
    eq = 1.0
    pos = False
    entry = 0.0
    trades = 0
    for i in range(max(n + 1, i0), min(i1, N)):
        s = sum(C[i - n:i]) / n
        s_prev = sum(C[i - 1 - n:i - 1]) / n if i - 1 - n >= 0 else s
        p, pp = C[i], C[i - 1]
        if not pos and p > s and pp <= s_prev:
            pos = True; entry = p; trades += 1
        elif pos and p < s:
            eq *= (1 + (p - entry) / entry - 2 * cost)
            pos = False
    if pos:
        eq *= (1 + (C[min(i1, N) - 1] - entry) / entry - 2 * cost)
    return (eq - 1) * 100, trades

def idx_of_year(y):
    idx = [i for i in range(N) if YEARS[i] == y]
    return (idx[0], idx[-1] + 1) if idx else None

print("=" * 66)
print("滚动前向验证   数据 %s   %d 根   成本 %.2f%%/边" % (DB, N, COST * 100))
print("=" * 66)

years = sorted(set(YEARS))
print()
print("=== 1) 每个参数逐年表现（看是否只靠某一年）===")
hdr = "SMA   " + "".join("%8s" % y for y in years) + "    全期"
print(hdr)
for n in (100, 150, 200, 250, 300):
    cells = []
    for y in years:
        r = idx_of_year(y)
        v = sim_range(n, r[0], r[1])[0] if r else 0.0
        cells.append("%+7.0f%%" % v)
    total = sim_range(n, 0, N)[0]
    print("%-5d " % n + "".join("%8s" % c for c in cells) + "  %+7.0f%%" % total)

print()
print("=== 2) 滚动前向：用前 2 年选参数，在第 3 年用 ===")
print("训练窗口            选出   训练收益   次年(样本外)  当年最优参数")
oos_all, fixed_all = [], []
for i in range(2, len(years)):
    tr_years = years[max(0, i - 2):i]
    te_y = years[i]
    tr_r = (idx_of_year(tr_years[0])[0], idx_of_year(tr_years[-1])[1])
    te_r = idx_of_year(te_y)
    if not te_r:
        continue
    best = max((100, 150, 200, 250, 300),
               key=lambda n: sim_range(n, tr_r[0], tr_r[1])[0])
    tr_ret = sim_range(best, tr_r[0], tr_r[1])[0]
    oos_ret = sim_range(best, te_r[0], te_r[1])[0]
    # 参照：如果当年用了最优参数会是多少
    best_te = max((100, 150, 200, 250, 300),
                  key=lambda n: sim_range(n, te_r[0], te_r[1])[0])
    oos_all.append(oos_ret)
    # 固定 200 在同一年
    fixed_all.append(sim_range(200, te_r[0], te_r[1])[0])
    print("%-18s  %4d   %+8.0f%%   %+9.0f%%   %4d"
          % ("%d-%d" % (tr_years[0], tr_years[-1]), best, tr_ret, oos_ret, best_te))

print()
print("样本外（滚动选参）平均 %.1f%%/年" % (sum(oos_all) / len(oos_all)))
print("样本外（固定 200）平均 %.1f%%/年" % (sum(fixed_all) / len(fixed_all)))
print("每年都用当年最优平均 %.1f%%/年  <- 这是做不到的上限（未来函数）"
      % (sum(max(sim_range(n, idx_of_year(y)[0], idx_of_year(y)[1])[0]
                 for n in (100, 150, 200, 250, 300))
             for y in years if idx_of_year(y)) / len(years)))
