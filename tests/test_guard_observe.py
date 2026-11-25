# -*- coding: utf-8 -*-
"""BUS-C：`position_guard.decide()` 接入系统仓出场（观察期只记录不执行）。

背景
----
`position_guard.py` 是一套**完整的持仓管理系统**：P0 保命减仓 → P1 止损
→ P2 结构离场 → P3 分批止盈 → P4 加仓 → P5 持有，外加 `check_invariants`
（含"止损必须落在爆仓价安全一侧"）。它有 93 条测试、逻辑齐全，
但**生产零调用** —— 引擎里 grep `check_invariants` / `GuardPosition` / `liq_price`
全是 0 命中。它是一套写好了、测过了、从没运行过的规则。

BUS-D 要把它接线成唯一的出场出口。接线之前必须先回答：
**它和现状差多少？** 这就是本步（观察期）的全部目的。

本文件钉的六件事
----------------
1. ★★★ **观察者绝不产生动作** —— 这是本步最大的风险。
   一个"顺手"的观察者会变成第二个下单引擎，而它的手还没被验证过。
2. 守卫真的被调用了（不是又造一个孤儿模块）
3. 分歧写进 `decisions(action="guard_compare")`
4. 15m 节流（同一根 15m 不重复跑，否则决策表被灌满）
5. 无持仓时什么都不做（用户原话：「没有持仓这些都没意义」）
6. 两个 0（不分批止盈 / 不加仓）是用户裁决，不是漏配置
"""
from __future__ import annotations

import types

import pytest

from ai_trader.trading import engine as E
from ai_trader.trading import kline_intent as ki
from ai_trader.trading import position_guard as PG


# ------------------------------------------------------------------ 脚手架
def _bars(ts0, n, step_ms, px=61000.0, drift=0.0):
    out = []
    for i in range(n):
        p = px + drift * i
        t = ts0 + i * step_ms
        out.append(types.SimpleNamespace(
            open_time=t, ts=t, open=p, high=p + 30, low=p - 30, close=p,
            volume=1.0, interval_ms=step_ms))
    return out


class _Db(object):
    def __init__(self):
        self.decisions = []
        self.risks = []

    def log_decision(self, d):
        self.decisions.append(d)

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, detail))


def _engine(pos=None, liq_pct=4.0455, risk_u=10.1):
    """只够跑 `_guard_observe` 的引擎桩（不构造完整 DecisionEngine）。"""
    st = types.SimpleNamespace()
    st.db = _Db()
    st.pos = pos
    st.adopted_pos = None
    st.tcfg = types.SimpleNamespace(symbol="BTCUSDT", strategy="pending")
    st.decisions = 0
    st._last_bar_time = 0
    st.last_decision = None
    st._guard_obs_ts = 0
    st._guard_obs_last = None

    st._liq_distance_pct = lambda: liq_pct
    st._sizing_params = lambda: None
    st._guard_config_for_observe = lambda: PG.GuardConfig(
        risk_per_trade=risk_u, max_adds=0, max_partials=0)

    # _log_decision 用**真实实现**（它要落 decisions，正是被测对象），
    # 但 feed.hub 是它唯一的外部依赖，给个假的。
    class _Hub(object):
        def __init__(self):
            self.published = []

        def publish(self, msg):
            self.published.append(msg)

    st.feed = types.SimpleNamespace(hub=_Hub())

    for name in ("_log_decision", "_guard_observe",
                 "_guard_position_for_observe",
                 # F7（2026-11-24 审查）：观察期与人工仓统一用 `_resolve_liq`
                 # 选取爆仓价来源，观察者要能调到它。
                 "_resolve_liq",
                 # BUS-D：`_guard_observe` 现在用 `_bar_ts` / `_bar_ohlcv`
                 # 取时间与价量（真实 feed.Bar 只有 open_time，没有 ts）
                 "_bar_ts", "_bar_ohlcv"):
        raw = E.DecisionEngine.__dict__[name]
        if isinstance(raw, staticmethod):
            setattr(st, name, raw.__func__)
        else:
            def _bound(*a, _n=name, **kw):
                return getattr(E.DecisionEngine, _n)(st, *a, **kw)
            _bound.__name__ = name
            setattr(st, name, _bound)
    return st


