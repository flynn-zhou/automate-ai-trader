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


def _run(equity):
    """跑一次 check_funds_and_plan，返回 {(名字): (级别, 说明)}。"""
    cfg = load()
    cfg.trading.exec_market = "spot"
    pf.results[:] = []
    spec = {"min_qty": 0.00001, "min_notional": 5.0, "base": "BTC"}
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
    """只剩个位数时，余量已经不够覆盖实测最大回撤 46.1%，必须 WARN。"""
    r = _run(6.0)
    level, detail = r["回撤余量"]
    assert level == pf.WARN, (
        "6U 账户的回撤余量被判成 %s（%s）—— 它离生死线只有一点点，必须报"
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

    生死线约 5.5U，要让 46.1% 的回撤后仍高于它：
        5.5 / (1 - 0.461) ≈ 10.2U
    所以 10.2U 附近应该从 WARN 翻成 OK。这条把「翻在哪」也钉住，
    免得将来阈值被悄悄改成别的数。
    """
    lo = _run(8.0)["回撤余量"][0]
    hi = _run(14.0)["回撤余量"][0]
    assert lo == pf.WARN, "8U 应该 WARN，实际 %s" % lo
    assert hi == pf.OK, "14U 应该 OK，实际 %s" % hi
