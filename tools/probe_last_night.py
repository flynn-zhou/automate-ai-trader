# -*- coding: utf-8 -*-
"""核对：昨夜（11-25 20:00 之后）是否有窗口、有可采集计划。"""
from __future__ import annotations

import os
import sys

BASE = "/home/cryptobot/ai-trader"
sys.path.insert(0, BASE)

from replay_night import production_params, fmt, ro, KBAR_DB  # noqa
from ai_trader.trading.plan_source import PlanSource, resolve_range_db
from ai_trader.trading import thesis as T

T0 = 1_795_608_000_000     # 2026-11-25 20:00 CST


def _book():
    book = T.LineBook()
    con = ro(KBAR_DB)
    try:
        for r in con.execute(
                "select line_key, ts, line_price, line_ready_ms from kbar_line"
                " where symbol='BTCUSDT' order by ts"):
            book.add({"line_key": r[0], "ts": int(r[1]),
                      "line_price": float(r[2]), "line_ready_ms": int(r[3])})
    finally:
        con.close()
    return book


def main():
    base = os.path.join(BASE, "data")
    src = PlanSource(book_provider=_book,
                     kbar_db=os.path.join(base, "kbar.db"),
                     range_db=resolve_range_db(base), symbol="BTCUSDT")
    ctx = src.context()
    print("== 覆盖 11-25 20:00 之后的窗口 ==")
    n = 0
    for w in ctx.windows:
        if int(w.end_ms) < T0:
            continue
        n += 1
        print("   %-4s %s → %s" % (w.direction, fmt(w.start_ms),
                                   fmt(w.end_ms)))
    if not n:
        print("   （无）")

    plans, _ = src.plans(production_params(), ctx)
    print("\n== 该区间内的计划（%d 条） ==" % len(plans))
    rows = [p for p in plans if int(p.effective_claim_ts) >= T0]
    for p in rows:
        print("   %s %s 挂 %.2f 止损 %.2f 止盈 %.2f  claim %s"
              % (p.kind, p.direction, p.order_price, p.stop_price,
                 p.target_price, fmt(p.effective_claim_ts)))
    if not rows:
        print("   （无）")
    print("\n合计：窗口 %d 个、计划 %d 条（全历史窗口 %d 个）"
          % (n, len(rows), len(ctx.windows or ())))


if __name__ == "__main__":
    main()
