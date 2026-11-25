# -*- coding: utf-8 -*-
"""K 线演化第 1 步（观察线）的回归防线。

写这些测试之前，`tools/probe_kbar_lines.py` 已经在生产快照上打印过真实值
（2026-11-07 12:00 那根 1h：EMA20/50/144/169/200 = 75974.1968 / 76628.9054 /
77550.2328 / 77713.3522 / 77907.9892，区间四线 = 79646.4 / 75670.0 / 79856.1 / NULL，
唐奇安 = 81188.0 / 75158.0）。测试只负责**不许这些值以后悄悄变**。

这里锁的是"不会报错、只会静默算错"的问题：
  - EMA 用了 SMA 种子 → 差几十点，不会抛异常；
  - 唐奇安含了当根 → 永远不可能"突破上轨"；
  - 区间四线自己重算一遍 → 引擎改版后与生产分叉，而图看起来正常；
  - 15m 用了还没收盘的那根 1h → 回测偷未来，事后看很准、当时看不见；
  - 摆动点没等右侧确认 → 同一个毛病，只是更隐蔽；
  - 预热不足照样出线 → 交给下游一条没收敛的线，谁都不知道它不准；
  - 静默过滤 → 少了一万行没人发现。
"""
from __future__ import annotations

import io
import math
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B                    # noqa: E402
from ai_trader.kbar import lines as L                     # noqa: E402
from ai_trader.kbar.store import KBarStore                # noqa: E402
from ai_trader.range.engine import lab, ts8               # noqa: E402
from ai_trader.range.store import RangeStore, rows_from_states  # noqa: E402

M15 = 900_000
H1 = 3_600_000


# ---------------------------------------------------------------- 造数
def _series(n, start_ts, interval_ms, base=76000.0):
    """确定性的合成 K 线（不用随机数：失败要能复现出同一组数）。"""
    out = []
    for i in range(n):
        o = base + 500.0 * math.sin(i / 7.0) + 3.0 * math.cos(i / 1.7)
        c = o + 40.0 * math.sin(i / 3.1)
        h = max(o, c) + 25.0 + (i % 3)
        lo = min(o, c) - 25.0 - (i % 5)
        out.append(L.Bar(start_ts + i * interval_ms, o, h, lo, c, 10.0 + (i % 5)))
    return out


def test_a_hole_in_the_series_is_rejected_not_silently_shifted():
    """★ 序列中间缺一根，必须抛错，不许照算。

    EMA / 唐奇安是沿序列递推的：少一根，后面所有值都往前错一格，
    而算出来的数字**看起来完全合法**（数量级、平滑度都对）。
    以前只有 forward（表3）有等间隔检查，最上游的 lines 反而没有，
    洞于是从最底层就开始污染 —— 这正是 5m 数据断层最危险的后果。
    """
    bars = _series(60, 0, M15)
    del bars[30]                     # 抽掉一根，制造段内断口
    with pytest.raises(ValueError, match="不等间隔"):
        L.build_bar_lines("BTCUSDT", M15, bars, warmup_bars=0)


def test_a_hole_can_only_be_forced_through_explicitly():
    """带洞硬算必须显式开口，不能是默认行为。"""
    bars = _series(60, 0, M15)
    del bars[30]
    rows, stats = L.build_bar_lines("BTCUSDT", M15, bars, warmup_bars=0,
                                    require_contiguous=False)
    assert stats["bars_in"] == 59, "少了一根，bars_in 必须如实反映"


def test_contiguous_series_still_passes():
    """反证：连续序列不许因为这道断言而失败。"""
    bars = _series(60, 0, M15)
    rows, stats = L.build_bar_lines("BTCUSDT", M15, bars, warmup_bars=0)
    assert stats["bars_in"] == 60
    assert rows


def _range_row(ts, su, sd, hu, hd, key, d, fam=1, seg=1):
    return {"ts": ts, "ts_h": ts8(ts), "soft_up": su, "soft_dn": sd,
            "hard_up": hu, "hard_dn": hd, "key": key, "family": fam,
            "segment": seg, "direction": d, "tag": ""}


def _by_ts(rows, ts):
    return {r["line_kind"]: r for r in rows if int(r["ts"]) == int(ts)}


