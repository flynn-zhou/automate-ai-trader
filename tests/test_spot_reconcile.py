# -*- coding: utf-8 -*-
"""实盘启动对账：现货必须能对账，且不能把止损算成 0。

事故背景（2026-11-01 代码审查发现）：
reconcile_live() 对 name in ("live","spot") 都会调用 self.ex.position()，
但 SpotExecutor 根本没实现 position()——实盘现货启动时必然抛 AttributeError，
被 except 吞掉后带着可能过期的数据库记录继续跑，
正是这段代码注释里自己警告的"止损打在不存在/错误仓位"的情形。

第二个坑：现货查不到开仓价（交易所只记余额）。如果拿 0 兜底，
止损价会被算成 0，"现价 <= 0" 永远不成立……但反过来如果写成 0 又当成本，
任何价格都会触发止损。总之 entry 不能是 0。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor
from ai_trader.trading.engine import DecisionEngine


class FakeSpot:
    def __init__(self, usdt=0.0, btc=0.0, px=30000.0):
        self.usdt, self.btc, self.px = usdt, btc, px

    async def __call__(self, method, path, params, signed):
        if path.endswith("/exchangeInfo"):
            return {"symbols": [{
                "baseAssetPrecision": 8, "baseAsset": "BTC", "quoteAsset": "USDT",
                "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001000",
                             "minQty": "0.00001000"},
                            {"filterType": "NOTIONAL", "minNotional": "5.0"}]}]}
        if path.endswith("/account"):
            return {"balances": [
                {"asset": "USDT", "free": str(self.usdt), "locked": "0"},
                {"asset": "BTC", "free": str(self.btc), "locked": "0"}]}
        raise AssertionError("未预期端点 " + path)


class Feed:
    def __init__(self, px=30000.0):
        self.last_price = px
        self.cfg = AppCfg()


def _env(btc=0.0, px=30000.0):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    feed = Feed(px)
    ex = SpotExecutor(feed, cfg, "k", "s")
    ex._transport = FakeSpot(btc=btc, px=px)

    # ★ 「最小 guard 桩」是个陷阱（第 115 轮踩到）。
    #
    # 这个桩原先只有 notify/notify_decision，于是当引擎给对账不一致
    # 接上 position_mismatch()（第 6 条闸：硬停）时，这两个测试
    # 直接 AttributeError —— 而它们真正要验的东西（接管仓位、清仓记账）
    # 和 guard 毫无关系。桩越是最小，越会在别人加接线时先崩，
    # 且崩得像是"新功能坏了"。
    #
    # 现在的处理不是"补个空方法让它闭嘴"，而是让桩把调用【记下来】，
    # 这样这两个测试顺便也钉住了"不一致时会真的踩闸"。
    class _Guard:
        def __init__(self):
            self.mismatches = []

        def notify(self, *a, **k): pass
        def notify_decision(self, *a, **k): pass

        def position_mismatch(self, detail):
            self.mismatches.append(detail)

    # 桩必须和【真实 TradeDB】接口一致，否则测试会对着想象通过。
    # 历史教训：这里原先只实现了 open_trade(d)，而真实 TradeDB 的
    # open_trade() 是无参读取方法；引擎里也错误地调用了 open_trade({...})。
    # 两边同时错，测试就一直绿——直到用真实接口的集成测试把它戳穿。
    class _DB:
        def __init__(self):
            self.closed, self.logged, self.risks = [], [], []

        def close_trade(self, d):            # 真实签名：close_trade(self, t: dict)
            self.closed.append(d)

        def log_trade(self, t):              # 真实签名：log_trade(self, t: dict) -> None
            self.logged.append(t)

        def open_trade(self):                # 真实签名：open_trade(self) -> Optional[dict]
            return None

        def log_risk(self, kind, detail=""):
            self.risks.append((kind, detail))

        @property
        def opened(self):                    # 兼容旧断言：把写入的记录当"开过的仓"
            return self.logged

    return DecisionEngine(cfg, feed, ex, None, guard=_Guard()), ex, feed, _DB()


@pytest.mark.asyncio
async def test_spot_executor_has_position():
    """方法必须存在，否则实盘启动对账直接抛 AttributeError。"""
    eng, ex, feed, _ = _env(btc=0.003)
    p = await ex.position()
    assert p["qty"] == pytest.approx(0.003)
    assert p["side"] == "long"
    assert p["entry"] is None, "现货查不到开仓价，必须显式返回 None 而不是 0"


@pytest.mark.asyncio
async def test_reconcile_passes_when_matched():
    eng, ex, feed, db = _env(btc=0.003)
    eng.pos = {"side": "long", "qty": 0.003, "entry": 30000.0, "stop": 22500.0,
               "target": 0.0, "entry_ts": 1, "bars": 0, "best": 30000.0,
               "trade_id": None, "max_hold_bars": 10**9, "open_reason": "",
               "fee": 0.0, "signal": "trend"}
    eng.db = db
    await eng.reconcile_live()
    assert eng.last_error == ""
    assert eng.pos is not None, "对账一致时不能动仓位"


@pytest.mark.asyncio
async def test_reconcile_adopts_unknown_position_without_zero_stop():
    """交易所有币、数据库没有：要接管，但止损绝不能算成 0。"""
    eng, ex, feed, db = _env(btc=0.003, px=30000.0)
    eng.pos = None
    eng.db = db
    await eng.reconcile_live()
    assert eng.pos is not None, "必须接管交易所的持仓"
    assert eng.pos["stop"] > 0, "止损价不能是 0（会变成一开机就平仓）"
    assert eng.pos["qty"] == pytest.approx(0.003)

    # 接管必须【真的写入一条成交记录】。
    # 原先只断言 eng.pos 有值——而引擎调的是不存在的方法签名，
    # 异常被吞掉后 pos 照样被赋值，测试因此一直是绿的。
    # 记账没写进去，事后复盘时这笔仓就像不存在，边际统计直接少一笔。
    assert len(db.logged) == 1, "接管持仓没有写入成交记录（实际 %d 条）" % len(db.logged)
    rec = db.logged[0]
    assert rec["qty"] == pytest.approx(0.003)
    assert rec["entry_price"] > 0
    assert rec["stop_price"] > 0
    assert rec["paper"] == 0, "实盘接管的记录必须标记为非纸面"
    assert rec["strategy"] == "trend", "接管记录必须带上策略名"

    # 接管之后必须踩第 6 条闸（账实不符 = 停下来等人查）。
    # 以前这里只接管不停，等于"不一致？修正一下继续交易"。
    assert eng.guard.mismatches, "接管了孤儿仓位却没有硬停"
    assert "对账" in eng.guard.mismatches[0]


@pytest.mark.asyncio
async def test_reconcile_clears_db_pos_when_exchange_flat():
    """交易所空仓但数据库有仓：以交易所为准，把数据库的仓标记为已平。"""
    eng, ex, feed, db = _env(btc=0.0)
    eng.pos = {"side": "long", "qty": 0.003, "entry": 30000.0, "stop": 22500.0,
               "target": 0.0, "entry_ts": 1, "bars": 0, "best": 30000.0,
               "trade_id": None, "max_hold_bars": 10**9, "open_reason": "",
               "fee": 0.0, "signal": "trend"}
    eng.db = db
    await eng.reconcile_live()
    assert eng.pos is None, "交易所已空仓，数据库的仓必须清掉"
