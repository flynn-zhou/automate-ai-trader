# -*- coding: utf-8 -*-
"""周期换算的守卫：`slots_per_day` / `ticks_per_bar` 必须按周期推，不许写死。

## 为什么单独一个文件

2026-11-09 建 5m 时抓到的 bug：

    facts.py:282  slots = int(slots_per_day) if slots_per_day else SLOTS_PER_DAY
    build.py:468  F.bar_facts(bars, interval_ms, ...)     # ← 没传 slots_per_day

`SLOTS_PER_DAY = 96` 是 `24*60/15`，**只有 15m 对**。所以这段代码在 15m 上
完全正确、在**任何别的周期上静默算错**：5m 时 `(ts // 300000) % 96` 只覆盖
8 小时，三个不同时段被当成同一个「同时段」，`vol_slot_rel` 变成垃圾。

它不报错、不崩溃、测试全绿 —— 因为当时只有 15m 一种数据。
而 `vol_slot_rel` 恰好是留出集里最强的维度之一（z −5.31 / +5.26），
所以「静默算错」的代价不是少一个字段，是整个结论歪掉。

这里钉死两件事：
  1. 换算函数必须给出**算术事实**（能整除一天的才认），且 15m 与旧常量一致
  2. 96 在 5m 上确实会把时段撞在一起（把 bug 的后果本身写成测试）
"""
from __future__ import annotations

import pytest

from ai_trader.kbar import facts as F
from ai_trader.kbar.build import facts_params_snapshot

MS_PER_DAY = 86_400_000

# (周期, 一天几根, 一根几根 3s)
KNOWN = [(300_000, 288, 100),      # 5m
         (900_000, 96, 300),       # 15m ← 旧常量就是这一组
         (1_800_000, 48, 600),     # 30m
         (3_600_000, 24, 1200),    # 1h
         (14_400_000, 6, 4800)]    # 4h


# --------------------------------------------------------------- 换算函数本身
@pytest.mark.parametrize("iv,slots,ticks", KNOWN)
def test_slots_and_ticks_are_arithmetic_facts(iv, slots, ticks):
    """一天的槽数 × 周期必须正好是一天；一根的 3s 桶数同理。"""
    assert F.slots_per_day_for(iv) == slots
    assert F.slots_per_day_for(iv) * iv == MS_PER_DAY
    assert F.ticks_per_bar_for(iv) == ticks
    assert F.ticks_per_bar_for(iv) * F.TICK_INTERVAL_MS == iv


def test_15m_derivation_reproduces_the_old_hardcoded_constants():
    """15m 是老库里已经落好的那一套。推导值必须与旧常量**逐位相等**，
    否则等于在没有任何人要求的情况下改写了 17,654 根的历史口径。"""
    assert F.slots_per_day_for(900_000) == F.SLOTS_PER_DAY == 96
    assert F.ticks_per_bar_for(900_000) == F.TICKS_PER_BAR == 300


def test_5m_needs_288_not_96():
    """这条是 bug 的正身：5m 一天 288 根，不是 96。"""
    assert F.slots_per_day_for(300_000) == 288
    assert F.slots_per_day_for(300_000) != F.SLOTS_PER_DAY


@pytest.mark.parametrize("bad", [0, -1, -300_000])
def test_non_positive_interval_is_rejected(bad):
    with pytest.raises(ValueError):
        F.slots_per_day_for(bad)
    with pytest.raises(ValueError):
        F.ticks_per_bar_for(bad)


def test_interval_that_does_not_divide_a_day_is_rejected():
    """420000 ms（7 分钟）不能整除一天 → 没有稳定的「同时段」概念。

    取整过去会得到一个悄悄漂移的槽号，比报错更糟。
    """
    with pytest.raises(ValueError) as ei:
        F.slots_per_day_for(420_000)
    assert "不能整除一天" in str(ei.value)


