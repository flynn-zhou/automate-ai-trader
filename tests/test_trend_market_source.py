# -*- coding: utf-8 -*-
"""测试：趋势策略的决策数据源必须与执行市场一致。

为什么：回测与信号一致性验证用的是【现货】K 线，
线上一直在用【永续合约】K 线。实测 2000 根 4h 里 above 判定分歧 2 处，
全部落在"离 SMA200 < 0.1%"的临界带（20 根中 2 根），
也就是恰好发生在信号触发的那一刻。

影响有限（最多挪一根，而延迟 1 根的代价在回测里几乎为零），
但"回测验证的世界"和"线上决策的世界"不该是两个市场。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading.trend import TrendStrategy


def test_spot_mode_uses_spot_klines():
    """现货模式下必须走 api.binance.com/api/v3/klines。"""
    c = AppCfg()
    c.trading.exec_market = "spot"
    t = TrendStrategy(c.trading)
    assert t.exec_market == "spot"


def test_perp_mode_uses_futures_klines():
    """永续模式下仍走 fapi。"""
    c = AppCfg()
    c.trading.exec_market = "perp"
    t = TrendStrategy(c.trading)
    assert t.exec_market == "perp"


def test_explicit_market_wins_over_config():
    """显式传入的市场优先于配置（引擎走这条路径）。"""
    c = AppCfg()
    c.trading.exec_market = "perp"
    t = TrendStrategy(c.trading, exec_market="spot")
    assert t.exec_market == "spot", (
        u"引擎显式传了 spot，但策略用了 %r —— 会用合约 K 线去决策现货单" % t.exec_market)


def test_engine_builds_strategy_on_the_right_market():
    """引擎建出来的策略，市场必须对。

    这条测试【只管结果】，不管引擎有没有显式传参。
    我最初写的名字是 test_engine_passes_exec_market，声称验证"必须显式传"，
    但做变异检查时发现：把那行 exec_market= 删掉，测试依然全绿。
    因为 __init__ 里还有一条回退——直接读 cfg.exec_market，
    而它和引擎传的是同一个值。也就是说那行显式传参其实是冗余的。

    真正的逻辑在 __init__ 的回退判断里：对那里做变异，3 条测试变红。
    所以这里改成如实描述，只验证最终结果。
    """
    c = AppCfg()
    c.trading.strategy = "trend"
    c.trading.exec_market = "spot"
    from ai_trader.trading.engine import DecisionEngine
    feed = type("F", (), {
        "cfg": type("C", (), {"market": type("M", (), {
            "fapi_base": "http://x", "http_proxy": ""})()})(),
        "src": "WS", "last_price": 78000.0,
        "hub": type("H", (), {"publish": staticmethod(lambda m: None)})()})()
    ex = type("E", (), {"name": "spot"})()
    e = DecisionEngine(c, feed, ex, None, guard=None)
    # 覆盖 seed，避免测试联网
    import ai_trader.trading.trend as tm
    orig = tm.TrendStrategy.seed_from_rest
    tm.TrendStrategy.seed_from_rest = lambda self, need=420: 0
    try:
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            e._step_trend({}, 78000.0, 71.77, 0))
    finally:
        tm.TrendStrategy.seed_from_rest = orig
    assert e.trend is not None
    assert e.trend.exec_market == "spot", (
        u"引擎建出的策略用的是 %r 市场的数据" % e.trend.exec_market)
