# -*- coding: utf-8 -*-
"""持仓中重启，且下穿已过 —— 策略必须还能出场。

这是【失败驱动】的测试：先写一个当前代码过不了的测试，证明缺陷存在。

缺陷：
  want_exit 要求 (not above) and prev_above —— 必须是"新鲜下穿"。
  进程如果在【下穿之后】重启，持仓仍在，prev_above 已是 False，
  want_exit 从此永远返回 None。

  这笔持仓唯一的出路就剩 25% 熔断止损，
  而历史最深浮亏只有 -11.3%，熔断永远碰不到 ->
  仓位会一路持有穿过整个下跌趋势。

  回测里出场条件是"任何一根收盘跌破 SMA"（C[i] < sma(i)），
  没有 prev_above 这个要求。所以这是【实现与回测不一致】，
  不是"线上比回测更保守"。

对照组：进场侧有 startup_entry 专门处理"重启时交叉已过"，出场侧没有。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading.trend import TrendStrategy

N = 200
IV = 14400000


def _cfg():
    c = AppCfg()
    c.trading.sma_lookback = N
    return c


def _strat(closes, reconcile=False):
    tg = TrendStrategy(_cfg().trading, symbol="BTCUSDT")
    tg.seeded = True
    tg.need_reconcile = reconcile
    tg.bars = [{"t": i * IV, "o": c, "h": c, "l": c, "c": c, "v": 1.0}
               for i, c in enumerate(closes)]
    return tg


def _series_after_cross():
    """造一段：长期横盘 -> 急跌 -> 收盘跌破 SMA，返回 (全序列, 首次跌破的下标)。"""
    flat = [30000.0 + (i % 5) * 10 for i in range(260)]
    drop = [flat[-1] * (0.97 ** (k + 1)) for k in range(6)]
    closes = flat + drop
    for k in range(len(flat) - 1, len(closes)):
        if not _strat(closes[:k + 1]).state(closes[:k + 1][-1])["above"]:
            return closes, k
    raise AssertionError("造样本失败：没有找到跌破点")


def test_exit_signal_exists_on_the_crossing_bar():
    """基准：下穿当根必须有出场信号（否则连正常情况都出不了场）。"""
    closes, k = _series_after_cross()
    tg = _strat(closes[:k + 1])
    assert tg.want_exit(closes[k]) is not None, u"下穿当根竟无出场信号"


def test_exit_signal_survives_restart_after_cross():
    """缺陷核心：重启发生在下穿之后，出场信号必须仍然存在。

    场景 = 持仓中、下穿已过去 1 根、此时进程重启。
    回测在"任何一根收盘跌破 SMA"时出场，所以这种情况回测早就平了。
    线上若返回 None，这笔持仓就再也不会按策略平掉。
    """
    closes, k = _series_after_cross()
    # 下穿之后又走了一根
    sub = closes[:k + 2]
    tg = _strat(sub)                     # 新进程 = 新的策略对象
    st = tg.state(sub[-1])
    assert st["above"] is False, u"样本构造错误：最后一根应已在均线下方"
    assert st["prev_above"] is False, u"样本构造错误：上一根应也在均线下方"
    we = tg.want_exit(sub[-1])
    assert we is not None, (
        u"下穿后重启，want_exit 返回 None：这笔持仓只能等 25% 熔断，"
        u"而历史最深浮亏仅 -11.3%，等于永不按策略出场。"
        u"回测在 close < sma 时就会出场，所以线上偏离了回测。")
    assert "下穿" in we["reason"] or "SMA" in we["reason"]


def test_exit_signal_survives_many_bars_below():
    """连续多根在均线下方，出场信号不能消失。"""
    closes, k = _series_after_cross()
    sub = closes[:k + 5]
    tg = _strat(sub)
    assert tg.state(sub[-1])["above"] is False
    assert tg.want_exit(sub[-1]) is not None, (
        u"已在均线下方多根，want_exit 仍为 None -> 持仓无法出场")


def test_no_exit_signal_while_above():
    """反向约束：价格在均线上方时绝不能有出场信号（否则会乱平仓）。"""
    flat = [30000.0 + (i % 5) * 10 for i in range(300)]
    tg = _strat(flat)
    st = tg.state(flat[-1])
    assert st["above"] is True
    assert tg.want_exit(flat[-1]) is None, u"在均线上方却给了出场信号"


def test_entry_side_has_reconciliation_but_exit_side_mirrors_it():
    """对称性检查：进场侧有 startup_entry 处理"重启时交叉已过"，
    出场侧必须有对应的处理。这条测试在心理上是最重要的一条——
    它记录的是"同一类问题在一个方向修了、另一个方向没修"。"""
    flat = [30000.0 + (i % 5) * 10 for i in range(260)]
    up = [flat[-1] * (1.02 ** (k + 1)) for k in range(3)]
    closes = flat + up
    tg = _strat(closes, reconcile=True)
    st = tg.state(closes[-1])
    assert st["above"] is True and st["prev_above"] is True
    # 进场侧：want_entry 不给信号（交叉已过），但 startup_entry 兜底
    assert tg.want_entry(closes[-1]) is None
    assert tg.startup_entry(closes[-1]) is not None, u"进场侧对账失效"
    # 出场侧：镜像场景必须有等价兜底
    closes2, k = _series_after_cross()
    sub = closes2[:k + 3]
    tg2 = _strat(sub, reconcile=True)
    assert tg2.state(sub[-1])["above"] is False
    assert tg2.want_exit(sub[-1]) is not None, (
        u"进场侧有 startup_entry 兜底，出场侧没有镜像兜底")
