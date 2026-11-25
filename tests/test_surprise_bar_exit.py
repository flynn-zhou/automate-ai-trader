# -*- coding: utf-8 -*-
"""惊喜K线出场：单根 5m 顺向 ≥ `surprise_pts` 立即止盈。

★ 用户原话（2026-11-17）：

  「止盈不是固定的，可以先定一个很远的止盈，但是我想要的是现实跟随，
    根据我们的区间演化 + K先演化，判断风向！且任何一根 5m 的持仓顺向
    涨跌幅达到 500U，都要立即止盈，因为很难得！这是我对除了预期与现实
    之外的一个系统要求，我们不是长线，遇到这种惊喜K线必须走」

所以这是**独立于固定止盈价**的第二条出场规则。这份文件钉四件事：

  1. 到阈值就走，不到就走不了（阈值必须真的生效）
  2. **顺向**：做多只认向上、做空只认向下，反向的大根**不许**触发
  3. **单根**：不是从进场累计。否则它退化成第二个固定止盈，
     与「止盈不是固定的」直接矛盾
  4. 优先级：止损 > 正常止盈 > 惊喜K线 > 失效平仓

★ 用什么价结算：按**这一根的收盘价**。
  判据也用「收盘相对开盘」，两者同一口径 —— 不许「用影线极值判、
  用收盘价记」，那会让纸面样本静默变好看（这个项目所有偏差都必须朝
  不利方向）。
"""
import os
import sys
from dataclasses import replace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai_trader.trading import pending_plan as P      # noqa: E402
from ai_trader.trading import thesis as T            # noqa: E402

M15 = T.BAR_INTERVAL_MS
T0 = 1_700_000_000_000 - (1_700_000_000_000 % 1_800_000)


class Bar:
    def __init__(self, ts, o, h, l, c):
        self.ts, self.o, self.h, self.l, self.c = ts, o, h, l, c


def _book(n=10, price=1e6):
    bk = T.LineBook()
    for i in range(n):
        bk.add({"line_key": T.SLOW_LINE_KEY, "ts": T0 + i * M15,
                "line_price": price, "line_ready_ms": T0 + i * M15 + M15})
    return bk


def _long(**kw):
    base = dict(thesis_id="t", kind="测试", direction=T.UP, order_price=100.0,
                stop_price=90.0, target_price=1_000.0,
                order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL,
                surprise_pts=500.0)
    base.update(kw)
    return T.Thesis(**base)


def _flat(n, c=100.0):
    """横盘：开盘=收盘=100，影线各 1 点 → 顺向幅度 0，什么都不触发。"""
    return [Bar(T0 + i * M15, c, c + 1.0, c - 1.0, c) for i in range(n)]


def _surprise_states(th, bars, book):
    res = T.run(th, bars, book, start_at=0)
    return res, [t.to_state for t in res.transitions]


# ============================================================ 一、阈值生效

def test_surprise_bar_takes_profit_immediately():
    th = _long()
    bars = _flat(8)
    # 第 3 根：开盘 100，收盘 620 → 顺向 520 点 ≥ 500
    bars[3] = Bar(T0 + 3 * M15, 100.0, 625.0, 99.0, 620.0)
    res, states = _surprise_states(th, bars, _book(8))
    assert res.state == T.CLOSED_TP, (res.state, states)
    ev = res.transitions[-1].evidence
    assert ev["surprise_points"] == pytest.approx(520.0)
    assert ev["exit_price"] == 620.0, "必须按这一根的收盘价结算"
    assert "surprise_pts" in ev


def test_below_threshold_does_not_trigger():
    th = _long()
    bars = _flat(8)
    bars[3] = Bar(T0 + 3 * M15, 100.0, 505.0, 99.0, 500.0)   # 正好 400 点不够
    res, states = _surprise_states(th, bars, _book(8))
    assert T.CLOSED_TP not in states, states


def test_threshold_is_configurable():
    th = _long(surprise_pts=50.0)
    bars = _flat(6)
    bars[2] = Bar(T0 + 2 * M15, 100.0, 70.0, 99.0, 160.0)    # 顺向 60 点
    res, _ = _surprise_states(th, bars, _book(6))
    assert res.state == T.CLOSED_TP, res.state


