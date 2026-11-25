# -*- coding: utf-8 -*-
"""表 5 · 读数层回填（把判断落库）。

用户 2026-11-08 的要求：「所有演化结论字段或者描述都要入 db 库！这个落盘数据库是必须的」。

它做的事：读表 1（OHLC + 量能）+ 表 0（当时的线快照）→ 每根算一句判断
（插针 / 插针收回 / 恐慌盘 / 插针后稳住 / 针后远离 / 无）→ 写表 5 `kbar_shape`。

⚠ 本层**不使用未来数据**，所以它和第 1~4 步在同一侧。
  但它自己也**不许** import forward（有测试扫）。

只读 `data/kbar.db`，写也只写它 —— 生产库 market.db / market_range.db 一律不碰。

用法：
    python tools/backfill_kbar_shape.py
    python tools/backfill_kbar_shape.py --rebuild
    python tools/backfill_kbar_shape.py --dry-run
    python tools/backfill_kbar_shape.py --no-swing          # 关掉局部极值针
    python tools/backfill_kbar_shape.py --allow-cross-bar    # 打开跨根收回（默认关）
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import (build_and_persist_shape,  # noqa: E402
                           shape_params_snapshot)
from ai_trader.kbar.store import KBarStore  # noqa: E402


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def pct(a, b):
    return 0.0 if not b else 100.0 * a / b


def bj(ms):
    """UTC 毫秒 → 北京时间字符串。库里全是 UTC，人读要北京。"""
    if ms is None:
        return "-"
    return datetime.datetime.utcfromtimestamp(
        (int(ms) + 8 * 3600 * 1000) / 1000.0).strftime("%Y-%m-%d %H:%M")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--rebuild", action="store_true", help="先删掉本 symbol/interval 的表 5")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--no-swing", action="store_true",
                    help="不把「局部极值」当针（退回只认单根长影线）")
    ap.add_argument("--allow-cross-bar", action="store_true",
                    help="打开「跨根收回」（默认关：实测会命中 37%% 的根，见 D-21）")
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s（先跑前 5 步的回填）" % a.kbar_db)

    hr("0. 前置检查")
    with KBarStore(a.kbar_db) as st:
        rows = st.evolution_rows(a.symbol, a.interval)
        n_line = st.count("kbar_line", a.symbol, a.interval)
        n_shape = st.shape_counts(a.symbol, a.interval)
    n_facts = len(rows)
    n_ohlc = sum(1 for r in rows if r["o"] is not None)
    n_vol = sum(1 for r in rows if r.get("vol_rel") is not None)
    print("  表 1 有 %d 行，其中 %d 根有 OHLC、%d 根有 vol_rel" % (n_facts, n_ohlc, n_vol))
    print("  表 0 有 %d 行线（判「收回」要用当时那份快照）" % n_line)
    print("  表 5 现在有 %d 行 %s" % (n_shape["rows"],
                                  ("（指纹 %s）" % ",".join(n_shape["params_hashes"]))
                                  if n_shape["params_hashes"] else ""))
    if not n_ohlc:
        raise SystemExit("表 1 没有 OHLC，先跑 tools/backfill_kbar_facts.py")
    if n_vol < n_ohlc:
        print("\n  ⚠ 有 %d 根没有 vol_rel —— 恐慌盘判不出来（量能是它的必要条件）。"
              % (n_ohlc - n_vol))
        print("    这些根的结论里会写明「量能或主动性缺失」，不会静默当「不是恐慌盘」。")

    hr("1. 判 + 落库")
    stats = build_and_persist_shape(a.kbar_db, a.symbol, a.interval,
                                    rebuild=a.rebuild, dry_run=a.dry_run,
                                    allow_swing=not a.no_swing,
                                    allow_cross_bar=a.allow_cross_bar)
    if stats.get("error"):
        raise SystemExit(stats["error"])
    print("  输入根数=%d   要写行数=%d   口径指纹=%s"
          % (stats["bars_in"], stats["rows"], stats["params_hash"]))
    print("  局部极值针=%s   跨根收回=%s"
          % ("开" if stats["allow_swing"] else "关",
             "开" if stats["allow_cross_bar"] else "关"))
    if a.dry_run:
        print("  --dry-run：没有写库")
    else:
        print("  写入=%d 行   删旧=%s" % (stats["persisted"], stats.get("deleted", 0)))

    hr("2. 结论分布（label 只取一个，按优先级）")
    n = stats["bars_in"]
    for k in list(stats["params"]["states"]):
        v = stats["by_label"].get(k, 0)
        print("    %-8s %6d  %5.2f%%" % (k, v, pct(v, n)))
    print("    %-8s %6d  %5.2f%%" % ("合计", sum(stats["by_label"].values()),
                                    pct(sum(stats["by_label"].values()), n)))
    print("\n  多个状态同时成立的根：%d（label 只报最靠前的那个，其余在 states 里）"
          % stats["multi_state"])

    hr("3. 针的形态（这是 v2 与 v1 最大的不同）")
    p = stats["pins"]
    print("  本根自己是针：长影线 %d / 局部极值 %d" % (p["wick"], p["swing"]))
    print("    ⚠ 局部极值 0 是**设计如此**，不是没接线：")
    print("      若把「本根是极值」也算插针，全库 5,852 根（33.1%）都是插针 ——")
    print("      下跌趋势里每根新低都满足，那就不是形态了。")
    print("      极值只当「稳住/远离」的参照针，见下面这行。")
    r = stats["ref_pins"]
    print("  稳住/远离参照的针：长影线 %d / 局部极值 %d" % (r["wick"], r["swing"]))
    if r["swing"]:
        print("    ← 这 %d 根就是 v1 判不出来的：v1 只认单根长影线，"
              "多根下跌打出的低点它根本看不见。" % r["swing"])

    if a.dry_run:
        hr("4. 这次用到的口径（dry-run，未写入 kbar_meta）")
        print(json.dumps(shape_params_snapshot(a.interval), ensure_ascii=False, indent=2))
        return 0

    hr("4. 从库里读回（不信内存变量）")
    with KBarStore(a.kbar_db) as st:
        c = st.shape_counts(a.symbol, a.interval)
        sample = st.shape_rows(a.symbol, a.interval)
    print("  表 5 行数=%d  vs  表 1 的 %d 根：%s"
          % (c["rows"], stats["bars_in"],
             "一致 OK" if c["rows"] == stats["bars_in"] else "不一致 ← 错！"))
    print("  覆盖 %s .. %s（北京）" % (bj(c["first_ts"]), bj(c["last_ts"])))
    print("  口径指纹：%s  —— %s"
          % (",".join(c["params_hashes"]),
             "单一口径 OK" if len(c["params_hashes"]) == 1 else "混着多套 ← 错！"))
    print("  label 分布：%s" % c["by_label"])

    hr("5. 抽查：结论里 states 与 label 是否自洽")
    # 「无」这一族（无 / 无·真空 / 无·贴线）的 label 本来就不在 states 里 ——
    # 它表达的是「一个形态都没成立」，不是形态之一。见 shape.LABEL_NO_ALL。
    from ai_trader.kbar import shape as _S
    bad = [r for r in sample
           if r["label"] not in _S.LABEL_NO_ALL
           and r["label"] not in (r["states"] or "").split(",")]
    bad2 = [r for r in sample if r["n_states"] != len(
        [s for s in (r["states"] or "").split(",") if s])]
    # 拆词之后不应该再有裸的「无」—— 有就说明构建时没把 n_touch 传进 judge
    bare = [r for r in sample if r["label"] == _S.STATE_NONE]
    print("  label 不在 states 里：%d 行 %s" % (len(bad), "OK" if not bad else "← 错！"))
    print("  n_states 与 states 条数不符：%d 行 %s" % (len(bad2), "OK" if not bad2 else "← 错！"))
    print("  裸「无」（未拆词）：%d 行 %s"
          % (len(bare), "OK" if not bare else "← 错！说明 n_touch 没传进 judge"))
    if bad:
        r = bad[0]
        print("    例：ts=%s label=%s states=%r" % (r["ts"], r["label"], r["states"]))

    hr("6. 抽 6 根有结论的根，把落库的真值打出来")
    shown = 0
    for r in sample:
        if r["label"] in _S.LABEL_NO_ALL:
            continue
        print("\n    %s（ts=%d）  label=%s  states=%s"
              % (bj(r["ts"]), r["ts"], r["label"], r["states"]))
        print("      针：自己是 %s/%s   参照 %s(%s)"
              % (r["side"] or "-", r["pin_kind"] or "-",
                 bj(r["ref_pin_ts"]), r["ref_pin_kind"] or "-"))
        print("      结构：全幅=%.1f 实体=%.0f%% 下影=%.0f%% 上影=%.0f%% 收位=%.0f%%"
              % (r["rng"], pct(r["body_pct"], 1), pct(r["dn_wick_pct"], 1),
                 pct(r["up_wick_pct"], 1), pct(r["close_pos"], 1)))
        print("      量能：vol_rel=%s 主动买=%s   收回 %d 条"
              % (r["vol_rel"], r["taker_buy_ratio"], r["n_recovered"]))
        print("      证据（落库原文，前 3 行）：")
        for line in (r["evidence"] or "").split("\n")[:3]:
            print("        · %s" % line)
        shown += 1
        if shown >= 6:
            break

    hr("7. 这次用到的口径（已写进 kbar_meta）")
    print(json.dumps(shape_params_snapshot(a.interval), ensure_ascii=False, indent=2))
    print("\n⚠ 全部阈值都是**临时值**（D-21）。改了 shape.py 顶部常量就必须 --rebuild，")
    print("  否则表里会混着两套口径 —— check_kbar_pipeline.py 会把这种表判红。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
