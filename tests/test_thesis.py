# -*- coding: utf-8 -*-
"""预期-现实管理（ai_trader/trading/thesis.py）的回归测试。

## 这个文件在钉什么

不是「函数能跑」，而是用户**明确纠正过我的每一条口径**：

1. **「有效」= 收盘价，影线完全不参与。**
   用户：「再 15m 的 K 线收盘前，向上插针 10 次都过了 15m 的 ema20，都无所谓，
   只要收盘那一刻价格在 ema20 下面，就算无效！」
   → 所以有一条测试专门构造「影线远超线、收盘在另一侧」的 K 线，断言**不**失效。

2. **慢路径只认第 3 根，是跳读不是滚动。**
   用户：「第三根跌破就认第三根，只认第 3 根！」
   → 中间根无论怎么走都不看、不重置。测试同时钉「中间根不管」与
   「第 3 根跌破就不算」。

3. **快路径独立结算，不许傻等第 3 根。**
   用户：「是不是要提前确认，别傻傻的等第三根！独立结算！」
   → 有一条测试让快路径在第 2 根就触发。

4. **快路径自己就是确认，不再向上确认。**
   用户：「30m 的有效站稳不用确认，这里是对 15m 的站稳的确认，
   不是需要大一级周期的有效确认哦！」
   → 快路径只看 15m 收盘 vs ema20@30m，没有额外的确认根数。

5. **EMA 用当根自己的值（移动参照）。**
   → 有两条测试专门区分「用当根」和「用固定参照」，用固定参照会得出相反结论。

6. **INVALIDATED 与 CANCELLED 必须分开。**
   前者根本没挂过单，后者挂过再撤。混淆不会报错，只会两边都错。

7. **同根 K 线内顺序取保守侧。**
   OHLC 没有 bar 内顺序是数据缺陷，不是实现细节。
"""
from __future__ import annotations

from collections import namedtuple

import pytest

from ai_trader.trading import thesis as T

M15 = T.BAR_INTERVAL_MS
M30 = 1_800_000
# 整 30m 边界起步，这样 15m 与 30m 的桶边界关系是确定的
T0 = 1_700_000_000_000 - (1_700_000_000_000 % M30)

Bar = namedtuple("Bar", "ts o h l c v")


# ------------------------------------------------------------------ 工具

def bars_of(closes, highs=None, lows=None):
    """按收盘价造一串 15m bar；默认影线上下各 5 点。"""
    out = []
    for i, c in enumerate(closes):
        c = float(c)
        h = float(highs[i]) if highs is not None else c + 5.0
        l = float(lows[i]) if lows is not None else c - 5.0
        out.append(Bar(T0 + i * M15, c, h, l, c, 1.0))
    return out


def book_of(slow=None, fast=None):
    """slow 按 15m 逐根给；fast 按 30m 逐根给。None 表示这根没有值。"""
    b = T.LineBook()
    for i, p in enumerate(slow or ()):
        if p is None:
            continue
        b.add({"line_key": T.SLOW_LINE_KEY, "ts": T0 + i * M15,
               "line_price": float(p), "line_ready_ms": T0 + i * M15 + M15})
    for k, p in enumerate(fast or ()):
        if p is None:
            continue
        b.add({"line_key": T.FAST_LINE_KEY, "ts": T0 + k * M30,
               "line_price": float(p), "line_ready_ms": T0 + k * M30 + M30})
    return b


