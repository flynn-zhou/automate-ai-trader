# -*- coding: utf-8 -*-
"""2026-11-24 全链路审查的回归测试（F1/F2/F3/F4/F5/F7/F8/F9/F10）。

这批测试的每一条都对应一个**已证实**的缺陷，不是"以防万一"。
它们的作用是：任何人再把那行代码改回去，测试立刻变红。

写测试时的取舍：凡是能断言"某件事**没有**发生"的，就不断言"结果正确"。
因为这类缺陷的共同形态是 —— 代码看起来做对了某件事，实际没做。
"""
from __future__ import annotations

import asyncio
import types

import pytest

from ai_trader.trading import engine as E


# ------------------------------------------------------------------ 脚手架
def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class _Db(object):
    def __init__(self):
        self.decisions = []
        self.risks = []
        self.closes = []
        self.trades = []

    def log_decision(self, d):
        self.decisions.append(d)

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, detail))

    def close_trade(self, t):
        self.closes.append(t)

    def log_trade(self, t):
        self.trades.append(t)

    def risk_kinds(self):
        return [k for k, _ in self.risks]

    def decision_actions(self):
        return [d["action"] for d in self.decisions]


class _Fill(object):
    def __init__(self, price=61250.0, fee=0.5, ok=True, reason="", qty=0.0):
        self.ok = ok
        self.price = price
        self.fee = fee
        self.reason = reason
        # ★ 真实成交量。`qty=0` 表示执行器没给（走计划量兜底）。
        #   生产里 `market_order` 返回的是 `FillResult.qty=filled`。
        self.qty = qty


class _Ex(object):
    def __init__(self, price=61250.0, liq=58356.2, fill_qty=None):
        self.calls = []
        self._price = price
        self._liq = liq
        # 部分成交模拟：None = 完全按请求量成交
        self._fill_qty = fill_qty

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.calls.append((side, qty, reduce_only))
        filled = qty if self._fill_qty is None else min(self._fill_qty, qty)
        return _Fill(price=self._price, qty=filled)

    async def mark_price(self):
        return self._price

    async def position(self):
        return {"qty": 0.032, "entry": 60880.0, "side": "long",
                "liq_price": self._liq, "unrealized": 0.0}


def _adopted(qty=0.032, entry=60880.0, stop=59894.81, fee=0.5):
    return {"side": "long", "qty": qty, "entry": entry, "stop": stop,
            "target": 0.0, "entry_ts": 1795400000000, "bars": 0,
            "best": entry, "worst": entry, "trade_id": None,
            "max_hold_bars": 0, "open_reason": E.ADOPT_REASON,
            "paper": 0, "fee": fee}


def _engine(adopted=None, pos=None, ex=None, liq=58356.2, liq_pct=4.0455,
            step=0.001):
    st = types.SimpleNamespace()
    st.db = _Db()
    st.ex = ex or _Ex(liq=liq)
    st.pos = pos
    st.adopted_pos = adopted
    st.tcfg = types.SimpleNamespace(symbol="BTCUSDT", strategy="pending",
                                    max_hold_bars=600, trend_stop_pct=25.0,
                                    qty_step=step)
    st.decisions = 0
    st._last_bar_time = 0
    st.last_decision = None
    st._liq_cache = None
    st._liq_src = ""
    st._last_equity = 114.18
    st.feed = types.SimpleNamespace(
        hub=types.SimpleNamespace(publish=lambda m: None),
        agg=types.SimpleNamespace(
            current=lambda iv: types.SimpleNamespace(
                high=61100.0, low=61000.0, close=61050.0, open=61020.0)))
    st._qty_step = lambda: step
    st._liq_distance_pct = lambda: liq_pct
    st._sizing_params = lambda: None
    st._stop_distance_pct = lambda ref=0.0: liq_pct / 2.5
    st._confirm_stop_price = _confirm_factory()
    st._guard_config_for_observe = lambda: _guard_cfg()

    for name in ("_log_decision", "_adopted_close", "_adopted_slot",
                 "_is_adopted", "_adopted_anchor_ts", "_adopted_bars",
                 "_guard_position_for_adopted", "_guard_position_for_observe",
                 "_resolve_liq", "_live_liq_price", "_persist_adopted_stop",
                 "_step_adopted_exit", "_guard_observe", "_bar_ts",
                 "_bar_ohlcv",
                 # A1（2026-11-25 交易域审查）：交易所侧保护性止损单
                 "_adopted_stop_state", "_adopted_stop_cid",
                 "_sync_adopted_stop_order", "_cancel_adopted_stop_order"):
        raw = E.DecisionEngine.__dict__[name]
        if isinstance(raw, staticmethod):
            setattr(st, name, raw.__func__)
        else:
            def _bound(*a, _n=name, **kw):
                return getattr(E.DecisionEngine, _n)(st, *a, **kw)
            _bound.__name__ = name
            setattr(st, name, _bound)
    return st


