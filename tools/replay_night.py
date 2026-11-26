# -*- coding: utf-8 -*-
"""一夜复盘：逐根回放「该不该在该时刻开仓」，以及系统现状开不开得出来。

只读生产库，不下单、不写库。

用法:
    python /tmp/replay_night.py 2026-11-25T20:00 2026-11-26T08:00
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

BASE = os.environ.get("AI_TRADER_DATA", "/home/cryptobot/ai-trader")
#: 代码根：默认与数据同根。做参数扫描时指向一份拷贝，
#: 这样「改口径试跑」不碰线上应用目录。
sys.path.insert(0, os.environ.get("AI_TRADER_CODE", BASE))
RANGE_DB = os.path.join(BASE, "data/market_range.db")
KBAR_DB = os.path.join(BASE, "data/kbar.db")
SYMBOL = "BTCUSDT"
M15 = 900_000
M5 = 300_000
M1H = 3_600_000


def ro(p):
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True)


def fmt(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(int(ts) / 1000))


def parse_dt(s):
    return int(time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M")) * 1000)


# ------------------------------------------------------------------ 数据
def load_evolution(iv):
    con = ro(KBAR_DB)
    try:
        rows = con.execute(
            "select ts,o,h,l,c,vol from kbar_evolution"
            " where symbol=? and interval_ms=? order by ts",
            (SYMBOL, iv)).fetchall()
    finally:
        con.close()
    return [dict(ts=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]),
                 c=float(r[4]), v=float(r[5] or 0.0)) for r in rows]


def load_1h():
    con = ro(RANGE_DB)
    try:
        rows = con.execute(
            "select ts,open,high,low,close,volume from bars_1h"
            " where symbol=? order by ts", (SYMBOL,)).fetchall()
    finally:
        con.close()
    return [dict(ts=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]),
                 c=float(r[4]), v=float(r[5] or 0.0)) for r in rows]


def load_tags():
    con = ro(RANGE_DB)
    try:
        rows = con.execute(
            "select ts,tag,direction from range_evolution"
            " where symbol=? order by ts", (SYMBOL,)).fetchall()
    finally:
        con.close()
    return {int(r[0]): ((r[1] or ""), (r[2] or "")) for r in rows}


def load_h1_vols():
    con = ro(KBAR_DB)
    try:
        rows = con.execute(
            "select (ts/3600000)*3600000 as h, sum(vol) from kbar_evolution"
            " where symbol=? and interval_ms=? group by h",
            (SYMBOL, M15)).fetchall()
    finally:
        con.close()
    return {int(r[0]): float(r[1] or 0.0) for r in rows}


def production_params():
    """★ 必须和生产同一个来源：`engine._pending_params()`。

    它把 `max_stop_pct` 从 sizing.derive() 填进去。直接 `PendingParams()`
    会让止损静默落回振幅规则，止损距离和线上不是一个数。
    """
    from ai_trader.trading.pending_plan import PendingParams
    # ★ 口径必须和生产一致：`engine._max_stop_distance_pct` 返回的是**百分数**
    #   （1.618 表示 1.618%）。第一版我按小数 0.01618 传进去，止损距离变成
    #   0.016% —— 止损价和挂单价只差几分钱，`detect_all` 每根都抛
    #   「做多必须 止损 < 挂单 < 止盈」。那是我回放的 bug，不是生产的 bug
    #   （生产日志里「计划源出错」是 0 次，正好印证）。
    return PendingParams(max_stop_pct=1.618)


# ------------------------------------------------------------------ 回放
def main():
    t0 = parse_dt(sys.argv[1]) if len(sys.argv) > 1 else parse_dt("2026-11-25T20:00")
    t1 = parse_dt(sys.argv[2]) if len(sys.argv) > 2 else int(time.time() * 1000)

    b15 = load_evolution(M15)
    b5 = load_evolution(M5)
    b1h = load_1h()
    tags = load_tags()
    vols = load_h1_vols()
    print("数据：15m %d 根 / 5m %d 根 / 1h %d 根" % (len(b15), len(b5), len(b1h)))

    from ai_trader.trading import pending_plan as P
    from ai_trader.trading import plan_source as PS
    from ai_trader.trading import thesis as T

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
    print("max_stop_pct = %.6f" % float(params.max_stop_pct))

    # 逐根 15m 收盘复现 harvest
    cuts = [b["ts"] for b in b15 if t0 <= b["ts"] <= t1]
    print("\n回放 %d 根 15m：%s → %s" % (len(cuts), fmt(cuts[0]), fmt(cuts[-1])))

    def _ctx(cur15):
        _b15 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"])
                for x in b15 if x["ts"] <= cur15]
        _b5 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"])
               for x in b5 if x["ts"] <= cur15]
        _b1h = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"])
                for x in b1h if x["ts"] <= cur15]
        tg = {k: v for k, v in tags.items() if k <= cur15}
        vl = dict(vols)

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
                self.v = vl.get(int(b.ts), float(getattr(b, "v", 0.0) or 0.0))

        wins = P.build_open_windows(
            [_H1(b) for b in _b1h], tag_of, line_of,
            vol_mult=PS.WINDOW_VOL_MULT, vol_win=PS.WINDOW_VOL_WIN,
            extra_bars=P.WINDOW_EXTRA_BARS)
        return P.PlanContext(bars15=_b15, book=book, bars_1h=_b1h,
                             bars_5m=_b5, windows=wins)

    hits = []
    n_windows = 0
    for _i, cur15 in enumerate(cuts):
        if _i % 8 == 0:
            print("  ... %s (%d/%d)" % (fmt(cur15), _i, len(cuts)), flush=True)
        ctx = _ctx(cur15)
        if not ctx.windows:
            continue
        n_windows += 1
        try:
            plans, stats = P.detect_all(ctx, params, require_window=True,
                                        include_mirror=True)
        except Exception as e:
            print("  ! %s detect_all 出错：%s" % (fmt(cur15), e))
            continue
        for pl in plans:
            if int(pl.effective_claim_ts) != int(cur15):
                continue
            hits.append((cur15, pl, dict(stats or {})))

    print("有开仓窗口的根数：%d / %d" % (n_windows, len(cuts)))
    print("harvest 命中（effective_claim_ts == cur15）：%d 条" % len(hits))
    for cur15, pl, st in hits:
        print("  %s  %s %s 挂 %.2f 止损 %.2f 止盈 %.2f  claim_on_entry=%s"
              % (fmt(cur15), pl.kind, pl.direction, pl.order_price,
                 pl.stop_price, pl.target_price,
                 bool(getattr(pl, "claim_on_entry", False))))


if __name__ == "__main__":
    main()
