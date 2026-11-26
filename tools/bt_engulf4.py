# -*- coding: utf-8 -*-
"""反包策略 v4：**每档独立目标 1R/2R/3R** + 止损逐层上爬 + 15m 过滤。

★ v3 的对齐错误（用户 2026-11-26 指出）
  v3 的阶梯是「所有档位共用」1R 平一半、2R 全平 —— 于是
    · 第三份（30% 入场、风险最小）在 2R 就被砍掉，本该拿到的 3R 全部丢失；
    · 30% 那档入场最低、1R 距离最短，反被"1R 平一半"压得最狠。
  用户口径是：**第一份 1R 止盈、第二份 2R 止盈、第三份 3R 止盈**，
  每份各自独立出场，互不牵制；止损随每次止盈**逐层上爬**。

本脚本口径
  入场：实体 80% / 50% / 30% 三档限价（做空镜像）
  目标：第 k 档 = 入场_k + k x R（k = 1,2,3）
  止损：初始 = 最低点 - stop_pct x (H-L)
        第 1 档止盈后爬到 实体 a%，第 2 档止盈后爬到 实体 b%
        （a,b 由 --ladder 给，如 30/50、40/60、50/80；只朝有利方向爬）
  R 两种口径
    risk —— R = |收盘 - 初始止损|（止损放宽后 R 同步放大）
    bar  —— R = 收盘 - 最低（用户原口径，止损放宽后 R 不变）
  最小风险门槛：某档 |入场-止损| < k x 该档手续费 → 该档不做（k 可扫）
  过滤：① 波动率近期性（近20根振幅头部10%必须落在最近10根内）
        ② 背景放量突破（可选）
  费：限价 maker 0.02% + 出场 taker 0.05%
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

BASE = "/home/cryptobot/ai-trader"
sys.path.insert(0, BASE)

M15 = 900_000
SYMBOL = "BTCUSDT"
HOURS = float(os.environ.get("BT_HOURS", "720"))
MAX_BARS = 48
FEE_TAKER = 0.0005
FEE_MAKER = 0.0002
FEE_RT = FEE_MAKER + FEE_TAKER          # 0.07%

VOL_WIN, VOL_TOP, VOL_RECENT = 20, 0.10, 10
BRK_WIN, BRK_RECENT, BRK_VOL_MULT = 20, 10, 2.0
SPLIT = (0.8, 0.5, 0.3)                  # 三档入场位置（实体百分比）


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


def is_up_body(p, b):
    """实体反包（多方）。`<=`/`>=`：连续市场里今开常等于前收。"""
    return (p["c"] < p["o"] and b["c"] > b["o"]
            and b["c"] >= p["o"] and b["o"] <= p["c"])


def is_dn_body(p, b):
    return (p["c"] > p["o"] and b["c"] < b["o"]
            and b["c"] <= p["o"] and b["o"] >= p["c"])


def vol_recency_ok(bars, i):
    lo = max(0, i - VOL_WIN + 1)
    rs = [bars[k]["h"] - bars[k]["l"] for k in range(lo, i + 1)]
    if len(rs) < VOL_WIN or max(rs) <= 0:
        return False
    s = sorted(rs)
    thresh = s[min(len(s) - 1, int(round((1.0 - VOL_TOP) * (len(s) - 1))))]
    if thresh <= 0:
        return False
    r0 = max(0, i - VOL_RECENT + 1)
    return any((bars[j]["h"] - bars[j]["l"]) >= thresh for j in range(r0, i + 1))


def vol_break_ok(bars, i, up):
    lo = max(BRK_WIN, i - BRK_RECENT + 1)
    for j in range(lo, i + 1):
        prev = bars[max(0, j - BRK_WIN):j]
        if len(prev) < 10:
            continue
        mv = sum(x["v"] for x in prev) / len(prev)
        if mv <= 0 or bars[j]["v"] < BRK_VOL_MULT * mv:
            continue
        if up:
            if bars[j]["c"] < min(x["l"] for x in prev):
                return True
        else:
            if bars[j]["c"] > max(x["h"] for x in prev):
                return True
    return False


def simulate(bars, i, up, stop_pct, r_mode, ladder, min_risk_mult):
    """每档独立目标 k*R，止损逐层上爬。

    ladder = (a, b)：第 1 档止盈后止损爬到实体 a%，第 2 档后爬到 b%。
    """
    b = bars[i]
    O, H, L, C = b["o"], b["h"], b["l"], b["c"]
    body = C - O
    span = H - L
    if span <= 0 or abs(body) < 1e-12:
        return None
    sgn = 1.0 if up else -1.0
    stop0 = (L - stop_pct * span) if up else (H + stop_pct * span)
    r_bar = (C - L) if up else (H - C)
    r_risk = abs(C - stop0)
    R = r_risk if r_mode == "risk" else r_bar
    if R <= 0:
        return None

    legs = []
    for k, frac in enumerate(SPLIT):
        px = O + frac * body
        risk = abs(px - stop0)
        if risk <= 0:
            continue
        # ★ 风险连 min_risk_mult 倍手续费都不到 → 这档不做
        if min_risk_mult > 0 and risk < min_risk_mult * px * FEE_RT:
            continue
        legs.append({"k": k, "px": px, "w": 1.0 / len(SPLIT), "fill": None,
                     "closed": [], "exit": None, "why": ""})
    if not legs:
        return None

    stop = stop0
    tp_done = set()
    for j in range(i + 1, min(i + 1 + MAX_BARS, len(bars))):
        nb = bars[j]
        # 1) 先用**本根开盘前**的止损判断（避免同根内爬损后反手打自己）
        stop_hit = (nb["l"] <= stop) if up else (nb["h"] >= stop)
        for t in legs:
            if t["fill"] is None or t["exit"] is not None:
                continue
            if stop_hit:
                rest = 1.0 - sum(f for _, f in t["closed"])
                t["closed"].append((stop, rest))
                t["exit"], t["why"] = stop, "止损"
        # 2) 成交
        for t in legs:
            if t["fill"] is not None or t["exit"] is not None:
                continue
            if (nb["l"] <= t["px"]) if up else (nb["h"] >= t["px"]):
                t["fill"] = j
        # 3) 各档独立目标
        for t in legs:
            if t["fill"] is None or t["exit"] is not None or t["k"] in tp_done:
                continue
            tp = t["px"] + sgn * (t["k"] + 1) * R
            if (nb["h"] >= tp) if up else (nb["l"] <= tp):
                t["closed"].append((tp, 1.0))
                t["exit"], t["why"] = tp, "%dR止盈" % (t["k"] + 1)
                tp_done.add(t["k"])
                # 逐层上爬：第 1 档后 → 实体 a%，第 2 档后 → 实体 b%
                if t["k"] == 0:
                    ns = O + ladder[0] * body
                elif t["k"] == 1:
                    ns = O + ladder[1] * body
                else:
                    continue
                stop = max(stop, ns) if up else min(stop, ns)
        if all(t["exit"] is not None for t in legs if t["fill"] is not None):
            break

    last = bars[min(i + MAX_BARS, len(bars) - 1)]["c"]
    for t in legs:
        t.setdefault("r_gross", 0.0)
        t.setdefault("r_net", 0.0)
        t.setdefault("fee_r", 0.0)
        if t["fill"] is None:
            continue
        if t["exit"] is None:
            rest = 1.0 - sum(f for _, f in t["closed"])
            t["closed"].append((last, rest))
            t["exit"], t["why"] = last, "超时"
        tot = sum(f for _, f in t["closed"])
        if tot <= 0:
            continue
        avg = sum(px * f for px, f in t["closed"]) / tot
        gross = (avg - t["px"]) * sgn
        fee = t["px"] * FEE_RT
        t["r_gross"] = gross / R
        t["r_net"] = (gross - fee) / R
        t["fee_r"] = fee / R

    filled = [t for t in legs if t["fill"] is not None and t["closed"]]
    wsum = sum(t["w"] for t in filled)
    if not filled or wsum <= 0:
        return None
    return {"r_gross": sum(t["w"] * t["r_gross"] for t in filled) / wsum,
            "r_net": sum(t["w"] * t["r_net"] for t in filled) / wsum,
            "fee_r": sum(t["w"] * t["fee_r"] for t in filled) / wsum,
            "n_leg": len(filled), "R": R}


def run(bars, t_lo, t_end, stop_pct, r_mode, ladder, min_risk_mult,
        use_rec, use_brk, side="up"):
    n = w = 0
    sg = sn = fr = rs = 0.0
    n_raw = 0
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        if not (t_lo <= b["ts"] <= t_end):
            continue
        up = is_up_body(p, b)
        dn = (not up) and is_dn_body(p, b)
        if not (up or dn):
            continue
        n_raw += 1
        if side == "up" and not up:
            continue
        if side == "down" and not dn:
            continue
        if use_rec and not vol_recency_ok(bars, i):
            continue
        if use_brk and not vol_break_ok(bars, i, up):
            continue
        s = simulate(bars, i, up, stop_pct, r_mode, ladder, min_risk_mult)
        if s is None:
            continue
        n += 1
        sg += s["r_gross"]
        sn += s["r_net"]
        fr += s["fee_r"]
        rs += s["R"]
        if s["r_net"] > 0:
            w += 1
    return {"n_raw": n_raw, "n": n, "win": w, "r_gross": sg, "r_net": sn,
            "fee_r": fr / n if n else 0.0, "R": rs / n if n else 0.0}


def row(label, r, width=40):
    if not r["n"]:
        print("  %-*s  n=0" % (width, label))
        return
    print("  %-*s n=%-3d 胜率%3.0f%% R%5.0f点 费/R%.3f 总R毛%+8.2f "
          "总R净%+8.2f 均R净%+.3f"
          % (width, label, r["n"], 100.0 * r["win"] / r["n"], r["R"],
             r["fee_r"], r["r_gross"], r["r_net"], r["r_net"] / r["n"]))


def main():
    bars = load15()
    t_end = int(time.time() * 1000)
    t_lo = t_end - int(HOURS * 3600 * 1000)
    print("=" * 122)
    print("反包策略 v4 · 15m · %s → %s（%.0f 天）"
          % (fmt(t_lo), fmt(t_end), HOURS / 24))
    print("每档独立目标：①8 0%入场→1R  ②50%入场→2R  ③30%入场→3R")
    print("止损初始 = 最低点 - stop_pct x (H-L)；第1档止盈后爬到实体 a%，"
          "第2档后爬到实体 b%（逐层上爬）")
    print("=" * 122)

    for r_mode in ("risk", "bar"):
        print("\n" + "#" * 122)
        print("### 止盈基准 R = %s"
              % ("|收盘-止损|（止损放宽后 R 同步放大）" if r_mode == "risk"
                 else "收盘-最低（用户原口径）"))
        print("#" * 122)
        for side, sname in (("up", "只做多"), (None, "上下都做")):
            print("\n--- %s ---" % sname)
            print("  %-40s %-5s %-5s %-9s %-9s %-9s %s"
                  % ("配置", "n", "胜率", "R(点)", "总R毛", "总R净", "均R净"))
            for stop_pct in (0.0, 0.30, 0.50):
                for ladder in ((0.30, 0.50), (0.40, 0.60), (0.50, 0.80)):
                    for mm in (0, 6, 8):
                        for use_rec, tag in ((1, "+波动率"), (0, "裸")):
                            r = run(bars, t_lo, t_end, stop_pct, r_mode,
                                    ladder, mm, use_rec, 0, side=side)
                            row("止损%3.0f%% 爬%2.0f/%2.0f 风险%.0fx %s"
                                % (stop_pct * 100, ladder[0] * 100,
                                   ladder[1] * 100, mm, tag), r)


if __name__ == "__main__":
    main()
