# -*- coding: utf-8 -*-
"""第 4 步 · 事件层回填。

从表 0（线位）+ 表 1（K 线事实）+ 表 2（碰到几条线）派生事件，
落 `kbar_event`，并把 `event / prev_event / on_line / since_event_idx / state`
写回表 1。

只读 `data/kbar.db`，写也只写它 —— 生产库 `market.db` / `market_range.db` 一律不碰。

用法：
    python tools/backfill_kbar_events.py
    python tools/backfill_kbar_events.py --rebuild
    python tools/backfill_kbar_events.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import build_and_persist_events, event_params_snapshot  # noqa: E402


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def fmt(ms):
    import datetime
    if ms is None:
        return "-"
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--rebuild", action="store_true", help="先删掉本 symbol/interval 的事件")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s（先跑前 3 步的回填）" % a.kbar_db)

    hr("0. 前置检查")
    from ai_trader.kbar.store import KBarStore
    with KBarStore(a.kbar_db) as st:
        n_line = len(st.line_series(a.symbol, a.interval))
        n_facts = len(st.evolution_rows(a.symbol, a.interval))
        n_rx = st.reaction_counts(a.symbol, a.interval)
    print("  表 0 有 %d 条线" % n_line)
    print("  表 1 有 %d 根事实" % n_facts)
    print("  表 2 有 %d 行反应 / %d 根" % (n_rx["rows"], n_rx["bars"]))
    if not n_line:
        raise SystemExit("表 0 是空的，先跑 tools/backfill_kbar_lines.py")
    if not n_facts:
        raise SystemExit("表 1 是空的，先跑 tools/backfill_kbar_facts.py")
    if not n_rx["rows"]:
        raise SystemExit("表 2 是空的，先跑 tools/backfill_kbar_reactions.py")

    hr("1. 派生 + 落库")
    stats = build_and_persist_events(a.kbar_db, a.symbol, a.interval,
                                    rebuild=a.rebuild, dry_run=a.dry_run)
    if stats.get("error"):
        raise SystemExit(stats["error"])
    print("  输入根数=%d   事件行=%d   有事件的根=%d"
          % (stats["bars_in"], stats["rows"], stats["bars_with_event"]))
    if a.dry_run:
        print("  --dry-run：没有写库")
    else:
        print("  写入=%d 行   表 1 那几列回填=%d 根"
              % (stats["persisted"], stats.get("bar_cols_written")))

    hr("2. 事件分布")
    for k, v in (stats.get("by_event") or {}).items():
        print("    %-16s %5d" % (k, v))

    hr("3. 丢弃清单（哪些 (根,线) 对没参与判定、为什么）")
    for k, v in (stats.get("skipped") or {}).items():
        print("    %-28s %6d" % (k, v))
    print("\n  注意：这里只列「没法判定」的对。")
    print("  「线存在但收盘没换边」不是丢弃 —— 那是**没有事件**，本身就是事实。")

    hr("4. 三态（每根 K 线落在哪一类）")
    n = stats["bars_in"]
    print("  有事件            %6d  %5.1f%%" % (stats["bars_with_event"],
                                             100.0 * stats["bars_with_event"] / n))
    print("  有接触但无事件    %6d  %5.1f%%" % (stats["bars_touch_no_event"],
                                             100.0 * stats["bars_touch_no_event"] / n))
    print("  neutral（无接触） %6d  %5.1f%%" % (stats["bars_neutral"],
                                             100.0 * stats["bars_neutral"] / n))
    print("\n  为什么 neutral 不是 §8.2 的全文：")
    print("    §8.2 = 「没碰到任何观察线、量正常、在空间中间」。")
    print("    后两项都要阈值 = 判断标准，第 5 步之前不许碰（见 D-17）。")
    print("    所以这里只用无阈值的那一半：没碰到任何线 → neutral。")

    if a.dry_run:
        return 0

    hr("5. 从库里读回（不信内存变量）")
    with KBarStore(a.kbar_db) as st:
        c = st.event_counts(a.symbol, a.interval)
    print("  表 2.5 行数=%d  覆盖 %d 根" % (c["rows"], c["bars"]))
    print("\n  表 1 的填充率:")
    ev = c["evolution"]
    print("    event 非空        %6d / %6d  (%5.1f%%)"
          % (ev["with_event"], ev["bars"], 100.0 * ev["with_event"] / ev["bars"]))
    print("    state='neutral'   %6d / %6d  (%5.1f%%)"
          % (ev["neutral"], ev["bars"], 100.0 * ev["neutral"] / ev["bars"]))
    print("    state 非空        %6d / %6d  (%5.1f%%)"
          % (ev["with_state"], ev["bars"], 100.0 * ev["with_state"] / ev["bars"]))
    print("\n  按线种类:")
    for k, v in (c.get("by_line_kind") or {}).items():
        print("    %-16s %5d" % (k, v))

    hr("6. 这次用到的口径（已写进 kbar_meta）")
    print(json.dumps(event_params_snapshot(a.interval), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