def _pos(side="long", entry=61000.0, qty=0.03, stop=60200.0, target=64000.0):
    return {"side": side, "entry": entry, "qty": qty, "stop": stop,
            "target": target, "bars": 5, "best": entry, "worst": entry,
            "open_reason": "挂单成交", "fee": 1.0}


def _bars_by_iv(ts15=1795400100000, px=61000.0, drift=0.0):
    return {900000: _bars(ts15 - 59 * 900000, 60, 900000, px, drift)}


# ============================================ 1. ★★★ 绝不产生动作（最重要）
def test_observer_never_touches_the_position():
    """观察者必须对 `self.pos` **零改动**。

    这条是本步的安全底线：观察期唯一允许的副作用是一行决策记录。
    变异测试会把 `_guard_observe` 改成顺手 `self.pos["stop"] = ...`，
    这条必须立刻变红。
    """
    p = _pos()
    before = dict(p)
    st = _engine(pos=p)
    E.DecisionEngine._guard_observe(st, _bars_by_iv(px=59000.0), 59000.0,
                                    1795400100000)
    assert p == before, "观察者改了持仓：%r -> %r" % (before, p)


def test_observer_never_calls_any_trade_api():
    """观察者不许调用 `_close` / 任何下单接口。"""
    st = _engine(pos=_pos())

    async def _boom(*a, **kw):
        raise AssertionError("观察者调用了平仓/下单！")

    for name in ("_close", "_adopted_close", "_pending_close", "_open",
                 "place_order", "market_order"):
        setattr(st, name, _boom)
        setattr(st.ex if hasattr(st, "ex") else st, name, _boom, )if False else None
    # 直接断言桩上没有 ex（观察者拿不到执行器）
    assert not hasattr(st, "ex"), "观察者不该持有执行器"

    E.DecisionEngine._guard_observe(st, _bars_by_iv(px=59000.0), 59000.0,
                                    1795400100000)
    assert st.db.decisions, "应当写出了一条对照决策"


# ======================================== 2. 守卫真的被调用（防孤儿）
def test_guard_decide_is_actually_called(monkeypatch):
    """`position_guard.decide` 必须被真正调用 —— 防"又一个孤儿模块"。"""
    called = {}
    real = PG.decide

    def _spy(pos, price, cfg, kline=None):
        called["pos"] = pos
        called["price"] = price
        called["cfg"] = cfg
        called["kline"] = kline
        return real(pos, price, cfg, kline=kline)

    monkeypatch.setattr(PG, "decide", _spy)
    st = _engine(pos=_pos())
    E.DecisionEngine._guard_observe(st, _bars_by_iv(), 61000.0, 1795400100000)

    assert "pos" in called, "decide 没被调用 —— 又造了一个孤儿模块"
    gp = called["pos"]
    assert isinstance(gp, PG.GuardPosition)
    assert gp.direction == "long"
    assert gp.avg_entry == pytest.approx(61000.0)
    assert gp.qty == pytest.approx(0.03)
    assert called["price"] == pytest.approx(61000.0)
    # K 线读数必须喂进去：P1 的恐慌插针与 P2 的结构离场都靠它
    assert isinstance(called["kline"], ki.KlineRead)


def test_guard_receives_a_kline_read():
    """`KlineRead` 必须真的算出来并传进去（P2/P1 的唯一输入）。"""
    st = _engine(pos=_pos())
    E.DecisionEngine._guard_observe(st, _bars_by_iv(), 61000.0, 1795400100000)
    d = st.db.decisions[0]
    assert d["features"]["kline"] is not None
    assert "intent" in d["features"]["kline"]
    assert "atr" in d["features"]["kline"]


