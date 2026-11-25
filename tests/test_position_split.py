# -*- coding: utf-8 -*-
"""拆仓（系统仓 / 人工仓分开记账）的回归测试。

## 为什么需要这一组

2026-11-24 的生产事故，以及用户随后给出的纠正，共同指向同一个结构性缺陷：

    引擎只有**一个** `self.pos`，于是"这手仓是谁开的"这件事在代码里不存在。
    币安永续一个品种只有一个净持仓，交易所答不上来；内存里又不分，
    结果就是：任何一条规则都能对任何来源的仓动手。

用户原话（三句，逐条对应本文件里的测试）：

1. 「系统帮我管理手工单的出场我是满意的，只是要正确」
   → 人工仓的出场由 `_step_adopted_exit` 管，判据只用接管之后的 K 线。
2. 「有人工单时不挂，但是系统策略痕迹要留，方便回溯找问题」
   → 有手工仓时不下单，但决策照常落库（`pending_suppressed`）。
3. 「系统决策跟下不下单没一点关系，系统不停，决策不止」
   「假如已存在的单子是系统单子，属于加仓行为，当然要执行」
   → 系统单成交必须落账为**加仓**，而且只能记自己那一份。
"""
import asyncio
from types import SimpleNamespace

import pytest

from ai_trader.trading import engine as E
from ai_trader.trading import thesis as T
from ai_trader.trade_db import ADOPT_REASON


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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

    def risk_kinds(self):
        return [k for k, _ in self.risks]


class FakeEx(object):
    """只实现落账路径用到的那两个方法。"""

    def __init__(self, filled=0.010, qty=0.042):
        self._filled = filled
        self._qty = qty
        self.market_calls = []

    async def query_order(self, client_id="", order_id="", symbol=None):
        # `pending_own_filled_qty` 用 **属性** 访问（getattr(od, "status")），
        # 不是 dict 下标 —— 返回 dict 会被判成"查不到"，直接走保守分支。
        return SimpleNamespace(
            status="FILLED" if self._filled > 0 else "NEW",
            executed_qty=self._filled, client_id=client_id)

    async def position(self):
        return {"qty": self._qty, "entry": 61000.0,
                "side": "buy" if self._qty > 0 else "sell"}

    async def market_order(self, side, qty, reason="", reduce_only=False,
                           client_id=""):
        self.market_calls.append((side, qty, reduce_only))
        return SimpleNamespace(ok=True, price=61000.0, fee=1.0)


def _manual_lot(qty=0.032, entry=60880.0, stop=45660.0):
    return {"side": "long", "qty": qty, "entry": entry, "stop": stop,
            "target": 0.0, "entry_ts": 1795390000000, "bars": 0,
            "best": entry, "worst": entry, "trade_id": None,
            "max_hold_bars": 10 ** 9, "open_reason": ADOPT_REASON,
            "paper": 0, "fee": 0.0}


def _thesis(direction=T.UP, qty=0.010, price=61178.10):
    return T.Thesis(thesis_id="下跌三引线假突破限价单|up|1795481700000|61178.1",
                    kind="x", direction=direction, order_price=price,
                    stop_price=60231.95, target_price=62216.10,
                    order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL,
                    surprise_pts=0.0)


def _engine_stub(adopted=None, pos=None, ex=None):
    st = SimpleNamespace()
    st.ex = ex or FakeEx()
    st.db = FakeDB()
    st.tcfg = SimpleNamespace(symbol="BTCUSDT", strategy="pending",
                              max_hold_bars=600, trend_stop_pct=25.0)
    st.pos = pos
    st.adopted_pos = adopted
    # 只把被测到的引擎方法绑上去。
    # ★ 必须用 MethodType 手工绑定：直接从 `__dict__` 取到的是**未绑定**函数，
    #   挂到实例上不会自动获得 self（这是普通属性，不是描述符）。
    import types as _types
    for _name in ("_adopted_slot", "_is_adopted", "_norm_side",
                  # A1（2026-11-25 交易域审查）：平仓之后要撤/改交易所侧
                  # 那张保护性止损单，`_adopted_close` 会调它们。
                  "_adopted_stop_state", "_adopted_stop_cid",
                  "_sync_adopted_stop_order", "_cancel_adopted_stop_order",
                   "_pending_plan_ts", "_pending_repair_params",
                  # 2026-11-25 晚：成交落账/平仓现在还会推飞书、撤地板单。
                  # 桩上绑产品真实现（空壳会让"接线是否发生"测成空白）。
                  "_push", "_system_stop_state", "_cancel_system_stop_order",
                  "_maintain_system_protection"):
        _fn = E.DecisionEngine.__dict__[_name]
        _fn = getattr(_fn, "__func__", _fn)
        setattr(st, _name, _types.MethodType(_fn, st))
    # F1（2026-11-24 全链路审查）：`_adopted_close` 现在要判断
    # "卖出量 < 持仓量 = 部分平仓"，需要交易所步长。
    # 0.001 是 BTCUSDT 永续的真实步长。
    st._qty_step = lambda: 0.001
    st._liq_cache = None
    return st


