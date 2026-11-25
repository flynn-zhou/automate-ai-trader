# -*- coding: utf-8 -*-
"""挂单策略接线：`strategy="pending"` 必须走挂单链，而且必须能安全地「只出计划」。

★ 这一份首先是一个**安全修复**的证明，不是新功能测试。

2026-11-17 之前的状态：`config` 的策略白名单里已经有 `pending`，
但 `engine._step` 里没有任何 `_is_pending()` 分支。于是配了 pending 的环境
会**直接落进剥头皮逻辑**（3s 动量 + 可达性门）—— 一个已被回测证伪的负期望策略。
不报错、不告警，看板上策略名还写着 pending。

三个必须钉住的不变量：

1. pending **绝不**落到剥头皮链（哪怕没接线，也只许不下单）
2. dry-run 下**一张单都不许发**（用户要求先跑一轮只出计划）
3. 推进游标必须在**动作落地之后**，且动作按 (thesis, bar_ts, kind) 幂等
   —— 进程重启后同一根 K 重算，止损不能平第二次
"""
from __future__ import annotations

import asyncio

import pytest

from ai_trader.config import AppCfg
from ai_trader.settings import SettingsStore
from ai_trader.trading import pending_thesis_store as PTS
from ai_trader.trading import thesis as T
from ai_trader.trading.engine import DecisionEngine


# ---------------------------------------------------------------- 夹具

class _Feed:
    last_price = 78000.0

    class _Agg:
        def __init__(self, px):
            self._b = type("B", (), {"close": px, "open": px, "high": px,
                                     "low": px, "open_time": 0,
                                     "interval_ms": 3000})()

        def current(self, _iv):
            return self._b

    def __init__(self, px=78000.0):
        self.last_price = px
        self.agg = self._Agg(px)
        self.src = "WS"
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()

    def on_bar(self, *_a, **_k):
        pass


class _RecEx:
    """只记录调用的执行器。`name` 故意叫 scalp-maker，证明分流不看执行器名字。"""

    name = "live"
    supports_pending = True

    def __init__(self):
        self.placed = []
        self.cancelled = []
        self.equity_calls = 0

    async def equity(self):
        self.equity_calls += 1
        return {"balance": 50.0, "unrealized": 0.0, "equity": 50.0,
                "position_qty": 0.0, "position_side": ""}

    async def place_order(self, side, qty, order_type="LIMIT", price=None,
                          stop_price=None, reduce_only=False, client_id="",
                          working_type="CONTRACT_PRICE"):
        self.placed.append({"side": side, "qty": qty, "type": order_type,
                            "price": price, "reduce_only": reduce_only,
                            "client_id": client_id})
        return type("O", (), {"status": "NEW", "order_id": "1"})(), ""

    async def cancel_order(self, order_id="", client_id="", symbol=None):
        self.cancelled.append({"order_id": order_id, "client_id": client_id})
        return True

    async def open_orders(self, symbol=None):
        return []

    async def query_order(self, order_id="", client_id="", symbol=None):
        return None

    async def position(self):
        return {"qty": 0.0, "entry": 0.0, "side": ""}


class _DB:
    """最小 TradeDB 替身：只记 risk 事件。"""

    def __init__(self):
        self.risks = []

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, detail))

    def log_decision(self, *_a, **_k):
        pass

    def log_equity(self, *_a, **_k):
        pass


def _bars(ts0, n, step_ms, px=78000.0):
    out = []
    for i in range(n):
        t = ts0 + i * step_ms
        out.append(type("B", (), {
            "open_time": t, "ts": t,
            "open": px, "high": px + 10, "low": px - 10, "close": px,
            "volume": 1.0, "interval_ms": step_ms})())
    return out


def _engine(tmp_path, dry=True, wired=True, strategy="pending"):
    cfg = AppCfg()
    cfg.trading.strategy = strategy
    cfg.trading.mode = "paper"
    cfg.trading.exec_market = "perp"
    store = SettingsStore(str(tmp_path / "market_settings.db"))
    store.seed_from(cfg.trading)
    store.apply_to(cfg.trading)
    eng = DecisionEngine(cfg, _Feed(), _RecEx(), _DB(), guard=None,
                         settings=store,
                         pending_store=(PTS.PendingThesisStore(str(tmp_path / "p.db"))
                                        if wired else None),
                         pending_dry_run=dry)
    return eng, store