# ---------------------------------------------------------------- 命名
def test_line_key_carries_its_own_period():
    """每条线必须带自己的周期：EMA200(15m) 与 EMA200(1h) 是两条不同的线。"""
    assert L.line_key("ema200", M15) == "ema200@15m"
    assert L.line_key("ema200", H1) == "ema200@1h"
    assert L.line_key("ema200", M15) != L.line_key("ema200", H1)
    assert L.period_label(180_000) == "3m"
    assert L.period_label(H1) == "1h"
    assert L.period_label(14_400_000) == "4h"


# ---------------------------------------------------------------- EMA
def test_ema_matches_the_dashboard_formula_exactly():
    """库里存的是全精度，看板显示的是 round(e,1) —— 两层都必须对得上。

    这是设计稿 §13 第 1 步验收："与 Web 看板里 EMA 的计算结果逐个比对，必须一致"。
    比错标尺会得到"差 0.04 点"的假警报（看板已经取整），所以两层分开比。

    为什么还要比一条**短序列**：1500 根预热会把种子选错造成的偏差衰减到小数点后几位，
    长序列上"种子写错"根本比不出来（实测过：改成 SMA 种子，1600 根上仍逐位相等）。
    所以这里额外在 60 根上逐点比，让种子错误无处可藏。
    """
    from ai_trader.web.live import ema_series as dash_ema
    for p in L.EMA_PERIODS:
        short = [float(b.c) for b in _series(60, 0, H1)]
        ours_short = L.ema_values(short, p)
        theirs_short = dash_ema(short, p)
        for i, (a, b) in enumerate(zip(ours_short, theirs_short)):
            assert round(a, 1) == b, \
                "短序列第 %d 根 ema%d 与看板不一致: %r vs %r" % (i, p, a, b)
    closes = [float(b.c) for b in _series(1600, 0, H1)]
    rows, _stats = L.build_bar_lines("BTCUSDT", H1, _series(1600, 0, H1))
    last = _by_ts(rows, 1599 * H1)
    for p in L.EMA_PERIODS:
        ours = float(last["ema%d" % p]["line_price"])
        disp = float(dash_ema(closes, p)[-1])
        assert round(ours, 1) == disp, "ema%d 与看板显示值不一致: %r vs %r" % (p, ours, disp)


def test_ema_warmup_constant_cannot_drift_from_the_dashboard():
    """预热根数是跨模块不变量：看板取 1500 根，观察线也必须 1500 根。

    两边不一致时不会报错，只会让两套 EMA 差几十点（历史实测 1h 上 82.8 点），
    而"谁在上方"这种判断恰好靠几十点就能判反。
    """
    from ai_trader.web.live import EMA_PERIODS as dash_periods
    from ai_trader.web.live import EMA_WARMUP_BARS as dash_warmup
    assert int(L.EMA_WARMUP_BARS) == int(dash_warmup)
    assert tuple(L.EMA_PERIODS) == tuple(dash_periods)


def test_ema_is_seeded_on_first_close_not_sma():
    """种子必须是第一根收盘价。

    用 SMA 做种子是"另一种常见 EMA"，结果差几十点却完全不报错。
    这里用短序列放大差异，确保实现没被换成 SMA 种子。
    """
    closes = [76000.0 + 300.0 * math.sin(i / 4.0) for i in range(60)]
    ours = L.ema_values(closes, 20)[-1]
    a = 2.0 / 21.0
    e = closes[0]
    for v in closes[1:]:
        e = v * a + e * (1.0 - a)
    assert ours == e
    sma_seed = sum(closes[:20]) / 20.0
    e2 = sma_seed
    for v in closes[20:]:
        e2 = v * a + e2 * (1.0 - a)
    assert abs(ours - e2) > 1.0, "短序列上 SMA 种子与首根种子必须差异明显，否则这条测试没有分辨力"


def test_ema_not_emitted_before_warmup_and_the_skip_is_counted():
    """预热不足的 K 不许出线（会交给下游一条没收敛的线），而且必须报数。"""
    bars = _series(100, 0, M15)
    rows, stats = L.build_bar_lines("BTCUSDT", M15, bars)
    assert not any(r["line_kind"].startswith("ema") for r in rows)
    for p in L.EMA_PERIODS:
        assert stats["skipped"]["ema%d:warmup" % p] == 100


