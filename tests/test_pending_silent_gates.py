# -*- coding: utf-8 -*-
"""挂单链路的三处静默缺陷（2026-11-25 交易域审查）。

三条都是「生产上规则根本没生效」，而且**都不报错**：

1. `_pending_harvest` 手工拼 `Thesis`，漏了 `horizon_bars` /
   `stale_distance` / `surprise_pts`，全取 0 → 三条规则静默关闭。
   实测生产库 22/22 行都是 0。
2. `next_index` 把 `len(b15)` 这个哨兵当成合法游标落了库（deque maxlen=400），
   而 `if not next_index` 只对 0 重算 → `start_at == len(b15)` →
   `range(len,len)` 一根都不遍历 → 状态机**永久冻结**。
   实测那 4 条 EXPECTED 全是 `next_index=400`、`armed_at_bar=None`、动作零行。
3. `pending_thesis_store.set_exit()` 定义之后**零调用** → `net_r` 永远 0.0。

★ 脚手架复用 `test_pending_ownership` 的 `_bind`：那里绑的是**引擎真实方法**，
  重写一份桩就等于测自己写的桩。
"""

import asyncio
import dataclasses
import inspect
import time
from types import SimpleNamespace

import pytest

import test_pending_ownership as _own

from ai_trader.trading import engine as E
from ai_trader.trading import pending_plan as P
from ai_trader.trading import pending_thesis_store as PTS
from ai_trader.trading import thesis as T

_bind = _own._bind
_run = _own._run
_FakeDB = _own.FakeDB


# ---------------------------------------------------------------- 工具


def _plan(**kw):
    """一条**真实** `P.Plan`（三引线，up，LIMIT）。

    ★ 必须是真 Plan 而不是 SimpleNamespace：采集路径会调 `pl.validate()`、
       `pl.effective_claim_ts`，桩上一缺就静默走「计划自洽性不过，丢弃」分支，
      测试会以「没采集到」失败，掩盖真正要验的参数落地。
    """
    base = dict(
        kind=P.KIND_WICKS, direction=T.UP, family=T.FAMILY_REVERSAL,
        order_price=60000.0, stop_price=59000.0, target_price=62000.0,
        order_type=T.ORDER_LIMIT, bar_index=10, bar_ts=1795500000000,
        claim_ts=1795500000000, claim_on_entry=False,
        evidence={"stop_distance": 1000.0},
    )
    base.update(kw)
    return P.Plan(**base)


def _code_only(fn):
    """函数的源码去掉注释行 —— 注释里提到旧写法不算「又写回去了」。"""
    out = []
    for ln in inspect.getsource(fn).splitlines():
        if ln.strip().startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def _bars15(n, start_ts=1000, step=900000):
    """15m 序列 —— 复用 `test_pending_ownership._bars` 的形状（带 .ts）。"""
    return _own._bars(n, start_ts=start_ts, step=step)


def _bars_iv(b15, price=61000.0):
    """`_step_pending` 的入参：**必须有 300000（5m）那一档**，否则直接 return。"""
    last = int(b15[-1].ts) + 900000
    return {
        300000: [SimpleNamespace(open_time=last, open=price, high=price,
                                 low=price, close=price)],
        900000: list(b15),
    }


class _Store(object):
    """只记录写回的 store。"""

    def __init__(self):
        self.written = []
        self.exits = []
        self.new_rows = []
        self._rows = []

    def upsert_thesis(self, row):
        self.written.append(dict(row))

    def add_if_new(self, row):
        self.new_rows.append(dict(row))
        return True

    def set_exit(self, thesis_id, ts, price, net_r, note=""):
        self.exits.append({"thesis_id": thesis_id, "ts": ts,
                           "price": price, "net_r": net_r, "note": note})

    def live(self, limit=200):
        return list(self._rows)

    def put_action(self, **kw):
        return True

    def mark_applied(self, *a, **kw):
        pass


# ------------------------------------------------- 1. 参数必须真的落地


