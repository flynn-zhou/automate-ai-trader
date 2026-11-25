# -*- coding: utf-8 -*-
"""挂单式买法回放器 CLI。

只读 kbar.db，不写库、不下单。

    # 单组参数跑一遍，列出每个信号
    python tools/replay_setup.py --days 14 --show-signals

    # 扫参数（一次只动一个，避免过拟合）
    python tools/replay_setup.py --days 14 --sweep

    # 指定方向（默认 short = 下跌趋势里做空；long = 上涨趋势镜像）
    python tools/replay_setup.py --days 14 --direction long --sweep

    # 去掉「带量突破区间」前置条件，看它到底有没有用
    python tools/replay_setup.py --days 14 --no-precondition
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import replay as R   # noqa: E402

SYM, IV = "BTCUSDT", 900000


def _f(ts):
    return dt.datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")


def load_bars(db: str, days: int, interval_ms: int = IV,
              symbol: str = SYM) -> list:
    """读出 15m bar + 每根的 ema20。没有 ema20 的根会被丢掉（回放不能猜）。"""
    lab = "15m" if interval_ms == 900000 else "5m"
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        lo = int(dt.datetime.now().timestamp() * 1000) - days * 86400 * 1000
        rows = con.execute(
            "select ts,o,h,l,c,up_wick_pct,dn_wick_pct,close_pos,vol_rel,event"
            " from kbar_evolution where symbol=? and interval_ms=? and ts>?"
            " and o is not null order by ts", (symbol, interval_ms, lo)).fetchall()
        ema = dict(con.execute(
            "select ts,line_price from kbar_line where symbol=? and interval_ms=?"
            " and line_key=? and ts>? and line_price is not null",
            (symbol, interval_ms, "ema20@" + lab, lo)).fetchall())
    finally:
        con.close()
    out, dropped = [], 0
    for r in rows:
        e = ema.get(r[0])
        if e is None:
            dropped += 1
            continue
        out.append({"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                    "up_wick_pct": r[5], "dn_wick_pct": r[6],
                    "close_pos": r[7], "vol_rel": r[8], "event": r[9],
                    "ema20": e})
    if dropped:
        sys.stderr.write("  [注意] 有 %d 根 %s 没有 ema20（预热不够），"
                         "回放已跳过 —— 这是覆盖缺口，不是没有信号\n"
                         % (dropped, lab))
    return out


GRID = {
    "wick_strong": (0.55, 0.65, 0.75),
    "wick_any": (0.25, 0.35, 0.50),
    "above_tol": (0.0005, 0.0015, 0.0030),
    "touch_tol": (0.0005, 0.0015, 0.0030),
    "vol_min": (2.0, 3.0, 5.0),
    "lookback": (4, 8, 16, 32),
    "max_wait": (4, 8, 16),
    "entry_wait": (2, 4, 8),
    "stop_frac": (0.25, 0.50),
    "stop_basis": ("15m", "1h"),
    "stop_bars": (1, 2),
    "min_stop_pct": (0.0, 0.2, 0.4, 0.6),
    "max_hold": (4, 8, 16),
}


def _row(line, s):
    return ("  %-14s %-6s 信号%-4d 触发%-4d 筛掉%-4d 成交%-4d "
            "止损率%s 胜率%s 均净%+.4f%% 均R%+.2f 止损中位%.3f%%"
            % (s["param"], str(line["value"]), s["signals"], s["triggered"],
               s["filtered"], s["settled"],
               "%.0f%%" % (s["stop_rate"] * 100),
               "%.0f%%" % (s["win_rate"] * 100), s["avg_net_pct"],
               s["avg_r"], s["median_stop_pct"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="挂单式买法规则回放")
    ap.add_argument("--kbar-db", default=os.path.join(ROOT, "data", "kbar.db"))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--interval-ms", type=int, default=IV)
    ap.add_argument("--direction", choices=("short", "long"), default="short")
    ap.add_argument("--stop-basis", choices=("15m", "1h"), default="15m")
    ap.add_argument("--stop-bars", type=int, default=2)
    ap.add_argument("--stop-frac", type=float, default=0.25)
    ap.add_argument("--min-stop-pct", type=float, default=0.0,
                    help="止损距离小于这个百分比就丢掉（%%），用来筛掉噪声信号")
    ap.add_argument("--no-precondition", action="store_true")
    ap.add_argument("--fee", type=float, default=R.DEFAULT_FEE_PCT)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--show-signals", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    bars = load_bars(args.kbar_db, args.days, args.interval_ms)
    if not bars:
        sys.stderr.write("没有可用 bar（缺 ema20 或库里没数据）\n")
        return 2
    print("回放窗口 %s → %s，%d 根带 ema20 的 15m；方向 %s；前置条件 %s"
          % (_f(bars[0]["ts"]), _f(bars[-1]["ts"]), len(bars), args.direction,
             "关" if args.no_precondition else "开"))

    base = R.Params(direction=args.direction,
                    require_precondition=not args.no_precondition,
                    stop_basis=args.stop_basis, stop_bars=args.stop_bars,
                    stop_frac=args.stop_frac,
                    min_stop_pct=args.min_stop_pct)
    sigs = R.find_signals(bars, base)
    s = R.summarize(sigs, args.fee)

    if args.json:
        print(json.dumps({"summary": s, "signals": sigs},
                         ensure_ascii=False, indent=2))
        return 0

    print("\n=== 基准参数 ===")
    for k, v in base.as_dict().items():
        print("  %-22s %s" % (k, v))
    print("\n  信号 %d ｜ 触发 %d ｜ 筛掉 %d（止损太窄）｜ 成交 %d ｜ "
          "止损率 %.0f%% ｜ 胜率 %.0f%% ｜ 均净 %+.4f%% ｜ 累计 %+.2f%% ｜ 均R %+.2f"
          % (s["signals"], s["triggered"], s["filtered"], s["settled"],
             s["stop_rate"] * 100, s["win_rate"] * 100, s["avg_net_pct"],
             s["sum_net_pct"], s["avg_r"]))
    print("  止损距离 中位 %.3f%% / 均值 %.3f%%　←如果只有零点零几，说明抓的不是"
          "「好几百点」那个形态" % (s["median_stop_pct"], s["avg_stop_pct"]))
    print("  MFE 均值 %+.2f%% ｜ MAE 均值 %+.2f%%（止盈该放哪，看这两个数）"
          % (s["avg_mfe_pct"], s["avg_mae_pct"]))

    if args.show_signals:
        print("\n=== 逐个信号 ===")
        for x in sigs:
            st = x.get("status")
            if st != "settled":
                print("  %s A收%.1f wick%.2f → %s"
                      % (_f(x["a_ts"]), x["a_close"], x["a_wick"], st))
                continue
            print("  A %s → B %s (等%d根) 触发%.1f 进场%.1f 止损%.1f → "
                  "%s 平 %.1f [%s] 净%+.2f%% R%+.2f MFE%+.2f MAE%+.2f"
                  % (_f(x["a_ts"]), _f(x["b_ts"]), x["wait"], x["trigger"],
                     x["entry"], x["stop"], _f(x["exit_ts"]), x["exit"],
                     x["why"], x["gross_pct"] - args.fee, x["r_multiple"],
                     x["mfe_pct"], x["mae_pct"]))

    if args.sweep:
        print("\n=== 单参数扫描（按均净降序）===")
        rows = R.sweep(bars, base, GRID)
        for r in rows:
            print(_row(r, r))
        best = rows[0]
        print("\n  最高：%s=%s 均净 %+.4f%%（成交 %d 次）—— **单参数最优不等于可用，"
              "要看它在相邻取值上是否连续**"
              % (best["param"], best["value"], best["avg_net_pct"],
                 best["settled"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
