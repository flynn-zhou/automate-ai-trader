# -*- coding: utf-8 -*-
"""引擎级验证：重启后持仓必须按策略出场（不只是策略对象层面）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000


class _Feed:
    def __init__(self, px):
        self.px = px
        self.cfg = type("C", (), {"market": type("M", (), {
            "fapi_base": "http://x", "http_proxy": ""})()})()
        # _log_decision 会往 hub 里发看板事件，假 feed 必须带一个
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()

    def agg(self):
        return self

    def current(self, iv):
        return type("B", (), {"high": self.px, "low": self.px,
                              "close": self.px, "open": self.px})()


class _Ex:
    name = "paper"

    def __init__(self, px):
        self.px = px
        self.sold = []

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.sold.append((side, qty, reason))
        return type("F", (), {"ok": True, "price": self.px, "qty": qty,
                              "fee": 0.0, "order_id": "x", "status": "FILLED",
                              "reason": ""})()

    async def equity(self):
        # 引擎按 eq["equity"] 取值，返回裸 float 会 TypeError
        return {"equity": 100.0}


class _DB:
    def __init__(self):
        self.closed = []
        self.decisions = []
        self.orders = []
        self.fills = []
        self.risks = []

    def log_order(self, d): self.orders.append(d)
    def log_fill(self, d): self.fills.append(d)
    def close_trade(self, d): self.closed.append(d)
    def log_risk(self, k, m): self.risks.append((k, m))
    def log_decision(self, d): self.decisions.append(d)


def _eng(px, closes, entry):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = 25.0
    feed = _Feed(px)
    ex = _Ex(px)
    db = _DB()
    e = DecisionEngine(cfg, feed, ex, db, guard=None)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    e.trend.bars = [{"t": i * IV4H, "o": c, "h": c, "l": c, "c": c, "v": 1.0}
                    for i, c in enumerate(closes)]
    e.trend.seeded = True
    e.trend.need_reconcile = False
    # 止损设在成本下方 25%，和线上 trend_stop_pct 一致，场景才自洽
    e.pos = {"side": "long", "qty": 0.001, "entry": entry,
             "stop": entry * 0.75, "target": 0.0, "entry_ts": 1, "bars": 0,
             "best": entry, "worst": entry, "trade_id": 1,
             "max_hold_bars": 10**9, "open_reason": "", "fee": 0.1,
             "signal": "trend"}
    return e, ex, db


@pytest.mark.asyncio
async def test_restart_with_position_below_sma_actually_sells():
    """重启时持仓、且最近收盘都在均线下方 -> 这一轮必须真的卖出。

    缺陷的用户可见后果：不卖的话仓位会一路持有穿过下跌趋势，
    只能等 25% 熔断，而历史最深浮亏仅 -11.3%。
    """
    closes = [30000.0 + (i % 5) * 10 for i in range(260)]
    closes += [closes[-1] * (0.97 ** (k + 1)) for k in range(3)]
    px = closes[-1]
    e, ex, db = _eng(px, closes, entry=closes[-2])   # 成本就在跌前，止损远低于现价
    await e._step_trend({}, px, 100.0, 1700000000000)
    assert e.pos is None, u"重启后持仓没有出场——会一路持有穿过下跌趋势"
    assert len(ex.sold) == 1, u"必须真的卖出一次"
    assert ex.sold[0][0] == "sell"
    assert len(db.closed) == 1, u"必须落一条平仓记录"


@pytest.mark.asyncio
async def test_no_sell_when_still_above_sma():
    """反向约束：价格还在均线上方时，重启不能误平仓。"""
    closes = [100.0 + i for i in range(300)]
    px = closes[-1]
    e, ex, db = _eng(px, closes, entry=px)
    await e._step_trend({}, px, 100.0, 1700000000000)
    assert e.pos is not None, u"价格在均线上方却把仓位平了"
    assert not ex.sold


@pytest.mark.asyncio
async def test_catastrophe_stop_wins_over_sma_exit():
    """熔断优先级：跌穿 25% 止损时必须按熔断记，不能记成 SMA 出场。
    两种出场的归因不同，混了就查不出真实原因。"""
    closes = [100.0 + i for i in range(300)]      # 仍在均线上方
    entry = 100000.0
    px = 70000.0                                   # 远低于 stop=75000
    e, ex, db = _eng(px, closes, entry=entry)
    await e._step_trend({}, px, 100.0, 1700000000000)
    assert e.pos is None
    assert len(db.closed) == 1
    reason = db.closed[0].get("close_reason") or ""
    assert "熔断" in reason, u"应按熔断出局，实际记成：%s" % reason
