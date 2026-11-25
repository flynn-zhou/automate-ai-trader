# -*- coding: utf-8 -*-
"""上线体检里的「回撤余量」必须真的会报警。

背景（第 97 轮）：
  账户小到一定程度，最小下单限制会让策略【永久】开不出仓。
  它不是亏损，是结构性失效 —— 不报错、不告警，只是永远不成交。
  实测 BTC 77749 时：现货生死线约 5.5U，永续约 77.9U。

  所以 preflight 里加了这一项。但「加了检查」和「检查有效」是两件事：
  这一轮我一度只加了代码、没有任何测试去逼它报警 ——
  那种检查最危险，因为它让人以为有人守着。

这个测试就是对它的失败测试：好账户必须 OK，小账户必须 WARN。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ai_trader.config import load          # noqa: E402
import preflight_live as pf                # noqa: E402

PX = 77749.0


class _Ex:
    name = "paper"


class _Feed:
    last_price = PX


#: 交易所规格（永续）。★ 2026-11-17 只做合约，所以体检也必须拿合约规格。
SPEC = {"min_qty": 0.001, "min_notional": 55.0, "base": "BTC"}


def _floor():
    """上游死线（能建一手的最小权益）。

    用与体检相同的二分法算出来，**不硬写** —— 硬写的数在手数或最小名义一变
    就变成一条说谎的测试（这个项目已经吃亏多次）。
    永续口径下它应该是一手 0.001 BTC 的价值 × (1+缓冲)。
    """
    cfg = load()
    eng = pf._make_engine(cfg, cfg.trading) if hasattr(pf, "_make_engine") else None
    if eng is None:
        eng = pf._engine_for_tests(cfg, cfg.trading) if hasattr(pf, "_engine_for_tests") else None
    if eng is None:
        from ai_trader.trading.engine import DecisionEngine
        eng = DecisionEngine.__new__(DecisionEngine)
        eng.cfg = cfg
        eng.tcfg = cfg.trading
        eng.feed = _Feed()
        eng.ex = _Ex()
        from ai_trader.trading.trend import TrendStrategy
        eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    lo, hi = 0.0, 5000.0
    if not eng._trend_plan(PX, hi, {"name": "x"}).ok:
        raise AssertionError("权益 %.0fU 都建不了仓，规格有问题" % hi)
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if eng._trend_plan(PX, mid, {"name": "x"}).ok:
            hi = mid
        else:
            lo = mid
    return hi


def _run(equity):
    """跑一次 check_funds_and_plan，返回 {(名字): (级别, 说明)}。"""
    cfg = load()
    pf.results[:] = []
    spec = dict(SPEC)
    eq = {"equity": equity, "balance": equity, "position_qty": 0.0}
    pf.check_funds_and_plan(cfg, cfg.trading, PX, spec, eq, _Feed(), _Ex())
    return {name: (level, detail) for level, name, detail, _fix in pf.results}


def test_healthy_account_reports_ok_headroom():
    """定投够多的账户，回撤余量必须是 OK —— 否则这个检查就是在瞎叫。"""
    r = _run(1275.0)
    assert "回撤余量" in r, "体检里没有「回撤余量」这一项：%r" % list(r)
    level, detail = r["回撤余量"]
    assert level == pf.OK, (
        "1275U 账户被判成 %s（%s）—— 好账户不该报警" % (level, detail))


def test_small_account_warns_about_headroom():
    """不够撑一次实测最大回撤 46.1% 的账户，必须 WARN。

    ★ 2026-11-17 只做合约之后生死线从 5.5U 变成 ~78U，所以“小账户”的量级
      也变了：取生死线的 1.15 倍（能开仓，但一轮正常回撤就卡住）。
      硬写 6.0 在合约口径下已经是“一手都开不出”，会摸到另一档去。
    """
    r = _run(_floor() * 1.15)
    level, detail = r["回撤余量"]
    assert level == pf.WARN, (
        "刚刚够开一手的账户被判成 %s（%s）—— 它离生死线只有一点点，必须报"
        % (level, detail))
    assert "停摆" in detail or "开不出" in detail, detail


def test_zero_equity_reports_bad_and_does_not_crash():
    """权益为 0 时必须明确报 NO-GO，而且这一行不能消失。

    为什么要求这么死（这条一开始写宽了，放过了一个变异）：
      如果只要求「没崩、有这一行」，那么把 zero-equity 分支删掉之后，
      代码会走进 elif 分支算 1 - floor/0 -> ZeroDivisionError ->
      被外层 except 兜成 WARN「算不出」，测试照样绿。
      但那时报告的是一句含糊的「算不出」，而不是「你没钱」。
      运维要看到的是后者。所以这里必须钉死 NO-GO。
    """
    r = _run(0.0)
    assert "回撤余量" in r, "权益为 0 时这一行消失了 —— 不显示的检查等于没有"
    level, detail = r["回撤余量"]
    assert level == pf.BAD, (
        "权益为 0 却只报了 %s（%s）；必须 NO-GO，不能降级成「算不出」"
        % (level, detail))
    assert "余量" in detail, detail


def test_headroom_boundary_matches_the_measured_floor():
    """余量要能用：找到一个「刚好够 46.1% 回撤」的权益，界线两侧判断要翻转。

    ★ 2026-11-17 只做合约之后，生死线从现货的 5.5U 变成合约的 **77.9U**
    （一手 0.001 BTC ≈ 77.75U，再加保证金余量）。要让 46.1% 的回撤后仍高于它：
        77.9 / (1 - 0.461) ≈ 144.5U
    所以 120U 应该 WARN、180U 应该 OK。这条把「翻在哪」也钉住，
    免得将来阈值被悄悄改成别的数。

    ★ 为什么要重算而不是直接把 8/14 改成别的数：因为“翻转点”是由
      `_floor` 和实测回撤两个事实算出来的，硬写一个数只会在下一次
      手数变动时变成一条说谎的测试。
    """
    floor = _floor()
    # 46.1% 是策略在 6.84 年权威数据上的实测最大回撤
    need = floor / (1.0 - 0.461)
    lo = _run(need * 0.8)["回撤余量"][0]
    hi = _run(need * 1.25)["回撤余量"][0]
    assert lo == pf.WARN, "权益 %.1fU（不够撑过 46.1%%）应该 WARN，实际 %s" % (need * 0.8, lo)
    assert hi == pf.OK, "权益 %.1fU（够撑过 46.1%%）应该 OK，实际 %s" % (need * 1.25, hi)
    # 死廿线本身也钉一下：它是合约的一手，不是现货的 5.5U
    assert floor > 50.0, "生死线 %.1fU 低于合约一手：规格没有切过来" % floor
