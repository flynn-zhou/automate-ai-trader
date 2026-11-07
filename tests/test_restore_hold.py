# -*- coding: utf-8 -*-
"""重启恢复：持仓上限必须来自库里记录的 value，不能被默认值覆盖。

2026-11-01 事故：一笔按 18 小时(21600 根)设计的单，进程重启后被当成 600 根强平，
平掉时它还在正常轨道上。这里钉死这个行为。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trade_db import TradeDB


def test_open_trade_returns_max_hold_bars():
    d = tempfile.mkdtemp()
    db = TradeDB(os.path.join(d, "t.db"))
    db.start()
    try:
        db.log_trade({"symbol": "BTCUSDT", "side": "long", "qty": 0.01,
                      "entry_ts": 1000, "entry_price": 100.0,
                      "stop_price": 99.0, "target_price": 102.0,
                      "max_hold_bars": 21600, "fee": 0.05})
        import time
        time.sleep(0.9)
        ot = db.open_trade()
        assert ot is not None
        assert ot["max_hold_bars"] == 21600
    finally:
        db.stop()
