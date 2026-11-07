# -*- coding: utf-8 -*-
"""永续下单也必须留手续费缓冲，否则信号来了却下不出去。

缺陷：_fee_buffer() 对永续返回 0，于是名义额可以等于全部权益。
1x 下保证金 = 名义额，再加 taker 费，交易所会以「保证金不足」拒单。
100U 账户靠 step 取整侥幸躲过（0.001 手 = 78U），
但权益落在 step 整数倍上时就踩到——例如 780.15U 时 floor 出 0.01 手。

这不是理论：趋势信号一年只有几十次，正好在那一刻被拒，
等于错过一整段行情，而且日志里只会看到一条下单失败。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading.engine import DecisionEngine


class _Feed:
    src = "WS"
    last_price = 78015.0

    def __init__(self, cfg):
        self.cfg = cfg
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()


class _PerpEx:
    name = "live"          # 永续执行器（main.py 里 LiveExecutor 的 name）


class _SpotEx:
    name = "spot"          # 现货执行器（SpotExecutor.name）


def _eng(market="perp"):
    """注意：_is_spot() 以【执行器 name】为准，不是 exec_market 配置。

    engine.py:791 解释过原因：以真正下单的那个执行器为准，
    否则配置写 spot、执行器却是永续时，仓位计算会自相矛盾。
    所以测试必须同时给对执行器——只改配置的话，
    spot 用例实际走的是永续分支，会"通过得不对"。
    """
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "perp" if market == "perp" else "spot"
    ex = _PerpEx() if market == "perp" else _SpotEx()
    eng = DecisionEngine(cfg, _Feed(cfg), ex, None, guard=None)
    return eng


def test_perp_fee_buffer_is_not_zero():
    """永续的缓冲不能是 0。"""
    eng = _eng("perp")
    assert eng._fee_buffer() > 0, (
        "永续缓冲为 0：名义额可以吃满权益，保证金+手续费必然不足")
    assert eng._fee_buffer() < eng._fee_buffer() + 1   # 平凡但防止被改成负数
    spot = _eng("spot")
    assert spot._fee_buffer() > eng._fee_buffer(), (
        "现货缓冲应大于永续（现货手续费更高）")


def test_perp_plan_leaves_room_for_fee():
    """权益正好是 step 整数倍时，计划名义额必须严格小于权益。"""
    eng = _eng("perp")
    price = 78015.0
    # 780.15 / 78015 = 0.01 BTC，正好是 0.001 的整数倍
    equity = price * 0.01
    plan = eng._trend_plan(price, equity, {"name": "trend_entry"})
    assert plan.ok, "计划应当成立：%s" % plan.reason
    assert plan.notional < equity, (
        "名义额 %.2f 吃满了全部权益 %.2f —— 1x 下保证金就等于名义额，"
        "再加上手续费会被交易所按保证金不足拒单" % (plan.notional, equity))
    room = equity - plan.notional
    fee = plan.notional * 0.0005          # 永续 taker 0.05%
    assert room > fee, (
        "留给手续费的空间 %.4f 不够付 %.4f" % (room, fee))


def test_spot_plan_leaves_more_room():
    """现货同理，而且留得更多（手续费 0.1%）。"""
    eng = _eng("spot")
    price = 78015.0
    equity = price * 0.01
    plan = eng._trend_plan(price, equity, {"name": "trend_entry"})
    assert plan.ok, plan.reason
    assert plan.notional < equity
    room = equity - plan.notional
    assert room > plan.notional * 0.001, (
        "现货留给手续费的空间 %.4f 不够付 0.1%%" % room)
