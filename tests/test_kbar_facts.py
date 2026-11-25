# -*- coding: utf-8 -*-
"""第 2 步（事实层）测试。

每个用例都对应一条口径或一个踩过的坑，注释里写明「错的话会怎样」。
特别注意三个回归点，它们都是实跑时先出问题、才补成测试的：
  1. 同价重合的线必须合并，否则区间名退化（曾出现全部 "at_S"）；
  2. 3s 缺桶但量自洽时 conc 仍然精确，按桶数一刀切会误判成算不出；
  3. 重跑第 2 步不能把第 3/4 步填的字段抹成 NULL。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar import build as B      # noqa: E402
from ai_trader.kbar import facts as F      # noqa: E402
from ai_trader.kbar.store import KBarStore  # noqa: E402

IV = 900_000
T0 = 1_793_000_000_000 // IV * IV          # 对齐到 15m 的整点


def bar(ts, o, h, l, c, v=None, buy=None, symbol="BTCUSDT"):
    return {"symbol": symbol, "ts": ts, "o": o, "h": h, "l": l, "c": c,
            "v": v, "buy_vol": buy}


def rng_row(ts, direction="DOWN", soft_up=None, soft_dn=None, hard_up=None,
            hard_dn=None, key=None, family=1, segment=7):
    return {"ts": ts, "direction": direction, "soft_up": soft_up, "soft_dn": soft_dn,
            "hard_up": hard_up, "hard_dn": hard_dn, "key": key,
            "family": family, "segment": segment}


def ticks(n, base_ts, vol=1.0, prices=None):
    out = []
    for i in range(n):
        p = (prices or {}).get(i, 100.0)
        out.append({"ts": base_ts + i * F.TICK_INTERVAL_MS,
                    "o": p, "h": p, "l": p, "c": p, "v": vol})
    return out


# ================================================================ 结构
def test_structure_formulas():
    """body/影线/收盘位置的手算口径。错的话所有结构统计都没有意义。"""
    s = F.structure(100.0, 110.0, 90.0, 106.0)
    assert s["dir"] == 1
    assert s["body_pct"] == pytest.approx(6.0 / 20.0)
    assert s["up_wick_pct"] == pytest.approx(4.0 / 20.0)
    assert s["dn_wick_pct"] == pytest.approx(10.0 / 20.0)
    assert s["close_pos"] == pytest.approx(16.0 / 20.0)
    assert s["body_pct"] + s["up_wick_pct"] + s["dn_wick_pct"] == pytest.approx(1.0)


def test_structure_dir_sign_and_tie():
    assert F.structure(100, 101, 99, 99)["dir"] == -1
    assert F.structure(100, 101, 99, 100)["dir"] == 0


def test_structure_flat_bar_refused():
    """h==l 时分母是 0：必须拒算并给原因，而不是抛异常或算成 inf。"""
    assert F.structure(100, 100, 100, 100)["bad"] == "flat_bar"
    assert F.structure(None, 1, 2, 3)["bad"] == "null_ohlc"


# ================================================================ 量能
def test_vol_rel_excludes_current_bar():
    """分母必须是**前 N 根**，不含当根。

    构造上要让「含不含当根」真的改变中位数 —— 否则测试是假绿的：
    第一版用 19 个 1.0 + 1 个 100.0，含不含当根中位数都是 1.0，变异体抓不住。
    """
    # 小窗口精确定位：前 4 根 [1,1,100,100] 中位 50.5；把当根 100 算进去中位变 100
    assert F.vol_rel([1.0, 1.0, 100.0, 100.0, 100.0], 4, n=4) == pytest.approx(100.0 / 50.5)
    # 默认窗口（20）：前 20 根 10 低 10 高，中位 50.5；含当根后被高值占据，中位变 100
    vols = [1.0] * 10 + [100.0] * 10 + [100.0]
    assert F.vol_rel(vols, 20) == pytest.approx(100.0 / 50.5)


def test_vol_rel_needs_full_window():
    assert F.vol_rel([2.0] * (F.VOL_REL_N - 1) + [3.0], F.VOL_REL_N - 1) is None


def test_vol_rel_refuses_window_with_missing_volume():
    """窗口里有未知量时必须拒算：用残缺窗口算中位会让比值虚高，像假放量。"""
    vols = [None] + [1.0] * (F.VOL_REL_N - 1) + [5.0]
    assert F.vol_rel(vols, len(vols) - 1) is None


def test_vol_slot_rel_uses_only_that_slot_and_needs_samples():
    """同槽归一：别的槽的历史不能混进来，样本不够也不给。"""
    hist = {3: [10.0] * F.VOL_SLOT_MIN_SAMPLES, 4: [1000.0] * 50}
    assert F.vol_slot_rel(20.0, 3, hist) == pytest.approx(2.0)
    assert F.vol_slot_rel(20.0, 5, hist) is None          # 该槽没历史
    assert F.vol_slot_rel(20.0, 3, {3: [10.0] * 3}) is None   # 样本不足
    assert F.vol_slot_rel(None, 3, hist) is None              # 当根无量


def test_slot_of_maps_to_day_slot():
    assert F.slot_of(T0, IV) == (T0 // IV) % 96
    assert F.slot_of(T0 + IV, IV) == (T0 // IV + 1) % 96


def test_bar_facts_volume_none_is_not_zero():
    """没有量的根：vol 必须是 None，且**不能**以 0 进入同槽历史（会毒化中位数）。"""
    bars = [bar(T0 + i * IV, 100, 101, 99, 100, v=(None if i < 30 else 5.0))
            for i in range(40)]
    rows, stats = F.bar_facts(bars, IV, symbol="BTCUSDT")
    assert rows[0]["vol"] is None
    assert rows[0]["vol_rel"] is None
    # 前 30 根无量 -> 后 10 根的槽历史里没有 0
    slot = F.slot_of(T0 + 35 * IV, IV)
    hist_vals = [r["vol"] for r in rows if F.slot_of(r["ts"], IV) == slot
                 and r["vol"] is not None]
    assert all(v > 0 for v in hist_vals)
    assert stats["skipped"]["vol:no_source"] == 30


def test_taker_buy_ratio_is_buy_over_vol():
    """taker_buy_ratio = 主动买量 / 总量。用错列（例如卖出量）会算出 1-它。"""
    bars = [bar(T0 + i * IV, 100, 101, 99, 100, v=10.0, buy=3.0) for i in range(3)]
    rows, _ = F.bar_facts(bars, IV, symbol="BTCUSDT")
    assert rows[0]["taker_buy_ratio"] == pytest.approx(0.3)


def test_taker_buy_ratio_missing_buy_counter():
    bars = [bar(T0, 100, 101, 99, 100, v=10.0, buy=None)]
    _, stats = F.bar_facts(bars, IV, symbol="BTCUSDT")
    assert stats["skipped"]["taker_buy_ratio:no_buy_vol"] == 1


# ================================================================ 内路径
def test_path_conc_uses_bar_total_as_denominator():
    tk = ticks(F.TICKS_PER_BAR, T0, vol=1.0)     # 300 根 3s = 一根 15m
    tk[7]["v"] = 6.0                             # 299*1 + 6 = 305，其中最大桶 6
    pf = F.path_facts(tk, T0, 305.0, IV)
    assert pf["conc"] == pytest.approx(6.0 / 305.0)
    assert pf["n_bad"] is None


def test_path_conc_allows_missing_buckets_when_volume_consistent():
    """缺桶但 sum(3s)==K线量：分子分母出自同一组桶，conc 仍精确。

    这是实跑发现的：641 根里有 156 根只有 297~299 个桶，却与 K 线量完全相等。
    若按「桶数必须 300」一刀切，这些根会被误判成算不出（曾少算 83 根）。
    """
    tk = ticks(F.TICKS_PER_BAR - 2, T0, vol=1.0)   # 298 根
    tk[3]["v"] = 10.0                              # 297*1 + 10 = 307
    pf = F.path_facts(tk, T0, 307.0, IV)
    assert pf["conc"] == pytest.approx(10.0 / 307.0)
    assert pf["n_bad"] == "ticks_incomplete_but_consistent"


def test_path_conc_refuses_volume_mismatch():
    """3s 不是这根 K 的完整分解时比值会被压低，必须拒算。"""
    tk = ticks(F.TICKS_PER_BAR, T0, vol=1.0)
    assert F.path_facts(tk, T0, 99.0, IV)["conc"] is None
    assert F.path_facts(tk, T0, 99.0, IV)["n_bad"] == "ticks_vol_mismatch"


def test_path_refuses_without_bar_volume():
    tk = ticks(F.TICKS_PER_BAR, T0, vol=1.0)
    pf = F.path_facts(tk, T0, None, IV)
    assert pf["conc"] is None and pf["n_bad"] == "bar_vol_missing"


def test_path_extreme_minutes():
    """t_low_min / t_high_min = 极值出现在这根的第几分钟。"""
    tk = ticks(F.TICKS_PER_BAR, T0, vol=1.0)
    tk[4]["l"] = 50.0
    tk[11]["h"] = 150.0
    pf = F.path_facts(tk, T0, float(F.TICKS_PER_BAR), IV)
    assert pf["t_low_min"] == 4 * F.TICK_INTERVAL_MS // 60_000
    assert pf["t_high_min"] == 11 * F.TICK_INTERVAL_MS // 60_000


def test_path_no_ticks():
    pf = F.path_facts([], T0, 1.0, IV)
    assert pf["n_bad"] == "no_ticks" and pf["conc"] is None


# ================================================================ 区间位置 zone
def test_zone_role_mapping_down():
    """DOWN 段：S=soft_up, R=hard_up, P=soft_dn, K=key（实测 hard_dn 恒空）。"""
    row = rng_row(T0, "DOWN", soft_up=79646.4, soft_dn=75670.0,
                  hard_up=79856.1, hard_dn=None, key=75158.0)
    roles = F.levels_by_role(row)
    assert roles == {"S": 79646.4, "R": 79856.1, "P": 75670.0, "K": 75158.0}
    assert F.zone_of(77000.0, roles) == "P-S"       # K < P < 77000 < S < R
    assert F.zone_of(74000.0, roles) == "below_K"
    assert F.zone_of(81000.0, roles) == "above_R"


def test_zone_role_mapping_up():
    """UP 段：S=soft_dn, R=hard_dn, P=soft_up, K=key（实测 hard_up 恒空）。"""
    row = rng_row(T0, "UP", soft_up=80640.2, soft_dn=80387.1,
                  hard_up=None, hard_dn=80188.7, key=81188.0)
    roles = F.levels_by_role(row)
    assert roles == {"S": 80387.1, "R": 80188.7, "P": 80640.2, "K": 81188.0}
    assert F.zone_of(80500.0, roles) == "S-P"
    assert F.zone_of(80000.0, roles) == "below_R"
    assert F.zone_of(82000.0, roles) == "above_K"


def test_zone_pre_direction_has_no_roles():
    """PRE 四线全空：不能硬凑一个区间。"""
    assert F.levels_by_role(rng_row(T0, "PRE")) == {}
    assert F.zone_of(1.0, {}) is None


def test_zone_merges_equal_price_lines():
    """同价重合必须合并成一个节点（标签连写），否则会切出零宽度区间。

    实跑时 DOWN 段有 186 行 hard_up == soft_up，若不合并，落库的区间名
    会把一个点当成两条线（曾出现 "P-R" 而 S 其实就压在 R 上）。
    """
    row = rng_row(T0, "DOWN", soft_up=100.0, hard_up=100.0, soft_dn=95.0, key=90.0)
    roles = F.levels_by_role(row)
    assert F.zone_of(97.5, roles) == "P-SR"      # 不是 "P-R"
    assert F.zone_of(100.0, roles) == "P-SR"


def test_zone_partial_lines_named_among_present_only():
    """段首线没画全时，只在有值的线之间命名，不假装缺席的线在那儿。"""
    row = rng_row(T0, "DOWN", soft_up=None, hard_up=None, soft_dn=95.0, key=90.0)
    roles = F.levels_by_role(row)
    assert F.zone_of(92.0, roles) == "K-P"
    assert F.zone_of(99.0, roles) == "above_P"


def test_bar_facts_counts_zone_no_levels():
    """区间行四线全空（PRE 未成形）时必须计数，否则「没填 zone」会从报表消失。"""
    rr = [rng_row(T0 - 3600_000, "PRE")]                 # 全空
    bars_in = [bar(T0, 100, 101, 99, 100, v=1.0)]        # 收盘 T0+15m，用 T0-1h 行
    rows, stats = F.bar_facts(bars_in, IV, range_rows=rr, symbol="BTCUSDT")
    assert rows[0]["zone"] is None
    assert rows[0]["direction"] == "PRE"
    assert stats["skipped"]["zone:no_levels"] == 1


def test_bar_facts_counts_zone_partial_lines():
    """段首线没画全：区间名只在有值的线之间取，状态必须计数。"""
    rr = [rng_row(T0 - 3600_000, "DOWN", soft_up=None, hard_up=None,
                  soft_dn=95.0, key=90.0)]
    bars_in = [bar(T0, 100, 101, 99, 92.0, v=1.0)]
    rows, stats = F.bar_facts(bars_in, IV, range_rows=rr, symbol="BTCUSDT")
    assert rows[0]["zone"] == "K-P"
    assert stats["skipped"]["zone:partial_lines"] == 1


# ================================================================ 组装 / 防偷未来
def test_range_row_uses_only_closed_hour():
    """本根只能看到**已收盘**的 1h 行，否则就是偷未来。

    边界：1h 行恰好在 15m 根收盘那一刻收盘，算「已知」（<= 而不是 <），
    这与第 1 步 line_ready_ms 的边界口径一致。
    """
    rr = [rng_row(T0, "DOWN", soft_up=200, hard_up=210, soft_dn=190, key=180)]
    # 早一根：收盘 T0+30min，T0 那根 1h 还没收盘 -> 看不到
    early, _ = F.bar_facts([bar(T0 + IV, 100, 101, 99, 100, v=1.0)], IV,
                           range_rows=rr, symbol="BTCUSDT")
    assert early[0]["range_ts_h"] is None
    # 边界那根：收盘 T0+60min == 1h 行收盘 -> 看得到
    edge, _ = F.bar_facts([bar(T0 + 3 * IV, 100, 101, 99, 100, v=1.0)], IV,
                          range_rows=rr, symbol="BTCUSDT")
    assert edge[0]["range_ts_h"] == T0
    assert T0 + 3600_000 <= edge[0]["ts"] + IV


def test_bar_facts_counts_range_not_closed():
    bars_in = [bar(T0 + i * IV, 100, 101, 99, 100, v=1.0) for i in range(3)]
    _, stats = F.bar_facts(bars_in, IV, range_rows=None, symbol="BTCUSDT")
    assert stats["skipped"]["range:not_closed"] == 3


def test_seg_idx_counts_within_segment():
    """seg_idx = 该 1h 行在自己的 (family, segment) 段内的序号（从 0 起）。"""
    rr = [rng_row(T0, family=1, segment=7),
          rng_row(T0 + 3600_000, family=1, segment=7),
          rng_row(T0 + 7200_000, family=2, segment=7)]
    bars_in = [bar(T0 + 3 * IV, 100, 101, 99, 100, v=1.0)]
    rows, _ = F.bar_facts(bars_in, IV, range_rows=rr, symbol="BTCUSDT")
    # 该根能看到 T0+1h（收盘 T0+2h）与 T0+2h（收盘 T0+3h）-> 最新是 T0+2h，新段 index 0
    assert rows[0]["seg_idx"] == 0


def test_n_lines_from_line_counts():
    bars_in = [bar(T0, 100, 101, 99, 100, v=1.0), bar(T0 + IV, 100, 101, 99, 100, v=1.0)]
    rows, _ = F.bar_facts(bars_in, IV, line_counts={T0: 7}, symbol="BTCUSDT")
    assert rows[0]["n_lines"] == 7
    assert rows[1]["n_lines"] is None


def test_late_fields_left_null_in_step2():
    """第 3/4 步的字段这一步必须留空（不是漏，是顺序）。"""
    rows, _ = F.bar_facts([bar(T0, 100, 101, 99, 100, v=1.0)], IV, symbol="BTCUSDT")
    for col in ("n_touch", "n_break", "event", "prev_event", "on_line",
                "since_event_idx", "state", "path_shape", "max_tick_share"):
        assert rows[0][col] is None, col


# ================================================================ 多源合并
def test_merge_bars_priority_and_conflict_record():
    """冲突不静默选边：保留高优先级值，同时**逐条记录**。"""
    a = [{"ts": T0, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0, "buy_vol": 4.0}]
    b = [{"ts": T0, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.7, "v": 11.0, "buy_vol": 5.0}]
    bars, rep = B.merge_bars(a, b)
    assert bars[0]["c"] == 1.5                       # 高优先级胜出
    assert len(rep["conflicts"]) == 1
    assert set(rep["conflicts"][0]["diff"]) == {"c", "v"}
    assert rep["hits"] == [1, 0]


def test_merge_bars_no_conflict_when_other_source_lacks_volume():
    """CSV 那种只有结构的源不算冲突：None 不是 0。"""
    a = [{"ts": T0, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0, "buy_vol": 4.0}]
    c = [{"ts": T0, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": None, "buy_vol": None}]
    bars, rep = B.merge_bars(a, c)
    assert rep["conflicts"] == []
    assert bars[0]["v"] == 10.0


def test_merge_bars_fills_gaps_from_lower_priority():
    a = [{"ts": T0 + IV, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1.0, "buy_vol": 0.5}]
    c = [{"ts": T0, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": None, "buy_vol": None}]
    bars, rep = B.merge_bars(a, c)
    assert [b["ts"] for b in bars] == [T0, T0 + IV]
    assert rep["with_vol"] == 1 and rep["with_buy"] == 1


def test_merge_does_not_conflict_on_relative_noise():
    a = [{"ts": T0, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5000000001, "v": 1.0,
          "buy_vol": 0.5}]
    b = [{"ts": T0, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1.0, "buy_vol": 0.5}]
    _, rep = B.merge_bars(a, b)
    assert rep["conflicts"] == []


# ================================================================ 取数（真库/临时库）
def test_load_1m_as_15m_aggregates_correctly(tmp_path):
    p = str(tmp_path / "bt.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE klines (interval TEXT, open_time INTEGER, o REAL, h REAL,"
                " l REAL, c REAL, v REAL, q REAL, n INTEGER, b REAL, s REAL)")
    rows = []
    for i in range(15):                       # 一根完整 15m
        rows.append(("1m", T0 + i * 60_000, 100 + i, 110 + i, 90 + i, 100 + i,
                     1.0, 0.0, 1, 0.4, 999.0))
    for i in range(5):                        # 一根只有 5 根 1m 的不完整桶
        rows.append(("1m", T0 + IV + i * 60_000, 100, 120, 80, 100, 2.0, 0.0, 1, 0.5, 999.0))
    con.executemany("INSERT INTO klines VALUES (" + ",".join("?" * 11) + ")", rows)
    con.commit()
    con.close()

    out = B.load_1m_as_15m(p, IV)
    assert len(out) == 2
    assert out[0]["o"] == 100 and out[0]["c"] == 114
    assert out[0]["h"] == 124 and out[0]["l"] == 90
    assert out[0]["v"] == pytest.approx(15.0)
    assert out[0]["buy_vol"] == pytest.approx(6.0)
    assert out[0]["incomplete"] is False
    assert out[1]["incomplete"] is True        # 只有 5/15 根
    assert out[1]["v"] == pytest.approx(10.0)


def test_load_1m_ignores_sell_quote_column(tmp_path):
    """backtest.db 的 s 是主动买**报价额**，不是卖出量：绝对不能当卖量用。

    错的话 taker_buy_ratio 会变成天文数字或负数。
    """
    p = str(tmp_path / "bt.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE klines (interval TEXT, open_time INTEGER, o REAL, h REAL,"
                " l REAL, c REAL, v REAL, q REAL, n INTEGER, b REAL, s REAL)")
    con.execute("INSERT INTO klines VALUES ('1m',?,1,1,1,1,11.895,923769,1,11.264,874766.36)",
                (T0,))
    con.commit()
    con.close()
    out = B.load_1m_as_15m(p, IV)
    assert out[0]["buy_vol"] == pytest.approx(11.264)
    assert out[0]["v"] == pytest.approx(11.895)
    assert "sell_vol" not in out[0]            # 不产出卖量，杜绝误用


def test_market_db_buy_sell_are_volumes(tmp_path):
    """market.db 的 b/s 才是买量/卖量（b+s==v 实测精确成立）。"""
    p = str(tmp_path / "m.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE bars (symbol TEXT, interval_ms INTEGER, open_time INTEGER,"
                " o REAL, h REAL, l REAL, c REAL, v REAL, q REAL, n INTEGER, b REAL,"
                " s REAL, updated_at TEXT)")
    con.execute("INSERT INTO bars (symbol,interval_ms,open_time,o,h,l,c,v,b,s)"
                " VALUES ('BTCUSDT',900000,?,1,2,0.5,1.5,10.0,4.0,6.0)", (T0,))
    con.commit()
    con.close()
    out = B.load_market_bars_rich(p, "BTCUSDT", IV)
    assert out[0]["buy_vol"] == 4.0 and out[0]["sell_vol"] == 6.0
    assert out[0]["buy_vol"] + out[0]["sell_vol"] == pytest.approx(out[0]["v"])


def test_load_csv_bars_has_no_volume(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("ts,o,h,l,c\n%d,1.0,2.0,0.5,1.5\n" % T0, encoding="utf-8")
    out = B.load_csv_bars(str(p))
    assert out[0]["v"] is None and out[0]["buy_vol"] is None


def test_ticks_cover_aligns_to_buckets(tmp_path):
    """覆盖窗必须对齐到 K 线边界：半根会让 conc 的分母悄悄错掉。"""
    p = str(tmp_path / "m.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE bars (symbol TEXT, interval_ms INTEGER, open_time INTEGER,"
                " o REAL, h REAL, l REAL, c REAL, v REAL)")
    for ts in (T0 + 5000, T0 + IV + 8000):
        con.execute("INSERT INTO bars VALUES ('BTCUSDT',3000,?,1,1,1,1,1.0)", (ts,))
    con.commit()
    con.close()
    lo, hi = B.ticks_cover(p, "BTCUSDT", IV)
    assert lo == T0 and hi == T0 + 2 * IV


# ================================================================ 落库
def test_upsert_evolution_is_idempotent(tmp_path):
    p = str(tmp_path / "k.db")
    rows, _ = F.bar_facts([bar(T0, 100, 101, 99, 100, v=1.0, buy=0.5)], IV,
                          symbol="BTCUSDT")
    with KBarStore(p) as st:
        st.upsert_evolution(rows)
        st.upsert_evolution(rows)
        assert st.evolution_counts("BTCUSDT", IV)["bars"] == 1


def test_upsert_evolution_does_not_wipe_late_fields(tmp_path):
    """重跑第 2 步绝不能把第 3/4 步填的字段抹掉（踩过的坑）。

    错的话回填一次就把反应层/事件层清空，而且不报错，只是数据变空。
    """
    p = str(tmp_path / "k.db")
    rows, _ = F.bar_facts([bar(T0, 100, 101, 99, 100, v=1.0, buy=0.5)], IV,
                          symbol="BTCUSDT")
    with KBarStore(p) as st:
        st.upsert_evolution(rows)
        st._con.execute("UPDATE kbar_evolution SET n_touch = 3, n_break = 1,"
                        " event = 'touch', state = 'waiting' WHERE ts = ?", (T0,))
        st._con.commit()
        st.upsert_evolution(rows)              # 重跑第 2 步
        r = st.evolution_rows("BTCUSDT", IV)[0]
        assert r["n_touch"] == 3 and r["n_break"] == 1
        assert r["event"] == "touch" and r["state"] == "waiting"


def test_evolution_counts_reports_fill_rates(tmp_path):
    p = str(tmp_path / "k.db")
    rows, _ = F.bar_facts(
        [bar(T0, 100, 101, 99, 100, v=1.0, buy=0.5),
         bar(T0 + IV, 100, 101, 99, 100, v=None)], IV, symbol="BTCUSDT",
        line_counts={T0: 5})
    with KBarStore(p) as st:
        st.upsert_evolution(rows)
        c = st.evolution_counts("BTCUSDT", IV)
    assert c["bars"] == 2
    assert c["filled"]["vol_rel"] == 0          # 只有 2 根，凑不满 20 根窗口
    assert c["filled"]["taker_buy_ratio"] == 1  # 第一根有，第二根无量
    assert c["filled"]["n_lines"] == 1
    assert c["filled"]["conc"] == 0
    assert c["filled"]["event"] == 0


def test_delete_evolution_only_that_interval(tmp_path):
    p = str(tmp_path / "k.db")
    rows, _ = F.bar_facts([bar(T0, 100, 101, 99, 100, v=1.0)], IV, symbol="BTCUSDT")
    with KBarStore(p) as st:
        st.upsert_evolution(rows)
        assert st.delete_evolution("BTCUSDT", 60_000) == 0
        assert st.delete_evolution("BTCUSDT", IV) == 1
        assert st.evolution_counts("BTCUSDT", IV)["bars"] == 0


def test_evolution_table_has_all_design_columns(tmp_path):
    """表 1 的列名必须与设计稿一致，少一列就是少了一个事实。"""
    p = str(tmp_path / "k.db")
    KBarStore(p).close()
    con = sqlite3.connect(p)
    cols = {r[1] for r in con.execute("PRAGMA table_info(kbar_evolution)")}
    con.close()
    for c in ("symbol", "interval_ms", "ts", "idx", "range_ts_h", "fam", "seg",
              "seg_idx", "direction", "zone", "o", "h", "l", "c", "dir", "body_pct",
              "up_wick_pct", "dn_wick_pct", "close_pos", "vol", "vol_rel",
              "vol_slot_rel", "taker_buy_ratio", "conc", "path_shape",
              "max_tick_share", "t_low_min", "t_high_min", "n_lines", "n_touch",
              "n_break", "event", "prev_event", "on_line", "since_event_idx", "state"):
        assert c in cols, "缺列 %s" % c


def test_idx_is_absolute_bar_index():
    """idx 用 ts//interval（绝对根号），不随本次加载窗口变化。"""
    a, _ = F.bar_facts([bar(T0, 100, 101, 99, 100, v=1.0)], IV, symbol="BTCUSDT")
    b, _ = F.bar_facts([bar(T0 + 5000 * IV, 100, 101, 99, 100, v=1.0)], IV,
                       symbol="BTCUSDT")
    assert a[0]["idx"] == T0 // IV
    assert b[0]["idx"] == T0 // IV + 5000


def test_facts_params_snapshot_declares_undefined_fields():
    """口径参数必须把「设计上没算的」写进库，否则以后没人知道为什么是空的。"""
    p = B.facts_params_snapshot(IV)
    assert set(p["undefined_by_design"]) == {"path_shape", "max_tick_share"}
    assert "n_touch" in p["later_steps"] and "event" in p["later_steps"]
    assert p["vol_rel_n"] == F.VOL_REL_N
    assert p["slots_per_day"] == 96


def test_writes_refused_to_source_db(tmp_path):
    """落库目标不能是任何一个只读源（写错就污染生产库）。"""
    src = str(tmp_path / "market.db")
    with pytest.raises(ValueError):
        B.build_and_persist_facts(src, "BTCUSDT", IV, [], sources=[src])
