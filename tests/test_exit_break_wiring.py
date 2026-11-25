# -*- coding: utf-8 -*-
"""EXIT-1 接线验证：收盘跌破 / 有效跌破真的接在状态机里了。

`tests/test_break_rules.py` 钉的是**判读本身**（纯函数）。这个文件钉的是
**接线**：状态机在真实的 5m 序列上到底有没有按这条判据出场、出场价是谁、
归因写成什么。

两者必须分开测，因为接线断了的时候判读测试照样全绿 ——
那正是本项目历史上最贵的一类缺陷（判据写好了、零调用、什么都没发生）。

数据来源：2026-11-25 真实 5m K 线（用户投诉的那一笔）。
买点 58437.00，成交在 21:31:05。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trading import thesis as T            # noqa: E402

M15 = T.BAR_INTERVAL_MS
M5 = 300_000

#: 21:30 那根 5m 的开盘时刻（成交所在那根）
BAR_2130 = 1_795_616_400_000
T0 = BAR_2130 - 12 * M5          # 前面垫 12 根，凑够 15m 桶

LEVEL = 58437.00
#: 成交在 REAL_5M 的第 12 根（21:30）。
#:
#: ★ 为什么从第 11 根（21:25）开始当"预期已经存在"：
#:   · 从第 0 根起算的话，限价单在第 6 根就被打穿（那根低点 58376.90）——
#:     那就不是事故那一笔了。真实情况是这张单 20:00 挂着、21:31 才成交。
#:   · 状态机的规则是「挂单那根的 h/l 属于过去，成交从下一根才开始看」，
#:     所以要在第 11 根挂出去，成交才会正好落在第 12 根（21:30）——
#:     也就是用户说的「成交那根就收盘跌破」。
#:   · 第 11 根低点 58801.70 在买点上方，挂上去不会立刻成交。
FILL_IDX = 11
#: 成交那根在 REAL_5M 里的下标（21:30，收 58415.70）
FILL_BAR_IDX = 12

#: ★ `thesis.run(start_at=)` 是 **15m 下标**，不是 5m 下标。
#:   这个坑第一次写测试时踩了：传 11 进去，而 15m 序列只有 10 根 →
#:   一根都不进循环，结果是"状态机一次判定都没做"，看起来像判据没生效。
#:   所以这里按真实 15m 网格算：先把起点对齐到 15m，再求"挂单那根"落在哪个桶。
G15 = T0 - (T0 % M15)
#: 挂单（第 11 根 = 21:25）所在的 15m 桶下标
START15 = (T0 + FILL_IDX * M5 - G15) // M15


class Bar:
    def __init__(self, ts, o, h, l, c):
        self.ts, self.o, self.h, self.l, self.c = ts, o, h, l, c


#: 真实 5m（ts 下标, o, h, l, c）——事故当晚 21:00 起
REAL_5M = [
    (0, 58578.30, 58899.00, 58549.30, 58642.10),
    (1, 58642.10, 58703.70, 58610.00, 58629.70),
    (2, 58629.70, 58693.40, 58621.10, 58654.30),
    (3, 58654.30, 58688.20, 58626.00, 58642.70),
    (4, 58642.70, 58643.00, 58500.00, 58518.20),
    (5, 58518.20, 58543.30, 58471.20, 58502.40),
    (6, 58502.40, 58884.80, 58376.90, 58684.10),
    (7, 58684.10, 58831.20, 58520.20, 58707.80),
    (8, 58707.80, 58712.10, 58634.00, 58648.00),
    (9, 58648.00, 58674.00, 58542.60, 58561.40),
    (10, 58561.40, 58846.30, 58551.30, 58803.90),
    (11, 58803.90, 58859.90, 58801.70, 58831.30),
    # ★ 成交那根：收 58415.70，比买点低 21.3 点，vol 是邻居的 7~20 倍
    (12, 58831.20, 58884.80, 58204.00, 58415.70),
    # ★ 反弹那根：最高 58515.70，收回 58436.00（差 1 点没到买点）
    (13, 58415.70, 58515.70, 58360.10, 58436.00),
    (14, 58436.00, 58456.00, 58180.00, 58274.20),
    (15, 58274.20, 58362.50, 57882.00, 58296.00),
    (16, 58296.00, 58351.40, 58147.60, 58147.60),
]


def _bars5(rows=None):
    rows = REAL_5M if rows is None else rows
    return [Bar(T0 + i * M5, o, h, l, c) for i, o, h, l, c in rows]


def _bars15(n=10):
    """15m 只当"外层时钟"，价格不参与这条判据（用远离的价，避免误触别的规则）。

    ★ 必须从**15m 网格对齐**的 G15 起，否则 `_bucket_of` 划出来的桶和 5m
      的真实归属不一致，`subs` 取到的会是空 —— 那时状态机不会报错，
      只是"什么都判不出来"，比报错难查得多。
    """
    return [Bar(G15 + i * M15, 58600.0, 58601.0, 58599.0, 58600.0)
            for i in range(n)]


def _book(n=12):
    # 观察线放到天上去，确保「观察线拒绝」不会抢在收盘跌破前面出场
    bk = T.LineBook()
    for i in range(n):
        bk.add({"line_key": T.SLOW_LINE_KEY, "ts": G15 + i * M15,
                "line_price": 1e6, "line_ready_ms": G15 + i * M15 + M15})
    return bk


def _thesis(**kw):
    base = dict(thesis_id="t-break", kind="三引线", direction=T.UP,
                order_price=LEVEL, stop_price=LEVEL - 903.71,
                target_price=LEVEL + 3779.1, order_type=T.ORDER_LIMIT,
                family=T.FAMILY_REVERSAL, horizon_bars=100,
                stale_distance=0.0)
    base.update(kw)
    return T.Thesis(**base)


def _run(**kw):
    return T.run(_thesis(**kw), _bars15(), _book(),
                 entry_bars=_bars5(), start_at=START15, fill_bar_ts=BAR_2130,
                 claim_on_entry=True)


# ============================================== 一、真实事故的接线结果

def test_real_trade_exits_on_the_bounce_not_on_the_stop():
    """事故回放：出场必须是「收盘跌破后回抽离场」，不是止损。

    用户的判断：「你最开始的那个挂单，成交后，发现当跟就有效跌破，
    反弹回来就该离场的！」

    真实结果：21:30 收 58415.70（跌破）→ 21:35 反弹到 58515.70，
    我们的回抽位 58413.56 在 21:35 那根必然成交。
    **而实际系统一直扛到 22:43 才在 57454.60 认亏。**
    """
    res = _run()
    assert res.state == T.CLOSED_THESIS, res.states()
    tr = res.transitions[-1]
    assert tr.reason.startswith("收盘跌破后离场"), tr.reason
    ev = tr.evidence
    assert ev["break_state"] == "CLOSED_BELOW"
    assert ev["break_level"] == pytest.approx(LEVEL)
    # 回抽位在买点前方一步（BOUNCE_ATR = 0.10 ATR）
    assert ev["exit_level"] < LEVEL
    assert ev["exit_level"] > LEVEL - 60, "回抽位不能退得太远，否则等于不止损"
    # 出场价 = 回抽位，不是当根最高价（那是前视偷价）
    assert ev["exit_price"] == pytest.approx(ev["exit_level"])
    # 出场这根是 21:35（反弹那根）
    assert ev["bar_ts"] == T0 + 13 * M5
    # ★ 归因必须能和「预期失效」分开：两者都是 CLOSED_THESIS，
    #   只有动作码不同。混在一起的话统计里永远看不出这条规则赚了多少。
    codes = [a.reason for a in res.actions]
    assert "closed_below_entry" in codes, codes
    assert "thesis_invalidated" not in codes, codes


def test_exit_happens_long_before_the_catastrophic_stop():
    """出场必须发生在灾难止损（57491 附近）之前 —— 这是这条规则存在的全部理由。"""
    res = _run()
    tr = res.transitions[-1]
    px = float(tr.evidence["exit_price"])
    assert px > 57491.4, ("出场价 %.2f 低于灾难止损，说明这条判据没起作用" % px)
    # 实际亏损按 0.01 手算约 0.24U，而不是 9.8U
    loss_pts = LEVEL - px
    assert loss_pts < 30.0, ("出场只该认 %.1f 点，实际 %.1f 点" % (30.0, loss_pts))


def test_no_stop_transition_at_all():
    """整条链上不该出现止损 —— 出现就说明优先级排错了。"""
    res = _run()
    stops = [a for a in res.actions if a.reason == "stop"]
    assert not stops, [(a.reason, a.price) for a in stops]


# ============================================== 二、优先级

def test_stop_wins_when_the_same_bar_sweeps_it():
    """同一根里既砸穿止损又跌破买点 → 按止损（保守，不许让样本变好看）。

    理由：收盘跌破只有等收盘才知道，而止损是盘中就成交的。
    把跌破排到止损前面，等于用收盘才有的信息抢一个先发生的成交。
    """
    rows = list(REAL_5M)
    # 把成交那根的下影线砸到止损下方
    rows[12] = (12, 58831.20, 58884.80, LEVEL - 1000.0, 58415.70)
    res = T.run(_thesis(), _bars15(), _book(), entry_bars=_bars5(rows),
                start_at=START15, fill_bar_ts=BAR_2130, claim_on_entry=True)
    # ★ 注意：`Transition.reason` 是**人话描述**，动作码在 `Action.reason` 里。
    #   按人话去找 "stop" 永远找不到 —— 这个坑第一版就踩了。
    codes = [a.reason for a in res.actions]
    assert "stop" in codes, codes
    assert res.state == T.CLOSED_SL, res.states()


# ============================================== 三、有效跌破（不等回抽）

def test_confirmed_break_exits_at_market_without_waiting():
    """一路跌、没有回抽 → 锚点后第 3 根确认，市价走。

    场景：21:30 收盘跌破之后根本没有反弹（每根都在回抽位下方）。
    这时不能干等 —— 市场已经用 3 根 K 线表态，继续持有就是赌运气。
    出场价取**确认那根的收盘价**（判到它的时候这根已经走完，
    用得上的只有收盘价；不许用更早的高点假装成交）。
    """
    rows = [
        (10, 58460.00, 58470.00, 58450.00, 58460.00),
        (11, 58460.00, 58470.00, 58450.00, 58460.00),   # 挂单那根
        (12, 58831.20, 58884.80, 58204.00, 58415.70),   # 成交 + 收盘跌破
        # ★ 下面三根的最高价必须**全部低于回抽位**（≈58437−0.1×ATR），
        #   否则窗口期内就"回抽到位成交"了，走不到确认那一步。
        #   第一版把第 1 根最高价写成 58420，直接触发回抽离场 —— 判据没错，
        #   是数据没构造出"一路不回头"。
        #   还要**全部高于止损 57533.29**，否则同根里止损先成交（那是对的优先级，
        #   只是这条用例要验的是"确认"这条路）。
        (13, 58415.70, 58380.00, 58000.00, 58100.00),   # 第 1 根：没回抽
        (14, 58100.00, 58150.00, 57900.00, 57950.00),   # 第 2 根
        (15, 57950.00, 58000.00, 57700.00, 57750.00),   # 第 3 根 → 确认
    ]
    # 用 T0 重定位这 6 根，保持 15m 桶对齐
    bars5 = [Bar(T0 + (i - 10 + 10) * M5, o, h, l, c)
             for i, o, h, l, c in rows]
    # REAL_5M 的第 12 根 = T0+12*M5，这批数据第 12 行也就是 T0+12*M5
    start15 = (T0 + 11 * M5 - G15) // M15
    res = T.run(_thesis(), _bars15(), _book(), entry_bars=bars5,
                start_at=start15, fill_bar_ts=T0 + 12 * M5,
                claim_on_entry=True)
    assert res.state == T.CLOSED_THESIS, res.states()
    tr = res.transitions[-1]
    assert tr.reason.startswith("有效跌破"), tr.reason
    assert tr.evidence["break_state"] == "CONFIRMED"
    assert tr.evidence["bars_since"] >= 3
    # 出场价 = 确认那根的收盘（不是更早的高点）
    assert tr.evidence["exit_price"] == pytest.approx(57750.00)


# ============================================== 四、没有锚点 / 没有 5m

def test_no_exit_when_price_never_closes_below():
    """收盘从没跌破 → 不许出场（别把这条判据变成第二个止损）。"""
    rows = [(i, o, h, l, max(c, LEVEL + 5.0))
            for i, o, h, l, c in REAL_5M]
    res = T.run(_thesis(), _bars15(), _book(), entry_bars=_bars5(rows),
                start_at=START15, fill_bar_ts=BAR_2130, claim_on_entry=True)
    reasons = [t.reason for t in res.transitions]
    assert not any("跌破" in r for r in reasons), reasons


def test_no_judgement_without_5m_data():
    """只喂 15m 时不判「5m 收盘跌破」—— 拿粗周期冒充等于换了用户的规则。"""
    res = T.run(_thesis(), _bars15(), _book(), start_at=START15)
    reasons = [t.reason for t in res.transitions]
    assert not any("跌破" in r for r in reasons), reasons


def test_fill_bar_ts_is_carried_across_incremental_steps():
    """增量推进：成交那根在第二段看不到，锚点必须靠 fill_bar_ts 传下去。

    丢掉它的表现是"这条判据什么也不做"——不报错、不告警。
    """
    th = _thesis()
    bars5 = _bars5()
    prog = None
    closed = None
    for k in range(len(bars5)):
        res, prog = T.step(th, _bars15(), _book(), progress=prog,
                           entry_bars=bars5, start_at=START15, claim_on_entry=True,
                           fill_bar_ts=BAR_2130)
        if res.state == T.CLOSED_THESIS:
            closed = res
            break
    assert prog["fill_bar_ts"] == BAR_2130, "锚点没跟着游标走下去"
    assert closed is not None, "逐根推进时这条判据完全没生效"
    assert closed.transitions[-1].reason.startswith("收盘跌破后离场")


def test_anchor_survives_a_cold_step_that_never_saw_the_fill():
    """★ 冷启动：从"已成交"的游标接着推，本次调用**看不到**成交那根。

    这是重启后生产里真实发生的情况 —— 游标落了库，成交那根在上一段。
    锚点只能靠游标里的 `fill_bar_ts` 传进来；传不进来时这条判据就静默失效，
    既不会报错也不会告警，表现是"仓位一直拿着"，和没写这条规则一模一样。
    """
    bars5 = _bars5()
    # 游标：已经成交，停在成交那个 15m 桶（桶里含 21:25 挂单 / 21:30 成交）
    prog = {"state": T.FILLED, "next_index": START15, "fill_bar_ts": BAR_2130}
    res = None
    for _ in range(4):
        res, prog = T.step(_thesis(), _bars15(), _book(), progress=prog,
                           entry_bars=bars5, claim_on_entry=True)
        if res.state == T.CLOSED_THESIS:
            break
    assert res is not None and res.state == T.CLOSED_THESIS, (
        "冷启动之后这条判据完全没生效：state=%s" % (res.state if res else None))
    assert res.transitions[-1].reason.startswith("收盘跌破后离场")