def _confirm_factory(passthrough=True):
    async def _confirm(local):
        return local
    return _confirm


def _guard_cfg():
    from ai_trader.trading.position_guard import GuardConfig
    return GuardConfig(risk_per_trade=10.0, max_adds=0, max_partials=0)


def _bars(n=40, px=61000.0, ts0=1795400000000):
    out = []
    for i in range(n):
        t = ts0 + i * 900000
        out.append(types.SimpleNamespace(
            open_time=t, open=px, high=px + 40, low=px - 40, close=px,
            volume=1.0, interval_ms=900000))
    return out


def _bars_by_iv(b15):
    return {3000: [types.SimpleNamespace(
        open_time=1795400000000 + 3000, open=61000.0, high=61100.0,
        low=61000.0, close=61050.0, volume=1.0)], 900000: b15}


# =================================================== F1：部分平仓不许清整槽
def test_partial_close_keeps_the_remaining_lot_alive():
    """★★★ REDUCE 卖一部分之后，剩下的仓**必须还在**。

    这是 2026-11-24 审查 F1 的核心。库里躺着一个 25% 止损的遗留接管仓时
    （`structural_buffer` = 4.15/25 = 0.166），守卫会判
    「减仓 1 - 0.166/2.5 = 93.4%」—— 一次就走到这条路径上。

    改之前：卖 93.4% → 整槽清空 → 剩下 6.6% 无人管、库里也没有。
    """
    ad = _adopted(qty=0.032)
    st = _engine(adopted=ad)
    _run(E.DecisionEngine._adopted_close(st, 0.030, "long", 60880.0, "REDUCE 测试"))

    assert st.adopted_pos is not None, "部分平仓后整槽被清空了 —— 剩余仓无人管"
    assert st.adopted_pos["qty"] == pytest.approx(0.002), (
        "剩余数量必须是 0.032-0.030=0.002，实际 %.6f" % st.adopted_pos["qty"])
    # 交易所只卖掉该卖的量
    assert st.ex.calls == [("SELL", 0.030, True)]


def test_partial_close_books_the_remainder_so_a_restart_can_find_it():
    """剩余仓必须在 trades 里有一条 open 行，否则重启等于丢仓。"""
    ad = _adopted(qty=0.032)
    st = _engine(adopted=ad)
    _run(E.DecisionEngine._adopted_close(st, 0.030, "long", 60880.0, "REDUCE 测试"))

    assert len(st.db.closes) == 1, "已卖部分要关账"
    assert st.db.closes[0]["qty"] == pytest.approx(0.030)
    assert len(st.db.trades) == 1, "剩余仓必须补一条 open 行"
    rem = st.db.trades[0]
    assert rem["qty"] == pytest.approx(0.002)
    assert rem["entry_price"] == pytest.approx(60880.0)
    assert rem["entry_ts"] == 1795400000000, (
        "entry_ts 必须沿用原值 —— 改了它，判断锚点历史一起丢")
    assert str(rem["open_reason"]) == E.ADOPT_REASON, (
        "剩余仓的归属必须仍是接管仓，否则重启后被当成策略仓")


def test_partial_close_splits_the_open_fee_proportionally():
    """开仓费按比例分摊：不许全压在部分腿上，也不许剩余仓一分成本不留。"""
    ad = _adopted(qty=0.032, fee=3.2)
    st = _engine(adopted=ad)
    _run(E.DecisionEngine._adopted_close(st, 0.016, "long", 60880.0, "半仓"))

    # 卖一半 → 开仓费 3.2 里 1.6 归这一腿，1.6 留个剩余仓
    assert st.db.closes[0]["fee"] == pytest.approx(1.6 + 0.5), (
        "本腿手续费应含 1.6 开仓费 + 0.5 平仓费")
    assert st.db.trades[0]["fee"] == pytest.approx(1.6), "剩余仓要留住另一半成本"


