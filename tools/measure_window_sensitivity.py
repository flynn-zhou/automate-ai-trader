# -*- coding: utf-8 -*-
"""开仓窗口阈值的**敏感性测量**（只测量，不调参）。

★ 这个脚本**不是**用来挑最优格的。
  项目纪律（HANDOFF §八）明写：网格搜索挑最优格 = 挑到的是噪声极值。
  这里唯一的目的是回答一个问题：

      默认的 vol_mult=1.5 / vol_win=24 把窗口压到 0.48% 的时间，
      到底是「形态本来就罕见」，还是「门槛把正常机会挡掉了」？

  判断方法不是看哪个格子出计划最多（那正是过拟合），而是看
  **窗口数量随门槛变化的形状**：
      · 平坦 → 门槛不是瓶颈，样本少就是市场就这样
      · 陡峭 → 门槛可调，需要在生产更长数据上另做验证（不是在这里挑）

  测完**不改默认值**。阈值仍是未验证初值。

用法：
    python tools/measure_window_sensitivity.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai_trader.trading import pending_plan as P      # noqa: E402

HOUR = 3_600_000

VOL_MULTS = (1.0, 1.2, 1.5, 2.0, 3.0)
VOL_WINS = (12, 24, 48)
#: 当前默认值 —— 表里用 ★ 标出来
DEFAULT_CELL = (1.5, 24)


def _load_smoke():
    """复用冒烟脚本的数据装载器，避免两处写法漂掉。"""
    path = os.path.join(_ROOT, "tools", "_smoke_pending_plan.py")
    spec = importlib.util.spec_from_file_location("smoke", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def measure(smoke, b15, b5, b1h, book, tags, span_h,
            vol_mult, vol_win):
    wins = smoke.build_windows(b1h, book, tags,
                               vol_mult=vol_mult, vol_win=vol_win)
    hours = sum((w.end_ms - w.start_ms) / HOUR for w in wins)
    by_dir = Counter(w.direction for w in wins)
    lengths = [(w.end_ms - w.start_ms) / HOUR for w in wins]

    ctx = P.PlanContext(bars15=b15, book=book, bars_1h=b1h, bars_5m=b5,
                        windows=wins)
    plans, _ = P.detect_all(ctx, require_window=True, include_mirror=True)

    kinds = Counter(p.kind for p in plans)
    wicks = sum(n for k, n in kinds.items() if "三引线" in k)
    invv = sum(n for k, n in kinds.items() if "倒V" in k)
    ema = sum(n for k, n in kinds.items() if "EMA" in k)
    return {
        "windows": len(wins),
        "hours": hours,
        "pct": 100.0 * hours / span_h if span_h else 0.0,
        "down": by_dir.get(P.DOWN, 0),
        "up": by_dir.get(P.UP, 0),
        "med_len": (sorted(lengths)[len(lengths) // 2] if lengths else 0.0),
        "plans": len(plans),
        "wicks": wicks,
        "invv": invv,
        "ema": ema,
    }


def main():
    smoke = _load_smoke()
    b15 = smoke.load_evolution(900_000)
    b5 = smoke.load_evolution(300_000)
    b1h = smoke.load_bars_1h()
    book = smoke.load_line_book()
    tags = smoke.load_range_state()
    span_h = (b15[-1].ts - b15[0].ts) / HOUR if len(b15) > 1 else 0.0

    print("=" * 96)
    print("开仓窗口阈值敏感性（只测量，不调参；默认值不会被改动）")
    print("=" * 96)
    print("样本：15m %d 根 / 5m %d 根 / 1h %d 根，跨度 %.1f 小时"
          % (len(b15), len(b5), len(b1h), span_h))
    print("窗口定义：1h 收盘带量跌破 range_soft_dn@1h → 紧跟的同向 forming 段")
    print()

    head = ("vol_mult", "vol_win", "窗口段", "覆盖小时", "占比%",
            "DOWN", "UP", "中位段长h", "计划", "三引线", "倒V", "EMA拒绝")
    line = "%-9s %-8s %6s %9s %7s %5s %4s %9s %6s %7s %5s %7s"
    print(line % head)
    print("-" * 96)

    rows = {}
    for vm in VOL_MULTS:
        for vw in VOL_WINS:
            r = measure(smoke, b15, b5, b1h, book, tags, span_h, vm, vw)
            rows[(vm, vw)] = r
            star = " ★" if (vm, vw) == DEFAULT_CELL else "  "
            print(line % ("%g%s" % (vm, star), vw, r["windows"],
                          "%.1f" % r["hours"], "%.2f" % r["pct"],
                          r["down"], r["up"], "%.1f" % r["med_len"],
                          r["plans"], r["wicks"], r["invv"], r["ema"]))
        print("-" * 96)

    # ---- 形状分析：门槛到底是不是瓶颈
    base = rows[DEFAULT_CELL]
    print("\n[形状] 沿 vol_mult 方向（vol_win 固定 24）")
    print(line % head)
    for vm in VOL_MULTS:
        r = rows[(vm, 24)]
        star = " ★" if (vm, 24) == DEFAULT_CELL else "  "
        print(line % ("%g%s" % (vm, star), 24, r["windows"],
                      "%.1f" % r["hours"], "%.2f" % r["pct"],
                      r["down"], r["up"], "%.1f" % r["med_len"],
                      r["plans"], r["wicks"], r["invv"], r["ema"]))

    print("\n[结论口径] 只看形状，不看谁的计划多")
    lo = rows[(1.0, 24)]
    hi = rows[(3.0, 24)]
    print("  vol_mult 1.0 → 3.0：窗口 %d → %d 段，覆盖 %.2f%% → %.2f%%"
          % (lo["windows"], hi["windows"], lo["pct"], hi["pct"]))
    if base["windows"] == 0:
        print("  ★ 默认值下窗口为 0 —— 门槛过紧，或这段数据里根本没出现过该形态")
    elif lo["windows"] <= max(2, base["windows"]):
        print("  ★ 门槛放到最松也没有明显增加窗口 —— **门槛不是瓶颈**，")
        print("    窗口稀缺是市场结构决定的，不该往调参方向想。")
    elif hi["windows"] == 0:
        print("  ★ 门槛收紧到 3.0 就一个窗口都没有 —— 默认值附近很敏感，")
        print("    这正是「阈值是未验证初值」的含义：**换一段数据结论就会变**。")
    else:
        print("  ★ 窗口数随门槛单调下降但未塌陷 —— 门槛是有效约束，")
        print("    但仍需在生产更长数据上复核，不许在这里挑最小/最大格。")
    print("\n★ 本轮**不改任何默认值**。要改必须走「新假设 + 独立验证」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
