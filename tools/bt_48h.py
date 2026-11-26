# -*- coding: utf-8 -*-
"""最近 48 小时回测：A 现行系统逐笔 / B 用户的反包三分仓策略。

用法:
    python bt_48h.py A          # 现行系统
    python bt_48h.py B          # 反包三分仓
    python bt_48h.py ALL

★ 只读生产库，不下单、不写库。
★ 所有 R 都用「实际风险 = 入场价 - 止损价」折算，便于横向比较。
"""
from __future__ import annotations

import os
import sys
import time

BASE = "/home/cryptobot/ai-trader"
sys.path.insert(0, BASE)

HOURS = float(os.environ.get("BT_HOURS", "48"))
M15, M5, M1H = 900_000, 300_000, 3_600_000
TICK = 0.1          # BTCUSDT 永续最小变动价位
SYMBOL = "BTCUSDT"


def ro(p):
    import sqlite3
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True)


def fmt(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(int(ts) / 1000))


def load_bars(iv):
    con = ro(os.path.join(BASE, "data/kbar.db"))
    try:
        rows = con.execute(
            "select ts,o,h,l,c,vol from kbar_evolution"
            " where symbol=? and interval_ms=? order by ts",
            (SYMBOL, iv)).fetchall()
    finally:
        con.close()
    return [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4]), "v": float(r[5] or 0.0)}
            for r in rows]


# ====================================================================== A
def mode_a(t_end):
    """现行系统：生产同一套计划源，逐笔列出。"""
    from ai_trader.trading.plan_source import PlanSource, resolve_range_db
    from ai_trader.trading import pending_plan as P
    from ai_trader.trading import thesis as T
    from ai_trader.trading.sizing import SizingConfig, derive

    b15, b5 = load_bars(M15), load_bars(M5)
    con = ro(os.path.join(BASE, "data/market_range.db"))
    try:
        b1h = [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]), "v": float(r[5] or 0.0)}
               for r in con.execute(
                   "select ts,open,high,low,close,volume from bars_1h"
                   " where symbol=? order by ts", (SYMBOL,))]
    finally:
        con.close()

    book = T.LineBook()
    con = ro(os.path.join(BASE, "data/kbar.db"))
    try:
        for r in con.execute(
                "select line_key, ts, line_price, line_ready_ms from kbar_line"
                " where symbol=? order by ts", (SYMBOL,)):
            book.add({"line_key": r[0], "ts": int(r[1]),
                      "line_price": float(r[2]), "line_ready_ms": int(r[3])})
    finally:
        con.close()

    d = derive(SizingConfig(capital=114.03, risk_pct=1.0, leverage=22.0))
    # ★ 字段名是 `max_stop_distance_pct`（不是 max_stop_pct）——
    #   第一版读错名取到 0，止损会静默落回振幅规则，报出来的止损价是假的。
    pct = float(getattr(d, "max_stop_distance_pct", 0.0) or 0.0)
    params = P.PendingParams(max_stop_pct=pct or 1.618)

    src = PlanSource(book_provider=lambda: book,
                     kbar_db=os.path.join(BASE, "data/kbar.db"),
                     range_db=resolve_range_db(os.path.join(BASE, "data")),
                     symbol=SYMBOL)
    ctx = src.context()
    plans, _ = src.plans(params, ctx)

    all15 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b15]
    all5 = [P._Bar(x["ts"], x["o"], x["h"], x["l"], x["c"]) for x in b5]
    idx15 = {b.ts: i for i, b in enumerate(all15)}
    b15map = {x["ts"]: x for x in b15}
    b5map = {x["ts"]: x for x in b5}

    t_lo = t_end - int(HOURS * 3600 * 1000)
    sel = [p for p in plans if t_lo <= int(p.effective_claim_ts) <= t_end]

    print("=" * 96)
    print("A · 现行系统（VOL-1 口径：窗口=突破/反转 + 附近放量>=3x均量 + 只做倒V/EMA拒绝）")
    print("    区间 %s → %s（%.0f 小时）" % (fmt(t_lo), fmt(t_end), HOURS))
    print("    窗口 %d 个 / 全历史计划 %d 条 / 本区间 %d 条"
          % (len(ctx.windows or ()), len(plans), len(sel)))
    print("    止损口径 = max_stop_distance_pct(%.4f%%) - 成本；止盈 = 4R" % pct)
    print("=" * 96)

    if not sel:
        print("  ★ 本区间没有任何计划 —— 系统在这 48 小时里不会开仓。")
        # 补充：区间内有没有窗口
        ws = [w for w in (ctx.windows or ())
              if int(w.end_ms) >= t_lo and int(w.start_ms) <= t_end]
        print("  区间内窗口 %d 个：%s"
              % (len(ws), "无" if not ws else ""))
        for w in ws:
            print("     %-4s %s → %s" % (w.direction, fmt(w.start_ms),
                                        fmt(w.end_ms)))
        return

    for n, pl in enumerate(sel, 1):
        cts = int(pl.effective_claim_ts)
        # ★ 关键 K 必须按**该形态自己的周期**取：倒V/三引线是 5m、EMA拒绝是 15m。
        #   一律查 15m 会取不到（claim_ts 不是 15m 边界 → 全 0），
        #   第一版就是这样打印出一整排 0.0 的。
        sp = str(getattr(pl, "bar_space", "") or "")
        # ★ 两条序列都查：`bar_space` 在 Plan 上不一定有值（倒V/三引线在 5m
        #   上声明，claim_ts 是 5m 边界），按 15m 查会取不到 → 打印一整排 0。
        if cts % M15 == 0 and b15map.get(cts):
            bar, sp = b15map[cts], "15m"
        elif b5map.get(cts):
            bar, sp = b5map[cts], "5m"
        else:
            bar, sp = {}, sp or "?"
        bar = bar or {}
        th = P.to_thesis(pl, params=params)
        res = T.run(th, all15, book, start_at=idx15.get(cts, 0),
                    entry_bars=all5,
                    claim_on_entry=bool(getattr(pl, "claim_on_entry", False)))
        entry, stop, tgt = (float(pl.order_price), float(pl.stop_price),
                            float(pl.target_price))
        risk = abs(entry - stop)
        sign = 1.0 if str(pl.direction) == T.UP else -1.0
        filled = any(a.kind == "fill" for a in res.actions)
        close = why = None
        for a in res.actions:
            if a.kind == "close" and a.price is not None:
                close, why = float(a.price), (a.reason or "")
        r = ((close - entry) * sign / risk) if (close is not None
                                                and risk > 0) else None

        print("\n【%d】%s（%s K）  %s %s" % (n, fmt(cts), sp, pl.kind,
                                            pl.direction))
        print("     关键K：O %.1f  H %.1f  L %.1f  C %.1f  vol %.1f"
              % (bar.get("o", 0), bar.get("h", 0), bar.get("l", 0),
                 bar.get("c", 0), bar.get("v", 0)))
        print("     决策：入场 %.2f（%s）  止损 %.2f（距离 %.2f = %.3f%%）"
              "  止盈 %.2f（%.1fR）"
              % (entry, getattr(pl, "order_type", "?"), stop, risk,
                 risk / entry * 100.0 if entry else 0,
                 tgt, abs(tgt - entry) / risk if risk else 0))
        print("     成交：%s   终态 %s   出场 %s @ %s"
              % ("是" if filled else "否", res.state, why,
                 ("%.2f" % close) if close is not None else "-"))
        if r is not None:
            print("     结果：%+.2fR%s" % (r, "" if r > 0 else "  ← 亏损"))
        else:
            print("     结果：未成交/未终结（无 R）  原因 %s"
                  % (res.unresolved_reason or "-"))