def test_to_thesis_carries_all_three_params():
    """`to_thesis` 必须把三个参数原样带下去（它是唯一转换入口）。"""
    params = P.PendingParams(horizon_bars=12, stale_stop_mult=1.0,
                             surprise_pts=500.0)
    th = P.to_thesis(_plan(), thesis_id="x", params=params)
    assert th.horizon_bars == 12, "horizon_bars 没带下去 → 挂单永不过期"
    assert th.surprise_pts == 500.0, "surprise_pts 没带下去 → 惊喜欢出场关闭"
    assert th.stale_distance == pytest.approx(1000.0), (
        "反转族的 stale_distance 应为 stop_distance × stale_stop_mult，"
        "实际 %r → 「跑偏就撤单」永不触发" % (th.stale_distance,))


def test_harvest_writes_thesis_with_real_params():
    """采集落库的行里，三个参数必须**不是**默认 0（生产实测全是 0）。"""
    st = SimpleNamespace()
    st.db = _FakeDB()
    st.pending = _Store()
    b15 = _bars15(3, start_ts=1795500000000)
    b5 = _bars15(3, start_ts=1795500000000, step=300000)
    _cur15 = int(b15[-1].ts)
    # 只收「决策时刻 = 当前这根」的计划，所以 claim_ts 必须正好等于 cur15
    st.pending_source = lambda params: (
        [_plan(bar_ts=_cur15, claim_ts=_cur15)], {})
    st._pending_params = lambda: P.PendingParams(
        horizon_bars=12, stale_stop_mult=1.0, surprise_pts=500.0)
    st._pending_harvest_ts = 0
    st._pending_source_warned = False
    st._pending_plan_key = E.DecisionEngine._pending_plan_key
    _bind(st, "_pending_harvest")

    # `_pending_harvest` 是**同步**方法（采集不 await 任何东西）
    E.DecisionEngine._pending_harvest(st, b15, b5)

    assert len(st.pending.new_rows) == 1, (
        "计划没被采集落库：%r" % (st.pending.new_rows,))
    row = st.pending.new_rows[0]
    assert int(row["horizon_bars"]) == 12, (
        "落库的 horizon_bars=%r（应为 12）→ 挂单超时永远不触发"
        % (row["horizon_bars"],))
    assert float(row["surprise_pts"]) == 500.0, (
        "落库的 surprise_pts=%r（应为 500）→ `_surprise_hit` 遇 pts<=0 "
        "直接返回 None，惊喜欢出场彻底关闭" % (row["surprise_pts"],))
    assert float(row["stale_distance"]) > 0, (
        "落库的 stale_distance=%r（反转族应为 stop_distance×1.0）→ "
        "「挂单跑偏就撤」永不触发" % (row["stale_distance"],))


def test_harvest_does_not_hand_build_thesis():
    """结构性兜底：代码里不许再出现直接构造（注释里提旧写法不算）。"""
    src = _code_only(E.DecisionEngine._pending_harvest)
    assert "_T.Thesis(" not in src, (
        "_pending_harvest 又手工拼 Thesis 了 —— 必须走 to_thesis()，"
        "否则 horizon_bars/stale_distance/surprise_pts 会静默取 0")
    assert "to_thesis(" in src, "_pending_harvest 没有调用 to_thesis()"


# ------------------------------------------ 2. 游标不许把哨兵值落库


def _step_env(rows):
    st = SimpleNamespace()
    st.db = _FakeDB()
    st.pending = _Store()
    st.pending._rows = list(rows)
    st.pos = None
    st.adopted_pos = None
    st._pending_5m_ts = 0
    st.decisions_log = []
    st._applied_acts = []
    st.tcfg = SimpleNamespace(symbol="BTCUSDT", strategy="pending",
                              max_hold_bars=600, trend_stop_pct=25.0)
    st.ex = SimpleNamespace()
    st.pending_lines = None
    st._liq_cache = None
    st._liq_src = ""
    st._bind_ready = False
    _bind(st, *_own._ENGINE_METHODS)
    st._liq_distance_pct = lambda: 4.0455
    st._qty_step = lambda: 0.001
    # ★ 给**真实**参数：`_pending_repair_params` 靠它补旧行的 0 值。
    #   返回 None（旧的 `_sizing_params` 兜底）时修复是空操作，
    #   M8 变异体会存活 —— 那是假绿。
    #   ★★ 而且 `_pending_params` 必须**显式给**：它是 `_ENGINE_METHODS`
    #   之外的成员，漏了会抛 AttributeError，被 `_pending_repair_params`
    #   里那句 `except Exception: return th` 吞成「不修」—— 测试于是假绿。
    st._sizing_params = lambda: {"sizing.capital": 100.0,
                                 "sizing.leverage": 22.0}
    st._max_stop_distance_pct = lambda p: 1.6181818181818184
    st._pending_params = lambda: P.PendingParams(
        max_stop_pct=1.6181818181818184)
    st._pending_harvest = lambda *a, **kw: None
    st._pending_kbar_view = lambda: None
    st._pending_is_dry = lambda: True
    st._log_decision = lambda *a, **kw: st.decisions_log.append((a, kw))

    async def _noop(*a, **kw):
        return None

    st._step_selfcheck = _noop

    async def _apply(act, th, ts):
        st._applied_acts.append(str(getattr(act, "kind", "")))
        return True

    st._apply_pending_action = _apply
    return st


