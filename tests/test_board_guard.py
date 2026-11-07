# -*- coding: utf-8 -*-
"""大屏必须能看到「六道安全闸」的状态。

用户的诉求原话是：「我希望你能在大屏把你的交易因果表达出来，我要知道你在做什么」。

但大屏此前只展示趋势链（K线 -> SMA -> 趋势 -> 触发线），完全不提安全闸。
后果是一句【谎】：

    if (guard.can_open()) 会拦住开仓，但大屏仍然写
        动作：空仓等待
        开多触发线：涨到 82337.4（还需 +5.16%）并收盘确认

  这行字暗示「价格到了就会开」，而实际上不会开。
  数据源 DOWN、回撤熔断、连亏冷却、当日亏损超限——任何一种触发，
  用户都会看到同一句"涨到 X 就开"，然后困惑为什么没开。

"因果表达"如果漏掉了"为什么不能开"，那它就不是因果，只是愿望。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000


class _Feed:
    src = "WS"

    def __init__(self, px):
        self.px = px
        self.cfg = type("C", (), {"market": type("M", (), {
            "fapi_base": "http://x", "http_proxy": ""})()})()
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()

    def agg(self):
        return self

    def current(self, iv):
        return type("B", (), {"high": self.px, "low": self.px,
                              "close": self.px, "open": self.px})()


class _Ex:
    name = "paper"

    async def equity(self):
        return {"equity": 100.0}


class _DB:
    def log_order(self, d): pass
    def log_fill(self, d): pass
    def close_trade(self, d): pass
    def log_risk(self, k, m): pass
    def log_decision(self, d): pass


def _eng(guard=None):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    feed = _Feed(80000.0)
    e = DecisionEngine(cfg, feed, _Ex(), _DB(), guard=guard)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    e.trend.bars = [{"t": i * IV4H, "o": 100.0 + i, "h": 100.0 + i,
                     "l": 100.0 + i, "c": 100.0 + i, "v": 1.0} for i in range(8)]
    e.trend.seeded = True
    e.trend.need_reconcile = False
    return e, cfg


def test_state_exposes_guard_when_guard_blocks():
    """Guard 拦住开仓时，state 必须把"不能开"和原因带出来。"""
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    g = Guard(cfg)
    g.update(100.0, "DOWN", 1700000000000)      # 数据源 DOWN
    g.update(100.0, "DOWN", 1700060000000)      # 超过 30s 宽限
    ok, why = g.can_open()
    assert not ok, "构造前提：这个 Guard 必须先处于拦截状态"

    e, _ = _eng(guard=g)
    st = e.state()
    assert "guard" in st, "state() 里没有 guard —— 大屏无从知道安全闸的状态"
    assert st["guard"]["can_open"] is False, (
        u"guard 正在拦截，但 state 说可以开")
    assert why[:6] in (st["guard"]["block_reason"] or ""), (
        u"拦截原因没有传出来：state=%r" % st["guard"]["block_reason"])


def test_state_guard_says_can_open_when_healthy():
    """反向约束：健康时必须明确说「可以开」，不能一律报拦截。"""
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    g = Guard(cfg)
    g.update(100.0, "WS", 1700000000000)
    ok, _ = g.can_open()
    assert ok, "构造前提：健康 Guard 应允许开仓"

    e, _ = _eng(guard=g)
    st = e.state()
    assert st["guard"]["can_open"] is True
    assert st["guard"]["block_reason"] == ""


def test_state_without_guard_does_not_crash():
    """没接 guard 时不能崩，也不能谎称可以开仓。"""
    e, _ = _eng(guard=None)
    st = e.state()
    g = st.get("guard")
    assert g is not None, "没有 guard 时也应给出明确结构，而不是消失"
    assert g["can_open"] is None, (
        u"没有 guard 就不能断言「可以开」，应为 None（未知）")


def test_guard_block_reason_is_human_readable():
    """拦截原因必须是人话，能直接显示在大屏上。"""
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    g = Guard(cfg)
    # 步长必须 < 30s：第 113 轮起「引擎卡住」真的会触发，
    # 一步跳 60 秒会先撞上它，测的就不是数据源这道闸了。
    g.update(100.0, "DOWN", 1700000000000)
    g.update(100.0, "DOWN", 1700000018000)      # +18s
    g.update(100.0, "DOWN", 1700000033000)      # +33s（DOWN 累计 33s）
    e, _ = _eng(guard=g)
    reason = e.state()["guard"]["block_reason"]
    assert reason and len(reason) > 4, u"拦截原因太短或为空：%r" % reason
    assert any(w in reason for w in (u"数据源", u"DOWN", u"回撤", u"熔断", u"连亏")), (
        u"拦截原因看不出是什么闸：%r" % reason)
