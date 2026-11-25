# -*- coding: utf-8 -*-
"""纸面挂单能力的契约测试（Step 1）。

★ 这份文件要钉的是「**纸面与实盘的语义不许分叉**」，不是「纸面能挂单了」。

  k-trader 的教训：纸面/回测/实盘三条路径 →「回测赚、实盘亏」无法归因。
  所以这里三个层次都测：

    1. **契约层**：两个执行器都必须实现同样四个方法，签名一致；
       挂单类型集合必须是同一份（`_PENDING_TYPES` 与 `PENDING_TYPES`）。
    2. **校验层**：同一批非法入参，两边给出同样的拒绝理由（live 的这几条
       校验在 `await self.spec()` **之前**，所以不联网也能测）。
    3. **成交层**：影线触及、跳空、止损/止盈的触发方向、maker/taker 费率、
       滑点方向、同根歧义 —— 逐条用构造出来的 K 验。

用户 2026-11-17 拍板的成交判据：**影线触及 + 同根歧义单独记账**。
"""
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_trader.config import load                             # noqa: E402
from ai_trader.exchange.base import Executor, PendingOrder     # noqa: E402
from ai_trader.exchange.paper import PaperExecutor             # noqa: E402
from ai_trader.exchange.paper_pending import (                 # noqa: E402
    PENDING_TYPES, PendingBook)

M5 = 300_000


class Bar:
    def __init__(self, ts, o, h, l, c):
        self.ts, self.o, self.h, self.l, self.c = ts, o, h, l, c


class _Agg:
    def __init__(self, bar):
        self._bar = bar

    def current(self, iv):
        return self._bar


class _Feed:
    def __init__(self, bar):
        self.agg = _Agg(bar)
        self.last_price = bar.c


# ============================================================ 一、契约层

CONTRACT = ("place_order", "cancel_order", "query_order", "open_orders")


def test_base_executor_declares_the_pending_contract():
    """★ 四个方法必须在**基类**上，否则「纸面没有挂单」这种事只能等生产发现。"""
    for name in CONTRACT:
        assert hasattr(Executor, name), "基类缺 %s" % name
        attr = getattr(Executor, name)
        assert inspect.iscoroutinefunction(attr), "%s 必须是 async" % name


def test_both_executors_implement_the_contract():
    from ai_trader.exchange.live import LiveExecutor
    for cls in (PaperExecutor, LiveExecutor):
        for name in CONTRACT:
            assert name in cls.__dict__, (
                "%s 覆盖了基类的 %s —— 没覆盖的实现等于继承了一个 NotImplementedError"
                % (cls.__name__, name))


def test_pending_types_are_one_source():
    """★ 两套词汇不许分叉：纸面能挂的类型集合必须和实盘逐字相同。

    这个项目在这件事上栽过（HANDOFF §23：`direction` 大写、常量小写，
    直接比较永远不等，7 段窗口全部静默错过）。
    """
    from ai_trader.exchange.live import LiveExecutor
    assert tuple(PENDING_TYPES) == tuple(LiveExecutor._PENDING_TYPES)


def test_place_order_signature_matches():
    from ai_trader.exchange.live import LiveExecutor
    a = inspect.signature(PaperExecutor.place_order)
    b = inspect.signature(LiveExecutor.place_order)
    assert list(a.parameters) == list(b.parameters), (
        "参数名不一致会让调用方在两条路径上写出不同的代码")


# ============================================================ 二、校验层

BAD_INPUTS = [
    dict(side="HOLD", qty=1.0, order_type="LIMIT", price=100.0),
    dict(side="BUY", qty=1.0, order_type="TRAILING_STOP", price=100.0),
    dict(side="BUY", qty=1.0, order_type="LIMIT", price=None),
    dict(side="BUY", qty=1.0, order_type="STOP_MARKET", price=100.0),
]


