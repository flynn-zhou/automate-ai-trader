# -*- coding: utf-8 -*-
"""接管孤儿仓位时，成本基准必须来自真实成交，不能拿现价猜。

为什么这条重要：现货交易所只记余额、不记开仓价。
进程如果"买入成功后、写库之前"崩掉，重启时会看到一个
"交易所有币、数据库没仓"的孤儿仓位。

旧做法是拿当前市价当成本，这在价格已经走开时会同时算错两件事：
  1. 止损价按错误成本算（40000 买、28000 重启 -> 止损 21000，
     可这仓已经浮亏 30%，再跌 25% 才动，等于保护失效）；
  2. 平仓时 pnl 按错误成本结算，把真实亏损记小。
第 2 条直接废掉"用实测数据找真实边际"这个目标——账错了，边际就是假的。

现在用 /api/v3/myTrades 的真实成交回放算加权成本，
并且要求回放出的净持仓和交易所余额自洽，否则宁可退回估算并大声告警。
"""
import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor
from tools.fake_binance import FakeBinance


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []


@pytest_asyncio.fixture
async def fake():
    f = await FakeBinance(usdt=100000.0, btc=0.0, price=40000.0).start()
    try:
        yield f
    finally:
        await f.stop()


def _ex(fake, px=40000.0):
    cfg = AppCfg()
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.taker_fee = 0.001
    return SpotExecutor(Feed(px), cfg, fake.api_key, fake.api_secret, base=fake.base())


@pytest.mark.asyncio
async def test_cost_basis_after_single_buy(fake):
    """买入 0.001 @40000，手续费 0.1% 扣在 BTC 上。

    到手 0.000999，而不是 0.001。这里必须按【净持仓】断言：
    cost_basis 的判据就是"回放出的净持仓要和交易所余额自洽"，
    交易所余额确实只有 0.000999。

    第 58 轮之前这里断言的是毛量 0.001，看起来通过了，
    其实是因为 reconcile_live 的容差 0.5% 放过了 0.1% 的偏差。
    累计 6 笔之后偏差越过 0.5%，成本基准就会被永久弃用。
    """
    ex = _ex(fake)
    try:
        await ex.market_order("buy", 0.001, "t")
        cb = await ex.cost_basis()
        assert cb["ok"]
        net = 0.001 * (1 - 0.001)
        assert cb["qty"] == pytest.approx(net, rel=1e-6), "应为到手净量，不是成交毛量"
        # 实付 40U 只换来 0.000999，真实成本必然高于成交价 40000
        assert cb["avg"] == pytest.approx(40000.0 / 0.999, rel=1e-6), (
            "成本要按实付金额算：被当手续费扣掉的币也是钱换来的")
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_cost_basis_is_weighted_across_two_buys(fake):
    """两次不同价买入，成本必须是加权平均，不是最后一次价。"""
    ex = _ex(fake)
    try:
        await ex.market_order("buy", 0.001, "b1")     # @40000
        fake.price = 50000.0
        await ex.market_order("buy", 0.001, "b2")     # @50000
        cb = await ex.cost_basis()
        assert cb["qty"] == pytest.approx(0.002 * 0.999, rel=1e-6)
        # 实付 40+50=90U，到手 2*0.000999 -> 加权成本
        assert cb["avg"] == pytest.approx(90.0 / (0.002 * 0.999), rel=1e-6), (
            "不是加权平均")
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_cost_basis_after_partial_sell(fake):
    """卖出一半后，剩下的成本价不变（加权平均法的性质）。"""
    ex = _ex(fake)
    try:
        await ex.market_order("buy", 0.002, "b")
        cb1 = await ex.cost_basis()
        await ex.market_order("sell", 0.001, "s")
        cb2 = await ex.cost_basis()
        assert cb2["qty"] == pytest.approx(0.002 * 0.999 - 0.001, rel=1e-6)
        assert cb2["avg"] == pytest.approx(cb1["avg"], rel=1e-6)
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_full_sell_leaves_only_substep_dust(fake):
    """平掉整笔之后，账上不会精确归零，只会剩不足一个 step 的零头。

    ★ 这条用例原来断言的是 qty == 0，而且是绿的——但通过的理由是错的。
    它写的是「买 0.001 然后卖 0.001」，可买入的手续费扣在 BTC 上，
    到账只有 0.000999，那笔 0.001 的卖单在真实币安上会被 -2010 拒掉。
    当时假交易所在余额检查【之前】就写了成交明细，于是 cost_basis 回放时
    看到一张并不存在的成交，刚好算出 0。
    也就是说它验的不是「卖出后归零」，而是「假交易所帮我记了一笔没发生的成交」。

    真实世界是：现货一买一卖永远不可能精确归零。
      买 0.001 @40000 -> 到手 0.000999
      卖按 step 0.00001 向下取整 -> 只卖 0.00099，剩 0.000009
    这 0.000009 小于一个 step，交易所根本不收，永远卖不掉。
    所以正确的断言是「剩余不足一个 step」，而不是「等于 0」。
    这也是 SpotExecutor.position() 把 < step 的余额折叠成空仓的依据。
    """
    ex = _ex(fake)
    step = 0.00001
    try:
        await ex.market_order("buy", 0.001, "b")
        r = await ex.market_order("sell", 0.001, "s")
        assert r.ok, ("卖出应当被截断到账上可卖数量后成交，"
                      "而不是被交易所拒单：%s" % r.reason)
        cb = await ex.cost_basis()
        assert cb["ok"] is True
        assert 0.0 <= cb["qty"] < step, (
            "剩余必须落在 [0, step) 内；实际 %.8f" % cb["qty"])
        # 余额本身也要对得上：这说明成交明细和账没有分家
        st = await ex.equity()
        assert st["position_qty"] == pytest.approx(cb["qty"], abs=1e-9)
        # 不足一个 step 的余额必须被当作空仓，否则引擎会永久卡在这点上
        pos = await ex.position()
        assert pos["qty"] == 0.0
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_cost_basis_unavailable_reports_not_ok(fake):
    """拿不到成交历史时必须明确说"不可信"，不能编一个数字。"""
    fake.trades_available = False
    ex = _ex(fake)
    try:
        cb = await ex.cost_basis()
        assert cb["ok"] is False, "取不到成交却报告可信"
        assert cb["avg"] == 0.0
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_engine_rejects_inconsistent_trade_history(fake):
    """成交历史净持仓和交易所余额对不上时，必须弃用这个成本。

    真实场景：币是转进钱包的、或成交历史被 limit 截断。
    这时候"回放出来的成本"根本不是这个仓的成本，用它比不用更危险。
    """
    from ai_trader.trade_db import TradeDB
    from ai_trader.trading.engine import DecisionEngine
    import tempfile

    cfg = AppCfg()
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.strategy = "trend"
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    ex = _ex(fake)
    eng = DecisionEngine(cfg, Feed(40000.0), ex, tdb, guard=None)
    try:
        # 成交历史只有 0.001，但交易所实际有 0.01（差额来自充值或历史截断）
        await ex.market_order("buy", 0.001, "b")
        avg = await eng._cost_basis_from_trades(0.01)
        assert avg == 0.0, "净持仓对不上却采用了不可信的成本"
    finally:
        await ex.close()
        tdb.stop()


