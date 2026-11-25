# -*- coding: utf-8 -*-
"""规则回放器的测试。

守两件事：
1. **信号识别的方向对称性** —— 做多镜像和做空必须是同一份逻辑换方向，
   否则两个方向迟早不同步。
2. **回放不能自己编止盈** —— 一旦回放里藏了个止盈规则，跑出来的胜率
   就是我编的那条的胜率，而不是用户规则的胜率。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import replay as R   # noqa: E402

TICK = 0.1
EMA = 1000.0


def _bar(ts, o, h, l, c, upw=0.0, dnw=0.0, vol=1.0, ev=None, ema=EMA):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c,
            "up_wick_pct": upw, "dn_wick_pct": dnw, "close_pos": 0.5,
            "vol_rel": vol, "event": ev, "ema20": ema}


def _flat(ts, px=EMA, ema=EMA):
    """一根不碰 ema20 的平淡 K 线。"""
    return _bar(ts, px, px + 3, px - 3, px, upw=0.2, dnw=0.2, ema=ema)


def _pad(n, start=0, ema=EMA):
    return [_flat(start + i, ema=ema) for i in range(n)]


def _short_setup(precondition=True):
    """一个最小的做空信号：带量向下突破 → A 刺上 ema20 收在下 → B 再被拒。"""
    bars = []
    if precondition:
        bars.append(_bar(0, 1000, 1002, 990, 992, upw=0.05, dnw=0.5,
                         vol=5.0, ev="向下突破"))
    bars.append(_bar(1, 992, 1008, 991, 996, upw=0.75, dnw=0.05))   # A：刺上 1000 收 996
    bars.append(_bar(2, 996, 1002, 995, 997, upw=0.25, dnw=0.10))
    # B：又碰 1000 又被拒。上引线故意只到 0.45 —— 满足「带上引线」(wick_any)
    # 但不满足 A 的「很长的上引线」(wick_strong)，否则这根自己也会变成一个 A 信号。
    bars.append(_bar(3, 997, 1005, 996, 999, upw=0.45, dnw=0.05))
    # 进场：跌破 B 低点 996 − 1tick
    bars.append(_bar(4, 999, 999.5, 994, 995, upw=0.05, dnw=0.6))
    # 继续跌，时间出场
    for i in range(5, 14):
        bars.append(_bar(i, 995 - i, 996 - i, 993 - i, 994 - i,
                         upw=0.1, dnw=0.1))
    return bars


def _long_setup(precondition=True):
    """做空的最小信号**上下镜像**：把价格绕 EMA 折一下、h/l 互换、上下引线互换。"""
    out = []
    for b in _short_setup(precondition):
        out.append(_bar(b["ts"], 2 * EMA - b["o"], 2 * EMA - b["l"],
                       2 * EMA - b["h"], 2 * EMA - b["c"],
                       upw=b["dn_wick_pct"], dnw=b["up_wick_pct"],
                       vol=b["vol_rel"],
                       ev=("向上突破" if b["event"] == "向下突破"
                           else b["event"])))
    return out


def test_short_signal_is_found_and_settles():
    bars = _short_setup()
    sigs = R.find_signals(bars, R.Params(direction="short"))
    settled = [s for s in sigs if s["status"] == "settled"]
    assert len(settled) == 1, [s.get("status") for s in sigs]
    s = settled[0]
    assert s["entry"] == pytest.approx(996 - TICK)      # B 低点下方 1 tick
    assert s["stop"] > s["entry"]                        # 空单止损在上方
    assert s["gross_pct"] > 0                            # 一路跌，空单赚
    assert s["why"] == "time"


def test_long_mirror_gives_the_same_numbers():
    """镜像必须对称 —— 但要分清「哪种对称」。

    价格空间的量（进场价、止损价、出场价）绕 ema20 反射后必须**严格**对应；
    百分比收益**不可能**严格相等，因为收益率的分母是进场价，而反射改变了它
    （995.9 与 1004.1 不是一个基数）。所以这里断言价格空间严格对称 +
    收益率近似相等，而不是假装它们一样。
    """
    sh = [s for s in R.find_signals(_short_setup(), R.Params(direction="short"))
          if s["status"] == "settled"]
    lg = [s for s in R.find_signals(_long_setup(), R.Params(direction="long"))
          if s["status"] == "settled"]
    assert len(sh) == len(lg) == 1
    a, b = sh[0], lg[0]
    for key in ("entry", "stop", "exit"):
        assert a[key] + b[key] == pytest.approx(2 * EMA, abs=1e-9), \
            "%s 没有镜像对称" % key
    assert a["why"] == b["why"] == "time"
    assert a["hold"] == b["hold"]
    # 收益率近似相等（差在分母），相对差 < 2%
    assert a["gross_pct"] == pytest.approx(b["gross_pct"], rel=0.02)


def test_precondition_gates_the_signal():
    """没有「带量突破区间」前置，这个信号必须被拒掉。"""
    bars = _short_setup(precondition=False)
    on = R.find_signals(bars, R.Params(direction="short",
                                       require_precondition=True))
    off = R.find_signals(bars, R.Params(direction="short",
                                        require_precondition=False))
    assert on == [], "前置条件开着时不该有信号"
    assert len(off) == 1, "关上时应该有"


def test_volume_below_threshold_does_not_count_as_heavy():
    bars = _short_setup()
    bars[0]["vol_rel"] = 2.0          # 低于 vol_min=3.0
    assert R.find_signals(bars, R.Params(direction="short")) == []
    ok = R.Params(direction="short").replace(vol_min=2.0)
    assert len(R.find_signals(bars, ok)) == 1


def test_unfilled_stop_order_is_reported_not_silently_dropped():
    """挂单没被打到必须单独算「没成交」，不能当成亏或赚。"""
    bars = _short_setup()
    # 把 B 之后的几根抬高，让价格永远破不到 996−1tick
    for i in (4, 5, 6, 7):
        bars[i] = _bar(i, 999, 1004, 998, 1002, upw=0.2, dnw=0.1)
    sigs = R.find_signals(bars, R.Params(direction="short"))
    assert len(sigs) == 1
    assert sigs[0]["status"] == "no_fill"
    s = R.summarize(sigs)
    assert s["settled"] == 0 and s["triggered"] == 1
    assert s["fill_rate"] == 0.0


def test_stop_loss_uses_the_wider_of_the_two_previous_ranges():
    """止损基准 = 前两根里 h−l 较大的那根 × stop_frac（用户定的一档）。"""
    bars = _short_setup()
    bars[2] = _bar(2, 996, 1002, 995, 997, upw=0.25, dnw=0.10)   # h−l = 7
    bars[1] = _bar(1, 992, 1008, 991, 996, upw=0.75, dnw=0.05)   # h−l = 17 ← 较大
    sigs = [s for s in R.find_signals(bars, R.Params(direction="short"))
            if s["status"] == "settled"]
    s = sigs[0]
    entry = 996 - TICK
    assert s["stop"] - entry == pytest.approx(17 * 0.25, abs=1e-9)


def test_stop_frac_is_a_knob_and_halving_it_narrows_the_stop():
    bars = _short_setup()
    a = [s for s in R.find_signals(bars, R.Params(direction="short",
                                                  stop_frac=0.25))
         if s["status"] == "settled"][0]
    b = [s for s in R.find_signals(bars, R.Params(direction="short",
                                                  stop_frac=0.50))
         if s["status"] == "settled"][0]
    assert (b["stop"] - b["entry"]) == pytest.approx(2 * (a["stop"] - a["entry"]))


HOUR = R.HOUR_MS
#: 对齐到整点的时间基准（否则 ts=0,1,2… 全落进同一个 1h 桶）
BASE = 1_700_000_000_000 - (1_700_000_000_000 % HOUR)
M15 = 900_000


def _hour_fixture():
    """两个 1h 桶：上一小时振幅 110，当前小时振幅很小。

    索引 0-3 在桶 BASE，索引 4-7 在桶 BASE+1h。
    """
    bars = []
    for i in range(4):                       # 桶 BASE：低 900 高 1010
        lo = 900.0 if i == 0 else 995.0
        hi = 1010.0 if i == 3 else 998.0
        bars.append(_bar(BASE + i * M15, 995, hi, lo, 996, upw=0.2, dnw=0.2))
    for i in range(4):                       # 桶 BASE+1h
        bars.append(_bar(BASE + HOUR + i * M15, 996, 999, 994, 997,
                         upw=0.2, dnw=0.2))
    return bars


def test_hour_ranges_aggregate_15m_into_exact_hour_buckets():
    hb = R.hour_ranges(_hour_fixture())
    assert len(hb) == 2, hb
    assert hb[BASE] == [900.0, 1010.0]
    assert hb[BASE + HOUR] == [994.0, 999.0]


def _signal_in_second_hour():
    """A 在 BASE+75m，B 在 BASE+90m，成交根在 BASE+105m。

    进场那一刻当前 1h 桶（BASE+1h）还没走完，已完成的只有桶 BASE（振幅 110）。
    """
    bars = _hour_fixture()
    bars[5] = _bar(BASE + 75 * 60_000, 996, 1008, 995, 996, upw=0.75, dnw=0.05)  # A
    bars[6] = _bar(BASE + 90 * 60_000, 996, 1005, 995, 999, upw=0.45, dnw=0.05)  # B
    bars[7] = _bar(BASE + 105 * 60_000, 999, 999.5, 990, 991, upw=0.05, dnw=0.6)  # 成交
    return bars


def test_1h_stop_basis_uses_a_completed_hour_and_never_looks_ahead():
    """1h 基准必须只用**已完成**的 1h 桶。

    进场那一刻当前这根 1h 还没走完，用它的高/低等于偷看未来。
    这里当前桶振幅只有 5，已完成桶振幅 110 —— 若实现偷看了当前桶，
    止损距离会变成 1.25 而不是 27.5，一眼就能看出来。
    """
    bars = _signal_in_second_hour()
    p = R.Params(direction="short", stop_basis="1h", stop_bars=1,
                 stop_frac=0.25, require_precondition=False, max_wait=4)
    sigs = [s for s in R.find_signals(bars, p) if s["status"] == "settled"]
    assert sigs, [s.get("status") for s in R.find_signals(bars, p)]
    s = sigs[0]
    assert s["entry"] == pytest.approx(995 - 0.1)
    assert s["stop"] - s["entry"] == pytest.approx(110 * 0.25, abs=1e-9)


def test_1h_basis_gives_a_wider_stop_than_15m_on_the_same_signal():
    """用户的核心修正：15m 的 1/4 太小，1h 的 1/4 才接近"好几百点"。"""
    bars = _signal_in_second_hour()
    common = dict(direction="short", stop_frac=0.25,
                  require_precondition=False, max_wait=4)
    w15 = [s for s in R.find_signals(bars, R.Params(stop_basis="15m", **common))
           if s["status"] == "settled"][0]
    w1h = [s for s in R.find_signals(bars, R.Params(stop_basis="1h", **common))
           if s["status"] == "settled"][0]
    assert w1h["stop_pct"] > 5 * w15["stop_pct"], \
        "1h 基准应该给出明显更宽的止损（%.3f%% vs %.3f%%）" \
        % (w1h["stop_pct"], w15["stop_pct"])


def test_min_stop_pct_filters_noise_signals_out():
    """止损距离太小的信号要能筛掉，而不是硬算它的胜率。"""
    bars = _short_setup()
    loose = [s for s in R.find_signals(bars, R.Params(direction="short"))
             if s["status"] == "settled"]
    assert loose, "基准下应该有成交"
    tight = R.summarize(R.find_signals(
        bars, R.Params(direction="short", min_stop_pct=99.0)))
    assert tight["settled"] == 0
    assert tight["filtered"] >= 1


def test_summary_reports_the_stop_distance_itself():
    """止损距离的中位数必须报出来 —— 它是判断"抓到的是不是那个形态"的第一手证据。"""
    s = R.summarize(R.find_signals(_short_setup(), R.Params(direction="short")))
    assert s["median_stop_pct"] > 0
    assert s["avg_stop_pct"] > 0


def test_replay_reports_mfe_and_mae_but_invents_no_take_profit():
    """回放不许自己编止盈：只报 MFE/MAE，让「止盈该放哪」由数据说。

    如果哪天有人往 find_signals 里塞了一个「到 1R 就平」的规则，
    回放出来的胜率就变成那条规则的胜率了 —— 这条测试专门拦这个。
    """
    import inspect
    src = inspect.getsource(R.find_signals)
    for banned in ("take_profit", "tp_px", "target_px", "1R", "1.5R"):
        assert banned not in src, "回放器里出现了止盈逻辑：%s" % banned
    s = [x for x in R.find_signals(_short_setup(), R.Params(direction="short"))
         if x["status"] == "settled"][0]
    assert "mfe_pct" in s and "mae_pct" in s


def test_entry_bar_does_not_judge_the_stop_by_default():
    """成交那一根默认不判止损 —— 否则构造性 100% 止损。

    15m OHLC 没有先后顺序：挂单是盘中跌破才成交的，而这根的 high 很可能
    出现在成交之前。照 high 判止损 = 假设"成交瞬间已在最高点"，止损率必然 100%。
    第一版就是这么错的（止损率 100%、MFE 恒 0.00%）。
    """
    bars = _short_setup()
    # 把成交那根（index 4）做成一根大阳：high 远高于止损，但 low 也破了挂单价
    bars[4] = _bar(4, 999, 1010, 994, 1009, upw=0.05, dnw=0.10)
    sigs = [s for s in R.find_signals(bars, R.Params(direction="short"))
            if s["status"] == "settled"]
    assert sigs[0]["why"] != "stop", "成交那根不该触发止损"
    # 悲观端（允许同一根内被扫）应该会止损 —— 两者之差就是建模不确定性
    pess = [s for s in R.find_signals(
        bars, R.Params(direction="short", same_bar_stop=True))
        if s["status"] == "settled"]
    assert pess[0]["why"] == "stop"


def test_flat_market_yields_nothing():
    assert R.find_signals(_pad(200), R.Params()) == []


def test_missing_ema_bars_are_skipped_not_crashed():
    bars = _short_setup()
    for b in bars:
        b["ema20"] = None
    assert R.find_signals(bars, R.Params(direction="short")) == []


def test_summarize_of_empty_is_all_zero_not_a_crash():
    s = R.summarize([])
    assert s["settled"] == 0 and s["win_rate"] == 0.0


def test_sweep_is_single_variable_and_returns_base_row():
    rows = R.sweep(_short_setup(), R.Params(direction="short",
                                            require_precondition=False),
                   {"stop_frac": (0.25, 0.50)})
    assert any(r["param"] == "(基准)" for r in rows)
    assert {r["value"] for r in rows if r["param"] == "stop_frac"} == {0.25, 0.5}
    assert rows == sorted(rows, key=lambda r: r["avg_net_pct"], reverse=True)
