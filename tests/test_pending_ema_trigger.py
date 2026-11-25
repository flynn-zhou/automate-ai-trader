# -*- coding: utf-8 -*-
"""EMA 拒绝：入场触发口径（`struct_lookback`）与时间闸（`horizon_bars`）。

用户 2026-11-17「1、2、3都做」，对应三处：
  1. 入场触发从「往前 10 根的结构低点」改成「拒绝根自己的极值」；
  2. 同根「既成交又失效」必须打 `ambiguous_same_bar`，不许静默按先成交算；
  3. `horizon_bars` 从 0（不限制）改成 12 根 15m（3 小时）。

★ 这三个数字都不是拍脑袋的，各自的证据写在 `PendingParams` 的注释里。
"""
import os
import sys
from collections import namedtuple

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai_trader.trading import pending_plan as P    # noqa: E402
from ai_trader.trading import thesis as T          # noqa: E402

M15 = T.BAR_INTERVAL_MS
T0 = 1_700_000_000_000 - (1_700_000_000_000 % M15)
PX = 100_000.0
TICK = 0.1

Bar = namedtuple("Bar", "ts o h l c v")


def b15(i, o, h, l, c):
    return Bar(T0 + i * M15, float(o), float(h), float(l), float(c), 1.0)


class _Book:
    """线值替身：ema20@15m 报 `price`，ema50@15m 报 `price + 400`。

    ★ 2026-11-20：原来它对**所有**键返回同一个价，趋势背景闸因此看到
      `ema20 == ema50` → `trend_flat`（判不出背景 → 不做）。真实行情里
      不同周期的两条均线不可能恒等，所以这是替身的失真。
      慢线放在快线**上方** = 下跌背景，正合 EMA 拒绝（趋势族，做空）的极性。
    """

    def __init__(self, price=PX):
        self.price = float(price)

    def at(self, key, ts):
        px = self.price + 400.0 if key == T.TREND_SLOW_KEY else self.price
        return T.LinePoint(price=px, src_ts=int(ts), ready_ms=int(ts))


def _linebook(bars, price):
    """给 ema20@15m **和** ema20@30m 两条线（都用 add，不能塞 dict）。

    ★ 两条都要给：失效判据有快慢两条路径 —— 慢路径要连续
      `SLOW_CONFIRM_BARS`(3) 根收盘，快路径（ema20@30m）**当根**就判。
      只给慢线的话，成交那根的 verdict 还是 PENDING，同根歧义根本触发不到。
    """
    b = T.LineBook()
    for bar in bars:
        ts = int(bar.ts)
        b.add({"line_key": T.SLOW_LINE_KEY, "ts": ts,
               "line_price": float(price), "line_ready_ms": ts + M15})
        b.add({"line_key": T.FAST_LINE_KEY, "ts": ts,
               "line_price": float(price), "line_ready_ms": ts})
    return b


def reject_down(i, level=PX):
    """做空方向：上影线刺穿 ema、收盘回到下方。"""
    c = level - 10.0
    return b15(i, c, level + 110.0, c - 90.0, c)


M5 = 300_000


def _h1(n=20, half=160.0):
    """1h 序列必须铺到 T0 之前 —— 否则 `h1_prev2_amp` 取不到
    「前两根已收盘的 1h」→ 返回 None → `no_amp` → 一条计划都没有。"""
    start = T0 - 6 * 3_600_000
    return [Bar(start + i * 3_600_000, PX, PX + half, PX - half, PX, 1.0)
            for i in range(n)]


def _m5(n=40, half=45.0):
    """5m 序列同样要盖住 T0 之后那段 —— `amp5_at` 取不到也会 `no_amp`。"""
    return [Bar(T0 + i * M5, PX, PX + half, PX - half, PX, 1.0)
            for i in range(n)]


def ema_ctx(patch=None, n=10):
    bars = [reject_down(i) for i in range(n)]
    for i, b in (patch or {}).items():
        bars[i] = b
    return P.PlanContext(bars15=bars, book=_Book(), bars_1h=_h1(),
                         bars_5m=_m5())


# ------------------------------------------------------- 1. 入场触发口径

def test_default_struct_lookback_is_zero():
    """默认 0 = 就用拒绝根自己的极值，不再往前找 10 根。"""
    assert P.PendingParams().struct_lookback == 0