@pytest.mark.parametrize("kw", BAD_INPUTS)
def test_rejects_the_same_things_as_the_real_exchange(kw):
    """同一批非法入参，live 与 paper 都必须拒绝，且理由前缀一致。

    ★ live 的这几条校验在 `await self.spec()` 之前 —— 不联网也能测。
    """
    from ai_trader.exchange.live import LiveExecutor

    cfg = load()
    feed = _Feed(Bar(0, 100.0, 101.0, 99.0, 100.0))
    paper = PaperExecutor(feed, cfg)
    live = LiveExecutor(feed, cfg, "k", "s")

    o1, why1 = _run(paper.place_order(**kw))
    o2, why2 = _run(live.place_order(**kw))
    assert o1.status == "REJECTED" and why1, "纸面没有拒绝：%r" % (kw,)
    assert o2.status == "REJECTED" and why2, "实盘没有拒绝：%r" % (kw,)
    # 只比前 4 个字：两边措辞可以不同，但**说的一定是同一件事**
    assert why1[:4] == why2[:4], "拒绝理由分叉：paper=%r live=%r" % (why1, why2)


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


# ============================================================ 三、成交语义

def _book(**kw):
    return PendingBook(**kw)


def test_limit_buy_fills_on_wick_touch():
    b = _book()
    o, err = b.place("BUY", 1.0, "LIMIT", price=99.0, client_id="x", ts=0)
    assert err == "" and o.status == "NEW"
    fills = b.on_bar(Bar(M5, 100.0, 101.0, 98.9, 100.5))   # 影线 98.9 ≤ 99
    assert len(fills) == 1
    f = fills[0]
    assert (f.client_id, f.side, f.price) == ("x", "BUY", 99.0)
    assert f.how == "touch" and f.ambiguous is False
    assert o.status == "FILLED" and o.executed_qty == 1.0


def test_limit_buy_not_filled_when_wick_misses():
    b = _book()
    b.place("BUY", 1.0, "LIMIT", price=99.0, client_id="x", ts=0)
    assert b.on_bar(Bar(M5, 100.0, 101.0, 99.1, 100.5)) == []


def test_limit_buy_gap_fills_at_the_better_open():
    """★ 跳空低开时限价买单成交在 `open`，不是挂单价。

    用挂单价成交等于凭空给了一个开盘根本拿不到的价格 —— 那是回测作弊。
    """
    b = _book()
    b.place("BUY", 1.0, "LIMIT", price=99.0, client_id="x", ts=0)
    f = b.on_bar(Bar(M5, 95.0, 96.0, 94.0, 95.5))[0]
    assert f.price == 95.0 and f.how == "gap"


def test_limit_sell_gap_fills_at_the_better_open():
    b = _book()
    b.place("SELL", 1.0, "LIMIT", price=101.0, client_id="x", ts=0)
    f = b.on_bar(Bar(M5, 105.0, 106.0, 104.0, 105.5))[0]
    assert f.price == 105.0 and f.how == "gap"


def test_stop_market_buy_triggers_upward():
    b = _book(slippage_ticks=2.0)
    b.place("BUY", 1.0, "STOP_MARKET", stop_price=102.0, client_id="s", ts=0)
    assert b.on_bar(Bar(M5, 100.0, 101.9, 99.0, 101.5)) == [], "没上破不该触发"
    f = b.on_bar(Bar(M5, 102.0, 103.0, 101.5, 102.5))[0]
    assert f.price == 104.0, "触发价 102 + 滑点 2 = 104（永远更差）"
    assert f.how == "touch", "开盘正好等于触发价，不算跳空"


def test_stop_market_sell_triggers_downward_and_slips_worse():
    b = _book(slippage_ticks=1.5)
    b.place("SELL", 1.0, "STOP_MARKET", stop_price=98.0, client_id="s", ts=0)
    f = b.on_bar(Bar(M5, 97.0, 97.5, 96.0, 96.5))[0]
    assert f.price == 95.5, "触发价 98 - 滑点 1.5；开盘 97 更差 → 取 97-1.5"
    assert f.how == "gap"


def test_take_profit_triggers_the_opposite_way_of_a_stop():
    """止盈与止损的**触发方向相反**，这里是最容易抄错的一处。"""
    b = _book()
    b.place("SELL", 1.0, "TAKE_PROFIT_MARKET", stop_price=105.0,
            client_id="t", ts=0)
    assert b.on_bar(Bar(M5, 100.0, 104.9, 99.0, 100.0)) == []
    f = b.on_bar(Bar(M5, 100.0, 105.5, 99.0, 105.0))[0]
    assert f.price == 105.0

    b2 = _book()
    b2.place("BUY", 1.0, "TAKE_PROFIT_MARKET", stop_price=95.0,
             client_id="t2", ts=0)
    assert b2.on_bar(Bar(M5, 100.0, 101.0, 95.5, 100.0)) == []
    assert b2.on_bar(Bar(M5, 100.0, 101.0, 94.5, 95.0))[0].price == 95.0


def test_limit_costs_maker_and_stop_costs_taker():
    """赢的一路走 maker、亏的一路走 taker —— 和 `PricePlan` 的成本模型同口径。"""
    b = _book(fee_maker=0.0002, fee_taker=0.0005)
    b.place("BUY", 1.0, "LIMIT", price=100.0, client_id="m", ts=0)
    b.place("SELL", 1.0, "STOP_MARKET", stop_price=99.0, client_id="t", ts=0)
    fills = b.on_bar(Bar(M5, 100.0, 100.5, 98.5, 99.0))
    by = {f.client_id: f for f in fills}
    assert by["m"].fee == pytest.approx(100.0 * 0.0002)
    assert by["t"].fee == pytest.approx(99.0 * 0.0005)


def test_same_bar_both_sides_is_flagged_ambiguous():
    """★ 同一根 K 里买卖两边都够到 → 先后分不清，必须标记并单独记账。

    真实市场只会先发生一边。给了「影线触及」这个判据，就必然出现这种根，
    不许当成两次正常成交混进统计。
    """
    b = _book()
    b.place("BUY", 1.0, "LIMIT", price=99.0, client_id="b", ts=0)
    b.place("SELL", 1.0, "LIMIT", price=101.0, client_id="s", ts=0)
    fills = b.on_bar(Bar(M5, 100.0, 101.5, 98.5, 100.0))
    assert len(fills) == 2
    assert all(f.ambiguous for f in fills), "同根两个方向都成交却没标记"


def test_one_sided_bar_is_not_ambiguous():
    b = _book()
    b.place("BUY", 1.0, "LIMIT", price=99.0, client_id="b", ts=0)
    b.place("SELL", 1.0, "LIMIT", price=105.0, client_id="s", ts=0)
    fills = b.on_bar(Bar(M5, 100.0, 101.5, 98.5, 100.0))
    assert [f.client_id for f in fills] == ["b"]
    assert fills[0].ambiguous is False


def test_canceled_order_never_fills():
    b = _book()
    o, _ = b.place("BUY", 1.0, "LIMIT", price=99.0, client_id="x", ts=0)
    ok, why = b.cancel(client_id="x")
    assert ok and why == ""
    assert b.on_bar(Bar(M5, 100.0, 101.0, 98.0, 100.0)) == []
    ok2, why2 = b.cancel(client_id="x")
    assert not ok2, "已经不在挂着的单再撤一次必须失败，不能静默成功"


def test_duplicate_client_id_is_rejected():
    b = _book()
    b.place("BUY", 1.0, "LIMIT", price=99.0, client_id="x", ts=0)
    o, why = b.place("BUY", 1.0, "LIMIT", price=98.0, client_id="x", ts=0)
    assert o.status == "REJECTED" and "client_id" in why


def test_open_orders_excludes_terminal_states():
    b = _book()
    b.place("BUY", 1.0, "LIMIT", price=99.0, client_id="a", ts=0)
    b.place("BUY", 1.0, "LIMIT", price=90.0, client_id="b", ts=0)
    b.on_bar(Bar(M5, 100.0, 101.0, 98.0, 100.0))
    assert [o.client_id for o in b.open_orders()] == ["b"]
    assert len(b.all_orders()) == 2, "all_orders 要含终态，对账要看「挂过什么」"


def test_zero_qty_is_rejected():
    b = _book()
    o, why = b.place("BUY", 0.0, "LIMIT", price=99.0, client_id="x", ts=0)
    assert o.status == "REJECTED" and "数量" in why


# ============================================================ 四、账务同源

def _paper():
    cfg = load()
    feed = _Feed(Bar(0, 100.0, 101.0, 99.0, 100.0))
    return PaperExecutor(feed, cfg)


def test_paper_fill_goes_through_the_single_accounting_path():
    """挂单成交必须走和市价成交**同一个** `_apply_fill`，不许再抄一遍公式。"""
    ex = _paper()
    before = ex.balance
    _run(ex.place_order("BUY", 0.01, "LIMIT", price=99.0, client_id="e1"))
    out = ex.on_bar_5m(Bar(M5, 100.0, 101.0, 98.0, 100.0))
    assert len(out) == 1
    f, r = out[0]
    assert r.ok and ex.pos_qty == pytest.approx(0.01)
    assert ex.pos_entry == pytest.approx(f.price)
    assert ex.balance < before, "开仓要扣手续费"


def test_reduce_only_stop_never_flips_the_position():
    """★ 平仓止损单按原量成交会**把仓位翻到反方向** —— 第 109 轮的真实形态。

    截断逻辑现在只有一份（`_apply_fill`），挂单路径同样受它保护。
    """
    ex = _paper()
    ex.pos_qty = 0.01
    ex.pos_entry = 100.0
    _run(ex.place_order("SELL", 0.05, "STOP_MARKET", stop_price=99.0,
                        reduce_only=True, client_id="sl"))
    out = ex.on_bar_5m(Bar(M5, 100.0, 100.5, 98.0, 98.5))
    assert out and out[0][1].ok
    assert ex.pos_qty == pytest.approx(0.0), (
        "仓位翻成 %.8f —— reduce_only 没有截断" % ex.pos_qty)


def test_reduce_only_on_flat_position_is_rejected():
    ex = _paper()
    _run(ex.place_order("SELL", 0.01, "STOP_MARKET", stop_price=99.0,
                        reduce_only=True, client_id="sl"))
    out = ex.on_bar_5m(Bar(M5, 100.0, 100.5, 98.0, 98.5))
    assert out and not out[0][1].ok
    assert "无仓可平" in out[0][1].reason


def test_open_orders_returns_a_plain_list_like_the_real_exchange():
    """★ 返回 `List[PendingOrder]`，**不是**元组。

    `engine._sync_working_orders` 直接 `len(remote)` 和 `reconcile_orders(local, remote)`，
    给它一个元组会静默算错（元组长度恒为 2）。所以这里比对的是**实盘的类型**。
    """
    from ai_trader.exchange.live import LiveExecutor
    ex = _paper()
    got = _run(ex.open_orders())
    assert isinstance(got, list) and got == []
    ret = inspect.signature(LiveExecutor.open_orders).return_annotation
    assert "List" in str(ret) and "Tuple" not in str(ret)


def test_cancel_and_query_return_the_same_types_as_the_real_exchange():
    """`cancel_order → bool`、`query_order → PendingOrder|None`。

    ★ 这里出过一次错：基类第一版写成「都返回 (值, 说明) 元组」，
      而实盘返回裸值。`engine._resolve_vanished` 写的是 `if ok:` ——
      元组恒为真，于是「撤单失败」会被当成成功。
    """
    from ai_trader.exchange.live import LiveExecutor
    ex = _paper()
    _run(ex.place_order("BUY", 1.0, "LIMIT", price=99.0, client_id="c1"))
    assert _run(ex.cancel_order(client_id="c1")) is True
    assert _run(ex.cancel_order(client_id="c1")) is False
    assert _run(ex.place_order("BUY", 1.0, "LIMIT", price=99.0,
                               client_id="c2"))[0].status == "NEW"
    q = _run(ex.query_order(client_id="c2"))
    assert q is not None and q.working
    assert _run(ex.query_order(client_id="nope")) is None
    for name, want in (("cancel_order", "bool"), ("query_order", "Optional")):
        ret = str(inspect.signature(getattr(LiveExecutor, name)).return_annotation)
        assert want in ret, "%s 实盘返回 %r，纸面对齐目标错了" % (name, ret)


def test_spot_executor_declares_no_pending_capability():
    """★ 现货执行器没有挂单实现 —— 必须显式标记，不能靠 `hasattr` 猜。

    基类声明了 `open_orders` 之后，`hasattr` 恒为真；引擎若照旧用 hasattr 判断，
    启动对账会在现货上抛「不支持查挂单列表」并写一条假的 `orders_fetch_failed`，
    而哨兵正是靠「risk_events 为空」判断健康的。
    """
    from ai_trader.exchange.spot import SpotExecutor
    assert SpotExecutor.supports_pending is False
    assert PaperExecutor.supports_pending is True