def test_zero_threshold_disables_the_rule():
    """0 = 关。不许「0 就每根都触发」—— 那会让所有单在挂单后就立刻平掉。"""
    th = _long(surprise_pts=0.0)
    bars = _flat(6)
    bars[2] = Bar(T0 + 2 * M15, 100.0, 700.0, 99.0, 650.0)
    res, states = _surprise_states(th, bars, _book(6))
    assert T.CLOSED_TP not in states, states


# ============================================================ 二、顺向

def test_surprise_is_directional_for_longs():
    """做多：**向下**的大根不许触发（那是亏的，不是惊喜）。"""
    th = _long(surprise_pts=300.0)
    bars = _flat(6)
    # 向下 400 点，但止损在 90 会先拦下；这里把止损放远一点，只测方向
    th = _long(surprise_pts=300.0, stop_price=1.0)
    bars[2] = Bar(T0 + 2 * M15, 700.0, 705.0, 295.0, 300.0)  # 向下 400
    res, states = _surprise_states(th, bars, _book(6))
    assert T.CLOSED_TP not in states, "做多被一根向下的大根触发了止盈"


def test_surprise_is_directional_for_shorts():
    """做空：向上 500 点不许触发；向下 500 点才触发。"""
    th = T.Thesis(thesis_id="s", kind="测试", direction=T.DOWN,
                  order_price=100.0, stop_price=1_000.0, target_price=1.0,
                  order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL,
                  surprise_pts=500.0)
    up = _flat(6)
    up[2] = Bar(T0 + 2 * M15, 100.0, 605.0, 99.0, 600.0)      # 向上 500
    res, states = _surprise_states(th, up, _book(6))
    assert T.CLOSED_TP not in states, "做空被一根向上的大根触发了止盈"

    dn = _flat(6)
    dn[2] = Bar(T0 + 2 * M15, 700.0, 701.0, 195.0, 200.0)     # 向下 500
    res, states = _surprise_states(th, dn, _book(6))
    assert res.state == T.CLOSED_TP, (res.state, states)


# ============================================================ 三、单根

def test_surprise_is_measured_per_bar_not_cumulative():
    """★ 不许按「从进场累计」算 —— 那它就成了第二个固定止盈。

    这里每根只走 200 点，五根累计 1000 点，但**没有一根**到 500。
    """
    th = _long(surprise_pts=500.0)
    bars = [Bar(T0 + i * M15, 100.0 + i * 200.0,
                101.0 + i * 200.0, 99.0 + i * 200.0, 200.0 + i * 200.0)
            for i in range(6)]
    res, states = _surprise_states(th, bars, _book(6))
    assert T.CLOSED_TP not in states, "按累计算了：%r" % (states,)


def test_one_big_bar_among_flat_ones_triggers():
    th = _long(surprise_pts=500.0)
    bars = _flat(10)
    bars[7] = Bar(T0 + 7 * M15, 100.0, 900.0, 99.0, 880.0)
    res, _ = _surprise_states(th, bars, _book(10))
    assert res.state == T.CLOSED_TP
    assert res.transitions[-1].bar_ts == T0 + 7 * M15


# ============================================================ 四、优先级

def test_stop_wins_over_surprise_on_the_same_bar():
    """同一根既砸穿止损又顺向 500 点 → 只能是止损。

    那种根只可能是巨震（先下后上或反之），OHLC 分不清先后。
    按不利于我的那一侧结算 —— 否则样本会静默变好看。
    """
    th = _long(surprise_pts=300.0)
    bars = _flat(8)
    # 同一根：低点 85 砸穿止损 90，收盘 500 顺向 400 点
    bars[3] = Bar(T0 + 3 * M15, 100.0, 505.0, 85.0, 500.0)
    res, states = _surprise_states(th, bars, _book(8))
    assert res.state == T.CLOSED_SL, (res.state, states)


