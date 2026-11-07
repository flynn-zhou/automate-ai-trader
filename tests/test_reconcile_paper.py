# -*- coding: utf-8 -*-
"""实盘对账关闭遗留持仓时，必须保留它原本的 paper 标记。

缺陷：reconcile_live() 里三处 close_trade/log_trade 硬编码 "paper": 0，
而同一个文件其他地方（312/862/874/961/976）都用
    1 if self.ex.name == "paper" else 0
来算这个标记。5 处算对、3 处写死，说明是疏漏而不是有意。

后果：从纸面切到实盘后，库里遗留的【纸面】持仓会被交易所对账关闭，
并记成 paper=0（真实交易）。于是：
  - realized_pnl(paper=1) 漏掉这笔的盈亏
  - realized_pnl(paper=0) 多出一笔根本不存在的真实交易
  - 纸面/实盘两本账从这一刻起就串了

注意：接管交易所实际持仓时 paper=0 是【对的】（交易所真有币），
这一条不能一起改掉，所以测试要分开钉。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy


class _Feed:
    src = "WS"

    def __init__(self, px=100.0):
        self.px = px
        self.cfg = type("C", (), {"market": type("M", (), {
            "fapi_base": "http://x", "http_proxy": ""})()})()
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self.bar_hooks = []

    def agg(self):
        return self

    def current(self, iv):
        return type("B", (), {"high": self.px, "low": self.px,
                              "close": self.px, "open": self.px})()


class _Ex:
    """实盘现货执行器，交易所侧【没有】持仓。"""
    name = "spot"

    async def position(self):
        return {"qty": 0.0, "entry": 0.0}

    async def equity(self):
        return {"equity": 100.0}


class _DB:
    def __init__(self, open_trade=None):
        self.closed = []
        self.trades = []
        self._open = open_trade

    def open_trade(self):
        return self._open

    def close_trade(self, d):
        self.closed.append(d)

    def log_trade(self, d):
        self.trades.append(d)

    def log_order(self, d): pass
    def log_fill(self, d): pass
    def log_risk(self, k, m): pass
    def log_decision(self, d): pass


def _eng(paper_flag):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    ot = {"id": 7, "symbol": "BTCUSDT", "side": "long", "qty": 0.001,
          "entry_ts": 1, "entry_price": 100.0, "stop_price": 90.0,
          "target_price": 0.0, "open_reason": "test", "paper": paper_flag,
          "max_hold_bars": 10 ** 9}
    db = _DB(open_trade=ot)
    e = DecisionEngine(cfg, _Feed(), _Ex(), db, guard=None)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    e.trend.seeded = True
    e.trend.need_reconcile = False
    e.attach()
    return e, db


@pytest.mark.asyncio
async def test_reconcile_close_keeps_paper_flag():
    """遗留的是纸面仓 -> 对账关闭时仍须记成 paper=1。"""
    e, db = _eng(paper_flag=1)
    assert e.pos is not None, "attach 应恢复出这笔持仓"
    await e.reconcile_live()
    assert len(db.closed) == 1, "交易所空仓，应关闭这笔遗留持仓"
    rec = db.closed[0]
    assert rec.get("paper") == 1, (
        u"这笔是纸面持仓，却被记成了 paper=%r。"
        u"纸面/实盘两本账从此串了。" % rec.get("paper"))


@pytest.mark.asyncio
async def test_reconcile_close_keeps_real_flag():
    """遗留的是真实仓 -> 必须仍记成 paper=0（反向约束）。"""
    e, db = _eng(paper_flag=0)
    assert e.pos is not None
    await e.reconcile_live()
    assert len(db.closed) == 1
    assert db.closed[0].get("paper") == 0, (
        u"真实持仓被记成了 paper=%r，会把真钱交易藏进纸面账"
        % db.closed[0].get("paper"))
