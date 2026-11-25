# -*- coding: utf-8 -*-
"""K 线演化 · 第 3 步（反应层）回填。

从表 0（`kbar_line`）+ 表 1（`kbar_evolution`）派生表 2（`kbar_reaction`），
并把「每根碰到几条线」回填到表 1 的 `n_touch`。

前置：
    第 1 步已回填  python tools/backfill_kbar_lines.py --rebuild ...
    第 2 步已回填  python tools/backfill_kbar_facts.py  --rebuild ...

用法：
    python tools/backfill_kbar_reactions.py --rebuild
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B      # noqa: E402
from ai_trader.kbar.store import KBarStore  # noqa: E402


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def fmt(ms):
    if not ms:
        return "-"
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="K 线演化第 3 步：反应层回填")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--kbar-db", default=B.DEFAULT_KBAR_DB)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    iv_ms = {"5m": 300_000, "3m": 180_000, "15m": 900_000, "30m": 1_800_000,
             "1h": 3_600_000}.get(a.interval)
    if iv_ms is None:
        print("不认识的周期: %s" % a.interval)
        return 2

    hr("0. 前置检查（两张来源表都得在）")
    with KBarStore(a.kbar_db) as st:
        c0 = st.counts(a.symbol, iv_ms)
        c1 = st.evolution_counts(a.symbol, iv_ms)
    print("  表 0 kbar_line      %6d 行 / %5d 根  %s .. %s"
          % (c0["lines"], c0["bars"], fmt(c0["first_ts"]), fmt(c0["last_ts"])))
    print("  表 1 kbar_evolution %6d 行 / %5d 根  %s .. %s"
          % (c1["bars"], c1["bars"], fmt(c1["first_ts"]), fmt(c1["last_ts"])))
    if not c0["lines"]:
        print("\n  表 0 是空的，先跑 tools/backfill_kbar_lines.py")
        return 1
    if not c1["bars"]:
        print("\n  表 1 是空的，先跑 tools/backfill_kbar_facts.py")
        return 1

    hr("1. 派生 + 落库")
    stats = B.build_and_persist_reactions(a.kbar_db, a.symbol, iv_ms,
                                          rebuild=a.rebuild, dry_run=a.dry_run)
    if stats.get("error"):
        print("  失败：%s" % stats["error"])
        return 1
    print("  输入根数=%s  评估 (根,线) 对=%s  碰到=%s  进表=%s"
          % (stats.get("bars_in"), stats.get("pairs_evaluated"),
             stats.get("touch"), stats.get("rows")))
    if a.rebuild:
        print("  重建前删除=%s 行" % stats.get("deleted"))
    if stats.get("dry_run"):
        print("  --dry-run：没有写库")
        return 0
    print("  写入=%s 行   n_touch 回填=%s 根" % (stats.get("persisted"),
                                                stats.get("n_touch_bars")))

    hr("2. 丢弃清单（哪些 (根,线) 对没进表、为什么）")
    sk = stats.get("skipped") or {}
    if sk:
        for k in sorted(sk):
            print("    %-28s %8d" % (k, sk[k]))
    else:
        print("    （无）")
    print("\n  没进表的 (根,线) 对里，绝大多数是「线存在但没碰到」——")
    print("  那不是一种反应，它的事实留在表 0。这里只列「线还不存在」的那些。")

    hr("3. 每根碰到几条线")
    print("  一根都没碰到的根: %s / %s"
          % (stats.get("bars_without_touch"), stats.get("bars_in")))
    print("\n  按线种类的触碰行数:")
    for k, n in sorted((stats.get("by_kind") or {}).items()):
        print("    %-18s %7d" % (k, n))

    hr("4. 从库里读回（不信内存变量）")
    with KBarStore(a.kbar_db) as st:
        c = st.reaction_counts(a.symbol, iv_ms)
        c1b = st.evolution_counts(a.symbol, iv_ms)
    print("  表 2 行数=%d  覆盖 %d 根  %s .. %s"
          % (c["rows"], c["bars"], fmt(c["first_ts"]), fmt(c["last_ts"])))
    print("\n  各字段填充率:")
    for k in ("side", "pen_depth_pct", "recover_pct", "close_side",
              "dwell_bars", "vol_rel", "taker_buy_ratio", "nth_touch"):
        n = c["filled"][k]
        print("    %-16s 有值 %6d / %6d  (%5.1f%%)"
              % (k, n, c["rows"], 100.0 * n / max(1, c["rows"])))
    print("\n  表 1 的 n_touch: 有值 %d / %d"
          % (c1b["filled"].get("n_touch", 0), c1b["bars"]))

    print("\n  结论")
    print("  表 2 的 dwell_bars 是**故意留空**的：")
    print("    「贴着」需要距离阈值（属判断标准），待确定 D-8 —— 见 docs/K线演化-待确定清单.md")
    print("  nth_touch 按设计稿 §6 表 2 实现：键 (fam, seg, line_key)，按区间演化换段从 1 重数。")
    print("    段缺失的根（range_evolution 起点之前）留空并计 nth_touch:no_segment ——")
    print("    那不是「第一次」，是「不知道第几次」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
