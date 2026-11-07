# -*- coding: utf-8 -*-
"""仓位取整必须【向下】——永不超过权益。

原实现用 round()，会向上超配：实测 180 次开仓里有几次资金使用率达到 131%，
也就是标称 1x 名义实际是 1.31x。
后果：现货被执行器按可用余额截断（记账与实际不符），永续可能直接因余额不足被拒。

改成向下取整后：最高恰好 100.0%，超配次数 0。
代价是平均使用率 90%（渐进到 0.001 BTC 的整数倍），这是欠配——安全的方向。
现货步长 0.00001 时能到 99.9%。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load
from ai_trader.trading.engine import DecisionEngine


class _Feed:
    last_price = 77749.0


def _engine():
    cfg = load()
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = cfg
    eng.tcfg = cfg.trading
    eng.feed = _Feed()
    eng.ex = type("E", (), {"name": "paper"})()
    return eng


@pytest.mark.parametrize("price", [77749.0, 30000.0, 120000.0, 5432.1])
def test_plan_never_exceeds_equity(price):
    """任何价格下，计划名义都不能超过权益（1x）。"""
    eng = _engine()
    for equity in (100.0, 257.0, 1000.0, 75.0):
        plan = eng._trend_plan(price, equity, {"name": "trend_up", "reason": "x"})
        if not plan.ok:
            continue
        assert plan.notional <= equity + 1e-6, (
            "价格 %.0f 权益 %.0f 时超配：名义 %.2f" % (price, equity, plan.notional))
        assert plan.leverage <= 1.0 + 1e-6, (
            "价格 %.0f 权益 %.0f 时杠杆 %.3fx 超过 1x" % (price, equity, plan.leverage))


def test_plan_qty_is_floor_not_round():
    """同一价格下，取整必须是向下——四舍五入会让某些权益值超配。"""
    eng = _engine()
    price = 77749.0
    # 找一个用 round 会超配的权益值：让 qty/step 的小数部分 > 0.5
    step = eng._qty_step()
    bad = 0
    for equity in range(60, 4000, 7):
        plan = eng._trend_plan(price, float(equity), {"name": "trend_up", "reason": "x"})
        if plan.ok and plan.notional > equity + 1e-6:
            bad += 1
    assert bad == 0, "有 %d 个权益值出现超配" % bad