# ============================ 3. 两个 0 是用户裁决，不是漏配置
def test_observer_disables_partials_and_adds_per_user_ruling():
    """用户 2026-11-24：「不要分批止盈」「不要加仓」→ max_partials=0, max_adds=0。"""
    from ai_trader.trading.sizing import SizingConfig, derive
    # 走真实配置推导（不是桩），确认参数真的被写死成 0
    st = _engine()
    st._sizing_params = lambda: {"sizing.capital": 100.0,
                                 "sizing.risk_pct": 10.1,
                                 "sizing.leverage": 22.0}
    cfg = E.DecisionEngine._guard_config_for_observe(st)
    assert cfg.max_partials == 0, "用户明确说了不要分批止盈"
    assert cfg.max_adds == 0, "用户明确说了不要加仓"
    assert cfg.safety_multiple == pytest.approx(2.5)
    # R 必须来自 sizing.derive()，不能是硬编码
    expect = derive(SizingConfig(capital=100.0, risk_pct=10.1,
                                 leverage=22.0)).risk_per_trade
    assert cfg.risk_per_trade == pytest.approx(expect)


# ================================ 4. 分歧写进 guard_compare
def test_divergence_is_written_as_guard_compare():
    """守卫要出场而引擎还持有时，必须写一条 guard_compare 决策。"""
    # 价格跌破止损 60200 → 守卫必判 P1 EXIT_STOP
    st = _engine(pos=_pos(stop=60200.0))
    E.DecisionEngine._guard_observe(st, _bars_by_iv(px=60000.0), 60000.0,
                                    1795400100000)
    assert st.db.decisions, "没有写决策"
    d = st.db.decisions[0]
    assert d["action"] == "guard_compare"
    assert d["signal"] == "guard_compare"
    assert "守卫观察" in d["reason"]
    assert PG.EXIT_STOP in d["reason"], (
        "价格已越过止损，守卫应判 EXIT_STOP，实际：%s" % d["reason"])
    # 分歧要进 gates 列（看板据此着色）
    assert d["gates"] and d["gates"][0]["ok"] is False


def test_agreement_is_marked_when_both_hold():
    """守卫说 HOLD 时，决策行必须标明「双方一致」，不能也算成分歧。

    ★ 止损用 600 点（0.98%），缓冲 = 4.0455/0.984 = 4.1 倍 > 2.5，P0 不触发。
      用 1000 点会触发 P0-b 减仓（见下面那条刀锋测试），那是真实行为不是 bug。
    """
    st = _engine(pos=_pos(entry=61000.0, stop=60400.0))
    E.DecisionEngine._guard_observe(st, _bars_by_iv(), 61000.0, 1795400100000)
    d = st.db.decisions[0]
    assert PG.HOLD in d["reason"]
    assert "双方一致" in d["reason"]
    assert d["gates"][0]["ok"] is True


def test_p0_b_reduce_fires_when_stop_sits_at_the_sizing_cap():
    """★★ 观察期发现的一个真实刀锋：`max_stop_distance_pct` 与
    `safety_multiple` 被设计成**刚好相等**，于是"止损正好顶到上限"的仓位
    结构性缓冲正好 2.50 倍 —— P0-b 判据是 `< 2.5`，任何一点滑点、
    一点取整、一点更好/更差的成交价，都会让它掉到线下并触发**减仓**。

    数值：爆仓距离 4.0455%，上限 = 4.0455/2.5 = 1.6182%，
    止损取 1.639%（略超上限）→ 缓冲 4.0455/1.639 = 2.47 < 2.50 → REDUCE。

    这不是 bug，是 BUS-D 必须带走的事实：接线之前要么把 safety_multiple
    留出余量，要么接受"刚开仓就被要求减一点"。观察期存在的意义正是
    先把这类刀锋暴露出来，而不是等它上线后自己发现。
    """
    st = _engine(pos=_pos(entry=61000.0, stop=60000.0))   # 1000 点 = 1.639%
    gp = E.DecisionEngine._guard_position_for_observe(st, 61000.0)
    # 先确认几何：缓冲确实 2.47 倍
    assert gp.structural_buffer() == pytest.approx(2.468, abs=0.01)
    assert gp.structural_buffer() < 2.5

    E.DecisionEngine._guard_observe(st, _bars_by_iv(), 61000.0, 1795400100000)
    d = st.db.decisions[0]
    assert PG.REDUCE in d["reason"], (
        "止损越过上限时守卫应当要求减仓，实际：%s" % d["reason"])
    assert "P0" in d["reason"]


