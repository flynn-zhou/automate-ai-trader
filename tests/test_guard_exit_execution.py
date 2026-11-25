# -*- coding: utf-8 -*-
"""BUS-D：出场执行权交给 `position_guard`，人工仓走 P0+P1（无 P2）。

三件事，每件都有独立的失败模式：

1. **执行侧手数门槛（用户裁决 ②）**
   `position_guard` 会算出数学上正确、交易上不可执行的减仓量。
   观察期实测：止损顶到上限时它要求平掉 `8.99e-17` BTC，而步长是 0.001。
   不设门槛就会每 15 分钟发一张注定失败的指令。

2. **人工仓删除结构判据**
   原「结构失效」用 `invalidation_verdict(family=FAMILY_TREND)`，
   原「观察线穿透」用 `observation_reject` —— 两者都要求"入场时建立的结构"，
   人工仓没有。用户问过「你人工仓还一定要基于结构失效的判断吗？」，
   答案是不要，所以两条都删。

3. **真实爆仓价**
   推算公式忽略逐仓/全仓、维持保证金档位、浮盈。
   11-24 实测同一笔仓：推算 4.0455% / 逐仓 4.15% / 全仓 5.48%。
"""
from __future__ import annotations

import types

import pytest

from ai_trader.trading import engine as E
from ai_trader.trading import position_guard as PG
from ai_trader.trading.exit_executor import (SKIP_NOTHING, SKIP_TOO_SMALL,
                                             EXIT_STOP, HOLD, MOVE_STOP,
                                             REDUCE, describe, plan_exit,
                                             round_down_to_step)

STEP = 0.001


# ==================================================== 1. 手数门槛（核心）
def _dec(action, close_qty=None, reason="测试", new_stop=None):
    return PG.GuardDecision(action, reason, {}, new_stop=new_stop,
                            close_qty=close_qty)


def test_tiny_reduce_is_rejected_as_hold():
    """★ 用户裁决 ② 的核心：减仓量取整后不足一个步长 → HOLD。

    这正是观察期实测的病例：8.99e-17 BTC vs 步长 0.001。
    """
    plan = plan_exit(_dec(REDUCE, close_qty=8.99e-17), qty=0.03, step=STEP)
    assert plan.action == HOLD
    assert plan.why_not == SKIP_TOO_SMALL
    assert plan.qty == 0.0


def test_reduce_just_below_one_step_is_rejected():
    """差一点点就到步长，也必须拒绝（宁可不动，不可发注定失败的指令）。"""
    plan = plan_exit(_dec(REDUCE, close_qty=0.0009), qty=0.03, step=STEP)
    assert plan.action == HOLD and plan.why_not == SKIP_TOO_SMALL


def test_reduce_at_exactly_one_step_is_allowed():
    """正好一个步长必须放行 —— 反向对照，防门槛写成"永远不动"。"""
    plan = plan_exit(_dec(REDUCE, close_qty=0.001), qty=0.03, step=STEP)
    assert plan.action == REDUCE
    assert plan.qty == pytest.approx(0.001)


def test_reduce_is_rounded_down_not_up():
    """减仓量向下取整：超卖在合约上等于反向开仓。"""
    plan = plan_exit(_dec(REDUCE, close_qty=0.0029), qty=0.03, step=STEP)
    assert plan.qty == pytest.approx(0.002)


def test_full_exit_ignores_the_step_gate():
    """全部离场是**逃生**，不能被步长门槛拦住。

    ★ 这条和"减仓不足就 HOLD"不是一回事：止损离场是保命动作，
      数量取整后仍会被交易接口再取整一次；而"因为数量太小所以不卖"
      会让止损彻底失效 —— 那是最坏的一种安全。
    """
    plan = plan_exit(_dec(EXIT_STOP), qty=0.03, step=STEP)
    assert plan.action == EXIT_STOP
    assert plan.qty == pytest.approx(0.03)


