# -*- coding: utf-8 -*-
"""执行器熔断：哨兵能发现（已实测），大屏也要看得见。

★ 这一条我先猜错了，记录下来：
我原以为哨兵会漏报，因为熔断文案「已熔断：余额核对失败：…」
一个关键词都不命中它 4.5 检查里的
    资金不足 / 下单失败 / 退避 / 权益数据过期 / 查询失败。
但真实引擎路径下，引擎记的是
    signal='trend_up', action='skip', reason='下单失败：已熔断：…'
哨兵的第一支条件 (signal LIKE 'trend%' AND action='skip') 直接命中。
实测 trend_blocked_2h=1，issues 有内容 —— 报警是好的。

第一次写的测试之所以"通过"，是因为我手工插了一行 action='skip'，
等于把结论先做出来了。改成跑真实引擎路径之后才看清事实。

所以本文档钉两件事：
  1) 回归：执行器熔断必须仍然能被哨兵数出来（防止将来改坏）。
  2) 大屏：熔断状态必须显示。哨兵每 2 小时吼一次，
     但用户盯着大屏时看到的仍是「空仓等待 / 涨到 X 就开」——
     和上一轮修的 guard 是同一个问题：状态对了，因果没说出来。
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.sentinel import Sentinel
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000
HALT = u"余额核对失败：期望 0.00128，实际 0.0"


class _Feed:
    src = "WS"

    def __init__(self, px):
        self.px = px
        self.last_price = px
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
    """现货执行器，已熔断。文案与真实代码一致（单层前缀）。"""
    name = "spot"

    def __init__(self, halted=HALT):
        self.halted = halted

    async def market_order(self, side, qty, reason="", reduce_only=False):
        if self.halted:
            return type("F", (), {"ok": False, "reason": "已熔断：" + self.halted,
                                  "price": 78000.0, "qty": 0.0, "fee": 0.0,
                                  "order_id": "", "status": "rejected"})()
        return type("F", (), {"ok": True, "price": 78000.0, "qty": qty,
                              "fee": 0.0, "order_id": "x", "status": "filled",
                              "reason": ""})()

    async def equity(self):
        return {"equity": 71.77, "balance": 71.77, "position_qty": 0.0,
                "unrealized": 0.0}


def _eng(db, ex):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    e = DecisionEngine(cfg, _Feed(100.0), ex, db, guard=None)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    # 造"上穿"：先横盘、再跌破、最后一根拉起
    closes = [100.0] * 6 + [90.0] * 3 + [120.0]
    e.trend.bars = [{"t": i * IV4H, "o": c, "h": c, "l": c, "c": c, "v": 1.0}
                    for i, c in enumerate(closes)]
    e.trend.seeded = True
    e.trend.need_reconcile = False
    e.pos = None
    return e


def test_halted_executor_still_visible_to_sentinel():
    """回归：执行器熔断必须仍然被哨兵数出来。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    try:
        e = _eng(tdb, _Ex())
        now = int(time.time() * 1000)
        asyncio.new_event_loop().run_until_complete(
            e._step_trend({}, 120.0, 100.0, now))
        time.sleep(0.8)          # 等单写入队列落盘

        fd2, mpath = tempfile.mkstemp(suffix=".db")
        os.close(fd2)
        m = sqlite3.connect(mpath)
        m.execute("CREATE TABLE bars (interval_ms INTEGER, open_time INTEGER,"
                  " o REAL, h REAL, l REAL, c REAL, v REAL)")
        m.execute("INSERT INTO bars VALUES (3000, ?, 1,1,1,1,1)", (now,))
        m.commit()
        m.close()
        try:
            n = Sentinel(path, mpath).check()["facts"].get("trend_blocked_2h") or 0
            assert n >= 1, (
                u"哨兵数出 %d 条阻塞。执行器熔断后所有下单都失败，"
                u"但如果哨兵看不见，系统表现和「在等信号」一模一样。" % n)
        finally:
            os.unlink(mpath)
    finally:
        tdb.stop()
        os.unlink(path)


def test_state_exposes_executor_halt():
    """大屏必须能看出执行器已熔断。"""
    e = _eng(type("D", (), {"open_trade": lambda self: None,
                            "log_decision": lambda self, d: None,
                            "log_order": lambda self, d: None,
                            "log_fill": lambda self, d: None,
                            "close_trade": lambda self, d: None,
                            "log_risk": lambda self, k, m: None,
                            "log_trade": lambda self, d: None,
                            "save_executor": lambda self, d: None})()
              , _Ex())
    st = e.state()
    ex = st.get("executor") or {}
    assert ex.get("halted"), (
        u"state() 里看不出执行器已熔断：%r。"
        u"用户盯着大屏只看到「空仓等待 / 涨到 X 就开」，"
        u"而实际上永远不会开。" % ex)
    assert u"余额核对失败" in (ex.get("reason") or "")


def test_state_shows_no_halt_when_healthy():
    """反向约束：健康时不能谎报熔断。"""
    e = _eng(type("D", (), {"open_trade": lambda self: None,
                            "log_decision": lambda self, d: None,
                            "log_order": lambda self, d: None,
                            "log_fill": lambda self, d: None,
                            "close_trade": lambda self, d: None,
                            "log_risk": lambda self, k, m: None,
                            "log_trade": lambda self, d: None,
                            "save_executor": lambda self, d: None})(),
              _Ex(halted=""))
    ex = e.state().get("executor") or {}
    assert ex.get("halted") is False, u"健康的执行器被报成熔断：%r" % ex
    assert ex.get("reason") == ""
