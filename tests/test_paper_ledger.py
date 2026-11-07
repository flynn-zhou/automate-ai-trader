# -*- coding: utf-8 -*-
"""当前代码：一笔完整往返之后，账目必须自洽。

线上库里有 7.77U 对不上、且 fee 只记了一边。那些是剥头皮时代的数据，
但「账目能不能自洽」这件事必须现在就测，否则将来趋势交易一样会错，
而错的方向永远是【看起来赚了】。
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.market.agg import Bar

PX0 = 78000.0
PX1 = 79000.0


class _Agg:
    def __init__(self, px):
        self.px = px

    def current(self, iv):
        return Bar(interval_ms=3000, open_time=0, open=self.px, high=self.px,
                   low=self.px, close=self.px, volume=1.0, closed=True)


class _Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.agg = _Agg(px)
        self.src = "ws"

    def set(self, px):
        self.last_price = px
        self.agg.px = px


def _round_trip():
    cfg = AppCfg()
    cfg.trading.exec_market = "spot"
    cfg.trading.slippage_bps = 0.0
    feed = _Feed(PX0)
    ex = PaperExecutor(feed, cfg)
    rate = cfg.trading.taker_fee_for(True)

    qty = 0.001
    loop = asyncio.get_event_loop()
    b0 = ex.balance
    o = loop.run_until_complete(ex.market_order("buy", qty, reason="测试开仓"))
    assert o.ok, o.reason
    feed.set(PX1)
    c = loop.run_until_complete(ex.market_order("sell", qty, reason="测试平仓"))
    assert c.ok, c.reason
    b1 = ex.balance

    gross = (c.price - o.price) * qty
    fee_open = o.price * qty * rate
    fee_close = c.price * qty * rate
    return {"d_balance": b1 - b0, "gross": gross, "fee_open": fee_open,
            "fee_close": fee_close, "fee_sum": fee_open + fee_close,
            "o_fee": o.fee, "c_fee": c.fee, "qty": qty, "rate": rate}


def test_balance_change_equals_gross_minus_both_fees():
    r = _round_trip()
    expect = r["gross"] - r["fee_sum"]
    assert r["d_balance"] == pytest.approx(expect, abs=1e-9), (
        u"余额变化 %.9f，而 毛利%.6f - 手续费%.6f = %.9f。对不上说明记账漏项。"
        % (r["d_balance"], r["gross"], r["fee_sum"], expect))


def test_fill_fee_is_reported_for_both_sides():
    r = _round_trip()
    assert r["o_fee"] > 0, u"开仓没报手续费"
    assert r["c_fee"] > 0, u"平仓没报手续费"
    assert r["o_fee"] == pytest.approx(r["fee_open"], rel=1e-9)
    assert r["c_fee"] == pytest.approx(r["fee_close"], rel=1e-9)
