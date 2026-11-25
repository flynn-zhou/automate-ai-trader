# -*- coding: utf-8 -*-
"""挂单链「越权平仓」的回归测试 —— 2026-11-24 生产事故（真钱）。

事故经过（全部有库/日志证据）：
  用户 11-23 手工买入 0.032 BTC @ 60880，被实盘对账接管
  （`open_reason='实盘对账接管'`，按规则「接管仓不判策略出场」）。
  11-24 09:00:13 挂单链采集到一条新计划，`next_index=0` 于是从**行情缓冲
  最老那一根**（11-20 05:00）开始重放：立刻 ARMED → 历史上某根判成交 →
  11-20 20:00 那根判「观察线拒绝」→ 产出 close 动作 →
  09:00:14 拿**市价**平掉用户的真实持仓（61289.4），而决策价是 4 天前的
  61389.5。同一形态 11-21 已经发生过一次（trades id=27）。

  并且：那一轮计划**一张单都没挂出去**（币安 -4015，clientOrderId 36 字符
  超出「<36」限制），但状态照样被写成 ARMED —— 幽灵成交、幽灵平仓。

本文件钉住四件事，任何一条回退都必须红：
  1. clientOrderId 合法（<36 字符、全 ASCII、可复现、每 thesis 唯一）。
  2. 新 thesis 的起点是**它自己那根**，不是缓冲第 0 根（拒绝历史重放）。
  3. 挂单失败 → 动作不算已执行、状态退回 EXPECTED（不判成交）。
  4. 只平自己开的仓：接管仓整段跳过、非「挂单成交」的仓拒绝平。
"""
import asyncio
from types import SimpleNamespace

from ai_trader.trading import engine as E
from ai_trader.trading import thesis as T

# ---------------------------------------------------------------- 工具

