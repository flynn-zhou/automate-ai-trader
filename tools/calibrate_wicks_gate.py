# -*- coding: utf-8 -*-
"""三引线处置实验：把三引线也放进窗口闸，看它窗口内是否为正。

背景（calibrate_vol_gate2 的结论）：
  · 受窗口闸约束的两条形态（倒V + EMA拒绝）在 N=3.0/近邻2 下：
    9 笔、胜率 89%、均 R +1.862 —— 正收益。
  · 三引线（`WINDOW_GATE[三引线] = False`，不受闸）：
    758 笔、胜率 14%、均 R −0.515、总 R −390 —— 亏损源，且与量能门无关。

本脚本只改一件事：`WINDOW_GATE[三引线]` 是否置 True，对照三种变体：
  V0 现状（三引线不受闸）
  V1 三引线也受闸（窗口内才做）
  V2 三引线完全停用
若 V1 明显好于 V0，就把三引线也纳入窗口闸；若 V1 样本太小无意义，则停用。
"""
from __future__ import annotations

import os
import sys

BASE = os.environ.get("AI_TRADER_DATA", "/home/cryptobot/ai-trader")
sys.path.insert(0, os.environ.get("AI_TRADER_CODE", BASE))

from replay_night import (M15, M5, M1H, load_evolution, load_1h, load_tags,  # noqa
                          load_h1_vols, production_params, ro, KBAR_DB, parse_dt)
from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T

SYMBOL = "BTCUSDT"
VOL_MULT = 3.0
VOL_NEAR = 2


class _H1(object):
    __slots__ = ("ts", "o", "h", "l", "c", "v")

    def __init__(self, b, v):
        self.ts, self.o, self.h, self.l = (int(b["ts"]), float(b["o"]),
                                           float(b["h"]), float(b["l"]))
        self.c = float(b["c"])
        self.v = float(v)


def settle(pl, all15, all5, book, params, idx15):
    try:
        th = P.to_thesis(pl, params=params)
        start = idx15.get(int(pl.effective_claim_ts), 0)
        res = T.run(th, all15, book, start_at=start, entry_bars=all5,
                    claim_on_entry=bool(getattr(pl, "claim_on_entry", False)))
    except Exception as e:
        return None, "err:%s" % str(e)[:24]
    entry, stop = float(pl.order_price), float(pl.stop_price)
    risk = abs(entry - stop)
    close = why = None
    for a in res.actions:
        if a.kind == "close" and a.price is not None:
            close, why = float(a.price), (a.reason or "")
    if close is None or risk <= 0:
        return None, res.state
    sign = 1.0 if str(pl.direction) == T.UP else -1.0
    return round((close - entry) * sign / risk, 3), why or res.state


def stat(rs):
    if not rs:
        return "n=0（无样本）"
    n = len(rs)
    wr = 100.0 * sum(1 for r in rs if r > 0) / n
    tot = sum(rs)
    return ("n=%-4d 胜率 %3.0f%%  总R %+8.2f  均R %+.3f  最好%+.1f 最差%+.1f"
            % (n, wr, tot, tot / n, max(rs), min(rs)))


def main():
    t0 = parse_dt(sys.argv[1]) if len(sys.argv) > 1 else parse_dt("2026-08-01T00:00")
    t1 = parse_dt(sys.argv[2]) if len(sys.argv) > 2 else parse_dt("2026-11-26T08:00")

    b15, b5, b1h = load_evolution(M15), load_evolution(M5), load_1h()
    tags, vols = load_tags(), load_h1_vols()
    params = production_params()
    book = T.LineBook()
    con = ro(KBAR_DB)
    try:
        for r in con.execute(
                "select line_key, ts, line_price, line_ready_ms from kbar_line"
                " where symbol=? order by ts", (SYMBOL,)):
            book.add({"line_key": r[0], "ts": int(r[1]),
                      "line_price": float(r[2]), "line_ready_ms": int(r[3])})
    finally:
        con.close()

    hours = [_H1(b, vols.get(int(b["ts"]), 0.0)) for b in b1h]
    all15 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b15]
    all5 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b5]
    pk = [P._Bar(h.ts, h.o, h.h, h.l, h.c) for h in hours]
    idx15 = {b.ts: i for i, b in enumerate(all15)}

    def tag_of(ts):
        v = tags.get((int(ts) // M1H) * M1H)
        return v if v else ("", "")

    def line_of(ts, key):
        pt = book.at(key, int(ts) + M1H)
        return None if pt is None else float(pt.price)

    wins = P.build_open_windows(hours, tag_of, line_of, vol_mult=VOL_MULT,
                                vol_win=P.WINDOW_VOL_WIN, vol_near=VOL_NEAR,
                                start_tags=P.WINDOW_START_TAGS,
                                extra_bars=P.WINDOW_EXTRA_BARS)
    print("口径：N=%.1f 近邻=%d → 窗口 %d 个\n"
          % (VOL_MULT, VOL_NEAR, len(wins)), flush=True)

    orig_gate = dict(P.WINDOW_GATE)

    def run(kinds, gate_wicks, label):
        P.WINDOW_GATE[P.KIND_WICKS] = gate_wicks
        ctx = P.PlanContext(bars15=all15, book=book, bars_1h=pk, bars_5m=all5,
                            windows=wins)
        plans, _ = P.detect_all(ctx, params, kinds=kinds, require_window=True,
                                include_mirror=True)
        rs, seen, reasons = [], set(), {}
        for pl in plans:
            cts = int(pl.effective_claim_ts)
            if cts < t0 or cts > t1:
                continue
            k = (pl.kind, pl.direction, cts, round(float(pl.order_price), 2))
            if k in seen:
                continue
            seen.add(k)
            r, why = settle(pl, all15, all5, book, params, idx15)
            if r is None:
                continue
            rs.append(r)
            reasons[why] = reasons.get(why, 0) + 1
        print("%-34s %s" % (label, stat(rs)), flush=True)
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:4]
        if top:
            print("%34s 出场：%s" % ("", "  ".join("%s×%d" % (k, v)
                                                  for k, v in top)), flush=True)
        P.WINDOW_GATE.update(orig_gate)
        return rs

    print("== V0 现状：三引线不受闸，三条全开 ==", flush=True)
    run(list(P.KINDS), False, "  V0 全部形态")

    print("\n== V1 三引线也受闸 ==", flush=True)
    r1 = run([P.KIND_WICKS], True, "  V1 只三引线（窗口内）")

    print("\n== V2 三引线停用（只做受闸两条） ==", flush=True)
    run([P.KIND_INVERSE_V, P.KIND_EMA_REJECT], True, "  V2 倒V+EMA拒绝")

    print("\n== 结论 ==", flush=True)
    if r1:
        n = len(r1)
        tot = sum(r1)
        print("  三引线放进窗口闸后：%d 笔、总R %+.2f、均R %+.3f"
              % (n, tot, tot / n), flush=True)
        print("  → %s" % ("窗口内为正，纳入闸" if tot > 0
                          else "窗口内仍为负，停用"), flush=True)
    else:
        print("  三引线放进窗口闸后 0 笔（窗口内根本不触发）→ 停用无损失",
              flush=True)


if __name__ == "__main__":
    main()