def test_start_index_sentinel_is_buffer_length():
    """先钉哨兵语义：计划那根比缓冲里所有 K 都新时返回 len。"""
    bars = _bars15(2, start_ts=1000)
    # 计划那根比缓冲里所有 K 都新 → 返回 len 当哨兵
    assert E.DecisionEngine._pending_start_index(bars, 10 ** 12) == 2


def test_frozen_cursor_is_recomputed():
    """`next_index == len(b15)` 时必须按**稳定锚点**重算，而不是继续空转。

    ★ `bar_ts` 列故意设成一个漂到缓冲区外的新时间（生产上每次推进都会被
      覆盖成当前 K）：只有锚点取自 `thesis_id` 里的声明时刻，才能算回下标 1。
      这一条同时钉住「接线」—— 变异验证里把锚点换回 `row["bar_ts"]`
      必须变红（只测辅助函数是抓不到的）。
    """
    b15 = _bars15(4, start_ts=1000)
    _plan_ts = 1000 + 900000                      # 计划那根 = 下标 1
    th = P.to_thesis(_plan(bar_ts=_plan_ts),
                     thesis_id="卡住|up|%d|60000.0" % _plan_ts)
    row = PTS.row_of(th, state=T.EXPECTED, next_index=4,
                     bar_ts=10 ** 12)             # 列已漂到缓冲之外

    st = _step_env([row])
    seen = {}
    real = T.step

    def _spy(thesis, bars, book, **kw):
        seen["start"] = int((kw.get("progress") or {}).get("next_index") or 0)
        return real(thesis, bars, book, **kw)

    orig = T.step
    T.step = _spy
    try:
        _run(E.DecisionEngine._step_pending(
            st, _bars_iv(b15), 60000.0, 100.0, int(time.time() * 1000)))
    finally:
        T.step = orig

    assert seen.get("start") == 1, (
        "游标卡在缓冲长度时没按 thesis_id 的声明时刻重算，start_at=%r（应为 1）"
        % (seen.get("start"),))


def test_step_pending_repairs_legacy_zero_params():
    """`_step_pending` 必须真的调用参数修复并写回（接线，不是辅助函数）。"""
    b15 = _bars15(4, start_ts=1000)
    _plan_ts = 1000 + 900000
    th = P.to_thesis(_plan(bar_ts=_plan_ts, order_price=60000.0,
                           stop_price=59000.0),
                     thesis_id="旧行|up|%d|60000.0" % _plan_ts)
    th = dataclasses.replace(th, horizon_bars=0, stale_distance=0.0,
                             surprise_pts=0.0)
    row = PTS.row_of(th, state=T.EXPECTED, next_index=0, bar_ts=_plan_ts)

    st = _step_env([row])
    _run(E.DecisionEngine._step_pending(
        st, _bars_iv(b15), 60990.0, 100.0, int(time.time() * 1000)))

    assert st.pending.written, "这一轮没有写回，无法验证修复"
    w = st.pending.written[-1]
    assert int(w["horizon_bars"]) == 12 and float(w["surprise_pts"]) == 500.0, (
        "旧行的 0 参数没被修复就写回了：hz=%r surp=%r —— 三条规则对这条"
        "预期依旧处于关闭状态" % (w["horizon_bars"], w["surprise_pts"]))


