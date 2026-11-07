# -*- coding: utf-8 -*-
"""每笔交易的真实不利偏移（MAE）分布——尾部风险到底多大？

为什么现在才算得准：
  上一轮修好了 MFE/MAE 跟踪（趋势模式下 p['worst'] 现在真的更新）。
  在这之前，mae 列是"出场价的反向移动"，用它看尾部风险会得出错误结论。

  这不只是修了个字段，而是打开了一个之前做不了的分析：
  【一笔交易最难受的时候到底亏多少】，而不是"最后亏多少"。
  两者在震荡行情里差别很大。

要回答：
  1. 6.84 年里最坏的单笔亏损是多少？
  2. 有多少笔曾经浮亏超过 10%/15%/20%？（决定 25% 档位有没有意义）
  3. 均线出场 vs 硬止损，谁先动作？
"""
import os, sys, statistics
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

trades = []
i = NL + 1
pos = False
entry = 0.0
eidx = 0
best = 0.0
worst = 0.0
while i < N:
    s = sum(C[i - NL:i]) / NL
    sp = sum(C[i - 1 - NL:i - 1]) / NL
    if not pos:
        if C[i] > s and C[i - 1] <= sp:
            pos = True
            entry = C[i]
            eidx = i
            best = worst = entry
    else:
        best = max(best, H[i])
        worst = min(worst, L[i])
        if C[i] < s:
            ret = (C[i] - entry) / entry - 2 * COST
            trades.append({
                "bars": i - eidx,
                "ret": ret * 100,
                "mfe": (best - entry) / entry * 100,
                "mae": (worst - entry) / entry * 100,
            })
            pos = False
    i += 1
if pos:
    ret = (C[N - 1] - entry) / entry - 2 * COST
    trades.append({"bars": N - eidx, "ret": ret * 100,
                   "mfe": (best - entry) / entry * 100,
                   "mae": (worst - entry) / entry * 100})

rets = sorted(t["ret"] for t in trades)
maes = sorted(t["mae"] for t in trades)
mfes = sorted(t["mfe"] for t in trades)
n = len(trades)

print("=" * 70)
print("每笔交易的真实不利偏移分布   %d 笔  6.84 年" % n)
print("=" * 70)
print()
print("=== 最终收益（已扣双边手续费）===")
print("  最差 %+.1f%%   中位 %+.1f%%   最好 %+.1f%%"
      % (rets[0], rets[n // 2], rets[-1]))
win = sum(1 for r in rets if r > 0)
print("  胜率 %.0f%%（%d 胜 / %d 负）" % (win * 100.0 / n, win, n - win))
print()
print("=== 曾经浮亏到多少（MAE，这才是尾部风险）===")
print("  最深 %+.1f%%   中位 %+.1f%%   最浅 %+.1f%%"
      % (maes[0], maes[n // 2], maes[-1]))
for th in (5, 10, 15, 20, 25):
    k = sum(1 for m in maes if m <= -th)
    print("  浮亏曾超过 %2d%% 的：%3d 笔（%.0f%%）" % (th, k, k * 100.0 / n))
print()
print("=== 曾经浮盈到多少（MFE）===")
print("  最高 %+.1f%%   中位 %+.1f%%" % (mfes[-1], mfes[n // 2]))
print()
print("=== 关键判断：25% 熔断有意义吗 ===")
over25 = sum(1 for m in maes if m <= -25)
print("  浮亏曾达到 -25%% 的交易：%d 笔" % over25)
if over25 == 0:
    print("  => 6.84 年里一笔都没有。25%% 止损从未也不可能保护任何东西。")
    print("     它不是风控，是个从未上岗的保险丝。")
print()
print("=== 尾部：最难受的 5 笔 ===")
for t_ in sorted(trades, key=lambda x: x["mae"])[:5]:
    print("  MAE %+6.1f%%  最终 %+6.1f%%  持 %d 根(%.1f 天)  MFE %+5.1f%%"
          % (t_["mae"], t_["ret"], t_["bars"], t_["bars"] * 4 / 24.0, t_["mfe"]))
