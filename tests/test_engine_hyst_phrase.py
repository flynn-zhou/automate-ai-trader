# -*- coding: utf-8 -*-
"""引擎侧：持仓措辞与等待预期的判据必须跟着滞回带走。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading import trend as trend_mod
from ai_trader.trading.engine import DecisionEngine

IV4H = 14400000


class _Feed:
    src = "WS"

    def __init__(self, cfg, px):
        self.cfg = cfg
        self.last_price = px
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()


class _Paper:
    name = "paper"


def _eng(hyst, prices, px):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 10
    cfg.trading.trend_hysteresis_pct = hyst
    eng = DecisionEngine(cfg, _Feed(cfg, px), _Paper(), None, guard=None)
    st = trend_mod.TrendStrategy(cfg.trading, symbol="BTCUSDT",
                                 exec_market="spot")
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(prices)]
    eng.trend = st
    return eng


# 窗口 = 9 个 100 + 1 个 99 -> SMA 99.9，滞回 0.2% 时下沿 = 99.9*0.998 = 99.7002
BAND = [100.0] * 9 + [99.0]


def test_hold_phrase_says_holding_when_inside_lower_band():
    """价格已跌破均线、但仍在出场线之上：系统继续持有，措辞不能说成要出场。

    这是滞回带来的新状态：99.8 < SMA 99.9，但 99.8 > 下沿 99.7002。
    旧代码拿到 above=False 就写"已跌破SMA，等下一根确认"，
    与实际"仍持有"相矛盾——复盘时会以为出场早该触发。
    """
    prices = BAND + [99.8]
    eng = _eng(0.2, prices, 99.8)
    st = eng.trend.state(99.8)
    assert st["above"] is False, "构造前提：价格确实在裸均线下方"
    assert st["exit_above"] is True, "构造前提：仍在下沿之上，不该出场"
    phrase = eng._hold_phrase(st)
    assert "继续持有" in phrase, (
        "仍在下沿之上应当说继续持有，实际措辞：%s" % phrase)
    assert "已跌破" not in phrase


def test_hold_phrase_says_broken_when_below_exit_line():
    """反证：真正跌破下沿时必须说"已跌破"，不能永远报持有。"""
    prices = BAND + [99.5]        # 99.5 < 99.7002
    eng = _eng(0.2, prices, 99.5)
    st = eng.trend.state(99.5)
    assert st["exit_above"] is False
    assert "已跌破" in eng._hold_phrase(st)


def test_snapshot_shows_wait_inside_upper_band():
    """价格在均线上方但没到触发线时，仍要给等待预期。

    旧判据是 not above，这段会被整个跳过——而这时最需要说明"还要多久"。
    """
    prices = BAND + [99.95]       # 在均线上方，但低于触发线 99.9*1.002=100.0998
    eng = _eng(0.2, prices, 99.95)
    snap = eng._build_trend_snapshot(99.95, 100.0)
    assert snap["above"] is True, "构造前提：在均线上方"
    assert snap["need_pct"] > 0, "构造前提：还没到触发线"
    assert snap.get("wait"), (
        "在上半带时也必须给等待预期，否则大屏只剩'还需涨 X%'")


def test_snapshot_passes_hysteresis_to_wait():
    """快照必须把滞回半宽传给 expected_wait。

    表是按 0.2% 标定的；若引擎传 0（或干脆不传），
    得到的就不是"到触发线"的天数。这里验证传的是配置里的真实值：
    传对了 -> 不打 extrapolated 标记。
    """
    prices = BAND + [99.95]
    eng = _eng(0.2, prices, 99.95)
    snap = eng._build_trend_snapshot(99.95, 100.0)
    ew = snap["wait"]
    assert not ew.get("extrapolated"), (
        "引擎传的 h 与表标定值不一致：requested=%s calibrated=%s"
        % (ew.get("requested_hyst_pct"), ew.get("calibrated_hyst_pct")))
    assert ew["source"]["hyst_pct"] == pytest.approx(0.2)


def test_snapshot_wait_uses_trigger_calibrated_table():
    """等待天数必须来自按触发线标定的表（1.7 天档），不是裸均线档（1.3）。"""
    prices = BAND + [99.95]
    eng = _eng(0.2, prices, 99.95)
    snap = eng._build_trend_snapshot(99.95, 100.0)
    assert snap["dist_pct"] > -2.0, "构造前提：落在 (-2,0] 桶"
    assert snap["wait"]["median_days"] == pytest.approx(1.7)


def test_snapshot_wait_absent_without_hysteresis_when_above():
    """反证：无滞回且已在均线上方时不该给等待预期（那时不是等上穿）。"""
    prices = BAND + [100.5]
    eng = _eng(0.0, prices, 100.5)
    snap = eng._build_trend_snapshot(100.5, 100.0)
    assert snap["above"] is True
    assert not snap.get("wait")
