# -*- coding: utf-8 -*-
"""纸面执行器状态持久化：重启后余额/持仓不能丢，否则引擎与执行器会对不上。"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.trade_db import TradeDB


class FakeAgg:
    def current(self, ms):
        class B:
            close = 80000.0
        return B()


class FakeFeed:
    last_price = 80000.0
    agg = FakeAgg()


def test_executor_state_survives_restart():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")
    db = TradeDB(path)
    db.start()
    try:
        cfg = AppCfg()
        ex = PaperExecutor(FakeFeed(), cfg)
        ex.restore_from_db(db)
        asyncio.get_event_loop().run_until_complete(ex.market_order("sell", 0.001))
        # 等写线程落库
        import time
        time.sleep(0.9)
        ex2 = PaperExecutor(FakeFeed(), cfg)
        ex2.restore_from_db(db)
        assert abs(ex2.balance - ex.balance) < 1e-6
        assert abs(ex2.pos_qty - ex.pos_qty) < 1e-9
    finally:
        db.stop()
