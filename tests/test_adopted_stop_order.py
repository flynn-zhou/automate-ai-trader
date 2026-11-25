# -*- coding: utf-8 -*-
"""A1 / A2 / A3 —— 2026-11-25「交易域审查」的回归测试。

这三条都属于同一类问题：**系统以为自己有保护，其实没有**。

  A1  22 倍杠杆的持仓，全部保护只有进程内 3 秒一次的轮询。
      进程挂了 / 服务器重启 / 网络断 / 行情源卡住 —— 仓位在交易所侧裸奔。
      修法：人工仓在交易所挂一张 reduceOnly 的 STOP_MARKET。
  A2  止损的「触发价」和「复核价」来自两个不同的价格源，快跌时
      复核价落后 → 判「行情陈旧」→ 拒绝平仓，而那正是最该平的一秒。
      修法：都用最新成交价（= 币安 STOP 单的触发口径）。
  A3  P0 保命用的爆仓价有 5 分钟 TTL，而 `equity()` 每 tick 已经顺手
      拿到过新鲜的真实值，却被丢掉。修法：用它喂缓存。

写测试的取舍与 `test_audit_1124_full_chain.py` 一致：能断言「某件事没有
发生」的，就不断言「结果正确」—— 这类缺陷的共同形态是代码看起来做了、
其实没做。
"""
from __future__ import annotations

import asyncio
import inspect
import re
import types

from ai_trader.trading import engine as E


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ------------------------------------------------------------------ 替身
class _Order(object):
    def __init__(self, oid="", status="NEW", stop_price=0.0, qty=0.0):
        self.order_id = oid
        self.status = status
        self.stop_price = stop_price
        self.qty = qty


class _LiveEx(object):
    """够用的「实盘」执行器替身。只记录挂单/撤单，不碰网络。"""

    name = "live"

    def __init__(self, price=61250.0, place_err="", existing=None):
        self.placed = []
        self.cancelled = []
        self.queried = []
        self._price = price
        self._place_err = place_err
        self._existing = existing
        self._seq = 0

    async def place_order(self, side, qty, order_type="LIMIT", price=None,
                          stop_price=None, reduce_only=False, client_id="",
                          working_type="CONTRACT_PRICE"):
        self.placed.append({
            "side": side, "qty": qty, "type": order_type,
            "price": price, "stop_price": stop_price,
            "reduce_only": reduce_only, "client_id": client_id,
            "working_type": working_type})
        if self._place_err:
            return _Order(), self._place_err
        self._seq += 1
        return _Order(oid="EX-%d" % self._seq), ""

    async def cancel_order(self, order_id="", client_id="", symbol=None):
        self.cancelled.append(order_id)
        return True

    async def query_order(self, order_id="", client_id="", symbol=None):
        self.queried.append(client_id)
        return self._existing

    async def mark_price(self):
        return self._price

    async def last_trade_price(self):
        return self._price

    async def market_order(self, side, qty, reason="", reduce_only=False,
                           client_id=""):
        # 与真实 `FillResult` 对齐：`_adopted_close` 先看 `ok`，再看 `qty`/`fee`。
        return types.SimpleNamespace(ok=True, price=self._price, fee=0.1,
                                     qty=qty)


class _Db(object):
    def __init__(self):
        self.risks = []
        self.decisions = []
        self.closes = []
        self.trades = []

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, detail))

    def log_decision(self, d):
        self.decisions.append(d)

    def close_trade(self, t):
        self.closes.append(t)

    def log_trade(self, t):
        self.trades.append(t)

    def kinds(self):
        return [k for k, _ in self.risks]


def _lot(qty=0.032, entry=60880.0, stop=59894.81):
    return {"side": "long", "qty": qty, "entry": entry, "stop": stop,
            "target": 0.0, "entry_ts": 1795400000000, "bars": 0,
            "best": entry, "worst": entry, "trade_id": None,
            "max_hold_bars": 0, "open_reason": E.ADOPT_REASON,
            "paper": 0, "fee": 0.5}


_METHODS = ("_adopted_slot", "_is_adopted", "_adopted_stop_state",
            "_adopted_stop_cid", "_sync_adopted_stop_order",
            "_cancel_adopted_stop_order", "_adopted_close",
            "_nope")


