# -*- coding: utf-8 -*-
"""安慰剂检验：把入场时刻随机化，看「正收益」还剩多少。

## 为什么必须做这一步

30 天窗口（10-18 → 11-17）BTC 从 ~80k 跌到 ~64k，跌了 20%。
在这个窗口里，**任何**偏空、持有几小时的策略都会赚钱 —— 那不是策略的
边缘，那是市场给的 beta。我扫了 30 组参数，最好的一组「19 笔、胜率 58%、
均净 +0.46%、累计 +8.76%」，如果不做这个检验就发出去，等于把 beta 当成 alpha。

做法：保持**完全相同的**出场逻辑、止损、持有时间、方向，只把入场时刻
换成同一天里的随机 5m 时刻，重复 N 次。如果真信号的表现落在随机分布的
中间，那它没有信息；只有落在尾部才说明信号本身有价值。
"""
from __future__ import annotations

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ai_trader.kbar import setup2 as S          # noqa: E402
from replay_setup2 import load                 # noqa: E402

FEE = 0.09
HOLD = 16

def settle(b5, b15, entries, direction):
    """对一批 {entry_ts, entry, stop} 做出场判定，返回 (笔数, 止损数, 累计净%)。"""
    down = direction == "down"
    n = stop = 0
    total = 0.0
    for e in entries:
        idx = next((i for i, b in enumerate(b15) if b["ts"] >= e["entry_ts"]),
                   None)
        if idx is None:
            continue
        idx += 1
        if idx >= len(b15):
            continue
        end = min(len(b15) - 1, idx + HOLD)
        exit_px, why = b15[end]["c"], "time"
        for b in b15[idx:end + 1]:
            if down and float(b["h"]) >= e["stop"]:
                exit_px, why = e["stop"], "stop"
                break
            if (not down) and float(b["l"]) <= e["stop"]:
                exit_px, why = e["stop"], "stop"
                break
        gross = ((e["entry"] - exit_px) if down
                 else (exit_px - e["entry"])) / e["entry"] * 100.0
        n += 1
        total += gross - FEE
        if why == "stop":
            stop += 1
    return n, stop, total


def main() -> int:
    db = os.path.join(ROOT, "data", "kbar.db")
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    b5, b15, line_px = load(db, days)
    hours = S.aggregate_1h(b15)
    lines = S.lines_by_hour(line_px, hours)
    gp = S.GateParams(min_breaks=1, gate_vol=1.2, direction="down")
    rp = S.RejectParams(direction="down")
    ep = S.EntryParams(direction="down", entry_pts=250.0, entry_vol=3.0,
                       stop_frac=0.0, stop_bars=1, min_stop_rng_mult=1.5,
                       wait_bars5=48)

    breaks = S.detect_breaks(hours, lines, gp)
    gates = S.gate_state(breaks, gp, hours)
    cases = sorted(S.find_pin_case(b5, rp) + S.find_ema_case(b15, rp),
                   key=lambda c: c["at_ts"])
    picked = []
    for g in gates:
        for c in S.cases_after(g, cases):
            picked.append((g, c))
    # 和 replay_setup2 用同一套去重 —— 否则安慰剂检验的笔数和真实信号对不上
    picked = S.dedup_cases(picked)
    entries = [e for e in S.dedup_entries(
        S.find_entries(b5, [c for _, c in picked], hours, ep))
        if e["status"] == "ok"]

    real = settle(b5, b15, entries, "down")
    print("=== 真实信号 ===")
    print("  成交 %d 笔 · 止损 %d · 累计净 %+.2f%% · 均净 %+.3f%%"
          % (real[0], real[1], real[2],
             real[2] / real[0] if real[0] else 0.0))

    # 随机入场：同样的「等 48 根 5m」跨度、同样的止损宽度倍数、同样的持有
    rng = random.Random(20261117)
    valid = [b for b in b5 if b["h"] and b["l"] and b["c"]]
    rng_rng = [float(b["h"]) - float(b["l"]) for b in valid]
    sims = []
    for _ in range(400):
        picks = []
        for e0 in entries:
            b = valid[rng.randrange(len(valid))]
            rngx = float(b["h"]) - float(b["l"])
            stop_dist = rngx * ep.min_stop_rng_mult
            px = float(b["c"])
            picks.append({"entry_ts": b["ts"], "entry": px,
                          "stop": px + stop_dist})
        sims.append(settle(b5, b15, picks, "down"))
    totals = sorted(s[2] for s in sims)
    n_sim = len(totals)
    better = sum(1 for t in totals if t >= real[2])
    med = totals[n_sim // 2]
    p90 = totals[int(n_sim * 0.90)]
    print()
    print("=== 随机入场（400 次，同样笔数 / 同样止损 / 同样持有）===")
    print("  随机累计净：P50 %+.2f%%  P90 %+.2f%%  最好 %+.2f%%  最差 %+.2f%%"
          % (med, p90, totals[-1], totals[0]))
    print("  真实信号 %+.2f%% → 胜过 %.1f%% 的随机样本（p ≈ %.2f）"
          % (real[2], (1 - better / float(n_sim)) * 100,
             better / float(n_sim)))
    if better / float(n_sim) > 0.10:
        print("  结论：没法把真实信号和随机入场区分开 —— 这个「正收益」是市场 beta，不是信号。")
    else:
        print("  结论：真实信号落在随机分布的右尾，但样本只有 %d 笔，仍需更长窗口复核。"
              % real[0])

    # 大盘涨跌：alpha 还是 beta
    if len(b15) > 10:
        first, last = float(b15[0]["c"]), float(b15[-1]["c"])
        print()
        print("=== 窗口本身的涨跌（这点最关键）===")
        print("  %d 天里 15m 收盘 %.1f → %.1f，%+.2f%%"
              % (days, first, last, (last - first) / first * 100))
        print("  做空方向在这个窗口里天然占优 —— 任何偏空策略都会被它推成「赚钱」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
