# -*- coding: utf-8 -*-
"""K 线演化 · 第 2 步回填：事实层（结构 / 量能 / 内路径）→ kbar_evolution。

用法（默认就是把能拿到的 15m 历史全填了）：

    python tools/backfill_kbar_facts.py --rebuild
    python tools/backfill_kbar_facts.py --dry-run          # 只算不写
    python tools/backfill_kbar_facts.py --market-db <生产只读快照>   # 用快照补最近的 15m

它做三件事，每一件都必须能说清「少数出来的那些去哪了」：
  1. 合并三个 15m 来源（优先级 market.db > 1m 聚合 > CSV），冲突逐条打印；
  2. 按 K 线算结构 + 量能；3s 覆盖窗内再算内路径；
  3. 落库，然后从库里读回行数与**每个字段的填充率**（填充率是这一步的验收核心）。

三个来源为什么必须合并，而不是只用其中一个 —— 见 ai_trader/kbar/build.py 文件头。
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B          # noqa: E402
from ai_trader.kbar import facts as F          # noqa: E402
from ai_trader.kbar import lines as L          # noqa: E402
from ai_trader.kbar.store import KBarStore     # noqa: E402

DEFAULT_BACKTEST_DB = os.path.join(ROOT, "data", "backtest.db")
DEFAULT_CSV = os.path.join(ROOT, "data", "range_bars_15m.csv")


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def fmt(ms):
    if not ms:
        return "-"
    import datetime
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000.0).strftime("%Y-%m-%d %H:%M")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="K 线演化第 2 步：事实层回填")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--market-db", default=B.DEFAULT_MARKET_DB,
                    help="market.db 或它的只读快照（提供真实 15m + b/s + 3s 桶）")
    ap.add_argument("--backtest-db", default=DEFAULT_BACKTEST_DB,
                    help="backtest.db（提供 1m，用来补长历史的量能）")
    ap.add_argument("--csv", default=DEFAULT_CSV,
                    help="range_bars_15m.csv（只提供结构，最老的那段）")
    ap.add_argument("--range-db", default=B.DEFAULT_RANGE_DB)
    ap.add_argument("--kbar-db", default=B.DEFAULT_KBAR_DB)
    ap.add_argument("--no-1m", action="store_true", help="不用 1m 补量能")
    ap.add_argument("--no-csv", action="store_true", help="不用 CSV 补结构")
    ap.add_argument("--no-ticks", action="store_true", help="不算内路径")
    ap.add_argument("--rebuild", action="store_true", help="先删掉该周期的旧事实层再写")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--quiet-skip", action="store_true", help="不打印跳过清单明细")
    a = ap.parse_args(argv)

    iv_ms = {"5m": 300_000, "3m": 180_000, "15m": 900_000, "30m": 1_800_000,
             "1h": 3_600_000, "4h": 14_400_000}.get(a.interval)
    if iv_ms is None:
        print("不认识的周期: %s（支持 5m/3m/15m/30m/1h/4h）" % a.interval)
        return 2
    sym = a.symbol

    hr("0. 数据源自证（到底读了哪些库、各有多少根）")
    sources = [a.market_db, a.range_db]
    srcA, srcB, srcC = [], [], []

    if os.path.exists(a.market_db):
        srcA = B.load_market_bars_rich(a.market_db, sym, iv_ms)
        print("  A) market.db  %s  %d 根  %s .. %s"
              % (os.path.basename(a.market_db), len(srcA),
                 fmt(srcA[0]["ts"]) if srcA else "-",
                 fmt(srcA[-1]["ts"]) if srcA else "-"))
    else:
        print("  A) market.db  不存在（跳过）: %s" % a.market_db)

    if not a.no_1m and os.path.exists(a.backtest_db):
        srcB = B.load_1m_as_15m(a.backtest_db, iv_ms)
        inc = sum(1 for b in srcB if b.get("incomplete"))
        print("  B) backtest.db 1m 聚合  %d 根  %s .. %s   不完整桶=%d"
              % (len(srcB), fmt(srcB[0]["ts"]) if srcB else "-",
                 fmt(srcB[-1]["ts"]) if srcB else "-", inc))
        if a.backtest_db not in sources:
            sources.append(a.backtest_db)
    elif not a.no_1m:
        print("  B) backtest.db  不存在（跳过）")

    if not a.no_csv and os.path.exists(a.csv):
        srcC = B.load_csv_bars(a.csv)
        print("  C) %s  %d 根  %s .. %s（只有结构，无量）"
              % (os.path.basename(a.csv), len(srcC),
                 fmt(srcC[0]["ts"]) if srcC else "-",
                 fmt(srcC[-1]["ts"]) if srcC else "-"))
    elif not a.no_csv:
        print("  C) csv  不存在（跳过）")

    bars, mrep = B.merge_bars(srcA, srcB, srcC)
    print("\n  合并后: %d 根  %s .. %s" % (len(bars), fmt(bars[0]["ts"]), fmt(bars[-1]["ts"])))
    print("  各源命中根数: A=%s B=%s C=%s" % tuple(mrep["hits"]))
    print("  有量能的根数: %d   有主动买量的根数: %d"
          % (mrep["with_vol"], mrep["with_buy"]))

    hr("1. 源冲突（同一 min 上两源数值不符 —— 不静默选边，全部列出）")
    if not mrep["conflicts"]:
        print("  无冲突")
    else:
        print("  冲突 %d 处（保留高优先级源的值）:" % len(mrep["conflicts"]))
        for c in mrep["conflicts"][:20]:
            print("    ts=%s（%s）  保留源#%d  另一源#%d  差异=%s"
                  % (c["ts"], fmt(c["ts"]), c["kept_src"], c["seen_src"],
                     {k: "%.6f vs %.6f" % v for k, v in c["diff"].items()}))
        if len(mrep["conflicts"]) > 20:
            print("    ...还有 %d 处" % (len(mrep["conflicts"]) - 20))

    hr("2. 区间演化行 + 3s 内路径的取数范围")
    range_rows = []
    if os.path.exists(a.range_db):
        range_rows = B.load_range_rows(a.range_db, sym)
        print("  range_evolution: %d 行  %s .. %s"
              % (len(range_rows), fmt(range_rows[0]["ts"]) if range_rows else "-",
                 fmt(range_rows[-1]["ts"]) if range_rows else "-"))
    else:
        print("  range_evolution: 库不存在（区间位置将全部留空并计数）: %s" % a.range_db)

    ticks = {}
    if not a.no_ticks and os.path.exists(a.market_db):
        tick_lo, tick_hi = B.ticks_cover(a.market_db, sym, iv_ms)
        if tick_lo:
            bars_in = [b["ts"] for b in bars if tick_lo <= b["ts"] < tick_hi]
            print("  3s 桶覆盖: %s .. %s" % (fmt(tick_lo), fmt(tick_hi)))
            print("  落在覆盖窗内的 K 线: %d 根（窗外的内路径必为 NULL，会计入丢弃清单）"
                  % len(bars_in))
            if bars_in:
                ticks = B.load_ticks_3s(a.market_db, sym, min(bars_in),
                                        max(bars_in) + iv_ms, iv_ms)
                print("  载入 3s 桶 %d 根，覆盖 %d 根 K 线"
                      % (sum(len(v) for v in ticks.values()), len(ticks)))
        else:
            print("  3s 桶: 没有数据（内路径将全部留空）")

    hr("3. 计算 + 落库")
    stats = B.build_and_persist_facts(
        a.kbar_db, sym, iv_ms, bars, range_rows, ticks,
        rebuild=a.rebuild, sources=sources, dry_run=a.dry_run)
    print("  输入根数=%s   产出行数=%s   落库=%s"
          % (stats.get("bars_in"), stats.get("rows"), stats.get("persisted")))
    if a.rebuild:
        print("  重建前删除的旧行数=%s" % stats.get("deleted"))

    if not a.quiet_skip:
        hr("4. 丢弃清单（禁止静默过滤：每个空字段都要说得出为什么）")
        sk = stats.get("skipped") or {}
        if sk:
            for k in sorted(sk):
                print("    %-34s %8d" % (k, sk[k]))
        else:
            print("    （无逐根跳过）")
        print("    %-34s %8d" % ("内路径:整根没有 3s 桶", stats.get("no_ticks", 0)))
        print("    %-34s %8d" % ("内路径:缺桶但量自洽(conc 仍精确)",
                                stats.get("ticks_incomplete", 0)))
        print("    %-34s %8d" % ("内路径:3s 不是这根 K 的完整分解",
                                stats.get("ticks_vol_mismatch", 0)))
        print("    %-34s %8d" % ("内路径:K 线没有量（无法做分母）",
                                stats.get("bar_vol_missing", 0)))
        print("    %-34s %8d" % ("max_tick_share:需逐笔（不可算）",
                                stats.get("max_tick_share_undefined", 0)))
        print("    %-34s %8d" % ("path_shape:词表未定（判断标准）",
                                stats.get("path_shape_undefined", 0)))

    if a.dry_run:
        print("\n  --dry-run：没有写库")
        return 0

    hr("5. 从库里读回：行数 + 每个字段的填充率（不信内存变量）")
    with KBarStore(a.kbar_db) as st:
        c = st.evolution_counts(sym, iv_ms)
        meta = st.get_meta("facts_params:%s:%s" % (sym, L.period_label(iv_ms)))
    n = c["bars"] or 1
    print("  表 1 行数=%d  覆盖 %s .. %s" % (c["bars"], fmt(c["first_ts"]), fmt(c["last_ts"])))
    for col in ("range_ts_h", "zone", "vol_rel", "vol_slot_rel", "taker_buy_ratio",
                "conc", "t_low_min", "n_lines", "path_shape", "max_tick_share",
                "n_touch", "event"):
        v = c["filled"].get(col, 0)
        print("    %-16s 有值 %6d / %6d  (%5.1f%%)" % (col, v, c["bars"], 100.0 * v / n))
    print("\n  口径参数（已写进 kbar_meta）: %s" % ((meta or "")[:200]))

    hr("结论")
    ok = True
    if stats.get("rows", 0) == 0:
        print("  [FAIL] 一行都没产出")
        ok = False
    if c["bars"] and not c["filled"].get("range_ts_h"):
        print("  [FAIL] 区间位置一根都没填上（range_evolution 没接到）")
        ok = False
    if c["bars"] and not c["filled"].get("taker_buy_ratio"):
        print("  [FAIL] taker_buy_ratio 一根都没填上（量能没接到）")
        ok = False
    print("  %s" % ("全部检查通过。" if ok else "有检查未通过，见上。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
