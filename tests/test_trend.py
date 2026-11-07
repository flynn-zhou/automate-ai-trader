# -*- coding: utf-8 -*-
"""趋势策略的判定逻辑。

钉死三件事：
1. SMA 不含被判定的那根（和回测 sma_at 一致）。
2. 上穿才开多，一路在上方不重复开。
3. 下穿才平多。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import TradingCfg
from ai_trader.trading.trend import TrendStrategy


def make(prices, n=5):
    cfg = TradingCfg(strategy="trend", sma_lookback=n)
    tg = TrendStrategy(cfg)
    tg.bars = [{"t": i * 1000, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(prices)]
    return tg


def test_sma_excludes_current_bar():
    tg = make([100.0] * 6)
    st = tg.state()
    # 最后一根是 100，SMA 取前 5 根
    assert st["ready"] is True
    assert abs(st["sma"] - 100.0) < 1e-9
    assert st["bars"] == 6


def test_cross_up_gives_entry():
    # 前 6 根横盘 100，然后拉到 130，触发上穿
    prices = [100.0] * 6 + [130.0]
    tg = make(prices)
    sig = tg.want_entry(130.0)
    assert sig is not None
    assert "上穿" in sig["reason"]


def test_no_repeat_entry_while_above():
    prices = [100.0] * 6 + [130.0, 131.0, 132.0]
    tg = make(prices)
    assert tg.want_entry(132.0) is None      # 已经在 SMA 上方，不重复开


def test_cross_down_gives_exit():
    prices = [100.0] * 6 + [130.0] + [70.0]
    tg = make(prices)
    sig = tg.want_exit(70.0)
    assert sig is not None
    assert "下穿" in sig["reason"]


def test_not_ready_without_enough_bars():
    tg = make([100.0] * 3, n=5)
    st = tg.state()
    assert st["ready"] is False
    assert tg.want_entry(100.0) is None
    assert tg.want_exit(100.0) is None
