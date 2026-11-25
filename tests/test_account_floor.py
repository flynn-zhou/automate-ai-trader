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
    eng = _engine("perp")
    fee = float(eng.tcfg.taker_fee)      # ★ 2026-11-17 只做合约：用永续 taker
    checked = 0
    for price in (30000.0, 77749.0, 120000.0):
        for equity in (100.0, 257.0, 1275.0):
            plan = eng._trend_plan(price, equity, {"name": "x", "reason": "y"})
            if not plan.ok:
                # ★ 不是每个组合都开得出来了（例：价 120000 时一手就要 120U，
                #   而 trend_leverage=1x）。不许拿这个当借口放掉下面的断言 ——
                #   `checked` 计数保证「至少真的验过几个组合」，否则这条测试会空转。
                continue
            checked += 1
            need = plan.notional * (1.0 + fee)
            assert need <= equity + 1e-6, (
                "价格 %.0f 权益 %.0f：名义 %.2fU 加上 taker 费共需 %.2fU，"
                "超出权益 %.2fU，下单会被拒"
                % (price, equity, plan.notional, need, need - equity))
    assert checked >= 4, (
        "只有 %d 个组合真的跑到断言（共 9 个）—— 太少就等于这条测试在空转"
        % checked)
