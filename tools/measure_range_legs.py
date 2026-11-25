# -*- coding: utf-8 -*-
"""量「区间演化的四条腿」在真实计划上落在哪（S5 先量后接）。

为什么先量不先接：已经有一条负面测量挡在前面——11 条真实计划里，
1h 的 R（反向边界）落在止损距离之内 **0/11**。如果 R 当判据永远不会触发，
接上去等于没接。所以要先把四条腿的**几何分布**量清楚，再决定接哪条。

四条腿的映射（唯一来源 `kbar/lines.py::range_p` 的注释，不要在这里另推一遍）：

    DOWN: soft_up=S, soft_dn=lo_b=P, hard_up=hi_b=R, hard_dn=None
    UP  : soft_up=hi_b=P, soft_dn=S,   hard_up=None,   hard_dn=lo_b=R

    所以 P = range_p@1h（按方向折叠进 soft_*），R = range_hard_*@1h，
    K = range_key@1h，S = 另一个 soft。

输出的量纲统一换成 **R 倍**（R = |挂单价 − 止损价|），
这样不同计划（振幅不同）之间才能比较：

    r_mult < 0      : 腿在入场价的**反侧**（多头在下方）——若绝对值 < 1 就落在止损里
    -1 <= r < 0     : 落在「入场价 ↔ 止损价」之间 → **会立刻触发，当判据无意义**
    0 <= r < 1      : 刚过入场价
    1 <= r < 4      : 入场到止盈之间 → **可以当移动止盈的锚**
    r >= 4          : 已经超过止盈

判读方式：一条腿要能当「现实跟随」的锚，必须大量落在 `1 <= r < 4`，
且不是大量 NULL。落在 `-1..0` 的比例就是「接上去会立刻误触发」的风险。

用法：
    python tools/measure_range_legs.py                # 本地库
    python tools/measure_range_legs.py --db-root DIR  # 指定数据目录
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trading import pending_plan as P
from ai_trader.trading.plan_source import PlanSource
from ai_trader.trading.thesis import DOWN, UP

#: 腿名 → kbar_line 的 line_key。R 按方向取两条，其余一条。
LEG_KEYS = {
    "P": ("range_p@1h",),
    "K": ("range_key@1h",),
    "S_UP": ("range_soft_up@1h",),
    "S_DN": ("range_soft_dn@1h",),
    "R_UP": ("range_hard_up@1h",),
    "R_DN": ("range_hard_dn@1h",),
}

R_MS = 3_600_000


def _leg(book, key, ts):
    """取某条线在 ts 那一根 1h **收盘后**的值。"""
    pt = book.at(key, int(ts) + R_MS)
    if pt is None:
        return None
    try:
        v = float(pt.price)
    except (TypeError, ValueError, AttributeError):
        return None
    return v if v > 0 else None


def _r_mult(leg_px, order_px, stop_px, direction):
    """腿价换算成 R 倍（正 = 在入场价的顺向一侧）。"""
    r = abs(float(order_px) - float(stop_px))
    if r <= 0:
        return None
    if direction == UP:
        return (float(leg_px) - float(order_px)) / r
    return (float(order_px) - float(leg_px)) / r


BUCKETS = [("落进止损(<-1R)", -1e9, -1.0),
           ("止损↔入场(-1..0)", -1.0, 0.0),
           ("入场↔1R(0..1)", 0.0, 1.0),
           ("1R↔止盈(1..4)", 1.0, 4.0),
           ("超止盈(>=4R)", 4.0, 1e9)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default="data")
    ap.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args()

    from ai_trader.main import kbar_lines_for

    class _Cfg(object):
        class store:
            path = os.path.join(args.db_root, "market.db")

        class trading:
            strategy = "pending"
            symbol = args.symbol

    from ai_trader.trading.plan_source import resolve_range_db
    range_db = resolve_range_db(args.db_root)
    kbar_db = os.path.join(args.db_root, "kbar.db")
    print("kbar_db  =", kbar_db)
    print("range_db =", range_db)

    src = PlanSource(book_provider=kbar_lines_for(_Cfg()),
                     kbar_db=kbar_db, range_db=range_db, symbol=args.symbol)
    ctx = src.context()
    params = P.PendingParams()

    win_plans, _ = src.plans(params, ctx=ctx)
    print("带开仓窗口的计划: %d 条，窗口 %d 个"
          % (len(win_plans), len(ctx.windows or ())))

    # 全量（不限窗口）：腿的几何不依赖窗口，样本更多更好判
    ctx_all = P.PlanContext(ctx.bars15, ctx.book,
                            bars_1h=getattr(ctx, "_h1", ()),
                            bars_5m=getattr(ctx, "bars5", ()),
                            windows=None)
    all_plans, _st = P.detect_all(ctx_all, params, require_window=False,
                                  include_mirror=True)
    print("全量计划（不限窗口）: %d 条" % len(all_plans))

    for tag, plans in (("带窗口", win_plans), ("全量", all_plans)):
        if not plans:
            print("\n=== %s：0 条，跳过 ===" % tag)
            continue
        print("\n" + "=" * 78)
        print("=== %s：%d 条计划 ===" % (tag, len(plans)))
        print("=" * 78)

        # 先看 R（止损距离）与目标是不是 4R
        ratios = []
        for p in plans:
            r = abs(p.order_price - p.stop_price)
            if r > 0:
                ratios.append((abs(p.target_price - p.order_price), r))
        if ratios:
            tgt = [t / r for t, r in ratios]
            tgt.sort()
            print("止盈/R 倍数: 中位 %.3f  最小 %.3f  最大 %.3f"
                  % (tgt[len(tgt) // 2], tgt[0], tgt[-1]))
            print("止损距离%%:  中位 %.3f%%"
                  % (sorted(abs(p.order_price - p.stop_price)
                            / p.order_price * 100 for p in plans)[len(plans) // 2]))

        rows = []
        for name, keys in LEG_KEYS.items():
            vals, miss = [], 0
            for p in plans:
                key = keys[0]
                v = _leg(ctx.book, key, p.effective_claim_ts)
                if v is None:
                    miss += 1
                    continue
                rm = _r_mult(v, p.order_price, p.stop_price, p.direction)
                if rm is None:
                    miss += 1
                    continue
                vals.append(rm)
            n = len(plans)
            if not vals:
                rows.append((name, miss, n, None, [0] * len(BUCKETS)))
                continue
            vals_sorted = sorted(vals)
            med = vals_sorted[len(vals_sorted) // 2]
            cnt = []
            for _label, lo, hi in BUCKETS:
                cnt.append(sum(1 for x in vals if lo <= x < hi))
            rows.append((name, miss, n, med, cnt))

        hdr = "%-8s %8s %8s" % ("腿", "有值率", "中位R")
        for label, _lo, _hi in BUCKETS:
            hdr += " %14s" % label
        print(hdr)
        for name, miss, n, med, cnt in rows:
            have = n - miss
            line = "%-8s %7.1f%% %8s" % (
                name, 100.0 * have / n, "-" if med is None else "%.2f" % med)
            for c in cnt:
                line += " %7d(%4.1f%%)" % (c, 100.0 * c / n if n else 0.0)
            print(line)

        # 按方向拆开看 R（hard 线按方向存在，合并看会被 NULL 掩盖）
        print("\nR（反向边界）按方向拆：")
        for d in (UP, DOWN):
            sub = [p for p in plans if p.direction == d]
            if not sub:
                continue
            ok = 0
            in_stop = 0
            usable = 0
            for p in sub:
                key = "range_hard_up@1h" if d == UP else "range_hard_dn@1h"
                v = _leg(ctx.book, key, p.effective_claim_ts)
                if v is None:
                    continue
                ok += 1
                rm = _r_mult(v, p.order_price, p.stop_price, d)
                if rm is None:
                    continue
                if -1.0 <= rm < 0.0:
                    in_stop += 1
                elif 1.0 <= rm < 4.0:
                    usable += 1
            print("   %-5s 计划 %3d 条，R 有值 %3d（%.0f%%），"
                  "落进止损 %d，可当锚 %d"
                  % (d, len(sub), ok, 100.0 * ok / len(sub), in_stop, usable))

        # ★ 必须按家族拆开：direction 是**交易方向**，而 range_p 是按
        #   **区间方向**折叠的。反转单逆着区间做，P 落在反侧是设计如此，
        #   混在一起看会把「设计」读成「失效」。
        print("\n按家族拆（可当锚 = 落在 1R..4R，只有这部分会真的改变行为）：")
        fams = {}
        for p in plans:
            fams.setdefault(getattr(p, "family", "?"), []).append(p)
        for fam, sub in sorted(fams.items()):
            line = "   %-9s 计划 %4d" % (fam, len(sub))
            for name, keys in LEG_KEYS.items():
                if name.startswith("S_") or name.startswith("R_"):
                    continue        # 软/硬边界按方向存在，单看没意义
                usable = in_stop = 0
                for p in sub:
                    v = _leg(ctx.book, keys[0], p.effective_claim_ts)
                    if v is None:
                        continue
                    rm = _r_mult(v, p.order_price, p.stop_price, p.direction)
                    if rm is None:
                        continue
                    if -1.0 <= rm < 0.0:
                        in_stop += 1
                    elif 1.0 <= rm < 4.0:
                        usable += 1
                line += "   %s: 锚 %3d(%4.1f%%) 误触发 %3d(%4.1f%%)" % (
                    name, usable, 100.0 * usable / len(sub),
                    in_stop, 100.0 * in_stop / len(sub))
            print(line)

        # R 按家族 + 方向
        for fam in sorted(fams):
            sub = fams[fam]
            for d in (UP, DOWN):
                s2 = [p for p in sub if p.direction == d]
                if not s2:
                    continue
                key = "range_hard_up@1h" if d == UP else "range_hard_dn@1h"
                usable = in_stop = has = 0
                for p in s2:
                    v = _leg(ctx.book, key, p.effective_claim_ts)
                    if v is None:
                        continue
                    has += 1
                    rm = _r_mult(v, p.order_price, p.stop_price, d)
                    if rm is None:
                        continue
                    if -1.0 <= rm < 0.0:
                        in_stop += 1
                    elif 1.0 <= rm < 4.0:
                        usable += 1
                print("   R %-9s %-5s %4d 条 → 有值 %4d，锚 %3d(%4.1f%%)，"
                      "误触发 %3d(%4.1f%%)"
                      % (fam, d, len(s2), has, usable,
                         100.0 * usable / len(s2), in_stop,
                         100.0 * in_stop / len(s2)))



if __name__ == "__main__":
    main()
