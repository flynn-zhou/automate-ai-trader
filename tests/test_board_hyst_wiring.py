# -*- coding: utf-8 -*-
"""看板必须显示【真触发线】，不是裸均线。

同样的接线类失败：state() 算出 entry_line 是对的，
但快照没把它带出去，大屏就还按裸 SMA 报「涨到 X」——
用户盯着一个到了也不会开仓的价格。

第六十六轮踩过同一个坑（把 SMA200 说成静止门槛），
这次的形状不同、性质一样：报的门槛不是真门槛。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading import trend as trend_mod
from ai_trader.trading.engine import DecisionEngine

IV4H = 14400000
# 窗口 = 9 个 100 + 1 个 99 -> SMA = 99.9；最后一根 50 -> 位于均线下方
PRICES = [100.0] * 9 + [99.0] + [50.0]
PX = 100.0


class _Feed:
    src = "WS"

    def __init__(self, cfg, px):
        self.cfg = cfg
        self.last_price = px
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()


class _Paper:
    name = "paper"


def _eng(hyst, prices=None, px=None):
    prices = PRICES if prices is None else prices
    px = PX if px is None else px
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


def test_snapshot_trigger_equals_sma_without_hysteresis():
    """h=0 时触发线就是均线本身（不能凭空加上带宽）。"""
    snap = _eng(0.0)._build_trend_snapshot(PX, 100.0)
    assert snap["sma"] == pytest.approx(99.9)
    assert snap["trigger"] == pytest.approx(99.9)
    assert snap["hyst_pct"] == pytest.approx(0.0)


def test_snapshot_trigger_includes_hysteresis():
    """有带宽时触发线必须是 SMA*(1+h)，且 need_pct 按它算。"""
    snap = _eng(0.2)._build_trend_snapshot(PX, 100.0)
    assert snap["sma"] == pytest.approx(99.9)
    assert snap["trigger"] == pytest.approx(99.9 * 1.002, rel=1e-12), (
        "快照没把滞回带算进触发线，大屏会报一个到了也不开仓的价格")
    want = (99.9 * 1.002 - PX) / PX * 100.0
    assert snap["need_pct"] == pytest.approx(want, rel=1e-9)


def test_need_pct_zero_only_when_price_reaches_trigger():
    """价格已在均线上方、但还没到触发线时，need_pct 不能是 0。

    这是滞回带来的新状态：above=True 但还没到进场门槛。
    旧代码用 not above 判断，这里会误报"还需 0%"。
    """
    # 注意 state() 的 above 用的是【最后一根收盘】，不是传进来的 price。
    # 第 66 轮在同一个地方栽过：只改 price 参数，above 纹丝不动。
    # 窗口 = 9 个 100 + 1 个 99 -> SMA 99.9；最后收盘 99.95 -> 在上方，
    # 但触发线 99.9*1.002 = 100.0998，还差一截。
    prices = [100.0] * 9 + [99.0] + [99.95]
    snap = _eng(0.2, prices, 99.95)._build_trend_snapshot(99.95, 100.0)
    assert snap["sma"] == pytest.approx(99.9)
    assert snap["above"] is True, "构造前提：收盘价确实在均线上方"
    assert snap["need_pct"] > 0, (
        "在带子里时仍需上涨才能触发，need_pct 不能是 0")
    assert snap.get("roll_bars") is None, (
        "价格已在均线上方时，'等均线自己下来'不适用（均线下来反而更够不到）")


def test_need_pct_zero_at_or_above_trigger():
    """反证：价格到达触发线时 need_pct 必须是 0。"""
    eng = _eng(0.2)
    snap = eng._build_trend_snapshot(101.0, 100.0)     # 101 > 100.0998
    assert snap["need_pct"] == 0.0