def _engine(adopted=None, pos=None, ex=None, step=0.001):
    st = types.SimpleNamespace()
    st.db = _Db()
    st.ex = ex or _LiveEx()
    st.pos = pos
    st.adopted_pos = adopted
    st.tcfg = types.SimpleNamespace(symbol="BTCUSDT", strategy="pending")
    st._liq_cache = None
    st._qty_step = lambda: step
    for name in _METHODS:
        raw = E.DecisionEngine.__dict__.get(name)
        if raw is None:
            continue
        fn = getattr(raw, "__func__", raw)
        setattr(st, name, types.MethodType(fn, st))
    return st


# ------------------------------------------------- 1. 持有期间必须挂着单
def test_holding_a_lot_places_a_reduce_only_stop_market_order():
    """★ A1 的核心：持有期间交易所侧必须真的有一张止损单。

    断言的是"挂了什么"，不是"挂上了" —— 参数错一个（比如忘了
    reduce_only、或者用 LIMIT）就等于没有保护。
    """
    ex = _LiveEx(price=61250.0)
    st = _engine(adopted=_lot(stop=59894.81), ex=ex)

    _run(E.DecisionEngine._sync_adopted_stop_order(st, st.adopted_pos, 61250.0))

    assert len(ex.placed) == 1, "持有期间没有挂出任何交易所侧止损单"
    o = ex.placed[0]
    assert o["type"] == "STOP_MARKET"
    assert o["reduce_only"] is True, "不是 reduceOnly，触发后可能开出反向仓"
    assert o["side"] == "SELL", "多头的保护单必须是 SELL"
    assert abs(o["stop_price"] - 59894.81) < 1e-6
    assert abs(o["qty"] - 0.032) < 1e-9
    # 触发口径必须与 A2 一致：币安按最新成交价触发，不是标记价
    assert o["working_type"] == "CONTRACT_PRICE"
    assert st._adopted_stop_state()["oid"] == "EX-1"


def test_short_lot_protects_with_a_buy_stop_above_the_market():
    ex = _LiveEx(price=61250.0)
    lot = _lot(stop=62500.0)
    lot["side"] = "short"
    st = _engine(adopted=lot, ex=ex)

    _run(E.DecisionEngine._sync_adopted_stop_order(st, lot, 61250.0))

    assert len(ex.placed) == 1
    assert ex.placed[0]["side"] == "BUY"
    assert ex.placed[0]["stop_price"] > 61250.0


# ------------------------------------------------- 2. 幂等：不乱发请求
def test_already_synced_order_is_not_placed_again():
    """每 3 秒跑一次，参数没变就不许再发请求（否则就是每 3 秒一张单）。"""
    ex = _LiveEx(price=61250.0)
    st = _engine(adopted=_lot(), ex=ex)
    lot = st.adopted_pos

    _run(E.DecisionEngine._sync_adopted_stop_order(st, lot, 61250.0))
    _run(E.DecisionEngine._sync_adopted_stop_order(st, lot, 61250.0))
    _run(E.DecisionEngine._sync_adopted_stop_order(st, lot, 61250.0))

    assert len(ex.placed) == 1, "同一张单被重复挂出 %d 次" % len(ex.placed)


def test_restart_adopts_the_existing_exchange_order_by_client_order_id():
    """重启后内存里没有 order id，但 clientOrderId 是稳定的 ——
    必须认回交易所上那一张，而不是再挂一张第二张。
    """
    ex = _LiveEx(price=61250.0,
                 existing=_Order(oid="EX-OLD", stop_price=59894.81, qty=0.032))
    st = _engine(adopted=_lot(stop=59894.81), ex=ex)

    _run(E.DecisionEngine._sync_adopted_stop_order(st, st.adopted_pos, 61250.0))

    assert ex.queried, "没有按 clientOrderId 去认领已有单据"
    assert ex.placed == [], "重启后重复挂了一张止损单"
    assert st._adopted_stop_state()["oid"] == "EX-OLD"


