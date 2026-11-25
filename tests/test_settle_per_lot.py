# -*- coding: utf-8 -*-
"""BOOK-1：分笔结算 —— 一行 trade 只能代表一次成交。

生产事故（2026-11-25，trades id=33）：
    qty=0.02  entry_price=58437  exit_price=57454.60  pnl_gross=-12.048

真实成交是两笔：0.01@58437（我方挂单）+ 0.01@57677（用户手工加仓），
均价 58057。(-12.048) = (57454.60 - 58057) × 0.02 —— **盈亏算对了、
entry_price 记错了**，同一行内部自相矛盾：
    用 (exit - entry) * qty 复算 = (57454.60 - 58437) * 0.02 = -19.65 ≠ -12.048

根因是记账粒度：一行代表整笔仓，而整笔仓由多笔不同价的成交拼成。

这里钉的不变量（对**每一行**都成立）：
    |pnl_gross - (exit_price - entry_price) * qty * dir| < 1e-6
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trading import engine as E              # noqa: E402


class _DB(object):
    def __init__(self):
        self.trades = []
        self.closes = []

    def log_trade(self, t):
        self.trades.append(t)

    def close_trade(self, t):
        self.closes.append(t)

    def log_risk(self, *a, **k):
        pass


class _Stub(object):
    def __init__(self):
        self.db = _DB()
        self.tcfg = types.SimpleNamespace(symbol="BTCUSDT", strategy="pending")

    _lots_of = staticmethod(E.DecisionEngine._lots_of)
    _push_lot = staticmethod(E.DecisionEngine._push_lot)
    _settle_close = E.DecisionEngine._settle_close


def _invariant(rows, side="long"):
    """每一行都必须自洽。这条不变量就是这次事故的全部内容。"""
    d = 1 if side == "long" else -1
    for r in rows:
        want = (float(r["exit_price"]) - float(r["entry_price"])) * float(r["qty"]) * d
        assert abs(want - float(r["pnl_gross"])) < 1e-6, (
            "这一行自相矛盾：qty=%s entry=%s exit=%s，"
            "(exit-entry)*qty*dir=%.6f 但 pnl_gross=%.6f"
            % (r["qty"], r["entry_price"], r["exit_price"], want, r["pnl_gross"]))


def test_replay_of_the_incident():
    """完整复现 2026-11-25 那笔仓：两腿不同价，平仓 0.02 @ 57454.60。

    旧实现会写成一行 qty=0.02 / entry=58437 / pnl=-12.048（复算 -19.65）。
    新实现必须写成两行，各自自洽，且**合计**与整体结算一致。
    """
    st = _Stub()
    pos = {"side": "long", "qty": 0.01, "entry": 58437.0,
           "entry_ts": 1795613000000, "fee": 0.29,
           "lots": [{"qty": 0.01, "entry": 58437.0,
                     "ts": 1795613000000, "fee": 0.29}]}
    # 用户 22:33 在 57677 加仓 0.01
    st._push_lot(pos, 0.01, 57677.0, 1795626600000, fee=0.29)
    assert pos["qty"] == pytest.approx(0.02)
    assert pos["entry"] == pytest.approx((58437.0 + 57677.0) / 2)  # 58057
    assert len(pos["lots"]) == 2

    g, f, n = st._settle_close(
        pos, 0.02, 57454.60, 0.5745, "接管仓出场", exit_ts=1795627400000,
        symbol="BTCUSDT", strategy="pending")

    rows = st.db.closes
    assert len(rows) == 2, "两个 lot 必须写成两行，否则 entry_price 只能记其中一腿的价"
    _invariant(rows)
    entries = sorted(float(r["entry_price"]) for r in rows)
    assert entries == [57677.0, 58437.0], "每一行必须记自己那笔成交的价"
    # 合计口径不许变：和 close_booking 一样的 (exit - 均价) * 总量
    assert g == pytest.approx((57454.60 - 58057.0) * 0.02)
    assert abs(g - (-12.048)) < 0.01
    # 这两腿的盈亏差别很大 —— 这正是"加仓到底对不对"唯一的证据
    pnl_by_entry = {float(r["entry_price"]): float(r["pnl_gross"]) for r in rows}
    assert pnl_by_entry[58437.0] == pytest.approx((57454.60 - 58437.0) * 0.01)
    assert pnl_by_entry[57677.0] == pytest.approx((57454.60 - 57677.0) * 0.01)


def test_short_side_invariant():
    """空头方向也要成立（乘 dir）。"""
    st = _Stub()
    pos = {"side": "short", "qty": 0.01, "entry": 62000.0,
           "entry_ts": 1, "fee": 0.0,
           "lots": [{"qty": 0.01, "entry": 62000.0, "ts": 1, "fee": 0.0}]}
    st._push_lot(pos, 0.01, 63000.0, 2, fee=0.0)
    st._settle_close(pos, 0.02, 61000.0, 0.0, "出场", exit_ts=3)
    rows = st.db.closes
    assert len(rows) == 2
    _invariant(rows, side="short")
    assert sum(float(r["pnl_gross"]) for r in rows) == pytest.approx(
        (62000.0 - 61000.0) * 0.01 + (63000.0 - 61000.0) * 0.01)


def test_partial_close_consumes_fifo_and_keeps_the_rest():
    """部分平仓：FIFO 吃掉最早的 lot，剩下的留在槽里继续持有。

    以前这里把 sold 全压在一行上，剩下的仓数量对但成本是"第一腿的价"。
    """
    st = _Stub()
    pos = {"side": "long", "qty": 0.01, "entry": 58000.0, "entry_ts": 1,
           "fee": 0.1,
           "lots": [{"qty": 0.01, "entry": 58000.0, "ts": 1, "fee": 0.1}]}
    st._push_lot(pos, 0.02, 60000.0, 2, fee=0.2)
    st._settle_close(pos, 0.01, 61000.0, 0.0, "减仓", exit_ts=3)
    assert len(st.db.closes) == 1
    _invariant(st.db.closes)
    assert float(st.db.closes[0]["entry_price"]) == 58000.0, "FIFO 应该先吃最早那腿"
    assert pos["qty"] == pytest.approx(0.02), "剩下的 0.02 必须还在槽里"
    assert len(pos["lots"]) == 1
    assert float(pos["lots"][0]["entry"]) == 60000.0
    assert pos["fee"] == pytest.approx(0.2), "剩下的仓要带走它自己的开仓费"


def test_fee_is_allocated_not_duplicated():
    """开仓费按腿摊、平仓费按消耗量摊，合计必须等于整体口径。"""
    st = _Stub()
    pos = {"side": "long", "qty": 0.01, "entry": 58000.0, "entry_ts": 1,
           "fee": 0.5,
           "lots": [{"qty": 0.01, "entry": 58000.0, "ts": 1, "fee": 0.5}]}
    st._push_lot(pos, 0.03, 60000.0, 2, fee=1.5)
    st._settle_close(pos, 0.04, 61000.0, 2.0, "出场", exit_ts=3)
    fee_sum = sum(float(r["fee"]) for r in st.db.closes)
    assert fee_sum == pytest.approx(0.5 + 1.5 + 2.0), (
        "开仓费(0.5+1.5)+平仓费(2.0) 必须逐腿摊完，不能重复也不能漏")
    _invariant(st.db.closes)


def test_legacy_slot_without_lots_still_settles():
    """老槽（重启恢复、或旧代码留下的）没有 lots 也要能平 —— 退化成单腿。"""
    st = _Stub()
    pos = {"side": "long", "qty": 0.02, "entry": 58500.0, "entry_ts": 1,
           "fee": 0.3}
    g, f, n = st._settle_close(pos, 0.02, 57500.0, 0.1, "出场", exit_ts=2)
    rows = st.db.closes
    assert len(rows) == 1
    _invariant(rows)
    assert g == pytest.approx((57500.0 - 58500.0) * 0.02)


def test_push_lot_keeps_aggregates_consistent():
    """`_push_lot` 的汇总关系是整条链路的地基。"""
    st = _Stub()
    pos = {"side": "long", "qty": 0.01, "entry": 50000.0, "entry_ts": 1,
           "fee": 0.0}
    st._push_lot(pos, 0.02, 60000.0, 2, fee=0.0)
    st._push_lot(pos, 0.01, 70000.0, 3, fee=0.0)
    assert pos["qty"] == pytest.approx(0.04)
    assert pos["entry"] == pytest.approx(
        (50000.0 * 0.01 + 60000.0 * 0.02 + 70000.0 * 0.01) / 0.04)


def test_unknown_exit_price_closes_every_lot():
    """价未知时也必须把**每一腿**都关掉。

    只关一行 = 第二腿的行永远开着 → 重启后 `_restore_open_positions`
    读到它 → 「库里有仓、交易所没有」的幽灵仓 → 又一次接管。
    这是分笔记账引入的新风险面，必须钉住。

    同时：绝不允许为了凑数编一个平仓价 —— 按各腿自己的入场价占位、盈亏 0。
    """
    st = _Stub()
    pos = {"side": "long", "qty": 0.01, "entry": 58000.0, "entry_ts": 1,
           "fee": 0.0,
           "lots": [{"qty": 0.01, "entry": 58000.0, "ts": 1, "fee": 0.0}]}
    st._push_lot(pos, 0.01, 60000.0, 2, fee=0.0)
    g, f, n = st._settle_close(pos, 0.02, 0.0, 0.0, "交易所已无此仓",
                               exit_ts=3, mfe_mae=False)
    rows = st.db.closes
    assert len(rows) == 2, "两腿都得关，否则剩下的那行永远开着"
    for r in rows:
        assert float(r["pnl_gross"]) == pytest.approx(0.0)
        assert float(r["exit_price"]) == pytest.approx(float(r["entry_price"])), \
            "价未知时只能按该腿自己的入场价占位，不许编一个价"
    assert g == pytest.approx(0.0)
    assert "lots" not in pos, "全平之后槽里不该再留 lot"
