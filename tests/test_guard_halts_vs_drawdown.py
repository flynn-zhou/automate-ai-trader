# -*- coding: utf-8 -*-
"""无人值守闸的停机阈值必须【高于】策略自身的实测最大回撤。

否则会发生一件很隐蔽的事：策略在走它自己的正常回撤，
Guard 却在中途硬停，而且硬停需要人工 reset——7x24 无人值守时没人会去 reset，
于是系统从此不再开仓，表现上和"策略在等信号"一模一样。

这和 DECISION_MARKET.md:41-42 记的永续 1x 问题是同一个病：

    永续 1x 的生死线是 77.9 U，而策略实测最大回撤是 46.1 %。
    也就是说 100 U 账户一旦用永续 1x，回撤到 22 % 就永久停摆——
    它会在到达自己历史最差回撤之前就彻底开不出仓，而且不报错、不告警。

为什么需要这些测试（第 110 轮）：
  guard.py 里 max_drawdown_pct = 60.0 是【硬编码】的，不是配置项；
  而 46.1% 这个实测值只写在 DECISION_MARKET.md 的正文里。
  两边的数字没有任何测试把它们绑在一起——
  把 60 改成 40 不会让任何一条测试变红（改之前实测过：
  test_guard.py 等 6 个 guard 测试文件里，
  max_drawdown_pct 只作为看板渲染的假数据出现过）。

  这是跨模块的数字一致性，只靠"我记得 60 比 46 大"是不成立的。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import load
from ai_trader.guard import Guard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(ROOT, "DECISION_MARKET.md")


def _documented_mdd_pct():
    """从 DECISION_MARKET.md 读出策略实测最大回撤百分数。"""
    with open(DOC, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"策略实测最大回撤是\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    assert m is not None, (
        u"DECISION_MARKET.md 里找不到「策略实测最大回撤是 N %」这句。\n"
        u"这句是 Guard 阈值的设计依据，被删掉或改写后必须同步更新这条测试，"
        u"而不是让它悄悄失去依据。")
    return float(m.group(1))


def test_documented_mdd_is_the_number_we_think():
    """先把依据本身钉住：文档里的实测最大回撤必须是 46.1%。"""
    assert _documented_mdd_pct() == pytest.approx(46.1), (
        u"DECISION_MARKET.md 里的实测最大回撤变成了 %r（应为 46.1）。\n"
        u"如果这是重新回测得到的新值，那么 Guard 的 60%% 阈值必须重新评估——"
        u"它是按旧回撤定的。" % (_documented_mdd_pct(),))


def test_hard_halt_threshold_sits_above_the_strategy_drawdown():
    """★ Guard 的硬停阈值必须严格高于策略自身的最大回撤。"""
    g = Guard(load(), tdb=None)
    mdd = _documented_mdd_pct()
    assert g.max_drawdown_pct > mdd, (
        u"Guard 硬停阈值 %.1f%% 不高于策略实测最大回撤 %.1f%%。\n"
        u"后果：策略走自己的正常回撤时就会被硬停，而硬停需要人工 reset，"
        u"7x24 无人值守时没人 reset，系统从此不开仓且不报警。"
        % (g.max_drawdown_pct, mdd))


def test_daily_loss_halt_stays_below_the_hard_halt():
    """当日亏损闸不能比总体回撤闸更严，否则它才是真正的拦路虎。

    当日亏损是临时限制（次日自动解除），硬停是永久限制（要人工 reset）。
    如果当日闸比回撤闸还严，那么实际起作用的永远是那个临时闸，
    而"回撤到 X% 才停"这条设计根本没机会执行——等于阈值写错了地方。
    """
    g = Guard(load(), tdb=None)
    assert g.daily_loss_halt_pct <= g.max_drawdown_pct, (
        u"当日亏损闸 %.1f%% 比总体回撤闸 %.1f%% 还严"
        % (g.daily_loss_halt_pct, g.max_drawdown_pct))


def test_a_single_worst_case_trade_cannot_trip_the_daily_halt():
    """★ 单笔最坏情况下也不该触到当日亏损闸。

    现货 1x、25% 熔断：一笔打满仓位的止损最多亏掉约 25% 权益。
    当日亏损闸若定在 25% 以下，一次正常的止损就会把当天封掉，
    于是策略在自己的正常亏损上被反复停手——正是"风控吃掉边际"。
    """
    cfg = load()
    stop_pct = float(cfg.trading.trend_stop_pct)
    g = Guard(cfg, tdb=None)
    assert g.daily_loss_halt_pct > stop_pct, (
        u"当日亏损闸 %.1f%% 不高于单笔熔断 %.1f%%："
        u"一笔正常止损就会封掉当天，策略在自己的正常亏损上被停手。"
        % (g.daily_loss_halt_pct, stop_pct))
