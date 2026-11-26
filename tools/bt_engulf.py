# -*- coding: utf-8 -*-
"""反包三分仓策略回测（15m），上下镜像。

用户的规则（原话）：
  「15m 的 K 线出现反包时…把下的总单仓位分成 3 份，
    第一份买反包K的最高点买突破，
    第二份买反包K的实体K的80%位置买回调，
    第三份买反包K的实体K的50%位置买回调，能成交几单算几单！
    止损放在反包K的最低点下方1个tick，
    止盈就是反包K的最低点和收盘价的1R和2R，
    目标到过1R止盈一半，同时止损上移到反包K实体50%的位置」

★ 第一版写错的点（本轮修）：
  down（看跌反包）的①档必须挂在**最低点**做跌破卖出、止损在**最高点**上方，
  我误套了 up 的公式（①档挂在最高点、止损=最高点+1tick），
  于是风险只有 0.1 个 tick，R 被除成 +2073R 这种垃圾数。
  现在 up/down 各写一套明确的价格与方向。

★ 1R 的定义（用户的"最低点和收盘价"）：
  up   → 1R = 收盘 - 最低
  down → 1R = 最高 - 收盘
  止盈档 = 入场价 ± n × 1R（n=1,2）。
  同时另报一列「按实际风险折算的 R」= 盈亏 / |入场-止损|，两者一起看。

★ 反包定义单独做三种变体对照（用户的 4 个时刻里 22:45 / 09:00 在
  实体反包口径下不成立，需要确认是口径差异还是我看错）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

BASE = "/home/cryptobot/ai-trader"
sys.path.insert(0, BASE)

M15 = 900_000
TICK = 0.1
SYMBOL = "BTCUSDT"
HOURS = float(os.environ.get("BT_HOURS", "48"))
MAX_BARS = 48          # 最长持有 48 根 15m = 12 小时

#: ★★ 手续费（单边）。币安 USDT 永续 taker = 0.05%（有 BNB 折扣 0.04%）。
#: 这一项是**必须算**的：本策略的止损极窄（③50% 档常见 0.15~0.20%），
#: 双边 0.08~0.10% 折成 R 就是 0.4~0.6R —— 比测出来的边际大一个数量级。
#: 不算费的"正收益"是假的。
FEE_SIDE = float(os.environ.get("BT_FEE", "0.0005"))
FEE_RT = FEE_SIDE * 2.0


def fmt(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(int(ts) / 1000))


def load15():
    con = sqlite3.connect("file:%s?mode=ro" % os.path.join(BASE, "data/kbar.db"),
                          uri=True)
    try:
        rows = con.execute(
            "select ts,o,h,l,c,vol from kbar_evolution"
            " where symbol=? and interval_ms=? order by ts",
            (SYMBOL, M15)).fetchall()
    finally:
        con.close()
    return [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4]), "v": float(r[5] or 0.0)}
            for r in rows]


# ---------------------------------------------------------------- 反包口径
def is_up_body(p, b):
    """实体反包（多方）：前阴、今阳，今实体包住前实体。"""
    return (p["c"] < p["o"] and b["c"] > b["o"]
            and b["c"] > p["o"] and b["o"] < p["c"])


def is_dn_body(p, b):
    return (p["c"] > p["o"] and b["c"] < b["o"]
            and b["c"] < p["o"] and b["o"] > p["c"])


def is_up_range(p, b):
    """外包（多方）：今收 > 前高、今开 < 前低（吞掉整根 oscillating bar）。"""
    return b["c"] > b["o"] and b["c"] > p["h"] and b["o"] < p["l"]


def is_dn_range(p, b):
    return b["c"] < b["o"] and b["c"] < p["l"] and b["o"] > p["h"]


def is_up_close(p, b):
    """弱口径：前阴今阳，今收超前开。"""
    return p["c"] < p["o"] and b["c"] > b["o"] and b["c"] > p["o"]


def is_dn_close(p, b):
    return p["c"] > p["o"] and b["c"] < b["o"] and b["c"] < p["o"]


VARIANTS = [("实体反包", is_up_body, is_dn_body),
            ("外包", is_up_range, is_dn_range),
            ("弱口径", is_up_close, is_dn_close)]


# ---------------------------------------------------------------- 单笔模拟
def simulate(bars, i, up, tp_mode="bar"):
    """在第 i 根（反包K）之后模拟三档。

    tp_mode:
      "bar"  —— 用户口径：1R = 反包K「收盘-最低」（做空为最高-收盘），
                止盈 = 入场 ± n×1R。
      "risk" —— 1:1 / 1:2 按**自身实际风险**（入场-止损）算。
                两种口径必须都看：`bar` 口径下①档的风险(H-L)天然大于
                它的报酬(C-L)，会系统性吃亏；`risk` 口径下三档赔率一致。
    """
    b = bars[i]
    O, H, L, C = b["o"], b["h"], b["l"], b["c"]
    body = C - O

    if up:
        lv1, stop0 = H, L - TICK          # ①最高点买突破  止损=最低点-1tick
        r_unit = C - L                    # 用户口径：收盘 - 最低
        sgn = 1.0
    else:
        lv1, stop0 = L, H + TICK          # ①最低点卖跌破  止损=最高点+1tick
        r_unit = H - C
        sgn = -1.0

    lv2 = O + 0.8 * body                  # 实体 80%
    lv3 = O + 0.5 * body                  # 实体 50%
    mid = O + 0.5 * body                  # 止损上移目标

    trs = [{"n": "①突破", "px": lv1},
           {"n": "②80%", "px": lv2},
           {"n": "③50%", "px": lv3}]
    for t in trs:
        t.update(fill=None, exit=None, why="", stop=stop0,
                 half=None, done=False)
        # 每个档位自己的 1R
        t["r1"] = abs(t["px"] - stop0) if tp_mode == "risk" else r_unit

    for j in range(i + 1, min(i + 1 + MAX_BARS, len(bars))):
        nb = bars[j]
        for t in trs:
            if t["exit"] is not None:
                continue
            if t["fill"] is None:
                if t["n"].startswith("①"):
                    hit = nb["h"] >= t["px"] if up else nb["l"] <= t["px"]
                else:
                    hit = nb["l"] <= t["px"] if up else nb["h"] >= t["px"]
                if not hit:
                    continue
                t["fill"] = j
            # 同一根里止损与止盈都碰到 → 先算止损（保守）
            if (nb["l"] <= t["stop"]) if up else (nb["h"] >= t["stop"]):
                t["exit"], t["why"] = t["stop"], "止损"
                continue
            tp1 = t["px"] + sgn * t["r1"]
            tp2 = t["px"] + sgn * 2 * t["r1"]
            if t["half"] is None:
                hit1 = nb["h"] >= tp1 if up else nb["l"] <= tp1
                if hit1:
                    t["half"] = tp1
                    t["stop"] = mid          # 到 1R 平一半 + 止损上移
            hit2 = nb["h"] >= tp2 if up else nb["l"] <= tp2
            if hit2:
                t["exit"], t["why"] = tp2, "2R止盈"
        if all(t["exit"] is not None for t in trs):
            break

    # 未终结 → 按最后一根收盘结
    last = bars[min(i + MAX_BARS, len(bars) - 1)]["c"]
    for t in trs:
        if t["fill"] is not None and t["exit"] is None:
            t["exit"], t["why"] = last, "超时"

    filled = [t for t in trs if t["fill"] is not None]
    for t in filled:
        ex = t["exit"]
        # 平掉一半用 tp1 价，剩下一半用最终出场价
        px = ((t["half"] + ex) / 2.0) if t["half"] is not None else ex
        t["pnl"] = (px - t["px"]) * sgn          # 价格点（未扣费）
        risk = abs(t["px"] - stop0)
        t["r"] = (t["pnl"] / risk) if risk > 0 else 0.0
        t["exit_px"] = px
        # ★ 手续费：入场 + 出场各一次 taker。按**入场价**折算成点数，
        #   再折成 R —— 窄止损下这一项会吃掉很大一块。
        fee_pts = t["px"] * FEE_RT
        t["fee"] = fee_pts
        t["pnl_net"] = t["pnl"] - fee_pts
        t["r_net"] = (t["pnl_net"] / risk) if risk > 0 else 0.0

    return {"bar": b, "up": up, "O": O, "H": H, "L": L, "C": C,
            "lv": (lv1, lv2, lv3), "stop": stop0, "r_unit": r_unit,
            "trs": trs, "filled": filled,
            "pnl": sum(t["pnl"] for t in filled) / 3.0,   # 三等分仓
            "pnl_net": sum(t["pnl_net"] for t in filled) / 3.0,
            "port_r": (sum(t["pnl"] for t in filled) / 3.0 / r_unit)
            if r_unit > 0 else 0.0,
            "port_r_net": (sum(t["pnl_net"] for t in filled) / 3.0 / r_unit)
            if r_unit > 0 else 0.0}


def run_variant(bars, t_lo, t_end, fu, fd, detail=False, tp_mode="bar",
                weights=(1 / 3.0, 1 / 3.0, 1 / 3.0), only=None):
    sigs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        if not (t_lo <= b["ts"] <= t_end):
            continue
        if fu(p, b):
            sigs.append((i, True))
        elif fd(p, b):
            sigs.append((i, False))

    n_done = n_fill = n_win = 0
    tot_port = 0.0
    tot_pnl = 0.0
    tot_net_r = 0.0
    tot_net_pnl = 0.0
    # 按档位拆开统计：这是回答「三份仓位各自值不值得做」的关键
    legs = {k: {"n": 0, "fill": 0, "win": 0, "r": 0.0, "r_net": 0.0,
                "pnl": 0.0, "pnl_net": 0.0, "fee": 0.0}
            for k in ("①突破", "②80%", "③50%")}
    if detail:
        print("\n反包K 共 %d 根\n" % len(sigs))
    for i, up in sigs:
        s = simulate(bars, i, up, tp_mode=tp_mode)
        # 组合口径：按 weights 加权重算（默认三等分），only 指定单档
        if only is not None:
            leg = [t for t in s["trs"] if t["n"] == only][0]
            want = leg["n"]
            wsum = 1.0
            port_r = (leg["pnl"] / s["r_unit"]) if s["r_unit"] > 0 else 0.0
            port_pnl = leg["pnl"]
            if leg["fill"] is None:
                port_r, port_pnl = 0.0, 0.0
            n_used = 1 if leg["fill"] is not None else 0
        else:
            wsum = sum(weights)
            port_r, port_pnl, n_used = 0.0, 0.0, 0
            for t, w in zip(s["trs"], weights):
                if t["fill"] is None or w <= 0:
                    continue
                port_r += w * (t["pnl"] / s["r_unit"]) if s["r_unit"] > 0 else 0
                port_pnl += w * t["pnl"] / wsum if False else w * t["pnl"]
                n_used += 1
            port_pnl = port_pnl / wsum if wsum else 0.0
            port_r = port_r / wsum if wsum else 0.0
        s["port_r"], s["pnl"] = port_r, port_pnl
        # 扣费版本：按同样的权重对 (pnl - fee) 加总
        if only is not None:
            leg = [t for t in s["trs"] if t["n"] == only][0]
            net_r = ((leg["pnl_net"] / s["r_unit"])
                     if (leg["fill"] is not None and s["r_unit"] > 0) else 0.0)
            net_pnl = leg["pnl_net"] if leg["fill"] is not None else 0.0
        else:
            net_r, net_pnl = 0.0, 0.0
            for t, w in zip(s["trs"], weights):
                if t["fill"] is None or w <= 0:
                    continue
                net_r += w * ((t["pnl_net"] / s["r_unit"])
                              if s["r_unit"] > 0 else 0.0)
                net_pnl += w * t["pnl_net"]
            net_r = net_r / wsum if wsum else 0.0
            net_pnl = net_pnl / wsum if wsum else 0.0
        s["port_r_net"], s["pnl_net_w"] = net_r, net_pnl
        s["n_used"] = n_used
        for t in s["trs"]:
            g = legs[t["n"]]
            g["n"] += 1
            if t["fill"] is not None:
                g["fill"] += 1
                g["r"] += t["r"]
                g["r_net"] += t["r_net"]
                g["pnl"] += t["pnl"]
                g["pnl_net"] += t["pnl_net"]
                g["fee"] += t["fee"]
                if t["pnl"] > 0:
                    g["win"] += 1
        if n_used == 0:
            if detail:
                print("  %s %-4s 未成交" % (fmt(s["bar"]["ts"]),
                                             "up" if up else "down"))
            continue
        n_done += 1
        n_fill += len(s["filled"])
        tot_port += s["port_r"]
        tot_pnl += s["pnl"]
        tot_net_r += s["port_r_net"]
        tot_net_pnl += s["pnl_net_w"]
        if s["port_r_net"] > 0:
            n_win += 1
        if not detail:
            continue
        b = s["bar"]
        print("  %s %-4s  反包K O%.1f H%.1f L%.1f C%.1f 实体%+.1f"
              % (fmt(b["ts"]), "up" if up else "down", s["O"], s["H"],
                 s["L"], s["C"], s["C"] - s["O"]))
        print("      三档 ①%.1f(突破) ②%.1f(80%%) ③%.1f(50%%)"
              "  止损 %.1f  1R=%.1f"
              % (s["lv"][0], s["lv"][1], s["lv"][2], s["stop"], s["r_unit"]))
        for t in s["trs"]:
            if t["fill"] is None:
                print("        %-6s 未成交" % t["n"])
                continue
            moved = abs(t["stop"] - s["stop"]) > 1e-9
            print("        %-6s @%9.1f → 均价%9.1f  %-10s %+8.1f 点  %+5.2fR"
                  % (t["n"], t["px"], t["exit_px"],
                     ("移损" if moved and t["why"] == "止损" else t["why"]),
                     t["pnl"], t["r"]))
        print("        组合：%+8.1f 点  %+.2fR(按1R=%.1f)  成交%d份"
              % (s["pnl"], s["port_r"], s["r_unit"], len(s["filled"])))
    return {"n_sig": len(sigs), "n_done": n_done, "n_fill": n_fill,
            "n_win": n_win, "port_r": tot_port, "pnl": tot_pnl,
            "port_r_net": tot_net_r, "pnl_net": tot_net_pnl, "legs": legs}


def main():
    bars = load15()
    t_end = int(time.time() * 1000)
    t_lo = t_end - int(HOURS * 3600 * 1000)
    sel_bars = [b for b in bars if t_lo <= b["ts"] <= t_end]

    print("=" * 100)
    print("反包三分仓回测 · %s → %s（%.0f 小时 / %d 根 15m）"
          % (fmt(t_lo), fmt(t_end), HOURS, len(sel_bars)))
    print("=" * 100)

    # ---- 用户点名的 4 个时刻，逐口径对照
    want = ["11-25 16:00", "11-25 20:15", "11-25 22:45", "11-26 09:00"]
    idx = {fmt(b["ts"]): k for k, b in enumerate(bars)}
    print("\n【用户点名的时刻 · 各口径是否算反包】")
    print("   %-12s %-4s %-9s %-7s %-7s   O→C 实体"
          % ("时刻", "前根", "实体反包", "外包", "弱口径"))
    for w in want:
        k = idx.get(w)
        if k is None:
            print("   %-12s 数据里没有这根" % w)
            continue
        b, p = bars[k], bars[k - 1]
        pdir = "阴" if p["c"] < p["o"] else "阳"
        marks = []
        for name, fu, fd in VARIANTS:
            ok = fu(p, b) if b["c"] > b["o"] else fd(p, b)
            marks.append("✅" if ok else "—")
        print("   %-12s %-4s %-9s %-7s %-7s   %.1f→%.1f (%.1f)"
              % (w, pdir, marks[0], marks[1], marks[2], b["o"], b["c"],
                 b["c"] - b["o"]))

    # ---- 主口径详细表：弱口径（用户的 4 个例子全部覆盖）
    print("\n" + "=" * 100)
    print("主口径 = 弱口径（前阴今阳 且 今收 > 前开；镜像）")
    print("  ★ 选它是因为用户点名的 4 个时刻它全中，实体反包只中 2 个")
    print("=" * 100)
    m = run_variant(bars, t_lo, t_end, is_up_close, is_dn_close, detail=True)

    # ---- 三口径 × 两种止盈口径 汇总对照（含/不含手续费）
    print("\n" + "=" * 100)
    print("汇总对照（%d 小时）  手续费 %.3f%%/边（双边 %.3f%%）"
          % (HOURS, FEE_SIDE * 100, FEE_RT * 100))
    print("  [bar] = 用户口径，1R 取「收盘-最低」；[risk] = 1R 取各自的入场-止损")
    print("=" * 100)
    print("  口径        基准   反包  有成交  盈利  组合总R(不含费) 组合总R(扣费)"
          "  单setup均R(扣费)")
    for name, fu, fd in VARIANTS:
        for mode in ("bar", "risk"):
            r = run_variant(bars, t_lo, t_end, fu, fd, detail=False,
                            tp_mode=mode)
            avg = (r["port_r_net"] / r["n_done"]) if r["n_done"] else 0.0
            wr = (100.0 * r["n_win"] / r["n_done"]) if r["n_done"] else 0.0
            print("  %-10s  %-7s %4d   %5d   %3.0f%%  %+10.2f      %+10.2f"
                  "     %+8.3f"
                  % (name, "[%s]" % mode, r["n_sig"], r["n_done"], wr,
                     r["port_r"], r["port_r_net"], avg))

    # ---- 分档统计：三份仓位各自值不值得做（扣费前后并列）
    print("\n" + "=" * 100)
    print("分档统计（弱口径）—— 回答「三份里哪份该留」，扣费前 vs 扣费后")
    print("=" * 100)
    for mode in ("bar", "risk"):
        r = run_variant(bars, t_lo, t_end, is_up_close, is_dn_close,
                        detail=False, tp_mode=mode)
        print("\n  [%s] 止盈基准" % mode)
        print("     档位    成交  胜率   均R(不含费)  均R(扣费)   均费(点)"
              "   均风险(点)")
        for k in ("①突破", "②80%", "③50%"):
            g = r["legs"][k]
            if not g["fill"]:
                print("     %-8s %5d  ——" % (k, g["fill"]))
                continue
            fee_avg = g["fee"] / g["fill"]
            print("     %-8s %5d  %3.0f%%   %+8.3f    %+8.3f   %8.1f"
                  % (k, g["fill"], 100.0 * g["win"] / g["fill"],
                     g["r"] / g["fill"], g["r_net"] / g["fill"], fee_avg))

    # ---- 权重方案对照：三份怎么分才不亏
    print("\n" + "=" * 100)
    print("权重方案对照（%d 小时）—— 三份怎么分才不亏" % HOURS)
    print("=" * 100)
    print("  方案                              反包  有成交  盈利setup  组合总R"
          "   单setup均R   组合总点")
    plans = [
        ("三等分 1:1:1（用户原方案）", (1 / 3.0, 1 / 3.0, 1 / 3.0), None),
        ("倒金字塔 1:2:3（越深越重）", (1 / 6.0, 2 / 6.0, 3 / 6.0), None),
        ("只要③50%", (0, 0, 1.0), None),
        ("②+③ 各半", (0, 0.5, 0.5), None),
    ]
    for name, fu, fd in [("实体反包", is_up_body, is_dn_body),
                         ("弱口径", is_up_close, is_dn_close)]:
        print("\n  【%s】" % name)
        for label, w, only in plans:
            r = run_variant(bars, t_lo, t_end, fu, fd, detail=False,
                            tp_mode="risk", weights=w, only=only)
            avg = (r["port_r_net"] / r["n_done"]) if r["n_done"] else 0.0
            wr = (100.0 * r["n_win"] / r["n_done"]) if r["n_done"] else 0.0
            print("    %-32s %4d   %5d   %3.0f%%   %+8.2f    %+8.2f   %+8.3f"
                  % (label, r["n_sig"], r["n_done"], wr, r["port_r"],
                     r["port_r_net"], avg))

    print("\n  说明：组合总R = Σ(三份各 1/3 仓的盈亏)/1R；「扣费」含双边"
          " %.2f%%" % (FEE_RT * 100))

    print("\n  说明：组合总R = Σ(三份各 1/3 仓的盈亏)/1R")
    print("        「移损」= 到过 1R、平掉一半后止损上移到实体 50% 又被扫出"
          "（不是原始止损）")


if __name__ == "__main__":
    main()
