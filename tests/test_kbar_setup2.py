# -*- coding: utf-8 -*-
"""买法三（1h 三次带量破区间 → 5m 量能根入场）的测试。

守三件事：
1. **闸门必须要求带量** —— 用户明确说「每次突破带量」。
   如果实现里漏了这个，闸门会在任何慢跌里都开着，等于没有闸门。
2. **止损基准是 1h 振幅，不是 15m** —— 这是我上一版错的量纲。
3. **不做未来函数** —— 止损只用入场时刻**已完成**的 1h 桶。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import setup2 as S   # noqa: E402

H = S.HOUR_MS
M15 = 900_000
M5 = 300_000
BASE = 1_700_000_000_000 - (1_700_000_000_000 % H)


def _b15(ts, o, h, l, c, vol=100.0, upw=0.2, dnw=0.2, ema=None):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "vol": vol,
            "up_wick_pct": upw, "dn_wick_pct": dnw, "ema20": ema}


def _b5(ts, o, h, l, c, vol=100.0, vol_rel=1.0):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c, "vol": vol,
            "vol_rel": vol_rel}


def _case(at_ts, kind="pin", ref_low=1000.0, ref_high=1010.0):
    """手搓一个拒绝形态。

    `ref_low` / `ref_high` 不是可选的：第 3 段要拿它判「反弹结构坏了没有」。
    第一版让 `find_entries` 自己去 bars5 里按 ts 找那根 bar，找不到就静默
    跳过破位确认 —— 一道闸被悄悄拆掉。现在缺这个字段会返回 `no_ref`。
    """
    return {"kind": kind, "at_ts": at_ts, "from_ts": at_ts, "bars": [],
            "ref_low": ref_low, "ref_high": ref_high}


def _hours(n=8, close=1000.0, step=-20.0, vol=100.0, rng=40.0):
    """造 n 个 1h 桶（用 4 根 15m 各占 1/4 小时）。"""
    bars = []
    for i in range(n):
        c = close + step * i
        o = c - step
        h, l = max(o, c) + rng / 2, min(o, c) - rng / 2
        t0 = BASE + i * H
        for j in range(4):
            bars.append(_b15(t0 + j * M15, o, h, l, c, vol=vol / 4))
    return bars


def _hours_from(closes, vols=None, rng=20.0):
    """按给定的**收盘价序列**造 1h 桶（每小时 4 根 15m）。

    为什么按收盘序列造：真实的三推是「跌-反弹回线上-再跌-再反弹-再跌」，
    也就是收盘要**来回穿**同一条线。如果让收盘单调下跌，只会穿一次，
    造不出「三次同向突破」。
    """
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        h = max(o, c) + rng / 2
        l = min(o, c) - rng / 2
        v = (vols[i] if vols else 100.0) / 4.0
        t0 = BASE + i * H
        for j in range(4):
            bars.append(_b15(t0 + j * M15, o, h, l, c, vol=v))
    return bars


def _flat_lines(hours, px, key="range_soft_dn@1h"):
    """把同一个线价投影到每个 1h 桶（用该小时最后一根 15m 的时刻）。"""
    out = {}
    for k, hb in hours.items():
        out[(hb["last_ts"], key)] = px
    return out


def test_aggregate_1h_is_exact():
    bars = _hours_from([1000.0, 980.0], vols=[400, 400], rng=20.0)
    hours = S.aggregate_1h(bars)
    assert len(hours) == 2
    k = BASE
    assert hours[k]["v"] == pytest.approx(400.0)      # 4 × 100
    assert hours[k]["c"] == pytest.approx(1000.0)
    # 首根 o == c == 1000，所以 h−l = 20（只有 rng，没有实体跨度）
    assert hours[k]["h"] - hours[k]["l"] == pytest.approx(20.0)
    # 第二小时：o=1000 c=980 → h = 1000+10 = 1010，l = 980−10 = 970
    k2 = BASE + H
    assert hours[k2]["h"] == pytest.approx(1010.0)
    assert hours[k2]["l"] == pytest.approx(970.0)
    assert hours[k2]["h"] - hours[k2]["l"] == pytest.approx(40.0)


def test_lines_project_onto_the_hour_using_its_last_15m():
    bars = _hours(2)
    hours = S.aggregate_1h(bars)
    line_px = {}
    for i in range(2):
        t0 = BASE + i * H
        for j in range(4):
            line_px[(t0 + j * M15, "range_soft_dn@1h")] = 900.0 + i
    lines = S.lines_by_hour(line_px, hours)
    assert len(lines) == 2
    assert lines[BASE]["range_soft_dn@1h"] == 900.0
    assert lines[BASE + H]["range_soft_dn@1h"] == 901.0


def test_break_needs_a_close_crossing_not_a_wick():
    """收盘突破才叫突破。影线穿过去又收回来不算。"""
    bars = _hours(4, close=1000, step=0, rng=40)
    hours = S.aggregate_1h(bars)
    # 让每小时的线价都 = 995；收盘 1000 始终在上方 → 无突破
    line_px = {}
    for i in range(4):
        for j in range(4):
            line_px[(BASE + i * H + j * M15, "range_soft_dn@1h")] = 995.0
    lines = S.lines_by_hour(line_px, hours)
    p = S.GateParams(direction="down")
    assert S.detect_breaks(hours, lines, p) == []


def test_break_detected_when_close_crosses():
    bars = _hours(4, close=1000, step=-20, rng=20)   # 1000, 980, 960, 940
    hours = S.aggregate_1h(bars)
    line_px = {}
    for i in range(4):
        for j in range(4):
            line_px[(BASE + i * H + j * M15, "range_soft_dn@1h")] = 970.0
    lines = S.lines_by_hour(line_px, hours)
    brks = S.detect_breaks(hours, lines, S.GateParams(direction="down"))
    assert len(brks) == 1
    assert brks[0]["h_ts"] == BASE + 2 * H       # 980 → 960 跌破 970
    assert brks[0]["beyond_pts"] == pytest.approx(10.0)


def test_gate_requires_three_breaks_within_the_lookback_window():
    """收盘来回穿同一条线三次 = 三个区间被依次跌破（用户说的「三次同方向突破」）。

    造法：1000 → 960（破）→ 1010（回到线上）→ 950（破）→ 1005（回）→ 940（破）。
    """
    closes = [1000.0, 960.0, 1010.0, 950.0, 1005.0, 940.0]
    bars = _hours_from(closes, rng=20.0)
    hours = S.aggregate_1h(bars)
    lines = S.lines_by_hour(_flat_lines(hours, 980.0), hours)
    brks = S.detect_breaks(hours, lines, S.GateParams(direction="down"))
    assert len(brks) == 3, [b["close"] for b in brks]
    # 量恒定 → vol_rel 1.0，不算「带量」
    g_strict = S.find_gates(brks, S.GateParams(direction="down",
                                               gate_vol=1.5), heavy_only=True)
    assert g_strict == [], "量比 1.0 不该算「带量突破」"
    g_loose = S.find_gates(brks, S.GateParams(direction="down",
                                             gate_vol=0.9), heavy_only=True)
    assert g_loose, "量比够时应该开闸"
    assert g_loose[0]["n_breaks"] == 3


def test_heavy_break_is_the_one_with_volume_spike():
    closes = [1000.0, 960.0, 1010.0, 950.0, 1005.0, 940.0]
    # 第 4 个小时（index 3，收 950 那次破位）量 5 倍
    bars = _hours_from(closes, vols=[100, 100, 100, 500, 100, 100], rng=20.0)
    hours = S.aggregate_1h(bars)
    lines = S.lines_by_hour(_flat_lines(hours, 980.0), hours)
    brks = S.detect_breaks(hours, lines,
                           S.GateParams(direction="down", vol_win=5,
                                        gate_vol=2.0))
    heavy = [b for b in brks if b["heavy"]]
    assert len(heavy) == 1, [(b["h_ts"] - BASE) // H for b in brks if b["heavy"]]
    assert heavy[0]["h_ts"] == BASE + 3 * H
    assert heavy[0]["vol_rel"] > 2.0


def test_gate_vol_parameter_is_not_decoration():
    """`gate_vol` 必须真的被用到。

    第一版 `find_gates` 直接读 `detect_breaks` 烘好的 `heavy` 字段，
    传进来的 `gate_vol` 被静默忽略 —— 看起来能调，实际调了没用。
    这比参数不存在更坏：它会让人误以为扫过一遍了。
    """
    closes = [1000.0, 960.0, 1010.0, 950.0, 1005.0, 940.0]
    bars = _hours_from(closes, rng=20.0)          # 量恒定 → vol_rel 恒为 1.0
    hours = S.aggregate_1h(bars)
    lines = S.lines_by_hour(_flat_lines(hours, 980.0), hours)
    brks = S.detect_breaks(hours, lines, S.GateParams(direction="down"))
    assert len(brks) == 3
    assert S.find_gates(brks, S.GateParams(direction="down", gate_vol=1.5)) == []
    assert S.find_gates(brks, S.GateParams(direction="down", gate_vol=0.9)), \
        "gate_vol 调低之后必须开闸，否则这个参数没接线"


def test_mirror_up_direction_works():
    bars = _hours(4, close=1000, step=+20, rng=20)      # 上涨
    hours = S.aggregate_1h(bars)
    line_px = {}
    for i in range(4):
        for j in range(4):
            line_px[(BASE + i * H + j * M15, "range_soft_up@1h")] = 1030.0
    lines = S.lines_by_hour(line_px, hours)
    brks = S.detect_breaks(hours, lines, S.GateParams(direction="up"))
    assert brks, "向上突破也要能识别"
    assert brks[0]["beyond_pts"] > 0


def test_pin_case_needs_two_consecutive_pins():
    p = S.RejectParams(direction="down", pin_count=2, pin_pct=0.6,
                       pin_pts=400.0)
    # 上引线 500 点、全幅 600 → up_pct = 0.83
    pin_up = _b5(BASE, 1000, 1500, 950, 1000)
    one = [_b5(BASE - M5, 1000, 1010, 990, 1000), pin_up,
           _b5(BASE + M5, 1000, 1010, 990, 1000)]
    assert S.find_pin_case(one, p) == [], "单根插针不够"
    two = [pin_up, _b5(BASE + M5, 1000, 1500, 950, 1000)]
    got = S.find_pin_case(two, p)
    assert len(got) == 1 and got[0]["kind"] == "pin"


def test_pin_case_requires_both_ratio_and_absolute_points():
    """只看比例，一根 5 点的小 K 也能占 70%。"""
    p = S.RejectParams(direction="down", pin_count=1, pin_pct=0.6,
                       pin_pts=400.0)
    tiny = _b5(BASE, 1000, 1003, 1000, 1000)      # 上引 3 点，占 100%
    assert S.find_pin_case([tiny], p) == []


def test_ema_case_needs_a_wick_and_a_close_below_ema():
    p = S.RejectParams(direction="down", ema_count=2, ema_wick=0.35,
                       ema_tol=0.0015)
    rej = _b15(BASE, 995, 1008, 994, 996, upw=0.75, ema=1000.0)
    got = S.find_ema_case([rej, _b15(BASE + M15, 995, 1008, 994, 996,
                                     upw=0.75, ema=1000.0)], p)
    assert len(got) == 1 and got[0]["kind"] == "ema"
    # 收盘在 ema 上方就不算拒绝
    above = _b15(BASE, 995, 1008, 994, 1005, upw=0.75, ema=1000.0)
    assert S.find_ema_case([above, above], p) == []


def test_cases_must_come_after_the_gate():
    gate = {"open_ts": BASE + 2 * H, "close_ts": BASE + 4 * H}
    before = {"kind": "pin", "at_ts": BASE + H, "from_ts": BASE, "bars": []}
    during = {"kind": "pin", "at_ts": BASE + 3 * H, "from_ts": BASE,
              "bars": []}
    after = {"kind": "pin", "at_ts": BASE + 5 * H, "from_ts": BASE,
             "bars": []}
    got = S.cases_after(gate, [before, during, after])
    assert got == [during]


def test_gate_stays_open_until_price_recovers_the_line():
    """闸门是**状态**不是一次性事件。

    第一版用「破位后固定 24 根 15m 之内」，实测漏掉了 11-15 07:00 开闸、
    11-16 00:30 才出现的拒绝 —— 中间隔了 70 根 15m。而那段正是用户描述的
    「跌到 61900 然后反弹」的行情。
    """
    closes = [1000.0, 960.0, 1010.0, 950.0, 1005.0, 940.0, 935.0, 930.0,
              1000.0]                       # 最后一根收回线上
    bars = _hours_from(closes, rng=20.0)
    hours = S.aggregate_1h(bars)
    lines = S.lines_by_hour(_flat_lines(hours, 980.0), hours)
    gp = S.GateParams(direction="down", gate_vol=0.9, min_breaks=3)
    gates = S.gate_state(S.detect_breaks(hours, lines, gp), gp, hours)
    assert len(gates) == 1
    g = gates[0]
    assert g["open_ts"] == BASE + 5 * H, "第 3 次破位是 +5h"
    assert g["invalidated"] is True
    assert g["close_ts"] == BASE + 8 * H, "收盘站回 980 的那一小时才是终点"
    # 闸门横跨 3 小时 —— 固定 24 根 15m（=6 小时）碰巧够，
    # 但真实行情里那段是 70 根，这才是必须改成状态的证据。
    assert (g["close_ts"] - g["open_ts"]) // H == 3


def test_stop_uses_the_completed_hour_not_the_current_one():
    """止损只用**已完成**的 1h 桶 —— 别偷看还没走完的那根。"""
    bars15 = _hours(8, close=1000, step=-20, rng=20, vol=100)
    hours = S.aggregate_1h(bars15)
    # 当前桶做得特别大，若实现偷看它，止损会明显变宽
    cur = BASE + 5 * H
    hours[cur]["h"] = 2000.0
    hours[cur]["l"] = 500.0

    cases = [_case(cur - M15, ref_low=1000.0, ref_high=1010.0)]
    bars5 = [_b5(cur + 0 * M5, 900, 1300, 850, 880, vol_rel=9.0)]
    p = S.EntryParams(direction="down", entry_pts=300.0, entry_vol=5.0,
                      stop_frac=0.25, stop_bars=1)
    out = S.find_entries(bars5, cases, hours, p)
    assert out and out[0]["status"] == "ok"
    # 上一小时桶振幅 20 + 20 = 40 → 止损 10 点；若偷看当前桶会是 375 点
    assert out[0]["stop_pts"] == pytest.approx(10.0)


def test_entry_needs_both_amplitude_and_volume_spike():
    # 入场时刻所在的 1h 桶 + 上一小时（止损基准要用已完成的那个）
    hours = {BASE - H: {"h": 1040.0, "l": 1000.0, "c": 1010.0},
             BASE: {"h": 1040.0, "l": 1000.0, "c": 1010.0}}
    case = [_case(BASE, ref_low=900.0, ref_high=1010.0)]
    p = S.EntryParams(direction="down", entry_pts=350.0, entry_vol=5.0)
    # 振幅只有 50 点
    small = [_b5(BASE + M5, 1060, 1070, 1020, 1030, vol_rel=9.0)]
    assert S.find_entries(small, case, hours, p)[0]["status"] == "no_entry_bar"
    # 振幅够但没量
    quiet = [_b5(BASE + M5, 1300, 1300, 850, 880, vol_rel=1.0)]
    assert S.find_entries(quiet, case, hours, p)[0]["status"] == "no_entry_bar"
    good = [_b5(BASE + M5, 1300, 1300, 850, 880, vol_rel=9.0)]
    out = S.find_entries(good, case, hours, p)
    assert out[0]["status"] == "ok"


def test_entry_bar_must_close_in_the_trade_direction():
    """收阳的那根大 K 不能拿来当空头入场。

    这一条是实测补上的：第一版只看振幅和量比，结果把「反弹还在继续」的
    那根大阳线当成了入场 —— 那根 K 本身就是往止损方向跑的，所以回放出来
    100% 止损。
    """
    hours = {BASE - H: {"h": 1040.0, "l": 1000.0, "c": 1010.0}}
    case = [_case(BASE, ref_low=900.0, ref_high=1010.0)]
    p = S.EntryParams(direction="down", entry_pts=350.0, entry_vol=5.0)
    bullish = [_b5(BASE + M5, 900, 1300, 850, 1290, vol_rel=9.0)]
    assert S.find_entries(bullish, case, hours, p)[0]["status"] == "no_entry_bar"
    bearish = [_b5(BASE + M5, 1290, 1300, 850, 880, vol_rel=9.0)]
    assert S.find_entries(bearish, case, hours, p)[0]["status"] == "ok"


def test_entry_needs_the_rejection_low_taken_out():
    """只出现一根大阴线还不够：得跌破拒绝那根的低点，反弹结构才算坏。"""
    hours = {BASE - H: {"h": 1040.0, "l": 1000.0, "c": 1010.0}}
    ref = _b5(BASE, 1000, 1020, 900, 920, vol_rel=1.0)      # 拒绝那根，低点 900
    p = S.EntryParams(direction="down", entry_pts=350.0, entry_vol=5.0)
    shallow = [_b5(BASE + M5, 1300, 1300, 950, 960, vol_rel=9.0)]   # 没破 900
    out = S.find_entries(shallow, [_case(BASE, ref_low=900.0)], hours, p)
    assert out[0]["status"] == "no_entry_bar"
    deep = [_b5(BASE + M5, 1300, 1300, 850, 860, vol_rel=9.0)]
    out2 = S.find_entries(deep, [_case(BASE, ref_low=900.0)], hours, p)
    assert out2[0]["status"] == "ok"


def test_stop_frac_is_a_knob():
    hours = {BASE - H: {"h": 1040.0, "l": 1000.0, "c": 1010.0}}
    case = [_case(BASE, ref_low=900.0, ref_high=1010.0)]
    bar = [_b5(BASE + M5, 1300, 1300, 850, 880, vol_rel=9.0)]
    a = S.find_entries(bar, case, hours, S.EntryParams(stop_frac=0.25))[0]
    b = S.find_entries(bar, case, hours, S.EntryParams(stop_frac=0.50))[0]
    assert b["stop_pts"] == pytest.approx(2 * a["stop_pts"])


def test_stop_distance_is_in_the_hundreds_not_tens():
    """这条是上一版错的量纲：1h 振幅 × 1/4 才可能落在几百点。

    上一版用 15m 的 h−l × 1/4，止损中位数只有 38 点，跟用户说的
    「好几百点」差了整整一个量级。
    """
    # 真实 BTC 1h 振幅约 400–450 点
    hours = {BASE - H: {"h": 10450.0, "l": 10000.0, "c": 10200.0}}
    # 拒绝那根的低点 10200，入场那根要跌破它才算结构坏
    case = [_case(BASE, ref_low=10200.0)]
    bar = [_b5(BASE + M5, 10600, 10600, 10100, 10150, vol_rel=9.0)]
    out = S.find_entries(bar, case, hours,
                         S.EntryParams(stop_frac=0.25))[0]
    assert out["status"] == "ok"
    assert out["stop_pts"] == pytest.approx(450 * 0.25)
    assert out["stop_pts"] > 100, "止损应该是三位数点数"


def test_stop_can_be_forced_outside_the_entry_bar_range():
    """止损必须落在入场那根 5m 之外。

    实测 30 天里 9/9 笔的 1h×1/4 止损都落在入场那根 5m 的振幅**里面**
    （振幅/止损 2.7–6.4 倍）。5m 的 OHLC 没有 bar 内顺序，这种止损
    「有没有被打」在 K 线数据上判不了 —— 不是参数问题，是坐标系选错了：
    入场在 5m，止损却用 1h/4。
    """
    hours = {BASE - H: {"h": 10100.0, "l": 10000.0, "c": 10050.0}}  # 振幅 100
    case = [_case(BASE, ref_low=10200.0)]
    bar = [_b5(BASE + M5, 10600, 10600, 10100, 10150, vol_rel=9.0)]  # 振幅 500
    a = S.find_entries(bar, case, hours, S.EntryParams(stop_frac=0.25))[0]
    assert a["stop_pts"] == pytest.approx(25.0), "1h 振幅 100 × 1/4"
    assert a["stop_pts"] < a["entry_rng"], "默认情况下止损在 K 线体内"
    b = S.find_entries(bar, case, hours,
                       S.EntryParams(stop_frac=0.25,
                                     min_stop_rng_mult=0.5))[0]
    assert b["stop_pts"] == pytest.approx(250.0), "振幅 500 × 0.5"
    assert b["stop_pts"] >= b["entry_rng"] * 0.5


def test_duplicate_cases_are_collapsed():
    """同一个拒绝形态只能算一次。

    闸门区间会重叠，同一个拒绝形态会被每个闸门各挑中一次；pin 和 ema
    两种形态也可能落在同一个 ts 上。实测 60 天「90 笔」去重后只有 33 笔 ——
    累计收益被放大了 2.7 倍。这类重复不改变单笔数字，只吹大样本量，
    最容易骗过自己。
    """
    a = _case(BASE, kind="pin")
    b = _case(BASE, kind="ema")            # 同 ts、不同形态
    c = _case(BASE + M15)
    gates = [{"open_ts": BASE - H, "close_ts": BASE + H},
             {"open_ts": BASE - 2 * H, "close_ts": BASE + 2 * H}]
    picked = [(gates[0], a), (gates[1], b), (gates[0], c)]
    got = S.dedup_cases(picked)
    assert [x[1]["at_ts"] for x in got] == [BASE, BASE + M15]


def test_duplicate_entries_are_collapsed():
    """两个不同的拒绝形态可能等到同一根量能 5m，只能算一笔。"""
    es = [{"status": "ok", "entry_ts": BASE, "entry": 1.0},
          {"status": "ok", "entry_ts": BASE, "entry": 1.0},
          {"status": "no_entry_bar"},
          {"status": "ok", "entry_ts": BASE + M5, "entry": 1.0}]
    got = S.dedup_entries(es)
    assert len(got) == 3
