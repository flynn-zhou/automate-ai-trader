# -*- coding: utf-8 -*-
"""策略喂进去的 4h K 必须和回测同源同构造。

回测（tools/long_only.py 等）用的是交易所官方 4h K 线：OHLCV 全是真的。
线上却是本地用逐笔聚合出来的 bar。2026-11-02 实测两者的差别：

    开盘时间        本地close   官方close   量比
    11-01 12:00    77930.1    77930.1     0.055
    11-01 16:00    77930.0    77930.0     0.000
    11-01 20:00    77949.0    77949.0     0.072
    11-02 00:00    77770.6    77770.6     0.014

收盘价这次碰巧一致（服务在收盘那一刻大概率在线），但成交量只有官方的
0~7%——因为聚合器每次重启都从零开始，一个 4h 桶只累积到本次运行期间的成交。

两个后果：
  1. 只要收盘瞬间数据源不活跃（第 81 轮就实测到 WS 全程静默），
     本地 bar 的 close 就是"我们最后一次收到的成交价"，而不是官方收盘价。
  2. 大屏上的 4h 成交量是假的（0.000 对 11197）。

所以：4h 收盘时优先取官方 K 线喂给策略，取不到才退回本地 bar。
这样线上策略的输入构造方式与回测完全一致。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.config import TradingCfg
from ai_trader.trading.trend import TrendStrategy


def _strat():
    cfg = TradingCfg()
    return TrendStrategy(cfg, symbol="BTCUSDT", fapi_base="https://fapi.binance.com")


class _Bar:
    def __init__(self, t, o, h, l, c, v=0.0):
        self.open_time, self.open, self.high = t, o, h
        self.low, self.close, self.volume = l, c, v


def test_parses_official_kline_into_bar():
    """官方 kline 数组要能变成策略认识的 bar（字段名不能错）。"""
    s = _strat()
    raw = [1704067200000, "42000.10", "42100.00", "41900.00", "42050.50",
           "1234.567", 1704081599999, "0", 100, "0", "0", "0"]
    b = s.bar_from_kline(raw)
    assert b is not None
    assert b.open_time == 1704067200000
    assert b.open == pytest.approx(42000.10)
    assert b.high == pytest.approx(42100.00)
    assert b.low == pytest.approx(41900.00)
    assert b.close == pytest.approx(42050.50)
    assert b.volume == pytest.approx(1234.567), u"成交量没解析出来"


def test_rejects_forming_kline():
    """还没收盘的 K 不能当已实现信号用——否则会拿半根 K 做判断。"""
    import time
    s = _strat()
    now = int(time.time() * 1000)
    t = (now // 14400000) * 14400000
    raw = [t, "1", "1", "1", "1", "1", t + 14400000 - 1, "0", 1, "0", "0", "0"]
    assert s.bar_from_kline(raw) is None, u"未收盘的 K 被接受了"


def test_rejects_malformed_kline():
    s = _strat()
    assert s.bar_from_kline([]) is None
    assert s.bar_from_kline(None) is None
    assert s.bar_from_kline([1, 2, 3]) is None


def test_bad_kline_price_rejected():
    """官方 K 线里若出现 0 价（第 81 轮那种噪声），不能喂进策略。"""
    s = _strat()
    raw = [1704067200000, "0", "0", "0", "0", "0",
           1704081599999, "0", 0, "0", "0", "0"]
    assert s.bar_from_kline(raw) is None, u"0 价的官方 K 线被接受了"


def test_official_bar_beats_local_in_on_bar():
    """on_bar 接受官方 bar 后，成交量必须是官方的，不是本地残缺量。"""
    s = _strat()
    s.bars = [{"t": 1704067200000 - 14400000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]
    local = _Bar(1704067200000, 42000.0, 42100.0, 41900.0, 42050.0, v=3.0)
    official_raw = [1704067200000, "42000.10", "42100.00", "41900.00", "42050.50",
                    "1234.567", 1704081599999, "0", 100, "0", "0", "0"]
    off = s.bar_from_kline(official_raw)
    assert s.on_bar(off) is True
    assert s.bars[-1]["v"] == pytest.approx(1234.567), (
        u"喂进去的是本地残缺量 %r，不是官方量" % s.bars[-1]["v"])
    assert s.bars[-1]["c"] == pytest.approx(42050.50)
    # 同一根再来一次必须被去重
    assert s.on_bar(off) is False
