# -*- coding: utf-8 -*-
"""现货一轮往返后的粉尘，会让系统在【第一笔交易之后】就卡死。

这条链是这么来的：
  1. 买入 0.00128 BTC，手续费 0.1% 扣在 BTC 上（spot.py 已正确处理），
     净到账 0.00127872 BTC。
  2. 平仓时按 step=0.00001 向下取整 -> 只能卖 0.00127，
     剩 0.00000872 BTC 卖不掉（比一个 step 还小，交易所不收）。
  3. 引擎 self.pos = None（它认为平完了）。
  4. 每 5 分钟的周期对账比较：数据库 0 vs 交易所 0.00000872，
     容差是 1e-9 —— 粉尘比容差大 8700 倍，于是判定【不一致】。
  5. 不一致的处理是"以交易所为准，接管这个仓"。
     于是引擎把 0.68U 的粉尘当成真实持仓接管了。
  6. 之后策略认为自己持仓中，不再开新仓；
     而这个仓要平仓时 qty 取整成 0，market_order 直接拒绝。

这不是理论推演——粉尘的量级可以算出来：
  0.00000872 BTC x 78000 = 0.68 USDT，而 step 折合 0.78 USDT。
只要现货手续费扣在基础币上，每一笔往返都会产生这个量级的残留。

【第 57 轮实测修正】：上面第 3~6 步在【一笔】之后并不会发生。
SpotExecutor.position() 会把"小于一个 step"的余额折叠成 0，
所以第一笔之后对账比较的是 0 vs 0，通过。

真正的卡死发生在【第二笔之后】：
  第 1 笔后余 0.00000872（< step -> position() 报 0）        对账通过
  第 2 笔后余 0.00001746（> step -> position() 报 0.00001746）对账失败 -> 接管幽灵仓
因为零头是【单调累积】的：每笔平仓只卖本笔的计划量（按 step 取整），
上一笔留下的零头根本没被碰过，于是越堆越多。

实测（价格 78000、step 0.00001、100U 起，走真实 SpotExecutor + 会扣费的交易所）：
  0.00000872 -> 0.00001746 -> 0.00002621 -> 0.00003497 -> 0.00004374
一年约 30 笔 -> 累积约 12U，约占权益 9%，是永久不参与策略的死钱。

所以本文件既要钉住"零头不算持仓"（执行器层），
也要钉住"零头不能累积"和"对账不能被零头骗"（引擎层）。
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor

STEP = 0.00001
DUST = 0.00000872      # 一轮往返后的残留，小于一个 step


class _Feed:
    def __init__(self, px):
        self.last_price = px


def _ex(balances, px=78000.0):
    cfg = AppCfg()
    cfg.trading.exec_market = "spot"
    cfg.trading.symbol = "BTCUSDT"
    ex = SpotExecutor(_Feed(px), cfg, "k", "s")
    ex._spec_cache = {"step": STEP, "min_qty": STEP, "min_notional": 5.0,
                      "qty_precision": 8, "base": "BTC", "quote": "USDT"}

    async def fake_req(method, path, params=None, signed=False):
        if path == "/api/v3/account":
            return {"balances": [{"asset": a, "free": str(v), "locked": "0"}
                                 for a, v in balances.items()]}
        if path == "/api/v3/ticker/price":
            return {"price": str(px)}
        if path == "/api/v3/myTrades":
            return []
        raise AssertionError("unexpected %s" % path)

    ex._req = fake_req
    return ex


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_dust_should_not_be_treated_as_a_position():
    """小于一个 step 的残留无法交易，应视为空仓。

    否则：引擎会接管它 -> 认为自己持仓 -> 不开新仓；
    而要平掉它时 qty 取整成 0，下单被拒 -> 永久卡死。
    """
    ex = _ex({"USDT": 99.1, "BTC": DUST})
    p = _run(ex.position())
    assert p["qty"] == pytest.approx(0.0, abs=1e-12), (
        u"粉尘应视为空仓：它小于一个 step，交易所根本卖不掉，",
        u"留着它只会让对账永远不一致、引擎误以为自己持仓中")


def test_real_position_still_reported():
    """真实持仓（远大于一个 step）必须照常报告，别把功能修没了。"""
    ex = _ex({"USDT": 0.1, "BTC": 0.00128})
    p = _run(ex.position())
    assert p["qty"] == pytest.approx(0.00128)


def test_dust_equity_still_counts_value():
    """粉尘的钱不能凭空消失——权益里仍要算它的市值。"""
    ex = _ex({"USDT": 99.1, "BTC": DUST})
    e = _run(ex.equity())
    assert e["equity"] == pytest.approx(99.1 + DUST * 78000.0, rel=1e-6), (
        u"把粉尘当空仓处理的是【可交易性】，不是估值。",
        u"钱还是我的，权益必须算进去")


# ---------------- 下面是第 57 轮补的：累积之后的后果 ----------------
# 上面三个用 SpotExecutor + 假 _req，覆盖"执行器怎么看零头"。
# 下面两个走真实 FakeBinance（它像币安一样把手续费扣在 BTC 上）
# 和真实 DecisionEngine，覆盖"累积之后会不会把系统搞死"。
from ai_trader.trade_db import TradeDB                      # noqa: E402
from ai_trader.trading.engine import DecisionEngine         # noqa: E402
from ai_trader.trading.risk import Plan                     # noqa: E402
from tools.fake_binance import FakeBinance                  # noqa: E402

PX = 78000.0


class _EngFeed:
    src = "WS"

    def __init__(self):
        self.last_price = PX
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self.agg = type("A", (), {"current": staticmethod(lambda iv: None)})()


async def _with_engine(fn):
    import tempfile
    cfg = AppCfg()
    cfg.trading.exec_market = "spot"
    cfg.trading.strategy = "trend"
    fake = FakeBinance(api_key="k", api_secret="s", usdt=100.0, btc=0.0, price=PX)
    await fake.start()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    try:
        feed = _EngFeed()
        ex = SpotExecutor(feed, cfg, api_key="k", api_secret="s", base=fake.base())
        eng = DecisionEngine(cfg, feed, ex, tdb, guard=None)
        return await fn(ex, eng)
    finally:
        await fake.stop()
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


async def _two_trips_then_reconcile(ex, eng):
    for _ in range(2):
        acct = await ex.equity()
        qty = ex.round_qty(acct["balance"] / PX / 1.0015, STEP)
        f = await ex.market_order("buy", qty, "in")
        assert f.ok, f.reason
        s = await ex.market_order("sell", f.qty, "out")
        assert s.ok, s.reason
    dust = (await ex.equity())["position_qty"]
    reported = (await ex.position())["qty"]
    await eng.reconcile_live()
    await asyncio.sleep(0.2)
    return dust, reported, eng.pos


def test_reconcile_does_not_take_over_dust():
    """零头累积到超过一个 step 后，对账不能把它当成持仓接管。

    这是"策略静默死亡"的最后一道闸：一旦 self.pos 被零头占住，
    引擎用 if self.pos 拦着，从此再也不开新仓——
    既不交易也不报错，是最难发现的一种失效。
    """
    dust, reported, pos = _run(_with_engine(_two_trips_then_reconcile))
    # 先证明前置条件成立：零头确实已经大到能被 position() 看见
    assert reported > 0, (
        "没造出可被 position() 看见的零头（前置条件不成立，这个测试证明不了什么）")
    assert dust * PX < 5.0, (
        "零头名义 %.2fU 应小于最小下单 5U" % (dust * PX))
    assert pos is None, (
        "对账把 %.8f BTC（约 %.2fU）的零头接管成了持仓 entry=%s —— "
        "这会让引擎永远不再开新仓" % (dust, dust * PX, pos and pos.get("entry")))


async def _five_round_trips(ex, eng):
    worst = 0.0
    for r in range(1, 6):
        eq = await ex.equity()
        qty = ex.round_qty(eq["balance"] / PX / 1.0015, STEP)
        plan = Plan(ok=True, reason="t", side="long", qty=qty, entry=PX,
                    notional=qty * PX, leverage=1.0, stop=PX * 0.75, target=PX * 1.5)
        await eng._open({"name": "trend_up", "reason": "t"}, plan, PX,
                        eq["equity"], r * 1000, {}, {})
        assert eng.pos, "第 %d 轮开仓失败" % r
        ok = await eng._close("t", PX, PX, {})
        assert ok, "第 %d 轮平仓失败" % r
        worst = max(worst, (await ex.equity())["position_qty"])
        await asyncio.sleep(0.1)
    return worst


def test_dust_does_not_accumulate_across_round_trips():
    """连续 5 轮开平后，账上余留必须仍小于一个 step。

    修复前每轮净增约 0.0000087 BTC，第 2 轮就跨过 step，
    5 轮后到 0.00004374；一年 30 笔约 12U 永久卡死。
    """
    worst = _run(_with_engine(_five_round_trips))
    assert worst < STEP, (
        "零头累积到 %.8f BTC，超过一个 step %.5f —— 既会卡死对账，"
        "又是永久不参与策略的死钱" % (worst, STEP))


async def _open_then_reconcile_while_holding(ex, eng):
    # 先制造一笔残留
    acct = await ex.equity()
    qty = ex.round_qty(acct["balance"] / PX / 1.0015, STEP)
    f = await ex.market_order("buy", qty, "in")
    assert f.ok, f.reason
    s = await ex.market_order("sell", f.qty, "out")
    assert s.ok, s.reason

    # 开仓（会把残留认领进 pos["dust"]）
    eq = await ex.equity()
    q2 = ex.round_qty(eq["balance"] / PX / 1.0015, STEP)
    plan = Plan(ok=True, reason="t", side="long", qty=q2, entry=PX,
                notional=q2 * PX, leverage=1.0, stop=PX * 0.75, target=PX * 1.5)
    await eng._open({"name": "trend_up", "reason": "t"}, plan, PX,
                    eq["equity"], 1000, {}, {})
    assert eng.pos, "开仓失败"
    before = (eng.pos["entry"], eng.pos["qty"], eng.pos.get("dust"))

    await eng.reconcile_live()
    await asyncio.sleep(0.3)
    return before, eng.pos


def test_reconcile_while_holding_keeps_position_intact():
    """持仓期间对账不能把仓位重写。

    现货账上余额 = 持仓 + 零头，而 pos["qty"] 只记持仓。
    如果 db_qty 不加零头，持仓期间每 5 分钟对账都会判"不一致"，
    然后接管重写：pos["qty"] 改成账上余额、dust 键丢掉
    （下一笔平仓就不回收零头，累积立刻恢复），入场价也可能被顶替。
    """
    (entry0, qty0, dust0), pos = _run(_with_engine(_open_then_reconcile_while_holding))
    assert dust0 and dust0 > 0, "前置条件不成立：没接管到零头"
    assert pos is not None, "对账把正在持有的仓位抹掉了"
    assert "dust" in pos, "对账重写仓位后丢了 dust 键，下一步就回收不到零头了"
    assert pos["qty"] == pytest.approx(qty0), (
        "对账把持仓量从 %.8f 改成 %.8f" % (qty0, pos["qty"]))
    assert pos["entry"] == pytest.approx(entry0), (
        "对账把入场价从 %.2f 改成 %.2f" % (entry0, pos["entry"]))
