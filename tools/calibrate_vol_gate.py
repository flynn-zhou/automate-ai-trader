# -*- coding: utf-8 -*-
"""量能门槛标定（回测）：把 vol_mult / vol_near 扫一遍，用真实数据夹出取值。

用户 2026-11-26 口径：
  量能门槛 = 附近出现「>= N 倍最近均量」的放量，N ∈ {3,4,5} 待定；
  **不一定是突破那一根**，看的是附近（前后各 vol_near 根 1h）的放量行为。

★ 效率：每个参数组合只建**一次**上下文、只调**一次** `detect_all`，
  然后对这一批计划逐条跑状态机结算。第一版按窗口重建上下文，
  单窗口要重扫 1.9 万根，12 组合跑了 8 分钟还一个字没吐。
★ 只读生产库；不下单、不写库。
"""
from __future__ import annotations

import itertools
import os
import sys

BASE = os.environ.get("AI_TRADER_DATA", "/home/cryptobot/ai-trader")
sys.path.insert(0, os.environ.get("AI_TRADER_CODE", BASE))

from replay_night import (M15, M5, M1H, load_evolution, load_1h, load_tags,  # noqa
                          load_h1_vols, production_params, ro, KBAR_DB, fmt,
                          parse_dt)
from ai_trader.trading import pending_plan as P
from ai_trader.trading import plan_source as PS
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
    except Exception as e:
        return None, "to_thesis_err:%s" % str(e)[:30], False
    start = idx15.get(int(pl.effective_claim_ts), 0)
    try:
        res = T.run(th, all15, book, start_at=start, entry_bars=all5,
                    claim_on_entry=bool(getattr(pl, "claim_on_entry", False)))
    except Exception as e:
        return None, "run_err:%s" % str(e)[:30], False
    entry, stop = float(pl.order_price), float(pl.stop_price)
    risk = abs(entry - stop)
    filled = any("fill" in (a.reason or "") for a in res.actions)
    close = None
    why = ""
    for a in res.actions:
        if a.kind == "close" and a.price is not None:
            close, why = float(a.price), (a.reason or "")
    if close is None or risk <= 0:
        return None, res.state, filled
    sign = 1.0 if str(pl.direction) == T.UP else -1.0
    return round((close - entry) * sign / risk, 3), res.state, filled


def main():
    t0 = parse_dt(sys.argv[1]) if len(sys.argv) > 1 else parse_dt("2026-08-01T00:00")
    t1 = parse_dt(sys.argv[2]) if len(sys.argv) > 2 else parse_dt("2026-11-26T08:00")

    b15 = load_evolution(M15)
    b5 = load_evolution(M5)
    b1h = load_1h()
    tags = load_tags()
    vols = load_h1_vols()
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

    print("1h %d 根（%s → %s）｜15m %d｜5m %d"
          % (len(hours), fmt(hours[0].ts), fmt(hours[-1].ts),
             len(all15), len(all5)), flush=True)

    def tag_of(ts):
        v = tags.get((int(ts) // M1H) * M1H)
        return v if v else ("", "")

    def line_of(ts, key):
        pt = book.at(key, int(ts) + M1H)
        return None if pt is None else float(pt.price)

    combos = list(itertools.product((2.0, 2.5, 3.0, 3.5, 4.0, 5.0), (0, 1)))
    print("\n%-7s %-5s %-6s %-6s %-6s %-6s %-7s %-7s %-7s"
          % ("量能门", "近邻", "窗口", "计划", "成交", "样本", "胜率", "总R", "均R"),
          flush=True)
    print("-" * 72, flush=True)

    for mult, near in combos:
        wins = P.build_open_windows(hours, tag_of, line_of,
                                    vol_mult=mult, vol_win=P.WINDOW_VOL_WIN,
                                    vol_near=near,
                                    start_tags=P.WINDOW_START_TAGS,
                                    extra_bars=P.WINDOW_EXTRA_BARS)
        ctx = P.PlanContext(bars15=all15, book=book, bars_1h=pk,
                            bars_5m=all5, windows=wins)
        try:
            plans, _st = P.detect_all(ctx, params, require_window=True,
                                      include_mirror=True)
        except Exception as e:
            print("  门 %.1f 近邻 %d：detect_all 出错 %s"
                  % (mult, near, str(e)[:50]), flush=True)
            continue
        n_fill = 0
        rs = []
        seen = set()
        for pl in plans:
            cts = int(pl.effective_claim_ts)
            if cts < t0 or cts > t1:
                continue
            key = (pl.kind, pl.direction, cts, round(float(pl.order_price), 2))
            if key in seen:
                continue
            seen.add(key)
            r, state, filled = settle(pl, all15, all5, book, params, idx15)
            if filled:
                n_fill += 1
            if r is not None:
                rs.append(r)
        tot = sum(rs)
        avg = tot / len(rs) if rs else 0.0
        wr = 100.0 * sum(1 for r in rs if r > 0) / len(rs) if rs else 0.0
        print("%-7.1f %-5d %-6d %-6d %-6d %-6d %-6.0f%% %-7.2f %-7.3f"
              % (mult, near, len(wins), len(plans), n_fill, len(rs), wr, tot,
                 avg), flush=True)


if __name__ == "__main__":
    main()
