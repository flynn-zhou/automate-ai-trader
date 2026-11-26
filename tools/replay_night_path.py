# -*- coding: utf-8 -*-
"""一夜复盘 step 2：价格路径 + 每根 15m 的形态检测明细。

回答两个问题：
  1. 哪一根该开仓却没开（形态成立 vs 计划没产出 vs 被闸拦住）
  2. 系统现状能不能开出来
"""
from __future__ import annotations

import os
import sys
import time

BASE = "/home/cryptobot/ai-trader"
sys.path.insert(0, BASE)

from replay_night import (M15, M5, load_evolution, load_1h, load_tags,   # noqa
                          load_h1_vols, production_params, ro, KBAR_DB,
                          RANGE_DB, SYMBOL, fmt, parse_dt)


def main():
    t0 = parse_dt(sys.argv[1]) if len(sys.argv) > 1 else parse_dt("2026-11-25T21:00")
    t1 = parse_dt(sys.argv[2]) if len(sys.argv) > 2 else parse_dt("2026-11-26T08:15")

    b15 = [b for b in load_evolution(M15) if t0 <= b["ts"] <= t1]
    print("== 15m 价格路径 ==")
    prev = None
    for b in b15:
        chg = "" if prev is None else " %+7.1f" % (b["c"] - prev)
        bar = "" if prev is None else ("  UP" if b["c"] > prev else "  DN")
        print("  %s  O%9.1f H%9.1f L%9.1f C%9.1f  v%9.1f%s%s"
              % (fmt(b["ts"]), b["o"], b["h"], b["l"], b["c"], b["v"], chg, bar))
        prev = b["c"]

    print()
    print("== 整段统计 ==")
    lo = min(b["l"] for b in b15)
    hi = max(b["h"] for b in b15)
    print("  区间 %.1f ~ %.1f（%.1f 点 / %.2f%%）"
          % (lo, hi, hi - lo, (hi - lo) / lo * 100.0))
    print("  起点 %.1f → 终点 %.1f（%+.2f%%）"
          % (b15[0]["c"], b15[-1]["c"],
             (b15[-1]["c"] - b15[0]["c"]) / b15[0]["c"] * 100.0))


if __name__ == "__main__":
    main()
