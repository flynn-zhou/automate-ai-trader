# -*- coding: utf-8 -*-
"""第 5 步 · 事后层回填（**独立进程**）。

    ⚠ 这一步是事后的。「之后涨了没有」本身就是事后信息 ——
      所以它单独跑、单独一张表、单独一个模块。
      **不要在任何实时/当下判定的入口里调它。**

从表 1（OHLC + 事件五列）+ 表 2.5（事件的判定线价）派生表 3 `kbar_forward`。

只读 `data/kbar.db`，写也只写它 —— 生产库 `market.db` / `market_range.db` 一律不碰。

用法：
    python tools/backfill_kbar_forward.py
    python tools/backfill_kbar_forward.py --rebuild
    python tools/backfill_kbar_forward.py --dry-run
    python tools/backfill_kbar_forward.py --held-bars 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import (build_and_persist_forward,  # noqa: E402
                           forward_params_snapshot)
from ai_trader.kbar.store import KBarStore  # noqa: E402


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def pct(a, b):
    return 0.0 if not b else 100.0 * a / b


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--rebuild", action="store_true", help="先删掉本 symbol/interval 的表 3")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--held-bars", type=int, default=None, help="覆盖 HELD_BARS")
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s（先跑前 4 步的回填）" % a.kbar_db)

    hr("0. 前置检查")
    with KBarStore(a.kbar_db) as st:
        n_facts = len(st.evolution_rows(a.symbol, a.interval))
        n_ev = st.event_counts(a.symbol, a.interval)
        n_fw = st.forward_counts(a.symbol, a.interval)
        rows = st.evolution_rows(a.symbol, a.interval)
    n_event_bars = sum(1 for r in rows if r.get("event") is not None)
    print("  表 1 有 %d 根事实，其中 %d 根有事件" % (n_facts, n_event_bars))
    print("  表 2.5 有 %d 条事件 / %d 根" % (n_ev["rows"], n_ev["bars"]))
    print("  表 3 现在有 %d 行（这次会覆盖/删除重算）" % n_fw["rows"])
    if not n_facts:
        raise SystemExit("表 1 是空的，先跑 tools/backfill_kbar_facts.py")
    if n_event_bars and not n_ev["rows"]:
        raise SystemExit("表 1 有事件但表 2.5 是空的，先跑 tools/backfill_kbar_events.py")

    hr("1. 派生 + 落库")
    stats = build_and_persist_forward(a.kbar_db, a.symbol, a.interval,
                                      rebuild=a.rebuild, dry_run=a.dry_run,
                                      held_bars=a.held_bars)
    if stats.get("error"):
        raise SystemExit(stats["error"])
    n = stats["bars_in"]
    print("  输入根数=%d   表 3 行数=%d" % (n, stats["rows"]))
    if a.dry_run:
        print("  --dry-run：没有写库")
    else:
        print("  写入=%d 行   删旧=%s" % (stats["persisted"], stats.get("deleted", 0)))

    hr("2. 未来收益（ret_k）的填充情况")
    print("  口径：ret_k = c[t + k*interval] / c[t] - 1，收盘对收盘，不扣费")
    for k in stats["params"]["horizons"]:
        have = n - stats["ret%d_null" % k]
        print("    ret%-2d 有值 %6d  (%5.1f%%)   末尾不足 %d 根＝%d"
              % (k, have, pct(have, n), k, stats["ret%d_null" % k]))
    print("\n  注意：末尾那几根是**结构上算不出来**（后面没有 K 线了），不是丢数据。")

    hr("3. 守住（held）")
    print("  口径：之后 %d 根的收盘是否一直接在「事件判定那一侧」"
          % stats["params"]["held_bars"])
    print("        参照价 = 事件判定当时那个线价（固定不变），容差 %.6f"
          % stats["params"]["held_tol_pct"])
    with_ev = stats["bars_with_event"]
    print("    有事件的根        %6d" % with_ev)
    print("      held = 1        %6d  (%5.1f%%)"
          % (stats["held_1"], pct(stats["held_1"], with_ev)))
    print("      held = 0        %6d  (%5.1f%%)"
          % (stats["held_0"], pct(stats["held_0"], with_ev)))
    print("      held 空（算不出）%6d  (%5.1f%%)"
          % (stats["held_null"], pct(stats["held_null"], with_ev)))
    if stats.get("held_ref_missing"):
        print("    拿不到参照价的根  %6d  ← 表 2.5 里没有 (ts, on_line) 那一行"
              % stats["held_ref_missing"])
    print("\n  为什么会有「算不出」：后面不足 %d 根、或当根收盘正好压在线上（容差 0）。"
          % stats["params"]["held_bars"])
    print("  为什么无事件的根 held 是空的：**「守住」的主语是某条线**；")
    print("  这根没有事件就没有线，填 0 等于说「它没守住一个不存在的线」。")

    if a.dry_run:
        hr("4. 这次用到的口径（dry-run，未写入 kbar_meta）")
        print(json.dumps(forward_params_snapshot(a.interval), ensure_ascii=False, indent=2))
        return 0

    hr("4. 从库里读回（不信内存变量）")
    with KBarStore(a.kbar_db) as st:
        c = st.forward_counts(a.symbol, a.interval)
        sample = st.forward_rows(a.symbol, a.interval)
    print("  表 3 行数=%d  覆盖 %d 根" % (c["rows"], c["rows"]))
    print("  填充率：")
    for col in ("ret1", "ret3", "ret5", "held", "held_line", "held_ref_price"):
        print("    %-16s %6d / %6d  (%5.1f%%)"
              % (col, c[col], c["rows"], pct(c[col], c["rows"])))
    print("  held 分布：%s" % c["by_held"])

    hr("5. 抽 8 根有事件的根，把真值打出来")
    shown = 0
    for r in sample:
        if r["held"] is None:
            continue
        print("    ts=%-14d held=%d  side=%-5s  ref=%-12s ret1=%-10s ret3=%-10s "
              "line=%s"
              % (r["ts"], r["held"], r["held_side"], r["held_ref_price"],
                 "None" if r["ret1"] is None else "%.6f" % r["ret1"],
                 "None" if r["ret3"] is None else "%.6f" % r["ret3"],
                 r["held_line"]))
        shown += 1
        if shown >= 8:
            break

    hr("6. 这次用到的口径（已写进 kbar_meta）")
    print(json.dumps(forward_params_snapshot(a.interval), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