def test_terminal_state_without_position_is_rejected():
    """状态机推出 `CLOSED_TP` 但引擎没有仓位 → 不许落终态（FILL-1）。

    ★ 2026-11-25 真钱事故：状态机只从 K 线推演，它不知道单有没有挂出去。
      14:50 挂单成功、14:55 就推出 CLOSED_TP，而交易所 executed_qty=0、
      `self.pos` 为空 —— 一旦落成终态，就不会再有任何一轮来管那张真实
      挂单，它就此变成孤儿（成交还会被判成"我方成交"而硬停）。

    ★ 用 spy 直接给出「已填好仓位的状态机结果」，因为真实的 K 线回放
      会先撞上已有的 `pending_phantom_filled` 分支（那条只处理
      **库里已经是 FILLED** 的旧行），测不到这条新拦截的接线。
    """
    b15 = _bars15(4, start_ts=1000)
    plan_ts = 1000
    th = P.to_thesis(_plan(bar_ts=plan_ts, order_price=60000.0,
                           stop_price=59000.0, target_price=61000.0),
                     thesis_id="自相矛盾|up|%d|60000.0" % plan_ts)
    row = PTS.row_of(th, state=T.ARMED, next_index=0, bar_ts=plan_ts)

    st = _step_env([row])
    st.pos = {}                      # ★ 引擎侧**没有**仓位事实

    fake_res = SimpleNamespace(
        state=T.CLOSED_TP, actions=[], ambiguous=[], next_index=1,
        armed_at_bar=0, armed_ref_close=60000.0,
        armed_at_sub_ts=int(b15[0].ts), best_favorable_px=61000.0,
        next_index_used=1,
        states=lambda: [T.ARMED, T.FILLED, T.CLOSED_TP])
    orig = T.step
    T.step = lambda *a, **kw: (fake_res, {
        "state": T.CLOSED_TP, "next_index": 1, "armed_at_bar": 0,
        "armed_ref_close": 60000.0, "armed_at_sub_ts": int(b15[0].ts),
        "best_favorable_px": 61000.0})
    try:
        _run(E.DecisionEngine._step_pending(
            st, _bars_iv(b15), 62000.0, 100.0, int(time.time() * 1000)))
    finally:
        T.step = orig

    assert st.pending.written, "这一轮没有写回，无法验证拦截"
    for w in st.pending.written:
        assert str(w["state"]) not in (T.CLOSED_TP, T.CLOSED_SL,
                                       T.CLOSED_THESIS), (
            "没有仓位事实却落了终态 %r → 真实挂单会变成孤儿"
            % (w["state"],))
    kinds = [k for k, _ in st.db.risks]
    assert "pending_terminal_without_position" in kinds, (
        "拦截了却没留风险事件，静默失败：%r" % (kinds,))


def test_sentinel_never_lands_in_the_store():
    """计划那根还没进缓冲：本轮空转，但落库的游标必须是 0。

    落 `len(b15)` 就是那个永久冻结（生产 4 条全是 400）。
    """
    b15 = _bars15(4, start_ts=1000)
    th = P.to_thesis(_plan(bar_ts=999900000),
                     thesis_id="未来|up|999900000|60000.0")
    row = PTS.row_of(th, state=T.EXPECTED, next_index=0, bar_ts=999900000)

    st = _step_env([row])
    _run(E.DecisionEngine._step_pending(
        st, _bars_iv(b15), 60000.0, 100.0, int(time.time() * 1000)))

    assert st.pending.written, "这一轮必须照常落库（决策连续性）"
    for w in st.pending.written:
        assert int(w["next_index"]) == 0, (
            "把哨兵 %r 落库了 → 下一轮不重算、状态机永久冻结"
            % (w["next_index"],))


# ------------------------------------------ 3. 入场价被穿过 → 计划作废


def test_crossed_entry_price_cancels_expected_thesis():
    """多头限价挂在市价**上方**：一挂出就成交、止损只剩一丝 → 必须作废。"""
    b15 = _bars15(4, start_ts=1000)
    th = P.to_thesis(_plan(order_price=60000.0, stop_price=59950.0),
                     thesis_id="穿过|up|1|60000.0", params=P.PendingParams())
    row = PTS.row_of(th, state=T.EXPECTED, next_index=0, bar_ts=1000)

    st = _step_env([row])
    _run(E.DecisionEngine._step_pending(
        st, _bars_iv(b15, 59000.0), 59000.0, 100.0,
        int(time.time() * 1000)))

    states = [w["state"] for w in st.pending.written]
    assert "CANCELLED" in states, (
        "入场价 60000 已被现价 59000 穿过，计划却还活着：states=%r" % (states,))