def test_client_order_id_is_stable_and_short_enough():
    """币安要求 clientOrderId < 36 字符，且必须稳定（同一笔仓同一个 id）。"""
    st = _engine(adopted=_lot(), ex=_LiveEx())
    cid1 = E.DecisionEngine._adopted_stop_cid(st, st.adopted_pos)
    cid2 = E.DecisionEngine._adopted_stop_cid(st, dict(st.adopted_pos))
    assert cid1 == cid2, "同一笔仓算出了两个不同的 clientOrderId"
    assert len(cid1) < 36
    assert cid1.startswith(E.ADOPTED_STOP_CID_PREFIX)


# ------------------------------------------------- 3. 止损抬高 → 单要跟着改
def test_raised_stop_replaces_the_exchange_order():
    """止损棘轮抬高之后，交易所侧那张单必须跟着改 ——
    否则进程一挂，保护就退回旧的、更松的位置。
    """
    ex = _LiveEx(price=61250.0)
    st = _engine(adopted=_lot(stop=59894.81), ex=ex)
    lot = st.adopted_pos

    _run(E.DecisionEngine._sync_adopted_stop_order(st, lot, 61250.0))
    lot["stop"] = 60500.0
    _run(E.DecisionEngine._sync_adopted_stop_order(st, lot, 61250.0))

    assert len(ex.placed) == 2
    assert ex.cancelled == ["EX-1"], "旧单没撤，交易所会留下两张止损单"
    assert abs(ex.placed[-1]["stop_price"] - 60500.0) < 1e-6


# ------------------------------------------------- 4. 减仓/全平要同步
def test_partial_close_resizes_the_exchange_order():
    """减仓之后那张单的量必须跟着变小，否则单上的量和真实仓对不上。"""
    ex = _LiveEx(price=61250.0)
    st = _engine(adopted=_lot(qty=0.032, stop=59894.81), ex=ex)
    lot = st.adopted_pos
    _run(E.DecisionEngine._sync_adopted_stop_order(st, lot, 61250.0))

    _run(E.DecisionEngine._adopted_close(st, 0.012, "long", 60880.0, "测试"))

    assert len(ex.placed) == 2, "减仓后没有重新挂一张小一点的单"
    assert abs(ex.placed[-1]["qty"] - 0.020) < 1e-9
    assert ex.cancelled == ["EX-1"]


def test_full_close_cancels_the_exchange_order():
    """仓没了，单必须撤 —— 不撤会一直占着保证金和挂单数。"""
    ex = _LiveEx(price=61250.0)
    st = _engine(adopted=_lot(qty=0.032), ex=ex)
    _run(E.DecisionEngine._sync_adopted_stop_order(st, st.adopted_pos, 61250.0))

    _run(E.DecisionEngine._adopted_close(st, 0.032, "long", 60880.0, "测试"))

    assert ex.cancelled == ["EX-1"]
    assert st.adopted_pos is None
    assert st._adopted_stop_state() == {}, "仓已平但本地还记着一张单"


def test_no_lot_cancels_a_leftover_order():
    """仓位在别处消失（强平/手机平仓）时，_step 的看门狗要能撤掉遗留单。

    这里验证它依赖的那个函数真的会撤单；「_step 里有没有接上」由下面
    那条结构性测试钉住。
    """
    ex = _LiveEx(price=61250.0)
    st = _engine(adopted=_lot(), ex=ex)
    _run(E.DecisionEngine._sync_adopted_stop_order(st, st.adopted_pos, 61250.0))
    assert st._adopted_stop_state()["oid"] == "EX-1"

    _run(E.DecisionEngine._cancel_adopted_stop_order(st, "人工仓已不在"))

    assert ex.cancelled == ["EX-1"]
    assert st._adopted_stop_state() == {}


def test_step_has_a_watchdog_that_cancels_when_the_lot_is_gone():
    """结构性判据：`_step` 里必须有「不是人工仓就撤遗留单」这一支。

    为什么用源码断言：`_step` 依赖十几个协作者，跑起来是集成测试的规模。
    而这里要防的东西极其具体 —— **有人删掉这一支**，遗留单就永远撤不掉。
    """
    src = inspect.getsource(E.DecisionEngine._step)
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "_cancel_adopted_stop_order" in body, \
        "`_step` 里没有撤遗留止损单的看门狗（仓在别处消失时单会一直挂着）"
    # 必须在「不是人工仓」的那一支上，而不是随便哪里
    assert re.search(r"elif\s+self\._adopted_stop_state\(\)", body), \
        "看门狗没有挂在 `if self._is_adopted()` 的 else 支上"