def test_target_wins_over_surprise_when_both_reach():
    """正常止盈价更远时，谁先到谁结算；同一根上正常止盈优先（价格更优）。"""
    th = _long(surprise_pts=100.0, target_price=300.0)
    bars = _flat(6)
    bars[2] = Bar(T0 + 2 * M15, 100.0, 350.0, 99.0, 340.0)
    res, _ = _surprise_states(th, bars, _book(6))
    assert res.state == T.CLOSED_TP
    ev = res.transitions[-1].evidence
    assert "target_price" in ev, "同一根上应该按正常止盈结算，而不是惊喜K线"


def test_surprise_beats_invalidation_close():
    """★ 失效平仓是按收盘价走；惊喜K线那根是最难得的一次机会。

    如果让失效先判，这根就会以「预期破功」的名义被平掉，
    而实际上它是行情给的最好的那一根。
    """
    # 趋势家族：快线在上方 → 做空时被快线打脸 = 失效
    th = T.Thesis(thesis_id="s", kind="测试", direction=T.DOWN,
                  order_price=100.0, stop_price=1_000.0, target_price=1.0,
                  order_type=T.ORDER_LIMIT, family=T.FAMILY_TREND,
                  surprise_pts=400.0)
    bars = _flat(6)
    # 向下 500 点：对做空是顺向 → 惊喜；同时收盘远离 ema，失效也会成立
    bars[3] = Bar(T0 + 3 * M15, 700.0, 701.0, 195.0, 200.0)
    book = _book(6, price=1_000_000.0)      # ema 远在上方 → 做空一直「合规」
    res, states = _surprise_states(th, bars, book)
    assert res.state == T.CLOSED_TP, (res.state, states)
    assert "surprise_points" in res.transitions[-1].evidence


# ============================================================ 五、配置接线

def test_pending_params_carries_the_threshold():
    assert P.PendingParams().surprise_pts == 500.0


def test_to_thesis_forwards_the_threshold():
    ctx_src = P.PendingParams()
    th = P.to_thesis(_plan_stub(), params=replace(ctx_src, surprise_pts=123.0))
    assert th.surprise_pts == 123.0


def _plan_stub():
    """最小可用的 Plan（只够 `to_thesis` 用）。"""
    return P.Plan(kind="测试", direction=P.UP, bar_ts=T0, bar_index=0,
                  order_price=100.0, stop_price=90.0, target_price=1_000.0,
                  order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL)


def test_surprise_must_be_non_negative():
    th = _long(surprise_pts=-1.0)
    with pytest.raises(ValueError, match="surprise_pts"):
        th.validate()


def test_fill_bar_uses_the_fill_price_not_the_bar_open():
    """★ 成交那根的基准必须是**成交价**，不是开盘价。

    实测反例（我自己写出来的）：挂 STOP buy @100500，那根 5m 是
    `o=100000 h=100505 l=99999 c=100502`。按 `o` 算顺向 502 点 → 立刻止盈，
    可实际只赚了 1.9 点。一根本来就很宽的大K 被误当成了利润。

    ★ 注意：**后续的根**仍然按各自的开盘价算 —— 那时我们早已在持仓里了。
    """
    th = T.Thesis(thesis_id="f", kind="测试", direction=T.UP,
                  order_price=100_500.0, stop_price=90_000.0,
                  target_price=1_000_000.0, order_type=T.ORDER_STOP,
                  family=T.FAMILY_REVERSAL, surprise_pts=500.0)
    bars = [Bar(T0 + i * M15, 100_000.0, 100_001.0, 99_999.0, 100_000.0)
            for i in range(6)]
    # 成交那根：从开盘 100000 看到 502 点的顺向，但成交在 100500
    bars[2] = Bar(T0 + 2 * M15, 100_000.0, 100_505.0, 99_999.0, 100_502.0)
    res, states = _surprise_states(th, bars, _book(6))
    assert T.CLOSED_TP not in states, (
        "成交那根按开盘价算了顺向幅度：实际只赚 2 点，被当成 502 点止盈了")
    assert res.state == T.FILLED, (res.state, states)