# ------------------------------------------------- 1. 加仓只记自己那一份

def test_book_fill_books_only_our_own_filled_qty_not_account_net():
    """★ 核心：账户净量 0.042 = 人工 0.032 + 我这一单 0.010。

    旧写法拿 `abs(actual["qty"])`（=0.042）落账，于是用户那 0.032
    被"洗"成 `挂单成交` 的策略仓 —— 它的入场价和止损同时消失。
    正确做法：只记查单确认的**自己成交的那 0.010**。
    """
    st = _engine_stub(adopted=_manual_lot(), ex=FakeEx(filled=0.010, qty=0.042))
    _run(E.DecisionEngine._pending_book_fill(st, _thesis()))

    assert len(st.db.trades) == 1
    rec = st.db.trades[0]
    assert rec["qty"] == pytest.approx(0.010), (
        "落账量必须是【自己成交的量】，不能是账户净量 0.042 —— "
        "后者会把用户的人工仓洗成策略仓")
    assert rec["open_reason"] == E.PENDING_FILL_REASON
    # 系统仓 = 我这 0.010；人工仓原样不动
    assert st.pos is not None and st.pos["qty"] == pytest.approx(0.010)
    assert st.adopted_pos is not None
    assert st.adopted_pos["qty"] == pytest.approx(0.032), "人工仓数量被改了"
    assert st.adopted_pos["entry"] == pytest.approx(60880.0), "人工仓成本被改了"
    assert st.adopted_pos["stop"] == pytest.approx(45660.0), "人工仓止损被改了"
    assert st.adopted_pos["open_reason"] == ADOPT_REASON


def test_book_fill_adds_to_existing_system_lot_without_touching_manual():
    """已有系统仓时同向成交 = 加仓：只加系统仓，人工仓一根手指都不许碰。"""
    sys_lot = {"side": "long", "qty": 0.005, "entry": 60000.0, "stop": 59000.0,
               "target": 66000.0, "entry_ts": 1795395000000, "bars": 0,
               "best": 60000.0, "worst": 60000.0, "trade_id": None,
               "max_hold_bars": 600, "open_reason": E.PENDING_FILL_REASON,
               "paper": 0, "fee": 0.0}
    st = _engine_stub(adopted=_manual_lot(), pos=dict(sys_lot),
                      ex=FakeEx(filled=0.010, qty=0.047))
    _run(E.DecisionEngine._pending_book_fill(st, _thesis()))

    assert st.pos["qty"] == pytest.approx(0.015), "系统仓应累加到 0.005+0.010"
    assert st.pos["entry"] == pytest.approx(
        (60000.0 * 0.005 + 61178.10 * 0.010) / 0.015, rel=1e-9)
    assert st.adopted_pos["qty"] == pytest.approx(0.032)
    assert st.adopted_pos["entry"] == pytest.approx(60880.0)


def test_book_fill_refuses_when_exchange_fills_more_than_the_account_holds():
    """查单成交量 > 账户净量 = 数据不自洽，宁可停手也不写一个超量。"""
    st = _engine_stub(ex=FakeEx(filled=0.050, qty=0.042))
    _run(E.DecisionEngine._pending_book_fill(st, _thesis()))
    assert st.db.trades == []
    assert "pending_fill_qty_inconsistent" in st.db.risk_kinds()


# ------------------------------------------------- 2. 两个来源的出场互不越权

