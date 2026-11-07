# -*- coding: utf-8 -*-
"""100U 账户的生死线：多小的权益还能建仓，以及现货 vs 永续差多少。

为什么需要（2026-11-02，第 97 轮）：
  回测假设「永远能满仓交易」。实盘有最小下单限制，账户小到一定程度就【永久】无法建仓。
  这不是亏损，是策略结构性失效 —— 而且不报错、不告警，只是永远不成交。

  实测（BTC 77749）：
    现货：最小可交易权益 5.5U（一手 0.00001 BTC = 0.78U，最小名义 5U）
    永续：最小可交易权益 77.9U（一手 0.001 BTC = 77.75U，最小名义 55U）

  ★ 所以 100U 用现货，真正的理由【不是资金费率，是手数】：
    永续一手就吃掉 78% 的账户，100U 根本没法正常建仓；
    而且回撤 22% 就永久停摆，而策略实测最大回撤是 46.1%。
    资金费率（11.46%/年 vs 6.22%/年）是第二位的理由。

  这两个数字必须钉住：任何人改 min_notional / qty_step / 手续费缓冲，
  让 100U 账户变得不可交易，这里要立刻红。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load
from ai_trader.trading.engine import DecisionEngine


class _Feed:
    last_price = 77749.0


def _engine(exec_market):
    """只在【测试对象】上切 exec_market，不动真实配置。"""
    cfg = load()
    cfg.trading.exec_market = exec_market
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = cfg
    eng.tcfg = cfg.trading
    eng.feed = _Feed()
    eng.ex = type("E", (), {"name": "paper"})()
    return eng


def _min_tradeable_equity(eng, price, hi=30000.0):
    """二分找最小的、能建仓的权益（0.1U 精度，和挂单取整一致）。"""
    lo, hi = 0.0, hi
    if not eng._trend_plan(price, hi, {"name": "x", "reason": "y"}).ok:
        raise AssertionError("权益 %.0fU 都建不了仓，规格有问题" % hi)
    for _ in range(60):
        mid = (lo + hi) / 2.0
        p = eng._trend_plan(price, mid, {"name": "x", "reason": "y"})
        if p.ok:
            hi = mid
        else:
            lo = mid
    return hi


def test_spot_100u_account_is_comfortably_tradeable():
    """现货下 100U 能建仓，而且资金使用率接近满仓（回测假设的就是这个）。"""
    eng = _engine("spot")
    price = 77749.0
    plan = eng._trend_plan(price, 100.0, {"name": "x", "reason": "y"})
    assert plan.ok, "100U 现货建不了仓：%s" % plan.reason
    assert plan.notional >= 99.0, (
        "100U 只用了 %.2fU，远低于回测假设的满仓；回测数字会系统性偏高"
        % plan.notional)
    assert plan.notional <= 100.0 + 1e-6, "超配：%.2fU" % plan.notional


def test_spot_floor_is_about_5u_not_55u():
    """现货的生死线在 5~7U，不是永续那个 55U。

    这条防的是「把永续的最小名义 55U 误用到现货上」——
    那样 100U 账户回撤 45% 就永久停摆，而策略的最大回撤就是 46%。
    """
    eng = _engine("spot")
    for price in (30000.0, 77749.0, 120000.0):
        floor = _min_tradeable_equity(eng, price)
        assert 4.5 <= floor <= 7.0, (
            "价格 %.0f 时现货生死线 %.2fU 落在 5~7U 之外" % (price, floor))


def test_perp_cannot_handle_a_100u_account():
    """永续在 100U 上结构性不可用 —— 这不是「更贵」，是「做不了」。

    一手 0.001 BTC ≈ 77.75U，占 100U 的 78%；
    权益低于约 77.9U（回撤 22%）就永久无法建仓。
    这条是「100U 为什么必须用现货」的证据，比资金费率更根本。
    """
    eng = _engine("perp")
    price = 77749.0
    floor = _min_tradeable_equity(eng, price, hi=2000.0)
    assert floor > 70.0, (
        "永续生死线 %.2fU 低于预期；若真的降下来了，说明手数/最小名义改了，"
        "100U 用现货的理由要重新论证" % floor)
    # 100U 在永续下虽然能开，但只用掉 78%，且回撤到 78U 以下就彻底停摆
    plan = eng._trend_plan(price, 100.0, {"name": "x", "reason": "y"})
    assert plan.ok
    assert plan.notional < 80.0, (
        "永续 100U 居然用掉 %.2fU，与「一手 77.75U」的推算不符" % plan.notional)
    # 回撤 22% 就没了 —— 比策略实测最大回撤 46.1% 更早到
    dead = eng._trend_plan(price, floor * 0.95, {"name": "x", "reason": "y"})
    assert not dead.ok, "权益 %.1fU 居然还能建仓" % (floor * 0.95)

def test_plan_leaves_room_for_the_taker_fee():
    """计划名义必须给手续费留出空间，否则下单会被交易所按余额不足拒掉。

    为什么单独一条：`qty = floor(...)` 本身就把名义压在权益之内，
    所以「不超配」是取整保证的，**不是因为留了缓冲**。
    缓冲真正要防的是【买完再扣 taker 费就负了】：
        notional + notional*taker_fee > equity   -> 拒单
    把缓冲改成 0，这条要红；只查「不超配」是查不出来的。
    """
    eng = _engine("spot")
    fee = float(eng.tcfg.spot_taker_fee)
    for price in (30000.0, 77749.0, 120000.0):
        for equity in (100.0, 257.0, 1275.0):
            plan = eng._trend_plan(price, equity, {"name": "x", "reason": "y"})
            assert plan.ok
            need = plan.notional * (1.0 + fee)
            assert need <= equity + 1e-6, (
                "价格 %.0f 权益 %.0f：名义 %.2fU 加上 taker 费共需 %.2fU，"
                "超出权益 %.2fU，下单会被拒"
                % (price, equity, plan.notional, need, need - equity))
