# -*- coding: utf-8 -*-
"""买法三回放：1h 三次带量破区间 → 等拒绝 → 5m 量能根入场 → 1h 振幅止损。

只读 kbar.db。

    python tools/replay_setup2.py --days 30
    python tools/replay_setup2.py --days 30 --show
    python tools/replay_setup2.py --days 30 --sweep
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import setup2 as S   # noqa: E402

SYM = "BTCUSDT"


def f(ts, fmt="%m-%d %H:%M"):
    return dt.datetime.fromtimestamp(ts / 1000).strftime(fmt)


def load(db: str, days: int, symbol: str = SYM):
    """返回 (bars5, bars15, line_px)。只读。"""
    lo = int(dt.datetime.now().timestamp() * 1000) - days * 86400 * 1000
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        def rows(iv):
            return [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                     "vol": r[5], "vol_rel": r[6], "up_wick_pct": r[7],
                     "dn_wick_pct": r[8], "event": r[9]}
                    for r in con.execute(
                        "select ts,o,h,l,c,vol,vol_rel,up_wick_pct,dn_wick_pct,"
                        "event from kbar_evolution where symbol=? and"
                        " interval_ms=? and ts>? and o is not null order by ts",
                        (symbol, iv, lo))]

        b5 = rows(300000)
        b15 = rows(900000)
        line_px: Dict[tuple, float] = {}
        for key in (S.BREAK_LINES_DOWN + S.BREAK_LINES_UP
                    + ("ema20@15m",)):
            for ts, px in con.execute(
                    "select ts,line_price from kbar_line where symbol=? and"
                    " interval_ms=? and line_key=? and ts>?"
                    " and line_price is not null", (symbol, 900000, key, lo)):
                line_px[(ts, key)] = px
    finally:
        con.close()
    # 把 ema20 挂到每根 15m 上
    for b in b15:
        b["ema20"] = line_px.get((b["ts"], "ema20@15m"))
    return b5, b15, line_px


def run(db: str, days: int, gp: S.GateParams, rp: S.RejectParams,
        ep: S.EntryParams, verbose: bool = False) -> Dict:
    b5, b15, line_px = load(db, days)
    hours = S.aggregate_1h(b15)
    lines = S.lines_by_hour(line_px, hours)
    breaks = S.detect_breaks(hours, lines, gp)
    gates = S.gate_state(breaks, gp, hours)

    cases = S.find_pin_case(b5, rp) + S.find_ema_case(b15, rp)
    cases.sort(key=lambda c: c["at_ts"])

    picked = []
    for g in gates:
        for c in S.cases_after(g, cases):
            picked.append((g, c))
    # 去重：同一个拒绝形态会被多个闸门各自挑中一次（闸门区间会重叠），
    # 而 pin / ema 两种 case 也可能落在同一个 ts 上。不去重的话同一笔交易
    # 会被数 2–4 次 —— 实测 60 天「90 笔」里只有 33 笔是唯一的，
    # 累计收益被放大了 2.7 倍。这种重复不改变均值，但会把样本量吹大，
    # 让 p 值和胜率都看起来比实际可靠。
    picked = S.dedup_cases(picked)
    entries = S.dedup_entries(
        S.find_entries(b5, [c for _, c in picked], hours, ep))

    settled = [e for e in entries if e["status"] == "ok"]
    stops = []
    for e in settled:
        # 出场：max_hold_15m 根 15m 之后、或先触止损。
        #
        # ★ 从入场那根 **15m 之后** 开始判，不是从包含入场 5m 的那根开始。
        #   15m 的 OHLC 没有 bar 内先后顺序：入场是 5m 收盘价，而那根 15m 的
        #   最高点完全可能出现在入场之前（反弹的最高点）。从它开始判等于用
        #   「入场之前就发生过的高点」去证明止损被打 —— 未来函数，而且方向
        #   单一：只会把结果推向「全部止损」。实测第一版就是这样，8 组参数
        #   里 7 组止损率 100%，那个数字根本不是策略的，是判法的。
        idx = next((i for i, b in enumerate(b15) if b["ts"] >= e["entry_ts"]),
                   None)
        if idx is None:
            continue
        idx += 1
        if idx >= len(b15):
            continue
        end = min(len(b15) - 1, idx + ep.max_hold_15m)
        exit_px, why = b15[end]["c"], "time"
        for b in b15[idx:end + 1]:
            hi, lo = float(b["h"]), float(b["l"])
            if ep.direction == "down" and hi >= e["stop"]:
                exit_px, why = e["stop"], "stop"
                break
            if ep.direction == "up" and lo <= e["stop"]:
                exit_px, why = e["stop"], "stop"
                break
        gross = ((e["entry"] - exit_px) if ep.direction == "down"
                 else (exit_px - e["entry"])) / e["entry"] * 100.0
        e["exit"], e["why"], e["gross_pct"] = exit_px, why, gross
        if why == "stop":
            stops.append(e)

    out = {
        "bars5": len(b5), "bars15": len(b15), "hours": len(hours),
        "breaks": len(breaks),
        "heavy_breaks": sum(1 for b in breaks
                            if b["vol_rel"] and b["vol_rel"] >= gp.gate_vol),
        "gates": len(gates),
        "cases": len(cases),
        "picked_cases": len(picked),
        "entries": len(entries),
        "settled": len(settled),
        "stops": len(stops),
        "stop_rate": (len(stops) / float(len(settled))) if settled else 0.0,
    }
    if settled:
        win = [e for e in settled if e["gross_pct"] > 0.09]
        out["win_rate"] = len(win) / float(len(settled))
        out["avg_net_pct"] = sum(e["gross_pct"] - 0.09 for e in settled) / len(settled)
        out["sum_net_pct"] = sum(e["gross_pct"] - 0.09 for e in settled)
        pts = sorted(e["stop_pts"] for e in settled)
        out["median_stop_pts"] = pts[len(pts) // 2]
        out["avg_stop_pts"] = sum(pts) / len(pts)
    else:
        out.update({"win_rate": 0.0, "avg_net_pct": 0.0, "sum_net_pct": 0.0,
                    "median_stop_pts": 0.0, "avg_stop_pts": 0.0})

    if verbose:
        print("\n  === 闸门（1h 三次带量破区间）===")
        for g in gates:
            print("    %s → %s (%s)  破 %d 次  收 %.1f → %.1f  最后一条线 %.1f"
                  % (f(g["open_ts"]), f(g["close_ts"]),
                     "已证伪" if g["invalidated"] else "还开着",
                     g["n_breaks"], g["first_close"], g["last_close"],
                     g["last_line_px"]))
            for b in g["breaks"]:
                print("        %s 收 %8.1f < %-16s %8.1f 越 %6.1f 量比 %s"
                      % (f(b["h_ts"]), b["close"], b["line"].split("@")[0],
                         b["line_px"], b["beyond_pts"],
                         ("%.2f" % b["vol_rel"]) if b["vol_rel"] else "—"))
        print("\n  === 拒绝形态（闸门之后）===")
        for g, c in picked:
            print("    %s  闸门 %s 之后 %s  (%s)"
                  % (f(c["at_ts"]), f(g["close_ts"]), c["kind"], c["kind"]))
        print("\n  === 入场 ===")
        for e in entries:
            if e["status"] != "ok":
                print("    %s  %s" % (f(e["case"]["at_ts"]), e["status"]))
                continue
            print("    %s 入 %.1f（5m振幅 %.0f 量比 %.1f）止损 %.1f（%+.0f 点 / %.2f%%）"
                  " → 出 %.1f [%s] 净 %+.2f%%"
                  % (f(e["entry_ts"]), e["entry"], e["entry_rng"],
                     e["entry_vol_rel"] or 0, e["stop"], e["stop_pts"],
                     e["stop_pct"], e.get("exit", 0), e.get("why", "?"),
                     e.get("gross_pct", 0) - 0.09))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="买法三回放")
    ap.add_argument("--kbar-db", default=os.path.join(ROOT, "data", "kbar.db"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-breaks", type=int, default=3)
    ap.add_argument("--gate-vol", type=float, default=1.5)
    ap.add_argument("--lookback-h", type=int, default=12)
    ap.add_argument("--pin-count", type=int, default=2)
    ap.add_argument("--pin-pts", type=float, default=350.0)
    ap.add_argument("--ema-count", type=int, default=2)
    ap.add_argument("--entry-pts", type=float, default=350.0)
    ap.add_argument("--entry-vol", type=float, default=5.0)
    ap.add_argument("--stop-frac", type=float, default=0.25)
    ap.add_argument("--stop-bars", type=int, default=1)
    ap.add_argument("--min-stop-mult", type=float, default=0.0,
                    help="止损至少 = 入场那根 5m 振幅 × 这个倍数（0=不设下限）")
    ap.add_argument("--wait-bars", type=int, default=12,
                    help="拒绝形态之后等几根 5m 找量能根（默认 12 = 1 小时）")
    ap.add_argument("--direction", choices=("down", "up"), default="down")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    gp = S.GateParams(min_breaks=args.min_breaks, lookback_h=args.lookback_h,
                      gate_vol=args.gate_vol, direction=args.direction)
    rp = S.RejectParams(pin_count=args.pin_count, pin_pts=args.pin_pts,
                        ema_count=args.ema_count, direction=args.direction)
    ep = S.EntryParams(entry_pts=args.entry_pts, entry_vol=args.entry_vol,
                       stop_frac=args.stop_frac, stop_bars=args.stop_bars,
                       direction=args.direction,
                       min_stop_rng_mult=args.min_stop_mult,
                       wait_bars5=args.wait_bars)

    out = run(args.kbar_db, args.days, gp, rp, ep, verbose=args.show)
    print("回放 %d 天（5m %d 根 / 15m %d 根 / 1h %d 个）"
          % (args.days, out["bars5"], out["bars15"], out["hours"]))
    print("  1h 破位 %d 次（其中带量 %d）→ 开闸 %d 次"
          % (out["breaks"], out["heavy_breaks"], out["gates"]))
    print("  拒绝形态 %d 个 → 闸门之后 %d 个 → 成交 %d 笔（%d 个没等到量能根）"
          % (out["cases"], out["picked_cases"], out["settled"],
             out["entries"] - out["settled"]))
    if out["settled"]:
        print("  止损率 %.0f%% ｜ 胜率 %.0f%% ｜ 均净 %+.3f%% ｜ 累计 %+.2f%%"
              % (out["stop_rate"] * 100, out["win_rate"] * 100,
                 out["avg_net_pct"], out["sum_net_pct"]))
        print("  止损距离 中位 %.0f 点 / 均值 %.0f 点"
              % (out["median_stop_pts"], out["avg_stop_pts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
