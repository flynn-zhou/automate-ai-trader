# -*- coding: utf-8 -*-
"""FILL-1：成交只能发生在**挂单之后**，且没有仓位就不许进终态。

来源是 2026-11-25 的真钱事故：
  13:25→14:45  `place_order` 连续失败（-2019 保证金不足）；
  14:50       保证金闸撤掉外来占位单后**第一次挂单成功**；
  同一批/下一根 `close` 也 applied=1，state=CLOSED_TP；
  而交易所那两张 `AI-pend-*` 单从头到尾是 NEW、executed_qty=0
  → 两张真实挂单变成**孤儿**（无人再管；一旦成交会被判成"我方成交"而硬停）。

根因（不是"同根成交"那么笼统）：
  挂单那一轮的 `continue` 只保护**同一次调用**。游标 `next_index` 被解冻
  重算到 386，而 `armed_at_bar` 是 399 —— 下一次调用从 386 开始遍历，
  ARMED 状态把挂单**之前**的 K 又扫了一遍，并在其中一根上判出成交。

不变量（二值事实，不是阈值）：挂单在 `armed_at_sub_ts` 收盘之后才存在，
那之前（含）的 K 里不可能成交。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trading import thesis as T                    # noqa: E402

M5 = 300000
M15 = 900000
T0 = 1795572900000


class _Bar(object):
    def __init__(self, ts, o, h, l, c):
        self.ts, self.o, self.h, self.l, self.c = ts, o, h, l, c


def _thesis():
    return T.Thesis(
        thesis_id="t|up|1|100.0000", kind="下跌三引线假突破限价单",
        direction=T.UP, order_price=100.0, stop_price=95.0,
        target_price=115.0, order_type=T.ORDER_LIMIT,
        family=T.FAMILY_REVERSAL, surprise_pts=0.0, horizon_bars=12)


def _walk(bars, progress, subs=None):
    res, newp = T.step(_thesis(), bars, T.LineBook(), progress=progress,
                       entry_bars=subs, entry_interval_ms=M5, interval_ms=M15)
    return res, newp


def _progress(sub_ts, armed_at_bar=0, next_index=0):
    return {"state": T.ARMED, "next_index": next_index,
            "armed_at_bar": armed_at_bar,
            "armed_at_sub_ts": sub_ts,
            "armed_ref_close": 100.0}


# ---------------------------------------------------------------- 不变量 1

def test_no_fill_when_only_pre_arm_bars_cross_the_price():
    """只有挂单**之前**的 K 穿过挂单价 → 不许成交（孤儿单的成因）。

    挂单之后价格一路走高、再没回到 100，所以永远不该成交。
    旧代码会把挂单前那根（穿过 100）算成成交 → FILLED → close
    → CLOSED_TP，而交易所那张单还是 NEW。
    """
    subs = [
        _Bar(T0 + 0 * M5, 101.0, 102.0, 99.0, 100.5),   # 穿过 100（挂单之前）
        _Bar(T0 + 1 * M5, 100.5, 101.5, 98.5, 99.5),    # 穿过 100（挂单之前）
        _Bar(T0 + 2 * M5, 99.5, 104.0, 99.2, 103.5),    # 挂单在这根收盘之后
        _Bar(T0 + 3 * M5, 103.5, 106.0, 103.0, 105.5),  # 挂单之后，走高
    ]
    bars = [_Bar(T0, 101.0, 106.0, 98.5, 105.5)]
    res, _ = _walk(bars, _progress(T0 + 2 * M5), subs=subs)
    assert res.state != T.FILLED, (
        "只有挂单之前的 K 穿过挂单价，却判成了成交 —— 这正是孤儿单的成因：%s"
        % (res.states(),))


def test_fill_happens_on_the_first_bar_after_arm():
    """挂单一根之后真的跌穿挂单价 → 必须成交（守卫不能把正常成交也挡掉）。"""
    subs = [
        _Bar(T0 + 0 * M5, 101.0, 102.0, 99.0, 100.5),   # 挂单之前，穿过 100
        _Bar(T0 + 1 * M5, 100.5, 101.0, 97.0, 97.5),    # 挂单之后，跌穿 100
    ]
    bars = [_Bar(T0, 101.0, 102.0, 97.0, 97.5)]
    res, _ = _walk(bars, _progress(T0 + 0 * M5), subs=subs)
    assert res.state == T.FILLED, (
        "挂单之后跌穿挂单价却不成交（守卫误伤）：%s" % (res.states(),))


def test_coarse_path_still_fills_after_armed_bar():
    """没有 5m 子根（粗节奏）时，靠 armed_at_bar 索引守卫；
    挂单之后那根仍然能成交。"""
    bars = [
        _Bar(T0 + 0 * M15, 101.0, 102.0, 99.0, 100.5),  # 挂单发生在这根收盘之后
        _Bar(T0 + 1 * M15, 100.5, 101.0, 97.0, 98.0),   # 挂单之后，跌穿 100
    ]
    prog = {"state": T.ARMED, "next_index": 0, "armed_at_bar": 0,
            "armed_ref_close": 100.5}
    res, _ = _walk(bars, prog)
    assert res.state == T.FILLED, (
        "挂单之后跌穿挂单价却不成交：%s" % (res.states(),))


def test_coarse_path_does_not_fill_on_the_armed_bar_itself():
    """粗节奏下，挂单那根自己的 h/l 属于过去，不许判成交。"""
    bars = [
        _Bar(T0 + 0 * M15, 101.0, 102.0, 96.0, 100.5),  # 这根低点穿过 100
        _Bar(T0 + 1 * M15, 100.5, 104.0, 100.2, 103.5),  # 之后一路走高
    ]
    prog = {"state": T.ARMED, "next_index": 0, "armed_at_bar": 0,
            "armed_ref_close": 100.5}
    res, _ = _walk(bars, prog)
    assert res.state != T.FILLED, (
        "挂单那一根自己的低点被当成成交了：%s" % (res.states(),))
