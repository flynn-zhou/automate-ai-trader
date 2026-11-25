# -*- coding: utf-8 -*-
"""事后要看多远：测「裁决在第几根之后不再变」。

用户 2026-11-10 问：
    「事后是多事后，最多经过多少根同周期的 k 线判断是事后最远距离？
      最少经过多少根才开始判事后？」

原来的答案是「N=1 和 N=3」，那是我拍的，不是测出来的。这个探针把它测出来。

方法：对每一根已经落库的判断，算出 N=1..MAXN 每个 N 下的裁决，然后找
「**从这一根起再也不变**」的那个 N —— 叫它「定型根数」。它的分布直接回答
「最多要等几根」。

    定型根数 = min{ N : 对所有 n >= N，裁决(N) == 裁决(MAXN) }

为什么一定存在：本层的裁决只依赖「后面窗口内有没有破极值」，
窗口越长越可能破，破了就不会再变回来（单调）。所以必然收敛。

同时报：N 越大，命中率怎么变；以及「N=1 就定型」的只占多少
—— 这决定了「只报 N=1」是不是在拿一个临时答案当结论。

只读 data/kbar.db。
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

MAXN = 12
LABELS = ("恐慌盘", "插针收回", "插针", "插针后稳住", "针后远离", "无·真空", "无·贴线", "无")


def hr(t):
    print("\n" + "=" * 92)
    print(t)
    print("=" * 92)


def pct(a, b):
    return 0.0 if not b else 100.0 * a / b


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--maxn", type=int, default=MAXN)
    a = ap.parse_args(argv)
    MAX = a.maxn

    with KBarStore(a.kbar_db) as st:
        facts = st.evolution_rows(a.symbol, a.interval)
        shp = st.shape_rows(a.symbol, a.interval)
    bars = [{"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
             "vol_rel": r.get("vol_rel"),
             "taker_buy_ratio": r.get("taker_buy_ratio")}
            for r in facts if r["o"] is not None]
    bars.sort(key=lambda x: int(x["ts"]))
    n = len(bars)
    hi = [b["h"] for b in bars]
    lo = [b["l"] for b in bars]

    # ---- 1. 从表 5 还原每根的预言（不重判） ----
    preds = [None] * n
    by_ts = {int(b["ts"]): i for i, b in enumerate(bars)}
    for r in shp:
        i = by_ts.get(int(r["ts"]))
        if i is None:
            continue
        jd = S.Judgement(
            states=tuple(x for x in (r["states"] or "").split(",") if x),
            label=r["label"], side=r["side"],
            evidence=tuple((r["evidence"] or "").splitlines()),
            pin_ts=r["ref_pin_ts"], pin_kind=r["pin_kind"],
            ref_pin_kind=r["ref_pin_kind"], ref_pin_side=r["ref_pin_side"])
        preds[i] = (r["label"], V.predict(bars, i, jd,
                                          ref_pin_side=r["ref_pin_side"]))

    # ---- 2. 算每个 N 下的「破了没」，再换裁决 ----
    # 单调性：窗口越长越可能破；破了不再变回 → 定型根数一定存在
    verdict = {}          # (i, N) -> verdict
    settle = {}           # i -> 定型根数
    for i in range(n):
        item = preds[i]
        if item is None:
            continue
        lab, p = item
        if p.kind == V.PRED_NONE:
            for N in range(1, MAX + 1):
                verdict[(i, N)] = V.VERDICT_NO_PRED
            settle[i] = 1
            continue
        ref = p.ref_price
        vs = {}
        for N in range(1, MAX + 1):
            if i + N >= n - 0 and i + N > n - 1:
                vs[N] = V.VERDICT_UNKNOWN
                continue
            fut = range(i + 1, i + 1 + N)
            if p.kind == V.PRED_QUIET:
                ext = max(max(abs(hi[k] - bars[i]["c"]),
                              abs(lo[k] - bars[i]["c"])) for k in fut)
                breached = ext >= p.amp
            elif p.side == S.SIDE_DOWN:
                breached = min(lo[k] for k in fut) <= ref - V.VERIFY_TOL_PTS
            else:
                breached = max(hi[k] for k in fut) >= ref + V.VERIFY_TOL_PTS
            held = (not breached) if p.must_hold else breached
            vs[N] = V.VERDICT_HELD if held else V.VERDICT_BROKE
        # 定型根数：从后往前找第一处与 MAX 不同
        target = vs[MAX]
        s = MAX
        for N in range(MAX - 1, 0, -1):
            if vs[N] != target:
                s = N + 1
                break
            s = N
        verdict.update({(i, N): v for N, v in vs.items()})
        settle[i] = s

    # ---- 3. 汇总 ----
    hr("0. 口径")
    print("  根数 %s   参与统计 %d" % ("{:,}".format(n), len(settle)))
    print("  测到 N = %d（同周期 15m）" % MAX)
    print("  判据：本层裁决只看「后面窗口内有没有破极值」，窗口越长越可能破，")
    print("        破了不会变回 → 所以每根的裁决一定会在某个 N 定型。")

    hr("1. 定型根数分布（「最多要等几根」的直接答案）")
    dist = Counter(settle.values())
    print("  %-8s %10s %8s %s" % ("定型于", "根数", "占比", "累计"))
    acc = 0
    for N in range(1, MAX + 1):
        c = dist.get(N, 0)
        acc += c
        print("  N=%-6d %10s %7.1f%% %7.1f%%"
              % (N, "{:,}".format(c), pct(c, len(settle)), pct(acc, len(settle))))
    Ss = sorted(settle.values())
    med = Ss[len(Ss) // 2]
    p90 = Ss[int(len(Ss) * 0.90)]
    p99 = Ss[min(len(Ss) - 1, int(len(Ss) * 0.99))]
    print("\n  中位 %d 根   p90 %d 根   p99 %d 根   最大 %d 根"
          % (med, p90, p99, Ss[-1]))
    print("  「N=1 就定型」占 %.1f%% —— 反过来看，只报 N=1 时"
          "有 %.1f%% 的判断还是临时答案。" % (pct(dist.get(1, 0), len(settle)),
                                              100 - pct(dist.get(1, 0), len(settle))))

    hr("2. 每个结论词要等几根才定型")
    print("  %-12s %8s %8s %8s %8s %8s" % ("label", "样本", "中位", "p90", "N=1定型", "最大"))
    per = defaultdict(list)
    for i, s in settle.items():
        if preds[i] is not None:
            per[preds[i][0]].append(s)
    for lab in LABELS:
        xs = per.get(lab, [])
        if not xs:
            continue
        xs_s = sorted(xs)
        print("  %-12s %8s %8d %8d %7.1f%% %8d"
              % (lab, "{:,}".format(len(xs)), xs_s[len(xs_s) // 2],
                 xs_s[int(len(xs_s) * 0.90)],
                 pct(sum(1 for x in xs if x == 1), len(xs)), xs_s[-1]))

    hr("3. 命中率随 N 怎么变（含同口径基准）")
    # 基准：随便一根，同问句命中率
    def base(kind, side, N):
        hit = tot = 0
        for i in range(n - N):
            fut = range(i + 1, i + 1 + N)
            if kind == "quiet":
                g = S.geometry(bars[i]["o"], bars[i]["h"], bars[i]["l"],
                               bars[i]["c"])
                amp = max(g.rng, S.WICK_PTS_MIN)
                ext = max(max(abs(hi[k] - bars[i]["c"]),
                              abs(lo[k] - bars[i]["c"])) for k in fut)
                hit += 1 if ext < amp else 0
            elif side == S.SIDE_DOWN:
                hit += 1 if min(lo[k] for k in fut) > lo[i] else 0
            else:
                hit += 1 if max(hi[k] for k in fut) < hi[i] else 0
            tot += 1
        return pct(hit, tot)

    print("  %-4s %10s %10s %8s %8s" % ("N", "命中", "基准", "提升", "z"))
    for N in range(1, MAX + 1):
        hh = bb = 0
        for (i, NN), v in verdict.items():
            if NN != N or v not in (V.VERDICT_HELD, V.VERDICT_BROKE):
                continue
            hh += 1 if v == V.VERDICT_HELD else 0
            bb += 1
        if not bb:
            continue
        # 混合基准：按本库实际预言种类加权（下针/上针/全幅各占多少）
        cnt = Counter()
        for i in range(n):
            item = preds[i]
            if item is None:
                continue
            p = item[1]
            if p.kind == V.PRED_NONE:
                continue
            if p.kind == V.PRED_QUIET:
                cnt["quiet"] += 1
            elif p.side == S.SIDE_DOWN:
                cnt["down"] += 1
            else:
                cnt["up"] += 1
        b = (cnt["down"] * base("price", S.SIDE_DOWN, N)
             + cnt["up"] * base("price", S.SIDE_UP, N)
             + cnt["quiet"] * base("quiet", None, N)) / max(1, sum(cnt.values()))
        lift, z = V.lift_z(hh, bb, b)
        print("  %-4d %9.1f%% %9.1f%% %+8.1f %+8.1f"
              % (N, pct(hh, bb), b, lift, z))

    hr("4. 相邻 N 之间裁决会翻转多少根（「还没定型」的规模）")
    print("  %-14s %10s %s" % ("区间", "翻转根数", "占参与统计"))
    for N in range(1, MAX):
        flip = sum(1 for i in settle if verdict.get((i, N)) != verdict.get((i, N + 1)))
        print("  N=%d → N=%-2d %10s %9.1f%%"
              % (N, N + 1, "{:,}".format(flip), pct(flip, len(settle))))

    hr("5. 结论")
    print("  · 「最少几根开始判」= 1 根：下一根收盘就能给出一个裁决。")
    print("  · 「最多几根」看上面的 p99 / 最大：过了这个 N 再等也不会变。")
    print("  · N=1 定型只占 %.1f%%，所以只报 N=1 是不够的 ——"
          % pct(dist.get(1, 0), len(settle)))
    print("    剩下的是「N=1 说成立、后面被推翻」，那正是最有用的信息。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
