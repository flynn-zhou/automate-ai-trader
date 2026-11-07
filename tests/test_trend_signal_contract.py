# -*- coding: utf-8 -*-
"""趋势信号的字段契约。

2026-11-01 事故：want_entry() 返回的字典缺 "name" 字段，
而 _open() 与决策日志都读 sig["name"]。
后果：常规"上穿开多"路径在第一次触发时抛 KeyError，
被上层 except 吞成"决策步骤异常"——策略永不建仓，但进程活着、日志有输出。

这和 bar.vol 那个 bug 完全同类：**从未执行过的代码路径里藏着致命的契约错误**。
所以这里把三条信号路径（上穿/下穿/启动对账）的必需字段全部钉死。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import TradingCfg
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000
REQUIRED = ("ok", "name", "reason")


def _tg(bars_close):
    tg = TrendStrategy(TradingCfg(strategy="trend", sma_lookback=5))
    tg.seeded = True
    tg.need_reconcile = False
    tg.bars = [{"t": i * IV4H, "o": c, "h": c, "l": c, "c": c, "v": 1.0}
               for i, c in enumerate(bars_close)]
    return tg


def test_want_entry_has_name():
    # 先跌再拉起：最后一根站上均线，前一根在下方 -> 上穿
    tg = _tg([100.0, 99, 98, 97, 96, 95, 94, 93, 120.0])
    sig = tg.want_entry(120.0)
    assert sig is not None, "构造的序列必须产生上穿信号"
    for k in REQUIRED:
        assert k in sig, "want_entry 缺少必需字段 %r（_open 会 KeyError）" % k
    assert sig["name"] == "trend_up"


def test_want_exit_has_name():
    # 先涨再跌落：最后一根跌破均线，前一根在上方 -> 下穿
    tg = _tg([100.0, 101, 102, 103, 104, 105, 106, 107, 80.0])
    sig = tg.want_exit(80.0)
    assert sig is not None, "构造的序列必须产生下穿信号"
    for k in REQUIRED:
        assert k in sig, "want_exit 缺少必需字段 %r" % k
    assert sig["name"] == "trend_down"


def test_startup_entry_has_name():
    tg = _tg([100.0, 99, 98, 97, 96, 95, 94, 93, 120.0])
    tg.need_reconcile = True
    sig = tg.startup_entry(120.0)
    assert sig is not None
    for k in REQUIRED:
        assert k in sig, "startup_entry 缺少必需字段 %r" % k
    assert sig["name"] == "trend_reconcile"


def test_signals_are_serializable_for_logging():
    """决策日志会把信号字段写进库，未序列化对象会炸。"""
    import json
    tg = _tg([100.0, 99, 98, 97, 96, 95, 94, 93, 120.0])
    for sig in (tg.want_entry(120.0), tg.want_exit(120.0)):
        if sig:
            json.dumps(sig["name"])
            json.dumps(sig["reason"])