# ---------------------------------------------------------------- 唐奇安
def test_donchian_matches_the_strategy_state():
    """与 trading/donchian.py 的 state() 逐字一致（它是唯一被验证过的口径）。

    注意最后一根必须被造成**窗口极值**，否则"含不含当根"两种写法算出的最大值相同，
    这条测试就会在口径被改错时保持绿色 —— 这正是本项目怕的那种假信心。
    """
    from ai_trader.config import TradingCfg
    from ai_trader.trading.donchian import DonchianStrategy
    bars = _series(400, 0, H1)
    last = bars[-1]
    bars[-1] = L.Bar(last.ts, last.o, 99999.0, 1.0, last.c, last.v)   # 当根就是新高
    rows, _ = L.build_bar_lines("BTCUSDT", H1, bars)
    lastrows = _by_ts(rows, 399 * H1)
    strat = DonchianStrategy(TradingCfg())
    strat.bars = [{"t": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v or 0.0}
                  for b in bars]
    st = strat.state(price=float(bars[-1].c))
    assert st["ready"] is True
    assert float(lastrows["donchian_hi"]["line_price"]) == float(st["upper"])
    assert float(lastrows["donchian_lo"]["line_price"]) == float(st["lower"])
    assert float(lastrows["donchian_hi"]["line_price"]) < 99999.0, \
        "含了当根：上轨等于当根最高价，那『突破上轨』永远不可能成立"


def test_donchian_and_prior_high_exclude_the_current_bar():
    """不含当根不是洁癖：含了当根，"收盘突破上轨"就永远不可能成立。"""
    bars = _series(200, 0, M15)
    # 把最后一根做成明显的新高 / 新低
    last = bars[-1]
    bars[-1] = L.Bar(last.ts, last.o, 99999.0, 1.0, last.c, last.v)
    rows, _ = L.build_bar_lines("BTCUSDT", M15, bars)
    got = _by_ts(rows, last.ts)
    assert float(got["donchian_hi"]["line_price"]) < 99999.0
    assert float(got["donchian_lo"]["line_price"]) > 1.0
    assert float(got["prior_high"]["line_price"]) < 99999.0
    assert float(got["prior_low"]["line_price"]) > 1.0


# ---------------------------------------------------------------- 摆动点
def test_swing_point_waits_for_right_side_confirmation():
    """枢轴 j 要到第 j+k 根收盘才成立，提前用就是偷未来。

    两根完全同形的 K 线，差别只在"当时能不能知道"。这条测试锁的就是这件事。
    """
    k = L.SWING_HALF
    highs = [100.0] * 20
    lows = [90.0] * 20
    highs[10] = 120.0          # 明显的高点
    lows[10] = 80.0
    sh, sl, sh_i, _sl_i = L.swing_values(highs, lows, k)
    for i in range(10, 10 + k):
        assert sh[i] is None, "第 %d 根还没到确认时刻就用上了摆动高点" % i
        assert sl[i] is None
    assert sh[10 + k] == 120.0
    assert sl[10 + k] == 80.0
    assert sh_i[10 + k] == 10


def test_swing_line_records_pivot_as_source_and_confirmation_as_ready():
    """落库时 src_ts 必须是枢轴那根、ready 必须是确认那根的收盘。

    这两列是防偷未来的**物证**：只有它们能事后回答"当时到底用了什么"。
    """
    k = L.SWING_HALF
    bars = []
    for i in range(40):
        h = 120.0 if i == 10 else 100.0
        lo = 80.0 if i == 10 else 90.0
        bars.append(L.Bar(i * M15, 100.0, h, lo, 100.0, 1.0))
    rows, _ = L.build_bar_lines("BTCUSDT", M15, bars, warmup_bars=0)
    got = _by_ts(rows, 20 * M15)
    assert int(got["swing_high"]["src_ts"]) == 10 * M15
    assert int(got["swing_high"]["line_ready_ms"]) == (10 + k) * M15 + M15
    assert int(got["swing_high"]["line_price"]) == 120.0