def test_adopted_close_only_closes_the_manual_lot():
    """平人工仓时只平人工仓的量，并且只清人工仓那个槽。"""
    sys_lot = {"side": "long", "qty": 0.010, "entry": 61000.0, "stop": 60000.0,
               "target": 62000.0, "entry_ts": 1795395000000, "bars": 0,
               "best": 61000.0, "worst": 61000.0, "trade_id": None,
               "max_hold_bars": 600, "open_reason": E.PENDING_FILL_REASON,
               "paper": 0, "fee": 0.0}
    ex = FakeEx()
    st = _engine_stub(adopted=_manual_lot(), pos=dict(sys_lot), ex=ex)
    _run(E.DecisionEngine._adopted_close(st, 0.032, "long", 60880.0, "测试"))

    assert ex.market_calls == [("SELL", 0.032, True)], (
        "必须只平人工仓那 0.032，且带 reduce_only，实际 %r" % (ex.market_calls,))
    assert st.adopted_pos is None, "人工仓槽要清空"
    assert st.pos is not None, "系统仓不许被这条路径清掉"
    assert st.pos["qty"] == pytest.approx(0.010)
    # 关账记录必须只覆盖人工仓那笔
    assert len(st.db.closes) == 1
    assert st.db.closes[0]["qty"] == pytest.approx(0.032)
    assert st.db.closes[0]["open_reason"] == ADOPT_REASON


def test_pending_close_still_refuses_to_touch_the_manual_lot():
    """纵深防御：`_pending_close` 面对一个人工仓必须拒绝（归属闸）。"""
    ex = FakeEx()
    st = _engine_stub(pos=_manual_lot(), ex=ex)
    act = SimpleNamespace(thesis_id="x", reason="r")
    _run(E.DecisionEngine._pending_close(st, act, None))
    assert ex.market_calls == [], "挂单链接管路径把人工仓卖了"
    assert "pending_close_not_owned" in st.db.risk_kinds()


# ------------------------------------------------- 3. 决策不因手工仓而停

def test_thesis_brief_describes_what_it_is_waiting_for():
    """决策文本必须回答「在等什么、离触发多远」，而不是只写"跳过"。"""
    st = _engine_stub()
    st.pos = None
    st.adopted_pos = None
    th = _thesis()
    txt = E.DecisionEngine._pending_thesis_brief(st, th, T.EXPECTED, 61000.0)
    # ★ 2026-11-25：必须写明「未挂单」。原来只写「等触发」，
    #   而生产上 EXPECTED 一条单都没挂到交易所 —— 那句话是误导性的。
    assert "未挂单" in txt
    assert "61178" in txt, "必须报出挂单价"
    assert "距挂单价" in txt, "必须报出距离（回溯时最想知道的就是这个）"


def test_thesis_brief_for_suppressed_plan_names_price_qty_and_stop():
    """A2：被抑制的计划要逐条写清「本应挂什么价、多少量、止损在哪」。"""
    st = _engine_stub(adopted=_manual_lot())
    th = _thesis()
    txt = E.DecisionEngine._pending_thesis_brief(st, th, T.EXPECTED, 61000.0,
                                                 suppressed=True)
    assert "应挂" in txt
    assert "61178" in txt, "必须写出本应挂出的价格"
    assert "止损" in txt, "必须写出止损位"
    assert "只决策不下单" in txt, "必须说清为什么没下单"


# ------------------------------------------------- 公共：_step_pending 替身装配

