# -*- coding: utf-8 -*-
"""执行器熔断必须有人知道。

上一轮修掉了「熔断会把止损一起关掉」，但留了一个洞没补：
  halted 是执行器的内存状态，sentinel 只读数据库，健康自检只看进程和 K 线。
  于是熔断发生时【没有任何人会收到通知】——只能靠人去打开看板。
  7x24 运行不会有人一直盯着看板。

所以引擎必须自己盯这个状态，并在跳变时走告警通道
（risk_events + journal + 飞书，即 Guard._event 那条路）。

三条要求：
  1. 第一次熔断必须立刻告警；
  2. 同一原因反复触发不能刷屏（否则告警会被自己淹没，等于没有）；
  3. 恢复也必须告警——否则人以为还坏着，会去查一个已经好了的系统。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.trading.engine import DecisionEngine


class Feed:
    src = "ws"

    def __init__(self, px=78000.0):
        self.px = px
        self.last_price = px
        self.agg = self
        self.bar_hooks = []
        self.cfg = AppCfg()

    def current(self, iv):
        px = self.px
        return type("B", (), {"high": px, "low": px, "close": px,
                              "open": px, "closed": True})()

    class _Hub:
        def publish(self, m):
            pass

    hub = _Hub()


class Ex:
    """带熔断状态的执行器替身。"""

    name = "spot"

    def __init__(self):
        self.halted = ""
        self.halted_ts = 0.0

    async def equity(self):
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
                "position_qty": 0.0}

    async def position(self):
        return {"qty": 0.0, "entry": 0.0}

    async def market_order(self, side, qty, reason="", reduce_only=False):
        raise AssertionError("这个测试不该下单")


class DB:
    def __init__(self):
        self.risks = []
        self.decisions = []
        self.trades = []
        self.equities = []

    def log_risk(self, kind, detail):
        self.risks.append((kind, detail))

    def log_decision(self, d):
        self.decisions.append(d)

    def log_equity(self, e):
        self.equities.append(e)

    def log_trade(self, t):
        self.trades.append(t)

    def log_order(self, o): pass
    def log_fill(self, f): pass
    def close_trade(self, t): pass
    def open_trade(self): return None
    def save_executor(self, s): pass
    def last_equity(self): return None
    def peak_equity(self, strategy=None, paper=None): return 100.0


class Journal:
    def __init__(self):
        self.rows = []

    def add(self, kind, title, body, data=None):
        self.rows.append((kind, title, body))


def build():
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    db = DB()
    jr = Journal()
    sent = []
    g = Guard(cfg, tdb=db, journal=jr, notify=lambda t, b: sent.append((t, b)))
    ex = Ex()
    feed = Feed()
    eng = DecisionEngine(cfg, feed, ex, db, guard=g)
    eng._seed_retry_ts = 2 ** 31        # 别让它去 REST 补种子
    return eng, ex, g, db, jr, sent


def test_first_halt_alerts_immediately():
    eng, ex, g, db, jr, sent = build()
    ex.halted = "余额核对失败：期望 0.001，实际 0.0"
    eng._watch_executor_halt()
    kinds = [k for k, _ in db.risks]
    assert "executor_halted" in kinds, (
        u"熔断没有被记进 risk_events：%r" % (kinds,))
    assert sent, u"熔断没有推送通知，7x24 运行不会有人知道"
    # 标题和正文都要能让人读懂：标题是给人看的，kind 只是机器标识
    joined = " ".join(t + " " + b for t, b in sent)
    assert u"熔断" in joined, sent
    assert "executor_halted" not in sent[0][0], (
        u"推送标题是代码标识符 %r，收到通知的人看不懂" % (sent[0][0],))


def test_repeat_same_halt_does_not_spam():
    eng, ex, g, db, jr, sent = build()
    ex.halted = "余额核对失败：期望 0.001，实际 0.0"
    for _ in range(6):
        eng._watch_executor_halt()
    n = len([1 for k, _ in db.risks if k == "executor_halted"])
    assert n == 1, (
        u"同一原因的熔断被重复告警 %d 次。刷屏会把真正的告警淹掉。" % n)
    assert len(sent) == 1, u"通知被重复推送 %d 次" % len(sent)


def test_changed_reason_is_rate_limited():
    """熔断原因里带动态数字，不限流就会每根 3s K 线报一次。

    要求：第一次立刻报；之后原因变了，5 分钟内不再报。
    """
    eng, ex, g, db, jr, sent = build()
    ex.halted = "余额核对失败：期望 0.0010，实际 0.0"
    eng._watch_executor_halt()
    assert len(sent) == 1
    for i in range(1, 6):
        ex.halted = "余额核对失败：期望 0.0010，实际 %d" % i
        eng._watch_executor_halt()
    assert len(sent) == 1, (
        u"原因里换个数字就重复推送了 %d 次，告警会把自己淹没" % len(sent))


def test_recovery_alerts():
    eng, ex, g, db, jr, sent = build()
    ex.halted = "余额核对失败"
    eng._watch_executor_halt()
    ex.halted = ""                     # 过期或被清除
    eng._watch_executor_halt()
    bodies = " ".join(b for _, b in sent)
    assert u"恢复" in bodies or u"解除" in bodies, (
        u"熔断解除没有通知，人还会以为坏着：%r" % (sent,))


def test_paper_executor_never_alerts():
    """纸面执行器没有 halted 属性，监视器必须是惰性的。"""
    eng, ex, g, db, jr, sent = build()
    del ex.halted
    for _ in range(3):
        eng._watch_executor_halt()
    assert not [1 for k, _ in db.risks if k == "executor_halted"], (
        u"没有熔断概念的执行器被误报成熔断")
    assert not sent, u"纸面模式不该推送熔断通知"


@pytest.mark.asyncio
async def test_step_wires_the_watcher():
    """光有方法不够，_step 必须真的调用它，否则线上永远不会触发。"""
    eng, ex, g, db, jr, sent = build()
    ex.halted = "余额核对失败：期望 0.001，实际 0.0"
    await eng._step({})
    kinds = [k for k, _ in db.risks]
    assert "executor_halted" in kinds, (
        u"_step 没有调用熔断监视器，线上这段代码等于不存在。risk=%r" % (kinds,))