def test_exit_qty_never_exceeds_position():
    """平仓量不许超过持仓（超卖 = 反向开仓）。"""
    plan = plan_exit(_dec(REDUCE, close_qty=999.0), qty=0.03, step=STEP)
    assert plan.qty <= 0.03 + 1e-12


def test_reduce_covering_whole_position_becomes_full_exit():
    """取整后正好等于全部持仓 → 走全平语义，不走"减仓"。

    否则会用减仓接口平掉 100%，部分成交/手续费归因都会歪。
    """
    plan = plan_exit(_dec(REDUCE, close_qty=0.03), qty=0.03, step=STEP)
    assert plan.action == EXIT_STOP


def test_unknown_step_does_not_block_escapes():
    """未知步长（配置缺失）时**放行**，不拦死出场。

    理由：拦掉所有出场会让止损彻底失效；"因为不知道能不能卖所以不卖"
    是最坏的安全。
    """
    plan = plan_exit(_dec(REDUCE, close_qty=0.002), qty=0.03, step=0.0)
    assert plan.action == REDUCE
    plan2 = plan_exit(_dec(EXIT_STOP), qty=0.03, step=0.0)
    assert plan2.action == EXIT_STOP


def test_hold_decision_is_a_noop():
    plan = plan_exit(_dec(HOLD, reason="P5 继续持有"), qty=0.03, step=STEP)
    assert plan.is_noop
    assert plan.why_not == SKIP_NOTHING


def test_add_is_refused_by_the_execution_layer():
    """用户裁决「不要加仓」：即使有人把 max_adds 调开，执行层也拒绝。"""
    plan = plan_exit(_dec(PG.ADD, reason="P4 加仓"), qty=0.03, step=STEP)
    assert plan.action == HOLD, "执行层必须拒绝加仓"


def test_move_stop_is_passed_through():
    plan = plan_exit(_dec(MOVE_STOP, new_stop=61500.0), qty=0.03, step=STEP)
    assert plan.action == MOVE_STOP
    assert plan.new_stop == pytest.approx(61500.0)


def test_move_stop_without_a_price_is_a_noop():
    plan = plan_exit(_dec(MOVE_STOP, new_stop=0.0), qty=0.03, step=STEP)
    assert plan.is_noop


def test_reduce_without_qty_is_a_noop():
    plan = plan_exit(_dec(REDUCE, close_qty=None), qty=0.03, step=STEP)
    assert plan.is_noop


def test_describe_explains_a_rejected_action():
    """执行层否决时必须说清"守卫要动、但动不了"，不能伪装成守卫的判断。"""
    plan = plan_exit(_dec(REDUCE, close_qty=1e-17), qty=0.03, step=STEP)
    txt = describe(plan)
    assert "不足一个交易所步长" in txt
    assert "REDUCE" in txt


def test_round_down_to_step_is_monotone():
    for q in (0.0, 0.0001, 0.001, 0.0019, 0.0025, 1.0):
        r = round_down_to_step(q, STEP)
        assert 0 <= r <= max(q, 0) + 1e-12


# ==================================== 2. 人工仓不再用结构判据出场
# ★ 夹具数值必须落在生产算路的合规区间，否则测的是守卫的 P0 而不是被测逻辑：
#   entry 61000、爆仓距离 4.0455% → liq ≈ 58532。
#   止损必须在 1.618% 以内（=> stop > 60013），否则守卫**正确地**判 P0-b 减仓，
#   测试就会误报成"守卫乱平仓"。
#   取 stop=60100（1.475%）→ 结构性缓冲 4.0455/1.475 = 2.74 倍 > 2.5 → HOLD。
_STOP_SAFE = 60100.0
_LIQ_SAFE = 58532.0