# ---------------------------------------------------------------- 1. 分流

def test_pending_never_falls_into_the_scalp_chain(tmp_path):
    """★ 核心安全断言：配了 pending 就必须走挂单链，不许碰剥头皮。

    反证方式：给引擎一个**看起来像纸面执行器的名字**也没用 ——
    分流只认配置值 `strategy`，不认执行器。
    """
    eng, _s = _engine(tmp_path, wired=True)
    assert eng._is_pending() is True
    assert eng._is_chan() is False
    # 三条互斥：pending 不能被任何别的谓词吞掉
    assert eng._is_donchian() is False


def test_step_actually_dispatches_to_pending(tmp_path):
    """★★ 真去调 `_step`，而不是只断言谓词。

    上一版只写了 `assert eng._is_pending() is True` —— 那钉的是**谓词**，
    而真正的危险是**分流**：谓词对、分流写错（或写了但被删），
    pending 就还是会落进剥头皮链。变异测试里
    `pending-falls-into-scalp` 就是专门删这一行分流的—— 这条测试必须能咬住它。
    """
    eng, _s = _engine(tmp_path, wired=True)
    hit = {}

    async def _spy(bars, price, equity, now_ms):
        hit["pending"] = True

    eng._step_pending = _spy

    async def _scalp_spy(*a, **_k):
        hit["scalp"] = True

    # 剥头皮链的入口名字随版本会变，所以能接全部接上
    for name in ("_step_scalp", "_step_momentum", "_maybe_enter", "_enter"):
        if hasattr(eng, name):
            setattr(eng, name, _scalp_spy)

    bars = {300000: _bars(0, 6, 300000), 900000: _bars(0, 6, 900000)}
    asyncio.get_event_loop().run_until_complete(eng._step(bars))
    assert hit.get("pending") is True, (
        "配了 strategy=pending，`_step` 却没调 `_step_pending` —— 它会落进剥头皮链")
    assert hit.get("scalp") is not True, "pending 走了剥头皮分支"


def test_pending_without_a_store_refuses_to_trade(tmp_path):
    """★ 没接线时**宁可不下单**，也不许落到剥头皮。

    这条防的是「以为配好了其实没接上」：如果这里落进剥头皮，
    系统会用一套已证伪的策略去下真钱单，而日志里策略名还写着 pending。
    """
    eng, _s = _engine(tmp_path, wired=False)
    ex = eng.ex
    bars = {300000: _bars(0, 20, 300000), 900000: _bars(0, 20, 900000)}

    asyncio.get_event_loop().run_until_complete(
        eng._step_pending(bars, 78000.0, 50.0, 1_000_000))
    assert ex.placed == [], "没接线却下单了"
    assert any(k == "pending_not_wired" for k, _d in eng.db.risks), (
        "没接线却没有任何 risk 事件 —— 静默空转，运维查不出来")
    # 只报一次（否则每根 3s K 线都刷一条，真正告警会被淹掉）
    n = len([1 for k, _ in eng.db.risks if k == "pending_not_wired"])
    asyncio.get_event_loop().run_until_complete(
        eng._step_pending(bars, 78000.0, 50.0, 2_000_000))
    assert len([1 for k, _ in eng.db.risks if k == "pending_not_wired"]) == n


# ---------------------------------------------------------------- 2. dry-run

def _seed_thesis(store, *, order=78000.0, stop=79000.0, direction=T.DOWN,
                 thesis_id="t1"):
    """种一条预期。

    ★ 作空的止损必须**高于**挂单价（上方才是亏）。第一版写成 77000
      了，于是 `Thesis` 构造直接报“做空：止损必须高于挂单价”——
      而测试把这个异常当成“没下单”读了，于是反向对照一直在空转。
      修好几何之后，三条「会下单」的测试才真正跑到了下单路径。
    """
    th = T.Thesis(
        thesis_id=thesis_id, kind="三引线假突破", direction=direction,
        order_type=T.ORDER_LIMIT, order_price=order, stop_price=stop,
        target_price=order - 4 * abs(order - stop), horizon_bars=12)
    store.upsert_thesis(PTS.row_of(th, state=T.EXPECTED, next_index=0))
    return th