def test_untouched_entry_price_keeps_thesis_alive():
    """反向对照：价格还没到入场价时**不许**撤（否则永远开不了单）。"""
    b15 = _bars15(4, start_ts=1000)
    th = P.to_thesis(_plan(order_price=60000.0), thesis_id="没到|up|1|60000.0")
    row = PTS.row_of(th, state=T.EXPECTED, next_index=0, bar_ts=1000)

    st = _step_env([row])
    _run(E.DecisionEngine._step_pending(
        st, _bars_iv(b15, 61000.0), 61000.0, 100.0,
        int(time.time() * 1000)))

    states = [w["state"] for w in st.pending.written]
    assert "CANCELLED" not in states, (
        "现价 61000 高于入场价 60000（等回踩中），不该撤单：states=%r"
        % (states,))


# ------------------------------------------------ 4. net_r 必须被写


def test_plan_ts_comes_from_thesis_id_not_from_the_drifting_column():
    """锚点必须取 thesis_id 里的声明时刻，不能取会漂的 `bar_ts` 列。

    ★ 生产实测：`bar_ts` 每次推进都被覆盖成当前 5m 的 ts（动作 id 需要它变），
      于是拿它当锚点会算出「计划那根就是现在」→ 游标永远等于缓冲末尾 →
      那 1 条剩下的预期就永久冻结（部署后线上就是这个现象）。
    """
    st = SimpleNamespace()
    _bind(st, "_pending_plan_ts")
    row = {"thesis_id": "形态|up|1795488600000|58930.0",
           "bar_ts": 1795578300000}          # 列已经漂到当前 K
    assert st._pending_plan_ts(row) == 1795488600000, (
        "锚点取错来源：用了会漂的 bar_ts，游标会永远停在缓冲末尾")


def test_plan_ts_falls_back_for_unparseable_ids():
    """自检单这类非标准 id 不许抛错（抛了整条链就停）。"""
    st = SimpleNamespace()
    _bind(st, "_pending_plan_ts")
    row = {"thesis_id": "selfcheck-1795145400000", "bar_ts": 123}
    assert st._pending_plan_ts(row) == 123


def test_repair_params_fills_zeros_from_current_params():
    """旧行三个参数为 0 时必须按当前口径补上（否则规则依旧关闭）。"""
    st = SimpleNamespace()
    st._pending_params = lambda: P.PendingParams(
        horizon_bars=12, stale_stop_mult=1.0, surprise_pts=500.0)
    _bind(st, "_pending_repair_params")

    th = P.to_thesis(_plan(order_price=60000.0, stop_price=59000.0),
                     thesis_id="旧|up|1|60000.0")
    th = dataclasses.replace(th, horizon_bars=0, stale_distance=0.0,
                             surprise_pts=0.0)
    got = E.DecisionEngine._pending_repair_params(st, {}, th)

    assert got.horizon_bars == 12, "旧行 horizon_bars 没补"
    assert got.surprise_pts == 500.0, "旧行 surprise_pts 没补"
    assert got.stale_distance == pytest.approx(1000.0), (
        "旧行 stale_distance 没按 |挂单价−止损价| 补上")


def test_repair_params_never_overwrites_existing_values():
    """只补 0，不覆盖非 0（将来改口径不许把历史行改掉）。"""
    st = SimpleNamespace()
    st._pending_params = lambda: P.PendingParams(
        horizon_bars=99, stale_stop_mult=1.0, surprise_pts=999.0)
    _bind(st, "_pending_repair_params")

    th = P.to_thesis(_plan(), thesis_id="新|up|1|60000.0")
    got = E.DecisionEngine._pending_repair_params(st, {}, th)
    assert got.horizon_bars == th.horizon_bars, "覆盖了已有的 horizon_bars"
    assert got.surprise_pts == th.surprise_pts, "覆盖了已有的 surprise_pts"


