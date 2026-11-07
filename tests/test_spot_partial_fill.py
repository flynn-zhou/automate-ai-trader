# -*- coding: utf-8 -*-
"""实盘下单的余额核对必须按【实际成交量】算，不能按【请求量】。

缺陷（实证，不是推理）：market_order 里
    expected = before + qty          <-- 请求量
    after    = 下单后的真实持仓
    if abs(after - expected) > 容差:
        回滚 + self.halted = 消息     <-- 永久停摆，直到人工重启

实测：
  成交 100% -> ok=True
  成交  60% -> ok=False，halted='余额核对失败：期望 0.00050000，实际 0.00030000'
  成交   0% -> ok=False，halted='余额核对失败：期望 0.00128000，实际 0.00000000'

为什么这是严重缺陷：
  1. 部分成交是市场的正常事件，不是错误。
  2. 触发后 self.halted 被置位，之后【所有】下单都返回「已熔断」，
     只有人工重启才恢复 —— 一次正常的部分成交能让系统永久停止交易。
  3. 它还会"回滚"：把真买到的那部分卖回去，于是该建的仓根本没建。
  4. 现场只留一条 critical 日志 + 一条飞书通知，容易漏看。

但这个检查本身是有价值的，不能删：
它防的是"回报说成交了、余额却没动"——那种情况下确实必须停。
所以修法是【把期望值算对】，而不是放宽检查。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor

PX = 78000.0


class _Feed:
    last_price = PX


def _mk(filled_ratio, balance_delta_ratio=None, start_btc=0.0):
    """造一个假交易所。filled_ratio = 报回成交的比例。

    balance_delta_ratio 单独控制【余额实际变动】的比例，
    用来区分"部分成交"和"回报说谎"这两种情况。
    """
    cfg = AppCfg()
    cfg.trading.symbol = "BTCUSDT"
    ex = SpotExecutor(_Feed(), cfg, "k", "s")
    state = {"btc": start_btc, "usdt": 100.0}
    delta_ratio = filled_ratio if balance_delta_ratio is None else balance_delta_ratio

    async def transport(method, path, params, signed):
        if path == "/api/v3/exchangeInfo":
            return {"symbols": [{"baseAsset": "BTC", "quoteAsset": "USDT",
                "baseAssetPrecision": 8, "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.00001000",
                     "minQty": "0.00001000"},
                    {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]}]}
        if path == "/api/v3/account":
            return {"balances": [
                {"asset": "USDT", "free": "%.8f" % state["usdt"], "locked": "0"},
                {"asset": "BTC", "free": "%.8f" % state["btc"], "locked": "0"}]}
        if path == "/api/v3/order":
            q = float(params["quantity"])
            reported = q * filled_ratio
            actual = q * delta_ratio
            state["btc"] += actual
            state["usdt"] -= actual * PX
            return {"orderId": 1, "executedQty": "%.8f" % reported,
                    "cummulativeQuoteQty": "%.8f" % (reported * PX),
                    "fills": [{"commission": "0", "commissionAsset": "USDT"}]}
        raise AssertionError("unexpected " + path)

    ex._transport = transport
    return ex


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_full_fill_still_works():
    """基准：完整成交必须成功且不熔断。"""
    ex = _mk(1.0)
    r = run(ex.market_order("buy", 0.0005, "full"))
    assert r.ok is True, r.reason
    assert ex.halted == "", "完整成交不该熔断"
    assert r.qty == pytest.approx(0.0005, rel=1e-9)


def test_partial_fill_is_accepted_not_fatal():
    """部分成交：按实际成交建仓，绝不熔断。"""
    ex = _mk(0.6)
    r = run(ex.market_order("buy", 0.0005, "partial"))
    assert ex.halted == "", (
        u"部分成交被当成余额核对失败，执行器已永久熔断：%r。"
        u"一次正常的部分成交会让系统停止交易。" % ex.halted)
    assert r.ok is True, u"部分成交应算成功：%s" % r.reason
    assert r.qty == pytest.approx(0.0003, rel=1e-6), (
        u"仓位应按实际成交量 0.0003 建，实际 %r" % r.qty)


def test_zero_fill_fails_cleanly_without_halting():
    """完全没成交：干净失败，不熔断（下次还能再试）。"""
    ex = _mk(0.0)
    r = run(ex.market_order("buy", 0.00128, "zero"))
    assert r.ok is False, "没成交不能报成功"
    assert ex.halted == "", (
        u"完全没成交却永久熔断：%r。这只是「这次没成交」，"
        u"下一轮应该还能继续试。")
    assert "熔断" not in (r.reason or "")


def test_balance_really_not_moving_still_halts():
    """回报说成交了，但余额真的没动 —— 这种情况必须熔断，检查不能放宽。"""
    ex = _mk(filled_ratio=1.0, balance_delta_ratio=0.0)
    r = run(ex.market_order("buy", 0.0005, "phantom"))
    assert r.ok is False
    assert ex.halted, (
        u"回报说成交、余额没动，这是真问题，必须熔断；实际 halted=%r" % ex.halted)