def test_dry_run_places_no_orders(tmp_path):
    """★ 「只出计划不挂单」必须是硬保证：执行器一次 place_order 都不许被调。"""
    eng, _s = _engine(tmp_path, dry=True)
    _seed_thesis(eng.pending)
    bars = {300000: _bars(0, 6, 300000), 900000: _bars(0, 6, 900000)}

    asyncio.get_event_loop().run_until_complete(
        eng._step_pending(bars, 78000.0, 50.0, 1_000_000))
    assert eng.ex.placed == [], (
        "dry-run 下发了单：%r" % (eng.ex.placed,))
    # 但动作必须被**记账**（否则无法回答「它本来会做什么」）
    # ★ 用 `actions()` 而不是 `pending_actions()`：dry-run 会把动作标成 applied，
    #   而后者只返回未执行的 —— 用它断言等于要求“记了但没处理”。
    acts = eng.pending.actions("t1")
    assert acts, "dry-run 连动作都没记 —— 那就不是『只出计划』，是没跑"
    assert acts[0]["kind"] == "place_order", acts[0]
    assert acts[0]["applied"] == 1
    assert float(acts[0]["price"]) > 0, "记下来的计划连价位都没有"


def test_dry_run_is_the_default(tmp_path):
    """默认必须是 dry-run。「要不要下真单」不该靠『没配』来决定。"""
    assert AppCfg().trading.pending_dry_run is True


def test_live_mode_would_place(tmp_path):
    """反向对照：关掉 dry-run 之后同一个场景必须真的下单。

    没有这条，上面那条「dry-run 不下单」可能因为「永远不下单」而永远绿。
    """
    eng, _s = _engine(tmp_path, dry=False)
    _seed_thesis(eng.pending)
    bars = {300000: _bars(0, 6, 300000), 900000: _bars(0, 6, 900000)}

    asyncio.get_event_loop().run_until_complete(
        eng._step_pending(bars, 78000.0, 50.0, 1_000_000))
    assert eng.ex.placed, (
        "关掉 dry-run 之后一张单都没发 —— 上面那条『dry-run 不下单』就是空转的绿")
    o = eng.ex.placed[0]
    assert o["qty"] > 0
    assert o["client_id"].startswith("AI-pend-"), o["client_id"]


# ---------------------------------------------------------------- 3. 推进入口

def test_only_one_advance_per_5m_bar(tmp_path):
    """★ 一根 5m 只能推进一次。

    `_step` 由 3s 桶驱动，一根 5m 里会进来约 100 次。不判重 = 同一根 K
    被推 100 次，成交判定重复执行、游标被推过头。
    """
    eng, _s = _engine(tmp_path, dry=False)
    _seed_thesis(eng.pending)
    bars = {300000: _bars(0, 6, 300000), 900000: _bars(0, 6, 900000)}
    loop = asyncio.get_event_loop()

    loop.run_until_complete(eng._step_pending(bars, 78000.0, 50.0, 1))
    first = len(eng.ex.placed)
    # 同一根 5m 再推 50 次
    for _ in range(50):
        loop.run_until_complete(eng._step_pending(bars, 78000.0, 50.0, 2))
    assert len(eng.ex.placed) == first, (
        "同一根 5m 被重复推进，多下了 %d 张单"
        % (len(eng.ex.placed) - first))

    # ★ 只数订单是不够的：幂等层会把重复推进产出的动作吸掉，
    #   于是「每根 3s 都推一次」这个漏洞在订单数上看不出来。
    #   推进次数本身才是不变量：一根 5m 只该推 1 次。
    calls = {"n": 0}
    real = eng.pending.upsert_thesis

    def _counting(row):
        calls["n"] += 1
        return real(row)

    eng.pending.upsert_thesis = _counting
    for _ in range(20):
        loop.run_until_complete(eng._step_pending(bars, 78000.0, 50.0, 3))
    assert calls["n"] == 0, (
        "同一根 5m 又被推了 %d 次（幂等层只能掩盖订单，"
        "盖不住“状态机被重复执行”本身）" % calls["n"])