def test_full_close_still_clears_the_slot():
    """全平仍然清槽 —— 部分平仓的改动不许把这条正确行为改坏。"""
    ad = _adopted(qty=0.032)
    st = _engine(adopted=ad)
    _run(E.DecisionEngine._adopted_close(st, 0.032, "long", 60880.0, "全平"))

    assert st.adopted_pos is None
    assert st.db.trades == [], "全平不该补 open 行"


def test_dust_remainder_is_promoted_to_a_full_close():
    """剩余不足一个步长 = 卖不掉，必须升格为全平，不留粉尘。"""
    ad = _adopted(qty=0.032)
    st = _engine(adopted=ad)
    # 卖 0.0315，剩 0.0005 < step 0.001
    _run(E.DecisionEngine._adopted_close(st, 0.0315, "long", 60880.0, "粉尘"))

    assert st.ex.calls == [("SELL", 0.032, True)], (
        "剩 0.0005 卖不掉，应一次卖完 0.032，实际 %r" % (st.ex.calls,))
    assert st.adopted_pos is None
    assert st.db.trades == []


def test_booking_uses_the_actual_filled_qty_not_the_requested_one():
    """★★ 记账与减仓必须用 `fill.qty`（交易所回来的实际成交量）。

    这是 2026-11-24 审查 A6 逮到的、我在 F1 修复里自己引入的缺陷：
    `_close`（策略仓）早就写对了 ——
        sold_qty = fill.qty if (fill.qty and fill.qty > 0) else p["qty"]
    而 `_adopted_close` 拿**计划量**减内存持仓。部分成交时后果是
    把"没卖掉的那部分"从内存里抹掉 —— F1 想堵的那个洞又回来了：
    剩余仓无人管出场，库里也不存在。

    场景：计划卖 0.030，交易所只成交 0.020。
      · 正确：已平 0.020，剩余 0.012（内存 + 库里的 open 行都是 0.012）
      · 错误：已平 0.030，剩余 0.002 —— 0.010 凭空消失
    """
    ad = _adopted(qty=0.032)
    st = _engine(adopted=ad, ex=_Ex(fill_qty=0.020))
    _run(E.DecisionEngine._adopted_close(st, 0.030, "long", 60880.0, "部分成交"))

    assert st.db.closes[0]["qty"] == pytest.approx(0.020), (
        "关账量必须是实际成交量 0.020，不能是计划量 0.030")
    assert st.adopted_pos is not None
    assert st.adopted_pos["qty"] == pytest.approx(0.012), (
        "剩余必须按实际成交量算：0.032-0.020=0.012，实际 %.6f"
        % st.adopted_pos["qty"])
    assert st.db.trades[0]["qty"] == pytest.approx(0.012), (
        "补的 open 行也必须按实际成交量算")


def test_booking_falls_back_to_the_requested_qty_when_fill_reports_none():
    """执行器没给 `fill.qty`（0/缺字段）时退回计划量 —— 不许因此减成 0。"""
    ad = _adopted(qty=0.032)
    st = _engine(adopted=ad, ex=_Ex(fill_qty=0.0))
    _run(E.DecisionEngine._adopted_close(st, 0.030, "long", 60880.0, "无回报"))

    assert st.db.closes[0]["qty"] == pytest.approx(0.030)
    assert st.adopted_pos["qty"] == pytest.approx(0.002)


# =================================================== F3：出场必须留下决策行
def test_guard_driven_exit_writes_a_decision_row():
    """★★ 守卫驱动的出场必须落一条 `action="close"` 的决策。

    用户最初的困惑就是「我没明白系统为什么要在这个价位出场」。
    这条路径以前只写 trades + risk_events，**决策流里一条记录都没有**。
    """
    ad = _adopted(qty=0.032, stop=59894.81)
    st = _engine(adopted=ad)
    b15 = _bars()
    _run(E.DecisionEngine._step_adopted_exit(
        st, _bars_by_iv(b15), b15, 59000.0, 1795400000000))

    assert st.ex.calls, "止损没触发，测试前提不成立"
    assert "close" in st.db.decision_actions(), (
        "守卫出场没写决策行 —— 用户依然看不到为什么出场；"
        "实际动作 %r" % (st.db.decision_actions(),))
    row = [d for d in st.db.decisions if d["action"] == "close"][0]
    assert "守卫" in row["reason"]
    assert row["features"]["liq_src"], "决策行必须写明爆仓价来源"