def _run(coro):
    """跑协程并把 event loop 还原（见 test_pending_chain 里的同款说明）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _bars(n, start_ts=1795000000000, step=900000):
    """造 n 根 15m bar（只要 ts 就够，起点换算不看价格）。"""
    return [SimpleNamespace(ts=start_ts + i * step, o=61000.0, h=61100.0,
                            l=60900.0, c=61000.0) for i in range(n)]


class FakeDB(object):
    def __init__(self):
        self.risks = []
        self.trades = []
        self.closes = []

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, detail))

    def log_trade(self, t):
        self.trades.append(t)

    def close_trade(self, t):
        self.closes.append(t)

    def risk_kinds(self):
        return [k for k, _ in self.risks]


def _bind(st, *names):
    """把引擎的**真实方法**绑到 SimpleNamespace 桩上。

    ★ 为什么要绑而不是重写：这些测试要验的正是 `DecisionEngine` 的真实逻辑
      （归属校验、失败传播）。重写一份桩就等于测自己写的桩，什么都证明不了。
      用 `SimpleNamespace` + 绑定真实方法，既不需要构造完整引擎，
      又跑的是产品代码。
    """
    for n in names:
        raw = E.DecisionEngine.__dict__.get(n)   # ★ 必须从 __dict__ 取：
        #   `getattr(cls, name)` 会把 staticmethod 解成普通函数，判不出来。
        if isinstance(raw, staticmethod):
            setattr(st, n, raw.__func__)         # 静态方法不传 self
        else:
            setattr(st, n, lambda *a, _n=n, **kw:
                    getattr(E.DecisionEngine, _n)(st, *a, **kw))
    return st


# 引擎里这些方法在挂单链上会被互相调用，桩上必须都能找到真实实现
_ENGINE_METHODS = ("_is_adopted", "_pending_place", "_pending_close",
                   "_pending_cancel", "_pending_size", "_pending_is_dry",
                   "_pending_warn_once", "_to_pending_bars",
                   "_step_adopted_exit", "_adopted_close", "_adopted_bars",
                   "_adopted_anchor_ts", "_adopted_slot",
                   "_pending_thesis_brief", "_pending_start_index",
                   "_confirm_stop_price",
                   # BUS-A：下单与拦截搬进出站路由层后，`_pending_place`
                   # 只负责拼 EntryIntent，真正跑闸门/下单的是这四个。
                   # 不绑它们，桩上的 `_pending_place` 就调不到产品代码。
                   "_pending_router", "_gate_pending_unattended",
                   "_gate_pending_size", "_place_pending_order",
                   # MGN-2：可用保证金闸要先跳过（桩上没有 available_margin）。
                   "_gate_pending_margin", "_pending_need_margin",
                   "_reclaim_margin",
                   # BUS-D：人工仓出场改由 position_guard 判（P0+P1），
                   # `_step_adopted_exit` 现在要调这四个才能跑完。
                   "_guard_position_for_adopted", "_live_liq_price",
                   "_guard_config_for_observe", "_persist_adopted_stop",
                   "_bar_ts", "_bar_ohlcv",
                   # F7（2026-11-24 全链路审查）：观察期与人工仓统一走
                   # `_resolve_liq()` 选爆仓价来源，不再各写一套。
                   "_resolve_liq", "_stop_distance_pct",
                   # A1（2026-11-25 交易域审查）：人工仓在交易所侧挂
                   # reduceOnly 止损单，`_adopted_close`/`_step_adopted_exit`
                   # 都要调这三个才能跑完（桩上没有它们会 AttributeError）。
                   "_adopted_stop_state", "_adopted_stop_cid",
                   "_sync_adopted_stop_order", "_cancel_adopted_stop_order",
                   # SIG-PARAMS（2026-11-25）：游标锚点与旧行参数修复。
                   # `_step_pending` 现在每条预期都要调这两个才能跑完。
                   "_pending_plan_ts", "_pending_repair_params")


class FakePending(object):
    def __init__(self, fresh=True):
        self.applied = []
        self.written = []
        self.fresh = fresh

    def put_action(self, thesis_id, kind, state_after="", bar_ts=0,
                   price=0.0, payload=None):
        """幂等登记。返回 True 表示这条动作是新的（生产上是 `fresh`）。"""
        return self.fresh

    def mark_applied(self, thesis_id, kind, bar_ts):
        self.applied.append((thesis_id, kind, bar_ts))

    def upsert_thesis(self, row):
        self.written.append(row)

    def live(self, limit=200):
        return []


# ------------------------------------------------- 1. clientOrderId 合法性

def test_client_id_is_short_ascii_and_deterministic():
    """币安要求 clientOrderId **< 36 字符**，原来截到正好 36 → 每一单都被拒。"""
    tid = "下跌三引线假突破限价单|up|1795481700000|61178.1000"   # 47 字符 / 含中文
    cid = E.pending_client_id(tid)
    assert len(cid) < 36, "clientOrderId 必须严格小于 36 字符，得到 %d" % len(cid)
    assert len(cid.encode("utf-8")) < 36, "字节数也要小于 36"
    assert cid.isascii(), "中文 id 会让字节数爆掉"
    assert cid.startswith(E.PENDING_CLIENT_PREFIX)
    # 稳定性：幂等重放与撤单都靠「同一个 thesis 算出同一个 id」
    assert E.pending_client_id(tid) == cid


def test_client_id_differs_per_thesis():
    """不同 thesis 必须得到不同 id，否则两张单会互相覆盖。"""
    a = E.pending_client_id("形态|up|1795481700000|61178.1000")
    b = E.pending_client_id("形态|up|1795481700000|61178.1001")
    assert a != b


def test_client_id_survives_pathological_lengths():
    """超长中文形态名也不许破约束（这正是原来那版踩的坑）。"""
    for tid in ("短", "x" * 300, "形态名|up|%d|%d" % (1795481700000, 61178.1),
                ""):
        cid = E.pending_client_id(tid)
        assert len(cid) < 36 and cid.isascii()


def test_cancel_uses_the_same_id_as_place():
    """撤单必须能算出与挂单**逐字相同**的 id，否则撤不掉自己的单。"""
    calls = []

    class Ex(object):
        async def cancel_order(self, client_id="", order_id="", symbol=None):
            calls.append(client_id)
            return True

    st = SimpleNamespace(ex=Ex(), db=FakeDB())
    act = SimpleNamespace(thesis_id="abc形态|up|1|2", reason="x")
    _run(E.DecisionEngine._pending_cancel(st, act))
    assert calls == [E.pending_client_id("abc形态|up|1|2")]


# --------------------------------------- 2. 起点必须是计划自己那根（拒绝重放）

def test_start_index_is_the_plans_own_bar_not_the_oldest():
    """★ 事故根因：400 根缓冲里，新 thesis 的起点是第 0 根（4 天前）。"""
    b15 = _bars(400)
    last_ts = b15[-1].ts
    idx = E.DecisionEngine._pending_start_index(b15, last_ts)
    assert idx == 399, "新 thesis 起点必须是它自己那根（399），得到 %d" % idx
    assert idx != 0, "起点落在第 0 根 = 从行情缓冲最老一根重放（事故的成因）"


def test_start_index_matches_by_timestamp_not_by_position():
    b15 = _bars(50)
    want = b15[17].ts
    assert E.DecisionEngine._pending_start_index(b15, want) == 17


def test_start_index_never_replays_when_plan_predates_the_buffer():
    """计划比缓冲最老一根还旧 → 必须**拒绝重放**，而不是退回 0。

    退回 0 就是「把一条几万根之后才成立的计划扔进历史里跑」，
    也就是事故本身。这里选「本轮不动、留下 ERROR」。
    """
    b15 = _bars(400)
    too_old = b15[0].ts - 1
    idx = E.DecisionEngine._pending_start_index(b15, too_old)
    assert idx == len(b15), "应当跳过本轮（%d），而不是从 0 重放（得到 %d）" % (
        len(b15), idx)


def test_start_index_waits_when_plan_bar_not_yet_arrived():
    b15 = _bars(10)
    future = b15[-1].ts + 900000
    assert E.DecisionEngine._pending_start_index(b15, future) == len(b15)


def test_start_index_tolerates_degenerate_input():
    assert E.DecisionEngine._pending_start_index([], 123) == 0
    assert E.DecisionEngine._pending_start_index(_bars(5), 0) == 0
    assert E.DecisionEngine._pending_start_index(_bars(5), None) == 0


def test_stepping_from_the_plan_bar_cannot_reach_backwards():
    """把起点喂给 `T.step`，断言它一根历史 K 都不许走。

    这是事故的直接机制：以前 `next_index=0` 会让状态机从历史第一根开始推进，
    并在**同一轮**里走出「成交 + 出场」。起点正确之后，计划那根就是最后一根，
    本轮除了「开始存在」之外不该产出任何成交/出场动作。
    """
    b15 = _bars(400)
    from ai_trader.trading import pending_thesis_store as PTS
    th = T.Thesis(thesis_id="t1", kind="x", direction=T.UP,
                  order_price=61000.0, stop_price=60900.0,
                  target_price=61200.0, order_type=T.ORDER_LIMIT,
                  family=T.FAMILY_REVERSAL, surprise_pts=0.0)
    row = PTS.row_of(th, state=T.EXPECTED, next_index=0,
                     bar_ts=b15[-1].ts, ambiguous=False, note="")
    prog = PTS.progress_of(row)
    assert int(prog.get("next_index") or 0) == 0        # 生产落库就是这个值

    # 旧行为（起点 0）：走过 400 根历史
    res_old, newp_old = T.step(th, b15, T.LineBook(), progress=dict(prog))
    assert newp_old["next_index"] > 1, "对照组应当走过多根历史（复现旧行为）"

    # 修复后：起点 = 计划自己那根
    fixed = dict(prog)
    fixed["next_index"] = E.DecisionEngine._pending_start_index(b15, b15[-1].ts)
    res_new, newp_new = T.step(th, b15, T.LineBook(), progress=fixed)
    assert fixed["next_index"] == 399
    assert newp_new["next_index"] == 400, (
        "只应推进这一根，得到 %r" % (newp_new["next_index"],))


# ------------------------------------------- 3. 挂单失败不得落成「已挂单」

def _place_stub(place_ok):
    """一个只够跑 `_apply_pending_action` 的引擎桩。"""
    class Ex(object):
        async def place_order(self, side, qty, order_type="LIMIT",
                              price=None, client_id="", **kw):
            if place_ok:
                return SimpleNamespace(status="NEW"), ""
            return None, 'HTTP 400 {"code":-4015,"msg":"Client order id ..."}'

        async def market_order(self, *a, **kw):
            return SimpleNamespace(ok=True, price=61000.0), ""

        async def position(self):
            return {"qty": 0.0, "entry": 0.0}

    st = SimpleNamespace()
    st.ex = Ex()
    st.db = FakeDB()
    st.pending = FakePending()
    st.guard = None                     # 安全闸：本测试不验它
    st.tcfg = SimpleNamespace(symbol="BTCUSDT", strategy="pending",
                              qty_step=0.001, max_hold_bars=600,
                              leverage=22.0, margin_cap_pct=100.0)
    st.pos = None
    st._pending_dry = False             # 非 dry-run：真的要走下单分支
    _bind(st, *_ENGINE_METHODS)
    # BUS-D：挂单链现在也会经过 `_step_adopted_exit` → 守卫要这两个
    st._liq_distance_pct = lambda: 4.0455
    st._qty_step = lambda: 0.001
    st._liq_cache = None
    st._liq_src = ""
    st._sizing_params = lambda: None
    # 仓位计算绕开：本测试只关心「下单成败如何影响状态推进」
    # 真实签名是 `_pending_size(price, stop, order_type) -> (qty, why)`
    st._pending_size = lambda price, stop, otype: (0.01, "")
    return st


def _place_action():
    return SimpleNamespace(thesis_id="t1", kind="place_order", side="BUY",
                           price=61000.0, order_type="LIMIT")


def _place_thesis():
    """下单要读 `thesis_obj.order_price/stop_price`，必须给真实形状。"""
    return T.Thesis(thesis_id="t1", kind="x", direction=T.UP,
                    order_price=61000.0, stop_price=60900.0,
                    target_price=61200.0, order_type=T.ORDER_LIMIT,
                    family=T.FAMILY_REVERSAL, surprise_pts=0.0)


def test_place_failure_is_not_marked_applied():
    """★ 下单被拒 → 动作不能记成「已执行」，否则状态机会接着判成交。"""
    st = _place_stub(place_ok=False)
    ok = _run(E.DecisionEngine._apply_pending_action(
        st, _place_action(), _place_thesis(), 1795000000000))
    assert ok is False, "下单失败却报告成功"
    assert st.pending.applied == [], "没挂出去的单被记成已执行 → 幽灵成交的入口"
    assert "pending_place_failed" in st.db.risk_kinds()


def test_place_success_is_marked_applied():
    st = _place_stub(place_ok=True)
    ok = _run(E.DecisionEngine._apply_pending_action(
        st, _place_action(), _place_thesis(), 1795000000000))
    assert ok is True
    assert len(st.pending.applied) == 1
    assert st.pending.applied[0][1] == "place_order"


# --------------------------------- 4. 只平自己开的仓（接管仓整段跳过 + 归属校验）

def _close_stub(open_reason, qty=0.032):
    class Ex(object):
        async def market_order(self, side, q, reason="", reduce_only=False):
            # ★ 返回**单个** fill 对象（不是 (fill, err) 元组）：
            #   `_pending_close` 读的是 `fill.ok` / `fill.price`。
            return SimpleNamespace(ok=True, price=61289.4)

        async def mark_price(self):
            return 61289.4

    st = SimpleNamespace()
    st.ex = Ex()
    st.db = FakeDB()
    st.tcfg = SimpleNamespace(symbol="BTCUSDT", strategy="pending",
                              max_hold_bars=600)
    st.pos = {"side": "long", "qty": qty, "entry": 60880.0,
              "stop": 45660.0, "target": 62216.1, "entry_ts": 1795300000000,
              "open_reason": open_reason, "paper": 0}
    _bind(st, *_ENGINE_METHODS)
    return st


def _close_action():
    return SimpleNamespace(thesis_id="t1", kind="close", reason="observation_reject")


def test_close_refuses_adopted_manual_position():
    """★ 事故正文：接管来的人工仓被挂单链用市价平掉。"""
    from ai_trader.trade_db import ADOPT_REASON
    st = _close_stub(ADOPT_REASON)
    _run(E.DecisionEngine._pending_close(st, _close_action(), None))
    assert st.db.closes == [], "接管的人工仓被策略平掉了"
    assert st.pos is not None, "仓位不该被清掉"
    assert "pending_close_not_owned" in st.db.risk_kinds()


def test_close_refuses_other_reasons_too():
    """任何不是「挂单成交」的来源都拒绝：手单、别的策略、未知来源。"""
    for reason in ("", "手动开仓", "趋势策略", None):
        st = _close_stub(reason)
        _run(E.DecisionEngine._pending_close(st, _close_action(), None))
        assert st.db.closes == [], "open_reason=%r 的仓被平了" % (reason,)


def test_close_works_for_its_own_position():
    st = _close_stub(E.PENDING_FILL_REASON, qty=0.005)
    st.pos["entry"] = 61000.0
    _run(E.DecisionEngine._pending_close(st, _close_action(), None))
    assert len(st.db.closes) == 1, "自己开的仓反而平不掉"
    assert st.pos is None


def _st_for_step_pending(pos=None, rows=None, adopted=None):
    """搭一个够 `_step_pending` 走完全程的替身引擎。

    ★ 为什么需要它：接管仓那条路上，`_step_pending` 不再提前 return，
      它会继续走到链路自检、取线、推进每一条 live 预期。
      所以替身必须把沿途用到的东西都准备好（自检、kbar 视图、动作执行），
      否则测试会以「替身不全」为理由失败，掩盖真正的行为断言。
    """
    from ai_trader.trade_db import ADOPT_REASON
    st = SimpleNamespace()
    st.db = FakeDB()
    st.pending = FakePending()
    st.pending._rows = list(rows or [])
    st.pending.live = lambda limit=200: st.pending._rows
    st.pos = pos
    st.adopted_pos = adopted
    st._pending_5m_ts = 0
    st.tcfg = SimpleNamespace(symbol="BTCUSDT", strategy="pending",
                              max_hold_bars=600, trend_stop_pct=25.0)
    st.ex = SimpleNamespace()
    st.decisions_log = []
    st._bind_ready = False
    _bind(st, *_ENGINE_METHODS)
    st.pending_lines = None
    # BUS-D：接管仓出场现在走守卫，要这三个（爆仓距离 / 步长 / R 来源）
    st._liq_distance_pct = lambda: 4.0455
    st._qty_step = lambda: 0.001
    st._sizing_params = lambda: None
    st._liq_cache = None
    st._liq_src = ""
    st._pending_harvest = lambda b15, b5: None
    st._pending_kbar_view = lambda: None
    st._pending_is_dry = lambda: True
    st._log_decision = lambda *a, **kw: st.decisions_log.append((a, kw))

    async def _noop(*a, **kw):
        return None

    st._step_selfcheck = _noop
    st._applied_acts = []

    async def _apply(act, th, ts):
        st._applied_acts.append(str(getattr(act, "kind", "")))
        return True

    st._apply_pending_action = _apply
    return st


def _adopted_lot():
    """一条**健康的**接管仓（止损在可达区间内）。

    ★ 2026-11-24 BUS-D：原来这里用事故遗产 `stop=45660`（距入场 25%）。
      BUS-D 之前它"很安静"，原因是结构判据根本没跑（`_adopted_bars`
      按 `.ts` 取值、真实 Bar 没有该字段，恒返回空）；
      BUS-D 之后守卫一上来就算出「入场到爆仓 4.0455% / 止损宽度 25%
      = 0.16 倍 < 2.5」，于是正确地要求减仓 —— 这里的桩没有
      `market_order`，就炸了。

      但这些用例的题目是「有人工仓时不挂新单」，与出场无关。
      继续用 25% 止损会让它们被一个无关的出场动作打断，
      所以改用生产实际会写入的 1.618% 止损。
      事故遗产那条状态另有专门用例
      （`test_legacy_25pct_stop_now_gets_called_out_by_the_guard`）。
    """
    from ai_trader.trade_db import ADOPT_REASON
    return {"side": "long", "qty": 0.032, "entry": 60880.0,
            "stop": _ADOPT_STOP, "entry_ts": 1795390000000,
            "open_reason": ADOPT_REASON}


def test_step_pending_never_places_when_position_is_adopted():
    """★ 接管仓期间挂单链**一次都不许下单**（用户要求：有人工单时不挂）。

    行为已从「整段跳过」升级为「不挂单 + 管出场 + 决策照做」
    （见 `_step_adopted_exit` 与 `_step_pending` 里的 suppressed 分支），
    但「不挂单」这条必须原样保留。
    """
    st = _st_for_step_pending(adopted=_adopted_lot())
    # 价格远高于熔断价、线全空 -> 三条判据都不触发
    bars = {300000: [SimpleNamespace(open_time=1795400000000, open=61000.0,
                                     high=61100.0, low=60900.0, close=61050.0)],
            900000: _bars(4, start_ts=1795395000000)}
    _run(E.DecisionEngine._step_pending(st, bars, 61000.0, 100.0,
                                       1795400000000))
    assert st.pending.applied == [], "接管仓期间挂单链不许执行任何动作（尤其不许挂单）"
    assert st._applied_acts == [], "接管仓期间不许落下任何真实动作"
    assert st.adopted_pos is not None, "接管仓不该被平（三条判据都没触发）"
    # 5m 游标照常前移（那根 K 已经处理过了）
    assert st._pending_5m_ts == 1795400000000


def test_adopted_keeps_deciding_and_only_suppresses_the_order(monkeypatch):
    """★★ 用户 2026-11-24 的核心纠正：**有手工仓 ≠ 系统不决策**。

    「有手工仓时不挂新单，不代表系统不决策。决策行为要落库留痕，
      以便回溯，只是因为手工的存在，所以不真实下单而已。」
    「系统决策跟下不下单没一点关系，系统不停，决策不止。」

    所以本条锁三个断言：
      1. 状态机确实推出了 place_order（决策没有停）
      2. 但真实下单一次都没发出去（`_apply_pending_action` 没被调用）
      3. 决策落库了，并且写明「因为人工仓所以不下单」
    """
    from ai_trader.trading import pending_thesis_store as PTS

    th = T.Thesis(thesis_id="人为压住|up|1795400000000|61178.1", kind="x",
                  direction=T.UP, order_price=61000.0, stop_price=60900.0,
                  target_price=61200.0, order_type=T.ORDER_LIMIT,
                  family=T.FAMILY_REVERSAL, surprise_pts=0.0)
    row = PTS.row_of(th, state=T.EXPECTED, next_index=0,
                     bar_ts=1795400000000, ambiguous=False, note="")

    place = SimpleNamespace(kind="place_order", thesis_id=th.thesis_id,
                            reason="挂")

    class _Res(object):
        actions = [place]
        ambiguous = False
        state = T.EXPECTED

    newp = {"state": T.ARMED, "next_index": 7,
            "armed_at_bar": 1795400000000, "armed_ref_close": 61050.0}
    monkeypatch.setattr(T, "step", lambda *a, **kw: (_Res(), newp))

    st = _st_for_step_pending(adopted=_adopted_lot(), rows=[row])
    bars = {300000: [SimpleNamespace(open_time=1795400000000, open=61000.0,
                                     high=61100.0, low=60900.0, close=61050.0)],
            900000: _bars(4, start_ts=1795395000000)}
    _run(E.DecisionEngine._step_pending(st, bars, 61000.0, 100.0,
                                       1795400000000))

    assert st._applied_acts == [], "有手工仓时真实下单动作一个都不许执行"
    assert st.pending.applied == [], "更不许 mark_applied（否则会被当成已挂出）"
    # 状态必须退回 EXPECTED：绝不能落成 ARMED。
    # 状态机判成交只看 K 线，ARMED 会顺着价格推出 FILLED → 出场动作，
    # 而交易所那头没有这张单（11-24 的 15 条幽灵动作就是这么来的）。
    assert st.pending.written, "thesis 必须被写回（决策不能停）"
    w = st.pending.written[0]
    assert w["state"] == T.EXPECTED, "被压住时不许落 ARMED，得到 %r" % w["state"]
    assert w["state"] not in T.TERMINAL_STATES, (
        "也**不许**标终态 CANCELLED —— 人工仓平掉后这条决策还可能成立，"
        "状态机每根 bar 从当前 K 线重新推导，不会用到旧 K 线")
    # 决策必须落库，且能读出「为什么没下单」
    blob = repr(st.decisions_log)
    assert st.decisions_log, "被压住这件事必须写成决策，否则无法回溯"
    assert ("压制" in blob or "压住" in blob or "不下单" in blob), (
        "决策内容要说明「因为人工仓所以不下单」，实际：%s" % blob)
    # ★ A2：逐条记明「本应挂什么价、多少量、止损在哪」。
    #   只记一个计数等于没记 —— 回溯时看不出系统当时到底想做什么。
    assert "应挂" in blob, "决策里必须写出本应挂出的计划，实际：%s" % blob
    assert "止损" in blob and "只决策不下单" in blob, (
        "决策里必须写明止损位与「只决策不下单」的结论，实际：%s" % blob)


def test_every_suppressed_plan_gets_its_own_trace(monkeypatch):
    """★★ A2：多条计划同时被压住时，**每一条都要单独留痕**。

    这里钉的是一个很容易复发的陷阱：`_log_decision` 按 `bar_time` 去重
    （同一根 3s/5m 桶只留第一条）。如果在循环里逐条写决策，
    第二条起会被静默丢掉 —— 看板上只会出现一条，用户会以为系统
    那一根只想过一个计划。真正的做法是循环内只登记、循环外汇总写一行，
    一次性把所有计划列全。
    """
    from ai_trader.trading import pending_thesis_store as PTS

    def _mk(tid, price):
        th = T.Thesis(thesis_id=tid, kind="x", direction=T.UP,
                      order_price=price, stop_price=price * 0.98,
                      target_price=price * 1.04, order_type=T.ORDER_LIMIT,
                      family=T.FAMILY_REVERSAL, surprise_pts=0.0)
        return th, PTS.row_of(th, state=T.EXPECTED, next_index=0,
                              bar_ts=1795400000000, ambiguous=False, note="")

    # ★ 挂单价必须**在市价下方**：这三条是 `up`+LIMIT 的「等回踩」买单，
    #   市价 61000 之下才成立。原来的 61100/61200/61300 在市价**上方**，
    #   等于「限价买单挂在卖价之上」= 一挂出就按市价成交，
    #   2026-11-25 新增的「入场价已被穿过」规则会先把它作废 ——
    #   而本用例要验的是「人工仓压制逐条留痕」，不该被那条规则打断。
    th_a, row_a = _mk("甲|up|1795400000000|60900.0", 60900.0)
    th_b, row_b = _mk("乙|up|1795400000000|60800.0", 60800.0)
    th_c, row_c = _mk("丙|up|1795400000000|60700.0", 60700.0)

    mapd = {tid: th for tid, th in ((th_a.thesis_id, th_a), (th_b.thesis_id, th_b),
                                    (th_c.thesis_id, th_c))}

    real_thesis_of = PTS.thesis_of
    monkeypatch.setattr(PTS, "thesis_of", lambda row: mapd[row["thesis_id"]])

    class _Res(object):
        ambiguous = False
        state = T.EXPECTED

        def __init__(self, tid):
            self.actions = [SimpleNamespace(kind="place_order", thesis_id=tid)]

    newp = {"state": T.ARMED, "next_index": 7,
            "armed_at_bar": 1795400000000, "armed_ref_close": 61050.0}
    monkeypatch.setattr(
        T, "step",
        lambda th, *a, **kw: (_Res(th.thesis_id), dict(newp)))

    st = _st_for_step_pending(adopted=_adopted_lot(),
                              rows=[row_a, row_b, row_c])
    bars = {300000: [SimpleNamespace(open_time=1795400000000, open=61000.0,
                                     high=61100.0, low=60900.0, close=61050.0)],
            900000: _bars(4, start_ts=1795395000000)}
    _run(E.DecisionEngine._step_pending(st, bars, 61000.0, 100.0,
                                       1795400000000))

    assert st._applied_acts == [], "三条计划一条都不许真下单"
    assert len(st.pending.written) == 3, (
        "三条计划都要被写回（决策不能停），实际 %d 条" % len(st.pending.written))
    for w in st.pending.written:
        assert w["state"] == T.EXPECTED
    blob = repr(st.decisions_log)
    assert "60900" in blob and "60800" in blob and "60700" in blob, (
        "三条不同的挂单价都必须出现在决策里（否则被去重丢掉了），实际：%s"
        % blob)
    assert blob.count("应挂") >= 3, (
        "每条计划都要有「应挂」说明，实际 %d 处：%s"
        % (blob.count("应挂"), blob))
    assert "3 条预期在管" in blob, "汇总要写清本轮在管几条，实际：%s" % blob


def test_step_pending_is_not_blocked_when_flat():
    """空仓不跳过（否则挂单策略永远不会开第一单）。"""
    st = SimpleNamespace()
    st.db = FakeDB()
    st.pending = FakePending()
    st.pos = None
    st._pending_5m_ts = 0
    _bind(st, *_ENGINE_METHODS)
    st._pending_is_dry = lambda: True
    st._pending_kbar_view = lambda: None
    st._pending_harvest = lambda b15, b5: None
    st._log_decision = lambda *a, **kw: None

    async def _noop():
        return None

    st._step_selfcheck = _noop
    bars = {300000: [SimpleNamespace(open_time=1795400000000, open=61000.0,
                                     high=61100.0, low=60900.0, close=61050.0)],
            900000: _bars(3)}
    # live() 返回空 → 走「没有 live 预期」分支，说明没有被 adopted 闸拦掉
    _run(E.DecisionEngine._step_pending(st, bars, 61000.0, 100.0,
                                       1795400000000))
    assert "pending_skipped_adopted" not in st.db.risk_kinds()


# ============================================================ 5. 接管仓出场管理
#
# 用户 2026-11-24 明确要求：「系统帮我管理手工单的出场我是满意的，只是要正确」。
# 「正确」的可实现定义：**早于接管时刻的 K 线一律不参与判断** —— 这正是
# 09:00 那次事故的反面（那时用 11-20 的 K 线判 11-24 的仓）。
#
# ★★ BUS-D 起，接管仓的判据改成守卫的 P0+P1（用户裁决：人工仓不需要结构
#    失效判断），所以夹具里的止损必须用**生产实际会写入的值**：
#        1.618% = 爆仓距离 4.0455% / 安全倍数 2.5
#        entry 60880 -> stop 59894.81
#    继续用事故遗产 45660（25%）会让守卫正确地判 P0-b「结构性缓冲 0.16 < 2.5」
#    并减仓 93.5% —— 那是守卫在指出"这个止损永远不可能触发"，
#    而不是守卫乱动。测"没触发时不动"必须用可达的止损。
_ADOPT_STOP = 59894.81          # 60880 * (1 - 1.61818%)
_ADOPT_LIQ = 58416.73           # 60880 * (1 - 4.04545%)
_ADOPT_STOP_SHORT = 61865.19    # 60880 * (1 + 1.61818%)
_ADOPT_LIQ_SHORT = 63342.68     # 60880 * (1 + 4.04545%)


def _adopted_st(price=61000.0, qty=0.032, entry=60880.0, stop=_ADOPT_STOP,
                entry_ts=1795390000000, managed=True, liq_price=0.0):
    """造一个「持有接管仓」的引擎桩。"""
    from ai_trader.trade_db import ADOPT_REASON
    calls = []

    class Ex(object):
        async def market_order(self, side, q, reason="", reduce_only=False):
            calls.append((side, q, reduce_only))
            # 手续费给个非零值：平仓成本必须真的记账（这个项目历史上
            # 「手续费漏记」出过事，见 _pending_close 的 fee=0.0 既存缺陷）。
            return SimpleNamespace(ok=True, price=price, fee=0.9806)

        async def mark_price(self):
            return price

        # BUS-D：守卫要真实爆仓价。默认给 0 表示"交易所没给"，
        # 走配置推算分支；需要测真实值的用例显式传 liq_price。
        async def position(self):
            return {"qty": qty, "entry": entry, "side": "long",
                    "liq_price": liq_price}

    st = SimpleNamespace()
    st.ex = Ex()
    st.db = FakeDB()
    st.tcfg = SimpleNamespace(symbol="BTCUSDT", strategy="pending",
                              max_hold_bars=600)
    st.pos = {"side": "long", "qty": qty, "entry": entry, "stop": stop,
              "target": 0.0, "entry_ts": entry_ts,
              "open_reason": ADOPT_REASON, "paper": 0}
    st.calls = calls
    _bind(st, *_ENGINE_METHODS)
    st._log_decision = lambda *a, **kw: None
    st.pending_lines = None
    st._liq_cache = None
    st._liq_src = ""
    st._sizing_params = lambda: None
    # 22 倍杠杆 + 0.5% 维持保证金的市场：爆仓距离 4.0455%
    st._liq_distance_pct = lambda: 4.0455
    st._qty_step = lambda: 0.001
    return st


def test_adopted_anchor_ts_comes_from_entry():
    st = _adopted_st(entry_ts=1795390000000)
    assert E.DecisionEngine._adopted_anchor_ts(st.pos) == 1795390000000


def test_adopted_anchor_ts_never_falls_back_to_zero():
    """锚点绝不退回 0 —— 退回 0 就等于允许重放整个缓冲，也就是事故本身。"""
    st = _adopted_st(entry_ts=0)
    got = E.DecisionEngine._adopted_anchor_ts(st.pos)
    assert got > 0, "锚点退到 0 会让状态机从最老一根 K 线开始重放"


def test_adopted_bars_exclude_everything_before_adoption():
    """★ 核心不变量：事故的反面。

    接管发生在 11-24 03:40（1795390000000 附近）。11-20 的 K 线
    （那根 `armed_ref_close=61962.2` 的来源）**不许参与任何判断**。
    """
    b15 = _bars(400)                                # 400 根，最早那根 ≈ 4 天前
    anchor = b15[300].ts                            # 假设这根是接管时刻
    kept = E.DecisionEngine._adopted_bars(b15, anchor)
    assert len(kept) == 100, "应当只保留接管之后的 100 根，得到 %d" % len(kept)
    assert all(int(b.ts) >= anchor for b in kept)
    assert kept[-1].ts == b15[-1].ts                # 最新那根仍在


def test_adopted_bars_works_on_the_real_feed_bar():
    """★★ 用**真实** `feed.Bar` 复查上一条 —— 这是 2026-11-24 抓到的既存缺陷。

    事故的深层原因不只是"用旧 K 线判断"，而是：`_adopted_bars` 用
    `getattr(b, "ts", 0)` 过滤，而真实 `feed.Bar` **只有 `open_time`**。
    于是每根都取到 0，过滤结果恒空，`_step_adopted_exit` 每次都在
    「还没有接管之后的已收盘 15m」就 return ——
    接管仓的两条结构判据**在生产上从未执行过一次**。

    上面的测试之所以没抓到它，是因为夹具 `_bars()` 造的是带 `.ts` 的
    假对象。假对象和真对象字段名不同，测试就绿了 ——
    这个项目栽过 8 次的同一个错：同一事实两个来源。

    所以这条测试**必须用真实的 `feed.Bar`**，不能用夹具。
    """
    from ai_trader.market.agg import Bar

    bars = [Bar(interval_ms=900000, open_time=1795000000000 + i * 900000,
                open=61000.0, high=61100.0, low=60900.0, close=61000.0,
                volume=1.0) for i in range(400)]
    anchor = bars[300].open_time
    kept = E.DecisionEngine._adopted_bars(bars, anchor)
    assert len(kept) == 100, (
        "真实 feed.Bar 上过滤结果为空 —— `_adopted_bars` 又退回按 .ts 取值了")
    assert E.DecisionEngine._bar_ts(kept[0]) == anchor


def test_bar_ts_accepts_all_three_real_shapes():
    """`_bar_ts` 要能吃下三种真实存在的 bar 形状，别只认一种。"""
    from ai_trader.market.agg import Bar

    real = Bar(interval_ms=900000, open_time=1795000000000, open=1.0,
               high=1.0, low=1.0, close=1.0, volume=1.0)
    assert E.DecisionEngine._bar_ts(real) == 1795000000000

    ts_obj = SimpleNamespace(ts=1795000009000)
    assert E.DecisionEngine._bar_ts(ts_obj) == 1795000009000

    assert E.DecisionEngine._bar_ts((1795000018000, 1, 1, 1, 1)) == 1795000018000

    assert E.DecisionEngine._bar_ts(SimpleNamespace()) == 0


def test_adopted_exit_holds_when_nothing_triggers():
    """没到止损、缓冲健康 → 不动仓位，且留一行决策痕。"""
    st = _adopted_st(price=61000.0)
    b15 = _bars(6, start_ts=1795395000000)
    _run(E.DecisionEngine._step_adopted_exit(
        st, {3000: [], 900000: b15}, b15, 61000.0,
        1795400000000))
    assert st.calls == [], "判据都没触发却平了仓"
    assert st.pos is not None


def test_legacy_25pct_stop_now_gets_called_out_by_the_guard():
    """★ 事故遗产 25% 止损（45660）不再是"静默无效"，守卫会指出来。

    这一条是 11-24 那个报错的直接答案：止损 45660 距入场 25%、
    而爆仓价距入场只有 4.0455%，所以止损**永远不可能触发**。
    BUS-D 之前它是一句写着但不执行的话（结构判据没接线，见上）；
    现在守卫用「入场到爆仓 / 止损宽度 = 0.16 倍」直接量化出来，
    并要求把仓位减到止损有意义为止。

    ★ 注意这不是"守卫乱砍仓"：真实修法是**把止损改到 1.618%**
      （接管时已经这么写了，见 `_stop_distance_pct`）。只有当库里
      还挂着旧的 25% 止损时，守卫才会走到这条减仓分支。
    """
    st = _adopted_st(price=61000.0, stop=45660.0)
    b15 = _bars(6, start_ts=1795395000000)
    _run(E.DecisionEngine._step_adopted_exit(
        st, {3000: [], 900000: b15}, b15, 61000.0,
        1795400000000))
    assert st.calls, "25% 止损（远超爆仓价）居然被判为健康仓位"
    assert "P0" in st.db.closes[0]["close_reason"]


def test_adopted_exit_fires_circuit_stop():
    """★ 止损（P1）：接管仓的止损以前**根本没接线**（`price <= p["stop"]`
    只在 `_step_trend` 里，而 pending 永远不进那条链）。

    价格取 59000：已跌破止损 59894.81，但仍在爆仓价 58416.73 之上 ——
    这才是"止损正常触发"的区间（跌破爆仓价就该走 P0 紧急了）。
    """
    st = _adopted_st(price=59000.0)
    b15 = _bars(6, start_ts=1795395000000)
    _run(E.DecisionEngine._step_adopted_exit(
        st, {3000: [], 900000: b15}, b15, 59000.0,
        1795400000000))
    assert st.calls == [("SELL", 0.032, True)], "止损没触发平仓"
    assert st.pos is None
    assert len(st.db.closes) == 1
    assert "接管仓出场" in st.db.closes[0]["close_reason"]
    assert "P1" in st.db.closes[0]["close_reason"]


def test_adopted_exit_does_not_fire_stop_on_short_in_the_wrong_direction():
    """做空时只有「价格涨过 stop」才算止损（镜像不许写反）。

    ★ 空单的止损必须在入场价**上方**（61865.19）。
      原来这条测试给 stop=45660（在入场价下方），那是一个
      "开仓即已盈利"的无效止损 —— 用它测方向会把符号错误测成通过。
    """
    from ai_trader.trade_db import ADOPT_REASON
    st = _adopted_st(price=60000.0, stop=_ADOPT_STOP_SHORT,
                     liq_price=_ADOPT_LIQ_SHORT)
    st.pos["side"] = "short"
    st.pos["open_reason"] = ADOPT_REASON
    b15 = _bars(6, start_ts=1795395000000)
    _run(E.DecisionEngine._step_adopted_exit(
        st, {3000: [], 900000: b15}, b15, 60000.0,
        1795400000000))
    assert st.calls == [], "做空时价格下跌不该触发止损"


def test_adopted_exit_ignores_stale_local_price():
    """本地价触发止损但交易所价没触发 → 判定行情陈旧，不平仓。"""
    st = _adopted_st(price=61000.0)        # mark_price 返回 61000
    b15 = _bars(6, start_ts=1795395000000)
    _run(E.DecisionEngine._step_adopted_exit(
        st, {3000: [], 900000: b15}, b15, 40000.0,
        1795400000000))                 # 本地价 40000 < 止损
    assert st.calls == [], "陈旧价假触发把健康仓位卖掉了"
    assert "stale_price" in st.db.risk_kinds()


def test_adopted_close_books_with_its_own_reason():
    """接管仓的平仓理由必须与「挂单策略出场」区分开，否则战绩归因又混了。"""
    st = _adopted_st(price=61500.0)
    _run(E.DecisionEngine._adopted_close(
        st, 0.032, "long", 60880.0, "观察线拒绝（ema20=61900）"))
    c = st.db.closes[0]
    assert c["close_reason"].startswith("接管仓出场")
    assert "挂单策略出场" not in c["close_reason"]
    assert c["open_reason"], "关账必须带上原本的 open_reason（人工仓归因）"
    # ★ 手续费必须记真实值：记 0 会让这笔的盈亏系统性偏乐观
    assert abs(c["fee"] - 0.9806) < 1e-9, "平仓手续费漏记（记成 %r）" % (c["fee"],)
    assert abs(c["pnl_net"] - (c["pnl_gross"] - 0.9806)) < 1e-9, "净盈亏没扣手续费"
    assert st.pos is None
    assert "adopted_closed" in st.db.risk_kinds()


def test_pending_close_still_refuses_adopted_even_though_adopted_close_exists():
    """★ 两条授权路径必须分开：`_pending_close` 不许因为「反正有人管」就放行。"""
    st = _adopted_st()
    act = SimpleNamespace(thesis_id="t1", kind="close", reason="observation_reject")
    _run(E.DecisionEngine._pending_close(st, act, None))
    assert st.db.closes == [], "挂单链又把接管仓平了"
    assert "pending_close_not_owned" in st.db.risk_kinds()


def test_suppression_leaves_no_armed_ghost_behind():
    """被人工仓压住之后，**不许在 pending 库里留下一条 ARMED 幽灵**。

    这是 11-24 那 15 条幽灵动作的结构性防线：状态机判成交只看 K 线，
    它不会问交易所「我那张单还在吗」。所以只要有一条 ARMED 记录存在
    （而交易所其实没单），后面就会顺着价格推出 FILLED → 出场。
    本条用一个「本应挂单、实际被压住」的场景验证状态没有被推成 ARMED。
    """
    # 该行为已由 test_adopted_keeps_deciding_and_only_suppresses_the_order
    # 完整覆盖（那里直接断言 written[0]["state"] == EXPECTED）。
    # 这里只留一条显式的"终态不许出现"断言，防止以后有人改回去。
    from ai_trader.trading import pending_thesis_store as PTS
    from ai_trader.trading import thesis as TT

    th = TT.Thesis(thesis_id="t1", kind="x", direction=TT.UP,
                   order_price=61000.0, stop_price=60900.0,
                   target_price=61200.0, order_type=TT.ORDER_LIMIT,
                   family=TT.FAMILY_REVERSAL, surprise_pts=0.0)
    row = PTS.row_of(th, state=TT.EXPECTED, next_index=0,
                     bar_ts=1795400000000, ambiguous=False, note="")
    assert "EXPECTED" in row.values() or row.get("state") == TT.EXPECTED
    assert TT.ARMED not in TT.TERMINAL_STATES
    assert TT.EXPECTED not in TT.TERMINAL_STATES, (
        "EXPECTED 不是终态：人工仓平掉后这条决策必须还能继续推进")


def test_adopted_exit_no_longer_fires_on_observation_reject():
    """★ BUS-D：观察线判据已按用户裁决删除，即使形态完全命中也不出场。

    用户原话：「这样的话，你人工仓还一定要基于结构失效的判断吗？」

    观察线和结构失效同属 P2（都要求"入场时盯的是哪条线/哪个结构"），
    人工仓没有入场结构，所以两条一起删。这条测试与
    `test_adopted_exit_no_longer_uses_observation_lines` 互补：
    那条用"一调就炸"证明没被调用，这条用**真实的命中形态**证明
    即使调用条件成立也不会出场 —— 防止以后有人"顺手接回去"。

    ★ 注意止损用的是可达值：如果用事故遗产 45660，
      守卫会因 P0-b（缓冲 0.16 倍）而减仓，测出来的就不是本条意图了。
    """
    from ai_trader.trading import thesis as TT

    st = _adopted_st(price=61300.0, entry=60880.0)
    anchor = 1795390000000
    bars = [
        TT_close_bar(anchor + 900000, 60900.0),
        TT_close_bar(anchor + 1800000, 60950.0),
    ]
    # 典型的"穿透后收回"：影线到 61100，收盘 60950 在线 61050 之下
    bars[-1] = SimpleNamespace(ts=anchor + 1800000, o=60900.0, h=61100.0,
                               l=60850.0, c=60950.0)

    book = TT.LineBook()
    for key in TT.OBSERVATION_KEYS:
        book.add_point(key, TT.LinePoint(61050.0, anchor, anchor))

    _run(E.DecisionEngine._step_adopted_exit(
        st, {3000: [], 900000: bars}, bars, 61300.0,
        anchor + 2700000))
    assert st.calls == [], (
        "观察线判据已删，却仍因它出场 —— 要么没删干净，要么把别的判据误接过来")
    assert st.pos is not None


def TT_close_bar(ts, close):
    return SimpleNamespace(ts=ts, o=close, h=close, l=close, c=close)


def test_incident_1124_replay_no_longer_closes_on_the_old_bar():
    """★ 事故回放：同样一根 11-20 20:00 的 K 线，现在不许再参与判断。

    这根 K 线确实能让 `observation_reject` 成立（h > ema20 > c）。
    事故正是这么发生的：它被当成「当前的拒绝」，用市价平在 61289.4。

    现在判据本身已按用户裁决删除，所以它连"参与判断的资格"都没有了；
    而锚点过滤（`_adopted_bars`）仍然在，作为第二道防线：
    即使将来有人重新接入某条结构判据，早于接管时刻的 K 线也进不来。
    """
    from ai_trader.trading import thesis as TT

    ADOPT_TS = 1795338000000          # 11-24 03:40（接管时刻）
    OLD_BAR_TS = 1795242600000        # 11-20 20:00（事故决策那根）
    assert OLD_BAR_TS < ADOPT_TS

    old_bar = SimpleNamespace(ts=OLD_BAR_TS, o=61900.0, h=61819.70,
                              l=61100.0, c=61389.50)
    # 接管之后的两根：都没穿透 ema20，收盘也在下方靠住
    b1 = SimpleNamespace(ts=ADOPT_TS + 900000, o=60900.0, h=60960.0,
                         l=60800.0, c=60920.0)
    b2 = SimpleNamespace(ts=ADOPT_TS + 1800000, o=60920.0, h=60980.0,
                         l=60850.0, c=60900.0)

    st = _adopted_st(price=61289.4, entry=60880.0, entry_ts=ADOPT_TS)
    book = TT.LineBook()
    # ema20@15m = 61789.83：那根老 K 的 high 穿过它、收盘回到它下面
    for key in TT.OBSERVATION_KEYS:
        book.add_point(key, TT.LinePoint(61789.83, ADOPT_TS, ADOPT_TS))

    b15 = [old_bar, b1, b2]
    _run(E.DecisionEngine._step_adopted_exit(
        st, {3000: [], 900000: b15}, b15, 61289.4,
        ADOPT_TS + 2700000))

    assert st.calls == [], (
        "事故复现：旧 K 线（11-20 20:00）又被拿来判出场了 —— 这正是要修的东西")
    assert st.pos is not None, "本就该继续持有的仓被平掉了"


def test_incident_1124_anchor_filter_still_blocks_old_bars():
    """锚点过滤这道防线必须还在（判据删了不等于防线可以拆）。

    直接验证 `_adopted_bars`：早于接管时刻的 bar 一律进不来。
    事故的机制是"旧 K 线被当成现在的判断依据"，
    所以这条不变量比任何单条判据都更底层。
    """
    ADOPT_TS = 1795338000000
    old = SimpleNamespace(ts=1795242600000, o=1, h=1, l=1, c=1)
    fresh = SimpleNamespace(ts=ADOPT_TS + 900000, o=1, h=1, l=1, c=1)
    kept = E.DecisionEngine._adopted_bars([old, fresh], ADOPT_TS)
    assert kept == [fresh], "锚点过滤失效：旧 K 线能进判断了"


def test_incident_1124_stop_still_fires_on_a_current_bar():
    """反证：不是把出场关掉，而是换成**不可否认的**数学判据。

    用户的问题「人工仓还一定要基于结构失效的判断吗？」答案是不要。
    但"删掉结构判据"不等于"永远不出场" —— 止损照样会平仓，
    这条就是那个反证。否则「修好」就变成了「永远不卖」，是另一种坏。
    """
    ADOPT_TS = 1795338000000
    st = _adopted_st(price=59000.0, entry=60880.0, entry_ts=ADOPT_TS)
    b15 = _bars(6, start_ts=ADOPT_TS + 900000)
    _run(E.DecisionEngine._step_adopted_exit(
        st, {3000: [], 900000: b15}, b15, 59000.0,
        ADOPT_TS + 2700000))
    assert st.calls == [("SELL", 0.032, True)], (
        "止损在接管之后仍然必须能平仓 —— 否则等于把出场整个关掉")
    assert "P1" in st.db.closes[0]["close_reason"]


def test_incident_1124_same_bar_shape_no_longer_closes():
    """同一根"穿透 ema20 又收回"的接管后 K 线，现在也不出场了。

    ★ 这是删判据的**直接后果**，必须显式钉住，否则将来无法分辨
      "判据被误删" 和 "判据被有意删除"。
      替代它的反证见 `test_incident_1124_stop_still_fires_on_a_current_bar`
      —— 出场能力没丢，只是换成了不依赖入场结构的数学判据。
    """
    from ai_trader.trading import thesis as TT

    ADOPT_TS = 1795338000000
    bad = SimpleNamespace(ts=ADOPT_TS + 900000, o=61600.0, h=61850.0,
                          l=61500.0, c=61650.0)
    st = _adopted_st(price=61650.0, entry=60880.0, entry_ts=ADOPT_TS)
    book = TT.LineBook()
    for key in TT.OBSERVATION_KEYS:
        book.add_point(key, TT.LinePoint(61789.83, ADOPT_TS, ADOPT_TS))
    b15 = [bad]
    _run(E.DecisionEngine._step_adopted_exit(
        st, {3000: [], 900000: b15}, b15, 61650.0,
        ADOPT_TS + 1800000))
    assert st.calls == [], (
        "观察线判据已删，这根 K 线却仍导致出场 —— 没删干净")