def test_no_reduce_when_stop_is_comfortably_inside_the_cap():
    """反向对照：止损留出余量时 P0-b 不许触发（否则"总是减仓"毫无意义）。"""
    st = _engine(pos=_pos(entry=61000.0, stop=60400.0))   # 600 点 = 0.98%
    gp = E.DecisionEngine._guard_position_for_observe(st, 61000.0)
    assert gp.structural_buffer() > 2.5
    E.DecisionEngine._guard_observe(st, _bars_by_iv(), 61000.0, 1795400100000)
    assert PG.REDUCE not in st.db.decisions[0]["reason"]


def test_liq_price_is_on_the_losing_side():
    """爆仓价必须在**亏损侧**：多单在下方，空单在上方。

    这条防的是符号写反——符号反了会让 `liq_buffer` 变成一个巨大的正数，
    守卫于是永远认为"离爆仓还远"，P0 保命整条失效。
    """
    st = _engine(pos=_pos(side="long", entry=61000.0))
    gp = E.DecisionEngine._guard_position_for_observe(st, 61000.0)
    assert gp.liq_price < gp.avg_entry, "多单爆仓价必须在入场价下方"
    assert gp.liq_price == pytest.approx(61000.0 * (1 - 0.040455), rel=1e-9)

    st2 = _engine(pos=_pos(side="short", entry=61000.0, stop=62000.0,
                           target=58000.0))
    gp2 = E.DecisionEngine._guard_position_for_observe(st2, 61000.0)
    assert gp2.liq_price > gp2.avg_entry, "空单爆仓价必须在入场价上方"


def test_initial_risk_is_money_not_price():
    """`initial_risk` 是**金额**（U），不是价格距离。

    P3 的 R 倍数全靠它。写成价格距离会让「+1R」变成 +1 元，
    分批止盈的位置错得离谱（历史缺陷：减仓后用错 qty，标注 +1R 实际只有 0.5R）。
    """
    st = _engine(pos=_pos(entry=61000.0, stop=60200.0, qty=0.03))
    gp = E.DecisionEngine._guard_position_for_observe(st, 61000.0)
    assert gp.initial_risk == pytest.approx(abs(61000.0 - 60200.0) * 0.03)


# ================================================ 5. 15m 节流
def test_same_15m_bar_is_not_observed_twice():
    """同一根 15m 只跑一次。每 3s 跑一次会把决策表灌满，
    而且是用同一根 K 线重复问同一个问题。"""
    st = _engine(pos=_pos())
    bars = _bars_by_iv(ts15=1795400100000)
    E.DecisionEngine._guard_observe(st, bars, 61000.0, 1795400100000)
    n1 = len(st.db.decisions)
    # 同一根 15m，再调 5 次
    for _ in range(5):
        E.DecisionEngine._guard_observe(st, bars, 61000.0, 1795400100000)
    assert len(st.db.decisions) == n1, "同一根 15m 被重复观察了"


def test_next_15m_bar_is_observed():
    """下一根 15m 必须重新观察（反向对照，防"节流写成了永久跳过"）。"""
    st = _engine(pos=_pos())
    E.DecisionEngine._guard_observe(st, _bars_by_iv(ts15=1795400100000),
                                    61000.0, 1795400100000)
    n1 = len(st.db.decisions)
    E.DecisionEngine._guard_observe(st, _bars_by_iv(ts15=1795401000000),
                                    61000.0, 1795401000000)
    assert len(st.db.decisions) == n1 + 1


