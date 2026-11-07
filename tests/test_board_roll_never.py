# -*- coding: utf-8 -*-
"""有滞回带时，必须明说「等均线自己下来是等不到的」。

第六十六轮给大屏加过一句「或价格不动、均线自己下来，约 N 天后上穿」，
用来纠正"把 SMA 说成静止门槛"。那句话在当时是对的。

加了滞回带之后它【变成假的】：
  价格不动 -> SMA 收敛到现价本身；
  进场要求 现价 > SMA*(1+h)；
  SMA 最多等于现价，永远够不到 现价*(1+h)。
于是 roll_forward_to_cross 返回 None，大屏会【沉默地少一句话】。

沉默比说错更危险：用户会以为系统在正常等待，其实在等一件不会发生的事。
"""
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


# 无滞回时"等均线下来"能等到：近期 200、最近跌到 50、现价 100 夹在中间，
# 均线随 200 被挤出而下降，最终会跌破 100。
PRICES = [150.0] * 3 + [200.0] * 8 + [50.0] * 5
PX = 100.0

# 有滞回时必须"等不到"：均线一步步收敛到现价本身，永远差那 0.2%。
#   window = [200] + [99.9]*9 -> SMA 109.91
#   k=1 时 200 被挤出、补进现价 100 -> SMA 99.91
#     h=0    : 100 > 99.91              -> 成立（能等到）
#     h=0.2% : 100 > 99.91*1.002=100.11 -> 不成立；之后窗口全是 100，
#              SMA 最多等于 100，永远够不到 100.2 -> 等不到
# 注意不能用"均线每步跳 10"的序列（纯 200/50 交替）：
#   步长远大于 0.2% 带宽，两种口径会在同一步跨过，测不出差别。
BAND_PRICES = [200.0] + [99.9] * 9 + [50.0]
BAND_PX = 100.0


def test_roll_never_flag_absent_without_hysteresis():
    """反证：无滞回时仍然可以等均线下来，不能误报'等不到'。"""
    snap = _eng(0.0, PRICES, PX)._build_trend_snapshot(PX, 100.0)
    assert snap.get("roll_bars"), "无滞回时应当能算出滚动天数"
    assert not snap.get("roll_never"), "无滞回时不该说等不到"


def test_roll_never_flag_set_with_hysteresis():
    """有滞回时必须明确报'等不到'，而不是沉默地少一句话。"""
    ok = _eng(0.0, BAND_PRICES, BAND_PX)._build_trend_snapshot(BAND_PX, 100.0)
    assert ok.get("roll_bars") == 1, (
        "构造前提：无滞回时应能等到（实际 %s）" % (ok.get("roll_bars"),))
    snap = _eng(0.2, BAND_PRICES, BAND_PX)._build_trend_snapshot(BAND_PX, 100.0)
    assert snap.get("roll_bars") is None, (
        "构造前提：带宽之下冻结现价永远够不到 SMA*(1+h)")
    assert snap.get("roll_never") is True, (
        "有滞回带时均线自己下来到不了触发线，必须报 roll_never；"
        "否则大屏会沉默，用户以为在正常等待")


def test_roll_never_not_set_when_price_already_at_trigger():
    """已经够到触发线时，不应该出现'等不到'的说法。"""
    # PRICES 的均线是 140，触发线 140*1.002=140.28；给 150 已越线
    snap = _eng(0.2, PRICES, PX)._build_trend_snapshot(150.0, 100.0)
    assert snap["need_pct"] == 0.0
    assert not snap.get("roll_never")
