# -*- coding: utf-8 -*-
"""归因：从「被推翻」的判断里，找 t 时刻**可观测但判定层还没用**的维度。

用户 2026-11-10 的要求（原话）：
    「我们用事后去看看能不能丰富当时的维度以及描述，开发时的参考意义，
      系统开发绝对要避免前视的问题」

所以本工具只回答一件事：**哪些维度和「判断对不对」有关，而且这个维度在
t 时刻就算得出来。**

两条硬约束，写在这里防止以后走偏：
  1. **只找维度，不调阈值。** 用户已明确「先不调阈值」。
  2. **候选维度必须 t 时刻可观测。** 凡是要看后面 K 线才能算的（比如
     「这次判断活了多久」），只能留在表 6 当诊断，**不许进表 5**。

当前判定层（shape.judge）实际只吃：OHLC + vol_rel + taker_buy_ratio。
表 1 的 zone/direction/conc/vol_slot_rel、表 2 整层、表 1.5 整层——一列没进。
它们全是 t 时刻可观测的，所以是这一轮要找的地方。

判定好坏的口径：表 6 的 N=1 裁决（看得最短、样本最全），
并且**只说提升量、不说命中率**（命中率不配基准就是废话）。
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import shape as S          # noqa: E402
from ai_trader.kbar import verify as V         # noqa: E402
from ai_trader.kbar.store import KBarStore     # noqa: E402

LABELS = ("恐慌盘", "插针收回", "插针", "插针后稳住", "针后远离", "无·真空", "无·贴线", "无")
MIN_N = 30          # 一个桶至少这么多根才算数


def hr(t):
    print("\n" + "=" * 96)
    print(t)
    print("=" * 96)


def pct(a, b):
    return 0.0 if not b else 100.0 * a / b


def bucket_of(key, val):
    """把一个特征落到可读的桶。None/缺失单独一桶，不许并进中心。"""
    if val is None:
        return "缺"
    if key == "vol_rel":
        return "缺" if val == 0 else ("<1.5" if val < 1.5 else
                                      "1.5~2.5" if val < 2.5 else
                                      "2.5~3.0" if val < 3.0 else ">=3.0")
    if key == "n_touch":
        return str(min(int(val), 6))
    if key == "since_event_idx":
        return "无事件" if val is None else ("0~2" if val <= 2 else
                                             "3~8" if val <= 8 else
                                             "9~32" if val <= 32 else ">32")
    if key == "dist_amp":
        if val is None:
            return "不参照针"
        return ("已破" if val < 0 else "0~0.25" if val <= 0.25 else
                "0.25~0.5" if val <= 0.5 else "0.5~0.75" if val <= 0.75 else
                "0.75~1.0" if val <= 1.0 else ">1.0")
    if key == "conc":
        return "缺" if val is None else ("<0.02" if val < 0.02 else
                                         "0.02~0.05" if val < 0.05 else ">=0.05")
    if key == "vol_slot_rel":
        return "缺" if val is None else ("<2" if val < 2 else
                                         "2~5" if val < 5 else ">=5")
    if key in ("event", "prev_event", "zone", "direction", "fam"):
        return "-" if val in (None, "") else str(val)
    return str(val)


FEATURES = ("event", "prev_event", "since_event_idx", "zone", "direction",
            "n_touch", "dist_amp", "vol_rel", "vol_slot_rel", "conc")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--horizon", type=int, default=1,
                    help="用哪个 N 的裁决当「对不对」（默认 1）")
    a = ap.parse_args(argv)

    with KBarStore(a.kbar_db) as st:
        facts = st.evolution_rows(a.symbol, a.interval)
        shp = st.shape_rows(a.symbol, a.interval)
        vfy = st.verify_rows(a.symbol, a.interval)
    bars = [{"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
             "vol_rel": r.get("vol_rel"),
             "taker_buy_ratio": r.get("taker_buy_ratio")}
            for r in facts if r["o"] is not None]
    bars.sort(key=lambda x: int(x["ts"]))
    n = len(bars)
    G = [S.geometry(b["o"], b["h"], b["l"], b["c"]) for b in bars]
    idx = {int(b["ts"]): i for i, b in enumerate(bars)}

    # ---- 组装：每根一个特征字典 + 它的裁决 ----
    evo = {int(r["ts"]): r for r in facts if r["o"] is not None}
    rows = []
    for r in shp:
        i = idx.get(int(r["ts"]))
        if i is None:
            continue
        e = evo.get(int(r["ts"]), {})
        # dist_amp：这一步只用 [0..t]（参照针在 t 之前，价格在 t）
        dist_amp = None
        if r["ref_pin_ts"] is not None:
            j = idx.get(int(r["ref_pin_ts"]))
            if j is not None:
                gp = G[j]
                amp = max(gp.rng, S.WICK_PTS_MIN)
                sd = r["ref_pin_side"] or S.pin_side(gp) \
                    or S.swing_pin_kind(bars, j, i - 1)
                if sd == S.SIDE_DOWN:
                    dist_amp = (G[i].l - gp.l) / amp
                else:
                    dist_amp = (gp.h - G[i].h) / amp
        rows.append({
            "ts": int(r["ts"]), "i": i, "label": r["label"],
            "states": (r["states"] or ""),
            "event": e.get("event"), "prev_event": e.get("prev_event"),
            "since_event_idx": e.get("since_event_idx"),
            "zone": e.get("zone"), "direction": e.get("direction"),
            "fam": e.get("fam"), "n_touch": e.get("n_touch"),
            "vol_rel": e.get("vol_rel"), "vol_slot_rel": e.get("vol_slot_rel"),
            "conc": e.get("conc"), "dist_amp": dist_amp,
        })

    # 裁决（只有 HELD/BROKE 进分母）
    verd = {}
    for v in vfy:
        if int(v["horizon"]) != a.horizon:
            continue
        if v["verdict"] in (V.VERDICT_HELD, V.VERDICT_BROKE):
            verd[int(v["ts"])] = v
    for r in rows:
        r["v"] = verd.get(r["ts"])

    ok = [r for r in rows if r["v"] is not None]
    hr("0. 口径")
    print("  特征来自『t 时刻可观测』的表 1 / 表 1.5 / 表 2：%s"
          % "、".join(FEATURES))
    print("  好坏看表 6 N=%d 的裁决（成立/推翻）；样本 %d 根"
          % (a.horizon, len(ok)))
    print("  判据：一个维度有用 = 在不同取值下**提升量**差很多。")
    print("        命中率差不看（不同桶的基准不同，直接比命中率是错的）。")
    print("  桶内 < %d 根不报，避免小样本噪声。" % MIN_N)

    # ---- 每桶的同口径基准 ----
    def base_rate(r):
        """这个桶里「随便一根」的命中率 —— 用同一问句对所有根算。"""
        p = r.get("_pred")
        if p is None or p.kind == V.PRED_NONE:
            return None
        N = a.horizon
        i = r["i"]
        if i + N >= n:
            return None
        fut = range(i + 1, i + 1 + N)
        cnt = tot = 0
        for k in range(n - N):
            if p.kind == V.PRED_QUIET:
                g = G[k]
                amp = max(g.rng, S.WICK_PTS_MIN)
                ext = max(max(abs(bars[m]["h"] - bars[k]["c"]),
                              abs(bars[m]["l"] - bars[k]["c"])) for m in fut_of(k, N))
                cnt += 1 if ext < amp else 0
            elif p.side == S.SIDE_DOWN:
                cnt += 1 if min(bars[m]["l"] for m in fut_of(k, N)) > bars[k]["l"] else 0
            else:
                cnt += 1 if max(bars[m]["h"] for m in fut_of(k, N)) < bars[k]["h"] else 0
            tot += 1
        return pct(cnt, tot)

    def fut_of(k, N):
        return range(k + 1, min(n, k + 1 + N))

    # 预先算好每根的预言（用表 5 落库的字段，不重判）
    by_ts = {int(r["ts"]): r for r in shp}
    for r in rows:
        s = by_ts[r["ts"]]
        jd = S.Judgement(
            states=tuple(x for x in (s["states"] or "").split(",") if x),
            label=s["label"], side=s["side"],
            evidence=tuple((s["evidence"] or "").splitlines()),
            pin_ts=s["ref_pin_ts"], pin_kind=s["pin_kind"],
            ref_pin_kind=s["ref_pin_kind"], ref_pin_side=s["ref_pin_side"])
        r["_pred"] = V.predict(bars, r["i"], jd, ref_pin_side=s["ref_pin_side"])

    # 全局基准（按预言种类分别算，再按样本加权）
    cache = {}

    def base_for(r):
        p = r["_pred"]
        key = (p.kind, p.side)
        if key not in cache:
            N = a.horizon
            hit = tot = 0
            for k in range(n - N):
                if p.kind == V.PRED_QUIET:
                    g = G[k]
                    amp = max(g.rng, S.WICK_PTS_MIN)
                    ext = max(max(abs(bars[m]["h"] - bars[k]["c"]),
                                  abs(bars[m]["l"] - bars[k]["c"]))
                              for m in fut_of(k, N))
                    hit += 1 if ext < amp else 0
                elif p.side == S.SIDE_DOWN:
                    hit += 1 if min(bars[m]["l"] for m in fut_of(k, N)) > bars[k]["l"] else 0
                else:
                    hit += 1 if max(bars[m]["h"] for m in fut_of(k, N)) < bars[k]["h"] else 0
                tot += 1
            cache[key] = pct(hit, tot)
        return cache[key]

    # ---- 每个结论词 × 每个特征：算各桶的提升量，看跨度 ----
    hr("1. 每个结论词里，哪个维度能把「成立/推翻」分开")
    print("  读法：跨度 = 最好的桶 − 最差的桶（提升量之差）。跨度大 = 这个维度有用。")
    print("        0 表示这个维度在该结论词内部没有区分力。\n")
    verdict_rows = []
    for lab in LABELS:
        sub = [r for r in ok if r["label"] == lab]
        if len(sub) < MIN_N:
            continue
        print("  ── %s（n=%d）──" % (lab, len(sub)))
        print("     %-16s %-14s %6s %7s %7s %8s"
              % ("维度", "取值", "样本", "命中", "基准", "提升"))
        best = {}
        for f in FEATURES:
            buckets = defaultdict(list)
            for r in sub:
                buckets[bucket_of(f, r[f])].append(r)
            stats = []
            for bk, rs in buckets.items():
                if len(rs) < MIN_N:
                    continue
                h = sum(1 for r in rs if r["v"]["verdict"] == V.VERDICT_HELD)
                b = sum(base_for(r) for r in rs) / len(rs)
                lift = pct(h, len(rs)) - b
                stats.append((bk, len(rs), pct(h, len(rs)), b, lift))
            if len(stats) < 2:
                continue
            stats.sort(key=lambda x: -x[4])
            span = stats[0][4] - stats[-1][4]
            best[f] = span
            for bk, ln, hp, bb, lf in stats:
                print("     %-16s %-14s %6d %6.1f%% %6.1f%% %+8.1f"
                      % (f, bk, ln, hp, bb, lf))
            print("     %-16s %-14s %6s %7s %7s %+8.1f  ← 跨度"
                  % ("", "", "", "", "", span))
        if best:
            top = sorted(best.items(), key=lambda kv: -kv[1])[:3]
            print("     → 区分力前三：%s"
                  % "、".join("%s %.1f" % (k, v) for k, v in top))
        print()
        verdict_rows.append((lab, len(sub), best))

    hr("2. 候选维度汇总（按跨度排序，跨结论词平均）")
    agg = defaultdict(list)
    for lab, ln, best in verdict_rows:
        for f, sp in best.items():
            agg[f].append(sp)
    print("  %-18s %8s %10s  %s" % ("维度", "出现次数", "平均跨度", "哪些结论词里最灵"))
    for f, sps in sorted(agg.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        who = [lab for lab, ln, best in verdict_rows if f in best]
        print("  %-18s %8d %10.1f  %s"
              % (f, len(sps), sum(sps) / len(sps), "、".join(who)))

    hr("3. 这一轮的边界（别越过去）")
    print("  · 上面只找**维度**，一个阈值都没调。用户明确「先不调阈值」。")
    print("  · 所有维度都来自表 1 / 表 1.5 / 表 2，**在 t 时刻就能算出来**，")
    print("    所以把它们加进判定层不会引入前视。")
    print("  · 反过来，像「这次判断活了多久」这种必须看后面的量，")
    print("    永远只能留在表 6 当诊断，不许进表 5。")
    print("  · 跨度大不等于该加。还要有机制解释，否则就是在这份数据上拟合噪声。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