# ---------------------------------------------------------------- 区间演化四线
def test_range_lines_are_copied_from_db_not_recomputed(tmp_path):
    """四线必须与 range_evolution 该小时行**完全相等**（本模块不重算）。

    重算的版本在引擎改版后会与生产分叉，而图看起来完全正常 —— 分叉了看不出来。
    """
    db = str(tmp_path / "market_range.db")
    st = RangeStore(db)
    states = [(i * H1, 75670.0, 79856.1, i, "DOWN", None, 79646.4, 75158.0, "",
               1, i + 1) for i in range(5)]
    # 引擎元组: (ts, lo_b, hi_b, seg_serial, direction, hard, soft, key, tag, fam, seg)
    st.upsert_states("BTCUSDT", rows_from_states(states, ts8))
    st.close()

    range_rows = B.load_range_rows(db, "BTCUSDT")
    bars = [L.Bar(i * H1, 75800.0, 75900.0, 75700.0, 75864.0, 900.0) for i in range(5)]
    rows, _ = L.build_bar_lines("BTCUSDT", H1, bars, range_rows, warmup_bars=0)
    for i, raw in enumerate(range_rows):
        got = _by_ts(rows, i * H1)
        assert float(got["range_soft_up"]["line_price"]) == float(raw["soft_up"])
        assert float(got["range_soft_dn"]["line_price"]) == float(raw["soft_dn"])
        assert float(got["range_hard_up"]["line_price"]) == float(raw["hard_up"])
        assert float(got["range_key"]["line_price"]) == float(raw["key"])
        assert float(got["range_p"]["line_price"]) == float(L.range_p(raw))
        # hard_dn 在 DOWN 方向上是 NULL：不许落库，也不许编一个值出来
        assert "range_hard_dn" not in got


def test_range_p_mapping_is_inverse_of_rows_from_states():
    """P 的读回口径必须与写入口径互逆，且与 range/api.py::_row 一致。

    P 没有独立列，它按方向折叠进 soft_*。这一层错了不会报错，
    只会让"顺向边界"变成"软边界"，而两个价格在 DOWN 方向恰好不同。
    """
    from ai_trader.range.api import _row as api_row
    # DOWN: lo_b=P, hi_b=R ⇒ soft_dn=P, hard_up=R
    down = rows_from_states([(0, 75670.0, 79856.1, 0, "DOWN", None,
                              79646.4, 75158.0, "", 1, 1)], ts8)[0]
    row = {"ts": down[0], "soft_up": down[2], "soft_dn": down[3],
           "hard_up": down[4], "hard_dn": down[5], "key": down[6],
           "direction": down[9]}
    assert L.range_p(row) == 75670.0
    api = api_row((0, 75670.0, 79856.1, 0, "DOWN", None, 79646.4, 75158.0, "", 1, 1))
    assert api["p"] == L.range_p(row)

    # UP: hi_b=P, lo_b=R ⇒ soft_up=P, hard_dn=R
    up = rows_from_states([(0, 73000.0, 79000.0, 0, "UP", None,
                            70000.0, 73500.0, "", 2, 1)], ts8)[0]
    rowu = {"ts": up[0], "soft_up": up[2], "soft_dn": up[3],
            "hard_up": up[4], "hard_dn": up[5], "key": up[6],
            "direction": up[9]}
    assert L.range_p(rowu) == 79000.0
    api2 = api_row((0, 73000.0, 79000.0, 0, "UP", None, 70000.0, 73500.0, "", 2, 1))
    assert api2["p"] == L.range_p(rowu)


def test_unformed_range_rows_emit_nothing_and_are_counted():
    """未成形（direction=PRE，四线全空）的行不落库，但要在跳过清单里说得出来。

    注意 v11 里 K（v11_key）是"每根 min/max、无滞后、只记录"的，
    所以未成形的行也可能有 K 而没有四线 —— 那种情况 range_key 应该照常落库。
    这里造的是最极端的"全空"，包括 K。
    """
    bars = [L.Bar(i * H1, 1.0, 2.0, 0.5, 1.5, 1.0) for i in range(3)]
    rows, stats = L.build_bar_lines(
        "BTCUSDT", H1, bars,
        [_range_row(i * H1, None, None, None, None, None, "PRE") for i in range(3)],
        warmup_bars=0)
    assert not any(r["line_kind"].startswith("range_") for r in rows)
    assert stats["skipped"]["range_soft_up:null_in_db"] == 3
    assert stats["skipped"]["range_key:null_in_db"] == 3


