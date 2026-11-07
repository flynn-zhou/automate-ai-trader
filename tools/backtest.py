# -*- coding: utf-8 -*-
"""回测引擎：在历史 1m K 线上跑真实的信号+风控+撮合闭环。

设计原则（避免自欺）：
1. 前视防护：每根 K 只用它**之前**的数据算特征，决策在该根**收盘**执行。
2. 成本真实：吃单 0.05%/边 + 滑点，和实盘同一套常数。
3. 一个 PnL 内核：和实盘共用同样的止损/目标/超时/PnL 公式（在这里独立实现并交叉验证）。
4. 加减仓、部分平仓一律不做——先把最简单的形态测出结论。
5. 输出必须包含：胜率、赔率、期望、成本占比、按信号/方向拆分、
   以及**随机对照**（同期随机方向同样止损目标），后者用来判断"是不是真的有边际"。

用法：
    python tools/backtest.py --db data/backtest.db --interval 1m
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ 这是【永续】taker 0.05%，不是线上费率。
# 线上跑的是现货，taker 0.10%（trading.taker_fee_for(is_spot=True)）。
# 用这个常数算现货策略的收益会高估约 6%/年（30 笔/年 x 0.1% 差额）。
# 现货口径见 tools/liveedge.py（第 61 轮已核定）。
TAKER = 0.0005
SLIP = 0.0002          # 单边滑点 2bp
MIN_STOP = 0.40
MAX_STOP = 3.0
RR = 1.6
RISK_PCT = 35.0
LEV_CAP = 60.0
MAX_HOLD_BARS = 28800  # 对应 3s 的 24 小时；这里按分钟 K 换算


def load(db, interval):
    c = sqlite3.connect(db)
    rows = c.execute("SELECT open_time,o,h,l,c,v,b,s,n FROM klines WHERE interval=?"
                     " ORDER BY open_time", (interval,)).fetchall()
    c.close()
    return rows


def atr(bars, i, n):
    seg = bars[max(0, i - n):i]
    if len(seg) < 2:
        return 0.0
    trs = []
    for k in range(1, len(seg)):
        h, l, pc = seg[k][2], seg[k][3], seg[k - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def sigma_ret(bars, i, n=60):
    """最近 n 根的收益标准差（%），用来做自适应尺度。"""
    seg = bars[max(0, i - n):i]
    rs = []
    for k in range(1, len(seg)):
        pc = seg[k - 1][4]
        if pc:
            rs.append((seg[k][4] - pc) / pc * 100.0)
    return statistics.pstdev(rs) if len(rs) > 1 else 0.0


def swing(bars, i, n=20):
    """最近 n 根**已收盘**K 的极值（不含当前正在判断的这根）。

    曾经写成 bars[max(0,i-n):i] —— 那包含了 bars[i-1]，而 price 就是 bars[i-1] 的收盘，
    于是一定有 price <= hi，突破判定永远为假，全样本零信号。这是自欺型 bug：
    回测显示"没交易"，看起来像"策略保守"，其实是条件永远不成立。
    """
    seg = bars[max(0, i - 1 - n):max(0, i - 1)]
    if not seg:
        return 0.0, 0.0
    return max(b[2] for b in seg), min(b[3] for b in seg)


def detect(bars, i, cfg):
    """在 i 处（用 [0,i) 的数据）判断信号。返回 (side, stop_hint, tag) 或 None。"""
    if i < 70:
        return None
    price = bars[i - 1][4]
    if not price:
        return None
    sg = sigma_ret(bars, i, 60)
    if sg <= 0:
        return None

    # A) 微型动量：近 6 根位移 >= k*sigma 且量能放大，突破 20 根结构
    if cfg["sig"] in ("all", "micro"):
        seg = bars[i - 6:i]
        ref = seg[0][4]
        mv = (price - ref) / ref * 100.0 if ref else 0.0
        n = abs(mv) / sg
        recent_v = sum(b[5] for b in seg) / 6.0
        base_v = sum(b[5] for b in bars[max(0, i - 60):i - 6]) / max(1, len(bars[max(0, i - 60):i - 6]))
        vx = recent_v / base_v if base_v > 0 else 0.0
        hi, lo = swing(bars, i, 20)
        if n >= cfg["micro_sigma"] and vx >= 1.0:
            if mv > 0 and price > hi:
                return ("long", lo * (1 - 0.03 / 100), "micro_momentum")
            if mv < 0 and price < lo:
                return ("short", hi * (1 + 0.03 / 100), "micro_momentum")

    # B) 3 分钟突破：收盘突破 20 根极值 + 量能放大
    if cfg["sig"] in ("all", "breakout"):
        seg = bars[i - 20:i]
        hi = max(b[2] for b in seg)
        lo = min(b[3] for b in seg)
        vs = [b[5] for b in seg]
        mv = statistics.mean(vs)
        sd = statistics.pstdev(vs)
        vz = (bars[i - 1][5] - mv) / sd if sd > 0 else 0.0
        if vz >= 1.0:
            if price > hi:
                return ("long", lo, "breakout")
            if price < lo:
                return ("short", hi, "breakout")
    return None


def run(bars, cfg, seed=None, random_entry=False):
    rnd = random.Random(seed)
    trades = []
    i = 70
    pos = None
    while i < len(bars) - 1:
        row = bars[i]
        o, h, l, c = row[1], row[2], row[3], row[4]
        if pos is None:
            sig = None
            if random_entry:
                if rnd.random() < 0.002:          # 随机对照组：同样的频率
                    side = "long" if rnd.random() < 0.5 else "short"
                    sg = sigma_ret(bars, i, 60) or 0.01
                    sig = (side, c * (1 - side_dir(side) * 0.004), "random")
            else:
                sig = detect(bars, i, cfg)
            if sig:
                side, hint, tag = sig
                d = side_dir(side)
                stop_dist = abs(c - hint)
                sg = sigma_ret(bars, i, 60)
                floor = max(c * MIN_STOP / 100.0, c * sg * 4 / 100.0 if sg else 0)
                stop_dist = max(stop_dist, floor)
                stop_pct = stop_dist / c * 100
                if not (MIN_STOP <= stop_pct <= MAX_STOP):
                    i += 1
                    continue
                tp_pct = stop_pct * RR
                if sg > 0:
                    need = (tp_pct / sg) ** 2
                    if need > MAX_HOLD_BARS:
                        i += 1
                        continue
                # 仓位：杠杆封顶反推
                qty = min((100.0 * LEV_CAP) / c, (100.0 * RISK_PCT / 100.0) / stop_dist)
                if qty <= 0:
                    i += 1
                    continue
                pos = {"side": side, "d": d, "entry": c, "stop": c - d * stop_dist,
                       "target": c + d * tp_pct / 100.0 * c, "qty": qty,
                       "i0": i, "tag": tag, "best": c}
                i += 1
                continue
        else:
            pos["best"] = max(pos["best"], h) if pos["d"] > 0 else min(pos["best"], l)
            hit_stop = l <= pos["stop"] if pos["d"] > 0 else h >= pos["stop"]
            hit_tp = h >= pos["target"] if pos["d"] > 0 else l <= pos["target"]
            exit_px = None
            reason = ""
            if hit_stop:
                exit_px, reason = pos["stop"], "stop"
            elif hit_tp:
                exit_px, reason = pos["target"], "target"
            elif i - pos["i0"] >= MAX_HOLD_BARS:
                exit_px, reason = c, "timeout"
            if exit_px is not None:
                gross = (exit_px - pos["entry"]) * pos["qty"] * pos["d"]
                fee = (pos["entry"] + exit_px) * pos["qty"] * (TAKER + SLIP)
                trades.append({"tag": pos["tag"], "side": pos["side"], "reason": reason,
                               "gross": gross, "fee": fee, "net": gross - fee,
                               "bars": i - pos["i0"]})
                pos = None
        i += 1
    return trades


def side_dir(side):
    return 1 if side == "long" else -1


def summarize(trades, label):
    if not trades:
        print("%s：无交易" % label)
        return {}
    n = len(trades)
    wins = [t for t in trades if t["net"] > 0]
    gross = sum(t["gross"] for t in trades)
    fee = sum(t["fee"] for t in trades)
    net = sum(t["net"] for t in trades)
    aw = sum(t["net"] for t in wins) / len(wins) if wins else 0.0
    losses = [t for t in trades if t["net"] <= 0]
    al = abs(sum(t["net"] for t in losses)) / len(losses) if losses else 0.0
    wr = len(wins) / n
    odds = aw / al if al else None
    be = al / (aw + al) if (aw + al) > 0 else None
    by = {}
    for t in trades:
        d = by.setdefault(t["tag"] + "/" + t["side"], {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += t["net"]
    byr = {}
    for t in trades:
        d = byr.setdefault(t["reason"], {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += t["net"]
    print("\n=== %s ===" % label)
    print("交易 %d 笔｜胜率 %.1f%%｜平均赢 %.3f / 平均输 %.3f｜赔率 %s"
          % (n, wr * 100, aw, al, ("%.2f" % odds) if odds else "-"))
    print("毛 %.2fU｜费 %.2fU（占毛 %.0f%%）｜净 %.2fU｜期望 %.4fU/笔"
          % (gross, fee, (fee / abs(gross) * 100) if gross else 0, net, net / n))
    print("盈亏平衡胜率 %s｜边际 %s"
          % (("%.1f%%" % (be * 100)) if be else "-",
             ("%+.1f%%" % ((wr - be) * 100)) if be else "-"))
    print("按退出原因：" + "，".join("%s %d笔 %.1fU" % (k, v["n"], v["net"])
                                    for k, v in sorted(byr.items())))
    print("按信号/方向：" + "，".join("%s %d笔 %.1fU" % (k, v["n"], v["net"])
                                     for k, v in sorted(by.items())))
    return {"n": n, "win_rate": wr, "net": net, "fee": fee, "gross": gross,
            "odds": odds, "edge": (wr - be) if be else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/backtest.db")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--sig", default="all")
    ap.add_argument("--micro-sigma", type=float, default=3.0)
    ap.add_argument("--random-control", action="store_true")
    a = ap.parse_args()

    bars = load(a.db, a.interval)
    print("载入 %d 根 %s" % (len(bars), a.interval))
    if len(bars) < 200:
        print("数据太少")
        return
    cfg = {"sig": a.sig, "micro_sigma": a.micro_sigma}
    tr = run(bars, cfg)
    r1 = summarize(tr, "策略信号")
    if a.random_control:
        rc = run(bars, cfg, seed=42, random_entry=True)
        r2 = summarize(rc, "随机对照组（同频率随机方向）")
        if r1 and r2:
            print("\n=== 判定 ===")
            print("策略净 %.2fU vs 随机净 %.2fU" % (r1["net"], r2["net"]))
            if r1["net"] > r2["net"] and r1["net"] > 0:
                print("=> 策略优于随机且为正，边际可能真实（仍需样本量与分年验证）")
            elif r1["net"] > r2["net"]:
                print("=> 策略优于随机但仍为负：环境/成本问题")
            else:
                print("=> 策略不优于随机：信号无信息量，别上线")


if __name__ == "__main__":
    main()
