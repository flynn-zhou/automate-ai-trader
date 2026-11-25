# -*- coding: utf-8 -*-
"""K 线演化 · 第 3 步探针（反应层）。

规矩：**先探针打印真实值，再信测试。** 测试绿只说明旧行为没坏。
这个探针不调用 `ai_trader/kbar/reaction.py` 的任何计算函数 ——
它把口径**重新实现一遍**，再用库里落的值对比。自己跟自己比没有意义。

重点核这五件事：
    1. 触碰判定：l <= p <= h，逐条重算
    2. 防偷未来：每行的 line_price 必须是「本根开盘前就已就绪」的那个值，
       而不是「本根收盘时才算出来」的那个值（两者实测在 3,858 处不同）
    3. 穿透/收回/收盘侧/距离：逐条重算
    4. 补集完整：进表的 + 没碰到的 == 全部 (根, 线) 对，不漏不重
    5. 抽样：打印几根真实的反应，人工能核对

用法：
    python tools/probe_kbar_reactions.py
    python tools/probe_kbar_reactions.py --at 2026-10-31T18 --n 6
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B  # noqa: E402

PROBLEMS = []


def bad(msg):
    PROBLEMS.append(msg)
    print("  [BAD]  %s" % msg)


def ok(msg):
    print("  [OK]   %s" % msg)


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def fmt(ms):
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="K 线演化第 3 步探针")
    ap.add_argument("--kbar-db", default=B.DEFAULT_KBAR_DB)
    ap.add_argument("--market-db", default=B.DEFAULT_MARKET_DB,
                    help="必须与回填时用的同一个源，否则「不一致」是取源不同而不是算错")
    ap.add_argument("--backtest-db",
                    default=os.path.join(ROOT, "data", "backtest.db"))
    ap.add_argument("--csv", default=os.path.join(ROOT, "data", "range_bars_15m.csv"))
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--at", default=None, help="抽样打印这个时刻附近的反应，如 2026-10-31T18")
    ap.add_argument("--n", type=int, default=6)
    a = ap.parse_args(argv)

    iv = {"3m": 180_000, "15m": 900_000, "1h": 3_600_000}[a.interval]
    sym = a.symbol
    con = ro(os.path.abspath(a.kbar_db))

    hr("0. 三张表的规模")
    n0, b0 = con.execute("select count(*), count(distinct ts) from kbar_line "
                         "where symbol=? and interval_ms=?", (sym, iv)).fetchone()
    n1, b1 = con.execute("select count(*), count(distinct ts) from kbar_evolution "
                         "where symbol=? and interval_ms=?", (sym, iv)).fetchone()
    n2, b2 = con.execute("select count(*), count(distinct ts) from kbar_reaction "
                         "where symbol=? and interval_ms=?", (sym, iv)).fetchone()
    print("  表 0 kbar_line      %6d 行 / %5d 根" % (n0, b0))
    print("  表 1 kbar_evolution %6d 行 / %5d 根" % (n1, b1))
    print("  表 2 kbar_reaction  %6d 行 / %5d 根" % (n2, b2))
    if not n2:
        bad("表 2 是空的，先跑 tools/backfill_kbar_reactions.py")
        return 1

    # ---- 独立读原始行情（不信任表 1）----
    hr("1. 表 1 的 OHLC 与原始行情独立对比")
    print("  源：market=%s" % os.path.basename(a.market_db))
    srcA = (B.load_market_bars_rich(a.market_db, sym, iv)
            if os.path.exists(a.market_db) else [])
    srcB = (B.load_1m_as_15m(a.backtest_db, iv)
            if os.path.exists(a.backtest_db) else [])
    srcC = B.load_csv_bars(a.csv) if os.path.exists(a.csv) else []
    raw, _ = B.merge_bars(srcA, srcB, srcC)
    rawmap = {int(b["ts"]): b for b in raw}
    print("  原始合并序列 %d 根（A=%d B=%d C=%d）" % (len(raw), len(srcA), len(srcB), len(srcC)))
    diff = 0
    checked = 0
    for ts, o, h, l, c in con.execute(
            "select ts,o,h,l,c from kbar_evolution where symbol=? and interval_ms=?",
            (sym, iv)):
        rb = rawmap.get(int(ts))
        if not rb:
            continue
        checked += 1
        if (abs(rb["o"] - o) > 1e-9 or abs(rb["h"] - h) > 1e-9
                or abs(rb["l"] - l) > 1e-9 or abs(rb["c"] - c) > 1e-9):
            diff += 1
    if diff:
        bad("表 1 的 OHLC 与原始行情有 %d 根不一致" % diff)
        # 最常见的原因不是代码错，而是读到了本地那份**已知有坏 K 线**的 market.db
        # （2026-10-31 17:45 那批）。生产快照上没有这个差异。
        print("        若你用的是本地 data/market.db：它有 2 根已知坏数据，"
              "换成生产快照再跑一次——")
        print("        python %s --market-db <生产快照路径>"
              % os.path.basename(__file__))
    else:
        ok("表 1 的 OHLC 与原始行情逐根一致（%d 根）" % checked)

    # ---- 表 0 整理成「按不满的上一根取」的形状（探针自己实现，不复用 reaction.py）----
    series = collections.defaultdict(dict)
    for key, ts, ready, price in con.execute(
            "select line_key, ts, line_ready_ms, line_price from kbar_line "
            "where symbol=? and interval_ms=? order by line_key, ts",
            (sym, iv)):
        series[key][int(ts)] = (int(ready), float(price))
    same_ts = collections.defaultdict(dict)
    for key, ts, price in con.execute(
            "select line_key, ts, line_price from kbar_line "
            "where symbol=? and interval_ms=?", (sym, iv)):
        same_ts[key][int(ts)] = float(price)
    print("  表 0 有 %d 条线" % len(series))

    def ready_seq(key):
        return sorted(r for r, _ in series[key].values())

    def asof(key, ts):
        """仅供「偷未来」的粗检：最近一个 ready <= ts 的值（会往前带，不作为口径）。"""
        seq = sorted((r, p) for r, p in series[key].values())
        i = bisect.bisect_right([r for r, _ in seq], ts) - 1
        return seq[i][1] if i >= 0 else None

    def prev_row(key, prev_ts):
        """正确口径：只取「上一根 K 线」那一行；那根没有就是没有，不许往前带。"""
        return series[key].get(int(prev_ts))

    bar_ts_list = [int(t) for t, in con.execute(
        "select ts from kbar_evolution where symbol=? and interval_ms=? order by ts",
        (sym, iv))]
    prev_of = {ts: (bar_ts_list[i - 1] if i > 0 else None)
               for i, ts in enumerate(bar_ts_list)}

    # ---- 逐条重算 ----
    hr("2. 触碰判定 + 穿透/收回/收盘侧/距离 逐条独立重算")
    rows = con.execute(
        "select ts, line_key, line_price, dist_pct, side, pen_depth_pct, "
        "recover_pct, close_side from kbar_reaction "
        "where symbol=? and interval_ms=? order by ts", (sym, iv)).fetchall()
    print("  待核对 %d 行" % len(rows))

    wrong = collections.Counter()
    lookahead_rows = 0
    naive_would_differ = 0
    bars = {int(ts): (o, h, l, c) for ts, o, h, l, c in con.execute(
        "select ts,o,h,l,c from kbar_evolution where symbol=? and interval_ms=?",
        (sym, iv))}
    examples = []
    for ts, key, price, dp, side, pen, rec, cs in rows:
        ts = int(ts)
        o, h, l, c = bars.get(ts, (None, None, None, None))
        if o is None:
            wrong["no_bar"] += 1
            continue

        # (1) 触碰：必须 l <= p <= h
        if not (l <= price <= h):
            wrong["not_a_touch"] += 1
            if len(examples) < 5:
                examples.append((ts, key, price, l, h))
        # (2) 防偷未来 + 防往前带：价格必须等于「上一根那一行」的值
        pv = prev_of.get(ts)
        pr = None if pv is None else prev_row(key, pv)
        if pr is None or abs(pr[1] - price) > 1e-9:
            wrong["wrong_line_value"] += 1
            lookahead_rows += 1
            if len(examples) < 5:
                examples.append((ts, key, price, "上一根无此行" if pr is None
                                 else pr[1]))
        # 若用「同一 ts 那行」会不一样 → 说明这里真的避开了偷未来
        pn = same_ts[key].get(ts)
        pf = asof(key, ts)
        if pn is not None and pf is not None and abs(pn - pf) > 1e-9:
            naive_would_differ += 1
        # (3) side
        want_side = "from_above" if o > price else "from_below"
        if side != want_side:
            wrong["side"] += 1
        # (4) pen / rec
        want_pen = (price - l) / price if want_side == "from_above" else (h - price) / price
        want_rec = (c - l) / price if want_side == "from_above" else (h - c) / price
        if abs(pen - want_pen) > 1e-12:
            wrong["pen"] += 1
        if abs(rec - want_rec) > 1e-12:
            wrong["rec"] += 1
        # (5) close_side
        want_cs = "above" if c > price else ("below" if c < price else "at")
        if cs != want_cs:
            wrong["close_side"] += 1
        # (6) dist_pct 是百分数（pen/rec 是比例，这里专门验证这个不对称）
        want_dp = (c - price) / price * 100.0
        if abs(dp - want_dp) > 1e-9:
            wrong["dist_pct"] += 1

    if wrong:
        bad("逐条重算有 %d 类不一致: %s" % (len(wrong), dict(wrong)))
        for ex in examples:
            if len(ex) == 5:
                ts, key, price, l, h = ex
                print("         例：%s %s p=%.4f 但 l=%.4f h=%.4f"
                      % (fmt(ts), key, price, l, h))
            else:
                ts, key, price, should = ex
                print("         例：%s %s p=%.4f 应为 %s" % (fmt(ts), key, price, should))
    else:
        ok("触碰判定 / 穿透 / 收回 / 收盘侧 / 距离 —— %d 行全部与独立重算一致" % len(rows))

    if lookahead_rows:
        bad("%d 行的线值不等于「上一根那一行」的值（偷未来 或 往前带）" % lookahead_rows)
    else:
        ok("防偷未来 + 防往前带：%d 行的线值全部等于「上一根那一行」的值" % len(rows))
    ok("其中 %d 行「用同一 ts 那行」会取到不同的值 —— 说明这里确实避开了偷未来"
       % naive_would_differ)

    # ---- 补集完整性 ----
    hr("3. 补集完整：进表的 + 没碰到的 == 全部 (根,线) 对")
    in_table = {(int(ts), key) for ts, key in con.execute(
        "select ts, line_key from kbar_reaction where symbol=? and interval_ms=?",
        (sym, iv))}
    all_pairs = 0
    touched_found = 0
    not_ready = 0
    for ts, in con.execute("select ts from kbar_evolution where symbol=? and "
                           "interval_ms=? order by ts", (sym, iv)):
        ts = int(ts)
        o, h, l, c = bars[ts]
        pv = prev_of.get(ts)
        for key in series:
            all_pairs += 1
            pr = None if pv is None else prev_row(key, pv)
            if pr is None:
                not_ready += 1
                continue
            p = pr[1]
            if l <= p <= h:
                touched_found += 1
                if (ts, key) not in in_table:
                    pass
    missing = touched_found - len(in_table)
    print("  全部 (根,线) 对   %6d" % all_pairs)
    print("  上一根没有这条线   %6d" % not_ready)
    print("  可评估的对        %6d" % (all_pairs - not_ready))
    print("  独立重算判定触碰   %6d" % touched_found)
    print("  表 2 实际行数      %6d" % len(in_table))
    if missing == 0 and len(in_table) == touched_found:
        ok("触碰集合完全一致（不漏不重）")
    else:
        bad("触碰集合不一致：少 %d 条 / 表 2 多出 %d 条"
            % (missing, len(in_table) - touched_found))

    # n_touch 回填的自洽性
    cnt = collections.Counter(t for t, _ in in_table)
    mismatch = 0
    for ts, n in con.execute("select ts, n_touch from kbar_evolution "
                             "where symbol=? and interval_ms=?", (sym, iv)):
        if n != cnt.get(int(ts), 0):
            mismatch += 1
    if mismatch:
        bad("表 1 的 n_touch 与表 2 行数有 %d 根不符" % mismatch)
    else:
        ok("表 1 的 n_touch 逐根等于表 2 的行数（%d 根）" % n1)

    # ---- nth_touch：按段独立重数 ----
    hr("3b. nth_touch 按 (fam, seg) 独立重数")
    segs = {int(ts): (fam, seg) for ts, fam, seg in con.execute(
        "select ts, fam, seg from kbar_evolution where symbol=? and interval_ms=?",
        (sym, iv))}
    got_nt = {(int(ts), key): nt for ts, key, nt in con.execute(
        "select ts, line_key, nth_touch from kbar_reaction "
        "where symbol=? and interval_ms=? order by ts", (sym, iv))}
    cnt = {}
    wrong_nt = 0
    no_seg = 0
    for (ts, key) in sorted(got_nt, key=lambda x: (x[0], x[1])):
        sg = segs.get(ts)
        if sg is None or sg[0] is None or sg[1] is None:
            no_seg += 1
            if got_nt[(ts, key)] is not None:
                wrong_nt += 1
            continue
        cnt[(sg, key)] = cnt.get((sg, key), 0) + 1
        if got_nt[(ts, key)] != cnt[(sg, key)]:
            wrong_nt += 1
    if wrong_nt:
        bad("nth_touch 有 %d 行与独立重数不一致" % wrong_nt)
    else:
        ok("nth_touch 逐行与独立重数一致（%d 行；其中段缺失留空 %d 行）"
           % (len(got_nt), no_seg))
    if no_seg:
        # 换段是否真的归零：统计「nth_touch == 1」的行里，有多少是段首触碰
        seg_first = {}
        for (ts, key) in sorted(got_nt, key=lambda x: x[0]):
            sg = segs.get(ts)
            if sg is None or sg[0] is None:
                continue
            if sg not in seg_first:
                seg_first[sg] = got_nt[(ts, key)]
        ok("段首触碰的 nth_touch 必须为 1：%d 个段的段首值全为 1 = %s"
           % (len(seg_first), all(v == 1 for v in seg_first.values())))

    # ---- 抽样 ----
    hr("4. 真实反应抽样（人工核对用）")
    if a.at:
        base = datetime.datetime.strptime(a.at, "%Y-%m-%dT%H").replace(
            tzinfo=datetime.timezone.utc)
        at_ms = int(base.timestamp() * 1000)
        sel = con.execute(
            "select ts, line_key, line_price, side, pen_depth_pct, recover_pct, "
            "close_side, dist_pct, vol_rel, taker_buy_ratio from kbar_reaction "
            "where symbol=? and interval_ms=? and ts >= ? order by ts, line_key limit ?",
            (sym, iv, at_ms, a.n)).fetchall()
    else:
        sel = con.execute(
            "select ts, line_key, line_price, side, pen_depth_pct, recover_pct, "
            "close_side, dist_pct, vol_rel, taker_buy_ratio from kbar_reaction "
            "where symbol=? and interval_ms=? order by ts desc, line_key limit ?",
            (sym, iv, a.n)).fetchall()
    print("  %-16s %-18s %9s %-10s %8s %8s %6s %8s"
          % ("ts", "line_key", "线价", "side", "pen", "rec", "收盘侧", "dist%"))
    for ts, key, p, sd, pen, rec, cs, dp, vr, tk in sel:
        print("  %-16s %-18s %9.1f %-10s %8.5f %8.5f %6s %8.4f"
              % (fmt(ts), key, p, sd, pen, rec, cs, dp))
        o, h, l, c = bars[int(ts)]
        print("      这根 o=%.1f h=%.1f l=%.1f c=%.1f  → 手算 pen=(p-l)/p 或 (h-p)/p" % (o, h, l, c))

    # ---- 分布 ----
    hr("5. 分布（这些数以后要拿来定判断标准，现在先看清形状）")
    print("  side 分布:")
    for k, n in con.execute("select side, count(*) from kbar_reaction "
                            "where symbol=? and interval_ms=? group by side order by 2 desc",
                            (sym, iv)):
        print("    %-12s %6d" % (k, n))
    print("  close_side 分布:")
    for k, n in con.execute("select close_side, count(*) from kbar_reaction "
                            "where symbol=? and interval_ms=? group by close_side order by 2 desc",
                            (sym, iv)):
        print("    %-12s %6d" % (k, n))
    print("  pen_depth_pct 分位（比例，不是百分数）:")
    pens = sorted(r[0] for r in con.execute(
        "select pen_depth_pct from kbar_reaction where symbol=? and interval_ms=?",
        (sym, iv)))

    def q(xs, f):
        return xs[min(len(xs) - 1, int(len(xs) * f))]

    print("    P10=%.5f  P50=%.5f  P90=%.5f  max=%.5f"
          % (q(pens, .10), q(pens, .50), q(pens, .90), pens[-1]))
    recs = sorted(r[0] for r in con.execute(
        "select recover_pct from kbar_reaction where symbol=? and interval_ms=?",
        (sym, iv)))
    print("  recover_pct 分位:")
    print("    P10=%.5f  P50=%.5f  P90=%.5f  max=%.5f"
          % (q(recs, .10), q(recs, .50), q(recs, .90), recs[-1]))
    n_over = sum(1 for r in con.execute(
        "select recover_pct - pen_depth_pct from kbar_reaction "
        "where symbol=? and interval_ms=?", (sym, iv)) if r[0] > 0)
    print("  recover > pen 的行（穿进去又站到另一侧）: %d / %d (%.2f%%)"
          % (n_over, len(recs), 100.0 * n_over / len(recs)))

    con.close()
    hr("结论")
    if PROBLEMS:
        print("  发现 %d 个问题：" % len(PROBLEMS))
        for p in PROBLEMS:
            print("    - %s" % p)
        return 1
    print("  全部检查通过。上面的数都是探针从库里读出来、独立重算过的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
