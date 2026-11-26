# -*- coding: utf-8 -*-
"""量能门槛标定 v2：按形态拆开量，只对「受窗口闸约束」的形态比 N。

★ v1 的错误（已修）：把三条形态混在一起算平均 R，而
  `WINDOW_GATE = {三引线: False}` —— 三引线**不受窗口闸/量能门约束**
  （它自己的状态机就是它的前提）。于是 1376 条计划里绝大多数是三引线，
  量能门调 2.0→3.0 只让窗口从 16 掉到 12，均 R 却几乎不动（-0.49 → -0.51），
  看起来"门槛无关"，其实是被三引线稀释了。

本脚本分三块报告：
  A. 受窗口闸约束的形态（倒V / EMA拒绝）在各 N 下的表现 → 用来定 N
  B. 三引线（不受窗口闸）单独一块 → 它的盈亏与 N 无关，但必须一起看
  C. 合并口径
"""
from __future__ import annotations

import os
import sys

BASE = os.environ.get("AI_TRADER_DATA", "/home/cryptobot/ai-trader")
sys.path.insert(0, os.environ.get("AI_TRADER_CODE", BASE))

from replay_night import (M15, M5, M1H, load_evolution, load_1h, load_tags,  # noqa
                          load_h1_vols, production_params, ro, KBAR_DB, fmt,
                          parse_dt)
from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T

SYMBOL = "BTCUSDT"


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
        return "样本 0"
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

    gated = {P.KIND_INVERSE_V, P.KIND_EMA_REJECT}   # 受窗口闸约束的两条
    print("受窗口闸约束：%s" % "、".join(sorted(gated)), flush=True)
    print("不受约束（自带状态机）：%s\n" % P.KIND_WICKS, flush=True)

    for near in (1, 2):
        print("=" * 78, flush=True)
        print("附近放量半径 vol_near = %d 根 1h" % near, flush=True)
        print("=" * 78, flush=True)
        for mult in (2.0, 3.0, 3.5, 4.0, 5.0):
            wins = P.build_open_windows(hours, tag_of, line_of, vol_mult=mult,
                                        vol_win=P.WINDOW_VOL_WIN, vol_near=near,
                                        start_tags=P.WINDOW_START_TAGS,
                                        extra_bars=P.WINDOW_EXTRA_BARS)
            ctx = P.PlanContext(bars15=all15, book=book, bars_1h=pk,
                                bars_5m=all5, windows=wins)
            plans, _ = P.detect_all(ctx, params, require_window=True,
                                     include_mirror=True)
            rg, rw = [], []
            seen = set()
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
                (rg if pl.kind in gated else rw).append(r)
            print("\n  N = %.1f  窗口 %d 条" % (mult, len(wins)), flush=True)
            print("    [受闸] " + stat(rg), flush=True)
            print("    [三引] " + stat(rw), flush=True)
            print("    [合计] " + stat(rg + rw), flush=True)


if __name__ == "__main__":
    main()
