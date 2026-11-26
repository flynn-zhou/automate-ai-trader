# -*- coding: utf-8 -*-
"""一夜复盘 step 3：逐根问「为什么这一根没开仓」。

对每根 15m 分别跑三个形态检测器，把它们各自给的 `why` 打出来 ——
这就是"该开却没开"的逐根答案：到底是形态真不成立，还是数据/闸挡的。
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
from ai_trader.trading import plan_source as PS
from ai_trader.trading import thesis as T


def main():
    t0 = parse_dt(sys.argv[1]) if len(sys.argv) > 1 else parse_dt("2026-11-25T22:00")
    t1 = parse_dt(sys.argv[2]) if len(sys.argv) > 2 else parse_dt("2026-11-26T08:15")

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
                " where symbol=? order by ts", ("BTCUSDT",)):
            book.add({"line_key": r[0], "ts": int(r[1]),
                      "line_price": float(r[2]), "line_ready_ms": int(r[3])})
    finally:
        con.close()

    params = production_params()
    idx15 = {b["ts"]: i for i, b in enumerate(b15)}
    cuts = [b["ts"] for b in b15 if t0 <= b["ts"] <= t1]
    print("逐根审计 %d 根：%s → %s" % (len(cuts), fmt(cuts[0]), fmt(cuts[-1])))
    print("%-16s %-9s %-8s %-22s %-22s %-22s"
          % ("时刻", "收盘", "涨跌", "三引线(反转)", "倒V(5m)", "EMA拒绝"))
    print("-" * 116)

    for cur15 in cuts:
        i = idx15[cur15]
        _b15 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"])
                for x in b15 if x["ts"] <= cur15]
        _b5 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"])
               for x in b5 if x["ts"] <= cur15]
        _b1h = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"])
                for x in b1h if x["ts"] <= cur15]
        tg = {k: v for k, v in tags.items() if k <= cur15}

        def tag_of(ts):
            return tg.get((int(ts) // M1H) * M1H, ("", ""))

        def line_of(ts, key):
            pt = book.at(key, int(ts) + M1H)
            return None if pt is None else float(pt.price)

        class _H1(object):
            __slots__ = ("ts", "o", "h", "l", "c", "v")

            def __init__(self, b):
                self.ts, self.o, self.h, self.l = b.ts, b.o, b.h, b.l
                self.c = b.c
                self.v = vols.get(int(b.ts), 0.0)

        wins = P.build_open_windows(
            [_H1(b) for b in _b1h], tag_of, line_of,
            vol_mult=PS.WINDOW_VOL_MULT, vol_win=PS.WINDOW_VOL_WIN,
            extra_bars=P.WINDOW_EXTRA_BARS)
        ctx = P.PlanContext(bars15=_b15, book=book, bars_1h=_b1h,
                            bars_5m=_b5, windows=wins)

        def _one(fn, idx, *a):
            try:
                pl, why = fn(idx, ctx, params, *a)
            except Exception as e:
                return "ERR:%s" % str(e)[:16]
            if pl is not None:
                return "★%s@%.0f" % (pl.direction, pl.order_price)
            return why or "?"

        w = _one(P.detect_wicks_false_break, len(_b15) - 1)
        v = _one(P.detect_inverse_v, len(_b5) - 1)
        e = _one(P.detect_ema_reject, len(_b15) - 1)
        chg = _b15[-1].c - _b15[-2].c if len(_b15) >= 2 else 0.0
        win = "WIN" if any(True for _ in wins) else "no-win"
        star = "  <<< " if ("★" in w or "★" in v or "★" in e) else ""
        print("%-16s %9.1f %+7.1f  %-22s %-22s %-22s %s%s"
              % (fmt(cur15), _b15[-1].c, chg, w, v, e, win, star))


if __name__ == "__main__":
    main()