# ====================================================================== B
def mode_b(t_end):
    """用户的反包三分仓策略（15m 反包）。"""
    b15 = load_bars(M15)
    t_lo = t_end - int(HOURS * 3600 * 1000)
    bars = [b for b in b15 if t_lo <= b["ts"] <= t_end]
    print("=" * 96)
    print("B · 反包三分仓（15m 反包K）")
    print("    区间 %s → %s（%d 根 15m）" % (fmt(t_lo), fmt(t_end), len(bars)))
    print("    规则：①最高点买突破 ②实体80%买回调 ③实体50%买回调")
    print("          止损 = 反包K最低点 - 1 tick；1R = 收盘 - 最低")
    print("          到 1R 平一半 + 止损上移到实体50%；2R 全平")
    print("=" * 96)

    # 先列出所有反包K，供人工核对
    sigs = []
    for i in range(1, len(b15)):
        b, p = b15[i], b15[i - 1]
        if b["ts"] < t_lo or b["ts"] > t_end:
            continue
        up = (p["c"] < p["o"] and b["c"] > b["o"]
              and b["c"] > p["o"] and b["o"] < p["c"])
        dn = (p["c"] > p["o"] and b["c"] < b["o"]
              and b["c"] < p["o"] and b["o"] > p["c"])
        if up or dn:
            sigs.append((i, "up" if up else "down"))

    print("\n【反包K清单】共 %d 根" % len(sigs))
    for i, d in sigs:
        b = b15[i]
        print("   %s  %-4s  O %9.1f H %9.1f L %9.1f C %9.1f  实体 %7.1f"
              % (fmt(b["ts"]), d, b["o"], b["h"], b["l"], b["c"],
                 abs(b["c"] - b["o"])))

    print("\n【逐笔模拟】")
    total_r = 0.0
    win = 0
    n_done = 0
    for i, d in sigs:
        b = b15[i]
        up = d == "up"
        O, H, L, C = b["o"], b["h"], b["l"], b["c"]
        body = C - O
        # 三份仓位
        lv1 = H                                   # 突破
        lv2 = O + 0.8 * body                      # 实体 80%
        lv3 = O + 0.5 * body                      # 实体 50%
        stop0 = (L - TICK) if up else (H + TICK)
        r_unit = abs(C - L) if up else abs(H - C)
        mid = O + 0.5 * body

        tranches = [{"name": "①突破", "px": lv1, "kind": "stop"},
                    {"name": "②80%", "px": lv2, "kind": "limit"},
                    {"name": "③50%", "px": lv3, "kind": "limit"}]
        # 逐根推进
        for tr in tranches:
            tr.update(fill_ts=None, exit_px=None, exit_ts=None, why="",
                      stop=stop0, half_done=False, r=0.0)
        for j in range(i + 1, min(i + 1 + 48, len(b15))):
            nb = b15[j]
            for tr in tranches:
                if tr["fill_ts"] is None:
                    hit = (nb["h"] >= tr["px"]) if up else (nb["l"] <= tr["px"])
                    if hit:
                        tr["fill_ts"] = nb["ts"]
                    else:
                        continue
                if tr["exit_px"] is not None:
                    continue
                # 到 1R 先平一半 + 移止损
                tp1 = tr["px"] + (r_unit if up else -r_unit)
                tp2 = tr["px"] + (2 * r_unit if up else -2 * r_unit)
                # ★ 歧义处理：同一根里止损与目标都碰到 → 先算止损（保守）
                hit_stop = ((nb["l"] <= tr["stop"]) if up
                            else (nb["h"] >= tr["stop"]))
                if hit_stop:
                    tr["exit_px"] = tr["stop"]
                    tr["exit_ts"] = nb["ts"]
                    tr["why"] = "止损"
                    continue
                if not tr["half_done"]:
                    hit1 = ((nb["h"] >= tp1) if up else (nb["l"] <= tp1))
                    if hit1:
                        tr["half_done"] = True
                        tr["stop"] = mid      # 止损上移到实体 50%
                        tr["half_px"] = tp1
                hit2 = ((nb["h"] >= tp2) if up else (nb["l"] <= tp2))
                if hit2:
                    tr["exit_px"] = tp2
                    tr["exit_ts"] = nb["ts"]
                    tr["why"] = "2R 止盈"
            if all(t["exit_px"] is not None for t in tranches):
                break
        # 结算
        r_sum = 0.0
        n_fill = 0
        detail = []
        for tr in tranches:
            if tr["fill_ts"] is None:
                detail.append("%s 未成交" % tr["name"])
                continue
            n_fill += 1
            risk = abs(tr["px"] - stop0)
            if tr["exit_px"] is None:
                last = b15[min(i + 48, len(b15) - 1)]["c"]
                ex = last
                why = "超时(收盘)"
            else:
                ex, why = tr["exit_px"], tr["why"]
            if tr.get("half_done") and tr.get("half_px"):
                half = tr["half_px"]
                px = (half + ex) / 2.0
            else:
                px = ex
            r = ((px - tr["px"]) * (1 if up else -1) / risk) if risk else 0.0
            r_sum += r
            detail.append("%s @%.1f→%.1f %s %+.2fR"
                          % (tr["name"], tr["px"], ex, why, r))
        if n_fill == 0:
            continue
        n_done += 1
        avg_r = r_sum / n_fill
        total_r += avg_r
        if avg_r > 0:
            win += 1
        print("\n%s %s  反包K O%.1f H%.1f L%.1f C%.1f"
              % (fmt(b["ts"]), d, O, H, L, C))
        print("   三档：①%.1f(突破) ②%.1f(80%%) ③%.1f(50%%)  止损 %.1f  1R=%.1f"
              % (lv1, lv2, lv3, stop0, r_unit))
        for s in detail:
            print("     " + s)
        print("   合计：成交 %d 份，均价 %+.2fR" % (n_fill, avg_r))

    print("\n" + "=" * 96)
    if n_done:
        print("汇总：反包 %d 个、有成交 %d 个、盈利 %d 个（%.0f%%）、"
              "总 R %+.2f、均 R %+.3f"
              % (len(sigs), n_done, win, 100.0 * win / n_done, total_r,
                 total_r / n_done))
    else:
        print("汇总：反包 %d 个，全部未成交" % len(sigs))


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "ALL").upper()
    t_end = int(time.time() * 1000)
    if mode in ("A", "ALL"):
        mode_a(t_end)
        print()
    if mode in ("B", "ALL"):
        mode_b(t_end)


if __name__ == "__main__":
    main()
