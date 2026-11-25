# -*- coding: utf-8 -*-
"""建表 7（`kbar_dims`）：把 39 个维度摊成一根 K 线一份「环境快照」。

用法：
    python tools/backfill_kbar_dims.py              # 全量
    python tools/backfill_kbar_dims.py --dry-run    # 只算不写
    python tools/backfill_kbar_dims.py --tail       # 只补最近几根（实时跟随）

⚠ 这一层**不参与判定**。它不改结论词、不改阈值、不改表 5/6/6b。
   作用是把「判据没读、但库里一直躺着」的维度摊到人眼前。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import build as B          # noqa: E402
from ai_trader.kbar import dims as D           # noqa: E402
from ai_trader.kbar.store import KBarStore     # noqa: E402

DB = "data/kbar.db"
SYM, IV = "BTCUSDT", 900000


def hr(t):
    print("\n" + "=" * 88)
    print(t)
    print("=" * 88)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--symbol", default=SYM)
    ap.add_argument("--interval-ms", type=int, default=IV)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tail", action="store_true",
                    help="只补最近几根（新 K 线进来后跑这个）")
    ap.add_argument("--tail-bars", type=int, default=3)
    a = ap.parse_args()

    since_ts = None
    if a.tail:
        with KBarStore(a.db) as st:
            rows = st.evolution_rows(a.symbol, a.interval_ms)
        ts = sorted(int(r["ts"]) for r in rows if r["o"] is not None)
        if not ts:
            print("表 1 没有可用的根")
            return 1
        since_ts = ts[-max(1, a.tail_bars)]

    hr("0. 口径")
    print("  库          %s" % a.db)
    print("  品种/周期   %s @ %d ms" % (a.symbol, a.interval_ms))
    print("  维度数      %d 个（见 docs/K线演化维度总账.md §三）" % len(D.DIMS))
    print("  口径指纹    dims_hash = %s" % D.dims_hash())
    print("  ⚠ 这一层**不参与判定**：只摊开环境，不改任何结论词或阈值")
    print("  分档切点    1/3、2/3、1.0、0/1~2/3+ —— 都不是拟合出来的，只用于翻人话")

    hr("1. 构建")
    if since_ts is not None:
        print("  增量：从 ts=%d 起" % since_ts)
    out = B.build_and_persist_dims(a.db, a.symbol, a.interval_ms,
                                   dry_run=a.dry_run, since_ts=since_ts)
    if out.get("error"):
        print("  ❌ %s" % out["error"])
        return 1
    print("  算出的根数  %d" % out.get("rows", 0))
    print("  落库行数    %d" % out.get("persisted", 0))
    print("  库里总行数  %s" % out.get("total", out.get("persisted")))
    print("  旁证/矛盾条 %d" % out.get("n_notes", 0))
    if out.get("by_label"):
        print("  label 分布  %s" % json.dumps(
            dict(sorted(out["by_label"].items(), key=lambda x: -x[1])),
            ensure_ascii=False))

    if a.dry_run:
        print("\n（--dry-run：没写库）")
        return 0

    hr("2. 库里的样子")
    with KBarStore(a.db) as st:
        c = st.dims_counts(a.symbol, a.interval_ms)
        n_bars = st.count_rows("kbar_evolution", a.symbol, a.interval_ms)
    print("  表 7 行数   %d   （表 1 有 %d 根）" % (c["rows"], n_bars))
    print("  口径        %s" % (c["dims_hashes"] or "-"))
    print("  有值维度    min %s / max %s / avg %s"
          % (c["filled_min"], c["filled_max"], c["filled_avg"]))
    if c["rows"] != n_bars:
        print("  ⚠ 行数与表 1 不等 —— 有一根没算到，或跑过别的周期")

    hr("3. 抽 3 根看真值（不打印估计值）")
    with KBarStore(a.db) as st:
        rows = st.dims_rows(a.symbol, a.interval_ms)
    import datetime as _dt

    def bj(ms):
        return _dt.datetime.utcfromtimestamp(ms / 1000.0 + 8 * 3600).strftime(
            "%Y-%m-%d %H:%M")

    # 挑有旁证/矛盾提示的，和「有值维度最多」的各一根
    noted = [r for r in rows if json.loads(r["notes_json"])]
    rich = sorted(rows, key=lambda r: -int(r["n_filled"]))[:1]
    picks = []
    for r in (noted[:1] + rich + rows[-1:]):
        if r and r["ts"] not in [x["ts"] for x in picks]:
            picks.append(r)
    for r in picks:
        vals = json.loads(r["values_json"])
        bks = json.loads(r["buckets_json"])
        notes = json.loads(r["notes_json"])
        print("\n  %s（北京）  ts=%d  label=%s  有值 %d/%d"
              % (bj(r["ts"]), r["ts"], r["label"], r["n_filled"], r["n_dims"]))
        line = []
        for name in D.DIM_NAMES:
            v = vals.get(name)
            if v is None:
                continue
            b = bks.get(name) or ""
            line.append("%s=%s%s" % (name, v, ("（%s）" % b) if b else ""))
        # 每行 3 个，别糊成一坨
        for i in range(0, len(line), 3):
            print("      " + "   ".join(line[i:i + 3]))
        if notes:
            print("      ── 提示 ──")
            for n in notes:
                print("      [%s] %s：%s" % (n["kind"], n["dims"], n["text"]))

    hr("4. 提醒")
    print("  · 这一层是**说明**，不是判据。别拿这里的任何值去当判定条件。")
    print("  · 要把某个维度升级成判据，见总账 §九（必须过留出集那一关）。")
    print("  · 新 K 线进来后跑 `--tail` 即可，不需要全量重算。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
