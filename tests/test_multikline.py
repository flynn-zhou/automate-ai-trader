# -*- coding: utf-8 -*-
"""多品种 K 线采集。

最容易错的地方是**字段错位**：币安原始数组里 [6] 是 closeTime、[7] 才是
quoteVolume、[8] 是 trades、[9] 是 takerBuyBase。错一格不会报错——
成交量变成时间戳、时间戳变成成交量，都是"看着正常的大数字"。
所以这里对每个字段单独断言真实取值。
"""
from __future__ import annotations

import pytest

from ai_trader.market.multikline import Bar, MultiKlineCollector, KLINE_MS, parse_kline

# 一根真实形状的 1h K 线（周末低量）
ROW = [1700000000000, "100.0", "110.0", "90.0", "105.0", "1000.0",
       1700003599999, "105000.0", 4321, "400.0", "42000.0", "0"]


class _Cfg:
    class trading:
        symbol = "BTCUSDT"
    class market:
        symbol = "BTCUSDT"
        fapi_base = "https://fapi.test"
        http_proxy = ""


class _Store:
    def __init__(self):
        self.rows = []          # [(symbol, Bar)]
    def put_bar(self, bar, symbol=None):
        self.rows.append((symbol, bar))


class _Subs:
    def __init__(self, syms):
        self._s = list(syms)
    def symbols(self, only_enabled=True):
        return list(self._s)


class _FakeRest:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0
    async def klines(self, interval, limit):
        self.calls += 1
        if self.fail:
            raise RuntimeError("HTTP 451")
        return [ROW]
    async def close(self):
        pass


def test_parse_kline_field_positions():
    b = parse_kline(ROW, 3600000)
    assert isinstance(b, Bar)
    assert b.interval_ms == 3600000
    assert b.open_time == 1700000000000
    assert b.open == 100.0 and b.high == 110.0 and b.low == 90.0 and b.close == 105.0
    assert b.volume == 1000.0, "volume 取到了别的字段"
    # [6] 是 closeTime，绝不能出现在 quote_volume 上
    assert b.quote_volume == 105000.0, "quote_volume 错位（[6] 是 closeTime 不是成交额）"
    assert b.trades == 4321
    assert b.buy_base == 400.0
    assert b.sell_base == 600.0, "主动卖 = 总量 - 主动买"


def test_unsupported_interval_raises():
    with pytest.raises(ValueError):
        MultiKlineCollector(_Cfg(), _Store(), _Subs([]), interval="7m")


def test_symbols_excludes_trading_symbol():
    c = MultiKlineCollector(_Cfg(), _Store(), _Subs(["BTCUSDT", "ETHUSDT", "SOLUSDT"]))
    assert c.symbols() == ["ETHUSDT", "SOLUSDT"], "交易品种应排除（由 Feed 负责）"


@pytest.mark.asyncio
async def test_collect_writes_each_symbol_separately():
    st = _Store()
    c = MultiKlineCollector(_Cfg(), st, _Subs(["BTCUSDT", "ETHUSDT", "SOLUSDT"]))
    c._client = lambda sym: _FakeRest()
    got = await c.collect_once()
    assert got == {"ETHUSDT": 1, "SOLUSDT": 1}
    assert {s for s, _ in st.rows} == {"ETHUSDT", "SOLUSDT"}, "写入了错的品种"
    assert c.collected == 2


@pytest.mark.asyncio
async def test_one_symbol_failure_does_not_block_others():
    """一个品种挂了不能拖死其他品种——这是降级，不是崩溃。"""
    st = _Store()
    c = MultiKlineCollector(_Cfg(), st, _Subs(["ETHUSDT", "SOLUSDT"]))
    c._client = lambda sym: _FakeRest(fail=(sym == "ETHUSDT"))
    got = await c.collect_once()
    assert "ETHUSDT" not in got
    assert got.get("SOLUSDT") == 1, "一个失败把另一个也带没了"
    assert c._fail["ETHUSDT"] == 1
    assert c.skipped == 1


@pytest.mark.asyncio
async def test_failure_counter_resets_on_success():
    st = _Store()
    c = MultiKlineCollector(_Cfg(), st, _Subs(["ETHUSDT"]))
    c._client = lambda sym: _FakeRest(fail=True)
    await c.collect_once()
    assert c._fail["ETHUSDT"] == 1
    c._client = lambda sym: _FakeRest(fail=False)
    await c.collect_once()
    assert c._fail["ETHUSDT"] == 0, "成功后计数没归零，告警会一直挂着"


def test_kline_ms_table_matches_intervals():
    """周期间隔表是 store 落库的键，不能随手改数值。"""
    assert KLINE_MS["1h"] == 3_600_000
    assert KLINE_MS["15m"] == 900_000
    assert KLINE_MS["4h"] == 14_400_000
