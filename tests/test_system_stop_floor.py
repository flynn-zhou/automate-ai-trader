# -*- coding: utf-8 -*-
"""B2：策略仓在交易所侧的**地板单**，以及它成交后怎么记账。

用户裁决（2026-11-25）：「我们做的是抄底行为，可能在 10 分钟内发生超级
大波动」。接管仓那张保护单已经提前到接管那一刻了，但**策略仓**（我们
自己开的仓，也就是常态仓）此前只有进程内止损 —— 进程一挂就裸奔。

两个必须钉住的点：

1. **地板单真的挂在交易所上**，而且是在接管仓为空时也会挂。
   （我第一版把维护调用写进了 `if self._is_adopted():` 里面 —— 顺序对、
   作用域错，等于"只有接管仓存在时才保护策略仓"，没有接管仓恰恰是最常见
   的情形。这种错源码看着很合理，只能靠断言钉住。）

2. **地板成交后不许记成打平**。原来对账遇到「交易所有仓 → 本地空仓」
   一律按入场价记平仓（pnl=0）。加了地板单之后这条路会被真实走到，
   于是真实亏损会被记成 0 —— 把风控做成了造假账。
"""
import asyncio
import inspect
from types import SimpleNamespace

import pytest

from ai_trader.trading import engine as E


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


class _Order(object):
    def __init__(self, oid="777", status="NEW", stop=0.0, qty=0.0,
                 avg=0.0, executed=None, cid=""):
        self.order_id = oid
        self.status = status
        self.stop_price = stop
        self.qty = qty
        self.avg_price = avg
        self.executed_qty = qty if executed is None else executed
        self.client_id = cid


class _Ex(object):
    name = "live"

    def __init__(self, existing=None, place_err=""):
        self.placed = []
        self.cancelled = []
        self._existing = existing
        self._place_err = place_err

    async def place_order(self, side, qty, **kw):
        self.placed.append(dict(kw, side=side, qty=qty))
        if self._place_err:
            return None, self._place_err
        return _Order(stop=kw.get("stop_price") or 0.0, qty=qty,
                      cid=kw.get("client_id") or ""), ""

    async def cancel_order(self, order_id=""):
        self.cancelled.append(order_id)
        return True

    async def query_order(self, order_id="", client_id=""):
        return self._existing


class _DB(object):
    def __init__(self):
        self.risks = []
        self.closes = []

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, detail))

    def close_trade(self, t):
        self.closes.append(t)

    def log_decision(self, *a, **kw):
        pass


def _st(pos=None, ex=None):
    st = SimpleNamespace()
    st.ex = ex or _Ex()
    st.db = _DB()
    st.tcfg = SimpleNamespace(symbol="BTCUSDT", strategy="pending")
    st.pos = pos
    st.adopted_pos = None
    st.feed = None
    st._qty_step = lambda: 0.001
    st._norm_side = lambda s: (s or "").lower()
    for n in ("_push", "_system_stop_state", "_cancel_system_stop_order",
              "_maintain_system_protection", "_resolve_closed_slot_price",
              "_adopted_stop_cid", "_adopted_stop_state"):
        raw = E.DecisionEngine.__dict__.get(n)
        if isinstance(raw, staticmethod):
            setattr(st, n, raw.__func__)
        else:
            setattr(st, n, lambda *a, _n=n, **kw:
                    getattr(E.DecisionEngine, _n)(st, *a, **kw))
    return st


def _pos(entry=100.0, stop=95.0, qty=0.01, side="long", ts=1795000000000):
    return {"side": side, "qty": qty, "entry": entry, "stop": stop,
            "target": 120.0, "entry_ts": ts, "fee": 0.05, "paper": 0}


# ------------------------------------------------------------ 1. 地板单挂出

def test_system_stop_is_placed_for_a_strategy_position():
    """策略仓必须有一张 reduceOnly STOP_MARKET 挂在交易所上。"""
    st = _st(pos=_pos())
    _run(E.DecisionEngine._maintain_system_protection(st, 110.0))
    assert len(st.ex.placed) == 1, "策略仓没有挂地板单：%r" % (st.ex.placed,)
    o = st.ex.placed[0]
    assert o["order_type"] == "STOP_MARKET"
    assert o["reduce_only"] is True, "不是 reduceOnly 就有开反向仓的风险"
    assert o["side"] == "SELL", "多头仓的保护单必须是 SELL"
    assert o["stop_price"] == pytest.approx(95.0)
    assert o["qty"] == pytest.approx(0.01)
    assert o["client_id"].startswith("AI-SLP-"), (
        "地板单的 client_id 必须以 AI-SLP- 开头（与接管仓 AI-SL- 区分）：%r"
        % (o["client_id"],))


def test_system_stop_is_idempotent_when_already_aligned():
    """已对齐就不许再发请求（每 3 秒一次，重复挂会打爆限频）。"""
    st = _st(pos=_pos())
    _run(E.DecisionEngine._maintain_system_protection(st, 110.0))
    _run(E.DecisionEngine._maintain_system_protection(st, 110.0))
    assert len(st.ex.placed) == 1, "重复挂了：%r" % (st.ex.placed,)