def test_trigger_uses_the_rejection_bar_itself_by_default():
    """★ 默认口径下，a = 拒绝根自己的 low（做空）。

    实测同一条形态：往前 10 根找 → 成交率 41.7%；用拒绝根自己 → 69.1%。
    差的那 27 个百分点全耗在「价格还没走到远处的挂单价就先被 ema 判失效」。
    """
    i = 9
    bars = [reject_down(k) for k in range(10)]
    # 第 1 根造一个明显更低的老结构位（老口径会选中它）
    bars[1] = b15(1, PX - 10, PX + 110, PX - 400, PX - 10)
    ctx = P.PlanContext(bars15=bars, book=_Book(), bars_1h=_h1(),
                         bars_5m=_m5())
    plan, why = P.detect_ema_reject(i, ctx, P.PendingParams())
    assert why == "ok", why
    last_low = min(float(b.l) for b in [bars[i]])
    assert plan.evidence["structure_low"] == pytest.approx(last_low)
    assert plan.evidence["struct_lookback"] == 0
    assert "拒绝根本身" in plan.evidence["structure_from"]


def test_trigger_can_still_look_back_if_asked():
    """老口径没删：`struct_lookback=10` 就回到「往前找结构位」。

    用户原话「通常不超过 10 根」仍然有效，参数只是换了默认值。
    """
    bars = [reject_down(k) for k in range(10)]
    bars[1] = b15(1, PX - 10, PX + 110, PX - 400, PX - 10)
    ctx = P.PlanContext(bars15=bars, book=_Book(), bars_1h=_h1(),
                         bars_5m=_m5())
    plan, why = P.detect_ema_reject(
        9, ctx, P.PendingParams(struct_lookback=10))
    assert why == "ok", why
    assert plan.evidence["structure_low"] == pytest.approx(PX - 400)


def test_trigger_evidence_records_where_the_base_came_from():
    """证据必须写清「a 是从哪儿取的」—— 否则事后没法判断用的哪套口径。"""
    ctx = ema_ctx()
    plan, _ = P.detect_ema_reject(9, ctx, P.PendingParams())
    ev = plan.evidence
    assert "structure_from" in ev and ev["structure_from"]
    assert ev["structure_range"] == [9, 9], (
        "默认口径下取值区间就是拒绝根自己")


# ------------------------------------------------- 2. 同根歧义必须记账

def _same_bar_case():
    """第 1 根「既触及挂单价、又收盘穿过 ema」的 15m。

    ★ 第 0 根必须**干净**（收盘在 ema 下方、且没碰到挂单价），
      否则会在 EXPECTED 状态就被判失效，根本走不到「成交」那一步。
      （第一版三根全一样，结果第一根就 CANCELLED。）
    """
    th = T.Thesis("amb", "测试", T.DOWN, order_price=100.0,
                  stop_price=110.0, target_price=80.0,
                  order_type=T.ORDER_STOP, family=T.FAMILY_TREND)
    bars = [
        # 第 0 根：收盘 140 < ema 150（不失效），low 130 > 100（不成交）
        Bar(T0, 140.0, 145.0, 130.0, 140.0, 1.0),
        # 第 1 根：low 99 ≤ 100（成交）且 close 160 > ema 150（失效）→ 同根
        Bar(T0 + M15, 140.0, 165.0, 99.0, 160.0, 1.0),
        Bar(T0 + 2 * M15, 160.0, 165.0, 130.0, 160.0, 1.0),
    ]
    return th, bars


def test_same_bar_fill_and_invalidation_is_recorded():
    """★ 同根既成交又失效 → 必须进 `ambiguous`，不许静默。

    OHLC 没有 bar 内顺序，「跌到挂单价」和「收盘站上 ema」哪个先发生
    **不可知**。以前默默按先成交算，于是那批脏数据混进了成交统计。
    """
    th, bars = _same_bar_case()
    res = T.run(th, bars, _linebook(bars, 150.0), claim=None)
    assert res.ambiguous_same_bar >= 1, (
        "同根成交+失效没有被记账（states=%r）" % (res.states(),))
    ent = res.ambiguous[0]
    assert "先后不可判定" in ent["why"]
    assert "先成交" in ent["handling"]
    # 成交那条转换上也必须有标记，方便按转换过滤
    fills = [t for t in res.transitions if t.to_state == T.FILLED]
    assert fills and fills[0].evidence.get("ambiguous_same_bar") is True