def _bind_step_pending(st):
    """给 SimpleNamespace 替身绑上 `_step_pending` 会用到的那几个引擎方法。

    ★ 必须分两类处理（两种都踩过）：
      - staticmethod（类字典里是 staticmethod 对象）→ 取 `__func__` 后**不许**
        再绑 self，否则会多传一个参数：
        `TypeError: takes 1 positional argument but 2 were given`；
      - 普通方法 → 必须手工 `MethodType` 绑 self（实例属性不是描述符，
        不会自动绑定，直接挂上去会报 "missing 1 required positional argument"）。
    """
    import types as _types

    async def _noop(*a, **kw):
        return None

    st._step_selfcheck = _noop
    st._step_adopted_exit = _noop
    st._pending_5m_ts = 0
    st._pending_is_dry = lambda: True
    st._pending_kbar_view = lambda: None
    st._pending_harvest = lambda b15, b5: None
    st.pending_lines = None
    st.decisions_log = []
    st._log_decision = lambda *a, **kw: st.decisions_log.append((a, kw))

    for _name in ("_pending_start_index", "_to_pending_bars",
                  "_adopted_bars", "_adopted_anchor_ts",
                  "_confirm_stop_price"):
        _fn = E.DecisionEngine.__dict__[_name]
        setattr(st, _name, getattr(_fn, "__func__", _fn))
    for _name in ("_pending_thesis_brief", "_adopted_slot"):
        _fn = E.DecisionEngine.__dict__[_name]
        setattr(st, _name,
                _types.MethodType(getattr(_fn, "__func__", _fn), st))
    return st


def _pending_bars():
    """两档 K 线：3s（当前桶）+ 15m（状态机用）。"""
    return {
        300000: [SimpleNamespace(open_time=1795400000000, open=61000.0,
                                 high=61100.0, low=60900.0, close=61050.0)],
        900000: [SimpleNamespace(open_time=1795395000000 + i * 900000,
                                 open=61000.0, high=61100.0, low=60900.0,
                                 close=61000.0) for i in range(4)],
    }


def _fake_pending(written, applied=None, rows=None):
    """pending store 的最小替身。"""
    applied = [] if applied is None else applied
    return SimpleNamespace(
        live=lambda limit=200: (rows or []),
        applied=applied,
        written=written,
        upsert_thesis=lambda r: written.append(r),
        put_action=lambda *a, **kw: True,
        mark_applied=lambda *a, **kw: applied.append(a))


def _row_of(th, **over):
    row = {"thesis_id": th.thesis_id, "kind": th.kind,
           "direction": th.direction, "family": th.family,
           "order_price": th.order_price, "stop_price": th.stop_price,
           "target_price": th.target_price, "order_type": th.order_type,
           "horizon_bars": 96, "stale_distance": 0.0, "surprise_pts": 0.0,
           "bar_ts": 1795400000000, "state": T.EXPECTED, "next_index": 0,
           "armed_at_bar": 0, "armed_ref_close": 0.0,
           "filled_ts": 0, "filled_price": 0.0, "exit_ts": 0,
           "exit_price": 0.0, "net_r": 0.0, "ambiguous": 0, "note": "",
           "updated_ts": 0}
    row.update(over)
    return row


# ------------------------------------------------- 4. side 规范化

def test_side_normalisation_covers_all_three_spellings():
    """side 在项目里有三种写法：long/short、buy/sell、BUY/SELL。

    对账里有一句 `_own * (1 if side == "long" else -1)`：任何不等于 "long"
    的写法都会被当成空头 —— 一笔 "buy" 的多仓会被算成负数，差额符号
    直接反掉。这是我在 2026-11-24 修对账时踩到的真缺陷。
    """
    for s in ("long", "LONG", "buy", "BUY", "b"):
        assert E.norm_side(s) == "long", "%r 应归一成 long" % (s,)
    for s in ("short", "SHORT", "sell", "SELL", "s"):
        assert E.norm_side(s) == "short", "%r 应归一成 short" % (s,)
    assert E.norm_side("") == ""
    assert E.norm_side(None) == ""


# ------------------------------------------------- 5. 幽灵动作防线

