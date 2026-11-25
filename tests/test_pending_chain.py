# -*- coding: utf-8 -*-
"""挂单链路后三环的回归测试：成交落账 / 平仓走对接口 / 来源分类认得出自己。

三条都是 2026-11-20 用真钱自检单抓到的**真缺陷**，不钉住必然复发：

1. 成交不落账 → 5 分钟后被当成人工仓接管，止损换成 25% 兜底、策略出场失效。
2. `_pending_close` 用 `place_order(order_type="MARKET")` → 执行器不支持 → 永远平不掉。
3. 挂单 client_id 不带 `AI-` 前缀 → 分类器把「我们自己的单」判成用户手单。
"""
import asyncio
from types import SimpleNamespace

import pytest

from ai_trader.trading import engine as E
from ai_trader.trading import thesis as T


def _run(coro):
    """跑协程并**把 event loop 还原**。

    ★ 不能用 asyncio.run()：Python 3.9 里它跑完会把当前线程的 event loop
      置成 None，于是后面任何用 `asyncio.get_event_loop()` 的老测试都会炸
      `RuntimeError: There is no current event loop in thread 'MainThread'`。
      实测：单跑这个文件全绿、全量跑就有 30 个无关测试失败 —— 这正是
      那 30 个失败的全部原因（不是产品代码的问题）。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


class FakeDB(object):
    def __init__(self):
        self.trades = []
        self.closes = []
        self.risks = []

    def log_trade(self, t):
        self.trades.append(t)

    def close_trade(self, t):
        self.closes.append(t)

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, detail))


class FakeEx(object):
    def __init__(self, qty=0.005, entry=61689.8, ok=True, price=61700.0,
                 order_filled=True, order_status="FILLED"):
        self.qty, self.entry, self.ok, self.price = qty, entry, ok, price
        self.market_calls = []
        self.place_calls = []
        # ★ 成交归属：`_pending_book_fill` 现在必须向交易所确认
        #   「我这张单真的成交了吗」。默认给一张已成交的单；
        #   置 order_filled=False 就模拟「账户里有仓，但不是我这张单」。
        self.order_filled = order_filled
        self.order_status = order_status
        self.queried = []

    async def query_order(self, order_id="", client_id="", symbol=None):
        """按 client_id 查单 —— 归属证据的唯一来源。"""
        self.queried.append(client_id)
        if not self.order_filled:
            return None
        return SimpleNamespace(status=self.order_status,
                               executed_qty=abs(float(self.qty or 0.0)))

    async def position(self):
        return {"qty": self.qty, "entry": self.entry,
                "side": "long" if self.qty > 0 else "short"}

    async def market_order(self, side, qty, reason="", reduce_only=False):
        if getattr(self, "fail_market", False):
            raise AssertionError("平仓不该走 place_order")
        self.market_calls.append((side, qty, reduce_only))
        return SimpleNamespace(ok=self.ok, price=self.price,
                               reason="" if self.ok else "交易所拒绝")

    async def place_order(self, *a, **kw):
        self.place_calls.append((a, kw))
        return SimpleNamespace(ok=False, status="REJECTED"), "不该用 place_order 平仓"

    async def mark_price(self):
        return self.price


def _engine_stub(ex=None, pos=None):
    st = SimpleNamespace()
    st.ex = ex or FakeEx()
    st.db = FakeDB()
    st.tcfg = SimpleNamespace(symbol="BTCUSDT", strategy="pending", max_hold_bars=600)
    st.pos = pos if pos is not None else {
        "side": "long", "qty": 0.005, "entry": 61689.8, "stop": 60735.71,
        "target": 65682.79, "entry_ts": 1795140000000, "open_reason": "挂单成交",
        "paper": 0}
    return st


def _thesis(direction=T.UP):
    return T.Thesis(thesis_id="selfcheck-1", kind="x", direction=direction,
                    order_price=61689.78, stop_price=60735.71,
                    target_price=65682.79, order_type=T.ORDER_LIMIT,
                    family=T.FAMILY_REVERSAL, surprise_pts=0.0)


# ------------------------------------------------------- 1. 成交落账

def test_book_fill_writes_trade_with_planned_stop_and_target():
    # ★ 必须从**空仓**开始：2026-11-24 拆仓后，成交落账是「加仓」语义，
    #   桩里预置一笔同向仓会让它走合并分支（qty 相加），测到的就不是
    #   「成交把仓位建起来」这件事。
    st = _engine_stub(pos={})
    _run(E.DecisionEngine._pending_book_fill(st, _thesis()))
    assert len(st.db.trades) == 1
    t = st.db.trades[0]
    # 止损/止盈必须来自**计划**，不是兜底值（兜底 25% 就是这次事故的后果）
    assert t["stop_price"] == 60735.71
    assert t["target_price"] == 65682.79
    assert t["qty"] == 0.005
    assert t["side"] == "long"
    assert t["open_reason"] == "挂单成交"
    # 仓位也要设进 self.pos，否则平仓时 qty=0 平不掉
    assert st.pos["qty"] == 0.005
    assert st.pos["stop"] == 60735.71


def test_book_fill_merges_into_system_lot_as_add_on():
    """★ 加仓：用户原话「已存在的单子是系统单子，属于加仓行为，当然要执行」。

    同向成交要**累加进系统仓**、均价按量加权，而不是覆盖整笔仓 ——
    覆盖会把已有那笔的入场价和止损一起换掉。
    """
    st = _engine_stub(pos={
        "side": "long", "qty": 0.004, "entry": 60000.0, "stop": 59000.0,
        "target": 66000.0, "entry_ts": 1795140000000,
        "open_reason": "挂单成交", "paper": 0})
    _run(E.DecisionEngine._pending_book_fill(st, _thesis()))
    assert st.db.trades[0]["qty"] == 0.005, "落账只记我这一单的量"
    assert st.pos["qty"] == pytest.approx(0.009), "同向成交要累加，不许覆盖"
    # (60000*0.004 + 61689.78*0.005) / 0.009
    assert st.pos["entry"] == pytest.approx(
        (60000.0 * 0.004 + 61689.78 * 0.005) / 0.009, rel=1e-6)
    assert st.pos["stop"] == 59000.0, "止损取更保守的（多头取更高那条）"
    assert st.pos["target"] == 66000.0, "止盈取更远的（多头取更高那条）"


def test_book_fill_refuses_to_overwrite_opposite_side_lot():
    """反向成交 = 先平后反手，不是加仓。宁可停手也不许覆盖整笔仓。"""
    st = _engine_stub(pos={
        "side": "long", "qty": 0.004, "entry": 60000.0, "stop": 59000.0,
        "target": 66000.0, "entry_ts": 1795140000000,
        "open_reason": "挂单成交", "paper": 0},
        ex=FakeEx(qty=-0.003, entry=61000.0))
    _run(E.DecisionEngine._pending_book_fill(st, _thesis(T.DOWN)))
    assert st.pos["side"] == "long", "原有系统仓不许被反向成交覆盖"
    assert st.pos["qty"] == 0.004
    assert "pending_fill_opposite_side" in [k for k, _ in st.db.risks]


def test_book_fill_short_side_and_qty_from_exchange():
    st = _engine_stub(ex=FakeEx(qty=-0.003, entry=61000.0), pos={})
    _run(E.DecisionEngine._pending_book_fill(st, _thesis(T.DOWN)))
    assert st.db.trades[0]["side"] == "short"
    assert st.db.trades[0]["qty"] == 0.003      # 取绝对值，方向单独记
    assert st.pos["side"] == "short"


def test_book_fill_without_exchange_position_does_not_guess():
    """查不到持仓量就记风险，绝不编一个量去保护不存在的仓。"""
    st = _engine_stub(ex=FakeEx(qty=0.0), pos={})
    _run(E.DecisionEngine._pending_book_fill(st, _thesis()))
    assert st.db.trades == []
    assert not st.pos
    assert "pending_fill_unbooked" in [k for k, _ in st.db.risks]


def test_book_fill_refuses_when_my_order_never_filled():
    """★ 2026-11-24 生产事故：账户里有仓 ≠ 这笔仓是我开的。

    用户手工买入 0.032 BTC 在对账接管之前，`ex.position()` 一样看得见它。
    以前直接拿它当「挂单成交」落账 —— 于是人工仓被洗成策略仓，
    接着被策略平掉。判据只能问交易所：我这张单成交了吗。
    """
    st = _engine_stub(ex=FakeEx(qty=0.032, entry=60880.0, order_filled=False),
                      pos={})
    _run(E.DecisionEngine._pending_book_fill(st, _thesis()))
    assert st.db.trades == [], "没有成交证据却落了账"
    assert not st.pos, "把不属于这条 thesis 的仓设成了自己的"
    assert "pending_fill_not_ours" in [k for k, _ in st.db.risks]
    # 必须真的去查了单，而不是凭价格猜
    assert st.ex.queried and st.ex.queried[0].startswith(E.PENDING_CLIENT_PREFIX)


def test_book_fill_refuses_when_order_still_resting():
    """状态机按 K 线判成交，但交易所说单还挂着（NEW）→ 不落账。"""
    st = _engine_stub(ex=FakeEx(qty=0.005, order_status="NEW"), pos={})
    _run(E.DecisionEngine._pending_book_fill(st, _thesis()))
    assert st.db.trades == []
    assert "pending_fill_not_ours" in [k for k, _ in st.db.risks]


# ------------------------------------------------------- 2. 平仓接口

def test_close_uses_market_order_not_place_order():
    st = _engine_stub()
    act = SimpleNamespace(thesis_id="selfcheck-1", reason="selfcheck_cancel")
    _run(E.DecisionEngine._pending_close(st, act, None))
    assert st.ex.place_calls == [], "平仓走了 place_order（执行器不接受 MARKET）"
    assert st.ex.market_calls == [("SELL", 0.005, True)]


def test_close_closes_the_ledger_and_clears_pos():
    """平仓成交后必须关账：不关的话下一轮对账会把差额判成 ours → 硬停。"""
    st = _engine_stub()
    act = SimpleNamespace(thesis_id="selfcheck-1", reason="selfcheck_cancel")
    _run(E.DecisionEngine._pending_close(st, act, None))
    assert len(st.db.closes) == 1
    c = st.db.closes[0]
    assert c["side"] == "long"
    assert c["entry_ts"] == 1795140000000   # close_trade 靠 (entry_ts, side) 定位
    assert c["exit_price"] == 61700.0
    assert c["target_price"] == 65682.79    # 关账时不能把计划的止盈丢掉
    assert c["pnl_gross"] > 0               # 61689.8 → 61700，多单小幅盈利
    assert st.pos is None


def test_close_failure_raises_and_keeps_position():
    st = _engine_stub(ex=FakeEx(ok=False))
    act = SimpleNamespace(thesis_id="selfcheck-1", reason="x")
    try:
        _run(E.DecisionEngine._pending_close(st, act, None))
    except RuntimeError:
        pass
    else:
        raise AssertionError("平仓失败必须抛出去，不能静默")
    assert st.db.closes == []
    assert st.pos is not None
    assert "pending_close_failed" in [k for k, _ in st.db.risks]


def test_close_without_position_is_a_noop():
    st = _engine_stub(pos={})
    act = SimpleNamespace(thesis_id="selfcheck-1", reason="x")
    _run(E.DecisionEngine._pending_close(st, act, None))
    assert st.ex.market_calls == []


# ------------------------------------------------------- 3. 来源分类

def test_pending_client_id_is_recognised_as_ours():
    """挂单前缀必须带 `AI-`：否则「我们的单丢了回报」永远判不成 ours（该硬停时不停）。"""
    fills = [{"client_id": "AI-pend-selfcheck-1795139100000", "qty": 0.005,
              "side": "BUY", "ts": 1795140000000}]
    prov, used = E.classify_delta(fills, 0.005)
    assert prov == E.PROV_OURS
    assert used


def test_old_pend_prefix_would_be_misread_as_manual():
    """把这条钉住：`pend-` 前缀会被判成人工仓（本轮的错判方向）。"""
    fills = [{"client_id": "pend-selfcheck-1795139100000", "qty": 0.005,
              "side": "BUY", "ts": 1795140000000}]
    prov, _ = E.classify_delta(fills, 0.005)
    assert prov == E.PROV_MANUAL
    assert E.AI_CLIENT_PREFIX == "AI-"


# ------------------------------------------------------- 4. 撤单后必须落终态

def _row(state=T.FILLED, next_index=42):
    """用**生产的** row_of 造行：手写字段会漏（第一版就漏了 horizon_bars）。"""
    from ai_trader.trading import pending_thesis_store as PTS
    return PTS.row_of(_thesis(), state=state, next_index=next_index,
                      armed_at_bar=3, armed_ref_close=1.0,
                      bar_ts=1795140000000, ambiguous=False, note="")


class _FakePending(object):
    def __init__(self):
        self.written = []

    def upsert_thesis(self, row):
        self.written.append(row)


def test_cancel_marks_the_row_terminal():
    """撤单+平仓都成功，行还停在 FILLED → 下一张自检单被拒，状态机继续发动作。"""
    from ai_trader.trading import selfcheck as SC

    st = SimpleNamespace(pending=_FakePending(), db=FakeDB())
    SC._mark_cancelled(st, _row(), True)
    w = st.pending.written
    assert len(w) == 1
    assert w[0]["state"] == T.CANCELLED
    assert w[0]["next_index"] == 42          # 游标不能被重置
    assert w[0]["state"] in T.TERMINAL_STATES


def test_cancel_without_close_still_leaves_terminal_state():
    """平仓没成功也不能留 FILLED —— 那会让自检单永久卡住。"""
    from ai_trader.trading import selfcheck as SC

    st = SimpleNamespace(pending=_FakePending(), db=FakeDB())
    SC._mark_cancelled(st, _row(next_index=7), False)
    w = st.pending.written
    assert w[0]["state"] == T.CLOSED_THESIS
    assert w[0]["state"] in T.TERMINAL_STATES