def test_restart_does_not_double_place(tmp_path):
    """★ 进程重启后重算同一根 K，动作必须幂等（不能平第二次仓 / 挂第二个单）。"""
    eng, store = _engine(tmp_path, dry=False)
    _seed_thesis(eng.pending)
    bars = {300000: _bars(0, 6, 300000), 900000: _bars(0, 6, 900000)}
    loop = asyncio.get_event_loop()
    loop.run_until_complete(eng._step_pending(bars, 78000.0, 50.0, 1))
    placed = len(eng.ex.placed)
    assert placed >= 1

    # 模拟重启：新引擎、同一个库、把游标退回「这根还没推过」
    eng2 = DecisionEngine(eng.cfg, _Feed(), _RecEx(), _DB(), guard=None,
                          settings=store, pending_store=eng.pending,
                          pending_dry_run=False)
    for row in eng2.pending.live():
        eng2.pending.upsert_thesis(dict(row, next_index=0))
    loop.run_until_complete(eng2._step_pending(bars, 78000.0, 50.0, 1))
    assert len(eng2.ex.placed) == 0, (
        "重启后把同一根 K 的动作又执行了一遍：%r" % (eng2.ex.placed,))


def test_apply_action_is_idempotent_at_the_action_level(tmp_path):
    """★ 直接钉幂等层本身：同一条动作执行两次，只能下一张单。

    为什么不能靠「重启后不重复下单」那条就够：那条的前提是
    重算出来的动作**与上次同一个 bar_ts**，而实际上止损被平第二次往往发生在
    游标落盘失败后的重试路径上。直接打这一层，不经过状态机的形状前提。
    """
    eng, _s = _engine(tmp_path, dry=False)
    th = _seed_thesis(eng.pending)
    from ai_trader.trading.thesis import Action

    # Action 的字段顺序是 (kind, thesis_id, reason, price, side, ...)
    act = Action("place_order", "t1", "test", 78000.0, "SELL", "LIMIT")
    loop = asyncio.get_event_loop()
    first = loop.run_until_complete(eng._apply_pending_action(act, th, 1_500_000))
    second = loop.run_until_complete(eng._apply_pending_action(act, th, 1_500_000))
    assert first is True, "第一次就没执行"
    assert second is False, (
        "同一条动作被执行了两次——平仓动作就是平两次")
    assert len(eng.ex.placed) == 1, eng.ex.placed


# ---------------------------------------------------------------- 4. 仓位

def test_sizing_uses_the_web_formula_not_a_second_one(tmp_path):
    """★ 仓位必须来自 `settings.sizing()` + `derive()`，不许另立一套口径。

    用户明确拒绝再过一遍「杠杆/风险填多少」的问题 —— Web 上早就有公式。
    所以这里断言的是「同一份配置算出来的数与 derive 一致」。
    """
    eng, store = _engine(tmp_path)
    from ai_trader.trading.sizing import SizingConfig, derive
    sz = store.sizing()
    d = derive(SizingConfig(capital=sz["sizing.capital"],
                            risk_pct=sz["sizing.risk_pct"],
                            leverage=sz["sizing.leverage"]))

    order, stop = 78000.0, 77000.0        # 止损 1000 点 = 1.282%，足够宽才能开
    qty, why = eng._pending_size(order, stop)
    assert why == "", why
    assert qty > 0
    notional = qty * order
    # 风险上限（plan_position 定位的，取整只会更小）
    assert notional * (abs(order - stop) / order) <= d.risk_per_trade + 1e-6, (
        "单笔风险 %.3fU 超过上限 %.3fU"
        % (notional * abs(order - stop) / order, d.risk_per_trade))


@pytest.mark.parametrize("stop_pct", [0.05, 0.119, 0.140, 0.192, 0.259, 0.336,
                                      0.5, 1.0, 2.0, 5.0])
