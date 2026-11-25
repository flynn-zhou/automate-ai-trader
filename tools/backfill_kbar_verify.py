# -*- coding: utf-8 -*-
"""表 6 · 佐证层回填（用**后面的** K 线验当时的判断）。

    「判断对不对，只能用后面的 K 线来判。」—— 用户 2026-11-08

它做的事：读表 5（当时的结论）→ 把每个结论词翻译成一句**可被推翻的话**
→ 拿 `bars[t+1 .. t+N]` 去核对 → 写表 6 `kbar_verify`。

⚠ 这是全项目**唯一**读未来 K 线的地方，和表 3（forward）同一条红线：
   判定路径（ai_trader/kbar/shape.py）永远不许 import 本层，有测试扫源码。
   **单向**：表 5 是输入，表 6 是旁注；佐证结果绝不回写表 5。

只读 `data/kbar.db`，写也只写它 —— 生产库 market.db / market_range.db 一律不碰。

用法：
    python tools/backfill_kbar_verify.py
    python tools/backfill_kbar_verify.py --rebuild
    python tools/backfill_kbar_verify.py --dry-run
    python tools/backfill_kbar_verify.py --horizons 1,3
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import (build_and_persist_verify,  # noqa: E402
                           verify as V)
from ai_trader.kbar.store import KBarStore  # noqa: E402


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def pct(a, b):
    return 0.0 if not b else 100.0 * a / b


def bj(ms):
    if ms is None:
        return "-"
    return datetime.datetime.utcfromtimestamp(
        (int(ms) + 8 * 3600 * 1000) / 1000.0).strftime("%Y-%m-%d %H:%M")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--horizons", default=None,
                    help="后面看几根，逗号分隔（默认 %s）"
                         % ",".join(str(h) for h in V.VERIFY_HORIZONS))
    ap.add_argument("--rebuild", action="store_true",
                    help="先删掉本 symbol/interval/每个 N 的表 6（默认就按 N 覆盖写）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s（先跑前 5 步的回填）" % a.kbar_db)
    horizons = ([int(x) for x in a.horizons.split(",") if x.strip()]
                if a.horizons else list(V.VERIFY_HORIZONS))

    hr("0. 前置检查")
    with KBarStore(a.kbar_db) as st:
        n_shape = st.shape_counts(a.symbol, a.interval)
        n_verify = st.verify_counts(a.symbol, a.interval)
        rows1 = st.evolution_rows(a.symbol, a.interval)
    n_ohlc = sum(1 for r in rows1 if r["o"] is not None)
    print("  表 1 有 %d 根有 OHLC（佐证要靠它们当未来）" % n_ohlc)
    print("  表 5 有 %d 行结论 %s"
          % (n_shape["rows"],
             ("（指纹 %s）" % ",".join(n_shape["params_hashes"]))
             if n_shape["params_hashes"] else ""))
    print("  表 6 现有 %d 行 %s"
          % (n_verify["rows"],
             ("（本层指纹 %s）" % ",".join(n_verify["verify_hashes"]))
             if n_verify["verify_hashes"] else ""))
    if not n_shape["rows"]:
        raise SystemExit("表 5 是空的，先跑 tools/backfill_kbar_shape.py")
    print("\n  本次口径：N=%s  容差=%.0f 点  预言规则=%s"
          % (",".join(str(h) for h in horizons), V.VERIFY_TOL_PTS,
             V.PRED_RULES_VERSION))
    print("    ← 容差 0 是刻意的：用户给的 20 只属于 after_pin 那一个场景，")
    print("      原话「我设置的这个容差只在这一个场景生效！别扩散！」")

    hr("1. 翻译预言 + 裁决 + 落库")
    stats = build_and_persist_verify(a.kbar_db, a.symbol, a.interval,
                                     horizons=horizons, rebuild=a.rebuild,
                                     dry_run=a.dry_run)
    if stats.get("error"):
        raise SystemExit(stats["error"])
    print("  表 5 结论=%d 行   要写=%d 行（× %d 个 N）"
          % (stats["shape_rows"], stats["rows"], len(stats["horizons"])))
    print("  本层口径指纹=%s" % stats["verify_hash"])
    if a.dry_run:
        print("  --dry-run：没有写库")
    else:
        print("  写入=%d 行" % stats["persisted"])

    hr("2. 裁决分布（读回库里，不信内存）")
    with KBarStore(a.kbar_db) as st:
        c = st.verify_counts(a.symbol, a.interval)
    for h in sorted(c["by_horizon"]):
        slot = c["by_horizon"][h]
        tot = sum(slot.values())
        print("  N=%d（共 %d 行）" % (h, tot))
        for v in V.VERDICT_ORDER:
            k = slot.get(v, 0)
            print("    %-8s %6d  %5.2f%%" % (v, k, pct(k, tot)))
        denom = slot.get(V.VERDICT_HELD, 0) + slot.get(V.VERDICT_BROKE, 0)
        if denom:
            print("    → 只在「成立+推翻」上算命中：%d/%d = %.1f%%"
                  % (slot.get(V.VERDICT_HELD, 0), denom,
                     pct(slot.get(V.VERDICT_HELD, 0), denom)))
        print("    ⚠ 这个数**不能单独看** —— 必须报同口径基准率，否则"
              "「N=1 命中 73%%」会被当成「七成准」，")
        print("      而「随便一根本根低点后面不被破」本来就有 53%%。见 probe 的「提升量」。")

    hr("3. label × N 的裁决交叉表")
    with KBarStore(a.kbar_db) as st:
        allrows = st.verify_rows(a.symbol, a.interval)
    tbl = {}
    for r in allrows:
        tbl.setdefault(r["label"], {}).setdefault(int(r["horizon"]), {})[
            r["verdict"]] = tbl.setdefault(r["label"], {}).setdefault(
                int(r["horizon"]), {}).get(r["verdict"], 0) + 1
    heads = sorted(set(int(r["horizon"]) for r in allrows))
    print("  %-12s %-4s %6s %6s %6s %6s %8s"
          % ("label", "N", "成立", "推翻", "无法判定", "无预言", "命中"))
    for lab in sorted(tbl):
        for h in heads:
            slot = tbl[lab].get(h, {})
            tot = sum(slot.values())
            denom = slot.get(V.VERDICT_HELD, 0) + slot.get(V.VERDICT_BROKE, 0)
            hr_ = "-" if not denom else "%.1f%%" % pct(
                slot.get(V.VERDICT_HELD, 0), denom)
            print("  %-12s %-4d %6d %6d %6d %6d %8s"
                  % (lab, h, slot.get(V.VERDICT_HELD, 0),
                     slot.get(V.VERDICT_BROKE, 0),
                     slot.get(V.VERDICT_UNKNOWN, 0),
                     slot.get(V.VERDICT_NO_PRED, 0), hr_))

    hr("4. 抽查：每根把「当时说的话」和「后面怎么裁决」并排打出来")
    want = []
    for lab in V_ORDER_LABELS:
        got = [r for r in allrows
               if r["label"] == lab and int(r["horizon"]) == horizons[0]]
        if got:
            want.append(got[0])
    for r in want:
        print("\n  %s  label=%s  states=%s" % (bj(r["ts"]), r["label"], r["states"]))
        print("    预言：%s" % r["pred_text"])
        print("    N=%d 裁决：%s" % (r["horizon"], r["verdict"]))
        if r["actual_price"] is not None and r["ref_price"] is not None:
            print("    价位：参照 %.1f  实际 %.1f  差 %+.1f 点（容差 %.0f）"
                  % (r["ref_price"], r["actual_price"], r["diff_pts"], r["tol_pts"]))
        elif r["actual_price"] is not None:
            print("    价位：实际最大偏离 %.1f 点（这句话的尺是「1 倍本根全幅」）"
                  % r["actual_price"])

    hr("5. 本层口径（已写进 kbar_meta）")
    print(json.dumps(V.verify_params(), ensure_ascii=False, indent=2))
    print("\n⚠ 改了 N / 容差 / 预言规则就重跑本工具；verify_hash 会变，")
    print("  check_kbar_pipeline.py 会把「表 5 新、表 6 旧」判红。")
    return 0


# 抽查时按 STATE_ORDER 的顺序挑（「无」放最后）
V_ORDER_LABELS = ("恐慌盘", "插针收回", "插针", "插针后稳住", "针后远离", "无·真空", "无·贴线", "无")


if __name__ == "__main__":
    sys.exit(main())