# ====================================== 6. 无持仓什么都不做
def test_flat_does_nothing():
    """空仓时不观察、不写决策 —— 用户原话：「没有持仓这些都没意义」。"""
    st = _engine(pos=None)
    E.DecisionEngine._guard_observe(st, _bars_by_iv(), 61000.0, 1795400100000)
    assert st.db.decisions == []


def test_flat_without_prior_observation_writes_nothing():
    """空仓且没有过观察 → 不写任何东西（不该凭空生成一行"仓位已不在"）。"""
    st = _engine(pos=None)
    st._guard_obs_last = None
    E.DecisionEngine._guard_observe(st, _bars_by_iv(), 61000.0, 1795400100000)
    assert st.db.decisions == []


def test_vanished_position_after_hold_is_reported_as_divergence():
    """方向二：上次 guard 说 HOLD、这次仓位没了 → 引擎先平了仓，是分歧。

    ★ 不覆盖这个方向，观察报告就只会告诉我们"守卫想平而引擎没平"，
      另一半真相（策略比守卫更急、频繁进出）永远看不见。
    """
    st = _engine(pos=None)
    st._guard_obs_last = {"guard_action": PG.HOLD, "ts": 1, "price": 61000.0}
    E.DecisionEngine._guard_observe(st, _bars_by_iv(ts15=1795401000000),
                                    61000.0, 1795401000000)
    assert st.db.decisions, "仓位消失后必须补一条对照"
    assert "分歧" in st.db.decisions[0]["reason"]
    assert "HOLD" in st.db.decisions[0]["reason"]
    assert st._guard_obs_last is None, "补判后要清空，否则会重复报"


def test_vanished_position_after_exit_judgement_is_agreement():
    """上次 guard 判了要出场、这次仓位没了 → 双方一致。"""
    st = _engine(pos=None)
    st._guard_obs_last = {"guard_action": PG.EXIT_STOP, "ts": 1, "price": 60000.0}
    E.DecisionEngine._guard_observe(st, _bars_by_iv(ts15=1795401000000),
                                    60000.0, 1795401000000)
    assert "一致" in st.db.decisions[0]["reason"]


# ============================================ 7. 坏输入不许炸
def test_no_15m_bars_is_a_noop():
    st = _engine(pos=_pos())
    E.DecisionEngine._guard_observe(st, {300000: _bars(0, 3, 3000)}, 61000.0,
                                    1795400100000)
    assert st.db.decisions == []


def test_unusable_liq_distance_is_recorded_as_a_skip_not_silence():
    """算不出爆仓距离时**不调守卫**，但要留一句"为什么跳过"。

    ★ 这条原来是 `assert st.db.decisions == []`（要求完全静默）。
      2026-11-24 全链路审查（F4）把它改掉了：静默的后果是观察期
      看起来"一切正常"，而实际上这一根根本没有对照记录 ——
      报表上少掉的样本不会自己说明少掉的原因。
      现在必须落一条 `guard_compare` 且理由里写清楚是"无止损"还是
      "拿不到爆仓价"，同时**不许**有任何守卫动作（gate 为空）。
    """
    st = _engine(pos=_pos(), liq_pct=0.0)
    E.DecisionEngine._guard_observe(st, _bars_by_iv(), 61000.0, 1795400100000)
    assert len(st.db.decisions) == 1, "跳过必须留痕，不能静默"
    row = st.db.decisions[0]
    assert row["action"] == "guard_compare"
    assert "跳过" in row["reason"]
    assert row["gates"] == [], "跳过时不许写任何守卫判断"


def test_zero_qty_position_is_skipped():
    st = _engine(pos=_pos(qty=0.0))
    assert E.DecisionEngine._guard_position_for_observe(st, 61000.0) is None