# --------------------------------------------------------------- bug 的后果
def test_96_slots_on_5m_really_does_collide_three_whole_periods():
    """把「96 用在 5m 上」这个错误**本身**写成测试。

    一天有 288 根 5m。用 288 个槽 → 288 个不同槽（每个时段自己一格）；
    用 96 个槽 → 只剩 96 个，**每 3 个真实时段被压成一个**。
    这就是 vol_slot_rel 静默变垃圾的机制，不是猜测。
    """
    day0 = 1_800_000_000_000 // MS_PER_DAY * MS_PER_DAY   # 对齐到某天 00:00
    tss = [day0 + i * 300_000 for i in range(288)]

    right = {F.slot_of(ts, 300_000, 288) for ts in tss}
    wrong = {F.slot_of(ts, 300_000, 96) for ts in tss}

    assert len(right) == 288, "288 个槽应该一个时段一格"
    assert len(wrong) == 96, "96 个槽把 288 个时段压成 96 格"
    # 压缩倍数正好是 3：288/96。16 点到 21 点被当成同一格的那类错配就是这么来的。
    assert len(right) // len(wrong) == 3

    # 具体盯一个例子：相隔 8 小时的三个根，在 96 槽下会撞在一起
    a = day0 + 0 * 300_000
    b = day0 + 96 * 300_000     # +8h
    c = day0 + 192 * 300_000    # +16h
    assert F.slot_of(a, 300_000, 96) == F.slot_of(b, 300_000, 96) \
        == F.slot_of(c, 300_000, 96), "96 槽下 00:00/08:00/16:00 必须撞成同一格"
    assert len({F.slot_of(a, 300_000, 288), F.slot_of(b, 300_000, 288),
                F.slot_of(c, 300_000, 288)}) == 3, \
        "288 槽下这三根必须各归各格"


# --------------------------------------------------------------- 调用点接线
def _bars(n, iv):
    return [{"ts": i * iv, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5,
             "v": 10.0, "buy_vol": 5.0} for i in range(n)]


def test_bar_facts_refuses_a_slot_count_that_contradicts_the_interval():
    """不许手工传一个与周期不符的 slots_per_day。

    这个参数存在的意义只是让调用方显式化，不是让它改口径 ——
    改了 vol_slot_rel 就与不可比的历史不同口径了。
    """
    with pytest.raises(ValueError) as ei:
        F.bar_facts(_bars(3, 300_000), 300_000, slots_per_day=96)
    assert "不符" in str(ei.value)


def test_bar_facts_accepts_the_matching_slot_count():
    """传对的不该报错（否则上面那条测试可能只是「永远报错」）。"""
    rows, _stats = F.bar_facts(_bars(3, 300_000), 300_000, slots_per_day=288)
    assert len(rows) == 3


@pytest.mark.parametrize("iv", [300_000, 900_000, 1_800_000])
def test_bar_facts_defaults_to_the_interval_slot_count(iv):
    """**不传 slots_per_day 也必须推对** —— 这是生产实际走的路径。

    这条特别关键：之前所有测试都显式传了 slots_per_day，于是
    「default 退回 SLOTS_PER_DAY=96」这个变异体存活了下来 ——
    而它正是原始 bug 的那一行（`build.py` 当初就是没传）。

    5m 下退回 96 会让槽数与周期不符，这里必须正常返回而不是报错或静默算错。
    """
    rows, _stats = F.bar_facts(_bars(3, iv), iv)
    assert len(rows) == 3, "不传 slots_per_day 时 %d ms 应该能正常算" % iv


@pytest.mark.parametrize("iv", [300_000, 900_000, 1_800_000])
def test_default_and_explicit_slot_count_produce_identical_rows(iv):
    """「不传」与「传对」必须逐字段相同 —— 否则两条路径有两套口径。"""
    a, _ = F.bar_facts(_bars(6, iv), iv)
    b, _ = F.bar_facts(_bars(6, iv), iv,
                       slots_per_day=F.slots_per_day_for(iv))
    assert a == b


@pytest.mark.parametrize("iv,slots,ticks", KNOWN)
def test_meta_snapshot_records_the_derived_values_not_a_constant(iv, slots,
                                                                ticks):
    """写进 kbar_meta 的必须是这个周期自己的值。

    漏了这一步，库就会**自己解释错自己**：表里明明是 5m 的数据，
    meta 里却写着 slots_per_day=96，后来的人照着 meta 复算必然对不上。
    """
    snap = facts_params_snapshot(iv)
    assert snap["slots_per_day"] == slots
    assert snap["ticks_per_bar"] == ticks
    assert snap["interval_ms"] == iv