def test_same_bar_fill_and_exit_is_recorded():
    """成交那根同根就打到止损 → 同样不可判定，同样必须记账。

    做空 STOP：挂单价 100（跌到就成交）、止损 110（涨到就止损）。
    一根 low=99 / high=115 的 K 让两件事同时成立 —— 谁先碰到判不了。
    """
    th = T.Thesis("amb2", "测试", T.DOWN, order_price=100.0,
                  stop_price=110.0, target_price=80.0,
                  order_type=T.ORDER_STOP, family=T.FAMILY_TREND)
    bars = [
        Bar(T0, 140.0, 145.0, 130.0, 140.0, 1.0),          # 干净：不成交
        Bar(T0 + M15, 140.0, 115.0 + 40.0, 99.0, 140.0, 1.0),  # 成交+止损同根
        Bar(T0 + 2 * M15, 140.0, 145.0, 130.0, 140.0, 1.0),
    ]
    res = T.run(th, bars, _linebook(bars, 1000.0), claim=None)
    kinds = [a["kind"] for a in res.ambiguous]
    assert "fill+exit" in kinds, (kinds, res.states())
    fills = [t for t in res.transitions if t.to_state == T.FILLED]
    assert fills and fills[0].evidence["ambiguous_same_bar"] is True


def test_clean_fill_is_not_flagged_ambiguous():
    """反证：真正干净的成交不许被误标成歧义。

    「干净」= 成交那根既没收盘穿过 ema（不失效），
    high 也没碰到 110（不止损）、low 没碰到 80（不止盈）。
    ★ 第一版直接复用 `_same_bar_case` 的 K 线，那根 high=165 ≥ 止损 110，
      本身就是一个 fill+exit 歧义 —— 于是「反证」自己先失败了。
    """
    th = T.Thesis("clean", "测试", T.DOWN, order_price=100.0,
                  stop_price=110.0, target_price=80.0,
                  order_type=T.ORDER_STOP, family=T.FAMILY_TREND)
    bars = [
        Bar(T0, 140.0, 145.0, 130.0, 140.0, 1.0),
        # low 99 ≤ 100 成交；high 105 < 110 不碰止损；low 99 > 80 不碰止盈
        Bar(T0 + M15, 140.0, 105.0, 99.0, 103.0, 1.0),
        Bar(T0 + 2 * M15, 103.0, 108.0, 101.0, 106.0, 1.0),
    ]
    res = T.run(th, bars, _linebook(bars, 1000.0), claim=None)
    assert [t.to_state for t in res.transitions].count(T.FILLED) >= 1
    assert res.ambiguous_same_bar == 0, res.ambiguous


# ------------------------------------------------------- 3. 时间闸

def test_default_horizon_is_set():
    """默认不再是 0（不限制）。"""
    assert P.PendingParams().horizon_bars == 12


def test_plan_carries_the_horizon_to_the_thesis():
    """计划上的 horizon 必须真的传到 thesis，否则闸是空设的。"""
    plan = P.Plan(kind="x", direction=P.DOWN, order_type=T.ORDER_STOP,
                  bar_index=0, bar_ts=T0, order_price=100.0,
                  stop_price=110.0, target_price=90.0)
    th = P.to_thesis(plan)
    assert th.horizon_bars == 12


def test_armed_order_expires_after_the_horizon():
    """挂单超过 horizon 根仍未成交 → EXPIRED。

    ★ 这里是**反证**式的：如果不设闸，这条会一直 ARMED 到序列结束。
      实测未成交的一条挂到 18 根（4.5 小时）还挂着 —— 完全没信息的仓位。
    """
    th = T.Thesis("hz", "测试", T.DOWN, order_price=50.0,
                  stop_price=60.0, target_price=40.0,
                  order_type=T.ORDER_STOP, horizon_bars=3)
    # 价格一直在 200 附近：永远碰不到 50 的挂单价
    bars = [Bar(T0 + i * M15, 200.0, 205.0, 195.0, 200.0, 1.0)
            for i in range(12)]
    res = T.run(th, bars, _linebook(bars, 1000.0), claim=None)
    assert res.state == T.EXPIRED, res.states()


def test_horizon_zero_still_means_no_gate():
    """0 仍然是「不设闸」—— 语义没变，只是默认值变了。"""
    th = T.Thesis("hz0", "测试", T.DOWN, order_price=50.0,
                  stop_price=60.0, target_price=40.0,
                  order_type=T.ORDER_STOP, horizon_bars=0)
    bars = [Bar(T0 + i * M15, 200.0, 205.0, 195.0, 200.0, 1.0)
            for i in range(12)]
    res = T.run(th, bars, _linebook(bars, 1000.0), claim=None)
    assert res.state == T.ARMED, res.states()
