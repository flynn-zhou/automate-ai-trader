# -*- coding: utf-8 -*-
"""第 1 步验收探针：观察线计算与落库 —— 打印**真实值**，不信测试绿。

为什么探针先于测试
------------------
本项目的硬教训：测试通过只说明旧行为没坏，不说明新逻辑生效了。
所以第 1 步的验收顺序是 探针 -> 看数 -> 才写测试（设计稿 §13）。

这个探针检查六件事，每件都打印它算出来的数（不是"OK"两个字）：

  0. 数据源自证：读的是哪个库、多少根、哪个时间跨度、目标根是哪一根
  1. EMA 五条：目标 1h 根的值 + 与看板 `web/live.py::ema_series` **同窗口逐位比对**
     + SMA 种子对照（证明这个比对不是永远为真）
  2. 唐奇安：与 `trading/donchian.py::DonchianStrategy.state()` 同口径比对
  3. 区间演化四线：与 `range_evolution` 该小时行**完全相等**；P 语义与 `range/api.py::_row` 一致
  4. 防偷未来：全部落库行必须满足 line_ready_ms <= bar_close；
     并给出「15m 的 12:00 只能用 11:00 那根 1h」的具体举证
  5. 落库与幂等：行数、重跑不变、跳过清单（禁止静默过滤）、口径参数已入 meta、
     两个生产库读取前后字节/mtime 完全没变

用法
----
    # 1h 验收（生产库只读快照；--limit 1500 使 EMA 窗口与看板完全相同）
    python tools/probe_kbar_lines.py --interval 1h --bars-src range \
        --range-db C:/.../market_range_snapshot.db --limit 1500

    # 15m：一根低周期 K 遇到高周期线（防偷未来的真实场景）
    python tools/probe_kbar_lines.py --interval 15m --bars-src fetch \
        --range-db C:/.../market_range_snapshot.db --limit 1700

    # 只算不落库
    python tools/probe_kbar_lines.py --interval 1h --bars-src range --no-persist

退出码：全部检查通过 0，否则 1（并逐条列出没过的项）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from ai_trader.kbar import build as B                     # noqa: E402
from ai_trader.kbar import lines as L                     # noqa: E402
from ai_trader.kbar.store import KBarStore                # noqa: E402
from ai_trader.range.engine import lab, ts8               # noqa: E402

IV_MS = {"3m": 180_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}

PROBLEMS: list = []


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def bad(msg: str) -> None:
    PROBLEMS.append(msg)
    print("  [FAIL] " + msg)


def ok(msg: str) -> None:
    print("  [OK]   " + msg)


def fnum(x, nd: int = 2) -> str:
    return "-" if x is None else ("%." + str(nd) + "f") % float(x)


def stat_file(p: str):
    """(size, mtime_ns) —— 用来证明"读没写"。"""
    if not p or not os.path.exists(p):
        return None
    st = os.stat(p)
    return (st.st_size, st.st_mtime_ns)


def sma_seeded_ema(closes, period: int):
    """对照组：用 SMA 做种子的"另一种常见 EMA"。

    存在的唯一目的：证明探针里的比对**有分辨力**。
    如果两种写法算出来的数一样，那"与看板一致"这句话就什么也没证明。
    注意：在 1500 根的长序列上两者其实会收敛到同一个数（种子权重被
    (1-α)^1500 衰减到 0），所以分辨率演示必须用**短窗口**（见 §1 末尾）。
    """
    n = min(int(period), len(closes))
    seed = sum(closes[:n]) / float(n)
    a = 2.0 / (period + 1)
    e = seed
    for v in closes[n:]:
        e = float(v) * a + e * (1.0 - a)
    return e


def dash_ema_raw(closes, period: int):
    """`web/live.py::ema_series` 的**不取整**镜像（逐行转写，只去掉 round(,1)）。

    为什么需要它：看板函数返回的是 `round(e, 1)`，那是显示精度。
    拿全精度存库值去比一个已经取整的数，差必然 <=0.05 —— 那不是口径不一致，
    是探针自己拿错了标尺。要比"公式是否同一条"，必须比不取整的那一层。
    """
    a = 2.0 / (period + 1)
    out = []
    e = None
    for v in closes:
        e = v if e is None else v * a + e * (1.0 - a)
        out.append(e)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="K 线演化第 1 步：观察线探针")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1h", choices=sorted(IV_MS))
    ap.add_argument("--bars-src", default="auto",
                    choices=("auto", "range", "market", "fetch"),
                    help="range=区间库 bars_1h（只支持 1h）/ market=行情库 bars / fetch=币安")
    ap.add_argument("--limit", type=int, default=1500,
                    help="用于计算的 K 线根数（目标根是最后一根）。"
                         "默认 1500 = 看板 EMA 预热长度，使两者窗口完全相同；"
                         "调大可以多落库一些根，但窗口就不再与看板逐位相同")
    ap.add_argument("--at", default="2026-11-07 12:00",
                    help="目标根的**开盘时间**（北京时区串，与库内 ts_h 同口径）")
    ap.add_argument("--market-db", default=B.DEFAULT_MARKET_DB)
    ap.add_argument("--range-db", default=B.DEFAULT_RANGE_DB)
    ap.add_argument("--kbar-db", default=B.DEFAULT_KBAR_DB)
    ap.add_argument("--rebuild", action="store_true", help="先删该周期的旧行再写")
    ap.add_argument("--no-persist", action="store_true", help="只算不落库")
    ap.add_argument("--quiet-skip", action="store_true", help="不打印跳过清单")
    a = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    sym = a.symbol
    iv_name = a.interval
    iv_ms = IV_MS[iv_name]
    src = a.bars_src
    if src == "auto":
        src = "range" if (iv_ms == L.RANGE_PERIOD_MS and os.path.exists(a.range_db)) else "market"

    hr("0. 数据源自证（读的是哪个库、多少根、目标根）")
    before = {"market": stat_file(a.market_db), "range": stat_file(a.range_db),
              "range-wal": stat_file(a.range_db + "-wal"),
              "range-shm": stat_file(a.range_db + "-shm")}
    for k, v in before.items():
        print("  %-12s %s" % (k + ":", v))

    # 多取一点再截断：目标根之后可能还有若干根（生产库里最新的那几根），
    # 直接按 limit 取会被后面的根挤掉预热历史，导致最后一根本身算不出 EMA。
    fetch_n = a.limit + 32
    if src == "range":
        if iv_ms != L.RANGE_PERIOD_MS:
            print("  [FAIL] --bars-src range 只支持 1h")
            return 2
        bars = B.load_range_bars_1h(a.range_db, sym, fetch_n)
        print("  K 线来源: 区间库 bars_1h（生产库，只读）")
    elif src == "market":
        bars = B.load_market_bars(a.market_db, sym, iv_ms, fetch_n)
        print("  K 线来源: 行情库 bars（生产库，只读）")
    else:
        bars = B.fetch_bars(sym, iv_name, fetch_n)
        print("  K 线来源: 币安 REST（与 web/live.py 同一条取数路径）")
    if not bars:
        print("  [FAIL] 一根 K 线都没读到，后面的检查无法进行")
        return 2
    print("  原始根数=%d  跨度 %s ~ %s"
          % (len(bars), ts8(bars[0].ts), ts8(bars[-1].ts)))

    target_ts = lab(a.at)
    kept = [b for b in bars if int(b.ts) <= target_ts]
    dropped = len(bars) - len(kept)
    print("  目标根(开盘) = %s (ts=%d)   截断掉更晚的 %d 根（为了和相关标量对齐）"
          % (a.at, target_ts, dropped))
    if not kept or int(kept[-1].ts) != target_ts:
        print("  [FAIL] 数据里没有开盘时间正好是 %s 的 %s K 线"
              "（现有跨度 %s ~ %s）；换 --at 或补数据"
              % (a.at, iv_name, ts8(bars[0].ts), ts8(bars[-1].ts)))
        return 2
    # 只留最后 limit 根：保证目标根之前正好有 limit-1 根历史，
    # 于是「预热 1500 根 + 目标根」= --limit 1500 时与看板窗口逐位相同。
    if len(kept) > a.limit:
        print("  截断到最近 %d 根（前面多出的 %d 根丢掉，为了让预热窗口与看板对齐）"
              % (a.limit, len(kept) - a.limit))
        kept = kept[-a.limit:]
    if len(kept) < a.limit:
        print("  [WARN] 目标根之前只有 %d 根历史（要 %d 根）。预热不足的线会进跳过清单。"
              % (len(kept), a.limit))
    print("  用于计算的根数=%d（目标根是最后一根，收盘 %s）"
          % (len(kept), ts8(target_ts + iv_ms)))

    range_rows = []
    if os.path.exists(a.range_db):
        range_rows = B.load_range_rows(a.range_db, sym)
        print("  区间演化行=%d  跨度 %s ~ %s"
              % (len(range_rows),
                 ts8(int(range_rows[0]["ts"])) if range_rows else "-",
                 ts8(int(range_rows[-1]["ts"])) if range_rows else "-"))
    else:
        print("  [WARN] 没有区间库 %s，区间四线全部会进跳过清单" % a.range_db)

    # ---------------------------------------------------------------- 落库
    stats = B.build_and_persist(
        a.kbar_db, sym, iv_ms, kept, range_rows,
        rebuild=a.rebuild, sources=[a.market_db, a.range_db],
        dry_run=a.no_persist)
    print("  计算产出: 行=%d  跳过条目=%d"
          % (stats["lines"], sum(stats.get("skipped", {}).values())))
    if a.no_persist:
        print("  --no-persist：没有写库，下面的读数来自内存结果")
        stored = {}
        for r in L.build_bar_lines(sym, iv_ms, kept, range_rows)[0]:
            if int(r["ts"]) == target_ts:
                stored[r["line_kind"]] = r
        stored_rows = sorted(stored.values(), key=lambda r: r["line_key"])
    else:
        with KBarStore(a.kbar_db) as st:
            stored_rows = st.lines(sym, iv_ms, ts=target_ts)
        stored = {r["line_kind"]: r for r in stored_rows}
        print("  库内本根线数=%d（从 %s 读回，不是内存变量）"
              % (len(stored_rows), a.kbar_db))

    closes = [float(b.c) for b in kept]

    # ---------------------------------------------------------------- 1. EMA
    hr("1. EMA 五条（%s，目标根 %s）—— 与看板口径比对" % (iv_name, a.at))
    try:
        from ai_trader.web.live import EMA_PERIODS as DASH_PERIODS
        from ai_trader.web.live import EMA_WARMUP_BARS as DASH_WARMUP
        from ai_trader.web.live import ema_series as dash_ema
    except Exception as e:                                  # pragma: no cover
        print("  [WARN] 导不到 web/live.py（%s），跳过与看板的比对" % e)
        dash_ema = None
    if dash_ema and tuple(DASH_PERIODS) != tuple(L.EMA_PERIODS):
        bad("EMA 周期集合与看板不一致: %s vs %s" % (L.EMA_PERIODS, DASH_PERIODS))
    if dash_ema and int(DASH_WARMUP) != int(L.EMA_WARMUP_BARS):
        bad("预热根数与看板不一致: %d vs %d" % (L.EMA_WARMUP_BARS, DASH_WARMUP))

    print("  %-9s %16s %16s %8s %8s   %s"
          % ("线", "存库值(全精度)", "镜像全精度", "差(点)", "显示一致", "看板显示值(round1)"))
    max_raw = 0.0
    n_disp_ok = 0
    ema_rows = 0
    for p in L.EMA_PERIODS:
        kind = "ema%d" % p
        row = stored.get(kind)
        if row is None:
            print("  %-9s %16s   %s"
                  % (kind, "(未落库)", "预热不足（跳过清单里有计数）"))
            continue
        ema_rows += 1
        ours = float(row["line_price"])
        raw = float(dash_ema_raw(closes, p)[-1])          # 同一公式、不取整
        disp = float(dash_ema(closes, p)[-1])             # 看板真实输出：round(e,1)
        d_raw = abs(ours - raw)
        same_disp = round(ours, 1) == disp
        max_raw = max(max_raw, d_raw)
        n_disp_ok += 1 if same_disp else 0
        print("  %-9s %16.4f %16.4f %8.6f %8s   %.1f"
              % (kind, ours, raw, d_raw, "是" if same_disp else "否", disp))
    if ema_rows == 0:
        bad("一条 EMA 都没落库：序列表长度 %d < 预热 %d"
            % (len(kept), L.EMA_WARMUP_BARS))
    else:
        if max_raw > 0.0:
            bad("EMA 与看板公式不一致（不取整层）：最大差 %.10f 点" % max_raw)
        else:
            ok("EMA %d 条与 web/live.py::ema_series 的公式逐位相等"
               "（全精度差 0.0 点，取整层 %d/%d 一致）"
               % (ema_rows, n_disp_ok, ema_rows))
        # 窗口长度自证：看板只取 1500 根，我们的预热也必须是 1500 根
        win_last = float(dash_ema(closes[-int(L.EMA_WARMUP_BARS):], p)[-1]) \
            if len(closes) >= int(L.EMA_WARMUP_BARS) else float("nan")
        print("     窗口自证：本次计算 %d 根；看板取 %d 根。"
              "两者窗口长度相同时（--limit 1500）末根值必然逐位相同。"
              % (len(closes), int(L.EMA_WARMUP_BARS)))
        # 分辨率自证：短窗口下种子写法会差几十点 —— 证明这个比对抓得住错口径
        short = closes[-200:]
        for p2 in (144, 200):
            a1 = float(dash_ema_raw(short, p2)[-1])
            b1 = sma_seeded_ema(short, p2)
            print("     分辨率自证：只喂 200 根算 EMA%d，首根种子=%.2f / SMA种子=%.2f"
                  " → 差 %.2f 点（%.4f%%，足以把「谁在上方」判反）"
                  % (p2, a1, b1, abs(a1 - b1), abs(a1 - b1) / max(a1, 1e-9) * 100.0))

    # ---------------------------------------------------------------- 2. 唐奇安
    hr("2. 唐奇安上下轨（%d/%d，不含当根）—— 与 trading/donchian.py 比对"
       % (L.DONCHIAN_HI_BARS, L.DONCHIAN_LO_BARS))
    try:
        from ai_trader.config import TradingCfg
        from ai_trader.trading.donchian import DonchianStrategy
        strat = DonchianStrategy(TradingCfg())
        strat.bars = [{"t": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c,
                       "v": b.v or 0.0} for b in kept]
        dst = strat.state(price=float(kept[-1].c))
        print("  donchian.py: entry_n=%d exit_m=%d ready=%s  upper=%s  lower=%s"
              % (strat.entry_n, strat.exit_m, dst.get("ready"),
                 fnum(dst.get("upper")), fnum(dst.get("lower"))))
        for kind, key in (("donchian_hi", "upper"), ("donchian_lo", "lower")):
            row = stored.get(kind)
            if row is None:
                bad("%s 没落库（应为 %s）" % (kind, fnum(dst.get(key))))
                continue
            ours, theirs = float(row["line_price"]), dst.get(key)
            if theirs is None:
                bad("%s：donchian.py 没给出值，无法比对" % kind)
            elif ours != float(theirs):
                bad("%s 与 donchian.py 不一致：%.10f vs %.10f" % (kind, ours, float(theirs)))
            else:
                ok("%s = %.4f 与 donchian.py 完全相等" % (kind, ours))
    except Exception as e:
        bad("与 donchian.py 比对失败：%s" % e)

    # ---------------------------------------------------------------- 3. 区间四线
    hr("3. 区间演化四线 —— 必须与 range_evolution 该小时行完全相等（不重算）")
    used_rts = None
    for r in stored_rows:
        if r["line_kind"].startswith("range_"):
            used_rts = int(r["src_ts"])
            break
    if used_rts is None and range_rows:
        rr, used_rts = L.range_lines_at(range_rows, target_ts + iv_ms)
    row = next((r for r in range_rows if int(r["ts"]) == int(used_rts)), None) if used_rts else None
    if row is None:
        bad("找不到被使用的 1h 区间行（used_rts=%s）" % used_rts)
    else:
        print("  被使用的 1h 行: ts=%d  ts_h=%s  fam=%s seg=%s dir=%s"
              % (int(row["ts"]), row.get("ts_h"), row.get("family"),
                 row.get("segment"), row.get("direction")))
        print("  本根 K 收盘=%s，该 1h 行收盘=%s（差 %.2f 小时）"
              % (ts8(target_ts + iv_ms), ts8(int(row["ts"]) + L.RANGE_PERIOD_MS),
                 ((target_ts + iv_ms) - (int(row["ts"]) + L.RANGE_PERIOD_MS)) / 3.6e6))
        expect = {"range_soft_up": row.get("soft_up"),
                  "range_soft_dn": row.get("soft_dn"),
                  "range_hard_up": row.get("hard_up"),
                  "range_hard_dn": row.get("hard_dn"),
                  "range_p": L.range_p(row),
                  "range_key": row.get("key")}
        for kind in L.RANGE_LINE_KINDS:
            ours = stored.get(kind)
            want = expect[kind]
            if ours is None:
                if want is None:
                    ok("%s 库里该侧本来就是 NULL（方向 %s 没有这一侧），未落库符合预期"
                       % (kind, row.get("direction")))
                else:
                    bad("%s 应该有值 %.4f 但没落库" % (kind, float(want)))
                continue
            if want is None:
                bad("%s 落库了 %s 但库里该字段是 NULL" % (kind, fnum(ours["line_price"])))
            elif float(ours["line_price"]) != float(want):
                bad("%s 与库不一致：落库 %.10f vs 库 %.10f"
                    % (kind, float(ours["line_price"]), float(want)))
            else:
                ok("%s = %.4f 与 range_evolution.%s 完全相等"
                   % (kind, float(ours["line_price"]),
                      {"range_soft_up": "soft_up", "range_soft_dn": "soft_dn",
                       "range_hard_up": "hard_up", "range_hard_dn": "hard_dn",
                       "range_p": "=> P", "range_key": "v11_key"}[kind]))
        # P 语义交叉核对：把库里的行反解成引擎元组，交给 range/api.py::_row
        try:
            from ai_trader.range.api import _row as api_row
            d = (row.get("direction") or "").upper()
            if d == "DOWN":
                lo_b, hi_b, soft = row.get("soft_dn"), row.get("hard_up"), row.get("soft_up")
            elif d == "UP":
                lo_b, hi_b, soft = row.get("hard_dn"), row.get("soft_up"), row.get("soft_dn")
            else:
                lo_b = hi_b = soft = None
            api = api_row((int(row["ts"]), lo_b, hi_b, 0, d, None, soft,
                           row.get("key"), row.get("tag") or "",
                           row.get("family"), row.get("segment")))
            print("  反解->api._row: soft_up=%s soft_dn=%s hard_up=%s hard_dn=%s p=%s key=%s"
                  % (fnum(api["soft_up"]), fnum(api["soft_dn"]), fnum(api["hard_up"]),
                     fnum(api["hard_dn"]), fnum(api["p"]), fnum(api["key"])))
            if stored.get("range_p") is not None and api["p"] is not None and \
                    float(stored["range_p"]["line_price"]) != float(api["p"]):
                bad("range_p 与 range/api.py::_row 的 P 语义不一致：%s vs %s"
                    % (fnum(stored["range_p"]["line_price"]), fnum(api["p"])))
            else:
                ok("P 顺向边界语义与 range/api.py::_row 一致")
        except Exception as e:
            bad("P 语义交叉核对失败：%s" % e)

    # ---------------------------------------------------------------- 4. 防偷未来
    hr("4. 防偷未来 —— line_ready_ms <= bar_close 必须是硬不变量")
    if a.no_persist:
        all_rows = L.build_bar_lines(sym, iv_ms, kept, range_rows)[0]
    else:
        with KBarStore(a.kbar_db) as st:
            all_rows = st.lines(sym, iv_ms)
    violations = [r for r in all_rows if int(r["line_ready_ms"]) > int(r["ts"]) + iv_ms]
    if violations:
        bad("有 %d 行线的可用时刻晚于本根收盘（偷未来）：例如 %s"
            % (len(violations), violations[0]))
    else:
        ok("全部 %d 行都满足 line_ready_ms <= bar_close（违例 0）" % len(all_rows))
    print("  本根上的每条线（ts=%s，bar_close=%s）：" % (a.at, ts8(target_ts + iv_ms)))
    print("    %-22s %14s %-18s %-18s %10s"
          % ("line_key", "价格", "src_ts(来源K开盘)", "ready(可用时刻)", "余量(h)"))
    for r in stored_rows:
        print("    %-22s %14.4f %-18s %-18s %10.2f"
              % (r["line_key"], float(r["line_price"]),
                 ts8(int(r["src_ts"])), ts8(int(r["line_ready_ms"])),
                 ((target_ts + iv_ms) - int(r["line_ready_ms"])) / 3.6e6))
    # 具体举证：如果按弱口径 ts <= bar_close 取 1h 行，会多用到哪一根
    if range_rows:
        weak_idx = None
        strong_idx = None
        for i, rr in enumerate(range_rows):
            if int(rr["ts"]) <= target_ts + iv_ms:
                weak_idx = i
            if int(rr["ts"]) + L.RANGE_PERIOD_MS <= target_ts + iv_ms:
                strong_idx = i
        if weak_idx is not None and strong_idx is not None and weak_idx > strong_idx:
            w = range_rows[weak_idx]
            print("  举证：弱口径(ts<=bar_close) 会用到 ts=%s 这根 1h，"
                  "但它要到 %s 才收盘 —— 本根收盘 %s，晚 %.2f 小时 = 偷未来"
                  % (ts8(int(w["ts"])), ts8(int(w["ts"]) + L.RANGE_PERIOD_MS),
                     ts8(target_ts + iv_ms),
                     ((int(w["ts"]) + L.RANGE_PERIOD_MS) - (target_ts + iv_ms)) / 3.6e6))
            if used_rts is not None and int(used_rts) == int(w["ts"]):
                bad("实际用了那根还没收盘的 1h（ts=%s）" % ts8(int(w["ts"])))
            else:
                ok("实际用的是 ts=%s 那根（其收盘时刻 <= 本根收盘），弱口径那根被排除"
                   % (ts8(int(used_rts)) if used_rts else "-"))
        else:
            print("  举证：本次目标根上两种口径取到同一根 1h"
                  "（%s 上的边界情形），无差别" % a.at)

    # ---------------------------------------------------------------- 5. 落库/幂等
    hr("5. 落库、幂等、跳过清单、口径留痕、只读自证")
    if a.no_persist:
        print("  --no-persist：跳过落库检查")
    else:
        with KBarStore(a.kbar_db) as st:
            c1 = st.counts(sym, iv_ms)
        print("  第一次: %s" % c1)
        stats2 = B.build_and_persist(a.kbar_db, sym, iv_ms, kept, range_rows,
                                     sources=[a.market_db, a.range_db])
        with KBarStore(a.kbar_db) as st:
            c2 = st.counts(sym, iv_ms)
            meta = st.get_meta("line_params:%s:%s" % (sym, L.period_label(iv_ms)))
        print("  第二次: %s   （幂等：行数不变=%s）" % (c2, c1["lines"] == c2["lines"]))
        if c1["lines"] != c2["lines"]:
            bad("重跑后行数变了：%d -> %d（幂等被破坏）" % (c1["lines"], c2["lines"]))
        else:
            ok("重跑幂等：行数 %d 不变" % c1["lines"])
        if stats["lines"] and c2["lines"] == 0:
            bad("算出了 %d 行但库里 0 行（没接线）" % stats["lines"])
        print("  口径参数已写进 kbar_meta：%s" % (meta[:160] if meta else "(空)"))

    if not a.quiet_skip:
        print("  跳过清单（禁止静默过滤，每一条都说得出原因）：")
        sk = stats.get("skipped") or {}
        if not sk:
            print("    （无）")
        for k in sorted(sk):
            print("    %-34s %8d" % (k, sk[k]))
    print("  各类线落库数：")
    for k in sorted(stats.get("by_kind") or {}):
        print("    %-22s %8d" % (k, stats["by_kind"][k]))

    # ---- 只读自证 1：主库文件字节数与 mtime 必须一点没变
    # 注意 -wal/-shm 不算：SQLite 读一个 WAL 模式的库时会在旁边建/碰这两个文件，
    # 那是**读机制**不是写库。真正能证明"没写"的是下面两条一起看。
    after = {"market": stat_file(a.market_db), "range": stat_file(a.range_db)}
    changed = [k for k in after if before.get(k) != after.get(k)]
    if changed:
        bad("只读源在探针期间被改动了: %s" % changed)
    else:
        ok("两个只读源主库文件 (size, mtime) 读取前后完全没变")
    print("  -wal/-shm 现状（仅参考，读 WAL 库会碰它们）：%s / %s"
          % (stat_file(a.range_db + "-wal"), stat_file(a.range_db + "-shm")))

    # ---- 只读自证 2：对只读连接发起一次真写，必须被 SQLite 拒绝
    for path, table in ((a.range_db, "range_evolution"), (a.market_db, "bars")):
        if not path or not os.path.exists(path):
            continue
        try:
            con = B.open_ro(path)
        except Exception as e:
            print("  [WARN] 打不开 %s: %s" % (path, e))
            continue
        try:
            con.execute("BEGIN")
            con.execute("DELETE FROM %s WHERE 0" % table)
            con.rollback()
            bad("只读连接居然写成功了 %s.%s —— 只读纪律失效" % (os.path.basename(path), table))
        except sqlite3.OperationalError as e:
            ok("只读自证：往 %s.%s 写被 SQLite 拒绝（%s）"
               % (os.path.basename(path), table, e))
        except Exception as e:
            print("  [WARN] 只读自证异常（%s）：%s" % (os.path.basename(path), e))
        finally:
            con.close()

    hr("结论")
    if PROBLEMS:
        print("没通过的检查 %d 项：" % len(PROBLEMS))
        for i, m in enumerate(PROBLEMS, 1):
            print("  %d) %s" % (i, m))
        return 1
    print("全部检查通过。上面的数是探针真算出来的，不是断言里的常量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