def test_guard_driven_reduce_writes_a_reduce_row():
    """减仓同样要留痕（`action="reduce"`），否则事后无法解释半仓。"""
    ad = _adopted(qty=0.032, stop=60880.0 * (1 - 0.040455 / 2.5))
    st = _engine(adopted=ad)
    b15 = _bars()
    _run(E.DecisionEngine._step_adopted_exit(
        st, _bars_by_iv(b15), b15, 61000.0, 1795400000000))
    # 这条仓的止损几乎贴着上限，守卫会判 REDUCE
    acts = st.db.decision_actions()
    if st.ex.calls:                      # 若判定触发
        assert "reduce" in acts or "close" in acts, (
            "减仓/出场没留决策行，实际 %r" % (acts,))


# =================================================== F5：MFE/MAE 不许恒为 0
def test_adopted_close_records_real_mfe_mae():
    """★ 人工仓的 MFE/MAE 以前恒写 0，而这两列是判断"出场是否过早"的唯一依据。

    造一段行情：入场 60880，最高冲到 61550（用户说他准备拿到这里），
    然后在 61250 被平。MFE 必须反映那段冲高。
    """
    ad = _adopted(qty=0.032, entry=60880.0)
    ad["best"] = 61550.0        # 持仓期间最高
    ad["worst"] = 60700.0       # 持仓期间最低
    st = _engine(adopted=ad)
    _run(E.DecisionEngine._adopted_close(st, 0.032, "long", 60880.0, "测试"))

    rec = st.db.closes[0]
    assert rec["mfe"] == pytest.approx(61550.0 - 60880.0), (
        "MFE 必须是 (最高-入场)=670，不能是 0")
    assert rec["mae"] == pytest.approx(60700.0 - 60880.0), (
        "MAE 按行业惯例为负（不利方向）")


# =================================================== F4：stop<=0 不是"止损在 0 元"
def test_observe_skips_a_position_without_a_hard_stop():
    """★★ 唐奇安仓合法地没有止损（stop=0），守卫必须被**跳过**。

    若把 stop=0 翻译成「止损在 0 元」，守卫会算出
    `d_stop = 入场价 - 0 = 60880`，结构性缓冲 = 4.15/60880 ≈ 0.00007 倍
    → 判「减仓 99.99%」。观察期报表上会天天出现假分歧。
    """
    pos = {"side": "long", "qty": 0.032, "entry": 60880.0, "stop": 0.0,
           "target": 0.0, "bars": 1, "best": 60880.0, "worst": 60880.0,
           "open_reason": "挂单成交", "fee": 0.0}
    st = _engine(pos=pos)
    gp = E.DecisionEngine._guard_position_for_observe(st, 61000.0)
    assert gp is None, "没有止损的仓不许被翻译成 stop=0 的守卫持仓"


def test_observe_records_a_skip_reason_instead_of_silence():
    """跳过必须留一句话，否则报表上少掉的样本不会自己说明原因。"""
    pos = {"side": "long", "qty": 0.032, "entry": 60880.0, "stop": 0.0,
           "target": 0.0, "bars": 1, "best": 60880.0, "worst": 60880.0,
           "open_reason": "挂单成交", "fee": 0.0}
    st = _engine(pos=pos)
    b15 = _bars()
    E.DecisionEngine._guard_observe(st, _bars_by_iv(b15), 61000.0,
                                    1795400000000)
    assert st.db.decisions, "跳过时静默了 —— 观察期会看起来一切正常"
    row = st.db.decisions[0]
    assert row["action"] == "guard_compare"
    assert "跳过" in row["reason"]
    assert "止损" in row["reason"], "理由要说清是'无止损'还是'拿不到爆仓价'"


def test_adopted_lot_without_a_stop_gets_one_repaired_not_treated_as_zero():
    """人工仓没有止损时**补一个**（爆仓距离/安全倍数），不是当成 0 元。"""
    ad = _adopted(qty=0.032, stop=0.0)
    st = _engine(adopted=ad)
    b15 = _bars()
    _run(E.DecisionEngine._step_adopted_exit(
        st, _bars_by_iv(b15), b15, 61000.0, 1795400000000))

    assert st.adopted_pos is not None
    fixed = float(st.adopted_pos["stop"])
    assert fixed > 0, "止损没被补上"
    # 1.618% 的止损：60880 * (1 - 0.01618) ≈ 59895
    assert 59000 < fixed < 60500, "补出来的止损距离不对：%.2f" % fixed
    assert "adopted_stop_missing_repaired" in st.db.risk_kinds()