def test_system_stop_is_cancelled_when_position_is_gone():
    """没有策略仓就必须撤掉地板单，否则白占保证金。"""
    st = _st(pos=_pos())
    _run(E.DecisionEngine._maintain_system_protection(st, 110.0))
    st.pos = None
    _run(E.DecisionEngine._maintain_system_protection(st, 110.0))
    assert st.ex.cancelled, "策略仓没了但地板单还挂着"


def test_system_stop_is_skipped_when_price_already_through_it():
    """止损已被现价穿过 → 不挂（币安会以 -2021 拒单），交给进程内平。"""
    st = _st(pos=_pos(stop=95.0))
    _run(E.DecisionEngine._maintain_system_protection(st, 94.0))
    assert st.ex.placed == [], "止损已被穿过还去挂，只会换来一次拒单"
    assert "system_stop_immediate" in [k for k, _ in st.db.risks]


def test_system_stop_is_skipped_on_paper():
    """纸面模式不许挂：条件单要 on_bar_5m 才触发，会造成"以为有保护"。"""
    st = _st(pos=_pos())
    st.ex = _ex = _Ex()
    _ex.name = "paper"
    _run(E.DecisionEngine._maintain_system_protection(st, 110.0))
    assert st.ex.placed == []


# --------------------------------------------------- 2. 作用域（我踩过的坑）

def test_step_maintains_system_stop_outside_the_adopted_branch():
    """`_step` 里策略仓地板单的维护**不许**放在 `if self._is_adopted():` 里。

    ★ 这条是给我自己的第一版纠错：我把它写进了接管仓分支里面，等于
      「只有接管仓存在时才保护策略仓」。而**没有接管仓恰恰是最常见的情形**
      —— 也就是说那版代码在常态下完全不保护策略仓，而且源码看起来完全合理。
    """
    src = inspect.getsource(E.DecisionEngine._step)
    body = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#"))
    i_call = body.find("_maintain_system_protection(")
    assert i_call != -1, "`_step` 没有维护策略仓地板单"
    i_gate = body.find("if self._is_adopted():")
    assert i_gate != -1, "找不到接管仓分支（结构变了就更新这条判据）"
    assert i_call < i_gate, (
        "策略仓地板单的维护被放进了接管仓分支之后/之内 —— 没有接管仓时"
        "它就完全不跑，而那是常态")


def test_reconcile_places_the_system_stop_after_rebuild():
    """重启后本地有仓、交易所侧没地板单 → 对账里就要补上。"""
    src = inspect.getsource(E.DecisionEngine._reconcile_positions)
    body = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#"))
    assert "_maintain_system_protection(" in body, (
        "对账恢复仓位之后没有补地板单 —— 重启到第一轮 _step 之间是裸奔的")


# ------------------------------------------- 3. 地板成交后按真实价记账

def test_stop_fill_is_booked_at_its_real_price_not_at_entry():
    """地板单成交 → 用它的成交价记账，不许记成「入场价 = 平仓价」。"""
    ex = _Ex(existing=_Order(oid="9", status="FILLED", stop=95.0,
                             qty=0.01, avg=94.0, executed=0.01))
    st = _st(pos=_pos(), ex=ex)
    got = _run(E.DecisionEngine._resolve_closed_slot_price(st, st.pos))
    assert got is not None, "地板单明明成交了，却没认出来"
    px, fee, src = got
    assert px == pytest.approx(94.0), "没用真实成交价：%r" % (px,)
    assert fee > 0, "平仓手续费应当是正数（不许记 0）"
    assert "AI-SLP-" in src


def test_stop_fill_falls_back_to_none_when_nothing_is_found():
    """查不到就返回 None（调用方退回占位记账 + 记风险事件），绝不猜价。"""
    st = _st(pos=_pos(), ex=_Ex(existing=None))
    assert _run(E.DecisionEngine._resolve_closed_slot_price(st, st.pos)) is None


def test_partial_fill_is_not_used_as_the_whole_position_price():
    """只成交了一部分 → 不拿它当整笔的平仓价（剩下的不是这张单平的）。"""
    ex = _Ex(existing=_Order(oid="9", status="FILLED", qty=0.01,
                             avg=94.0, executed=0.004))
    st = _st(pos=_pos(), ex=ex)
    assert _run(E.DecisionEngine._resolve_closed_slot_price(st, st.pos)) is None


def test_unpriced_close_is_not_silent():
    """还原不了时，占位记账必须留一条风险事件（它记出来的盈亏是 0）。"""
    src = inspect.getsource(E.DecisionEngine._reconcile_positions)
    assert "position_closed_unpriced" in src, (
        "按入场价占位记账时没有留痕 —— 那笔交易的盈亏会静默变成 0")
