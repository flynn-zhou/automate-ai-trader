# -*- coding: utf-8 -*-
"""公式止损：初始止损 = d_max - 成本，止盈 = 4 x d_max。

用户 2026-11-17 口径：「下回公式变了之后，22X 变成 33X，那就是固定 33X，
止损最远假如公式算出来是 2.3%，那就是 2.3% 的位置挂止损，完全按照公式来。」

所以这里钉的不是「1.618%」这个数，而是**它由公式推出、且随输入变化**。
写死字面量的实现必须被这些测试抓住。
"""
import pytest

from ai_trader.trading import pending_plan as P
from ai_trader.trading import sizing
from ai_trader.trading import thesis as T

PX = 78000.0
H1 = 355.3
A5 = 113.3


def _dmax(leverage=22.0, safety=2.5, mmr=0.005):
    # \u2605 \u4e0d\u5728\u6d4b\u8bd5\u91cc\u91cd\u5199\u516c\u5f0f \u2014\u2014 \u8d70\u751f\u4ea7\u90a3\u4e2a\u51fd\u6570\u3002
    c = sizing.SizingConfig(capital=100.0, risk_pct=10.0,
                            leverage=leverage,
                            maintenance_margin_rate=mmr,
                            safety_multiple=safety)
    return sizing.derive(c).max_stop_distance_pct


def _plan(max_stop_pct, order_type=T.ORDER_LIMIT, px=PX):
    return P.plan_prices(px, P.DOWN, H1, A5,
                         P.PendingParams(max_stop_pct=max_stop_pct),
                         order_type=order_type)


def test_default_is_still_the_amplitude_rule():
    """`max_stop_pct=0` 时保持旧的振幅规则 \u2014\u2014 历史单测与对比实验依赖它。"""
    pp = _plan(0.0)
    assert abs(pp.stop_distance - max(H1 * 0.25, A5)) < 1e-6
    assert pp.evidence["stop_rule"].startswith("amplitude")


def test_formula_stop_is_dmax_minus_cost():
    """\u2605\u2605 \u6b62\u635f = d_max \u2212 \u6210\u672c\u3002\u4e0d\u662f d_max\u3002

    \u56e0\u4e3a d_max \u662f\u300c\u6700\u574f\u4e8f R\u300d\u7684**\u603b\u8ddd\u79bb**\uff0c\u800c\u603b\u8ddd\u79bb = \u6b62\u635f + \u6210\u672c\u3002
    \u5982\u679c\u6b62\u635f\u76f4\u63a5\u6302\u5728 d_max\uff0c\u90a3\u4e48\u6b62\u635f+\u6210\u672c \u5c31\u4f1a\u8d85\u8fc7 d_max\uff0c
    `plan_position()` \u7684\u7206\u4ed3\u5b89\u5168\u6821\u9a8c\u4f1a\u628a**\u6bcf\u4e00\u7b14**\u90fd\u62d2\u6389\u3002
    """
    dmax = _dmax()
    pp = _plan(dmax)
    _cw, cost_pts = P.round_trip_costs(PX, T.ORDER_LIMIT, P.PendingParams())
    cost_pct = cost_pts / PX * 100.0
    assert cost_pct > 0, "\u6210\u672c\u4e0d\u80fd\u4e3a 0\uff0c\u5426\u5219\u8fd9\u6761\u6d4b\u8bd5\u662f\u7a7a\u8f6c\u7684"
    assert abs(pp.stop_price - PX) / PX * 100.0 == pytest.approx(
        dmax - cost_pct, abs=2e-3)
    # \u6b62\u635f + \u6210\u672c \u6070\u597d\u56de\u5230 d_max
    assert (abs(pp.stop_price - PX) / PX * 100.0 + cost_pct) == pytest.approx(
        dmax, abs=2e-3)


def test_target_is_four_times_dmax():
    """\u6b62\u76c8 = 4 \u00d7 d_max\u3002\u4e24\u6761\u7ebf\u540c\u4e00\u4e2a\u57fa\u51c6\uff0c4R \u624d\u662f\u771f\u7684 4 \u500d\u98ce\u9669\u3002"""
    dmax = _dmax()
    pp = _plan(dmax)
    tgt_pct = abs(pp.target_price - PX) / PX * 100.0
    assert tgt_pct == pytest.approx(4.0 * dmax, abs=2e-3)