# =================================================== F7：爆仓价单一来源
def test_observe_and_adopted_pick_the_same_liq_price():
    """★ 同一个账户净仓只有一个真实爆仓价，两条路径必须选到同一个值。"""
    pos = {"side": "long", "qty": 0.032, "entry": 60880.0, "stop": 59894.81,
           "target": 0.0, "bars": 1, "best": 60880.0, "worst": 60880.0,
           "open_reason": "挂单成交", "fee": 0.0}
    st = _engine(pos=pos)
    _run(st._live_liq_price())      # 暖缓存（生产由 _step 做）
    gp_obs = E.DecisionEngine._guard_position_for_observe(st, 61000.0)
    gp_ad = _run(E.DecisionEngine._guard_position_for_adopted(
        st, _adopted(qty=0.032), 61000.0))

    assert gp_obs.liq_price == pytest.approx(gp_ad.liq_price), (
        "观察期 %.2f 与人工仓 %.2f 用了不同的爆仓价来源"
        % (gp_obs.liq_price, gp_ad.liq_price))
    assert gp_obs.liq_price == pytest.approx(58356.2), "应当用交易所真实值"


def test_liq_source_is_actually_reported():
    """`_liq_src` 必须真的出现在输出里，不许只赋值不输出。"""
    ad = _adopted(qty=0.032, stop=59894.81)
    st = _engine(adopted=ad)
    b15 = _bars()
    _run(E.DecisionEngine._step_adopted_exit(
        st, _bars_by_iv(b15), b15, 61000.0, 1795400000000,
        log_quiet=False))
    assert st._liq_src in ("交易所真实", "配置推算"), (
        "爆仓价来源没被赋值：%r" % (st._liq_src,))
    assert st.db.decisions, "noop 也要写决策行"
    assert st._liq_src in st.db.decisions[-1]["reason"], (
        "来源没写进决策 —— docstring 承诺了'记进日志'，就必须能读出来")


# =================================================== F8：观察行不许挤掉策略行
def test_observe_row_does_not_consume_the_bar_dedup_slot():
    """★★ 观察行不许推进 `_last_bar_time`。

    以前 `guard_compare` 用 `force=True` 写行，顺带把去重游标推到当前桶，
    于是同一 tick 里策略自己那条 `force=False` 的决策被挡掉 ——
    时间线更热闹，信息更少。
    """
    pos = {"side": "long", "qty": 0.032, "entry": 60880.0, "stop": 59894.81,
           "target": 0.0, "bars": 1, "best": 60880.0, "worst": 60880.0,
           "open_reason": "挂单成交", "fee": 0.0}
    st = _engine(pos=pos)
    b15 = _bars()
    before = st._last_bar_time
    E.DecisionEngine._guard_observe(st, _bars_by_iv(b15), 61000.0,
                                    1795400000000)
    assert st._last_bar_time == before, (
        "观察行推进了去重游标 —— 同一 tick 的策略决策会被它挤掉")

    # 随后策略自己那条必须写得进去
    E.DecisionEngine._log_decision(
        st, _bars_by_iv(b15), 61000.0, 100.0, 1795400000000, "trend_entry",
        "hold", "策略自己的决策")
    assert "hold" in st.db.decision_actions(), "策略决策被观察行挤掉了"


def test_observe_rows_are_not_counted_as_decisions():
    """观察记录不计入 `self.decisions` —— 否则策略真卡死时这个数字还在涨。"""
    pos = {"side": "long", "qty": 0.032, "entry": 60880.0, "stop": 59894.81,
           "target": 0.0, "bars": 1, "best": 60880.0, "worst": 60880.0,
           "open_reason": "挂单成交", "fee": 0.0}
    st = _engine(pos=pos)
    b15 = _bars()
    n0 = st.decisions
    E.DecisionEngine._guard_observe(st, _bars_by_iv(b15), 61000.0,
                                    1795400000000)
    assert st.decisions == n0, "观察行被算进决策数了"


