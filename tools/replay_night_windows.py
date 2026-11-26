# -*- coding: utf-8 -*-
"""一夜复盘 step 6：为什么窗口这么少。

`build_open_windows` 依赖三样输入：
  · range_evolution 的 (tag, direction)  —— 按 1h 整点
  · bars_1h 的量能（带量突破）
  · 区间线（line_of）在 1h 收盘时已就绪
逐个查覆盖率，定位是哪一样让窗口几乎不出现。
"""
from __future__ import annotations

import os
import sys
import time

BASE = "/home/cryptobot/ai-trader"
sys.path.insert(0, BASE)

from replay_night import M1H, fmt, parse_dt, ro, RANGE_DB, KBAR_DB  # noqa


def main():
    t0 = parse_dt(sys.argv[1]) if len(sys.argv) > 1 else parse_dt("2026-11-20T00:00")
    t1 = parse_dt(sys.argv[2]) if len(sys.argv) > 2 else int(time.time() * 1000)

    con = ro(RANGE_DB)
    print("== range_evolution ==")
    n, mn, mx = con.execute(
        "select count(*), min(ts), max(ts) from range_evolution"
        " where symbol='BTCUSDT'").fetchone()
    print("  行数 %s  范围 %s ~ %s" % (n, fmt(mn) if mn else "-",
                                      fmt(mx) if mx else "-"))
    print("  tag 分布：")
    for r in con.execute(
            "select tag, direction, count(*) c from range_evolution"
            " where symbol='BTCUSDT' group by tag, direction order by c desc"):
        print("    %-14s %-6s %6d" % (r[0], r[1], r[2]))
    print("  ★ 本时段（%s 之后）的 tag 分布：" % fmt(t0))
    for r in con.execute(
            "select tag, direction, count(*) c from range_evolution"
            " where symbol='BTCUSDT' and ts>=? group by tag, direction"
            " order by c desc", (t0,)):
        print("    %-14s %-6s %6d" % (r[0], r[1], r[2]))

    print("\n== bars_1h ==")
    n2, mn2, mx2 = con.execute(
        "select count(*), min(ts), max(ts) from bars_1h"
        " where symbol='BTCUSDT'").fetchone()
    print("  行数 %s  范围 %s ~ %s" % (n2, fmt(mn2) if mn2 else "-",
                                      fmt(mx2) if mx2 else "-"))
    print("  本时段行数：", con.execute(
        "select count(*) from bars_1h where symbol='BTCUSDT' and ts>=?",
        (t0,)).fetchone()[0])
    con.close()

    con = ro(KBAR_DB)
    print("\n== 区间线（kbar_line）就绪情况 ==")
    for r in con.execute(
            "select line_key, count(*) c, max(ts) mx from kbar_line"
            " where symbol='BTCUSDT' group by line_key order by c desc"):
        print("  %-24s %6d  最新 %s" % (r[0], r[1], fmt(r[2])))
    con.close()


if __name__ == "__main__":
    main()