# ------------------------------------------------- 5. 不许制造"假保护"
def test_paper_executor_never_places_an_exchange_stop():
    """纸面执行器上挂单 = 制造"有交易所保护"的错觉，比不挂更危险。"""
    ex = _LiveEx(price=61250.0)
    ex.name = "paper"
    st = _engine(adopted=_lot(), ex=ex)

    _run(E.DecisionEngine._sync_adopted_stop_order(st, st.adopted_pos, 61250.0))

    assert ex.placed == [], "纸面上挂了一张永远不会触发的止损单"


# ------------------------------------------------- 6. 挂不上要说出来
def test_place_failure_is_recorded_and_retried_next_tick():
    ex = _LiveEx(price=61250.0, place_err="-2021 Order would immediately trigger")
    st = _engine(adopted=_lot(), ex=ex)

    _run(E.DecisionEngine._sync_adopted_stop_order(st, st.adopted_pos, 61250.0))

    assert "adopted_stop_order_failed" in st.db.kinds(), \
        "挂单失败没有任何记录（等于静默失去进程外保护）"
    assert st._adopted_stop_state() == {}, "失败后还记着一个不存在的单"
    # 下一轮必须重试，而不是永久放弃
    ex._place_err = ""
    _run(E.DecisionEngine._sync_adopted_stop_order(st, st.adopted_pos, 61250.0))
    assert len(ex.placed) == 2, "失败之后没有重试"
    assert st._adopted_stop_state().get("oid"), "重试成功后本地没有记住单号"


# ------------------------------------------------- 7. 不该挂的时候不挂
def test_already_triggered_stop_is_not_placed():
    """价格已经在止损外侧 → 进程内这一 tick 就该平掉它。
    这时候挂一张立刻会触发的单只是多一层滑点。
    """
    ex = _LiveEx(price=59000.0)
    st = _engine(adopted=_lot(stop=59894.81), ex=ex)

    _run(E.DecisionEngine._sync_adopted_stop_order(st, st.adopted_pos, 59000.0))

    assert ex.placed == []


def test_qty_below_one_step_is_not_placed():
    ex = _LiveEx(price=61250.0)
    st = _engine(adopted=_lot(qty=0.0004, stop=59894.81), ex=ex, step=0.001)

    _run(E.DecisionEngine._sync_adopted_stop_order(st, st.adopted_pos, 61250.0))

    assert ex.placed == []


def test_missing_or_zero_stop_is_not_placed_as_price_zero():
    """stop=0 不能被当成"止损在 0 元"去挂单（那是个永远不触发的单）。"""
    ex = _LiveEx(price=61250.0)
    lot = _lot()
    lot["stop"] = 0.0
    st = _engine(adopted=lot, ex=ex)

    _run(E.DecisionEngine._sync_adopted_stop_order(st, lot, 61250.0))

    assert ex.placed == []


# ------------------------------------------------- 8. A2 同源
def test_stop_confirmation_prefers_the_last_trade_price_over_mark_price():
    """★ A2：复核价必须与触发价同源（最新成交价），不能用标记价。

    用标记价复核的后果是可复现的：快跌时 last 先动、mark 慢半拍，
    于是「本地已触发、交易所未触发」→ 判「行情陈旧」→ **拒绝平仓**。
    """
    class _Both(object):
        def __init__(self):
            self.calls = []

        async def last_trade_price(self):
            self.calls.append("last")
            return 61000.0

        async def mark_price(self):
            self.calls.append("mark")
            return 61500.0

    st = types.SimpleNamespace(ex=_Both())
    got = _run(E.DecisionEngine._confirm_stop_price(st, 61500.0))

    assert got == 61000.0, "复核用了标记价而不是最新成交价"
    assert st.ex.calls == ["last"], "同时取两个价格源 = 两套事实"


def test_stop_confirmation_falls_back_to_mark_price_for_old_executors():
    class _OnlyMark(object):
        async def mark_price(self):
            return 61500.0

    st = types.SimpleNamespace(ex=_OnlyMark())
    assert _run(E.DecisionEngine._confirm_stop_price(st, 61500.0)) == 61500.0