def _adopted_engine(bars_n=30, liq=_LIQ_SAFE, stop=_STOP_SAFE, entry=61000.0,
                    side="long", qty=0.03, price=61000.0):
    """只够跑 `_step_adopted_exit` 的桩，绑定真实实现。"""
    calls = {"closed": [], "decisions": [], "risks": [], "position": 0}

    class _Db(object):
        def log_risk(self, kind, detail=""):
            calls["risks"].append((kind, detail))

        def log_decision(self, d):
            calls["decisions"].append(d)

    class _Ex(object):
        async def position(self):
            calls["position"] += 1
            return {"qty": qty if side == "long" else -qty, "entry": entry,
                    "side": side, "liq_price": liq}

    st = types.SimpleNamespace()
    st.db = _Db()
    st.ex = _Ex()
    st.tcfg = types.SimpleNamespace(symbol="BTCUSDT", strategy="pending")
    st.pos = None
    st.adopted_pos = {"side": side, "qty": qty, "entry": entry, "stop": stop,
                      "target": 0.0, "bars": 1, "best": entry, "worst": entry,
                      "open_reason": __import__("ai_trader.trade_db",
                                                fromlist=["ADOPT_REASON"]
                                                ).ADOPT_REASON,
                      "entry_ts": 1}
    st._liq_cache = None
    st._liq_src = ""

    async def _confirm(p):
        return p
    st._confirm_stop_price = _confirm
    st._qty_step = lambda: STEP
    st._sizing_params = lambda: None
    st._guard_config_for_observe = lambda: PG.GuardConfig(
        risk_per_trade=10.0, max_adds=0, max_partials=0)
    st._liq_distance_pct = lambda: 4.0455

    async def _ac(q, sd, en, reason):
        calls["closed"].append({"qty": q, "side": sd, "entry": en,
                                "reason": reason})
    st._adopted_close = _ac

    for name in ("_step_adopted_exit", "_adopted_slot", "_adopted_anchor_ts",
                 "_adopted_bars", "_guard_position_for_adopted",
                 "_guard_config_for_observe", "_live_liq_price",
                 "_persist_adopted_stop", "_log_decision",
                 # F7（2026-11-24 全链路审查）：爆仓价来源统一由
                 # `_resolve_liq()` 解析（观察期与人工仓同一个函数）。
                 # 这里绑真实实现，测试才能验证"真实值优先 / 推算兜底 /
                 # 方向不对拒绝"这三条。
                 "_resolve_liq",
                 # BUS-D：时间与价量必须走这两个取值函数
                 # （真实 feed.Bar 只有 open_time / open/high/low/close）
                 "_bar_ts", "_bar_ohlcv",
                 # A1（2026-11-25 交易域审查）：持有期间要在交易所侧
                 # 维持一张 reduceOnly 止损单。
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

    st.feed = types.SimpleNamespace(hub=types.SimpleNamespace(
        publish=lambda m: None))
    st.decisions = 0
    st._last_bar_time = 0
    st.last_decision = None

    b15 = []
    for i in range(bars_n):
        t = 1795000000000 + i * 900000
        px = 61000.0
        b15.append(types.SimpleNamespace(open_time=t, ts=t, open=px,
                                         high=px + 60, low=px - 60, close=px,
                                         volume=1.0, interval_mm=900000))
    return st, b15, calls


def _run(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_adopted_exit_no_longer_uses_structure_invalidation():
    """★★ 结构失效判据必须**不再被调用**。

    做法：把 `invalidation_verdict` 换成"一调就炸"，
    再让价格离止损很远（结构判据若还在就会触发平仓）。
    只要它被调用了，这条测试就会红。
    """
    from ai_trader.trading import thesis as T

    def _boom(*a, **kw):
        raise AssertionError("人工仓仍在调用结构失效判据（FAMILY_TREND）！")

    saved = T.invalidation_verdict
    T.invalidation_verdict = _boom
    try:
        st, b15, calls = _adopted_engine(price=61000.0, stop=_STOP_SAFE)
        _run(E.DecisionEngine._step_adopted_exit(
            st, {}, b15, 61000.0, 1795027000000))
    finally:
        T.invalidation_verdict = saved
    assert calls["closed"] == [], "价格未到止损，不该平仓"


def test_adopted_exit_no_longer_uses_observation_lines():
    """观察线判据同样必须不再调用（同属 P2 结构类）。"""
    from ai_trader.trading import thesis as T

    def _boom(*a, **kw):
        raise AssertionError("人工仓仍在调用观察线判据！")

    saved = T.observation_reject
    T.observation_reject = _boom
    try:
        st, b15, calls = _adopted_engine(price=61000.0, stop=_STOP_SAFE)
        _run(E.DecisionEngine._step_adopted_exit(
            st, {}, b15, 61000.0, 1795027000000))
    finally:
        T.observation_reject = saved
    assert calls["closed"] == []


def test_adopted_exit_still_fires_on_stop_breach():
    """删掉结构判据不等于删掉止损：价格越过止损必须平仓（P1 仍然工作）。"""
    st, b15, calls = _adopted_engine(price=60000.0, stop=_STOP_SAFE)
    _run(E.DecisionEngine._step_adopted_exit(
            st, {}, b15, 60000.0, 1795027000000))
    assert calls["closed"], "价格跌破止损却没平仓 —— P1 被破坏了"
    assert calls["closed"][0]["qty"] == pytest.approx(0.03)
    assert "P1" in calls["closed"][0]["reason"]


def test_adopted_exit_short_fires_on_stop_breach():
    """空单方向的镜像必须同样工作（符号写反会让止损永不触发）。"""
    st, b15, calls = _adopted_engine(side="short", entry=61000.0,
                                     stop=61900.0, liq=63468.0, price=62000.0)
    _run(E.DecisionEngine._step_adopted_exit(
            st, {}, b15, 62000.0, 1795027000000))
    assert calls["closed"], "空单价格涨破止损却没平仓"
    assert "P1" in calls["closed"][0]["reason"]


def test_adopted_exit_holds_when_price_is_far_from_stop():
    """离止损很远时不许平仓（反向对照）。"""
    st, b15, calls = _adopted_engine(price=61000.0, stop=_STOP_SAFE)
    _run(E.DecisionEngine._step_adopted_exit(
            st, {}, b15, 61000.0, 1795027000000))
    assert calls["closed"] == []


# ================================ 3. 真实爆仓价优先
def test_real_liquidation_price_is_preferred():
    """交易所给的爆仓价必须优先于配置推算值。"""
    st, b15, calls = _adopted_engine(entry=61000.0, liq=58600.0)
    gp = _run(E.DecisionEngine._guard_position_for_adopted(
        st, st.adopted_pos, 61000.0))
    assert gp is not None
    assert gp.liq_price == pytest.approx(58600.0)
    assert st._liq_src == "交易所真实"


def test_derived_liq_used_when_exchange_gives_nothing():
    """交易所不给爆仓价时退回推算值，并标明来源。"""
    st, b15, calls = _adopted_engine(entry=61000.0, liq=0.0)
    gp = _run(E.DecisionEngine._guard_position_for_adopted(
        st, st.adopted_pos, 61000.0))
    assert gp is not None
    assert st._liq_src == "配置推算"
    assert gp.liq_price == pytest.approx(61000.0 * (1 - 0.040455), rel=1e-9)


def test_liq_on_the_wrong_side_is_rejected():
    """交易所返回一个在盈利侧的爆仓价 → 必须拒绝并退回推算。

    符号/字段写反时最容易出的就是这个：多单爆仓价跑到上方，
    `liq_buffer` 会变成巨大正数，P0 保命整条静默失效。
    """
    st, b15, calls = _adopted_engine(entry=61000.0, liq=63000.0)  # 多单却在上方
    gp = _run(E.DecisionEngine._guard_position_for_adopted(
        st, st.adopted_pos, 61000.0))
    assert gp is not None
    assert gp.liq_price < 61000.0, "错误的爆仓价必须被拒绝"
    assert st._liq_src == "配置推算"


def test_liq_price_is_cached():
    """爆仓价要缓存 —— `_step` 每 3 秒一次，不能每 3 秒打一次交易所。"""
    st, b15, calls = _adopted_engine()
    _run(E.DecisionEngine._guard_position_for_adopted(st, st.adopted_pos, 61000.0))
    n1 = calls["position"]
    for _ in range(5):
        _run(E.DecisionEngine._guard_position_for_adopted(
            st, st.adopted_pos, 61000.0))
    assert calls["position"] == n1, "缓存没生效，每轮都在问交易所"


def test_adopted_kline_is_read_without_range_state():
    """★★ 结构类意图在这条路径上**不可能产生** —— 这是"删掉 P2"的机械保证。

    `kline_intent.read_kline()` 判「衰竭 / 派发 / 突破 / 延续」的**前提**
    是传入 `range_state`（要 `direction` 和边界 `p`）。人工仓这条路径
    调用时不传，所以 `intent` 只可能是 `none`。

    这条测试的价值：只断言"没调用 `invalidation_verdict` / `observation_reject`"
    是不够的 —— 将来有人换一种方式把结构结论接回来（比如直接用
    `read_kline` 的 intent）就绕过了那两个断言。钉住"不传 range_state"，
    就钉住了"结构意图在这里根本不可能存在"。

    （这条来自变异测试：我试着让 `intent == "exhaustion"` 时出场，
      结果测试全绿 —— 因为 exhaustion 在这条路径上不可达。
      不可达这个事实本身必须被测试钉住，否则它只是我的口头推断。）
    """
    from ai_trader.trading import kline_intent as ki

    seen = {}
    real = ki.read_kline

    def _spy(bars, i=None, range_state=None):
        seen["range_state"] = range_state
        seen["n"] = len(bars)
        return real(bars, i=i, range_state=range_state)

    ki.read_kline = _spy
    try:
        st, b15, calls = _adopted_engine(price=61000.0)
        _run(E.DecisionEngine._step_adopted_exit(
            st, {}, b15, 61000.0, 1795027000000))
    finally:
        ki.read_kline = real

    assert "range_state" in seen, "K 线根本没被读 —— 那 P1 的插针延迟也不会工作"
    assert seen["range_state"] is None, (
        "人工仓的 K 线读数开始带 range_state 了 —— 结构意图又变得可达")
    assert seen["n"] > 0


def test_panic_flush_bar_delays_the_stop_but_never_exits_by_itself():
    """恐慌插针只允许"推迟一次止损"，不许自己成为出场理由。

    造一根典型的空头陷阱：长下影 + 放量 + 收盘回到上半部。
    位置在止损之上（没触发止损），所以结论必须是 HOLD ——
    若它导致平仓，说明 K 线读数被当成了出场信号。
    """
    st, b15, calls = _adopted_engine(price=61000.0)
    # 把最后一根改成恐慌插针形态：下影极长、放量、收盘在上半部
    last = b15[-1]
    last.low = 60000.0
    last.high = 61100.0
    last.open = 61050.0
    last.close = 61050.0
    last.volume = 50.0        # 前几根都是 1.0，构成放量
    _run(E.DecisionEngine._step_adopted_exit(
            st, {}, b15, 61000.0, 1795027000000))
    assert calls["closed"] == [], "恐慌插针 K 线被当成了出场理由"


def test_max_partials_zero_disables_p3():
    """用户裁决「不分批止盈」→ max_partials=0，P3 永不触发。"""
    cfg = PG.GuardConfig(risk_per_trade=10.0, max_adds=0, max_partials=0)
    gp = PG.GuardPosition(symbol="BTCUSDT", direction="long", avg_entry=61000.0,
                          qty=0.03, stop_price=60000.0, liq_price=58500.0,
                          initial_risk=30.0, target_price=62500.0)
    d = PG.decide(gp, 62700.0, cfg)          # 已远超目标
    assert d.action != PG.PARTIAL_TP, "关掉分批止盈后 P3 不该触发"
