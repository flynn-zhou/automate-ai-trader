# -*- coding: utf-8 -*-
"""OWN-1：归属优先于接管 —— 我方成交不许被对账抢成人工仓。

生产事故（2026-11-25 21:35）：
    21:31:05 我方 `AI-pend-*` 成交 0.01@58437
    21:35:06 周期对账先跑 → 看到「交易所 +0.01 / 数据库空仓」→ 判人工仓接管
    21:35:07 状态机才发现已成交，但 adopted_pos 已占住这笔量
             → 挂单动作被 suppressed，并把状态强行压回 EXPECTED
    → `_pending_book_fill` 永不执行，这笔仓永久脱离策略管理
    → 最后在 57454.6 被接管仓的盘中止损扫掉，扫完立刻反弹到 58698

这两条测试钉的是不变量，不是实现细节：
    ① 落账前先问交易所「我方的单成交了吗」，成交了就落成策略仓；
    ② 同一份量不许同时存在于 self.pos 和 adopted_pos（归属纠正）。
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trading import engine as E              # noqa: E402
from ai_trader.trading import pending_thesis_store as _PTS  # noqa: E402


class _Stub(object):
    """最小引擎替身：只保留 _attribute_own_fills 用到的部分。"""

    def __init__(self, rows, fills, pos=None, adopted=None):
        self.pending = types.SimpleNamespace(live=lambda limit=200: rows)
        self.pos = pos
        self.adopted_pos = adopted
        self.ex = object()      # `pending_own_filled_qty(ex, tid)` 的第一个参数
        self.db = types.SimpleNamespace(log_risk=lambda *a, **k: None)
        self.booked = []
        self._fills = fills
        self._stub_fills = fills

    # ★ 用 staticmethod 包一层：`E.DecisionEngine._norm_side` 若是 staticmethod，
    #   直接赋给类属性会变成普通函数、自动多收一个 self，参数错位。
    _norm_side = staticmethod(E.DecisionEngine._norm_side)
    _thesis_in_pos = E.DecisionEngine._thesis_in_pos
    _claim_back_from_adopted = E.DecisionEngine._claim_back_from_adopted
    _attribute_own_fills = E.DecisionEngine._attribute_own_fills

    async def _pending_book_fill(self, th):
        self.booked.append(getattr(th, "thesis_id", ""))
        # 真实实现会设 self.pos；这里只记调用，pos 由用例自己摆


def _row(tid, state="ARMED"):
    return {"thesis_id": tid, "state": state}


@pytest.mark.asyncio
async def test_own_fill_is_booked_before_adoption(monkeypatch):
    """我方单已成交 → 先按策略仓落账，不许走接管。"""
    async def fake_qty(ex, tid):
        return 0.01 if tid == "T1" else None

    monkeypatch.setattr(E, "pending_own_filled_qty", fake_qty)
    monkeypatch.setattr(_PTS, "thesis_of",
                        lambda row: types.SimpleNamespace(
                            thesis_id=row["thesis_id"], order_price=58437.0))
    eng = _Stub([_row("T1")], {})
    n = await eng._attribute_own_fills()
    assert n == 1, "我方成交没有先落账 —— 对账会把它判成人工仓"
    assert eng.booked == ["T1"]


@pytest.mark.asyncio
async def test_no_fill_means_no_booking(monkeypatch):
    """交易所查不到我方成交 → 什么都不做（人工仓该接管就接管）。"""
    async def fake_qty(ex, tid):
        return None

    monkeypatch.setattr(E, "pending_own_filled_qty", fake_qty)
    eng = _Stub([_row("T1")], {})
    assert await eng._attribute_own_fills() == 0
    assert eng.booked == []


@pytest.mark.asyncio
async def test_quantity_is_claimed_back_from_adopted(monkeypatch):
    """误判成人工仓的量必须扣回 —— 同一份量不许记两次。

    不扣回的后果不是"多点库存"，而是下一轮对账算出
    「交易所比本地账少 0.01」→ 判 position_shrunk_externally → 硬停。
    用新故障掩盖旧故障，比原故障更难查。
    """
    async def fake_qty(ex, tid):
        return 0.01

    monkeypatch.setattr(E, "pending_own_filled_qty", fake_qty)
    monkeypatch.setattr(_PTS, "thesis_of",
                        lambda row: types.SimpleNamespace(
                            thesis_id=row["thesis_id"], order_price=58437.0))
    eng = _Stub(
        [_row("T1")], {},
        pos={"side": "long", "qty": 0.01, "entry": 58437.0},
        adopted={"side": "long", "qty": 0.01, "entry": 58437.0,
                 "stop": 57491.38})
    await eng._attribute_own_fills()
    assert eng.adopted_pos is None, (
        "人工仓里那 0.01 其实是我方成交，没有扣回 → 本地账会是交易所的 2 倍")

    # 部分扣回：人工仓比我方成交多，只扣我那一份
    eng2 = _Stub(
        [_row("T1")], {},
        pos={"side": "long", "qty": 0.01, "entry": 58437.0},
        adopted={"side": "long", "qty": 0.03, "entry": 59000.0,
                 "stop": 58000.0})
    await eng2._attribute_own_fills()
    assert eng2.adopted_pos["qty"] == pytest.approx(0.02), (
        "只该扣我方那 0.01，人工仓剩下的 0.02 必须留住")


@pytest.mark.asyncio
async def test_reverse_side_is_not_claimed(monkeypatch):
    """方向相反不扣：那是两笔不同的仓，不是归属问题。"""
    async def fake_qty(ex, tid):
        return 0.01

    monkeypatch.setattr(E, "pending_own_filled_qty", fake_qty)
    monkeypatch.setattr(_PTS, "thesis_of",
                        lambda row: types.SimpleNamespace(
                            thesis_id=row["thesis_id"], order_price=58437.0))
    eng = _Stub(
        [_row("T1")], {},
        pos={"side": "long", "qty": 0.01, "entry": 58437.0},
        adopted={"side": "short", "qty": 0.01, "entry": 59000.0, "stop": 60000.0})
    await eng._attribute_own_fills()
    assert eng.adopted_pos is not None, "反向仓被误扣了"
    assert eng.adopted_pos["qty"] == pytest.approx(0.01)


def test_suppressed_branch_consults_the_exchange():
    """压制分支必须先问交易所，而不是无条件压回 EXPECTED。

    这条是源码级断言：`suppressed` 一旦无条件把状态压回 EXPECTED，
    `_pending_book_fill`（挂在 `newp["state"] == FILLED` 上）就永不触发。
    """
    import io
    src = io.open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ai_trader", "trading", "engine.py"),
        encoding="utf-8").read()
    i = src.index("elif suppressed:")
    block = src[i:i + 2600]
    assert "pending_own_filled_qty" in block, (
        "suppressed 分支没有问交易所「我方单成交了吗」——"
        "已成交的仓会被压回 EXPECTED，永久脱离策略管理")
    assert "_claim_back_from_adopted" in block, (
        "确认是我方成交后没有把量从人工仓扣回来 → 同一份量记两次")