def test_pending_size_agrees_with_plan_position(tmp_path, stop_pct):
    """★★ 仓位只能有一个来源：同一输入下判定必须与 `plan_position` 一致。

    这条直接钉的是 2026-11-17 写出的那个缺陷：`_pending_size` 把
    保证金上限当成“夹一下”（min(by_risk, by_margin)），于是同一批 11 条真实计划
    两边给出相反答案：`plan_position` 说 0/11，`_pending_size` 说 11/11，
    而且实际风险只有 0.45–1.31U（配置说 2U）。

    反证：只钉“能开”是不够的 —— 必须两侧都钉，否则把两边都改成“总是拒绝”
    也能过。所以这里取 10 个止损宽度横跨“应该拒”和“应该开”两侧。
    """
    eng, store = _engine(tmp_path)
    from ai_trader.trading.sizing import SizingConfig, plan_position
    sz = store.sizing()
    # ★ 保证金上限必须取**引擎实际用的那份**（`,sizing_params()`，
    #   默认 100%）。不填就会落到 dataclass 默认 40%，
    #   于是这条测试自己就变成“拿两个不同口径去比”。
    p0 = eng._sizing_params()
    cfg = SizingConfig(capital=sz["sizing.capital"],
                       risk_pct=sz["sizing.risk_pct"],
                       leverage=sz["sizing.leverage"],
                       margin_cap_pct=float(p0.get("sizing.margin_cap_pct", 100.0)))
    base = 78000.0
    stop = base * (1.0 + stop_pct / 100.0)         # 做空：止损在上方
    # ★ 风险预算 = 止损距离 + 成本。引擎传给 plan_position 的就是这个量，
    #   测试必须用同一个量去比，否则又是“拿两个口径比”。
    from ai_trader.trading.pending_plan import PendingParams, round_trip_costs
    _cw, _cost_pts = round_trip_costs(base, "LIMIT", PendingParams())
    risk_pct = stop_pct + _cost_pts / base * 100.0
    pp = plan_position(cfg, risk_pct)
    qty, why = eng._pending_size(base, stop)
    if pp.ok:
        assert qty > 0, (
            "风控说可开而实际拒了（止损 %.3f%%）：%s" % (stop_pct, why))
    else:
        assert qty == 0, (
            "风控说不行而实际开了（止损 %.3f%%）：手数 %r"
            % (stop_pct, qty))
        assert why, "拒绝了却没有理由"
        # 理由必须来自 plan_position，不能是自己另写一套
        assert "风控拒绝" in why, why


def test_pending_skips_when_guard_says_no(tmp_path):
    """★★ 安全闸不能只管剥头皮和趋势：挂单路径也必须过 guard。

    上一版 `_pending_place` 直接 `ex.place_order`，于是数据源 DOWN、
    日亏损上限、连亏冷却、熔断、持仓不一致 —— 这些保护
    在最新的这条策略上全部失效，而且不报错。
    """
    eng, _s = _engine(tmp_path, dry=False)

    class _Guard:
        def can_open(self):
            return False, "数据源 DOWN"

    eng.guard = _Guard()
    _seed_thesis(eng.pending)
    bars = {300000: _bars(0, 6, 300000), 900000: _bars(0, 6, 900000)}
    asyncio.get_event_loop().run_until_complete(
        eng._step_pending(bars, 78000.0, 50.0, 1_000_000))
    assert eng.ex.placed == [], "安全闸说不行，挂单路径却下了单：%r" % (eng.ex.placed,)
    assert any(k == "pending_blocked_by_guard" for k, _d in eng.db.risks), (
        "被闸但没有 risk 事件，运维查不出来")


def test_pending_guard_allows_when_ok(tmp_path):
    """反向对照：guard 放行时必须能下单。

    没有这条，上面那条可能因为“永远不下单”而恒绿。
    """
    eng, _s = _engine(tmp_path, dry=False)

    class _Guard:
        def can_open(self):
            return True, ""

    eng.guard = _Guard()
    _seed_thesis(eng.pending)
    bars = {300000: _bars(0, 6, 300000), 900000: _bars(0, 6, 900000)}
    asyncio.get_event_loop().run_until_complete(
        eng._step_pending(bars, 78000.0, 50.0, 1_000_000))
    assert eng.ex.placed, "guard 放行了却不下单，上一条测试就是空转的绿"


