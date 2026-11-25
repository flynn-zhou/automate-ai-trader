# -*- coding: utf-8 -*-
"""K 线演化 · 第 1 步（观察线）全历史回填。

第 3 步的反应层从表 0（`kbar_line`）派生，而表 0 原先只有 1,700 根 15m
（2026-10-20 起，当时探针用 --limit 跑的）。表 1 已经有 17,584 根，
两张表按 ts 关联，覆盖差一个数量级就没法派生。

所以这里用**和事实层完全同一份合并序列**重建表 0：
同一份 o/h/l/c 意味着表 0 的 `bar_close` 与表 1 的 `c` 逐根一致，
`dist_pct` 与表 1 的 `c - p` 对得上，不会出现「两张表对同一天的价格不一样」。

用法：
    python tools/backfill_kbar_lines.py --rebuild \
        --market-db <生产只读快照> --range-db <range 只读快照>
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B          # noqa: E402
from ai_trader.kbar import lines as L          # noqa: E402
from ai_trader.kbar.store import KBarStore     # noqa: E402

DEFAULT_BACKTEST_DB = os.path.join(ROOT, "data", "backtest.db")
DEFAULT_CSV = os.path.join(ROOT, "data", "range_bars_15m.csv")


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def fmt(ms):
    if not ms:
        return "-"
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="K 线演化第 1 步：观察线全历史回填")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--market-db", default=B.DEFAULT_MARKET_DB)
    ap.add_argument("--backtest-db", default=DEFAULT_BACKTEST_DB)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--range-db", default=B.DEFAULT_RANGE_DB)
    ap.add_argument("--kbar-db", default=B.DEFAULT_KBAR_DB)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    iv_ms = {"5m": 300_000, "3m": 180_000, "15m": 900_000, "30m": 1_800_000,
             "1h": 3_600_000}.get(a.interval)
    if iv_ms is None:
        print("不认识的周期: %s（支持 5m/3m/15m/30m/1h）" % a.interval)
        return 2
    sym = a.symbol

    hr("0. 取数（与事实层用同一份合并序列）")
    srcA = (B.load_market_bars_rich(a.market_db, sym, iv_ms)
            if os.path.exists(a.market_db) else [])
    srcB = (B.load_1m_as_15m(a.backtest_db, iv_ms)
            if os.path.exists(a.backtest_db) else [])
    srcC = B.load_csv_bars(a.csv) if os.path.exists(a.csv) else []
    print("  A market.db=%d  B 1m聚合=%d  C csv=%d" % (len(srcA), len(srcB), len(srcC)))
    bars16, mrep = B.merge_bars(srcA, srcB, srcC)
    print("  合并后 %d 根  %s .. %s（冲突 %d 处，事实层已逐条报过）"
          % (len(bars16), fmt(bars16[0]["ts"]) if bars16 else "-",
             fmt(bars16[-1]["ts"]) if bars16 else "-", len(mrep["conflicts"])))

    range_rows = (B.load_range_rows(a.range_db, sym)
                  if os.path.exists(a.range_db) else [])
    print("  range_evolution: %d 行 %s .. %s"
          % (len(range_rows), fmt(range_rows[0]["ts"]) if range_rows else "-",
             fmt(range_rows[-1]["ts"]) if range_rows else "-"))

    bars = [L.Bar(b["ts"], b["o"], b["h"], b["l"], b["c"], b.get("v"))
            for b in bars16]

    hr("1. 计算 + 落库")
    stats = B.build_and_persist(a.kbar_db, sym, iv_ms, bars, range_rows,
                                rebuild=a.rebuild,
                                sources=[a.market_db, a.range_db],
                                dry_run=a.dry_run)
    print("  输入=%s 根   产出线=%s 行   落库=%s"
          % (stats.get("bars_in"), stats.get("lines"), stats.get("persisted")))
    if a.rebuild:
        print("  重建前删除=%s 行" % stats.get("deleted"))

    hr("2. 丢弃清单（每根为什么少了线）")
    sk = stats.get("skipped") or {}
    if sk:
        for k in sorted(sk):
            print("    %-28s %8d" % (k, sk[k]))
    else:
        print("    （无）")
    bk = stats.get("by_kind") or {}
    print("\n  产出的线种类:")
    for k in sorted(bk):
        print("    %-18s %8d 行" % (k, bk[k]))

    if a.dry_run:
        print("\n  --dry-run：没有写库")
        return 0

    hr("3. 从库里读回")
    with KBarStore(a.kbar_db) as st:
        c = st.counts(sym, iv_ms)
    print("  表 0 行数=%d  覆盖 %d 根 K 线  %s .. %s"
          % (c["lines"], c["bars"], fmt(c["first_ts"]), fmt(c["last_ts"])))
    for k, n in sorted(c["by_kind"].items()):
        print("    %-18s %8d" % (k, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
