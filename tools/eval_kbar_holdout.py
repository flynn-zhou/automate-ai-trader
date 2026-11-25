# -*- coding: utf-8 -*-
"""在**留出集**上评测「某个维度在某个结论词里有没有区分力」。

用法：
    python tools/eval_kbar_holdout.py                 # 只读评测，不登记
    python tools/eval_kbar_holdout.py --register      # 评测并登记（用掉留出集）
    python tools/eval_kbar_holdout.py --train         # 对照组：同一套格子跑训练段

⚠ 留出集只能用一次。`--register` 跑完，那段样本就「脏」了。
   之后的评测会在「第几次尝试」里显示出来。

⚠ 这个工具**不改任何判定**。它只回答「这个维度在这段没参与决定的样本上，
   还有没有区分力」。要把维度升级成判据，还要过总账 §九 的另外两关。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import dims as D          # noqa: E402
from ai_trader.kbar import holdout as H       # noqa: E402
from ai_trader.kbar.store import KBarStore    # noqa: E402

DB = "data/kbar.db"
SYM, IV = "BTCUSDT", 900000
LABELS = ("恐慌盘", "插针", "插针收回", "插针后稳住", "针后远离",
          "无·真空", "无·贴线")


def hr(t):
    print("\n" + "=" * 92)
    print(t)
    print("=" * 92)


def bj(ms):
    if ms is None:
        return "-"
    return datetime.datetime.utcfromtimestamp(ms / 1000.0 + 8 * 3600).strftime(
        "%Y-%m-%d %H:%M")


def load_rows(db, sym, iv):
    """把表 7（维度快照）与表 6b（存活结局）按 ts 拼起来。

    为什么要拼：表 7 有 39 个维度和 label，但没有结局；表 6b 有结局
    （已破/活到最后/must_hold），但没有维度。评测需要两边都在。
    """
    with KBarStore(db) as st:
        dims_rows = st.dims_rows(sym, iv)
        surv = {int(r["ts"]): r for r in st.survival_rows(sym, iv)}
    out = []
    for r in dims_rows:
        s = surv.get(int(r["ts"]))
        if s is None:
            continue
        out.append({
            "ts": int(r["ts"]),
            "label": r["label"],
            "values": json.loads(r["values_json"]),
            "buckets": json.loads(r["buckets_json"]),
            "status": s["status"],
            "must_hold": s["must_hold"],
            "break_n": s.get("break_n"),
        })
    out.sort(key=lambda x: x["ts"])
    return out


def report(rows, split, title):
    """在给定段上跑全部格子并打印。"""
    hr(title)
    per = H.evaluate_all(rows, LABELS)
    print("  这一段 %d 根" % len(rows))
    print("  候选格 %d 个，其中**可信**（聚类数 G≥%d）%d 个，聚类太少丢掉 %d 个"
          % (per["n_cells_all"], H.MIN_CLUSTERS, per["n_tested"],
             per["n_dropped_few_clusters"]))
    print("  ★ z_cluster≥%.2f 过线 %d 格（**主口径**，按 %d 根一块去相关）"
          % (H.Z_CRIT, per["n_pass_cluster"], H.BLOCK_STEP))
    print("    z_naive 过线 %d 格（与现有工具同口径）" % per["n_pass_naive"])
    print("    z_block 过线 %d 格（每 %d 根只留 1 根，只作对照）"
          % (per["n_pass_block"], H.BLOCK_STEP))
    print("  纯靠运气该有 %.1f 格过线    Bonferroni 门槛 z≥%.2f → 剩 %d 格"
          % (per["n_expected_by_chance"], per["z_bonferroni"],
             per["n_pass_bonferroni"]))
    if per["n_tested"]:
        print("  若全是噪声，至少过 %d 格的概率 p = %.4g"
              % (per["n_pass_cluster"], per["p_at_least_pass_cluster"]))

    for p in per["per_label"]:
        if not p["n_settled"]:
            print("\n  ── %s：这一段没有定型行" % p["label"])
            continue
        print("\n  ── %s  n=%d 定型=%d  基准命中=%.1f%%"
              % (p["label"], p["n"], p["n_settled"], p["base_pct"]))
        if not p["cells"]:
            print("     （没有达到 MIN_N=%d 的格子）" % H.MIN_N)
            continue
        print("     %-16s %-8s %5s %4s %6s %8s %9s %5s"
              % ("维度", "取值", "n", "G", "命中",
                 "z_naive", "z_cluster", "压小"))
        for c in p["cells"][:8]:
            zc = c["z_cluster"]
            sh = (abs(c["z_naive"] / zc) if zc else float("nan"))
            print("     %-16s %-8s %5d %4d %5.1f%% %+8.2f %9s %5s %s"
                  % (c["dim"], c["val"], c["n"], c["n_clusters"],
                     c["hit_pct"], c["z_naive"],
                     ("%+.2f" % zc) if zc is not None else "聚类不足",
                     ("%.1f" % sh) if zc else "-",
                     "★" if c["pass_cluster"] else ""))
        rest = len(p["cells"]) - 8
        if rest > 0:
            print("     …… 另有 %d 格（聚类够的排前面，再按 |z_cluster|）" % rest)
    return per


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--symbol", default=SYM)
    ap.add_argument("--interval-ms", type=int, default=IV)
    ap.add_argument("--register", action="store_true",
                    help="评测并登记（登记 = 留出集用掉一次）")
    ap.add_argument("--train", action="store_true",
                    help="对照组：同一套格子跑训练段")
    ap.add_argument("--note", default=None, help="这次评测想验什么")
    a = ap.parse_args(argv)

    rows = load_rows(a.db, a.symbol, a.interval_ms)
    if not rows:
        print("表 7 或表 6b 没数据 —— 先跑 backfill_kbar_dims.py")
        return 1

    split = H.make_split([r["ts"] for r in rows])
    if split.get("error"):
        print("❌ %s" % split["error"])
        return 1

    hr("0. 冻结切分（这份凭证要落库，改它就是新版本）")
    print("  全段    %s → %s   %d 根  (%.0f%%%% 训练)"
          % (bj(split["train_first_ts"]), bj(split["holdout_last_ts"]),
             split["n"], H.TRAIN_FRAC * 100))
    print("  训练段  %s → %s   %d 根"
          % (bj(split["train_first_ts"]), bj(split["train_last_ts"]),
             split["train_n"]))
    print("  embargo %s → %s   %d 根  ← 前视窗隔离，不进任何一段"
          % (bj(split["embargo_first_ts"]), bj(split["embargo_last_ts"]),
             split["embargo_n"]))
    print("  留出段  %s → %s   %d 根"
          % (bj(split["holdout_first_ts"]), bj(split["holdout_last_ts"]),
             split["holdout_n"]))
    print("  split_hash = %s   dims_hash = %s"
          % (split["split_hash"], D.dims_hash()))

    # 已经评测过几次
    with KBarStore(a.db) as st:
        used = st.holdout_run_count(a.symbol, a.interval_ms,
                                    split["split_hash"])
    print("\n  这个切分已被评测过 %d 次" % used)
    if used and not a.train:
        print("  ⚠ 留出集已经脏了。这次的结果要注明是「第 %d 次尝试」。" % (used + 1))

    hr("1. 各段 label 分布（先看两段像不像，再谈统计）")
    dist = {}
    for ph in ("train", "embargo", "holdout"):
        c = {}
        for r in rows:
            if H.phase_of(r["ts"], split) == ph:
                c[r["label"]] = c.get(r["label"], 0) + 1
        dist[ph] = c
    print("  %-12s %8s %8s %8s" % ("结论词", "训练段", "留出段", "占比差"))
    for lab in LABELS:
        tr = dist["train"].get(lab, 0)
        ho = dist["holdout"].get(lab, 0)
        pt = 100.0 * tr / max(1, split["train_n"])
        ph2 = 100.0 * ho / max(1, split["holdout_n"])
        print("  %-12s %8d %8d %+8.2f 个百分点" % (lab, tr, ho, ph2 - pt))
    print("  ⚠ 两段分布差太多，说明市场状态变了，留出段的结论不能直接外推。")

    if a.train:
        trows = [r for r in rows if H.phase_of(r["ts"], split) == "train"]
        report(trows, split, "2. 对照组：训练段（%d 根）" % len(trows))
        print("\n  ⚠ 训练段的结果**不是证据** —— 它只是用来跟留出段对比，")
        print("     看「同样的格子换一段数据还剩几格」。")
        return 0

    hrows = [r for r in rows if H.phase_of(r["ts"], split) == "holdout"]
    per = report(hrows, split, "2. 留出段评测（%d 根，没参与过任何决定）"
                 % len(hrows))

    hr("3. 怎么读这张表")
    print("  · 先看「过线几格 vs 纯靠运气该有几格」。过线的没比运气多，")
    print("    说明这些维度的区分力全是噪声 —— 这时候不要去看哪一格 z 最大。")
    print("  · 再看 Bonferroni 那行。验了 %d 格，单格要过 z≥%.2f 才算。"
          % (per["n_tested"], per["z_bonferroni"]))
    print("  · z_naive 只用来跟以前报过的数对齐，**不要拿它下结论**。")
    print("  · 过线的格子只是「候选」，还不是判据。升级见总账 §九。")

    if a.register:
        rec = {
            "symbol": a.symbol, "interval_ms": int(a.interval_ms),
            "split_version": split["version"],
            "split_hash": split["split_hash"], "dims_hash": D.dims_hash(),
            "train_n": split["train_n"], "holdout_n": split["holdout_n"],
            "embargo_n": split["embargo_n"],
            "n_tested": per["n_tested"],
            "n_pass_block": per["n_pass_block"],
            "n_pass_cluster": per["n_pass_cluster"],
            "n_pass_naive": per["n_pass_naive"],
            "n_expected": per["n_expected_by_chance"],
            "z_bonferroni": per["z_bonferroni"],
            "n_pass_bonf": per["n_pass_bonferroni"],
            "note": a.note,
        }
        with KBarStore(a.db) as st:
            rid = st.add_holdout_log(rec)
            st.set_meta("holdout_split:%s:%s"
                        % (a.symbol, "%d" % a.interval_ms), split)
        hr("4. 已登记（留出集用掉一次）")
        print("  日志行 id=%d" % rid)
        print("  这个切分累计评测 %d 次" % (used + 1))
        if used + 1 > 1:
            print("  ⚠ 这是第 %d 次。之前那几次的结论都要一起看，不能只报最好看的。"
                  % (used + 1))
    else:
        print("\n（没加 --register：这次评测不登记，留出集没用掉）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
