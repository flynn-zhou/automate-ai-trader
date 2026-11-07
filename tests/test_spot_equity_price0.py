# -*- coding: utf-8 -*-
"""重启后、第一个行情 tick 到之前，现货权益会严重低估——已修。

场景（真实可达）：
  进程重启时手里持有 0.00128 BTC，USDT 只剩 0.1。
  feed.start() 只启动后台任务，不阻塞等第一个 tick；
  紧接着 main.py 调 reconcile_live()。那一刻 feed.last_price 仍是 0。
  SpotExecutor.equity() 用 usdt + btc * last_price 算权益，
  于是把持有 100U 的账户算成 0.1U。

现在的行为：本地无价且持仓>0 时，向交易所要一次真实牌价来估值。
只在"持仓>0 且本地无价"这一种情况才发请求，
正常有行情时一次都不多发——否则 3s 循环会把账户接口打爆。

注：我原本怀疑这会触发 guard 的 60%% 回撤硬熔断。查证后不成立：
   GuardState 每次重启都是新的，peak_equity 从 0 开始，
   而回撤判定被 if peak_equity > 0 挡着。
   不能把"看起来会崩"当成"会崩"。
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor


class _Feed:
    def __init__(self, px):
        self.last_price = px


def _ex(feed, balances, mark=0.0, mark_fails=False):
    cfg = AppCfg()
    cfg.trading.exec_market = "spot"
    cfg.trading.symbol = "BTCUSDT"
    ex = SpotExecutor(feed, cfg, "k", "s")
    ex._spec_cache = {"step": 0.00001, "min_qty": 0.00001,
                      "min_notional": 5.0, "qty_precision": 8,
                      "base": "BTC", "quote": "USDT"}
    self_state = {"calls": 0}

    async def fake_req(method, path, params=None, signed=False):
        if path == "/api/v3/account":
            return {"balances": [{"asset": a, "free": str(v), "locked": "0"}
                                 for a, v in balances.items()]}
        if path == "/api/v3/ticker/price":
            self_state["calls"] += 1
            if mark_fails:
                raise RuntimeError("网络失败")
            return {"price": str(mark)}
        raise AssertionError("unexpected %s" % path)

    ex._req = fake_req
    ex._mark_calls = self_state
    return ex


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_equity_uses_mark_price_when_local_price_missing():
    """本地无价且持仓>0 -> 用交易所牌价估值，不能估成 0。"""
    ex = _ex(_Feed(0.0), {"USDT": 0.1, "BTC": 0.00128}, mark=78000.0)
    e = _run(ex.equity())
    assert e["position_qty"] == pytest.approx(0.00128)
    assert e["equity"] == pytest.approx(0.1 + 0.00128 * 78000.0, rel=1e-6), (
        u"本地无价时应向交易所要牌价，而不是把持仓估值成 0。"
        u"估成 0 会让 100U 账户看起来只剩 0.1U。")


def test_no_extra_request_when_price_available():
    """有本地价时，绝不能额外请求牌价接口。"""
    ex = _ex(_Feed(78000.0), {"USDT": 0.1, "BTC": 0.00128}, mark=99999.0)
    e = _run(ex.equity())
    assert e["equity"] == pytest.approx(0.1 + 0.00128 * 78000.0, rel=1e-6)
    assert ex._mark_calls["calls"] == 0, (
        u"有行情时还去问牌价，3s 循环会把接口打爆")


def test_no_request_when_flat():
    """空仓且无价时也没必要问牌价。"""
    ex = _ex(_Feed(0.0), {"USDT": 100.0, "BTC": 0.0}, mark=78000.0)
    e = _run(ex.equity())
    assert e["equity"] == pytest.approx(100.0)
    assert ex._mark_calls["calls"] == 0


def test_falls_back_gracefully_when_mark_fails():
    """取牌价失败时不能抛异常把决策步骤打崩，但要偏低并留痕。"""
    ex = _ex(_Feed(0.0), {"USDT": 0.1, "BTC": 0.00128}, mark_fails=True)
    e = _run(ex.equity())
    assert e["equity"] == pytest.approx(0.1)
    assert ex._mark_calls["calls"] == 1
