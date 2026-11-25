# -*- coding: utf-8 -*-
"""表 6 · 佐证层回探：命中率 → **提升量** → 这个结论词到底有没有信息量。

    「命中 73%」本身不是结论。N=1 时「随便一根，本根低点后面不被破」
    本来就有 53.1%（BTCUSDT@15m 实测）。不报基准，就是把「比瞎猜好 20 个点」
    说成「七成准」。

本工具干四件事，顺序不能反：

    1. 独立重算一遍表 6，与库里的行逐根对比（三段式：算得对 / 落得对 / 读得回）
    2. 算**同口径基准率**：同一个问句，对「随便一根」问一遍
    3. 算提升量（百分点）与 z 值 —— 没有 z 的提升量在 n=46 上等于噪声
    4. 分层：把「离参照针多远」这个混杂变量按住，再看结论词还有没有信息量

⚠ 基准必须与预言**用同一个参照点**。本项目踩过这个坑：拿「稳住说参照针守住」
   去比「随便一根说不破本根低点」，得出「稳住无用」的假结论 —— 苹果比橘子。
   正确做法是按 (本根极值 − 参照极值)/针幅 分层，在**同一层里**比。

只读 data/kbar.db，不写任何库。
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import shape as S          # noqa: E402
from ai_trader.kbar import verify as V         # noqa: E402
from ai_trader.kbar.store import KBarStore     # noqa: E402


def hr(t):
    print("\n" + "=" * 84)
    print(t)
    print("=" * 84)


def pct(a, b):
    return 0.0 if not b else 100.0 * a / b


def bj(ms):
    if ms is None:
        return "-"
    return datetime.datetime.utcfromtimestamp(
        (int(ms) + 8 * 3600 * 1000) / 1000.0).strftime("%m-%d %H:%M")


def geos(bars):
    return [S.geometry(b["o"], b["h"], b["l"], b["c"]) for b in bars]


def baseline_own_low(bars, n, N):
    """基准①：随便一根，「本根低点后面 N 根不被破」的比率。"""
    hit = tot = 0
    for i in range(n - N):
        if min(x["l"] for x in bars[i + 1:i + 1 + N]) >= bars[i]["l"]:
            hit += 1
        tot += 1
    return pct(hit, tot), tot


def baseline_own_high(bars, n, N):
    """基准②：随便一根，「本根高点后面 N 根不被破」。"""
    hit = tot = 0
    for i in range(n - N):
        if max(x["h"] for x in bars[i + 1:i + 1 + N]) <= bars[i]["h"]:
            hit += 1
        tot += 1
    return pct(hit, tot), tot


def baseline_prior_low(bars, n, N, look):
    """基准③：「之前 look 根最低点，后面 N 根不被破」。

    这是「稳住/远离」的**同口径**基准 —— 它们预言的是「参照针（一个更早的
    极值）守住」，所以基准也必须是「一个更早的极值守住」，而不是本根极值。
    """
    hit = tot = 0
    for i in range(look, n - N):
        j = min(range(i - look, i), key=lambda k: bars[k]["l"])
        if min(x["l"] for x in bars[i + 1:i + 1 + N]) >= bars[j]["l"]:
            hit += 1
        tot += 1
    return pct(hit, tot), tot


def baseline_prior_high(bars, n, N, look):
    hit = tot = 0
    for i in range(look, n - N):
        j = max(range(i - look, i), key=lambda k: bars[k]["h"])
        if max(x["h"] for x in bars[i + 1:i + 1 + N]) <= bars[j]["h"]:
            hit += 1
        tot += 1
    return pct(hit, tot), tot


def baseline_quiet(bars, n, N, min_amp):
    """基准④：「随便一根，后面 N 根不走出 >= 1 倍本根全幅」。"""
    hit = tot = 0
    for i in range(n - N):
        g = S.geometry(bars[i]["o"], bars[i]["h"], bars[i]["l"], bars[i]["c"])
        amp = max(g.rng, min_amp)
        ext = max(max(abs(x["h"] - bars[i]["c"]), abs(x["l"] - bars[i]["c"]))
                  for x in bars[i + 1:i + 1 + N])
        if ext < amp:
            hit += 1
        tot += 1
    return pct(hit, tot), tot


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--rebuild-check", action="store_true", default=True,
                    help="先独立重算表 6 并与库里对比（默认开）")
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s" % a.kbar_db)

    with KBarStore(a.kbar_db) as st:
        facts = st.evolution_rows(a.symbol, a.interval)
        shp = st.shape_rows(a.symbol, a.interval)
        stored = st.verify_rows(a.symbol, a.interval)
        cnt = st.verify_counts(a.symbol, a.interval)
    bars = [{"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
             "vol_rel": r.get("vol_rel"),
             "taker_buy_ratio": r.get("taker_buy_ratio")}
            for r in facts if r["o"] is not None]
    bars.sort(key=lambda x: int(x["ts"]))
    n = len(bars)
    G = geos(bars)
    by_ts = {int(r["ts"]): r for r in shp}
    look = S.SWING_LOOKBACK_BARS

    hr("0. 表 5 / 表 6 的体量与口径")
    print("  表 1 有 OHLC 的根：%d" % n)
    print("  表 5 结论行：%d（阈值指纹 %s）"
          % (len(shp), ",".join(sorted(set(r["params_hash"] for r in shp)))))
    print("  表 6 佐证行：%d（本层指纹 %s）"
          % (cnt["rows"], ",".join(cnt["verify_hashes"]) or "-"))
    print("  本层口径：N=%s  容差=%.0f  规则=%s"
          % (",".join(str(h) for h in V.VERIFY_HORIZONS), V.VERIFY_TOL_PTS,
             V.PRED_RULES_VERSION))

    # ---- 1. 独立重算，逐根与库对比 ----
    hr("1. 独立重算表 6，与库里逐行对比（不信库里就算过）")
    idx_of = {int(b["ts"]): i for i, b in enumerate(bars)}
    mism = []
    recomputed = {}
    for i, b in enumerate(bars):
        row = by_ts.get(int(b["ts"]))
        if row is None:
            continue
        jd = S.Judgement(
            states=tuple(x for x in (row["states"] or "").split(",") if x),
            label=row["label"], side=row["side"],
            evidence=tuple((row["evidence"] or "").splitlines()),
            pin_ts=row["ref_pin_ts"], pin_kind=row["pin_kind"],
            ref_pin_kind=row["ref_pin_kind"], ref_pin_side=row["ref_pin_side"])
        pred = V.predict(bars, i, jd, ref_pin_side=row["ref_pin_side"])
        for h in V.VERIFY_HORIZONS:
            res = V.verify(bars, i, pred, int(h))
            recomputed[(int(b["ts"]), int(h))] = (pred, res)
    for r in stored:
        key = (int(r["ts"]), int(r["horizon"]))
        got = recomputed.get(key)
        if got is None:
            mism.append((key, "库里多了一行"))
            continue
        pred, res = got
        if (pred.kind != r["pred_kind"] or pred.text != r["pred_text"]
                or res.verdict != r["verdict"]):
            mism.append((key, "预言/裁决不一致：重算 %s/%s vs 库 %s/%s"
                         % (pred.kind, res.verdict, r["pred_kind"], r["verdict"])))
        elif (res.actual_price is not None and r["actual_price"] is not None
              and abs(res.actual_price - r["actual_price"]) > 1e-6):
            mism.append((key, "实际价不一致：%r vs %r"
                         % (res.actual_price, r["actual_price"])))
    print("  重算行数=%d   库里行数=%d   不一致=%d  %s"
          % (len(recomputed), len(stored), len(mism),
             "OK" if not mism else "← 错！"))
    for k, why in mism[:5]:
        print("    例 %s N=%d：%s" % (bj(k[0]), k[1], why))

    # ---- 2. 命中率 + 基准 + 提升量 + z ----
    hr("2. 每个结论词的命中率 vs **同口径基准**（这才是结论）")
    print("  说明：基准是「同一个问句，对随便一根问一遍」。")
    print("        命中率 − 基准 = 提升量；z 是「这点提升有多少可能是噪声」。")
    print()
    for N in V.VERIFY_HORIZONS:
        b_own_lo, n_own_lo = baseline_own_low(bars, n, N)
        b_own_hi, _ = baseline_own_high(bars, n, N)
        b_prior_lo, n_prior = baseline_prior_low(bars, n, N, look)
        b_prior_hi, _ = baseline_prior_high(bars, n, N, look)
        b_quiet, _ = baseline_quiet(bars, n, N, S.WICK_PTS_MIN)
        print("  ── N=%d 的四个同口径基准 ──" % N)
        print("     本根低点守住      %5.1f%%（n=%d）" % (b_own_lo, n_own_lo))
        print("     本根高点守住      %5.1f%%" % b_own_hi)
        print("     之前%d根最低点守住  %5.1f%%（n=%d）" % (look, b_prior_lo, n_prior))
        print("     之前%d根最高点守住  %5.1f%%" % (look, b_prior_hi))
        print("     不出 1 倍全幅      %5.1f%%" % b_quiet)
        print()
        print("     %-12s %-8s %7s %7s %7s %7s %6s"
              % ("label", "预言", "命中", "样本", "基准", "提升", "z"))
        rows = [r for r in stored if int(r["horizon"]) == N]
        tbl = {}
        for r in rows:
            tbl.setdefault(r["label"], {}).setdefault(r["pred_kind"], []).append(r)
        for lab in ("恐慌盘", "插针收回", "插针", "插针后稳住", "针后远离", "无·真空", "无·贴线", "无"):
            for pk, sub in sorted(tbl.get(lab, {}).items()):
                hit = sum(1 for r in sub if r["verdict"] == V.VERDICT_HELD)
                denom = sum(1 for r in sub
                            if r["verdict"] in (V.VERDICT_HELD,
                                                V.VERDICT_BROKE))
                if not denom:
                    continue
                if pk in (V.PRED_PIN_LOW_HELD, V.PRED_PIN_LOW_BROKE):
                    base = (b_own_lo if pk == V.PRED_PIN_LOW_HELD
                            else 100.0 - b_own_lo)
                elif pk in (V.PRED_PIN_HIGH_HELD, V.PRED_PIN_HIGH_BROKE):
                    base = (b_own_hi if pk == V.PRED_PIN_HIGH_HELD
                            else 100.0 - b_own_hi)
                elif pk in (V.PRED_REF_LOW_HELD,):
                    base = b_prior_lo
                elif pk in (V.PRED_REF_HIGH_HELD,):
                    base = b_prior_hi
                else:
                    base = b_quiet
                lift, z = V.lift_z(hit, denom, base)
                print("     %-12s %-8s %6.1f%% %7d %6.1f%% %+7.1f %+6.1f"
                      % (lab, pk, pct(hit, denom), denom, base, lift, z))
        print()

    # ---- 3. 恐慌盘拆两半（这个结论词装了两个相反的东西）----
    hr("3. 「恐慌盘」拆两半：有没有下影，后续行为是**相反**的")
    print("  ⚠ 这里不能数「被推翻」：对不同预言，「被推翻」的物理含义相反。")
    print("     「不会破」被推翻 = 破了；「会破」被推翻 = 没破。")
    print("     所以下面统一换算成**客观事实**：后面到底破没破，再比基准。")
    print()
    for N in V.VERIFY_HORIZONS:
        b_own_lo, _ = baseline_own_low(bars, n, N)
        b_own_hi, _ = baseline_own_high(bars, n, N)
        print("  ── N=%d ──" % N)
        print("     基准「破本根低点」= %.1f%%   基准「破本根高点」= %.1f%%"
              % (100.0 - b_own_lo, 100.0 - b_own_hi))
        for sd, base in ((S.SIDE_DOWN, 100.0 - b_own_lo),
                         (S.SIDE_UP, 100.0 - b_own_hi)):
            for tag, want_wick in (("有影线（被买/卖回）", True),
                                   ("无影线（收在极值）", False)):
                sub = [r for r in stored
                       if r["label"] == "恐慌盘" and int(r["horizon"]) == N
                       and r["verdict"] in (V.VERDICT_HELD, V.VERDICT_BROKE)]
                # 按 pred_kind 认方向，按 must_hold 反推事实
                if sd == S.SIDE_DOWN:
                    kind_hold, kind_break = V.PRED_PIN_LOW_HELD, V.PRED_PIN_LOW_BROKE
                else:
                    kind_hold, kind_break = V.PRED_PIN_HIGH_HELD, V.PRED_PIN_HIGH_BROKE
                sel = [r for r in sub
                       if (r["pred_kind"] == kind_hold) == want_wick
                       and r["pred_kind"] in (kind_hold, kind_break)]
                if not sel:
                    continue
                # 客观「破了没」：守住类被推翻 = 破了；会破类成立 = 破了
                broke = sum(1 for r in sel
                            if (r["verdict"] == V.VERDICT_BROKE
                                and r["pred_kind"] == kind_hold)
                            or (r["verdict"] == V.VERDICT_HELD
                                and r["pred_kind"] == kind_break))
                lift, z = V.lift_z(broke, len(sel), base)
                print("     %-4s %-18s n=%4d  后面真破了 %5.1f%%  提升 %+6.1f  z=%+5.1f"
                      % (sd, tag, len(sel), pct(broke, len(sel)), lift, z))
        print()

    # ---- 4. 稳住/远离分层 ----
    hr("4. 「稳住/远离」分层：把距离这个混杂变量按住，再看还有没有信息")
    print("  为什么必须分层：稳住/远离 的分界线本身就是「距针 <= 1 倍针幅」。")
    print("  「离得近的极值更容易被碰」是真事 —— 不分层就是把「它离得近」")
    print("  当成「它判得准」。而这个项目上次正是这么得出「稳住无用」的假结论。")
    print()
    print("  对照组怎么取：同距离桶里**所有**「往回 3 根内有针」的根，")
    print("  不只取被标成远离的那些 —— 因为桶内根本没有「非稳住非远离」的根")
    print("  （距离 <=1 就叫稳住，>1 就叫远离，两者穷尽了带针的根）。")
    print()
    for N in V.VERIFY_HORIZONS:
        for sd in (S.SIDE_DOWN, S.SIDE_UP):
            print("  ── N=%d  参照针方向=%s ──" % (N, sd))
            print("     %-10s %8s %8s %8s %8s %8s"
                  % ("距离桶", "稳住命中", "同桶对照", "提升", "z", "样本"))
            buckets = {}
            for i in range(1, n - N):
                got = S.find_pin_before_ex(bars, i, look=S.STABLE_LOOKBACK_BARS,
                                           side=sd)
                if got is None:
                    continue
                j, _kind = got
                gp = G[j]
                amp = max(gp.rng, S.WICK_PTS_MIN)
                if sd == S.SIDE_DOWN:
                    dist = G[i].l - gp.l
                    held = min(x["l"] for x in bars[i + 1:i + 1 + N]) >= gp.l
                else:
                    dist = gp.h - G[i].h
                    held = max(x["h"] for x in bars[i + 1:i + 1 + N]) <= gp.h
                r = dist / amp
                # ⚠ dist < 0 = 这根已经破了针的极值。**必须单独立桶**，
                #   否则它们会挤进「0~0.25」当对照，把对照组拉低成 25%，
                #   于是稳住那一栏显得提升 +51 点 —— 那是被污染的对照算出来的假象。
                if r < 0:
                    bk = "<0（已破）"
                elif r <= 0.25:
                    bk = "0~0.25"
                elif r <= 0.5:
                    bk = "0.25~0.5"
                elif r <= 0.75:
                    bk = "0.5~0.75"
                elif r <= 1.0:
                    bk = "0.75~1.0"
                else:
                    bk = ">1.0"
                row = by_ts.get(int(bars[i]["ts"]))
                lab = ""
                if row is not None and row["ref_pin_ts"] == bars[j]["ts"]:
                    lab = row["label"]
                slot = buckets.setdefault(bk, {"st": [0, 0], "ot": [0, 0]})
                key = "st" if lab == S.STATE_STABLE else "ot"
                slot[key][0] += 1 if held else 0
                slot[key][1] += 1
            for bk in ("<0（已破）", "0~0.25", "0.25~0.5", "0.5~0.75",
                       "0.75~1.0", ">1.0"):
                slot = buckets.get(bk)
                if not slot:
                    continue
                sh, st_ = slot["st"]
                oh, ot = slot["ot"]
                if not st_ or not ot:
                    print("     %-10s %8s 稳住 n=%-4d 对照 n=%-4d（一边没样本，不比）"
                          % (bk, "-", st_, ot))
                    continue
                if bk == "<0（已破）":
                    # 这个桶**不可比**：稳住组只落在 [-20, 0)（容差允许的那 20 点），
                    # 对照组是「远低于 -20」全部。两边不是同一段距离，
                    # 算出来的提升量几乎全是「差多远」的差，不是结论词的功劳。
                    print("     %-10s %7.1f%% %7.1f%% %8s %8s  %d/%d ← 不可比"
                          % (bk, pct(sh, st_), pct(oh, ot), "-", "-", st_, ot))
                    continue
                lift, z = V.lift_z(sh, st_, pct(oh, ot))
                print("     %-10s %7.1f%% %7.1f%% %+8.1f %+8.1f  %d/%d"
                      % (bk, pct(sh, st_), pct(oh, ot), lift, z, st_, ot))
            print()

    # ---- 5. 结论 ----
    hr("5. 读法")
    print("  · 提升量 <= 0 的结论词，等于**没说**：它出现的根和随便一根没区别。")
    print("  · |z| < 2 的提升量当噪声看，尤其 n < 200 的时候。")
    print("  · 分层后提升量塌掉的，说明信息量来自那个混杂变量，不来自结论词本身。")
    print("  · 命中率永远不许单独报 —— 必须和同口径基准并排。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
