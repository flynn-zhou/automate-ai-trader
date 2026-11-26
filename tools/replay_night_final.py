# -*- coding: utf-8 -*-
"""一夜复盘 step 5：窗口时间线 + 反事实盈亏。

回答用户的两个问题：
  1. 什么时刻应该开仓而没开 —— 逐条给出被挡的计划
  2. 如果真开了会怎样 —— 用同一套状态机在真实后续 K 线上跑
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

SYMBOL = "BTCUSDT"
KINDS = list(P.KINDS)


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
                " where symbol=? order by ts", (SYMBOL,)):
            book.add({"line_key": r[0], "ts": int(r[1]),
                      "line_price": float(r[2]), "line_ready_ms": int(r[3])})
    finally:
        con.close()

    # ---------- 1. 窗口时间线 ----------
    def tag_of(ts):
        v = tags.get((int(ts) // M1H) * M1H)
        return v if v else ("", "")

    def line_of(ts, key):
        pt = book.at(key, int(ts) + M1H)
        return None if pt is None else float(pt.price)

    class _H1(object):
        __slots__ = ("ts", "o", "h", "l", "c", "v")

        def __init__(self, b):
            # load_1h() 返回的是 dict（replay_night 的口径），不是 Bar
            g = b.get if isinstance(b, dict) else (lambda k, d=None: getattr(b, k, d))
            self.ts, self.o, self.h, self.l = (int(g("ts")), float(g("o")),
                                               float(g("h")), float(g("l")))
            self.c = float(g("c"))
            self.v = vols.get(int(self.ts), 0.0)

    h1 = [_H1(b) for b in b1h]
    wins = P.build_open_windows(h1, tag_of, line_of,
                                vol_mult=PS.WINDOW_VOL_MULT,
                                vol_win=PS.WINDOW_VOL_WIN,
                                extra_bars=P.WINDOW_EXTRA_BARS)
    print("== 开仓窗口（全历史 %d 个），本时段内的：==" % len(wins))
    shown = 0
    for w in wins:
        if w.end_ms < t0 or w.start_ms > t1:
            continue
        shown += 1
        print("  %-8s %s → %s" % (w.direction, fmt(w.start_ms), fmt(w.end_ms)))
    if not shown:
        print("  （无）")
    print("  窗口总数 %d；本时段覆盖 %d" % (len(wins), shown))

    # ---------- 2. 被挡的计划 + 反事实 ----------
    params = production_params()
    idx15 = {b["ts"]: i for i, b in enumerate(b15)}
    cuts = [b["ts"] for b in b15 if t0 <= b["ts"] <= t1]
    all15 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b15]
    all5 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b5]

    print("\n== 形态成立但被窗口闸挡掉的（本时段） ==")
    found = 0
    for cur15 in cuts:
        _b15 = [x for x in all15 if x.ts <= cur15]
        _b5 = [x for x in all5 if x.ts <= cur15]
        _b1h = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"])
                for x in b1h if x["ts"] <= cur15]
        tg = {k: v for k, v in tags.items() if k <= cur15}

        def _tag(ts, tg=tg):
            v = tg.get((int(ts) // M1H) * M1H)
            return v if v else ("", "")

        _wins = P.build_open_windows(
            [_H1(x) for x in _b1h], _tag, line_of,
            vol_mult=PS.WINDOW_VOL_MULT, vol_win=PS.WINDOW_VOL_WIN,
            extra_bars=P.WINDOW_EXTRA_BARS)
        ctx = P.PlanContext(bars15=_b15, book=book, bars_1h=_b1h,
                            bars_5m=_b5, windows=_wins)
        for kind in KINDS:
            d0 = P.PATTERN_SPEC[kind]["direction"]
            seq = ctx.series_for(kind)
            for d in (d0, P.DOWN if d0 == P.UP else P.UP):
                pl, why = P.DETECTORS[kind](len(seq) - 1, ctx, params, d)
                if pl is None or int(pl.effective_claim_ts) != int(cur15):
                    continue
                if not P.WINDOW_GATE.get(kind, True):
                    continue
                bg = P.background_of(kind, pl.direction)
                space = P.CLAIM_MS.get(str(getattr(pl, "bar_space", "15m")), M15)
                w = ctx.window_at(int(pl.effective_claim_ts) + space)
                if w is not None and (bg is None or w.direction == bg):
                    continue        # 没被挡
                found += 1
                print("\n  %s  收盘 %.1f" % (fmt(cur15), _b15[-1].c))
                print("    形态：%s %s  挂 %.2f  止损 %.2f  止盈 %.2f"
                      % (pl.kind, pl.direction, pl.order_price,
                         pl.stop_price, pl.target_price))
                print("    被挡：%s" % ("无窗口" if w is None
                                       else "窗口方向 %s ≠ 背景 %s"
                                            % (w.direction, bg)))
                # 反事实：真开了会怎样
                try:
                    th = P.to_thesis(pl, params=params)
                except Exception as e:
                    print("    反事实：to_thesis 失败 %s" % str(e)[:50])
                    continue
                start = idx15.get(int(pl.effective_claim_ts), 0)
                try:
                    res = T.run(th, all15, book, start_at=start,
                                entry_bars=all5,
                                claim_on_entry=bool(getattr(pl,
                                                            "claim_on_entry",
                                                            False)))
                except Exception as e:
                    print("    反事实：回放失败 %s" % str(e)[:60])
                    continue
                acts = [(a.reason, (round(float(a.price), 1)
                                    if a.price else None)) for a in res.actions]
                print("    反事实：终态 %s" % res.state)
                for a, p in acts:
                    print("              %s @ %s" % (a, p))
    if not found:
        print("  （无）")


if __name__ == "__main__":
    main()
