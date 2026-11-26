# -*- coding: utf-8 -*-
"""一夜复盘 step 7：07:45 那条做空的反事实 + 窗口稀缺度 + 当前闸门状态。"""
from __future__ import annotations

import os
import sys
import time

BASE = "/home/cryptobot/ai-trader"
sys.path.insert(0, BASE)

from replay_night import (M15, M5, M1H, load_evolution, load_1h, load_tags,  # noqa
                          load_h1_vols, production_params, ro, KBAR_DB,
                          RANGE_DB, fmt, parse_dt)
from ai_trader.trading import pending_plan as P
from ai_trader.trading import plan_source as PS
from ai_trader.trading import thesis as T

SYMBOL = "BTCUSDT"


def main():
    b5 = load_evolution(M5)
    b15 = load_evolution(M15)
    b1h = load_1h()
    tags = load_tags()
    vols = load_h1_vols()

    # ---------- 1. 07:45 之后的价格 ----------
    t = parse_dt(sys.argv[1]) if len(sys.argv) > 1 else parse_dt("2026-11-26T07:45")
    print("== %s 之后的 5m ==" % fmt(t))
    n = 0
    for b in b5:
        if b["ts"] < t:
            continue
        n += 1
        if n > 24:
            break
        print("  %s  O%9.1f H%9.1f L%9.1f C%9.1f" %
              (fmt(b["ts"]), b["o"], b["h"], b["l"], b["c"]))

    # ---------- 2. 窗口稀缺度 ----------
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

    def tag_of(ts):
        v = tags.get((int(ts) // M1H) * M1H)
        return v if v else ("", "")

    def line_of(ts, key):
        pt = book.at(key, int(ts) + M1H)
        return None if pt is None else float(pt.price)

    class _H1(object):
        __slots__ = ("ts", "o", "h", "l", "c", "v")

        def __init__(self, b):
            self.ts, self.o, self.h, self.l = (int(b["ts"]), float(b["o"]),
                                               float(b["h"]), float(b["l"]))
            self.c = float(b["c"])
            self.v = vols.get(int(b["ts"]), 0.0)

    h1 = [_H1(b) for b in b1h]
    wins = P.build_open_windows(h1, tag_of, line_of,
                                vol_mult=PS.WINDOW_VOL_MULT,
                                vol_win=PS.WINDOW_VOL_WIN,
                                extra_bars=P.WINDOW_EXTRA_BARS)
    print("\n== 开仓窗口全历史（%d 个，1h 数据 %d 根 ≈ %.1f 天）=="
          % (len(wins), len(h1), len(h1) / 24.0))
    for w in wins:
        print("  %-4s %s → %s  (%.1f 小时)"
              % (w.direction, fmt(w.start_ms), fmt(w.end_ms),
                 (w.end_ms - w.start_ms) / 3_600_000.0))
    if len(h1) > 1:
        span_d = (h1[-1].ts - h1[0].ts) / 86_400_000.0
        cover = sum(min(w.end_ms, h1[-1].ts + M1H) - max(w.start_ms, h1[0].ts)
                    for w in wins) / 3_600_000.0
        print("  覆盖率：%.1f / %.0f 小时 = %.1f%%（策略可开仓的时间占比）"
              % (cover, span_d * 24, cover / (span_d * 24) * 100))

    # ---------- 3. 当前闸门 ----------
    print("\n== 当前交易闸门 ==")
    c = ro(os.path.join(BASE, "data/market_trade.db"))
    try:
        for tbl in ("guard_state", "risk_state", "state"):
            try:
                rows = c.execute("select * from %s" % tbl).fetchall()
            except Exception:
                continue
            cols = [d[0] for d in c.execute(
                "select * from %s limit 0" % tbl).description]
            for r in rows:
                d = dict(zip(cols, r))
                if tbl == "guard_state" or "halt" in str(d).lower():
                    print("  [%s] %s" % (tbl, d))
    finally:
        c.close()


if __name__ == "__main__":
    main()
