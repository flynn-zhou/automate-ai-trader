# -*- coding: utf-8 -*-
"""回填表 6b `kbar_verify_survival` —— 每根判断「活了多久」。

用法：
    python tools/backfill_kbar_survival.py                 # 全量
    python tools/backfill_kbar_survival.py --dry-run       # 只算不写
    python tools/backfill_kbar_survival.py --tail          # 只补尾部（增量）

`--tail` 就是「实时跟随」那条路：新 K 线进来后只重算还可能变的那几根。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import build as B           # noqa: E402
from ai_trader.kbar import verify as V          # noqa: E402
from ai_trader.kbar.store import KBarStore      # noqa: E402


def hr(t):
    print("\n" + "=" * 88)
    print(t)
    print("=" * 88)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--max-n", type=int, default=V.SURVIVAL_MAX_N)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tail", action="store_true", help="只补尾部（增量）")
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s" % a.kbar_db)

    hr("0. 口径")
    print("  库        %s" % a.kbar_db)
    print("  最大窗口  max_n = %d 根（同周期）" % a.max_n)
    print("  容差      %.1f 点" % V.VERIFY_TOL_PTS)
    print("  预言规则  %s" % V.PRED_RULES_VERSION)
    print("  口诀指纹  survival_hash = %s" % V.survival_hash())
    print("  三态：已破（第几根破）/ 活到最后（满 max_n 没破）/ 未成熟（没看完）")

    hr("1. 回填")
    if a.tail:
        st = B.update_verify_tail(a.kbar_db, a.symbol, a.interval)
        print("  增量：总根数 %d，从 %s 起重算 %d 根"
              % (st.get("bars", 0), st.get("since_ts"), st.get("recompute_bars", 0)))
        for k in ("verify", "survival"):
            d = st.get(k) or {}
            print("  %-9s 重算 %d 行，落库 %d 行，库里共 %d 行%s"
                  % (k, d.get("rows", 0), d.get("persisted", 0),
                     (d.get("db_after") or {}).get("rows", 0),
                     ("  ⚠ " + d["error"]) if d.get("error") else ""))
    else:
        d = B.build_and_persist_survival(a.kbar_db, a.symbol, a.interval,
                                         max_n=a.max_n, dry_run=a.dry_run)
        if d.get("error"):
            print("  ⚠ " + d["error"])
        print("  根数      %d" % d.get("rows", 0))
        print("  落库      %d%s" % (d.get("persisted", 0),
                                  "（dry-run 没写）" if a.dry_run else ""))
        print("  各态      %s" % d.get("by_status"))

    hr("2. 库里的样子")
    with KBarStore(a.kbar_db) as st:
        c = st.survival_counts(a.symbol, a.interval)
        print("  总行数    %d" % c["rows"])
        print("  定型      %d（已破 %d + 活到最后 %d）"
              % (c["settled"], c["by_status"].get("已破", 0),
                 c["by_status"].get("活到最后", 0)))
        print("  未成熟    %d ← 表尾，**没验完，不许算进分母**"
              % c["pending"])
        print("  指针      %s" % c["survival_hashes"])
        if len(c["survival_hashes"]) > 1:
            print("  ⚠ 混着多套口径，必须清理重算")
        print("\n  按预言种类：")
        for k, v in (c["by_pred_kind"] or {}).items():
            print("    %-14s %6d" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