def test_last_trade_price_hits_the_ticker_endpoint_unsigned():
    """`/fapi/v1/ticker/price` 是公开端点，不该签名。"""
    from ai_trader.exchange.live import LiveExecutor

    seen = {}

    class _Cfg(object):
        class trading(object):
            symbol = "BTCUSDT"

    ex = object.__new__(LiveExecutor)
    ex.cfg = _Cfg()
    ex.feed = types.SimpleNamespace(last_price=0.0)

    async def _fake_req(method, path, params=None, signed=True):
        seen.update(method=method, path=path, params=params, signed=signed)
        return {"price": "61234.5"}

    ex._req = _fake_req
    assert _run(LiveExecutor.last_trade_price(ex)) == 61234.5
    assert seen["path"] == "/fapi/v1/ticker/price"
    assert seen["signed"] is False
    assert seen["params"] == {"symbol": "BTCUSDT"}


# ------------------------------------------------- 9. A3 爆仓价不陈旧
def test_equity_returns_the_liq_price_from_the_same_position_call():
    """★ A3：`equity()` 已经调过一次 `position()`，爆仓价顺手带回来。

    断言两件事：返回值里有 liq_price，而且**只查了一次**持仓 ——
    两次查询就是"同一事实两个来源"，中间还会变。
    """
    from ai_trader.exchange.live import LiveExecutor

    n = {"position": 0}

    class _Cfg(object):
        class trading(object):
            symbol = "BTCUSDT"

    ex = object.__new__(LiveExecutor)
    ex.cfg = _Cfg()

    async def _fake_req(method, path, params=None, signed=True):
        return {"totalWalletBalance": "100.0", "totalUnrealizedProfit": "2.5"}

    async def _fake_position():
        n["position"] += 1
        return {"qty": 0.032, "entry": 60880.0, "side": "long",
                "liq_price": 58356.2, "unrealized": 2.5}

    ex._req = _fake_req
    ex.position = _fake_position
    d = _run(LiveExecutor.equity(ex))

    assert abs(d["liq_price"] - 58356.2) < 1e-6
    assert d["position_qty"] == 0.032
    assert n["position"] == 1, "equity() 查了 %d 次持仓" % n["position"]


def test_liq_cache_is_refreshed_from_equity_each_tick():
    """★ A3：每个 tick 都要用 equity() 带回的真实爆仓价刷新缓存。

    为什么用行为断言而不是源码断言：这一支最初写在 `_step` 内联里，
    变异验证实测"把它改成 `if False:` 测试仍然全绿" —— 源码断言只能
    证明那行字存在，证明不了它被执行。所以抽成
    `_seed_liq_cache_from_equity` 之后在这里直接调它。
    """
    st = _engine(adopted=_lot(), ex=_LiveEx())
    st._liq_cache = None

    E.DecisionEngine._seed_liq_cache_from_equity(
        st, {"liq_price": 58356.2, "position_qty": 0.032}, 1795400000000)

    cache = st._liq_cache
    assert cache, "equity 带回了真实爆仓价，但缓存没有被刷新（P0 会读 5 分钟前的值）"
    assert cache[0] == 1795400000000
    assert abs(cache[1] - 58356.2) < 1e-9
    assert cache[2] <= 10000, "TTL %d ms 比一个 tick 长太多，等于没刷新" % cache[2]


def test_liq_cache_is_not_clobbered_by_a_missing_liq_value():
    """交易所没给爆仓价（无仓/字段为空）时，不许把缓存清成 0 ——
    那会让 P0 以为"离爆仓距离无穷大"或者退化成不可用。
    """
    st = _engine(adopted=_lot(), ex=_LiveEx())
    st._liq_cache = (1, 58356.2, 60000)

    for eq in (None, {}, {"liq_price": 0.0}, {"liq_price": None},
               {"liq_price": "abc"}):
        E.DecisionEngine._seed_liq_cache_from_equity(st, eq, 2)
        assert st._liq_cache == (1, 58356.2, 60000), "被 %r 清掉了" % (eq,)


def test_step_calls_the_liq_cache_seeder():
    """结构性判据：`_step` 必须真的调它（否则抽出来的函数是死代码）。"""
    src = inspect.getsource(E.DecisionEngine._step)
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "_seed_liq_cache_from_equity(eq" in body, \
        "`_step` 没有使用 equity() 带回的真实爆仓价"
