# -*- coding: utf-8 -*-
"""S5 现实跟随：区间演化的 R 落在「1R ↔ 止盈」之间时把止盈收紧到 R。

用户口径（2026-11-19）：「止盈不是固定的，可以先定一个很远的止盈，但是我想要
的是**现实跟随**，根据我们的区间演化 + K 线演化，判断风向」。

K 线演化那一半已经落地（保本棘轮 / 恐慌盘 / 停滞闸）。这一半接区间演化的
**R（反向边界）**。

为什么只接 R、不接 P 和 K —— 量出来的（§26.12，1271 条真实计划）：

    腿             误触发(落进止损)   可当锚(1R..4R)
    R  反转单      2.0% ~ 3.2%        17.7% ~ 22.8%
    P  反转单      9.2%               7.7%
    K  反转单      0.7%               1.5%

P / K 在反转单上几乎失效：它们的折叠方向跟着**区间方向**走，而反转单逆着
区间做，所以 P 天然落在反侧。这是设计，不是 bug，但意味着它们不能当反转单的锚。

★ 这条规则是**只收紧、不放宽**的。方向刻意选过：放宽出事是「晚走」，
收紧出事是「早走」。在没验证过的初值上，永远选「少赚」那一侧。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T

TS = 1794000000000
HOUR = 3_600_000


def _book(points, ready=None):
    """points: [(key, price)]。

    ``ready`` 是这条线的就绪时刻，默认 ``TS``（声明之前就已就绪 —— 真实情形：
    声明用的是**上一根**已收盘的 1h）。传 ``TS + HOUR`` 可以构造
    「声明时还看不到」的情形。
    """
    b = T.LineBook()
    r = TS if ready is None else int(ready)
    for key, price in points:
        b.add((key, T.LinePoint(price=float(price), src_ts=TS, ready_ms=r)))
    return b


def _plan(direction=T.UP, order=100.0, stop=99.0, target=104.0, **kw):
    """默认：多单，入场 100、止损 99（R=1）、止盈 104（4R）。"""
    return P.Plan(kind="test", direction=direction, order_type="LIMIT",
                  bar_index=0, bar_ts=TS, order_price=order,
                  stop_price=stop, target_price=target, **kw)


# ---------------------------------------------------------------- 纯函数

def test_long_r_inside_the_band_tightens_the_target():
    """多单：R 在 +2R，落在 1R..4R 之间 → 止盈收紧到 R。"""
    tgt, rm = P.tighten_target_to_range(100.0, 99.0, 104.0, 102.0, T.UP)
    assert tgt == 102.0
    assert rm == pytest.approx(2.0)


def test_short_r_inside_the_band_tightens_the_target():
    """空单镜像：R 在入场上方（空单的反向侧），2R 处。"""
    tgt, rm = P.tighten_target_to_range(100.0, 101.0, 96.0, 98.0, T.DOWN)
    assert tgt == 98.0
    assert rm == pytest.approx(2.0)


def test_r_beyond_the_target_never_loosens_or_tightens():
    """R 在止盈之外（够不着）→ 维持原止盈。

    实测 40% 的 R 落在这里。它们是**无害的空转**，
    但一旦写成「R 更远就用 R」，就变成了放宽 —— 那是晚走的方向。
    """
    tgt, rm = P.tighten_target_to_range(100.0, 99.0, 104.0, 110.0, T.UP)
    assert tgt == 104.0
    assert rm == pytest.approx(10.0)


def test_r_inside_the_stop_distance_never_tightens():
    """★★ 最重要的一条：R 落在 0..1R 时**不许**收紧。

    实测 2~4% 的 R 落在「入场价 ↔ 止损价」之间。接上去会让挂单成交的
    同一根 K 立刻触发止盈，等于把「提前止损」伪装成「止盈」，
    归因 / 统计 / 看板全部被污染。
    """
    tgt, rm = P.tighten_target_to_range(100.0, 99.0, 104.0, 100.4, T.UP)
    assert tgt == 104.0, "R 在 0.4R 就收紧止盈 = 成交即止盈"
    assert rm == pytest.approx(0.4)


def test_r_exactly_at_one_r_tightens():
    """边界含 1R：刚刚走完一个止损距离就算确认。"""
    tgt, _ = P.tighten_target_to_range(100.0, 99.0, 104.0, 101.0, T.UP)
    assert tgt == 101.0


def test_r_exactly_at_the_target_does_not_tighten():
    """边界不含止盈：R 和止盈同价时「收紧」是空操作，别去改它。"""
    tgt, rm = P.tighten_target_to_range(100.0, 99.0, 104.0, 104.0, T.UP)
    assert tgt == 104.0
    assert rm == pytest.approx(4.0)


def test_missing_r_keeps_the_original_target():
    """取不到线（旧库 / 该方向没有 hard 线）→ 维持原止盈，不许崩。"""
    tgt, rm = P.tighten_target_to_range(100.0, 99.0, 104.0, None, T.UP)
    assert tgt == 104.0 and rm is None


def test_zero_stop_distance_is_not_a_division_by_zero():
    tgt, rm = P.tighten_target_to_range(100.0, 100.0, 104.0, 102.0, T.UP)
    assert tgt == 104.0 and rm is None


def test_the_ceiling_comes_from_the_plans_own_target_not_a_hardcoded_4r():
    """上限必须用计划自己的止盈，不能硬编码 4R。

    止盈是公式算出来的（`4 × d_max`），实测目标/R 中位 4.000 但最小 0.926。
    硬编码 4 会让「公式变了之后」这条规则静默失效，而且不会报错。
    """
    # 止盈只有 1.5R 的计划：R 在 3R 处 → 已经在止盈之外，不动
    tgt, _ = P.tighten_target_to_range(100.0, 99.0, 101.5, 103.0, T.UP)
    assert tgt == 101.5
    # 同一个 R，在 2R 处 → 仍然在 1.5R 止盈之外，不动
    tgt, _ = P.tighten_target_to_range(100.0, 99.0, 101.5, 102.0, T.UP)
    assert tgt == 101.5
    # R 在 1.2R 处 → 才落在 1R..1.5R 之间，收紧
    tgt, _ = P.tighten_target_to_range(100.0, 99.0, 101.5, 101.2, T.UP)
    assert tgt == 101.2


# ---------------------------------------------------------------- 取线

def test_claim_close_ms_respects_each_bar_space():
    """5m 声明 :55 收盘 = 整点；15m 声明 :45 收盘 = 整点。两者不能混。"""
    hour = 1_800_000_000_000
    p5 = P.dataclasses.replace(_plan(), bar_space="5m",
                               bar_ts=hour + 55 * 60_000,
                               claim_ts=hour + 55 * 60_000)
    p15 = P.dataclasses.replace(_plan(), bar_space="15m",
                                bar_ts=hour + 45 * 60_000,
                                claim_ts=hour + 45 * 60_000)
    assert P.claim_close_ms(p5) == hour + 3_600_000
    assert P.claim_close_ms(p15) == hour + 3_600_000
    # 同一时刻起点，周期不同 → 收盘时刻必须不同
    assert P.claim_close_ms(P.dataclasses.replace(
        p5, bar_ts=hour + 30 * 60_000, claim_ts=hour + 30 * 60_000)) \
        == hour + 35 * 60_000
    assert P.claim_close_ms(P.dataclasses.replace(
        p15, bar_ts=hour + 30 * 60_000, claim_ts=hour + 30 * 60_000)) \
        == hour + 45 * 60_000


def test_apply_range_target_only_sees_the_line_visible_at_claim_close():
    """★★ 走**真实路径**（apply_range_target），不许读到声明之后才就绪的线。

    构造：声明那根 5m 在 :20，收盘 :25（**不落在整点**，所以那根 1h 还没收盘）。
      - 声明时刻可见的 R = 130（在计划止盈 120 之外 → 不该收紧）
      - 整点 :60 才就绪的 R = 112（在 1R~4R 之间 → 会收紧到 112）

    偷未来的实现（`ts + 1h` = 1:20）会拿到 112，把止盈从 120 改成 112 ——
    回测凭空变好，实盘那一刻根本没有这条线。
    """
    hour = 1_800_000_000_000
    claim = hour + 20 * 60_000          # :20 声明，:25 收盘
    plan = P.dataclasses.replace(_plan(), bar_space="5m", bar_ts=claim,
                                 claim_ts=claim, order_price=100.0,
                                 stop_price=95.0, target_price=120.0)
    book = T.LineBook()
    book.add(("range_hard_dn@1h", T.LinePoint(price=130.0, src_ts=hour - 3_600_000,
                                             ready_ms=hour)))
    book.add(("range_hard_dn@1h", T.LinePoint(price=112.0, src_ts=hour,
                                             ready_ms=hour + 3_600_000)))
    out = P.apply_range_target(plan, book)
    assert out.target_price == 120.0, (
        u"止盈被声明之后才收盘的线收紧了（%.2f）：这是偷未来"
        % out.target_price)
    assert "range_r_price" not in (out.evidence or {})


def test_apply_range_target_does_tighten_when_the_line_is_visible():
    """反向对照：可见的线确实参与收紧，证明上一个测试不是因为功能失效。"""
    hour = 1_800_000_000_000
    claim = hour + 20 * 60_000
    plan = P.dataclasses.replace(_plan(), bar_space="5m", bar_ts=claim,
                                 claim_ts=claim, order_price=100.0,
                                 stop_price=95.0, target_price=120.0)
    book = T.LineBook()
    book.add(("range_hard_dn@1h", T.LinePoint(price=112.0,
                                             src_ts=hour - 3_600_000,
                                             ready_ms=hour)))
    out = P.apply_range_target(plan, book)
    assert out.target_price == 112.0
    assert out.evidence["range_r_mult"] == pytest.approx(2.4, abs=1e-4)


def test_r_key_follows_the_direction():
    """★ 取错方向不会报错，只会拿到另一侧的线 —— 必须钉死。"""
    book = _book([("range_hard_up@1h", 110.0),
                  ("range_hard_dn@1h", 90.0)])
    ms = P.claim_close_ms(_plan())
    assert P.range_r_price(book, ms, T.UP) == 90.0     # 多单的 R 在下方
    assert P.range_r_price(book, ms, T.DOWN) == 110.0  # 空单的 R 在上方


def test_r_is_read_at_the_claim_bar_close_not_before():
    """必须按「声明那根 K 已收盘」取 —— 否则就是偷未来。

    构造一条**恰好在声明 K 收盘那一刻**才就绪的线：
    在 TS（声明 K 开盘）取不到，在声明 K 收盘取得到。
    """
    p = _plan()
    ready = P.claim_close_ms(p)             # 15m 声明 → TS + 15 分钟就绪
    book = _book([("range_hard_dn@1h", 90.0)], ready=ready)
    assert book.at("range_hard_dn@1h", TS) is None, (
        u"前提：这条线在 TS（声明那根 K 开盘）还没就绪")
    assert P.range_r_price(book, P.claim_close_ms(p), T.UP) == 90.0


def test_5m_claim_never_reads_the_unfinished_hour():
    """★★ 5m 声明在 10:35 时，**不许**读到 10:00 那根 1h（它 11:00 才收盘）。

    这是 2026-11-19 修掉的真缺陷：原实现一律 `ts + 1h`，
    实测 392/1250 条计划（31.4%）读到了声明之后才就绪的线，最多提前 55 分钟。
    """
    hour = 1_800_000_000_000                  # 某个整点
    claim = hour + 35 * 60_000                # 10:35 那根 5m
    p = _plan()
    p = P.dataclasses.replace(p, bar_space="5m", bar_ts=claim, claim_ts=claim,
                              claim_on_entry=True)
    assert P.claim_close_ms(p) == claim + 300_000        # 10:40 收盘

    book = T.LineBook()
    # 09:00 那根 1h：10:00 就绪 —— 声明时唯一可见的
    book.add(("range_hard_dn@1h", T.LinePoint(price=100.0, src_ts=hour - 3_600_000,
                                             ready_ms=hour)))
    # 10:00 那根 1h：11:00 才就绪 —— 声明时**还看不到**
    book.add(("range_hard_dn@1h", T.LinePoint(price=999.0, src_ts=hour,
                                             ready_ms=hour + 3_600_000)))

    got = P.range_r_price(book, P.claim_close_ms(p), T.UP)
    assert got == 100.0, (
        u"5m 声明读到了尚未收盘的那根 1h（%.2f）：这是偷未来，"
        u"回测会拿到实盘此刻不可能有的边界" % (got,))


def test_15m_claim_at_hour_end_may_use_the_just_closed_hour():
    """边界：15m 声明在 10:45（11:00 收盘），10:00 那根 1h 也是 11:00 收盘。

    同一时刻收盘，不构成偷未来 —— 允许使用（与 lines.py 的
    `line_ready_ms <= bar_close_ms` 口径一致）。
    """
    hour = 1_800_000_000_000
    claim = hour + 45 * 60_000
    p = P.dataclasses.replace(_plan(), bar_space="15m", bar_ts=claim,
                              claim_ts=claim)
    assert P.claim_close_ms(p) == hour + 3_600_000
    book = T.LineBook()
    book.add(("range_hard_dn@1h", T.LinePoint(price=42.0, src_ts=hour,
                                             ready_ms=hour + 3_600_000)))
    assert P.range_r_price(book, P.claim_close_ms(p), T.UP) == 42.0


def test_unknown_direction_returns_none():
    book = _book([("range_hard_dn@1h", 90.0)])
    assert P.range_r_price(book, TS, "SIDEWAYS") is None


def test_unknown_direction_never_changes_the_target():
    """★★ 未知方向必须原地不动 —— 不许落进空单分支。

    这条是部署验收时现抓的：壳里手写了 "UP"，而 `T.UP == "up"`，
    于是多单被当成空单算，得到一个看起来很合理的「-2.0R」，
    止盈被静默改错。这类错误不报错、数字也不离谱，最难发现。
    """
    tgt, rm = P.tighten_target_to_range(100.0, 99.0, 104.0, 102.0, "UP")
    assert tgt == 104.0, "未知方向把止盈改了"
    assert rm is None, "未知方向不该给出 R 倍数"


def test_direction_case_matters_and_constants_are_lowercase():
    """钉住方向常量的实际取值，避免下一个人再手写大写。"""
    assert T.UP == "up" and T.DOWN == "down"
    tight = P.tighten_target_to_range(100.0, 99.0, 104.0, 102.0, T.UP)
    assert tight[0] == 102.0


# ---------------------------------------------------------------- 接计划

def test_apply_range_target_records_the_evidence():
    """收紧时要把「原 4R 止盈 / R 价位 / R 倍数」都记下来，否则事后无法归因。"""
    book = _book([("range_hard_dn@1h", 102.0)])
    plan = P.apply_range_target(_plan(), book)
    assert plan.target_price == 102.0
    assert plan.evidence["target_4r"] == 104.0
    assert plan.evidence["range_r_price"] == 102.0
    assert plan.evidence["range_r_mult"] == pytest.approx(2.0)


def test_apply_range_target_leaves_the_plan_alone_when_nothing_changes():
    """没收紧时必须返回**同一个对象**：否则每个计划都多一份 evidence，
    计划去重（按内容比）会失效，同一根 K 会被当成两条计划。"""
    book = _book([("range_hard_dn@1h", 110.0)])
    plan = _plan()
    assert P.apply_range_target(plan, book) is plan


def test_apply_range_target_without_a_book_is_a_noop():
    plan = _plan()
    assert P.apply_range_target(plan, None) is plan


def test_the_thesis_gets_the_tightened_target():
    """最终生效的必须是收紧后的止盈 —— 否则前面全白做。"""
    book = _book([("range_hard_dn@1h", 102.0)])
    plan = P.apply_range_target(_plan(), book)
    th = P.to_thesis(plan)
    assert th.target_price == 102.0
    assert th.stop_price == 99.0
    assert th.order_price == 100.0
