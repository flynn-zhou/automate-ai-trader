# -*- coding: utf-8 -*-
"""一夜复盘 step 8：核对"终态 CLOSED_TP 但出场价低于做多入场价"这个矛盾。

如果属实，这是归因 bug —— 和用户抱怨的 "entry 记的数据肯定是错的" 同一类。
"""
from __future__ import annotations

import os
import sys

BASE = "/home/cryptobot/ai-trader"
sys.path.insert(0, BASE)

from replay_night import (M15, M5, M1H, load_evolution, load_1h, load_tags,  # noqa
                          load_h1_vols, production_params, ro, KBAR_DB, fmt,
                          parse_dt)
from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T
from ai_trader.trading import plan_source as PS

SYMBOL = "BTCUSDT"


def main():
    cur15 = parse_dt("2026-11-25T23:30")
    b15 = load_evolution(M15)
    b5 = load_evolution(M5)
    b1h = load_1h()
    tags = load_tags()
    vols = load_h1_vols()
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

    params = production_params()
    all15 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b15]
    all5 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b5]

    _b15 = [x for x in all15 if x.ts <= cur15]
    _b5 = [x for x in all5 if x.ts <= cur15]
    _b1h = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"])
            for x in b1h if x["ts"] <= cur15]
    tg = {k: v for k, v in tags.items() if k <= cur15}

    def tag_of(ts):
        v = tg.get((int(ts) // M1H) * M1H)
        return v if v else ("", "")

    def line_of(ts, key):
        pt = book.at(key, int(ts) + M1H)
        return None if pt is None else float(pt.price)

    class _H1(object):
        __slots__ = ("ts", "o", "h", "l", "c", "v")

        def __init__(self, b):
            self.ts, self.o, self.h, self.l = (int(b.ts), float(b.o),
                                               float(b.h), float(b.l))
            self.c = float(b.c)
            self.v = vols.get(int(b.ts), 0.0)

    wins = P.build_open_windows([_H1(b) for b in _b1h], tag_of, line_of,
                                vol_mult=PS.WINDOW_VOL_MULT,
                                vol_win=PS.WINDOW_VOL_WIN,
                                extra_bars=P.WINDOW_EXTRA_BARS)
    ctx = P.PlanContext(bars15=_b15, book=book, bars_1h=_b1h, bars_5m=_b5,
                        windows=wins)
    pl, why = P.detect_inverse_v(len(ctx.series_for(P.KIND_INVERSE_V)) - 1,
                                 ctx, params, P.UP)
    print("形态：", pl.kind if pl else None, why)
    if pl is None:
        return
    print("  direction=%s entry=%.2f stop=%.2f target=%.2f claim_ts=%s "
          "claim_on_entry=%s bar_space=%s"
          % (pl.direction, pl.order_price, pl.stop_price, pl.target_price,
             fmt(pl.effective_claim_ts), getattr(pl, "claim_on_entry", False),
             getattr(pl, "bar_space", "?")))

    th = P.to_thesis(pl, params=params)
    start = 0
    for i, b in enumerate(all15):
        if b.ts == int(pl.effective_claim_ts):
            start = i
            break
    res = T.run(th, all15, book, start_at=start, entry_bars=all5,
                claim_on_entry=bool(getattr(pl, "claim_on_entry", False)))
    print("  终态:", res.state)
    print("  动作:")
    for a in res.actions:
        print("    %-24s price=%s" % (a.reason, a.price))
    for k in ("entry_price", "exit_price", "entry_ts", "exit_ts", "fill_ts",
              "qty", "pnl", "net_r"):
        if hasattr(res, k):
            print("  %-12s = %s" % (k, getattr(res, k)))


if __name__ == "__main__":
    main()
