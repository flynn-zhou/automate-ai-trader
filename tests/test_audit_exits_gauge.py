# -*- coding: utf-8 -*-
"""量具自身的测试。

★ 为什么量具也要测：它现在是我们判「出场改得对不对」的唯一依据。
  量具错一次，代价是照着错误的数改策略 —— 这一轮已经发生过一次
  （2026-11-19：空单顺向关键位判到了上方，得出 +0.470R，
   真值是 -0.016R）。所以把那次错误的**形状**钉成回归测试。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trading import thesis as T          # noqa: E402
from tools import audit_exits as A                 # noqa: E402

UP, DOWN = T.UP, T.DOWN
#: (ts, o, h, l)
BAR = 300_000


def _bars(*rows, ts0=1_800_000_000_000):
    return [(ts0 + i * BAR, float(o), float(h), float(l))
            for i, (o, h, l) in enumerate(rows)]


# ---------------------------------------------------------------- 方向口径

def _book(**kw):
    b = T.LineBook()
    for key, price in kw.items():
        b.add((key.replace("__", "@"), T.LinePoint(price=float(price),
                                                   src_ts=0, ready_ms=0)))
    return b


def test_nearest_favor_for_a_short_looks_below():
    """★★ 空单的顺向目标在**下方**。

    这条就是 2026-11-19 那次 +0.470R 假数字的形状：
    当时用「在上方」找空单的顺向关键位，于是空单全部退回 4R，
    多空两组样本被混在一起算期望。
    """
    book = _book(range_soft_dn__1h=95.0, range_soft_up__1h=105.0)
    d = A.nearest_level(book, A.FAV[DOWN], 100.0, 0, DOWN, +1, 10.0)
    assert d == pytest.approx(5.0), "空单顺向应取下方 95（距离 5）"


def test_nearest_favor_for_a_long_looks_above():
    book = _book(range_soft_dn__1h=95.0, range_soft_up__1h=105.0)
    d = A.nearest_level(book, A.FAV[UP], 100.0, 0, UP, +1, 10.0)
    assert d == pytest.approx(5.0), "多单顺向应取上方 105（距离 5）"


def test_nearest_adverse_for_a_long_looks_below():
    book = _book(range_soft_dn__1h=95.0, range_soft_up__1h=105.0)
    d = A.nearest_level(book, A.ADV[UP], 100.0, 0, UP, -1, 10.0)
    assert d == pytest.approx(5.0)


def test_nearest_adverse_for_a_short_looks_above():
    book = _book(range_soft_dn__1h=95.0, range_soft_up__1h=105.0)
    d = A.nearest_level(book, A.ADV[DOWN], 100.0, 0, DOWN, -1, 10.0)
    assert d == pytest.approx(5.0)


def test_nearest_level_respects_the_cap():
    """超出口径上限（cap）的关键位不算「顺向目标」。"""
    book = _book(range_soft_up__1h=200.0)
    assert A.nearest_level(book, A.FAV[UP], 100.0, 0, UP, +1, 10.0) is None


def test_nearest_level_ignores_lines_that_do_not_exist_yet():
    b = T.LineBook()
    b.add(("range_soft_up@1h", T.LinePoint(price=105.0, src_ts=0,
                                           ready_ms=10_000)))
    assert A.nearest_level(b, A.FAV[UP], 100.0, 5_000, UP, +1, 10.0) is None
    assert A.nearest_level(b, A.FAV[UP], 100.0, 20_000, UP, +1,
                           10.0) == pytest.approx(5.0)


# ---------------------------------------------------------------- 出场判定

def test_stop_wins_when_one_bar_reaches_both():
    """同一根里既够到止损又够到止盈 → 记止损（最坏）。

    实现甲（止损优先）必须给 -1R；实现乙会先判止盈 —— 两者的分歧
    由 `both` 条件豁免，不算量具 bug。
    """
    bars = _bars((100, 101, 99), (100, 120, 80))       # 第二根同时够到 110 和 90
    fill = 0
    a = A._outcome_stop_first(100.0, 90.0, 110.0, UP, bars, fill)
    assert a["stop"] is True and a["r"] == -1.0


def test_target_is_not_counted_on_the_fill_bar():
    """成交那根里不许记止盈 —— 那根里先成交还是先到目标不可知。"""
    bars = _bars((100, 130, 99))                        # 只有一根，且次根不存在
    a = A._outcome_stop_first(100.0, 90.0, 110.0, UP, bars, 0)
    assert a["r"] == 0.0, "成交当根就记止盈 = 最乐观假设"


def test_target_hit_on_the_next_bar_counts():
    bars = _bars((100, 101, 99), (101, 111, 100))
    a = A._outcome_stop_first(100.0, 90.0, 110.0, UP, bars, 0)
    assert a["stop"] is False
    assert a["r"] == pytest.approx(1.0)                 # (110-100)/10


def test_short_side_is_mirrored():
    bars = _bars((100, 101, 99), (99, 100, 89))
    a = A._outcome_stop_first(100.0, 110.0, 90.0, DOWN, bars, 0)
    assert a["stop"] is False
    assert a["r"] == pytest.approx(1.0)


def test_fill_requires_the_price_to_be_traded_through():
    """★ 成交判据 = 价格穿过挂单价（l <= order <= h），与方向无关。

    本系统的挂单两种都有（实测 678 条回撤限价单 / 593 条突破单），
    所以不能按方向写 `h >= order`：回撤买单的挂单价低于市价，
    那条判据在第一根就成立 —— 等于以更好的价格瞬时成交。
    """
    # 市价 105 上方横盘，买单挂 100：价格没下来 → 不成交
    bars = _bars((105, 106, 104), (105, 106, 104))
    assert A._fill_index(100.0, UP, bars, 0, 12) is None

    # 第三根跌到 98 → 穿过 100 → 成交
    bars = _bars((105, 106, 104), (105, 106, 104), (99, 100, 97))
    assert A._fill_index(100.0, UP, bars, 0, 12) == 2


def test_fill_works_for_a_breakout_order_too():
    """突破单（买单挂在市价上方）要等价格**涨到**挂单价。"""
    bars = _bars((95, 96, 94), (95, 96, 94))
    assert A._fill_index(100.0, UP, bars, 0, 12) is None
    bars = _bars((95, 96, 94), (98, 101, 97))
    assert A._fill_index(100.0, UP, bars, 0, 12) == 1


def test_fill_when_the_order_sits_inside_the_bar_range():
    bars = _bars((100, 105, 95))
    assert A._fill_index(100.0, UP, bars, 0, 12) == 0


def test_horizon_limits_the_wait_for_fill():
    bars = _bars((105, 106, 104), (105, 106, 104), (99, 100, 97))
    assert A._fill_index(100.0, UP, bars, 0, 2) is None      # 只等 2 根
    assert A._fill_index(100.0, UP, bars, 0, 3) == 2


# ---------------------------------------------------------------- 口径常数

def test_formula_stop_constant_matches_the_sizing_formula():
    """量具默认坐标系必须等于仓位公式算出来的那个数。"""
    from ai_trader.trading.sizing import SizingConfig, derive
    got = derive(SizingConfig(capital=100.0, risk_pct=1.0,
                              leverage=22.0)).max_stop_distance_pct
    assert A.D_MAX_FORMULA == pytest.approx(got), (
        "量具写死的公式止损和 sizing.derive() 不一致：改公式忘了改量具，"
        "量出来的数就又是另一个坐标系")
