# -*- coding: utf-8 -*-
"""整条实盘启动链路的集成测试：main.run(mode=live) 真的从 0 跑到能决策。

之前测的都是零件：
  LiveExecutor 能下单（自己构造的）
  reconcile_live 能接管（自己构造的）
  set_leverage 被调用（源码字符串 grep）

但用户注资以后实际执行的是 main.run() 这一整条：
  建 Store -> 建 Feed -> 建 TradeDB/Journal -> feed.start()
  -> 挑执行器 -> 建引擎 -> attach() -> reconcile_live()
  -> set_leverage（仅永续）-> 主循环

这条链路从没被一次跑通过。缺 API key 的拒绝路径测过了，
但"配置齐全、正常启动"那条路没有——而它才是注资后必然走的那条。

第一次跑就抓到真 bug：接管孤儿仓位时调用的是 open_trade({...})，
而 TradeDB.open_trade() 是【无参读取】方法，签名不匹配抛 TypeError。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader import main as main_mod
from ai_trader.config import AppCfg
from tools.fake_binance import FakeBinance


class _Hub:
    def publish(self, m):
        pass

    def add(self, *a, **k):
        pass


class _Bar:
    def __init__(self, px):
        self.o = self.h = self.l = self.c = px
        self.n = 1
        self.volume = 1.0
        self.open_time = 0
        self.interval_ms = 3000
        self.closed = True

    def as_dict(self):
        return {"o": self.o, "h": self.h, "l": self.l, "c": self.c}


class _Agg:
    def __init__(self, px):
        self._b = _Bar(px)

    def current(self, iv):
        return self._b if iv == 3000 else None


class StubFeed:
    """行情用桩：本测试关心的是启动顺序和接线，不是 WS 采集。"""
    def __init__(self, cfg, store, px=77815.0):
        self.cfg = cfg
        self.store = store
        self.last_price = px
        self.src = "ws"
        self.bar_hooks = []
        self.hub = _Hub()
        self.agg = _Agg(px)
        self.history = {}
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        pass

    def snapshot(self):
        return {"src": self.src, "price": self.last_price, "bars": {}, "ticks_total": 0,
                "rest_ticks": 0, "ticks_per_s": 0, "ws_connected": True}


class StubStore:
    def __init__(self, path):
        self.path = path
        self.written = 0

    def start(self):
        pass

    def stop(self):
        pass

    def put_bar(self, b):
        self.written += 1

    def put_event(self, *a, **k):
        pass


def _cfg(tmp_path, fake, **kw):
    cfg = AppCfg()
    cfg.trading.mode = "live"
    cfg.trading.exec_market = "perp"
    cfg.trading.strategy = "trend"
    cfg.trading.api_key = fake.api_key
    cfg.trading.api_secret = fake.api_secret
    cfg.trading.sma_lookback = 5
    cfg.store.path = str(tmp_path / "market.db")
    for k, v in kw.items():
        setattr(cfg.trading, k, v)
    return cfg


def _patch(monkeypatch, fake):
    """把真实执行器换成指向假交易所的同一个类。

    ★ 2026-11-17 只做合约：指向 `LiveExecutor`（以前是 `SpotExecutor`）。
      `fake_binance` 本来就把 `/fapi/v1/*` 全部实现了（exchangeInfo /
      premiumIndex / account / positionRisk / order / openOrders / leverage），
      所以这条链路不需要新的假服务器。
    """
    RealLive = main_mod.LiveExecutor

    def PatchedLive(feed, c, k, s, proxy=""):
        return RealLive(feed, c, k, s, base=fake.base(), proxy=proxy)

    monkeypatch.setattr(main_mod, "LiveExecutor", PatchedLive)
    monkeypatch.setattr(main_mod, "Feed", lambda c, st: StubFeed(c, st))
    monkeypatch.setattr(main_mod, "Store", StubStore)


def _risk_kinds(trade_db_path):
    """读风险事件表——engine_error 是最直接的"启动链路出异常"证据。"""
    years = [p for p in os.listdir(os.path.dirname(trade_db_path))
             if p.endswith("_trade.db")]
    if not years:
        return []
    c = sqlite3.connect(os.path.join(os.path.dirname(trade_db_path), years[0]), timeout=5)
    try:
        rows = list(c.execute("SELECT kind, COALESCE(detail,'') FROM risk_events"))
        return rows
    finally:
        c.close()


@pytest.mark.asyncio
async def test_live_perp_startup_has_no_engine_error(tmp_path, monkeypatch):
    """合约实盘启动全程不得产生 engine_error——这是链路通不通的硬指标。"""
    fake = await FakeBinance(usdt=100.0, btc=0.0, price=77815.0).start()
    try:
        cfg = _cfg(tmp_path, fake)
        _patch(monkeypatch, fake)
        await main_mod.run(cfg, seconds=2, with_web=False)

        paths = [p for (_m, p) in fake.calls]
        # ★ 合约的余额事实在 positionRisk + account 两边；启动对账首先要读持仓。
        #   （不钉 `/fapi/v2/account`：那是主循环里 equity() 才走的，
        #    而主循环第一步与 2s 时长有竞争，钉它等于钉时序。）
        assert "/fapi/v2/positionRisk" in paths, "启动时没有读真实持仓"
        # 交易对规格是**懒加载**的（第一笔下单时才读 exchangeInfo），
        # 所以不能在启动阶段要求它——那是钉错了时序。
        # 真正该钉的是：启动链路跑完了、没抛异常（见下面的 engine_error）。

        kinds = dict((k, d) for k, d in _risk_kinds(cfg.store.path))
        assert "engine_error" not in kinds, (
            "启动链路抛了异常：%r" % kinds.get("engine_error"))
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_live_perp_startup_sets_leverage(tmp_path, monkeypatch):
    """★ 反转了：合约启动必须**调用** set_leverage。

    以前这条验的是“现货没杠杆”。现货路径被删了，同一个关切点的
    **反面**才是现在的不变式：真实资金 + 永续而不设杠杆，下单会按
    交易所默认杠杆执行，而我们的仓位是按 `cfg.trading.max_leverage` 算出来的。
    """
    fake = await FakeBinance(usdt=100.0, btc=0.0, price=77815.0).start()
    try:
        cfg = _cfg(tmp_path, fake)
        _patch(monkeypatch, fake)
        await main_mod.run(cfg, seconds=2, with_web=False)

        paths = [p for (_m, p) in fake.calls]
        assert "/fapi/v1/leverage" in paths, (
            "合约启动没设杠杆：下单会按交易所默认杠杆执行，"
            "与我们算出来的仓位不一致；实际调用：%r" % (paths,))
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_live_startup_adopts_existing_position(tmp_path, monkeypatch):
    """账户里已有 BTC 时，启动对账必须把它接过来、算出止损、并且记账成功。

    真实场景：上次进程开仓后崩了，或用户手工买过币。
    不接管的话系统以为空仓，会在有持仓的状态下再买一次。
    """
    fake = await FakeBinance(usdt=0.0, price=77815.0).start()
    # ★ 永续的持仓事实在 positionRisk 里（现货那个 `btc=` 字段对合约无效）。
    #   不设它，这条测试就是在测“空仓不被接管”——假绿。
    fake.positions["BTCUSDT"] = {"amt": 0.002, "entry": 77815.0}
    try:
        cfg = _cfg(tmp_path, fake)
        _patch(monkeypatch, fake)
        monkeypatch.setattr(main_mod, "Feed", lambda c, st: StubFeed(c, st))
        monkeypatch.setattr(main_mod, "Store", StubStore)

        captured = {}
        RealEngine = main_mod.DecisionEngine

        class SpyEngine(RealEngine):
            async def reconcile_live(self):
                r = await super().reconcile_live()
                captured["pos"] = dict(self.pos) if self.pos else None
                # ★ 2026-11-24 拆仓后：接管来的仓进 `adopted_pos`，
                #   `self.pos` 只放系统仓。所以两个槽都抓。
                captured["adopted"] = (dict(self.adopted_pos)
                                       if self.adopted_pos else None)
                captured["exc"] = None
                return r

        monkeypatch.setattr(main_mod, "DecisionEngine", SpyEngine)
        await main_mod.run(cfg, seconds=2, with_web=False)

        pos = captured.get("adopted")
        assert pos is not None, "交易所有 BTC，启动对账却没有接管持仓"
        assert captured.get("pos") is None, (
            "接管来的人工仓不该进 self.pos（那是系统仓的槽）—— "
            "混进去就等于把用户手工仓洗成策略仓")
        assert pos["qty"] == pytest.approx(0.002, rel=1e-9)
        assert pos["stop"] > 0, "接管后止损为 0，等于没有保护"
        assert pos["stop"] < pos["entry"], "多头止损价应该低于成本"

        kinds = dict((k, d) for k, d in _risk_kinds(cfg.store.path))
        assert "engine_error" not in kinds, (
            "接管持仓时抛异常（历史上这里是 open_trade 签名错误）：%r"
            % kinds.get("engine_error"))
    finally:
        await fake.stop()