def test_stop_follows_the_configured_leverage_not_a_literal():
    """\u2605\u2605 \u6760\u6746\u53d8\u4e86\uff0c\u6b62\u635f\u5fc5\u987b\u81ea\u52a8\u8ddf\u7740\u53d8 \u2014\u2014 \u4ee3\u7801\u91cc\u4e0d\u8bb8\u5199\u6b7b 1.618\u3002

    \u7528\u6237\u539f\u8bdd\uff1a\u300c\u4e0b\u56de\u516c\u5f0f\u53d8\u4e86\u4e4b\u540e\uff0c22X \u53d8\u6210 33X\uff0c\u90a3\u5c31\u662f\u56fa\u5b9a 33X\u300d\u3002
    \u8fd9\u6761\u7528 22x \u4e0e 33x \u4e24\u4e2a\u8f93\u5165\u53bb\u9a8c\u8bc1\u8f93\u51fa\u786e\u5b9e\u4e0d\u540c\u3002
    """
    d22 = _dmax(leverage=22.0)
    d33 = _dmax(leverage=33.0)
    assert d22 != d33, "\u6760\u6746\u53d8\u4e86 d_max \u5374\u6ca1\u53d8\uff0c\u8bf4\u660e\u5199\u6b7b\u4e86\u5b57\u9762\u91cf"
    # \u6b62\u635f\u8ddd\u79bb\u5fc5\u987b\u8ddf\u7740\u4e24\u4e2a\u4e0d\u540c\u7684 d_max \u8d70
    s22 = _plan(d22).stop_distance
    s33 = _plan(d33).stop_distance
    assert s22 != s33
    # 22x \u4e0b\u7206\u4ed3\u8ddd\u79bb\u66f4\u8fdc \u2192 \u5141\u8bb8\u7684\u6b62\u635f\u66f4\u5bbd
    assert s22 > s33, "22x \u5e94\u8be5\u6bd4 33x \u5141\u8bb8\u66f4\u5bbd\u7684\u6b62\u635f"


def test_cost_comes_from_one_place():
    """\u6210\u672c\u53e3\u5f84\u53ea\u80fd\u6709\u4e00\u4e2a\u6765\u6e90\uff1a`round_trip_costs()`\u3002

    \u4ee5\u524d `plan_prices()` \u81ea\u5df1\u7b97\u4e00\u4efd\uff0c\u5b9a\u4ed3\u90a3\u5c42\u62ff\u4e0d\u5230\u5b83 \u2192 \u53c8\u662f\u4e24\u4e2a\u6765\u6e90\u3002
    """
    _cw, cost_pts = P.round_trip_costs(PX, T.ORDER_LIMIT, P.PendingParams())
    pp = _plan(_dmax())
    assert pp.evidence["cost_loss_pct"] == pytest.approx(
        cost_pts / PX * 100.0, abs=1e-3)


def test_stop_type_entry_costs_more_than_limit():
    """STOP \u8fdb\u573a\u662f taker + \u6ed1\u70b9\uff0c\u6210\u672c\u5fc5\u987b\u9ad8\u4e8e LIMIT\u3002

    \u5426\u5219\u5c31\u662f\u628a\u8d39\u79cd\u5904\u7406\u6210\u4e86\u4e00\u4e2a\u5e38\u91cf\uff08\u5386\u53f2\u4e0a\u771f\u7684\u9519\u8fc7\uff09\u3002
    """
    cw_lim, cl_lim = P.round_trip_costs(PX, T.ORDER_LIMIT, P.PendingParams())
    cw_stp, cl_stp = P.round_trip_costs(PX, T.ORDER_STOP, P.PendingParams())
    assert cl_stp > cl_lim
    assert cw_stp > cw_lim


def test_dmax_too_small_for_cost_gives_zero_distance():
    """d_max \u8fde\u6210\u672c\u90fd\u76d6\u4e0d\u4f4f\u65f6\uff0c\u6b62\u635f\u8ddd\u79bb\u4e3a 0 \u2014\u2014 \u4e0d\u8bb8\u9759\u9ed8\u5f04\u6210\u8d1f\u6570\u3002"""
    _cw, cost_pts = P.round_trip_costs(PX, T.ORDER_LIMIT, P.PendingParams())
    tiny = cost_pts / PX * 100.0 / 2.0        # \u53ea\u6709\u6210\u672c\u7684\u4e00\u534a
    pp = _plan(tiny)
    assert pp.stop_distance == 0.0
    assert pp.evidence["stop_rule"].startswith("formula")

def test_boundary_stop_is_not_rejected_by_float_noise():
    """★★ 止损**恰好等于**上限时必须通过，不能被浮点噪音拒掉。

    仓位本来就是按 d_max 定的，所以「止损 + 成本 = d_max」是精确边界。
    `plan_prices` 把止损距离 round 到 2 位小数（78000 价上 = 6.4e-6%），
    如果容差比这个小，合规计划会被判成「超过上限」—— 实测 4/11 被拒。
    """
    from ai_trader.trading.sizing import SizingConfig, plan_position, derive
    cfg = SizingConfig(capital=100.0, risk_pct=10.0, leverage=22.0)
    d = derive(cfg)
    pp = plan_position(cfg, d.max_stop_distance_pct)
    assert pp.ok, "止损恰好等于上限却被拒：%r" % (pp.reasons,)
    # 稍微收窄也必须过
    pp2 = plan_position(cfg, d.max_stop_distance_pct - 1e-5)
    assert pp2.ok


def test_real_overshoot_is_still_refused():
    """反向对照：真实超限必须仍然被拒，容差不是放宽。

    没有这条，把两个比较都删掉也能让上面那条变绿。
    """
    from ai_trader.trading.sizing import SizingConfig, plan_position, derive
    cfg = SizingConfig(capital=100.0, risk_pct=10.0, leverage=22.0)
    d = derive(cfg)
    for extra in (0.01, 0.1, 1.0):
        pp = plan_position(cfg, d.max_stop_distance_pct + extra)
        assert not pp.ok, "超限 %.3f%% 竟然通过了" % extra
        assert pp.reasons
