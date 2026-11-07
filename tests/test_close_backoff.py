# -*- coding: utf-8 -*-
"""平仓失败必须退避：止损是风险关键路径，但打满交易所会封 IP，封了就再也平不掉仓。

失败驱动：修复前实测【5 分钟内 100 次尝试 -> 折算每天 28,800 次】，
正好是 _open 注释里警告的那个数字（"币安会按限频直接封 IP"）。
而 _close 当时完全没有退避：只有 _open 有。
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.base import FillResult
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine


class _Bar:
    def __init__(self, ot, o, h, l, c, v=1.0):
        self.interval_ms = 3000
        self.open_time = ot
        self.o = self.open = o
        self.h = self.high = h
        self.l = self.low = l
        self.c = self.close = c
        self.v = self.volume = v


class _Feed:
    src = "WS"

    def __init__(self):
        self.last_price = 78000.0
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self.agg = type("A", (), {"current": staticmethod(lambda iv: None)})()


class _FailingEx:
    """持仓永远平不掉。"""
    name = "spot"

    def __init__(self):
        self.attempts = 0

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.attempts += 1
        return FillResult(ok=False, reason="Account has insufficient balance")

    async def equity(self):
        return {"balance": 100.0, "equity": 100.0, "position_qty": 0.0}

    async def position(self):
        return {"qty": 0.0, "entry": None, "side": "long"}


def _engine(tdb, ex):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    eng = DecisionEngine(cfg, _Feed(), ex, tdb, guard=None)
    # 摆一个已经跌破止损的持仓
    eng.pos = {"side": "long", "qty": 0.00127, "entry": 78000.0,
               "stop": 60000.0, "target": 120000.0, "entry_ts": 0,
               "bars": 0, "best": 78000.0, "worst": 78000.0,
               "trade_id": None, "max_hold_bars": 600,
               "open_reason": "t", "fee": 0.0, "signal": "trend_up", "dust": 0.0}
    return eng


def _run(tmp_path_factory_unused=None):
    pass


def test_close_backoff_limits_retry_rate():
    """持续失败时，平仓尝试次数必须被退避压下来。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    try:
        ex = _FailingEx()
        eng = _engine(tdb, ex)

        async def drive():
            # 模拟 5 分钟：100 根 3s K 线，价格一直在止损下方
            for i in range(100):
                bar = _Bar(i * 3000, 59000.0, 59500.0, 58000.0, 58500.0)
                await eng._manage(58500.0, {3000: [bar]})

        asyncio.new_event_loop().run_until_complete(drive())

        # 没有退避时这里是 100 次（折算 28,800/天）
        assert ex.attempts <= 3, (
            "5 分钟内尝试了 %d 次平仓（折算每天 %d 次）。"
            "持续失败会把币安限频打满，封 IP 之后就再也平不掉仓了——"
            "止损失效比开不了仓严重得多" % (ex.attempts, ex.attempts * 288))
        assert ex.attempts >= 1, "一次都没试过，止损等于放弃了"
        # 退避状态必须被记录，否则"下次什么时候试"无从判断
        assert getattr(eng, "_close_retry_ts", 0) > 0, "没有记录退避时间戳"
        assert eng._close_fail_count >= 1
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_close_success_resets_backoff():
    """平仓成功必须把退避清零，否则下一次止损会白等。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    try:
        class _OkEx(_FailingEx):
            async def market_order(self, side, qty, reason="", reduce_only=False):
                self.attempts += 1
                return FillResult(ok=True, price=59000.0, qty=qty, fee=0.1,
                                  order_id="1", status="filled", reason="")

        ex = _OkEx()
        eng = _engine(tdb, ex)
        eng._close_fail_count = 5
        eng._close_retry_ts = 0

        async def drive():
            bar = _Bar(0, 59000.0, 59500.0, 58000.0, 58500.0)
            return await eng._manage(58500.0, {3000: [bar]})

        closed = asyncio.new_event_loop().run_until_complete(drive())
        assert closed is True, "止损没执行"
        # 异步入队，等一下库才看得到
        for _ in range(50):
            if eng.pos is None:
                break
            import time as _t
            _t.sleep(0.02)
        assert eng._close_fail_count == 0, "平仓成功后失败计数没清零"
        assert eng._close_retry_ts == 0, "平仓成功后退避没解除"
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass
