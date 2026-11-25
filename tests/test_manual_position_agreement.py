# -*- coding: utf-8 -*-
"""第 6 条闸必须与「人工仓接管」约定对齐（§13.2 / 2026-11-19）。

背景：用户 2026-11-19 明确说「不带 AI- 的七笔单是我人工做的，而且我还会
持续做，当时跟系统约定好了」。约定就是 §13.2：

    手工仓 → 接管 → 不判策略出场 → 保留熔断止损 → **不是故障**

但第 6 条闸（第 115 轮加的）在接管完之后**无条件**硬停，而
``halt_kind="mismatch"`` 永不自动解除。于是：

    用户在手机上开仓 → 系统按约定接管 → 紧接着硬停自己 → 从此不再开新仓

11-13 那次硬停（0.15 BTC、亏 122.8 USDT）一直挂到 11-19，就是这条冲突
造成的。用户每按约定操作一次，系统就停一次。

本文件钉住「怎么分清」和「分清之后各自怎么处置」：

  1. 错配差额全部由**系统外**成交解释 → 人工仓 → 接管、**不硬停**
  2. 差额里有``AI-``前缀的成交 → 我们自己的单丢了回报 → **必须硬停**
  3. 查不到 / 凑不出差额 / 执行器不支持 → **必须硬停**（保守，绝不猜）
  4. 人工仓接管**不许**顺手清掉已有硬停（比如回撤硬停）
  5. 反向控制：一致时永远不停

★ 判据只看**差额**（交易所 − 数据库），不看整笔持仓。拿整笔持仓去解释，
系统自己正常的 ``AI-`` 成交会把人工仓一起污染成"我们的"，人工仓一次也
放不出去 —— 这个 bug 会表现得像"改了没生效"。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.trading.engine import (
    PROV_MANUAL, PROV_OURS, PROV_UNKNOWN, DecisionEngine,
    classify_delta, fills_explaining_delta)
from ai_trader.trading.trend import TrendStrategy

PX = 68000.0


class _Hub:
    def publish(self, *a, **k):
        pass


class _Feed:
    def __init__(self, px=PX):
        self.last_price = px
        self.cfg = AppCfg()
        self.hub = _Hub()
        self.src = "WS"

        class _Agg:
            def current(self, iv):
                class _B:
                    close = px
                return _B()
        self.agg = _Agg()


class _DB:
    def __init__(self):
        self.risks = []
        self.trades = []

    def log_risk(self, k, m):
        self.risks.append((k, m))

    def risk_kinds(self):
        return [k for k, _ in self.risks]

    def log_decision(self, *a, **k):
        pass

    def log_order(self, d):
        pass

    def log_fill(self, d):
        pass

    def log_trade(self, d):
        self.trades.append(d)
        return 1

    def log_equity(self, d):
        pass

    def close_trade(self, d):
        pass

    def open_trade(self, d):
        return None


class _LiveEx:
    """永续执行器替身：交易所有 qty 个 BTC，成交历史由 fills 给定。"""

    name = "live"
    supports_pending = True
    supports_fill_history = True

    def __init__(self, qty=0.0, entry=PX, fills=None, boom=None):
        self.qty = qty
        self.entry = entry
        self.mark = PX
        self.fills = list(fills or [])
        self.boom = boom          # 非 None 时 recent_fills 抛这个异常
        self.calls = 0

    async def equity(self):
        return {"balance": 100.0, "unrealized": 0.0,
                "equity": 100.0, "position_qty": self.qty,
                "position_side": "long" if self.qty else ""}

    async def mark_price(self):
        return self.mark

    async def position(self):
        return {"qty": self.qty, "entry": self.entry,
                "side": "long" if self.qty else ""}

    async def recent_fills(self, hours=168):
        self.calls += 1
        if self.boom is not None:
            raise self.boom
        return list(self.fills)

    async def market_order(self, side, qty, reason="", reduce_only=False):
        from ai_trader.exchange.base import FillResult
        return FillResult(ok=True, price=self.mark, qty=qty, fee=0.0,
                          status="filled")


class _NoHistoryEx(_LiveEx):
    """有仓位接口、但没有成交历史（现货/纸面/老执行器）。"""

    supports_fill_history = False

    async def recent_fills(self, hours=168):
        raise AssertionError("不支持查成交历史的执行器不该被调用")


def _fill(side, qty, cid, ts):
    return {"ts": ts, "side": side, "qty": qty, "price": PX, "client_id": cid}


def _eng(ex, db_qty=None, guard=None, strategy="trend"):
    cfg = AppCfg()
    cfg.trading.strategy = strategy
    cfg.trading.exec_market = "perp"
    cfg.trading.sma_lookback = 5
    feed = _Feed()
    db = _DB()
    g = guard if guard is not None else Guard(cfg)
    e = DecisionEngine(cfg, feed, ex, db, guard=g)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    e.trend.seeded = True
    e.trend.need_reconcile = False
    if db_qty is not None:
        e.pos = {"side": "long", "qty": db_qty, "entry": PX,
                 "stop": PX * 0.75, "target": 0.0, "entry_ts": 1, "bars": 0,
                 "best": PX, "worst": PX, "trade_id": 1,
                 "max_hold_bars": 10 ** 9, "open_reason": "", "fee": 0.0,
                 "signal": "trend", "dust": 0.0}
    return e, db, g


# ---------------- 归类：纯函数，先钉判据本身 ----------------

def test_delta_explained_by_external_fills_is_manual():
    """人工仓：差额正好被几笔系统外成交解释。"""
    # 11-13 的真实形状：04:06 之前用户分三笔买入共 0.06，clientOrderId 全空
    fills = [
        _fill("BUY", 0.038, "", 3000),
        _fill("BUY", 0.006, "", 2000),
        _fill("BUY", 0.016, "", 1000),
    ]
    used, ok = fills_explaining_delta(fills, 0.06)
    assert ok is True
    assert len(used) == 3, u"0.06 要三笔才凑齐，实际只用了 %d 笔" % len(used)
    assert classify_delta(fills, 0.06)[0] == PROV_MANUAL


def test_delta_explained_by_our_own_fill_is_ours():
    """我们自己的单丢了成交回报：差额里有 AI- 前缀 → 真故障。"""
    fills = [_fill("BUY", 0.05, "AI-1793499651238-1", 3000)]
    assert classify_delta(fills, 0.05)[0] == PROV_OURS


def test_any_ai_fill_in_the_delta_wins():
    """混合：只要解释差额的成交里有一笔是我们的，就必须按真故障处理。

    保守方向是**停**，不是放行：我们自己不知道那笔成交的存在。
    """
    fills = [
        _fill("BUY", 0.03, "", 3000),                      # 用户的
        _fill("BUY", 0.02, "AI-1793499651239", 2000),      # 我们的
    ]
    assert classify_delta(fills, 0.05)[0] == PROV_OURS


def test_delta_that_cannot_be_explained_is_unknown():
    """7 天成交凑不出差额 → unknown（不许猜，调用方照旧硬停）。"""
    fills = [_fill("BUY", 0.01, "", 3000)]
    used, ok = fills_explaining_delta(fills, 0.06)
    assert ok is False, u"凑不出 0.06 却报成功：%r" % (used,)
    assert classify_delta(fills, 0.06)[0] == PROV_UNKNOWN


def test_sell_fills_explain_a_short_delta():
    """反方向也要对：差额是负的（空头），必须用 SELL 累加出来。"""
    fills = [
        _fill("SELL", 0.02, "", 2000),
        _fill("SELL", 0.03, "", 1000),
    ]
    assert classify_delta(fills, -0.05)[0] == PROV_MANUAL


def test_only_the_delta_is_explained_not_the_whole_position():
    """★ 钉住「只看差额」——这是最容易被改错的一处。

    策略自己的仓（AI- 成交）在库里有记录，不是差额的一部分。
    如果实现改成「解释整笔持仓」，AI- 成交就会被卷进来，
    人工仓会被误判成 ours，硬停永远解除不了。

    注意：这条只是**纯函数**层面的仓库级约束，真正能抓住引擎里
    `delta = ex_qty - db_qty` 被改成 `delta = ex_qty` 的是下面
    test_engine_classifies_the_delta_not_the_whole_position()。
    """
    fills = [
        _fill("BUY", 0.05, "", 3000),                 # 用户加的 0.05 = 差额
        _fill("BUY", 0.10, "AI-1793499650000", 1000),  # 策略原有的 0.10
    ]
    # 库里已知 0.10，交易所 0.15 → 差额 +0.05
    assert classify_delta(fills, 0.05)[0] == PROV_MANUAL


@pytest.mark.asyncio
async def test_engine_classifies_the_delta_not_the_whole_position():
    """★★ 引擎级：策略自己那 0.10 不许把人工加的 0.05 一起污染成 ours。

    构造的是**最容易搞错**的形状——策略本来就有一笔仓：

        库里 0.10（策略开的，成交带 AI-）
        交易所 0.15（用户在手机上又加了 0.05）

    正确：差额 +0.05 只由那笔系统外的成交解释 → manual → 不硬停。
    错误（把 delta 写成 ex_qty=0.15）：要凑 0.15 就得把策略自己的
    0.10 卷进来 → 命中 AI- → ours → 硬停。

    这一条是变异 `delta-classified-from-whole-position` 的对手测试；
    纯函数版的 test_only_the_delta_... 抓不住它（那条不经过引擎）。
    """
    ex = _LiveEx(qty=0.15, fills=[
        _fill("BUY", 0.05, "", 3000),                  # 用户刚加的（最新）
        _fill("BUY", 0.10, "AI-1793499650000", 1000),  # 策略原本的仓
    ])
    e, db, g = _eng(ex, db_qty=0.10)
    await e.reconcile_live()

    # ★ 2026-11-24 拆仓后：交易所 0.15 比库里 0.10 多出的那 0.05
    #   （用户手工加的）接管进人工仓槽，策略自己那 0.10 留在系统仓槽。
    #   合成一个 dict 就再也分不开了 —— 而且「多出来的部分」才是接管对象，
    #   断言接管量等于 0.05 比断言「等于 0.15」更贴合约定的语义。
    assert (e.adopted_pos is not None
            and abs(float(e.adopted_pos["qty"]) - 0.05) < 1e-9), (
        u"构造前提：交易所 0.15 比库里 0.10 多出的 0.05 应被接管，实际 %r"
        % (e.adopted_pos and e.adopted_pos.get("qty"),))
    ok, why = g.can_open()
    assert ok, (
        u"策略自己那 0.10 被算进了差额，人工加的 0.05 因此被判成"
        u"「我们的单」而硬停。应当只解释差额（交易所−数据库）。"
        u"can_open=%r why=%r" % (ok, why))
    assert "manual_position_adopted" in db.risk_kinds()


# ---------------- 引擎接线：分清之后各自怎么处置 ----------------

@pytest.mark.asyncio
async def test_manual_position_is_adopted_without_hard_stop():
    """★ 核心：人工仓接管后 can_open 仍为真，并且留下可查的事件。"""
    ex = _LiveEx(qty=0.06, fills=[
        _fill("BUY", 0.038, "", 3000),
        _fill("BUY", 0.006, "", 2000),
        _fill("BUY", 0.016, "", 1000),
    ])
    e, db, g = _eng(ex, db_qty=None)
    await e.reconcile_live()

    assert e.adopted_pos is not None, u"人工仓必须被接管（§13.2 的约定）"
    ok, why = g.can_open()
    assert ok, (u"人工仓被硬停了 —— 用户每手工开一次仓系统就停一次，"
                u"11-13 那次就是这样挂到 11-19 的。can_open=%r why=%r"
                % (ok, why))
    assert g.st.halted_hard is False
    assert "manual_position_adopted" in db.risk_kinds(), (
        u"接管了人工仓却没有任何事件记录，出了事没法复盘")
    # 告警文案要能看出是人工仓，而不是"对账不一致"
    detail = dict(db.risks)["manual_position_adopted"]
    assert u"人工" in detail


@pytest.mark.asyncio
async def test_our_own_lost_fill_still_hard_stops():
    """反向控制：差额来自我们自己的单 → 必须硬停。

    没有这条，一个「永远不硬停」的实现也能通过上一测试。
    """
    ex = _LiveEx(qty=0.05, fills=[
        _fill("BUY", 0.05, "AI-1793499651238-1", 3000),
    ])
    e, db, g = _eng(ex, db_qty=None)
    await e.reconcile_live()
    ok, why = g.can_open()
    assert not ok, u"我们自己的单丢了成交回报却没硬停：can_open=%r" % (ok,)
    assert g.st.halted_hard is True
    assert g.st.halt_kind == "mismatch"
    assert "manual_position_adopted" not in db.risk_kinds(), (
        u"我们的单被误记成了人工仓")


@pytest.mark.asyncio
async def test_executor_without_fill_history_still_hard_stops():
    """保守：查不到成交历史（纸面/现货/老执行器）→ 照旧硬停。

    ★ 这一条防的是「为了放行人工仓而把闸整个拆掉」。
      不知来源时宁可停下来等人，也不能猜。
    """
    ex = _NoHistoryEx(qty=0.06)
    e, db, g = _eng(ex, db_qty=None)
    await e.reconcile_live()
    ok, _why = g.can_open()
    assert not ok, u"查不到成交历史却放行了：这是把闸拆了，不是对齐约定"
    assert g.st.halted_hard is True


@pytest.mark.asyncio
async def test_fill_history_error_still_hard_stops():
    """保守：查成交历史报错（网络/签名/限频）→ 照旧硬停，不能当成人工仓。"""
    ex = _LiveEx(qty=0.06, boom=RuntimeError("HTTP 500"))
    e, db, g = _eng(ex, db_qty=None)
    await e.reconcile_live()
    ok, _why = g.can_open()
    assert not ok, u"查询失败被静默当成人工仓放行了"
    assert g.st.halted_hard is True


@pytest.mark.asyncio
async def test_manual_adoption_never_clears_an_existing_hard_stop():
    """人工仓接管**不许**顺手解除已有硬停。

    构造：先因回撤硬停，然后人工仓被接管。
    如果实现里为了"放行人工仓"调了 guard.reset()，正在生效的
    回撤保护就被一起抹掉了 —— 这是拿安全换便利。
    """
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    g = Guard(cfg)
    g.st.halted_hard = True
    g.st.halt_kind = "drawdown"
    g.st.halt_reason = "回撤硬停（构造）"

    ex = _LiveEx(qty=0.06, fills=[_fill("BUY", 0.06, "", 1000)])
    e, db, _g = _eng(ex, db_qty=None, guard=g)
    await e.reconcile_live()

    assert g.st.halted_hard is True, u"接管人工仓把已有的回撤硬停清掉了"
    assert g.st.halt_kind == "drawdown", (
        u"硬停种类被改成了 %r —— 账实不符的闸和回撤的闸混了"
        % (g.st.halt_kind,))


@pytest.mark.asyncio
async def test_matching_position_never_halts():
    """反向控制：一致时永远不停，而且根本不去查成交历史（省一次 API）."""
    ex = _LiveEx(qty=0.06, fills=[_fill("BUY", 0.06, "", 1000)])
    e, db, g = _eng(ex, db_qty=0.06)
    await e.reconcile_live()
    ok, why = g.can_open()
    assert ok, u"持仓一致却硬停了：%r" % (why,)
    assert ex.calls == 0, (
        u"对账通过时不该查成交历史（实盘每 5 分钟一次，纯浪费配额）")


def test_guard_has_a_public_manual_position_entry():
    """结构约束：Guard 必须有一条专门给「人工仓接管」的公开入口。

    行为测试可以被"在 engine 里自己写 halted_hard=False"骗过，
    所以这里盯源码结构。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "ai_trader", "guard.py"),
               encoding="utf-8").read()
    assert "def manual_position_adopted" in src, (
        u"guard.py 里没有 manual_position_adopted()——人工仓接管只能"
        u"复用 position_mismatch()，而那一个会硬停")
