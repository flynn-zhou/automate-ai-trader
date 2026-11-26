# -*- coding: utf-8 -*-
"""用**生产同一套**计划源，问「此刻能不能开出来」。

★ 第一版传了空的 `LineBook()`，于是 `line_of` 恒 None、没有任何穿越、
  窗口必然是 0 —— 那不是系统的问题，是探针的问题。这里装上真实线表。
"""
from __future__ import annotations

import os
import sys
import time

BASE = os.environ.get("AI_TRADER_DATA", "/home/cryptobot/ai-trader")
sys.path.insert(0, os.environ.get("AI_TRADER_CODE", BASE))

from replay_night import production_params, fmt, ro, KBAR_DB  # noqa
from ai_trader.trading.plan_source import PlanSource, resolve_range_db
from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T


def main():
    base = os.path.join(BASE, "data")
    range_db = resolve_range_db(base)
    kbar_db = os.path.join(base, "kbar.db")
    print("区间库 %s" % range_db)

    # ★ 真实线表（生产 `main.kbar_lines_for` 装的就是这些）
    def _book():
        book = T.LineBook()
        con = ro(kbar_db)
        try:
            for r in con.execute(
                    "select line_key, ts, line_price, line_ready_ms from kbar_line"
                    " where symbol='BTCUSDT' order by ts"):
                book.add({"line_key": r[0], "ts": int(r[1]),
                          "line_price": float(r[2]),
                          "line_ready_ms": int(r[3])})
        finally:
            con.close()
        return book

    src = PlanSource(book_provider=_book, kbar_db=kbar_db,
                     range_db=range_db, symbol="BTCUSDT")
    ctx = src.context()
    print("bar15 %d 根 最新 %s | bar5m %d 根 最新 %s"
          % (len(ctx.bars15), fmt(ctx.bars15[-1].ts),
             len(ctx.bars5), fmt(ctx.bars5[-1].ts)))
    wins = list(ctx.windows or ())
    print("开仓窗口 %d 个：%s"
          % (len(wins),
             "无" if not wins else ""))
    for w in wins[-6:]:
        print("   %-4s %s → %s" % (w.direction, fmt(w.start_ms),
                                   fmt(w.end_ms)))
    if wins:
        print("   最近窗口距现在 %.1f 小时"
              % ((int(time.time() * 1000) - wins[-1].end_ms) / 3_600_000.0))

    params = production_params()
    t = time.time()
    plans, stats = src.plans(params, ctx)
    print("\nplans() 用时 %.1fs，产出 %d 条（启用形态 %d 条）"
          % (time.time() - t, len(plans), len(P.ENABLED_KINDS)))
    now15 = int(ctx.bars15[-1].ts)
    for pl in plans:
        print("  %s %s 挂 %.2f claim %s %s"
              % (pl.kind, pl.direction, pl.order_price,
                 fmt(pl.effective_claim_ts),
                 "★当前这根" if int(pl.effective_claim_ts) == now15 else ""))
    kinds = {}
    for pl in plans:
        kinds[pl.kind] = kinds.get(pl.kind, 0) + 1
    if kinds:
        print("按形态：%s" % kinds)
    print("\n→ 当前这根（%s）%s可采集的计划"
          % (fmt(now15),
             "有" if any(int(p.effective_claim_ts) == now15
                         for p in plans) else "没有"))


if __name__ == "__main__":
    main()
