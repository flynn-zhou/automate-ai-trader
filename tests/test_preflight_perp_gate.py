# -*- coding: utf-8 -*-
"""永续口径下「够不够开一手」必须按真实门槛判，不能只看 minNotional。

2026-11-02 实测：BTCUSDT 永续 minNotional=50，但 minQty=0.001 手 = 78.2U。
只比 minNotional 会得出「100U 很宽裕」的结论，而实际一手吃掉 78% 权益。
这条陷阱的代价：纸面账户 71.77U 时引擎每轮都在「资金不足」里空转，
而体检报告显示 GO。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ai_trader.config import load          # noqa: E402
import preflight_live as pf                # noqa: E402

PX = 78250.0


class _Ex:
    name = "paper"


class _Feed:
    last_price = PX


def _run(equity, is_spot=False):
    cfg = load()
    cfg.trading.exec_market = "spot" if is_spot else "perp"
    cfg.trading.strategy = "trend"
    pf.results[:] = []
    spec = ({"min_qty": 0.00001, "min_notional": 5.0, "base": "BTC"}
            if is_spot else
            {"min_qty": 0.001, "min_notional": 50.0, "base": "BTC"})
    eq = {"equity": equity, "balance": equity, "position_qty": 0.0}
    pf.check_funds_and_plan(cfg, cfg.trading, PX, spec, eq, _Feed(), _Ex())
    return {name: (level, detail) for level, name, detail, _fix in pf.results}


def test_lot_price_is_the_real_gate_not_min_notional():
    """一手价 78 大于 minNotional 50 时，报告里的一手门槛必须是 78。

    写成 minNotional 的话，79U 的账户会被判「够开一手」，
    而实际一手都开不出来——体检结论和现实正好相反。
    """
    r = _run(100.0)
    _lvl, detail = r["最小一手名义额"]
    assert "78" in detail, "一手名义额必须按 minQty×价 算，实际：%r" % detail


def test_account_below_one_lot_is_blocked():
    """71U（买不起一手 78U）必须在「实际计划」这一项报 NO-GO。

    这就是纸面账户当时的真实处境：不报的话，运维看到一路 GO 会以为能交易。
    """
    r = _run(71.0)
    lvl, detail = r["按当前权益的实际计划"]
    assert lvl == pf.BAD, "71U 买不起一手却只报了 %s（%s）" % (lvl, detail)


def test_account_above_one_lot_is_not_blocked():
    """配对用例：100U 能买一手，同一项必须是 GO。

    只测「71U 被拦」的话，一个恒返回 BAD 的实现也能通过——
    那样任何账户都切不了 live。
    """
    r = _run(100.0)
    lvl, detail = r["按当前权益的实际计划"]
    assert lvl == pf.OK, "100U 能买一手却报了 %s（%s）" % (lvl, detail)


def test_thin_headroom_is_a_warning_not_a_hard_block():
    """100U 对 78U 门槛：余量 22% < 实测回撤，必须提醒。

    但【不能】升级成硬阻断：回测里 100U 一手满仓跑完 7.15 年是活下来的
    （168U / 回撤 24.4%），真正开不出仓的处境由「实际计划」那一项拦。
    把提醒当阻断，等于给一个能跑的策略判死刑。
    """
    r = _run(100.0)
    lvl, detail = r["回撤余量"]
    assert lvl == pf.WARN, "100U 的余量被判成 %s（%s）" % (lvl, detail)
    assert "停摆" in detail or "开不出" in detail, detail


def test_spot_keeps_its_much_lower_gate():
    """现货门槛仍按 5U 最小名义，不能被永续的 78U 带偏。

    这条防的是「把永续逻辑顺手也套到现货上」——现货一手只有 0.78U，
    套上 78U 门槛会让本该能跑的 10U 现货账户被判死。
    """
    r = _run(10.0, is_spot=True)
    _lvl, detail = r["最小一手名义额"]
    assert "0.78" in detail or "0.79" in detail, \
        "现货一手应该约 0.78U，实际：%r" % detail

def test_the_gate_line_uses_lot_price_when_it_is_larger():
    """「够不够开一手」这一行必须用 max(一手价, minNotional)。

    这条是补上来的：上一版我断言的是「最小一手名义额」那一行，
    而那一行用的是 min_lot_notional，和 need 这个变量无关——
    所以把 need 改成只看 minNotional，测试照样全绿（变异存活）。
    断言打偏位置，比没有断言更危险：它给人一种"已经守住了"的错觉。

    判据：79U 时，若 need 正确取 78.2，则 79>78.2 -> GO；
    若 need 错取 50，则任何大于 50 的账户都 GO——
    所以必须用一个【落在两者之间】的权益来区分，这里用 60U。
    """
    r = _run(60.0)
    lvl, detail = r["够不够开一手"]
    assert lvl == pf.BAD, (
        "60U 买不起一手（78.2U）却报了 %s（%s）—— 门槛很可能只看了 minNotional 50"
        % (lvl, detail))
    assert "78" in detail, "门槛说明里应出现真实一手价 78，实际：%r" % detail


def test_the_gate_line_passes_above_one_lot():
    """配对：100U 明显够一手，这一行必须是 GO，不能恒报 BAD。"""
    r = _run(100.0)
    lvl, detail = r["够不够开一手"]
    assert lvl in (pf.OK, pf.WARN), "100U 够一手却报了 %s（%s）" % (lvl, detail)


def test_headroom_reports_how_much_room_is_left():
    """回撤余量必须把「还能亏多少」说成百分数，而不只是说"不够"。"""
    r = _run(100.0)
    _lvl, detail = r["回撤余量"]
    assert "余量" in detail and "%" in detail, detail
