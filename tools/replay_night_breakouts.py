# -*- coding: utf-8 -*-
"""一夜复盘 step 10：逐个「突破」事件查它为什么没长出开仓窗口。

`build_open_windows` 要同时满足 6 条：
  1) tag == 突破 且 direction 与窗口方向一致
  2) 不是第一根
  3) 顺向边界线在该 1h 收盘时已就绪
  4) 那根 1h 真的**穿过**了那条线（prev_close → close）
  5) 量 >= vol_mult × 前 24h 中位量
  6) 后面紧跟同方向的 forming 段
哪一条不满足，窗口就不存在 —— 而窗口不存在 = 一条计划都不会产生。
"""
from __future__ import annotations

import os
import sys
import time

BASE = "/home/cryptobot/ai-trader"
sys.path.insert(0, BASE)

from replay_night import M1H, load_1h, load_tags, load_h1_vols, ro, KBAR_DB, fmt, parse_dt  # noqa
from ai_trader.trading import pending_plan as P
from ai_trader.trading import plan_source as PS
from ai_trader.trading import thesis as T


def main():
    t0 = parse_dt(sys.argv[1]) if len(sys.argv) > 1 else parse_dt("2026-11-18T00:00")
    b1h = load_1h()
    tags = load_tags()
    vols = load_h1_vols()

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

    hours = [_H1(b) for b in b1h]
    idx = {h.ts: i for i, h in enumerate(hours)}

    # 汇总每个方向的「突破」事件
    print("== 本时段所有「突破」事件逐条体检 ==")
    print("（vol_mult=%.2f, vol_win=%d）\n"
          % (PS.WINDOW_VOL_MULT, PS.WINDOW_VOL_WIN))
    n_ev = n_win = 0
    for h in hours:
        if h.ts < t0:
            continue
        tag, tdir = tag_of(h.ts)
        if tag != P.TAG_BREAKOUT:
            continue
        n_ev += 1
        direction = P.DOWN if tdir == P._RANGE_DIR[P.DOWN] else P.UP
        if tdir not in (P._RANGE_DIR[P.DOWN], P._RANGE_DIR[P.UP]):
            print("  %s  突破 tdir=%r 不认识，跳过" % (fmt(h.ts), tdir))
            continue
        key = P.FORWARD_BOUNDARY[direction]
        i = idx[h.ts]
        px = line_of(h.ts, key)
        prev_close = hours[i - 1].c if i > 0 else None
        vols_slice = [hours[j].v for j in range(max(0, i - PS.WINDOW_VOL_WIN), i)]
        ref = P._median(vols_slice)
        ratio = (h.v / ref) if ref > 0 else 0.0
        down = direction == P.DOWN
        crossed = (prev_close is not None and px is not None and
                   ((prev_close >= px > h.c) if down else (prev_close <= px < h.c)))
        # 往后找 forming
        forming = []
        for nxt in hours[i + 1:]:
            t2, d2 = tag_of(nxt.ts)
            if t2 in (P.TAG_BREAKOUT, P.TAG_REVERSAL):
                break
            if t2 == P.TAG_FORMING and d2 == tdir:
                forming.append(nxt.ts)
        ok = (i > 0 and px is not None and crossed and ratio >= PS.WINDOW_VOL_MULT
              and bool(forming))
        n_win += 1 if ok else 0
        print("  %s  %s  收盘 %.1f" % (fmt(h.ts), direction, h.c))
        print("     线 %-18s = %s" % (key, ("%.1f" % px) if px else "★未就绪"))
        if prev_close is not None:
            print("     穿越：前收 %.1f → 今收 %.1f  线 %.1f  %s"
                  % (prev_close, h.c, px or 0,
                     "穿过" if crossed else "★没穿过"))
        print("     量比 %.2f（门 %.2f）%s"
              % (ratio, PS.WINDOW_VOL_MULT,
                 "" if ratio >= PS.WINDOW_VOL_MULT else "  ★量不够"))
        print("     后续 forming：%d 根 %s"
              % (len(forming), "" if forming else "  ★后面没长新区间"))
        print("     → %s\n" % ("✅ 开窗口" if ok else "❌ 无窗口"))

    wins = P.build_open_windows(hours, tag_of, line_of,
                                vol_mult=PS.WINDOW_VOL_MULT,
                                vol_win=PS.WINDOW_VOL_WIN,
                                extra_bars=P.WINDOW_EXTRA_BARS)
    print("突破事件 %d 个，其中长出窗口 %d 个；build_open_windows 实得 %d 个"
          % (n_ev, n_win, len(wins)))


if __name__ == "__main__":
    main()