def flat_book(n_bars, slow=1e6, fast=1e6):
    """把两条线都放在极高处 —— 任何收盘价都「站不上」，用于测状态机本身。"""
    return book_of(slow=[slow] * n_bars,
                   fast=[fast] * (n_bars // 2 + 2))


def short_thesis(order=100.0, stop=110.0, target=90.0, horizon=0):
    return T.Thesis(thesis_id="t1", kind="测试形态", direction=T.DOWN,
                    order_price=order, stop_price=stop, target_price=target,
                    horizon_bars=horizon)


def verdict_at(bars, i, book, direction=T.DOWN):
    return T.invalidation_verdict(bars, i, book, direction)


def never_claim(i, bars, book):
    return False, {"why": "测试：预期永不成立"}


# ============================================================ 一、LineBook

def test_line_not_readable_before_ready():
    """线值在 ready_ms 之前**不可读** —— 这是防偷未来的第一道门。"""
    b = book_of(slow=[100.0])
    ready = T0 + M15
    assert b.at(T.SLOW_LINE_KEY, ready - 1) is None
    assert b.at(T.SLOW_LINE_KEY, ready) is not None


def test_line_returns_latest_readable_value():
    b = book_of(slow=[100.0, 101.0, 102.0])
    # 第 1 根收盘时刻 = T0 + 2*M15，此时第 1 根（ready=T0+2*M15）刚好可用
    pt = b.at(T.SLOW_LINE_KEY, T0 + 2 * M15)
    assert pt is not None and pt.price == 101.0


def test_30m_line_is_not_usable_before_the_30m_bar_closed():
    """★ 防偷未来的核心测试：15m 收盘时，还没收盘的那个 30m 桶不能用。

    15m 第 i 根收盘于 T0+(i+1)*M15。30m 第 k 桶可用时刻 = T0+(k+1)*M30。
    所以 i=1 收盘（T0+30m）时，只能用 30m 第 0 桶；第 1 桶（ready=T0+60m）
    还要等。
    """
    b = book_of(fast=[90.0, 200.0])
    pt1 = b.at(T.FAST_LINE_KEY, T0 + 1 * M15 + M15)     # i=1 收盘
    assert pt1 is not None and pt1.price == 90.0, "用了还没收盘的 30m 桶"
    pt3 = b.at(T.FAST_LINE_KEY, T0 + 3 * M15 + M15)     # i=3 收盘
    assert pt3 is not None and pt3.price == 200.0


def test_max_staleness_returns_none_when_too_old():
    """数据停更时不能安静地把旧值当现在用。"""
    b = book_of(slow=[100.0])
    fresh = b.at(T.SLOW_LINE_KEY, T0 + M15)
    assert fresh is not None
    stale = b.at(T.SLOW_LINE_KEY, T0 + 100 * M15, max_staleness_ms=M15)
    assert stale is None


def test_missing_line_price_row_is_skipped_not_zero():
    """line_price 为 NULL = 这条线在这根上不存在，不能当成 0。"""
    b = T.LineBook()
    b.add({"line_key": T.SLOW_LINE_KEY, "ts": T0, "line_price": None,
           "line_ready_ms": T0 + M15})
    assert b.at(T.SLOW_LINE_KEY, T0 + M15) is None


def test_line_book_keys():
    b = book_of(slow=[1.0], fast=[2.0])
    # 字符串排序：'15m' 在 '30m' 前面（'1' < '3'）
    assert b.keys() == [T.SLOW_LINE_KEY, T.FAST_LINE_KEY]


# ================================================= 二、收盘价才算数（影线不算）

def test_upper_wick_far_above_line_does_not_invalidate():
    """★ 用户原话的直译：盘中插针 10 次过了 ema20，收盘在下面就算无效。

    这里把 high 抬到远高于线、close 压到线下面 —— 判据只看 close，
    所以绝不能失效。
    """
    slow = [100.0] * 4
    bars = bars_of([99.0, 99.0, 99.0, 99.0],
                   highs=[180.0, 190.0, 200.0, 999.0],   # 影线疯狂上刺
                   lows=[95.0] * 4)
    v, ev = verdict_at(bars, 3, book_of(slow=slow))
    assert v == T.PENDING, "影线穿刺被当成失效了 —— 违反了「收盘价才算数」"
    assert ev["slow"]["prev_close"] == 99.0 and ev["slow"]["now_close"] == 99.0


def test_lower_wick_below_line_does_not_invalidate_for_up_thesis():
    """做多镜像：下影线狂刺不算，只看收盘。"""
    slow = [100.0] * 4
    bars = bars_of([101.0] * 4, highs=[105.0] * 4, lows=[1.0] * 4)
    v, _ = verdict_at(bars, 3, book_of(slow=slow), direction=T.UP)
    assert v == T.PENDING


def test_close_exactly_at_line_is_not_above():
    """收盘恰好等于线价：严格大于才算「站上」。"""
    slow = [100.0] * 4
    bars = bars_of([100.0] * 4)
    v, _ = verdict_at(bars, 3, book_of(slow=slow))
    assert v == T.PENDING


# ==================================================== 三、慢路径只认第 3 根

def test_slow_path_ignores_the_middle_bars():
    """★ 跳读语义：第 0 根站上、第 1、2 根跌破、第 3 根又站上 → 失效。

    滚动窗口语义在这里会「因为中间跌破而重置计数」，判成不失效。
    用户的「只认第 3 根」要求判成失效。
    """
    slow = [100.0] * 4
    bars = bars_of([101.0, 90.0, 90.0, 101.0])
    v, ev = verdict_at(bars, 3, book_of(slow=slow))
    assert v == T.VIOLATED, "中间根被当成重置条件了 —— 那是滚动窗口，不是跳读"
    assert ev["slow"]["prev_hit"] is True and ev["slow"]["now_hit"] is True


def test_slow_path_only_the_third_bar_decides():
    """★ 只认第 3 根：前 2 根都站上、第 3 根跌破 → **不**失效。"""
    slow = [100.0] * 4
    bars = bars_of([101.0, 101.0, 101.0, 99.0])
    v, ev = verdict_at(bars, 3, book_of(slow=slow))
    assert v == T.PENDING, "第 3 根跌破了却判成失效"
    assert ev["slow"]["now_hit"] is False


def test_slow_path_does_not_fire_before_the_third_bar():
    """第 3 根之前不能提前下结论 —— 三值里的 pending 就是干这个的。"""
    slow = [100.0] * 4
    bars = bars_of([101.0, 101.0, 101.0, 101.0])
    book = book_of(slow=slow)
    assert verdict_at(bars, 0, book)[0] == T.PENDING
    assert verdict_at(bars, 1, book)[0] == T.PENDING
    assert verdict_at(bars, 2, book)[0] == T.PENDING
    assert verdict_at(bars, 3, book)[0] == T.VIOLATED


def test_slow_path_needs_history_beyond_the_lookback():
    """历史不足 3 根时慢路径根本不该评，且要说出原因。"""
    slow = [100.0] * 2
    bars = bars_of([101.0, 101.0])
    v, ev = verdict_at(bars, 1, book_of(slow=slow))
    assert v == T.PENDING
    assert "历史不足" in ev["slow"]["why"]


# ==================================================== 四、EMA 用当根自己的值

def test_slow_path_compares_each_bar_with_its_own_ema():
    """★ 移动参照：第 0 根用它自己的 ema(100)，第 3 根用 ema(97)。

    如果错误地把「当前这根」的线值套到两根上（用 97 判第 0 根），
    第 0 根的 98.5 > 97 就会被判成「站上」，从而得出失效 —— 反了。
    这条测试就是为了抓这个错法。
    """
    slow = [100.0, 99.0, 98.0, 97.0]
    bars = bars_of([98.5] * 4)
    v, ev = verdict_at(bars, 3, book_of(slow=slow))
    assert v == T.PENDING, "第 0 根被拿第 3 根的线值判了（固定/错位参照）"
    assert ev["slow"]["prev_line"] == 100.0
    assert ev["slow"]["now_line"] == 97.0


def test_moving_reference_catches_what_a_fixed_reference_would_miss():
    """★ 固定参照会得出相反结论的第二个例子。

    ema 前 3 根 95、第 3 根升到 100。收盘 99/99/99/98：
      移动参照：第 0 根 99>95 算站上；第 3 根 98>100 不成立 → **不失效**
      固定参照(95)：两根都「站上」→ 失效
    """
    slow = [95.0, 95.0, 95.0, 100.0]
    bars = bars_of([99.0, 99.0, 99.0, 98.0])
    v, ev = verdict_at(bars, 3, book_of(slow=slow))
    assert v == T.PENDING
    assert ev["slow"]["prev_line"] == 95.0
    assert ev["slow"]["now_line"] == 100.0


# ==================================================== 五、快路径独立结算

def test_fast_path_fires_immediately_without_waiting_for_third_bar():
    """★ 用户：「别傻傻的等第三根！独立结算！」

    第 1 根收盘就站上 ema20@30m —— 此时慢路径还差 2 根，必须直接失效。
    """
    slow = [100.0] * 4
    fast = [95.0, 95.0, 95.0]
    bars = bars_of([99.0, 96.0, 99.0, 99.0])   # 第 1 根收盘 96 > 95
    v, ev = verdict_at(bars, 1, book_of(slow=slow, fast=fast))
    assert v == T.VIOLATED, "快路径等了第 3 根（没有独立结算）"
    assert ev["paths"] == ["fast"]
    assert ev["fast"]["line"] == 95.0 and ev["fast"]["close"] == 96.0


def test_fast_path_does_not_add_its_own_confirmation_bars():
    """★ 快路径自己就是确认，不需要再多等几根。

    第 1 根收盘 96 站上 95 → 第 1 根就失效，而不是等到第 2/3 根。
    """
    slow = [100.0] * 5
    fast = [95.0, 95.0, 95.0]
    bars = bars_of([99.0, 96.0, 96.0, 96.0, 96.0])
    book = book_of(slow=slow, fast=fast)
    assert verdict_at(bars, 1, book)[0] == T.VIOLATED
    # 第二根同样成立，但关键点是第一根就成立
    assert verdict_at(bars, 2, book)[0] == T.VIOLATED


def test_fast_path_unavailable_at_first_bar_reports_why():
    """第 0 根收盘时还没有任何已收盘的 30m 桶 → 快路径不评，且要说出原因。"""
    slow = [100.0] * 4
    fast = [1.0, 1.0]
    bars = bars_of([99.0, 99.0, 99.0, 99.0])
    v, ev = verdict_at(bars, 0, book_of(slow=slow, fast=fast))
    assert v == T.PENDING
    assert ev["fast"]["line"] is None
    assert "就绪值" in ev["fast"]["why"]


def test_fast_path_uses_the_closed_30m_bucket_not_the_pending_one():
    """★ 快路径也必须防偷未来：i=1 用 30m 第 0 桶（90），不能用第 1 桶（200）。"""
    slow = [100.0] * 4
    fast = [90.0, 200.0]
    bars = bars_of([99.0, 100.0, 100.0, 100.0])
    book = book_of(slow=slow, fast=fast)
    v1, ev1 = verdict_at(bars, 1, book)
    assert ev1["fast"]["line"] == 90.0, "用了还没收盘的 30m 第 1 桶"
    assert v1 == T.VIOLATED          # 100 > 90
    # i=3 时第 1 桶已可用（200），100 > 200 不成立
    v3, ev3 = verdict_at(bars, 3, book)
    assert ev3["fast"]["line"] == 200.0
    assert v3 == T.PENDING


def test_both_paths_can_fire_and_are_reported():
    slow = [90.0] * 4
    fast = [90.0, 90.0]
    bars = bars_of([99.0, 99.0, 99.0, 99.0])
    v, ev = verdict_at(bars, 3, book_of(slow=slow, fast=fast))
    assert v == T.VIOLATED
    assert set(ev["paths"]) == {"slow", "fast"}


# ==================================================== 六、Thesis 校验

def test_short_thesis_rejects_stop_below_entry():
    """做空的止损放在入场价下面 = 永远打不到 = 回测里从不亏钱。"""
    with pytest.raises(ValueError, match="做空"):
        T.Thesis("t", "k", T.DOWN, order_price=100.0,
                 stop_price=90.0, target_price=80.0).validate()


def test_short_thesis_rejects_target_above_entry():
    with pytest.raises(ValueError, match="做空"):
        T.Thesis("t", "k", T.DOWN, order_price=100.0,
                 stop_price=110.0, target_price=120.0).validate()


def test_long_thesis_rejects_mirrored_prices():
    with pytest.raises(ValueError, match="做多"):
        T.Thesis("t", "k", T.UP, order_price=100.0,
                 stop_price=110.0, target_price=90.0).validate()


def test_thesis_rejects_unknown_direction():
    with pytest.raises(ValueError, match="direction"):
        T.Thesis("t", "k", "sideways", 100.0, 110.0, 90.0).validate()


def test_thesis_rejects_negative_horizon():
    with pytest.raises(ValueError, match="horizon"):
        T.Thesis("t", "k", T.DOWN, 100.0, 110.0, 90.0,
                 horizon_bars=-1).validate()


# ==================================================== 七、状态机

def test_invalidated_means_never_placed():
    """★ INVALIDATED：预期没成立就先被推翻 → **一张单都不许发**。"""
    bars = bars_of([60.0, 60.0, 60.0, 60.0])     # 收盘远高于慢线 50
    res = T.run(short_thesis(), bars, book_of(slow=[50.0] * 4),
                claim=never_claim)
    assert res.state == T.INVALIDATED
    assert res.actions == [], "INVALIDATED 却发了单（那是 CANCELLED 的语义）"
    assert res.transitions[-1].bar_ts == T0 + 3 * M15


def test_claim_arms_the_thesis_and_emits_place_order():
    # ★ 影线必须压在挂单价 100 下面：挂单是在 claim 成立那根**收盘时**才下的，
    # 那一根不可能成交。若 high 已经 ≥100，下一根就会立刻成交（见下一条测试）。
    bars = bars_of([99.0, 99.0], highs=[99.0, 99.0])
    res = T.run(short_thesis(), bars, flat_book(2))
    assert res.state == T.ARMED
    assert len(res.actions) == 1
    a = res.actions[0]
    assert a.kind == "place_order" and a.side == "sell" and a.price == 100.0
    # ★ 挂单类型必须一起发出去：同一个价位，LIMIT 和 STOP 的成交条件相反。
    assert a.order_type == T.ORDER_LIMIT


def test_cancelled_means_was_armed_then_invalidated():
    """★ CANCELLED：挂上了但预期破功 → 撤单。与 INVALIDATED 必须分开。"""
    bars = bars_of([60.0, 60.0, 60.0, 60.0],
                   highs=[61.0] * 4)             # 永远涨不到 200，不会成交
    th = short_thesis(order=200.0, stop=250.0, target=150.0)
    res = T.run(th, bars, book_of(slow=[50.0] * 4))
    assert res.state == T.CANCELLED
    assert [a.kind for a in res.actions] == ["place_order", "cancel"]
    assert res.actions[-1].reason == "thesis_invalidated"


def test_armed_then_price_reaches_order_fills():
    bars = bars_of([99.0, 99.0], highs=[99.0, 105.0])
    res = T.run(short_thesis(), bars, flat_book(2))
    assert T.FILLED in res.states()
    assert res.state == T.FILLED


# ---------------------------------------------------------------- 订单类型
#
# ★ 同一个价位，LIMIT 和 STOP 的成交条件**完全相反**。用户 2026-11-17 定的
#   三条形态里有两条是 STOP（突破才进），如果状态机只按方向判成交，
#   那两条的成交会被判反 —— 而且不会报错，只是所有统计一起变得没意义。

def test_sell_stop_fills_when_price_breaks_down_through_it():
    """做空 STOP：跌破挂单价才成交（突破挂法）。

    合法挂法：现价 105 在挂单价 100 的**上方**（SELL STOP 必须挂在下方）。
    """
    th = T.Thesis("t-stop", "k", T.DOWN, 100.0, 110.0, 90.0,
                  order_type=T.ORDER_STOP)
    bars = bars_of([105.0, 105.0], highs=[106.0, 106.0], lows=[104.0, 99.0])
    res = T.run(th, bars, flat_book(2))
    assert T.FILLED in res.states()
    assert res.actions[0].order_type == T.ORDER_STOP
    assert res.actions[0].side == "sell"


def test_sell_stop_does_not_fill_while_price_stays_above_it():
    """★ 反证：价格站在挂单价上方不动，STOP 空单不该成交。

    同一组价位用 LIMIT 会怎样？做空 LIMIT 必须挂在现价**上方**，
    所以这里 100 低于现价 105 —— 对 LIMIT 来说是非法挂法（`Plan.validate`
    会直接拒绝）。这就是 `order_type` 存在的意义：它决定价位合法区间，
    也决定「什么时候算成交」，两者都不能只看方向。
    """
    th = T.Thesis("t-stop", "k", T.DOWN, 100.0, 110.0, 90.0,
                  order_type=T.ORDER_STOP)
    bars = bars_of([105.0, 106.0], highs=[106.0, 107.0], lows=[104.0, 105.0])
    res = T.run(th, bars, flat_book(2))
    assert res.state == T.ARMED, "STOP 空单在价格没跌破时就成交了"
    assert T.FILLED not in res.states()


def test_buy_stop_fills_when_price_breaks_up_through_it():
    """做多 STOP：涨破挂单价才成交（倒 V 反包就是这个语义）。

    合法挂法：现价 95 在挂单价 100 的**下方**（BUY STOP 必须挂在上方）。
    """
    th = T.Thesis("t-bstop", "k", T.UP, 100.0, 90.0, 110.0,
                  order_type=T.ORDER_STOP)
    bars = bars_of([95.0, 95.0], highs=[96.0, 101.0], lows=[94.0, 94.0])
    res = T.run(th, bars, flat_book(2))
    assert T.FILLED in res.states()
    assert res.actions[0].side == "buy"


def test_buy_stop_does_not_fill_while_price_stays_below_it():
    """反证：做多 STOP 在价格没涨破之前必须一直挂着。

    ★ 做多 thesis 的夹具不能用 `flat_book`：它把两条线放在 1e6，
      意思是「任何收盘都在线下方」—— 那是**做空**的友好夹具，
      放到做多上就是「一出生就失效」，于是这里会看到 CANCELLED 而不是 ARMED。
      做多要把线放在价格下方。
    """
    th = T.Thesis("t-bstop", "k", T.UP, 100.0, 90.0, 110.0,
                  order_type=T.ORDER_STOP)
    bars = bars_of([95.0, 96.0], highs=[97.0, 98.0], lows=[94.0, 95.0])
    book = book_of(slow=[50.0, 50.0], fast=[50.0, 50.0])
    res = T.run(th, bars, book)
    assert res.state == T.ARMED
    assert T.FILLED not in res.states()


def test_buy_limit_fills_on_a_dip_while_buy_stop_does_not():
    """★ 同一个价位、同一个方向，LIMIT 与 STOP 的结论相反 —— 一行对照。

    现价 105，两个都是做多、挂单都在 100：
        做多 LIMIT（回落接）→ 合法（挂单价在现价下方），价格跌到 100 成交
        做多 STOP（涨破追） → 挂单价在现价下方是**非法**挂法
    所以这里只验证 LIMIT 会成交、而 STOP 在同样价位上根本不合法。
    """
    import pytest as _pytest

    limit = T.Thesis("t-l", "k", T.UP, 100.0, 90.0, 110.0,
                     order_type=T.ORDER_LIMIT)
    bars = bars_of([105.0, 105.0], highs=[106.0, 106.0], lows=[104.0, 99.0])
    res = T.run(limit, bars, flat_book(2))
    assert T.FILLED in res.states()

    stop = T.Thesis("t-s", "k", T.UP, 100.0, 90.0, 110.0,
                    order_type=T.ORDER_STOP)
    # 做多 STOP 的止损在挂单价下方、止盈在上方，价位本身自洽；
    # 但「挂单价低于现价」这件事由 Plan.validate 拦，不在 Thesis 里。
    stop.validate()
    assert stop.triggers_when_price_falls is False
    assert limit.triggers_when_price_falls is True


def test_action_carries_the_order_type():
    th = T.Thesis("t-bstop", "k", T.UP, 100.0, 90.0, 110.0,
                  order_type=T.ORDER_STOP)
    bars = bars_of([95.0, 95.0], highs=[96.0, 96.0], lows=[94.0, 94.0])
    res = T.run(th, bars, flat_book(2))
    a = res.actions[0]
    assert a.kind == "place_order"
    assert a.order_type == T.ORDER_STOP
    assert a.to_dict()["order_type"] == T.ORDER_STOP


def test_thesis_validate_rejects_unknown_order_type():
    import pytest as _pytest
    with _pytest.raises(ValueError, match="order_type"):
        T.Thesis("t", "k", T.UP, 100.0, 90.0, 110.0,
                 order_type="ICEBERG").validate()


def test_armed_expires_after_horizon():
    bars = bars_of([99.0] * 4, highs=[99.0] * 4)
    res = T.run(short_thesis(horizon=2), bars, flat_book(4))
    assert res.state == T.EXPIRED
    assert res.actions[-1].kind == "cancel"
    assert res.actions[-1].reason == "thesis_expired"


def test_filled_then_stop_hits():
    bars = bars_of([99.0, 99.0, 99.0], highs=[105.0, 115.0, 99.0])
    res = T.run(short_thesis(), bars, flat_book(3))
    assert res.state == T.CLOSED_SL
    assert res.actions[-1].price == 110.0


def test_filled_then_target_hits():
    # 第 0 根是「预期成立→挂单」，那一根不可能成交；第 1 根价格走到才成交；
    # 第 2 根打到止盈。
    bars = bars_of([99.0, 99.0, 99.0],
                   highs=[99.0, 105.0, 99.0],
                   lows=[95.0, 95.0, 85.0])
    res = T.run(short_thesis(), bars, flat_book(3))
    assert res.state == T.CLOSED_TP
    assert res.actions[-1].price == 90.0


def test_filled_then_thesis_invalidated_exits_at_close():
    """成交后现实不符合预期 → 按收盘价平掉（不是止损也不是止盈）。"""
    bars = bars_of([60.0, 60.0, 60.0, 60.0],
                   highs=[59.0, 105.0, 61.0, 61.0],
                   lows=[59.0] * 4)              # 到不了 target=30
    th = short_thesis(order=100.0, stop=130.0, target=30.0)
    res = T.run(th, bars, book_of(slow=[50.0] * 4))   # 收盘 60 > 50 → 第 3 根失效
    assert res.state == T.CLOSED_THESIS
    last = res.actions[-1]
    assert last.kind == "close" and last.reason == "thesis_invalidated"
    assert last.price == 60.0


def test_same_bar_stop_and_target_resolves_to_stop():
    """★ 同根既打止损又打止盈 → 按止损（保守）。

    OHLC 没有 bar 内顺序，这是数据缺陷。反过来说「先止盈」等于
    靠运气把回测做漂亮。
    """
    bars = bars_of([99.0, 99.0], highs=[105.0, 115.0], lows=[95.0, 85.0])
    res = T.run(short_thesis(), bars, flat_book(2))
    assert res.state == T.CLOSED_SL, "同根双杀却按止盈算 —— 回测会虚假变好"


def test_same_bar_fill_and_invalidation_does_not_cancel():
    """★ 同根既成交又失效 → 先成交再平掉，**不许**判成「取消」。

    判成 CANCEL 等于「事后挑掉坏单」，那是回测作弊。
    """
    bars = bars_of([60.0, 60.0, 60.0, 60.0],
                   highs=[101.0, 101.0, 101.0, 110.0],
                   lows=[59.0] * 4)
    th = short_thesis(order=105.0, stop=130.0, target=30.0)
    res = T.run(th, bars, book_of(slow=[50.0] * 4))
    states = res.states()
    assert T.CANCELLED not in states, "同根成交+失效被判成取消（回测作弊）"
    assert T.FILLED in states
    assert res.state == T.CLOSED_THESIS


def test_reversal_thesis_does_not_die_on_ema():
    """★ **反转**家族不用 ema 判失效（用户 2026-11-17 拍板：只认止损价）。

    旧行为：收盘跌破 ema → INVALIDATED。
    但反转里的做多是**大跌抄底**，一出生就在 ema20 下方 ——
    旧判据一出生就成立，于是每条都在第一根死掉，而且不报错。

    ★ 这条必须显式写 `family=reversal`：判据的轴是**家族**，不是方向。
      以前这里靠「direction == UP 就跳过」通过 —— 那条捷径在镜像上线后
      两头都错（反转有做空、趋势有做多），所以那个参数已经被删掉了。
    """
    th = T.Thesis("t2", "上涨镜像", T.UP, order_price=100.0,
                  stop_price=90.0, target_price=110.0,
                  family=T.FAMILY_REVERSAL)
    bars = bars_of([140.0, 140.0, 140.0, 140.0])   # 收盘远低于慢线 150
    res = T.run(th, bars, book_of(slow=[150.0] * 4), claim=never_claim)
    assert res.state != T.INVALIDATED, (
        "反转又被 ema 判死了；用户口径是只认止损价。states=%s"
        % res.states())
    assert res.state == T.EXPECTED


def test_reversal_short_also_does_not_die_on_ema():
    """★ 反转的**做空**（上涨里的摸顶）同样不看 ema。

    这是原缺陷的另一半：旧代码只对 `direction == UP` 跳过，于是
    镜像出来的反转空单照样被 ema 白杀（实测 4/6 条在挂单前就 CANCELLED）。
    """
    th = T.Thesis("t3", "上涨逆向倒V反包突破限价单", T.DOWN,
                  order_price=110.0, stop_price=120.0, target_price=90.0,
                  order_type=T.ORDER_STOP, family=T.FAMILY_REVERSAL)
    # 收盘一路在慢线下方 —— 对做空来说这是「没站上」，本来就不算打脸；
    # 真正要钉的是：即使收盘@160 站上慢线 150，反转也不判失效。
    bars = bars_of([160.0, 160.0, 160.0, 160.0])
    res = T.run(th, bars, book_of(slow=[150.0] * 4), claim=never_claim)
    assert res.state != T.INVALIDATED, (
        "反转空单又被 ema 判死了（这就是 §23.23 的缺陷）。states=%s"
        % res.states())


def test_trend_long_dies_when_price_closes_below_ema():
    """★ **趋势做多**（上涨里的回调做多）跌破 ema 就是失效。

    这是原缺陷的第三块：旧代码对 `direction == UP` 一律跳过，
    于是趋势多单被**白豁免**，永远不会因 ema 失效。
    """
    th = T.Thesis("t4", "上涨中继EMA多次拒绝", T.UP, order_price=100.0,
                  stop_price=90.0, target_price=110.0,
                  family=T.FAMILY_TREND)
    bars = bars_of([140.0, 140.0, 140.0, 140.0])   # 收盘跌破慢线 150
    res = T.run(th, bars, book_of(slow=[150.0] * 4), claim=never_claim)
    assert res.state == T.INVALIDATED, (
        "趋势多单跌破 ema 却没判失效（趋势没在继续上涨）。states=%s"
        % res.states())


def test_family_defaults_to_trend_not_exempt():
    """★ 没传家族时按**趋势**判 —— 保守方向。

    默认若取「豁免」，等于任何忘了传家族的调用点都静默丢掉 ema 判据，
    而这正是原缺陷的形状（不报错、只是判据没了）。
    """
    th = T.Thesis("t5", "忘了说家族", T.UP, order_price=100.0,
                  stop_price=90.0, target_price=110.0)
    assert th.family == T.FAMILY_TREND
    bars = bars_of([140.0, 140.0, 140.0, 140.0])
    res = T.run(th, bars, book_of(slow=[150.0] * 4), claim=never_claim)
    assert res.state == T.INVALIDATED, res.states()


def test_down_thesis_still_dies_on_ema():
    """★ 反证：做空（反弹做空，顺势）的 ema 判据**不能**被一起改掉。

    修极性的时候很容易把两边一起删 —— 那会把做空的失效也变成死的。
    """
    th = short_thesis(order=50.0, stop=60.0, target=40.0)
    bars = bars_of([160.0, 160.0, 160.0, 160.0])   # 收盘远高于慢线 150
    res = T.run(th, bars, book_of(slow=[150.0] * 4), claim=never_claim)
    assert res.state == T.INVALIDATED, res.states()


def test_terminal_state_stops_the_loop():
    """到了终态就停，不许在终态上继续产生转换。"""
    bars = bars_of([60.0] * 6, highs=[61.0] * 6)
    th = short_thesis(order=200.0, stop=250.0, target=150.0)
    res = T.run(th, bars, book_of(slow=[50.0] * 6))
    assert res.state == T.CANCELLED
    assert res.transitions[-1].bar_ts == T0 + 3 * M15
    assert res.terminal is True


# ==================================================== 八、可回溯

def test_every_transition_carries_evidence():
    """每次转换都必须带证据：判在哪根、比了什么值、走了哪条路径。"""
    bars = bars_of([60.0, 60.0, 60.0, 60.0], highs=[61.0] * 4)
    th = short_thesis(order=200.0, stop=250.0, target=150.0)
    res = T.run(th, bars, book_of(slow=[50.0] * 4))
    for tr in res.transitions:
        assert tr.bar_ts > 0
        assert tr.reason, "转换没有理由"
        assert tr.evidence, "转换没有证据"
    canc = res.transitions[-1]
    assert canc.to_state == T.CANCELLED
    assert canc.evidence["slow"]["prev_close"] == 60.0
    assert canc.evidence["slow"]["now_close"] == 60.0


def test_trace_records_one_verdict_per_bar():
    bars = bars_of([99.0] * 5)
    res = T.run(short_thesis(), bars, flat_book(5), trace=True)
    assert len(res.trace) >= 1
    assert all("verdict" in r and "evidence" in r for r in res.trace)


def test_states_and_reasons_helpers():
    bars = bars_of([60.0, 60.0, 60.0, 60.0], highs=[61.0] * 4)
    th = short_thesis(order=200.0, stop=250.0, target=150.0)
    res = T.run(th, bars, book_of(slow=[50.0] * 4))
    assert res.states() == [T.EXPECTED, T.ARMED, T.CANCELLED]
    assert len(res.reasons()) == 2


def test_claim_evidence_is_recorded_on_arming():
    def claim(i, bars, book):
        return True, {"pattern": "三引线", "bar": i}

    bars = bars_of([99.0, 99.0])
    res = T.run(short_thesis(), bars, flat_book(2), claim=claim)
    assert res.transitions[0].evidence["claim"]["pattern"] == "三引线"


# ============================================ 九、双节奏：入场在 5m，失效在 15m
#
# 用户口径：「入场全部在 5m K，K线演化和区间演化就用原先已经上线的周期」。
# 这一节钉住的是**两个时钟各自管什么**，以及它们交界处的判定顺序。

M5 = 300_000


def calm(n_bars, direction=T.UP):
    """把两条 EMA 放到「这一整套 bar 永远不会判失效」的那一侧。

    ★ `flat_book` 把线放在 1e6 —— 那对**做空**才是「价格站不上去」，
      对**做多**反而是「价格永远在下方」= 每根都判失效。
      做多的夹具必须把线放在下面（1.0），否则测出来的不是双节奏，
      而是「一上来就被推翻」。
    """
    far = 1e6 if direction == T.DOWN else 1.0
    return flat_book(n_bars, slow=far, fast=far)


def _m5(ts, c, h, l=None):
    return Bar(int(ts), c, h, float(l if l is not None else c - 1.0), c, 1.0)


def subs_of(bar15, specs):
    """在一根 15m 的窗口内造 5m bar。specs = [(close, high, low), ...]。"""
    return [_m5(int(bar15.ts) + k * M5, c, h, lo)
            for k, (c, h, lo) in enumerate(specs)]


def test_bucket_alignment_uses_open_time():
    """5m 归桶必须按**开盘时刻**对齐，与 line_ready_ms 的口径一致。

    15m 的 ts=T 覆盖 [T, T+15m)。一根 ts=T+10m 的 5m 属于桶 T，
    不是桶 T+15m —— 以前 pending_plan 用「往后找第一根 ts >= 收盘」，
    等于把 5m 信号硬拖成「再等一根 15m」，这是信号被吃掉的机制。
    """
    assert T._bucket_of(T0, M15) == T0
    assert T._bucket_of(T0 + 10 * 60_000, M15) == T0
    assert T._bucket_of(T0 + 14 * 60_000, M15) == T0
    assert T._bucket_of(T0 + 15 * 60_000, M15) == T0 + M15


def test_5m_order_of_events_beats_coarse_hl():
    """★ 这一条是双节奏存在的理由。

    做多 STOP 挂在 100：涨破才追多；止损 95。

    15m 那根 h=105 l=90 —— 只看粗 h/l 的话，「涨破 100 成交」和
    「下探 90 止损」在同一根里，成交后立刻就被判止损（实测旧行为就是
    CLOSED_SL）。但真实的先后是：
        5m#0 先下探到 90（那时**还没挂上单**，不构成止损）
        5m#1 才涨破 100（这时才成交，之后最低只到 99，没碰 95）
    所以正确结论是**成交且持仓中**，不是被止损。
    """
    th = T.Thesis(thesis_id="v", kind="倒V", direction=T.UP,
                  order_price=100.0, stop_price=95.0, target_price=120.0,
                  order_type=T.ORDER_STOP)
    b15 = Bar(T0, 97.0, 105.0, 90.0, 100.0, 1.0)
    subs = subs_of(b15, [(91.0, 92.0, 90.0), (100.0, 105.0, 99.0)])

    coarse = T.run(th, [b15], calm(1), initial_state=T.ARMED)
    assert coarse.state == T.CLOSED_SL, "粗粒度下会误判止损（这是被替代的行为）"

    fine = T.run(th, [b15], calm(1), initial_state=T.ARMED,
                 entry_bars=subs)
    assert fine.state == T.FILLED, fine.states()


def test_stop_and_target_are_judged_on_5m_hl():
    """止损止盈用 5m 的 h/l，且转换的 bar_ts 必须是**那根 5m** 的时刻。"""
    th = T.Thesis(thesis_id="v", kind="倒V", direction=T.UP,
                  order_price=100.0, stop_price=90.0, target_price=110.0,
                  order_type=T.ORDER_STOP)
    b15 = Bar(T0, 100.0, 112.0, 89.0, 100.0, 1.0)
    subs = subs_of(b15, [(100.0, 101.0, 99.0),   # 成交在这一根（h=101>=100）
                         (105.0, 107.0, 104.0),
                         (110.0, 112.0, 108.0)])  # 止盈在这里（h=112>=110）
    res = T.run(th, [b15], calm(1), initial_state=T.ARMED,
                entry_bars=subs)
    assert res.state == T.CLOSED_TP, res.states()
    tp = [t for t in res.transitions if t.to_state == T.CLOSED_TP][0]
    assert tp.bar_ts == int(b15.ts) + 2 * M5, (
        "止盈应该判在第 3 根 5m 上，实际 %d" % tp.bar_ts)


def test_stop_on_a_5m_bar_is_not_rejudged_by_the_coarse_bar():
    """★ 粗周期收盘不许再拿 15m 的 h/l 补一刀。

    做多 STOP @100，止损 90。5m：先成交，之后一路在 95 上方，最低 92
    （没碰 90）。15m 那根的 l=89 —— 若收盘用粗 h/l 再判一次，就会
    误报止损。用户要的是「跟入场一致，都用 5m 的 h/l」。
    """
    th = T.Thesis(thesis_id="v", kind="倒V", direction=T.UP,
                  order_price=100.0, stop_price=90.0, target_price=130.0,
                  order_type=T.ORDER_STOP)
    b15 = Bar(T0, 100.0, 105.0, 89.0, 100.0, 1.0)
    subs = subs_of(b15, [(100.0, 101.0, 99.0),
                         (96.0, 97.0, 92.0),
                         (98.0, 99.0, 93.0)])
    res = T.run(th, [b15], calm(1), initial_state=T.ARMED,
                entry_bars=subs)
    assert res.state == T.FILLED, (
        "5m 上最低 92 没碰止损 90，不该平仓；实际 %s" % res.states())


def test_invalidation_is_still_decided_on_the_15m_close_only():
    """★ 反证：失效不许被 5m 收盘提前触发。

    窗口里第 2、3 根 5m 收盘都站上了 ema20@30m（101、102），但
    15m 收盘回到 99 —— 用户口径「只要收盘那一刻价格在 ema20 下面，就算无效」
    指的是 **15m 收盘**。若错在 5m 上判失效，这里会 CANCELLED。
    """
    th = short_thesis(order=200.0, stop=250.0, target=150.0)
    b15 = Bar(T0, 99.0, 103.0, 98.0, 99.0, 1.0)
    subs = subs_of(b15, [(99.0, 100.0, 98.0),
                         (101.0, 103.0, 100.0),      # 5m 收盘站上 100
                         (99.0, 102.0, 98.0)])       # 15m 收盘 99，回到线下方
    book = book_of(slow=[50.0, 50.0], fast=[100.0, 100.0])
    res = T.run(th, [b15], book, initial_state=T.ARMED, entry_bars=subs)
    assert res.state == T.ARMED, (
        "5m 中途站上不该判失效，只有 15m 收盘才算；实际 %s" % res.states())


def test_invalidation_does_fire_on_the_15m_close():
    """反证的对照面：15m 收盘站上去了，就必须撤单。"""
    th = short_thesis(order=200.0, stop=250.0, target=150.0)
    b0 = Bar(T0, 99.0, 100.0, 98.0, 99.0, 1.0)          # 收盘 99 < 100，没事
    b1 = Bar(T0 + M15, 99.0, 103.0, 98.0, 101.0, 1.0)   # 收盘 101 > 100 → 撤单
    subs = (subs_of(b0, [(99.0, 100.0, 98.0),
                         (99.5, 100.0, 99.0),
                         (99.0, 100.0, 98.5)])
            + subs_of(b1, [(99.0, 99.5, 98.5),
                           (100.5, 101.0, 100.0),
                           (101.0, 103.0, 100.0)]))
    book = book_of(slow=[50.0, 50.0], fast=[100.0, 100.0])
    res = T.run(th, [b0, b1], book, initial_state=T.ARMED, entry_bars=subs)
    assert res.state == T.CANCELLED, res.states()
    cancel = [t for t in res.transitions if t.to_state == T.CANCELLED][0]
    assert cancel.bar_ts == int(b1.ts), (
        "撤单必须挂在 15m 那根的 ts 上，实际 %d" % cancel.bar_ts)


def test_claim_on_entry_arms_at_the_5m_signal_not_the_next_15m():
    """★ 倒V：声明必须在信号那根 5m 的收盘上，不是下一根 15m。

    信号在窗口第 2 根 5m（ts=T+5m）成立 → 就该在 T+5m 挂单；
    等 15m 收盘（T+15m，晚 10 分钟）价格往往已经涨破触发价。
    """
    signal_ts = T0 + 5 * 60_000
    armed = []

    def claim(j, ebars, book):
        ts = int(ebars[j].ts)
        armed.append(ts)
        return (ts == signal_ts), {"on": ts}

    th = T.Thesis(thesis_id="v", kind="倒V", direction=T.UP,
                  order_price=200.0, stop_price=90.0, target_price=300.0,
                  order_type=T.ORDER_STOP)
    b15 = Bar(T0, 100.0, 101.0, 99.0, 100.0, 1.0)
    subs = subs_of(b15, [(100.0, 100.5, 99.0),
                         (101.0, 101.5, 100.0),
                         (102.0, 102.5, 101.0)])
    res = T.run(th, [b15], calm(1), claim=claim,
                entry_bars=subs, claim_on_entry=True)
    assert res.state == T.ARMED, res.states()
    arm = [t for t in res.transitions if t.to_state == T.ARMED][0]
    assert arm.bar_ts == signal_ts, (
        "应在 5m 信号那根收盘挂单（%d），实际 %d" % (signal_ts, arm.bar_ts))


def test_without_claim_on_entry_the_same_signal_waits_for_the_15m_close():
    """对照面：claim_on_entry=False 时，同样的信号要等到 15m 收盘。

    这两条一起说明「三引线/EMA 拒绝仍按 15m 确认」是**有意保留**的，
    而不是顺手把三条形态都拖到 5m。
    """
    signal_ts = T0 + 5 * 60_000

    def claim(j, ebars, book):
        ts = int(ebars[j].ts)
        return (ts == signal_ts), {"on": ts}

    th = T.Thesis(thesis_id="v", kind="三引线", direction=T.UP,
                  order_price=200.0, stop_price=90.0, target_price=300.0,
                  order_type=T.ORDER_LIMIT)
    b15 = Bar(T0, 100.0, 101.0, 99.0, 100.0, 1.0)
    subs = subs_of(b15, [(100.0, 100.5, 99.0),
                         (101.0, 101.5, 100.0),
                         (102.0, 102.5, 101.0)])

    # 粗周期时钟上声明必须返回真（这里用一个只在粗时钟成立的 claim）
    def coarse_claim(i, bars, book):
        return True, {"coarse": i}

    res = T.run(th, [b15], calm(1), claim=coarse_claim,
                entry_bars=subs, claim_on_entry=False)
    assert res.state == T.ARMED, res.states()
    arm = [t for t in res.transitions if t.to_state == T.ARMED][0]
    assert arm.bar_ts == T0, arm.bar_ts


def test_no_5m_data_degrades_to_the_coarse_clock_unchanged():
    """反证：没给 5m 时行为必须和改造前**完全一致**（空列表也算没给）。"""
    th = T.Thesis(thesis_id="v", kind="倒V", direction=T.UP,
                  order_price=100.0, stop_price=90.0, target_price=110.0,
                  order_type=T.ORDER_STOP)
    b15 = Bar(T0, 100.0, 112.0, 89.0, 100.0, 1.0)
    a = T.run(th, [b15], calm(1), initial_state=T.ARMED)
    b = T.run(th, [b15], calm(1), initial_state=T.ARMED, entry_bars=[])
    assert a.states() == b.states()
    assert [t.bar_ts for t in a.transitions] == [t.bar_ts for t in b.transitions]


def test_a_bucket_without_5m_falls_back_to_that_15m_bar():
    """这一段没有 5m 数据 → 用这根 15m 判，不许整段失去判断力。"""
    th = T.Thesis(thesis_id="v", kind="倒V", direction=T.UP,
                  order_price=100.0, stop_price=90.0, target_price=110.0,
                  order_type=T.ORDER_STOP)
    b0 = Bar(T0, 100.0, 101.0, 99.0, 100.0, 1.0)
    b1 = Bar(T0 + M15, 105.0, 112.0, 104.0, 105.0, 1.0)   # 这根没有 5m
    subs = subs_of(b0, [(100.0, 101.0, 99.0)])
    res = T.run(th, [b0, b1], calm(2), initial_state=T.ARMED,
                entry_bars=subs)
    assert res.state == T.CLOSED_TP, (
        "没有 5m 的那根必须退回用 15m 判（h=112 触到止盈 110）；实际 %s"
        % res.states())


def test_start_at_defers_the_whole_thesis_until_its_declaration():
    """★ start_at：声明之前这条 thesis **还不存在**，不许被判失效。

    真实数据冒烟抓到的：把一条「某天某根才成立」的计划扔进一万七千根历史里
    跑，状态机从第 0 根就开始判失效 —— 而那时这条 thesis 根本没被声明过。
    结果 12/12 条计划在声明之前就被判 INVALIDATED，
    看起来像「形态全废」，其实是起点错了。

    这里前 5 根收盘都站在 ema 上方（对做空 = 已打脸），声明在第 5 根。
      * start_at=5 → 前 5 根不判 → 第 5 根收盘 99 站不上去 → ARMED
      * start_at=0 → 第 1 根就判掉 → INVALIDATED（这就是要避免的行为）
    """
    closes = [101.0, 101.0, 101.0, 101.0, 101.0, 99.0, 99.0]
    bars = bars_of(closes)
    book = book_of(slow=[99.0] * 7, fast=[100.0] * 5)
    # 挂单价放到 200（远高于所有 high），这样挂上之后不会被成交，
    # 状态就停在 ARMED —— 这一条只验「声明之前不判」，不掺成交
    th = short_thesis(order=200.0, stop=250.0, target=150.0)

    def claim(i, bs, bk):
        return i == 5, {"at": i}

    later = T.run(th, bars, book, claim=claim, start_at=5)
    assert later.state == T.ARMED, (
        "从声明那根开始算，不该在声明之前被判掉；实际 %s" % later.states())
    assert later.transitions[0].bar_ts == int(bars[5].ts)

    from_zero = T.run(th, bars, book, claim=claim)
    assert from_zero.state == T.INVALIDATED, (
        "反证：不设 start_at 时会在声明之前就被判掉（这正是要修的形状）；"
        "实际 %s" % from_zero.states())


def test_start_at_keeps_global_indices_for_the_slow_path():
    """start_at 只跳过判定，**不裁剪序列** —— 慢路径跳读第 3 根仍然成立。

    如果实现偷懒改成「切片后再跑」，前 3 根的慢路径就会退化成「历史不足」，
    失效会推迟 3 根才发生 —— 那是把判据放松了，且不报错。
    """
    closes = [101.0, 101.0, 101.0, 101.0, 101.0, 101.0, 101.0]
    bars = bars_of(closes)
    # 只给慢线，不给快线 → 只能靠「跳读第 3 根」那条路判失效
    book = book_of(slow=[100.0] * 7)
    th = short_thesis(order=50.0, stop=60.0, target=40.0)

    res = T.run(th, bars, book, claim=lambda i, b, k: (i == 3, {}), start_at=3)
    assert res.state in (T.CANCELLED, T.INVALIDATED), (
        "第 3 根声明后，慢路径（第 0 与第 3 根都在线上方）必须能判失效；"
        "实际 %s" % res.states())
    """挂单发生在收盘之后，本根的 h/l 属于过去 —— 不许拿它判成交。

    第 0 根自己就涨破 100（h=101），但挂单是它收盘之后的事；
    第 1 根最高只到 99.5，一直没破 100 → 结论只能是「挂着，没成交」。
    这条同时守住「声明那根不成交」和「不会因为声明引入前视」。
    """
    th = T.Thesis(thesis_id="v", kind="倒V", direction=T.UP,
                  order_price=100.0, stop_price=90.0, target_price=130.0,
                  order_type=T.ORDER_STOP)
    b0 = Bar(T0, 100.0, 101.0, 99.0, 100.0, 1.0)     # 自己就涨破 100
    subs0 = subs_of(b0, [(100.0, 101.0, 99.0)])
    b1 = Bar(T0 + M15, 99.5, 99.5, 98.0, 99.0, 1.0)  # 最高 99.5，没破 100
    subs1 = subs_of(b1, [(99.5, 99.5, 98.0)])
    res = T.run(th, [b0, b1], calm(2), claim=lambda i, b, k: (i == 0, {}),
                entry_bars=subs0 + subs1, claim_on_entry=True)
    assert res.state == T.ARMED, (
        "第 0 根不该成交（挂单在其收盘后），第 1 根没破 100 → 只能是 ARMED；"
        "实际 %s" % res.states())