def test_suppressed_place_order_discards_the_rest_of_the_batch(monkeypatch):
    """★★ 被压制的 place_order 必须让**整批**动作作废，而不是只跳过它自己。

    同一批 actions 里排在 place_order 后面的成交/出场动作，全部以
    「这张单存在」为前提。只 `continue` 跳过挂单、却继续执行后面的成交，
    就等于拿一个**不存在的仓位**下单 —— 11-24 那 15 条幽灵动作
    （状态机从 K 线推出 FILLED、再推出 close，而交易所那头根本没单）
    正是这么产生的。

    这条测试第一次是失败的：我先写成了 `continue`，复查 diff 时才发现。
    """
    from ai_trader.trading import pending_thesis_store as PTS

    th = _thesis()
    row = _row_of(th)

    class _Res(object):
        ambiguous = False
        state = T.EXPECTED
        # 关键：成交/出场动作排在挂单**后面**
        actions = [SimpleNamespace(kind="place_order", thesis_id=th.thesis_id),
                   SimpleNamespace(kind="fill", thesis_id=th.thesis_id),
                   SimpleNamespace(kind="close", thesis_id=th.thesis_id)]

    monkeypatch.setattr(
        T, "step",
        lambda *a, **kw: (_Res(),
                          {"state": T.ARMED, "next_index": 9,
                           "armed_at_bar": 1795400000000,
                           "armed_ref_close": 61050.0}))

    st = _bind_step_pending(_engine_stub(adopted=_manual_lot()))
    st.pending = _fake_pending([])

    applied = []

    async def _apply(act, th_, ts):
        applied.append(str(act.kind))
        return True

    st._apply_pending_action = _apply
    st.pending = _fake_pending([], rows=[row])
    st._apply_pending_action = _apply

    _run(E.DecisionEngine._step_pending(st, _pending_bars(), 61000.0, 100.0,
                                       1795400000000))

    assert applied == [], (
        "挂单被压制后，同一批里的成交/出场动作也必须全部丢弃，"
        "否则就是拿不存在的仓位下单（幽灵动作），实际执行了 %r" % (applied,))


# ------------------------------------------------- 6. 幽灵成交自愈

def test_phantom_filled_thesis_is_cancelled_not_retried_forever(monkeypatch):
    """`state=FILLED` 但没有成交记录、也没仓 → 必须终结，不能每根 bar 重试。

    生产库里真实存在这样一条（`filled_ts=0`、`filled_price=0.0`、
    交易所那张 `AI-pend-*` 单从未存在过）。不终结的话它每 5 分钟
    推一个 close，`_pending_close` 看到空仓就返回、不 mark_applied，
    于是无限重试、把决策流刷成噪声。
    """
    th = _thesis()
    row = _row_of(th, state="FILLED", next_index=400, armed_ref_close=64564.2,
                  filled_ts=0, filled_price=0.0)

    def _boom(*a, **kw):
        raise AssertionError("幽灵成交不该再被推进（会无限重试）")

    monkeypatch.setattr(T, "step", _boom)

    st = _bind_step_pending(_engine_stub())
    st.pending = _fake_pending([], rows=[row])

    _run(E.DecisionEngine._step_pending(st, _pending_bars(), 61000.0, 100.0,
                                       1795400000000))

    assert st.pending.written, "幽灵成交必须被写回终结"
    assert st.pending.written[0]["state"] == "CANCELLED", (
        "幽灵成交应终结为 CANCELLED，实际 %r"
        % (st.pending.written[0]["state"],))
    assert "幽灵" in str(st.pending.written[0].get("note") or ""), (
        "终结时必须写明原因，实际 %r" % (st.pending.written[0].get("note"),))
    assert "pending_phantom_filled" in st.db.risk_kinds(), "必须留一条风险事件"


def test_real_filled_thesis_is_not_treated_as_phantom(monkeypatch):
    """反向约束：真有成交时刻（filled_ts>0）的 FILLED 绝不能被当成幽灵。

    少了这一条，一个"把所有 FILLED 都取消掉"的实现也能过上面的测试，
    而那会把真实持仓的记录抹掉。
    """
    th = _thesis()
    row = _row_of(th, state="FILLED", next_index=400,
                  armed_at_bar=1795400000000, armed_ref_close=61050.0,
                  filled_ts=1795400300000, filled_price=61178.1)

    called = {"n": 0}

    class _Res(object):
        ambiguous = False
        state = T.FILLED
        actions = []

    def _step(*a, **kw):
        called["n"] += 1
        return _Res(), {"state": T.FILLED, "next_index": 401}

    monkeypatch.setattr(T, "step", _step)

    st = _bind_step_pending(_engine_stub())
    st.pending = _fake_pending([], rows=[row])

    _run(E.DecisionEngine._step_pending(st, _pending_bars(), 61000.0, 100.0,
                                       1795400000000))

    assert called["n"] == 1, "真成交的 thesis 必须照常推进"
    for w in st.pending.written:
        assert w.get("state") != "CANCELLED", (
            "真成交的 thesis 被误判成幽灵并取消了：%r" % (w,))