@pytest.mark.asyncio
async def test_engine_accepts_consistent_trade_history(fake):
    from ai_trader.trade_db import TradeDB
    from ai_trader.trading.engine import DecisionEngine
    import tempfile

    cfg = AppCfg()
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.strategy = "trend"
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    ex = _ex(fake)
    eng = DecisionEngine(cfg, Feed(40000.0), ex, tdb, guard=None)
    try:
        await ex.market_order("buy", 0.001, "b")     # @40000
        # 交易所余额是【扣过 BTC 手续费】的净量，用它当基准才和现实一致
        bal = (await ex.equity())["position_qty"]
        assert bal == pytest.approx(0.001 * 0.999, rel=1e-6)
        avg = await eng._cost_basis_from_trades(bal)
        assert avg == pytest.approx(40000.0 / 0.999, rel=1e-6), (
            "采用的成本要含手续费：实付 40U 只到手 0.000999")
    finally:
        await ex.close()
        tdb.stop()


# ---------------- 第 58 轮补：持仓中重启，成本基准必须扛住累计手续费 ----------------

@pytest.mark.asyncio
async def test_restart_takeover_survives_accumulated_fees(fake):
    """持仓中重启：接管成本必须来自真实成交，不能退回"当前价当成本"。

    为什么这条以前测不出来：cost_basis 用成交毛量回放，而账户余额是扣过 BTC
    手续费的净量，两者的差 = 累计手续费。reconcile_live 的容差是 0.5%，
    每笔买入约 0.1%，所以跑到第 6 笔左右偏差就越界了——
    于是【重启时若正持仓】，成本基准被判"对不上"而弃用，
    退回"按当前价当成本"并打 CRITICAL。

    后果正是这个模块开头写的那两条：止损价按错误成本算、平仓 pnl 记小，
    边际统计全废。所以必须跑够笔数才暴露。
    """
    STEP = 0.00001
    px = 40000.0
    fake.price = px
    ex = _ex(fake, px)
    try:
        # 先做 6 轮往返，把累计手续费堆到容差附近
        for _ in range(6):
            bal = (await ex.equity())["balance"]
            q = ex.round_qty(bal / px / 1.0015, STEP)
            f = await ex.market_order("buy", q, "in")
            assert f.ok, f.reason
            s = await ex.market_order("sell", f.qty, "out")
            assert s.ok, s.reason

        # 第 7 笔买入并留着 —— 模拟重启时正持仓
        bal = (await ex.equity())["balance"]
        q = ex.round_qty(bal / px / 1.0015, STEP)
        f = await ex.market_order("buy", q, "in")
        assert f.ok, f.reason
        real_entry = f.price

        # 重启时价格已经跌走，用错误成本会让止损价完全错位
        fake.price = 36000.0
        ex2 = _ex(fake, 36000.0)
        try:
            cb = await ex2.cost_basis()
            actual = (await ex2.equity())["position_qty"]
            dev = abs(cb["qty"] - actual) / actual
            assert dev < 0.005, (
                "回放净持仓 %.8f 与余额 %.8f 偏差 %.3f%%，超过 reconcile_live "
                "的 0.5%% 容差——重启接管时会弃用成本基准"
                % (cb["qty"], actual, dev * 100))
            assert cb["avg"] == pytest.approx(real_entry / 0.999, rel=1e-4), (
                "接管成本 %.2f 偏离真实成本 %.2f——退回现价会让止损价错位"
                % (cb["avg"], real_entry))
        finally:
            await ex2.close()
    finally:
        await ex.close()
