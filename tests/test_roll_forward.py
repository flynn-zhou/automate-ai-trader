# -*- coding: utf-8 -*-
"""看板的「开多触发线」不能把 SMA 当静止门槛。

缺陷：大屏写「涨到 82349（还需 +5.5%）并收盘确认」。
SMA200 每根 4h 都把最老的一根挤出去——被挤掉的那根比现价高，
均线就自己往下走。实测 2026-11-01（价 77719 / SMA 82337）：
价格完全不动，186 根 4h（约 31 天）后现价就会高于均线。

也就是说那次入场根本不需要涨价。用户看到"永远差 5.5%"，
会以为在等一个可能永远不来的上涨；实际系统在等均线下来。
两种状态的"还要多久"差了一个数量级，处置也不同。

这不是纯展示问题：决策日志用的是同一句措辞，运维排查时也会被误导。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000


def _mk(prices, lookback=10):
    cfg = AppCfg()
    cfg.trading.sma_lookback = lookback
    st = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(prices)]
    return st


# 价格形态必须让【冻结的现价】能真正追上都线：
#   近期 8 根 200（高价）、最近 5 根跌到 50，现价 100 夹在中间。
#   当前 SMA10 = 140 > 100（在均线下方）；
#   随着 200 被 100 挤出去，均值会降到 100 以下 -> 价格不动也能上穿。
# 反例（不要用）：历史一路从 200 跌到 100 —— 窗口最终全变成 100，
#   均值恰好等于 100，而 above 用的是严格大于，于是永远不上穿。
#   我第一版就是这么写的，测出来的 None 被误判成"滚动推算没接线"。
PRICES = [150.0] * 3 + [200.0] * 8 + [50.0] * 5
PX = 100.0


def test_roll_forward_finds_cross_when_price_is_flat():
    """价格不动、老的高价 K 被挤出去 -> 均线自己下来 -> 应当算出上穿根数。"""
    prices = list(PRICES)
    st = _mk(prices)
    r = st.roll_forward_to_cross(100.0)
    assert r["roll_bars"] is not None, (
        "价格不动时均线必然会向下滚到 100 以下，应当能算出上穿根数；"
        "返回 None 说明滚动推算没接线")
    assert r["roll_bars"] > 0
    assert r["roll_days"] >= 0


def test_roll_forward_is_consistent_with_the_window():
    """算出的根数必须自洽：第 k 根时 price > sma，第 k-1 根时不成立。"""
    st = _mk(PRICES)
    n = 10
    px = PX
    r = st.roll_forward_to_cross(px)
    k = r["roll_bars"]
    # 参考窗口必须与 state() 同口径：cl[-(n+1):-1]，不含被判定的那根。
    cl = [b["c"] for b in st.bars]
    window = list(cl[-(n + 1):-1])

    def sma_after(steps):
        w = list(window)
        for _ in range(steps):
            w.pop(0)
            w.append(px)
        return sum(w) / n

    assert px > sma_after(k), (
        "第 %d 根时还没上穿（均值 %.2f），算出的根数偏小" % (k, sma_after(k)))
    if k > 1:
        assert px <= sma_after(k - 1), (
            "第 %d 根时就已经上穿了（均值 %.2f），算出的根数偏大"
            % (k - 1, sma_after(k - 1)))


def test_roll_forward_returns_none_when_price_never_catches_up():
    """价格远低于所有历史 -> 冻结价格永远追不上均线，必须返回 None。

    反证用例：防止把"算不出来"伪装成一个很小的天数。
    """
    prices = [500.0] * 20
    st = _mk(prices)
    r = st.roll_forward_to_cross(100.0, max_bars=300)
    assert r.get("roll_bars") is None, (
        "价格 100 永远低于 500 的均线，不应算出上穿根数；"
        "算出 %s 说明滚动推算写错了" % r.get("roll_bars"))


def test_roll_forward_empty_when_not_ready():
    """历史不足时不能瞎算。"""
    st = _mk([100.0] * 3)          # 远少于 lookback+1
    r = st.roll_forward_to_cross(100.0)
    assert r == {}, "历史不足时应返回空字典，不能给出一个假的天数"
