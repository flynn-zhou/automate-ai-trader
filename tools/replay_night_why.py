# -*- coding: utf-8 -*-
"""一夜复盘 step 4：把「形态成立但没开仓」的每一根查到底。

对每根 15m：
  · 跑 detect_all（生产口径，含窗口闸/去重），看有没有产出
  · 若没产出但单独调检测器有形态 → 逐条说明被哪道闸挡了
  · 对被挡掉的那条计划，用 thesis.run 在这之后的真实 K 线上跑一遍，
    看「如果当时真开了」会怎样（成交/止损/止盈/收盘价）
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


def build(t0, t1):
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
    return b15, b5, b1h, tags, vols, book


def ctx_at(cur15, b15, b5, b1h, tags, vols, book):
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
    return P.PlanContext(bars15=_b15, book=book, bars_1h=_b1h, bars_5m=_b5,
                         windows=wins)


def why_filtered(pl, ctx):
    """复现 detect_all 里的窗口闸，说清这条计划为什么被丢。"""
    kind = pl.kind
    if not P.WINDOW_GATE.get(kind, True):
        return "该形态不进窗口闸（不应该被丢）"
    bg = P.background_of(kind, pl.direction)
    space = P.CLAIM_MS.get(str(getattr(pl, "bar_space", "15m")), M15)
    ts = int(pl.effective_claim_ts) + space
    w = ctx.window_at(ts)
    if w is None:
        return "收盘 %s 处**没有开仓窗口**" % fmt(ts)
    if bg is not None and w.direction != bg:
        return ("窗口方向 %s ≠ 该形态要求的背景 %s（窗口 %s~%s）"
                % (w.direction, bg, fmt(w.start_ms), fmt(w.end_ms)))
    return "窗口闸通过（应该没被丢，需复查）"


def simulate(pl, b15, b5, b1h, tags, vols, book):
    """如果当时真开了，会怎样。用同一套状态机跑后续真实 K 线。"""
    th = pl.to_thesis() if hasattr(pl, "to_thesis") else None
    if th is None:
        return "（计划无法转成 thesis）"
    start = 0
    for i, b in enumerate(b15):
        if int(b["ts"]) == int(pl.effective_claim_ts):
            start = i
            break
    _b15 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b15]
    _b5 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b5]
    try:
        res = T.run(th, _b15, book, start_at=start, entry_bars=_b5,
                    claim_on_entry=bool(getattr(pl, "claim_on_entry", False)))
    except Exception as e:
        return "回放出错：%s" % str(e)[:60]
    acts = [(a.reason, round(float(a.price or 0), 1)) for a in res.actions]
    return "终态 %s  动作 %s" % (res.state, acts)


def main():
    t0 = parse_dt(sys.argv[1]) if len(sys.argv) > 1 else parse_dt("2026-11-25T22:00")
    t1 = parse_dt(sys.argv[2]) if len(sys.argv) > 2 else parse_dt("2026-11-26T08:15")
    b15, b5, b1h, tags, vols, book = build(t0, t1)
    params = production_params()
    cuts = [b["ts"] for b in b15 if t0 <= b["ts"] <= t1]

    for cur15 in cuts:
        ctx = ctx_at(cur15, b15, b5, b1h, tags, vols, book)
        try:
            plans, stats = P.detect_all(ctx, params, require_window=True,
                                        include_mirror=True)
        except Exception as e:
            print("%s  detect_all 异常 %s" % (fmt(cur15), str(e)[:50]))
            continue
        mine = [pl for pl in plans
                if int(pl.effective_claim_ts) == int(cur15)]
        # 单独调检测器，看有没有被闸挡掉的形态
        raw = []
        for kind in KINDS:
            d = P.PATTERN_SPEC[kind]["direction"]
            seq = ctx.series_for(kind)
            for dd in (d, P.DOWN if d == P.UP else P.UP):
                pl, why = P.DETECTORS[kind](len(seq) - 1, ctx, params, dd)
                if pl is not None and int(pl.effective_claim_ts) == int(cur15):
                    raw.append(pl)
        blocked = [pl for pl in raw if pl not in mine]
        out_w = sum(v for k, v in stats.items() if k.endswith("outside_window"))
        if not mine and not blocked:
            continue
        print("\n=== %s  收盘 %.1f ===" % (fmt(cur15), ctx.bars15[-1].c))
        if out_w:
            print("  窗口闸丢弃计数 outside_window = %d" % out_w)
        for pl in mine:
            print("  ✅ 产出：%s %s 挂 %.2f 止损 %.2f 止盈 %.2f"
                  % (pl.kind, pl.direction, pl.order_price, pl.stop_price,
                     pl.target_price))
        for pl in blocked:
            print("  ⛔ 被挡：%s %s 挂 %.2f 止损 %.2f 止盈 %.2f"
                  % (pl.kind, pl.direction, pl.order_price, pl.stop_price,
                     pl.target_price))
            print("       原因：%s" % why_filtered(pl, ctx))
            print("       若开了：%s"
                  % simulate(pl, b15, b5, b1h, tags, vols, book))


if __name__ == "__main__":
    main()