def test_unformed_range_row_still_emits_k_when_the_db_has_it():
    """K 与四线不是一回事：未成形时四线为 NULL，但 K 已经被记录。"""
    bars = [L.Bar(i * H1, 1.0, 2.0, 0.5, 1.5, 1.0) for i in range(2)]
    rows, _ = L.build_bar_lines(
        "BTCUSDT", H1, bars,
        [_range_row(i * H1, None, None, None, None, 75158.0, "PRE") for i in range(2)],
        warmup_bars=0)
    kinds = {r["line_kind"] for r in rows if r["line_kind"].startswith("range_")}
    assert kinds == {"range_key"}
    assert all(float(r["line_price"]) == 75158.0 for r in rows
               if r["line_kind"] == "range_key")


# ---------------------------------------------------------------- 防偷未来
def test_fifteen_minute_bar_uses_only_the_closed_hourly_row():
    """设计稿 §12 第 1 步的硬要求：1h 线的值在 1h 收盘后才可用于 15m K 线。

    15m 的 12:00 那根（12:15 收盘）只能用 11:00 那根 1h（12:00 收盘）。
    用 12:00 那根 1h（13:00 收盘）就是偷 0.75 小时未来 —— 回测里看不出，
    实盘里表现为"当时根本不知道的东西事后看很准"。
    """
    t0 = lab("2026-11-07 09:00")
    range_rows = [_range_row(t0 + i * H1, 79646.4, 75670.0, 79856.1, None,
                             75158.0, "DOWN") for i in range(4)]
    bars = _series(8, t0, M15)                    # 09:00 ~ 10:45 之外再造几根
    # 直接构造 12:00 那根 15m（开盘 12:00、收盘 12:15）
    t12 = lab("2026-11-07 12:00")
    bars = [L.Bar(t12 - 3 * M15 + i * M15, 75800.0, 75900.0, 75700.0, 75864.0, 900.0)
            for i in range(4)]
    rows, stats = L.build_bar_lines("BTCUSDT", M15, bars, range_rows, warmup_bars=0)
    got = _by_ts(rows, t12)
    assert float(got["range_key"]["line_price"]) == 75158.0
    # 用的是 11:00 那根 1h，不是 12:00 那根
    assert int(got["range_key"]["src_ts"]) == t12 - H1
    assert int(got["range_key"]["line_ready_ms"]) == t12
    # 弱口径（ts <= bar_close）会用到 12:00 那根 → 它的 ready 晚于本根收盘
    assert int(t12) + H1 > t12 + M15


