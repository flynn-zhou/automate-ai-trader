# -*- coding: utf-8 -*-
"""看板快照里必须真的带上 roll_bars/roll_days（接线检查）。

为什么单独立一条：roll_forward_to_cross 本身有单元测试，
但"快照里没带上它"是另一类失败——函数对的，大屏还是只显示「还需涨 X%」。
接线漏掉不会报错，只会静默少一行字。这类必须单独钉。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.trading import trend as trend_mod
from ai_trader.trading.engine import DecisionEngine

IV4H = 14400000

# 与 test_roll_forward.py 同一形态：近期高价、最近暴跌、现价夹中间
PRICES = [150.0] * 3 + [200.0] * 8 + [50.0] * 5
PX = 100.0


class _Feed:
    src = "WS"
    last_price = PX

    def __init__(self, cfg):
        self.cfg = cfg
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()


class _Paper:
    name = "paper"


def _eng():
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 10
    eng = DecisionEngine(cfg, _Feed(cfg), _Paper(), None, guard=None)
    st = trend_mod.TrendStrategy(cfg.trading, symbol="BTCUSDT",
                                 exec_market="spot")
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(PRICES)]
    eng.trend = st
    return eng


def test_snapshot_carries_roll_forward():
    """空仓且在均线下方时，快照必须带 roll_bars/roll_days。"""
    snap = _eng()._build_trend_snapshot(PX, 100.0)
    assert snap.get("above") is False, "构造应当让价格落在均线下方"
    assert snap.get("roll_bars"), (
        "看板快照里没有 roll_bars —— 大屏只会显示「还需涨 X%%」，"
        "把「在等均线下来」说成「在等价格上涨」")
    assert snap.get("roll_days", 0) > 0


def test_snapshot_omits_roll_when_above_sma():
    """反证：已经在均线上方时不该带这个字段（没有「还要等几天」可言）。

    注意：above 由 state() 决定，它比的是【最后一根已收盘 K 的收盘价】
    与 SMA（不含它自己）——不是 _build_trend_snapshot 的 price 参数。
    所以要让 above 为真必须换 K 线，光改 price 不生效。
    （第一版就是只改了 price，用例红在「above 不是 True」上。）
    """
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 10
    eng = DecisionEngine(cfg, _Feed(cfg), _Paper(), None, guard=None)
    st = trend_mod.TrendStrategy(cfg.trading, symbol="BTCUSDT",
                                 exec_market="spot")
    prices = [100.0] * 10 + [300.0] * 6      # 末根 300，SMA(不含末根)=200
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(prices)]
    eng.trend = st

    snap = eng._build_trend_snapshot(300.0, 100.0)
    assert snap.get("above") is True, "构造应当让末根收盘站在均线上方"
    assert not snap.get("roll_bars"), (
        "已在均线上方还给出滚动天数，会让大屏显示一个不成立的等待")
