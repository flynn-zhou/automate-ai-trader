# -*- coding: utf-8 -*-
"""纸面余额只能算纸面交易的盈亏，不能把实盘的算进来。

实测过的缺陷（不是推理）：
  executor_state 缺失时，PaperExecutor 用 trades 重建余额：
      balance = paper_balance + tdb.realized_pnl()
  而 realized_pnl() 无差别求和所有已平仓交易。
  造数据：纸面 +10、实盘 -50 -> 重建出 60，正确值 110。

纸面账户和真钱账户是两本账。串了之后：
  - 纸面模式的权益、回撤、风控闸全部失真
  - 而这套系统刚要从纸面切到实盘，正是最需要两本账干净的时候
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.trade_db import TradeDB

_TRADE = ("INSERT INTO trades (symbol,side,qty,entry_ts,entry_price,exit_ts,exit_price,"
          "stop_price,target_price,pnl_gross,fee,pnl_net,paper,close_reason,open_reason)"
          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _db(rows):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    t = TradeDB(path)
    t.start()
    t.stop()
    c = sqlite3.connect(path)
    for pnl, paper in rows:
        c.execute(_TRADE, ("BTCUSDT", "long", 0.01, 1, 100.0, 2, 100.0 + pnl,
                           90.0, 120.0, pnl, 0.0, pnl, paper, "x", "y"))
    c.commit()
    c.close()
    return path


def _paper(tdb, balance=100.0):
    cfg = AppCfg()
    cfg.trading.paper_balance = balance
    pe = PaperExecutor.__new__(PaperExecutor)
    pe.feed = type("F", (), {})()
    pe.cfg = cfg
    pe.balance = balance
    pe.pos_qty = 0.0
    pe.pos_entry = 0.0
    pe._seq = 0
    pe.restore_from_db(tdb)
    return pe


def test_paper_balance_excludes_live_pnl():
    """纸面 +10、实盘 -50 -> 纸面余额必须是 110，不是 60。"""
    path = _db([(10.0, 1), (-50.0, 0)])
    try:
        tdb = TradeDB(path)
        tdb.start()
        pe = _paper(tdb)
        tdb.stop()
        assert pe.balance == pytest.approx(110.0), (
            u"纸面余额 %.2f，正确值 110.0 —— 实盘的亏损被算进了纸面账户"
            % pe.balance)
    finally:
        os.unlink(path)


def test_paper_balance_excludes_live_profit_too():
    """反向：实盘【赚】的钱也不能算进纸面。"""
    path = _db([(10.0, 1), (500.0, 0)])
    try:
        tdb = TradeDB(path)
        tdb.start()
        pe = _paper(tdb)
        tdb.stop()
        assert pe.balance == pytest.approx(110.0), (
            u"纸面余额 %.2f —— 实盘赚的钱被算进了纸面账户" % pe.balance)
    finally:
        os.unlink(path)


def test_realized_pnl_default_keeps_all_trades():
    """paper=None 保持全量语义（诊断脚本依赖它）。"""
    path = _db([(10.0, 1), (-50.0, 0)])
    try:
        tdb = TradeDB(path)
        tdb.start()
        try:
            assert tdb.realized_pnl() == pytest.approx(-40.0)
            assert tdb.realized_pnl(paper=1) == pytest.approx(10.0)
            assert tdb.realized_pnl(paper=0) == pytest.approx(-50.0)
        finally:
            tdb.stop()
    finally:
        os.unlink(path)