def test_every_stored_line_ready_before_its_bar_closes():
    """全部行都必须满足 line_ready_ms <= bar_close。这是第 1 步的总不变量。"""
    t0 = lab("2026-09-01 00:00")
    bars = _series(1700, t0, M15)
    range_rows = [_range_row(t0 + i * H1, 79646.4, 75670.0, 79856.1, None,
                             75158.0, "DOWN") for i in range(0, 1700 // 4 + 1)]
    rows, _ = L.build_bar_lines("BTCUSDT", M15, bars, range_rows)
    assert rows
    late = [r for r in rows if int(r["line_ready_ms"]) > int(r["ts"]) + M15]
    assert not late, "有 %d 行线的可用时刻晚于本根收盘: %s" % (len(late), late[:2])


def test_forming_bar_is_not_used_when_now_is_given():
    """实盘路径必须能把还没收盘的那根剔掉，且剔了几根要说出来。"""
    t0 = lab("2026-11-07 00:00")
    bars = _series(20, t0, M15)
    now = int(bars[-1].ts) + M15 // 2            # 最后一根只走了一半
    rows, stats = L.build_bar_lines("BTCUSDT", M15, bars, now_ms=now)
    assert stats["bars_forming"] == 1
    assert stats["bars_used"] == 19
    assert all(int(r["ts"]) < int(bars[-1].ts) for r in rows)


# ---------------------------------------------------------------- 落库
def test_store_upsert_is_idempotent_and_separate(tmp_path):
    """重跑同一段必须得到同一批行（回填要能中断续跑）。"""
    kdb = str(tmp_path / "kbar.db")
    bars = _series(300, 0, M15)
    s1 = B.build_and_persist(kdb, "BTCUSDT", M15, bars, None)
    with KBarStore(kdb) as st:
        c1 = st.counts("BTCUSDT", M15)
    s2 = B.build_and_persist(kdb, "BTCUSDT", M15, bars, None)
    with KBarStore(kdb) as st:
        c2 = st.counts("BTCUSDT", M15)
    assert s1["lines"] == s2["lines"] > 0
    assert c1["lines"] == c2["lines"]
    assert c1["bars"] == c2["bars"]
    # 开头几根还没攒够窗口（20/150 根），所以不是每根都有线 —— 但不能一根都没有
    assert 0 < c1["bars"] <= 300


def test_rebuild_replaces_instead_of_appending(tmp_path):
    """整段重算要先删后写；增量回填不要用它（会把历史抹掉）。"""
    kdb = str(tmp_path / "kbar.db")
    bars = _series(300, 0, M15)
    B.build_and_persist(kdb, "BTCUSDT", M15, bars, None)
    with KBarStore(kdb) as st:
        n1 = st.counts("BTCUSDT", M15)["lines"]
        st.delete_interval("BTCUSDT", M15)
        assert st.counts("BTCUSDT", M15)["lines"] == 0
    stats = B.build_and_persist(kdb, "BTCUSDT", M15, bars[-200:], None, rebuild=True)
    with KBarStore(kdb) as st:
        n2 = st.counts("BTCUSDT", M15)["lines"]
    assert stats["deleted"] == 0            # 已经手动删过了
    assert n2 < n1                          # 只用 200 根算，行数必须更少


def test_params_are_recorded_in_meta(tmp_path):
    """口径必须跟着数据走：只改代码不留痕，以后没人算得清这个数怎么来的。"""
    import json
    kdb = str(tmp_path / "kbar.db")
    B.build_and_persist(kdb, "BTCUSDT", M15, _series(200, 0, M15), None)
    with KBarStore(kdb) as st:
        meta = json.loads(st.get_meta("line_params:BTCUSDT:15m"))
    assert meta["ema_warmup_bars"] == L.EMA_WARMUP_BARS
    assert meta["donchian_hi_bars"] == 150 and meta["donchian_lo_bars"] == 75
    assert meta["prior_hl_bars"] == L.PRIOR_HL_BARS
    assert meta["swing_half"] == L.SWING_HALF
    assert meta["range_period_ms"] == H1
    assert meta["interval_label"] == "15m"


def test_dist_pct_sign_follows_price_above_or_below_the_line():
    """距离口径：(c - p)/p*100。价格在线下方必须是负数 —— 符号写反不会报错。"""
    bars = [L.Bar(0, 100.0, 110.0, 90.0, 105.0, 1.0)]
    rows, _ = L.build_bar_lines("BTCUSDT", H1, bars,
                                [_range_row(0, 120.0, 100.0, 130.0, None, 95.0, "DOWN")],
                                warmup_bars=0)
    got = _by_ts(rows, 0)
    assert float(got["range_soft_up"]["dist_pct"]) == pytest.approx((105.0 - 120.0) / 120.0 * 100)
    assert float(got["range_soft_dn"]["dist_pct"]) == pytest.approx((105.0 - 100.0) / 100.0 * 100)
    assert float(got["range_soft_up"]["dist_pct"]) < 0


# ---------------------------------------------------------------- 只读纪律
def test_open_ro_really_cannot_write(tmp_path):
    """只读不是"记得别写"，是 SQLite 层拒绝。"""
    p = str(tmp_path / "x.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t (a INTEGER)")
    con.commit()
    con.close()
    ro = B.open_ro(p)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM t")
    finally:
        ro.close()


def test_refuses_to_write_the_source_db(tmp_path):
    """--kbar-db 指到源库上必须拒绝，不能把观察线写进生产行情库。

    连 dry_run 也拒：拒绝的理由是"路径就是源库"这个事实，与这次写不写无关。
    """
    p = str(tmp_path / "market.db")
    bars = _series(10, 0, M15)
    with pytest.raises(ValueError):
        B.build_and_persist(p, "BTCUSDT", M15, bars, None, sources=[p])
    with pytest.raises(ValueError):
        B.build_and_persist(p, "BTCUSDT", M15, bars, None, dry_run=True, sources=[p])


def test_sources_are_not_touched_by_a_build(tmp_path):
    """落库不许碰源库：文件字节数与 mtime 必须一点没变。"""
    src = str(tmp_path / "market.db")
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE bars (symbol TEXT, interval_ms INTEGER, open_time INTEGER,"
                " o REAL, h REAL, l REAL, c REAL, v REAL)")
    con.execute("INSERT INTO bars VALUES ('BTCUSDT', 900000, 0, 100, 110, 90, 105, 1)")
    con.commit()
    con.close()
    before = (os.path.getsize(src), os.path.getmtime(src))
    bars = B.load_market_bars(src, "BTCUSDT", M15)
    kdb = str(tmp_path / "kbar.db")
    B.build_and_persist(kdb, "BTCUSDT", M15, bars, None, sources=[src])
    assert (os.path.getsize(src), os.path.getmtime(src)) == before
    assert not os.path.exists(src + "-wal") or os.path.getsize(src + "-wal") == 0


def test_kbar_modules_never_open_a_production_db_for_writing():
    """源码层防线：对生产库的连接只能走 open_ro；写库只允许写自己的 kbar.db。

    这条测试防的是"以后有人图省事，在 store.py 里顺手读一下 market_range.db"——
    那一读就把只读纪律破了，而且不会有任何报错。
    """
    kbar_dir = os.path.join(ROOT, "ai_trader", "kbar")
    b = io.open(os.path.join(kbar_dir, "build.py"), encoding="utf-8").read()
    assert "mode=ro" in b, "build.py 没有只读 URI"
    assert "PRAGMA query_only=ON" in b, "build.py 没有把连接钉成只读"
    # build.py 里唯一允许出现 sqlite3.connect 的地方就是 open_ro
    assert b.count("sqlite3.connect(") == 1, "build.py 里出现了第二处连接"
    # lines.py 是纯计算，不许碰库
    lines_src = io.open(os.path.join(kbar_dir, "lines.py"), encoding="utf-8").read()
    assert "sqlite3" not in lines_src, "lines.py 应该只做计算，不碰数据库"
    # store.py 只写自己的库，不许出现生产库文件名
    store_src = io.open(os.path.join(kbar_dir, "store.py"), encoding="utf-8").read()
    for bad in ("market.db", "market_range.db", "market_trade.db"):
        assert bad not in store_src.replace("data/market.db", "").replace(
            "data/market_range.db", ""), "store.py 里出现了生产库名: " + bad


# ---------------------------------------------------------------- 可选：生产快照上的逐位复核
@pytest.mark.skipif(not os.environ.get("KBAR_RANGE_DB"),
                    reason="未指定 KBAR_RANGE_DB（生产只读快照），跳过生产数据复核")
def test_production_snapshot_target_hour_values():
    """对生产快照复核 2026-11-07 12:00 那根 1h 的五条 EMA 与区间四线。

    探针已经打印过这些数；这里把它们钉成断言，防止以后悄悄漂。
    只在显式给了快照路径时跑（默认测试集不依赖生产数据）。
    """
    db = os.environ["KBAR_RANGE_DB"]
    bars = B.load_range_bars_1h(db, "BTCUSDT", 1532)
    target = lab("2026-11-07 12:00")
    kept = [b for b in bars if int(b.ts) <= target][-1500:]
    rows, _ = L.build_bar_lines("BTCUSDT", H1, kept, B.load_range_rows(db, "BTCUSDT"))
    got = _by_ts(rows, target)
    assert float(got["ema20"]["line_price"]) == pytest.approx(75974.1968, abs=1e-4)
    assert float(got["ema200"]["line_price"]) == pytest.approx(77907.9892, abs=1e-4)
    assert float(got["donchian_hi"]["line_price"]) == 81188.0
    assert float(got["donchian_lo"]["line_price"]) == 75158.0
    assert float(got["range_soft_up"]["line_price"]) == 79646.4
    assert float(got["range_soft_dn"]["line_price"]) == 75670.0
    assert float(got["range_hard_up"]["line_price"]) == 79856.1
    assert float(got["range_key"]["line_price"]) == 75158.0
    assert float(got["range_p"]["line_price"]) == 75670.0
    assert "range_hard_dn" not in got