def test_actual_risk_includes_fees_and_equals_R(tmp_path):
    """★★ 止损被打时的**总亏损（止损 + 手续费）必须等于 R。**

    旧契约是「qty × 止损距离 = R」，但止损被打时还要交手续费和滑点，
    于是实际亏损是 R 的 1.31 倍到 1.87 倍（真实 11 条实测）。
    现在抉定的是「qty × (止损 + 成本) = R」，且「qty × 止损 < R」
    （因为一部分风险预算被成本占了）。

    ★ 不能只钉“≤ R”—— 把仓位改成 0 也能过。所以钉的是“恰好等于”。
    """
    eng, store = _engine(tmp_path)
    from ai_trader.trading.sizing import SizingConfig, derive
    from ai_trader.trading.pending_plan import PendingParams, round_trip_costs
    sz = store.sizing()
    d = derive(SizingConfig(capital=sz["sizing.capital"],
                            risk_pct=sz["sizing.risk_pct"],
                            leverage=sz["sizing.leverage"]))
    opened = 0
    for stop_pct in (0.1, 0.15, 0.2, 0.25, 0.3, 0.5, 1.0):
        entry = 78000.0
        stop = entry * (1.0 + stop_pct / 100.0)
        _cw, cost_pts = round_trip_costs(entry, "LIMIT", PendingParams())
        qty, why = eng._pending_size(entry, stop)
        if qty <= 0:
            assert why, "拒绝了却没有理由"
            continue
        opened += 1
        stop_loss = qty * abs(entry - stop)
        fees = qty * cost_pts
        total = stop_loss + fees
        assert total <= d.risk_per_trade + 1e-6, (
            "止损被打时总亏损 %.3fU 超过 R=%.0fU"
            % (total, d.risk_per_trade))
        # ★ 下界：不允许“开了但只开了一小截”。一手就是 0.001 BTC。
        one_lot_total = 0.001 * (abs(entry - stop) + cost_pts)
        assert total >= d.risk_per_trade - one_lot_total - 1e-6, (
            "总风险 %.3fU 远低于配置的 %.0fU（被静默夹小了）"
            % (total, d.risk_per_trade))
        # ★ 成本确实占了预算：止损距离那一截必须严格小于 R
        assert stop_loss < d.risk_per_trade, (
            "止损距离已经用满了风险预算，成本没进去：qty × 止损 = %.3fU = R"
            % stop_loss)
    assert opened >= 3, "一条都没开，这条测试退化成了空转"


@pytest.mark.parametrize("entry,stop,expect", [
    (0.0, 77000.0, "缺少入场价"),
    (78000.0, 0.0, "缺少入场价"),
    (78000.0, 78000.0, "止损距离为 0"),
])
def test_bad_geometry_is_refused_with_a_reason(tmp_path, entry, stop, expect):
    """价格几何不成立时必须拒绝，并且理由指向具体原因（禁止静默拒单）。"""
    eng, _s = _engine(tmp_path)
    qty, why = eng._pending_size(entry, stop)
    assert qty == 0
    assert expect in why, why


def test_no_settings_means_no_trade(tmp_path):
    """★ 读不到 Web 配置时拒绝开仓 —— 绝不按硬编码默认值在真钱上算仓位。"""
    eng, _s = _engine(tmp_path)
    # ★ 现在取数走 `_sizing_params()`（与守卫同一份），所以“读不到配置”
    #   要模拟的是它返回 None，而不是把 settings 置空。
    eng._sizing_params = lambda: None
    qty, why = eng._pending_size(78000.0, 77700.0)
    assert qty == 0
    assert "仓位参数不可用" in why, why

def test_production_params_carry_the_formula_stop(tmp_path):
    """★★ 生产必须走 `_pending_params()`：它把 d_max 填进 max_stop_pct。

    不走它（直接 `PendingParams()`）会让止损静默落回振幅规则，
    于是仓位被放大十几倍、保证金装不下 —— 而且不报错。
    """
    eng, _s = _engine(tmp_path)
    p = eng._pending_params()
    assert p.max_stop_pct > 0, "生产参数没带 max_stop_pct，止损会落回振幅规则"
    from ai_trader.trading.sizing import SizingConfig, derive
    from ai_trader.trading.pending_plan import PendingParams
    sz = eng._sizing_params()
    d = derive(SizingConfig(capital=sz["sizing.capital"],
                            risk_pct=sz["sizing.risk_pct"],
                            leverage=sz["sizing.leverage"]))
    assert p.max_stop_pct == pytest.approx(d.max_stop_distance_pct, abs=1e-9)
    # 反向对照：默认参数必须是 0，否则上面那条断言是空的
    assert PendingParams().max_stop_pct == 0.0
    # 且它和 sizing 的杠杆一致（不是 trend_leverage 的 1.0）
    assert eng._plan_leverage() == pytest.approx(sz["sizing.leverage"])