def _close_env(pos):
    """够 `_pending_close` 跑完的替身（成交、关账、R 都走真实产品代码）。"""
    from ai_trader.trade_db import ADOPT_REASON  # noqa: F401  (确保模块存在)

    class _Fill(object):
        ok = True
        price = 61000.0
        fee = 0.0
        reason = ""

    st = SimpleNamespace()
    st.db = _FakeDB()
    st.pending = _Store()
    st.pos = pos

    class _Ex(object):
        async def market_order(self, *a, **kw):
            return _Fill()

        async def mark_price(self):
            return 61000.0

    st.ex = _Ex()
    st.tcfg = SimpleNamespace(symbol="BTCUSDT")
    # 平仓路径现在还会：撤地板单、推飞书。桩上绑**产品真实现**，
    # 不写空壳 —— 空壳会让"接线是否真的发生"变成测不到的空白。
    _bind(st, "_pending_close", "_push", "_system_stop_state",
          # BOOK-1：平仓分笔结算
          "_settle_close", "_lots_of", "_push_lot", "_pending_fill_bar_ts",
          "_cancel_system_stop_order")
    return st


def _pos(stop=59000.0, entry=60000.0, qty=0.01):
    from ai_trader.trading.engine import PENDING_FILL_REASON
    return {"qty": qty, "side": "long", "entry": entry, "stop": stop,
            "open_reason": PENDING_FILL_REASON, "entry_ts": 1, "paper": 0}


def test_pending_close_writes_net_r():
    """平仓必须落 R 倍数（行为断言）。

    ★ 为什么必须是行为断言：上一版只检查源码里有没有 `set_exit(` 这几个字，
      把 `if _risk > 0` 改成 `if False`（永远不写）它照样绿 —— 变异验证
      当场把这个假阳性抓了出来。
    """
    st = _close_env(_pos())
    act = SimpleNamespace(thesis_id="甲|up|1|60000.0", reason="测试")
    _run(E.DecisionEngine._pending_close(st, act, None))

    assert st.pending.exits, (
        "平仓后没有落 net_r —— 实盘 R 账本又变成死字段（22/22 全是 0.0）")
    got = st.pending.exits[0]
    # 毛盈亏 (61000-60000)*0.01 = 10U；1R = 0.01*|60000-59000| = 10U → 1.0R
    assert got["net_r"] == pytest.approx(1.0, abs=1e-9), (
        "R 倍数算错：净 %r / 1R 10U，应得 1.0R" % (got["net_r"],))
    assert got["price"] == 61000.0, "出场价没写对：%r" % (got["price"],)


def test_pending_close_without_stop_does_not_fake_a_zero_r():
    """算不出 1R（止损=入场）时**不许**写 0 —— 0 会被读成"打平"。"""
    st = _close_env(_pos(stop=60000.0, entry=60000.0))
    act = SimpleNamespace(thesis_id="乙|up|1|60000.0", reason="测试")
    _run(E.DecisionEngine._pending_close(st, act, None))

    assert not st.pending.exits, (
        "1R=0 时仍然写了 net_r=%r —— 那会把「量不了」伪装成「打平」"
        % (st.pending.exits,))


def test_pending_close_calls_set_exit():
    """结构性兜底（第二道）：源码里必须存在这个调用。"""
    src = _code_only(E.DecisionEngine._pending_close)
    assert "set_exit(" in src, (
        "_pending_close 没有调用 set_exit —— net_r 又会永远是 0.0")


def test_expected_brief_does_not_claim_an_order_is_resting():
    """EXPECTED 的文案不许读成「有张单挂着」（用户就是这么被误导的）。"""
    st = SimpleNamespace()
    _bind(st, "_pending_thesis_brief")
    th = P.to_thesis(_plan(), thesis_id="甲|up|1|60000.0")
    msg = st._pending_thesis_brief(th, T.EXPECTED, 61000.0)
    assert "未挂单" in msg, (
        "EXPECTED 的文案没写清「未挂单」，读者会以为有单在交易所等着：%r" % msg)
    assert "等触发（挂单" not in msg, (
        "旧文案回来了：%r —— 它描述的「挂单 X」并不存在于交易所" % msg)
