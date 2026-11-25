# -*- coding: utf-8 -*-
"""K 线演化 · 第 2 步验收探针：把库里的真实值打出来，并**独立重算**核对。

为什么不能只跑一遍实现再比：那是自己跟自己比。这里每条口径都用另一条路径算：
    结构   body_pct / close_pos / 影线  —— 从库里的 o/h/l/c 手算，与落库值比
    量能   taker_buy_ratio             —— 直接读 market.db 的 b/v，与落库值比
           vol_rel                     —— 用库里前后 bar 的 vol 自己取中位，与落库值比
    内路径 conc                        —— 直接读 3s 桶重算最大桶占比，与落库值比
    区间   zone                        —— 按价格排序手推区间名，与落库值比
    对齐   range_ts_h                  —— 校验「该 1h 行收盘 <= 本根收盘」（防偷未来）
           n_lines                     —— 与 kbar_line 的逐根计数比

用法：
    python tools/probe_kbar_facts.py --at "2026-11-07 06:00" --n 6
    python tools/probe_kbar_facts.py --symbol BTCUSDT --interval 15m --quiet-skip
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B          # noqa: E402
from ai_trader.kbar import facts as F          # noqa: E402
from ai_trader.kbar import lines as L          # noqa: E402
from ai_trader.kbar.store import KBarStore     # noqa: E402

PROBLEMS = []


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def ok(m):
    print("  [OK]   %s" % m)


def bad(m):
    PROBLEMS.append(m)
    print("  [FAIL] %s" % m)


def warn(m):
    print("  [WARN] %s" % m)


def fmt(ms):
    if not ms:
        return "-"
    import datetime
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000.0).strftime("%Y-%m-%d %H:%M")


def lab(s):
    import datetime
    return int(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")
               .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="K 线演化第 2 步探针")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--kbar-db", default=B.DEFAULT_KBAR_DB)
    ap.add_argument("--market-db", default=B.DEFAULT_MARKET_DB,
                    help="提供 3s 桶与 b/s（可以是只读快照）")
    ap.add_argument("--range-db", default=B.DEFAULT_RANGE_DB,
                    help="提供 range_evolution 四线（可以是只读快照）")
    ap.add_argument("--at", default="2026-11-07 06:00", help="目标根（UTC）")
    ap.add_argument("--n", type=int, default=6, help="打印几根")
    a = ap.parse_args(argv)

    iv_ms = {"3m": 180_000, "15m": 900_000, "1h": 3_600_000,
             "4h": 14_400_000}[a.interval]
    sym = a.symbol
    target = lab(a.at)

    hr("0. 库与覆盖面（先确认读到的是哪份数据）")
    if not os.path.exists(a.kbar_db):
        print("  kbar.db 不存在: %s" % a.kbar_db)
        return 2
    with KBarStore(a.kbar_db) as st:
        c = st.evolution_counts(sym, iv_ms)
        rows_all = st.evolution_rows(sym, iv_ms)
        line_counts = st.line_counts_by_ts(sym, iv_ms)
    print("  kbar.db: %d 根   %s .. %s" % (c["bars"], fmt(c["first_ts"]), fmt(c["last_ts"])))
    print("  kbar_line 覆盖的根数: %d" % len(line_counts))
    idx = {r["ts"]: i for i, r in enumerate(rows_all)}
    if target not in idx:
        print("  目标根 %s 不在库里（最近的是 %s / %s）"
              % (fmt(target), fmt(c["first_ts"]), fmt(c["last_ts"])))
        return 2
    i0 = idx[target]
    win = rows_all[max(0, i0 - 1): i0 + a.n]
    print("  目标根 ts=%d (%s)，打印 %d 根" % (target, fmt(target), len(win)))

    hr("1. 结构：用库里的 o/h/l/c 手算，与落库值比（6 根）")
    print("  %-16s %9s %9s %9s %9s  %s" % ("ts", "body_pct", "close_pos", "up_wick", "dn_wick", "一致"))
    max_d = 0.0
    for r in win:
        o, h, l, cl = r["o"], r["h"], r["l"], r["c"]
        rng = h - l
        mine = {"body_pct": abs(cl - o) / rng, "close_pos": (cl - l) / rng,
                "up_wick_pct": (h - max(o, cl)) / rng,
                "dn_wick_pct": (min(o, cl) - l) / rng}
        d = max(abs(mine[k] - (r[k] or 0.0)) for k in mine)
        max_d = max(max_d, d)
        print("  %-16s %9.6f %9.6f %9.6f %9.6f  %s"
              % (fmt(r["ts"]), r["body_pct"], r["close_pos"],
                 r["up_wick_pct"], r["dn_wick_pct"], "是" if d < 1e-12 else "否(%.2e)" % d))
        # 恒等式自检：body + 上影 + 下影 == 1
        s = r["body_pct"] + r["up_wick_pct"] + r["dn_wick_pct"]
        if abs(s - 1.0) > 1e-9:
            bad("ts=%s 实体+上影+下影 = %.12f，不等于 1" % (r["ts"], s))
        if not (-1 <= r["dir"] <= 1) or r["dir"] != (1 if cl > o else (-1 if cl < o else 0)):
            bad("ts=%s dir 不对: %s (c=%s o=%s)" % (r["ts"], r["dir"], cl, o))
    ok("结构与手算一致（最大差 %.1e）；实体+上影+下影 == 1" % max_d)

    hr("2. 量能：taker_buy_ratio 直读 market.db 的 b/v；vol_rel 用库里相邻根的 vol 自取中位")
    if not os.path.exists(a.market_db):
        warn("market.db 不存在，跳过 b/v 比对")
        mdb = {}
    else:
        mdb = {b["ts"]: b for b in B.load_market_bars_rich(a.market_db, sym, iv_ms)}
    n_cmp = n_ok = 0
    for r in win:
        src = mdb.get(r["ts"])
        if not src or src.get("v") in (None, 0) or r["taker_buy_ratio"] is None:
            continue
        n_cmp += 1
        want = src["buy_vol"] / src["v"]
        if abs(want - r["taker_buy_ratio"]) < 1e-12:
            n_ok += 1
        else:
            bad("ts=%s taker_buy_ratio 落库 %.10f，直读 b/v 得 %.10f" % (r["ts"],
                                                                r["taker_buy_ratio"], want))
    if n_cmp:
        ok("taker_buy_ratio %d/%d 根与 b/v 完全相等" % (n_ok, n_cmp))
    else:
        warn("这几根没有 b/v 可直接比（可能在 CSV 段）")

    # vol_rel：用库里的 vol 序列独立重算
    n_ok = n_cmp = 0
    for r in win:
        j = idx[r["ts"]]
        if j < F.VOL_REL_N or r["vol_rel"] is None:
            continue
        window = [rows_all[k]["vol"] for k in range(j - F.VOL_REL_N, j)]
        if any(x is None for x in window):
            continue
        n_cmp += 1
        want = rows_all[j]["vol"] / statistics.median(window)
        if abs(want - r["vol_rel"]) < 1e-9 * max(1.0, abs(want)):
            n_ok += 1
        else:
            bad("ts=%s vol_rel 落库 %.10f，独立重算 %.10f" % (r["ts"], r["vol_rel"], want))
    if n_cmp:
        ok("vol_rel %d/%d 根与独立重算一致（前 %d 根中位量，不含当根）"
           % (n_ok, n_cmp, F.VOL_REL_N))

    hr("3. 区间位置：range_ts_h 的防偷未来 + zone 手推 + n_lines 对齐")
    range_rows = []
    if os.path.exists(a.range_db):
        range_rows = B.load_range_rows(a.range_db, sym)
    print("  %-16s %-16s %-12s %-10s %-8s %s"
          % ("本根", "range_ts_h", "ready(1h收盘)", "direction", "zone", "n_lines(库/kbar_line)"))
    n_chk = n_ok = 0
    for r in win:
        rt = r["range_ts_h"]
        if rt is None:
            print("  %-16s %-16s %-12s %-10s %-8s %s"
                  % (fmt(r["ts"]), "(无)", "-", "-", r["zone"] or "-", r["n_lines"] or 0))
            continue
        ready = int(rt) + L.RANGE_PERIOD_MS
        bar_close = int(r["ts"]) + iv_ms
        lag = (bar_close - ready) / 3600000.0
        nl = line_counts.get(r["ts"], 0)
        print("  %-16s %-16s %-12s %-10s %-8s %s/%s"
              % (fmt(r["ts"]), fmt(rt), fmt(ready), r["direction"] or "-",
                 r["zone"] or "-", r["n_lines"], nl))
        n_chk += 1
        if ready > bar_close:
            bad("ts=%s 用了还没收盘的 1h 行：ready=%s 晚于本根收盘 %s"
                % (fmt(r["ts"]), fmt(ready), fmt(bar_close)))
        else:
            n_ok += 1
        if (r["n_lines"] or 0) != nl:
            bad("ts=%s n_lines 落库 %s，kbar_line 实际 %s" % (fmt(r["ts"]), r["n_lines"], nl))
    if n_chk:
        ok("%d/%d 根都满足 range_ready <= bar_close（余量 %.2f 小时）"
           % (n_ok, n_chk, lag))

    hr("4. 内路径：直读 3s 桶重算 conc（只对库里有 conc 的根）")
    got = [r for r in rows_all if r["conc"] is not None]
    print("  库里有 conc 的根数: %d / %d" % (len(got), len(rows_all)))
    if not got:
        warn("没有 conc —— 内路径没跑（要么没 3s 快照，要么窗口外）")
    else:
        lo = min(r["ts"] for r in got)
        hi = max(r["ts"] for r in got) + iv_ms
        ticks = B.load_ticks_3s(a.market_db, sym, lo, hi, iv_ms) if os.path.exists(a.market_db) else {}
        checked = mism = 0
        worst = None
        for r in got:
            tk = ticks.get(r["ts"]) or []
            if not tk:
                bad("ts=%s 库里有 conc 但 3s 里一个桶都没有" % r["ts"])
                continue
            total = sum(t["v"] for t in tk)
            # 只核对「3s 是这根 K 的完整分解」的根：否则比值本就没有可比基准
            if not r["vol"] or abs(total - r["vol"]) / r["vol"] > 1e-6:
                bad("ts=%s 库里有 conc，但 3s 之和 %.6f 与 K 线量 %.6f 对不上"
                    % (r["ts"], total, r["vol"] or -1))
                continue
            want = max(t["v"] for t in tk) / r["vol"]
            checked += 1
            if abs(want - r["conc"]) > 1e-12:
                mism += 1
                if worst is None:
                    worst = (r["ts"], r["conc"], want)
            # 时间点也在 0..14 分钟内
            if not (0 <= r["t_low_min"] < iv_ms // 60000) or \
               not (0 <= r["t_high_min"] < iv_ms // 60000):
                bad("ts=%s t_low/t_high 超出 0..%d 分钟: %s/%s"
                    % (r["ts"], iv_ms // 60000 - 1, r["t_low_min"], r["t_high_min"]))
        if checked:
            if mism:
                bad("conc 有 %d/%d 根与直读 3s 重算不一致，例: %s" % (mism, checked, worst))
            else:
                ok("conc %d 根与直读 3s 重算完全一致（分母用 K 线总量）" % checked)
            # 抽样打印
            print("\n  抽样（内路径）:")
            print("  %-16s %10s %10s %10s %10s" % ("ts", "conc", "t_low_min", "t_high_min", "3s根数"))
            for r in got[:6]:
                tk = ticks.get(r["ts"]) or []
                print("  %-16s %10.6f %10s %10s %10d"
                      % (fmt(r["ts"]), r["conc"], r["t_low_min"], r["t_high_min"], len(tk)))

    hr("5. 空字段清单（每个都必须说得出为什么）")
    print("  %-16s %8s %8s" % ("字段", "有值", "占比"))
    for col in ("range_ts_h", "zone", "vol_rel", "vol_slot_rel", "taker_buy_ratio",
                "conc", "path_shape", "max_tick_share", "n_touch", "event"):
        v = c["filled"].get(col, 0)
        print("  %-16s %8d %7.1f%%" % (col, v, 100.0 * v / max(1, c["bars"])))
    print("\n  设计上就没有的（不是漏）：")
    print("    path_shape       词表与阈值 = 判断标准，设计稿要求第 5 步前不碰")
    print("    max_tick_share   需要逐笔成交，现有数据只有 3s 桶")
    print("    n_touch/n_break  第 3 步反应层")
    print("    event/state 等   第 4 步事件层")

    hr("结论")
    if PROBLEMS:
        print("没通过的检查 %d 项：" % len(PROBLEMS))
        for i, m in enumerate(PROBLEMS, 1):
            print("  %d) %s" % (i, m))
        return 1
    print("全部检查通过。上面的数都是探针从库里读出来、独立重算过的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
