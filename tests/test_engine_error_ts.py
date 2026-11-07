# -*- coding: utf-8 -*-
"""引擎报错必须带【时间】暴露出去，否则看板无法区分「正在坏」和「早就好了」。

2026-11-02：last_error 一直只有文本。一旦写入就永久留着，
于是一个三天前的网络抖动，和一次正在发生的引擎崩溃，
在 API 返回里长得一模一样。看板即使把它画出来也只能画成一个永远红着的条，
用户很快学会无视它——那和没显示没区别。
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine


class _Feed:
    src = "WS"
    last_price = 78000.0

    def __init__(self):
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self.agg = type("A", (), {"current": staticmethod(lambda iv: None)})()


class _Ex:
    name = "paper"

    async def equity(self):
        return {"balance": 100.0, "equity": 100.0, "position_qty": 0.0}


def _eng():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    eng = DecisionEngine(cfg, _Feed(), _Ex(), tdb, guard=None)
    return eng, tdb


def test_state_exposes_error_timestamp():
    eng, tdb = _eng()
    try:
        st = eng.state()
        assert "last_error_ts" in st, (
            "state() 没有暴露 last_error_ts，看板无从判断错误新旧")
        assert st["last_error_ts"] == 0
        assert st["last_error"] == ""
    finally:
        tdb.stop()


def test_step_exception_stamps_the_error_time():
    """主循环异常时，必须同时写下文本和时间。"""
    eng, tdb = _eng()
    try:
        before = int(time.time() * 1000)

        class _Boom:
            async def _step(self, bars):
                raise RuntimeError("division by zero")

        eng._step = _Boom()._step
        asyncio.get_event_loop().run_until_complete(eng._on_bar({}))

        st = eng.state()
        assert "division by zero" in st["last_error"], (
            "异常文本没记录：%r" % st["last_error"])
        assert st["last_error_ts"] >= before, (
            "错误时间戳没更新（%r），看板会把它当成陈年旧账"
            % st["last_error_ts"])
    finally:
        tdb.stop()


def test_reconcile_failure_also_stamps_time():
    """实盘对账失败同样要留时间：它意味着"不知道交易所里有没有仓"。"""
    eng, tdb = _eng()
    try:
        class _BadPos(_Ex):
            name = "spot"

            async def position(self):
                raise IOError("timeout")

        eng.ex = _BadPos()
        before = int(time.time() * 1000)
        asyncio.get_event_loop().run_until_complete(eng.reconcile_live())

        st = eng.state()
        assert "对账失败" in st["last_error"], st["last_error"]
        assert st["last_error_ts"] >= before, (
            "对账失败没有记时间戳，%r" % st["last_error_ts"])
    finally:
        tdb.stop()