# =================================================== F2：止损判定频率
def test_adopted_exit_has_exactly_one_call_site_and_pending_does_not_gate_it():
    """★★★ 人工仓的出场判断**只能有一个调用点**，且在 `_step` 里。

    这是 F2 的结构性保证，值得用源码断言而不是行为断言：

      改之前唯一的调用点在 `_step_pending` 里，而那里先过了
      `ts <= self._pending_5m_ts: return` 这道 5m 节流 ——
      于是 22 倍杠杆、止损 1.618% 的仓位，**每 5 分钟才判一次**止损
      与爆仓保护。生产跑的正是 `strategy=pending`。

    为什么不能用行为断言：走一遍 `_step` 需要十几个依赖（权益、
    feed、guard、db……），桩本身比被测逻辑更容易出错；而"存在几个
    调用点"是纯文本事实，用 `inspect` 数得准。

    变异测试会把调用点搬回 `_step_pending`、或给 `_step` 的判据加回
    `and not self._is_pending()` —— 两种都会让这条变红。
    """
    import inspect
    import re

    def _call_lines(fn):
        """真正**执行**的调用行（注释里提到函数名不算）。"""
        out = []
        for ln in inspect.getsource(fn).splitlines():
            s = ln.strip()
            if s.startswith("#"):
                continue
            if re.search(r"\bawait\s+self\._step_adopted_exit\(", s):
                out.append(s)
        return out

    calls_step = _call_lines(E.DecisionEngine._step)
    calls_pending = _call_lines(E.DecisionEngine._step_pending)

    assert len(calls_step) == 1, (
        "`_step` 里应有且只有一处人工仓出场调用，实际 %d 处：%r"
        % (len(calls_step), calls_step))
    assert not calls_pending, (
        "`_step_pending` 又在 `await` 人工仓出场了（%r）。它会落在 "
        "5m 节流之后，等于把止损判定降到每 5 分钟一次" % (calls_pending,))

    # ★ 光数调用点不够：判据里加一个 `and not self._is_pending()`
    #   就能让 `_step` 那唯一一个调用点在 pending 策略下**永不执行**，
    #   而且调用点数量不变、看起来完全合规。必须单独钉判据。
    lines = inspect.getsource(E.DecisionEngine._step).splitlines()
    idx = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("#"):
            continue
        if re.search(r"\bawait\s+self\._step_adopted_exit\(", s):
            idx = i
            break
    assert idx is not None, "没找到调用行"
    call_indent = len(lines[idx]) - len(lines[idx].lstrip())
    # 往上找**缩进比调用行浅**的 `if`：中间可能夹着 `try:` 与赋值，不能只看上一行。
    # （第一版只看紧邻一行，于是把 `_b15 = ...` 当成"没有判据"，
    #   测试自己先红了 —— 变异验证会因此得出假结论。）
    gate = ""
    for ln in reversed(lines[:idx]):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        ind = len(ln) - len(ln.lstrip())
        if s.startswith("if ") and ind < call_indent:
            gate = s
            break
    assert gate, "调用点上方找不到 `if` 判据"
    assert "_is_pending" not in gate, (
        "人工仓出场被 `_is_pending()` 排除了（判据：%r）—— "
        "生产跑的正是 strategy=pending，它会退回每 5 分钟判一次" % gate)
    assert "_is_adopted" in gate, (
        "调用点的判据里没有 `_is_adopted()`（判据：%r）—— 那会对空仓也跑一遍" % gate)


# =================================================== F9：陈旧的路由结果
def test_stale_route_result_is_cleared_before_routing():
    """★ 路由抛异常时，`_last_route` 不许留着**上一条 thesis** 的拒绝原因。

    路由层刻意不吞异常（见 order_router 模块注释），所以异常会冒到
    `_step_pending`。那里读 `_last_route` 写决策行 —— 如果它是上一条
    thesis 的拒绝结果，就会把别人的闸门名字写进这一条的"证据"里。
    一条张冠李戴的证据比没有证据更糟：排查会一路追错方向。
    """
    class _BoomRouter(object):
        async def route(self, intent, ctx):
            raise RuntimeError("路由炸了")

    class _Act(object):
        thesis_id = "t1"
        price = 61000.0
        order_type = "LIMIT"
        side = "long"
        reason = ""
        bar_ts = 1795400000000

    class _Thesis(object):
        order_price = 61000.0
        stop_price = 59894.81
        target_price = 0.0

    st = _engine()
    st._pending_router = lambda: _BoomRouter()
    # 上一次留下的"证据"：某条 thesis 被安全闸拦了
    st._last_route = types.SimpleNamespace(
        placed=False, rejection="无人值守闸：数据源 DOWN")

    with pytest.raises(RuntimeError):
        _run(E.DecisionEngine._pending_place(st, _Act(), _Thesis()))

    assert st._last_route is None, (
        "路由抛异常后 `_last_route` 还是上一条 thesis 的结果 —— "
        "上层会把别人的闸门写进这条决策的行")

