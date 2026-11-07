# -*- coding: utf-8 -*-
"""注资那一刻的行为：真实 SpotExecutor + 真实 reconcile_live，账户里只有 USDT、没有 BTC。

为什么单独写这一个场景（第 111 轮）：
  这是整个项目最关键的一分钟——用户把钱打进来，我把 AI_TRADE_MODE 改成 live，
  服务重启，第一个跑的东西就是 main.py:91 的 engine.reconcile_live()。

  已有的测试覆盖了这个分支，但用的是【桩执行器】：
      test_startup_reconcile.py:181   _run(eng.reconcile_live())   # 两边都空 -> 不动
  桩执行器的 position() 直接返回一个数，而真实 SpotExecutor.position() 要：
      - 走 _req("GET","/api/v3/account") 解析 balances
      - 从 spec 里取 baseAsset / quoteAsset
      - 把「小于一个 step」的余额折叠成空仓
  这些细节桩测不到，而它们正好决定「会不会把 USDT 当成 BTC 接管下来」。

  万一 position_qty 读成了 quote（USDT），reconcile_live 会看到"交易所持有 100 个 BTC"，
  于是接管一个 100 BTC 的多仓、算出一个天文数字的止损、然后试图卖出一个不存在的仓位。
  这是能直接亏钱的错，值得用真实执行器走一遍。

顺带钉住三件事（都是注资时刻的真实风险）：
  1. 空仓时不许产生任何 risk_event（否则哨兵会误报"对账不一致"）。
  2. 空仓时必须自报"对账通过"，而不是静默返回（静默=下次真不一致也没人知道）。
  3. 权益必须是 USDT + BTC*价，缺价时不能把账户估值成 0。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import load
from ai_trader.exchange.spot import SpotExecutor
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trade_db import TradeDB


class _Agg:
    def current(self, iv):
        return None


class _Feed:
    def __init__(self, px):
        self.last_price = px
        self.agg = _Agg()
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _spot(balances, feed, monkeypatch):
    """真实的 SpotExecutor，只把 HTTP 那层换成假账户。"""
    cfg = load()
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.exec_market = "spot"
    ex = SpotExecutor(feed, cfg, "k", "s", proxy="")

    async def _req(method, path, params=None, signed=True):
        if path == "/api/v3/account":
            return {"balances": [{"asset": a, "free": str(v), "locked": "0"}
                                 for a, v in balances.items()]}
        raise AssertionError("意外请求：" + path)

    async def _spec():
        return {"step": 0.00001, "min_notional": 5.0, "base": "BTC", "quote": "USDT"}

    monkeypatch.setattr(ex, "_req", _req)
    monkeypatch.setattr(ex, "spec", _spec)
    return ex


def _engine(ex, feed, tdb):
    cfg = load()
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    eng = DecisionEngine(cfg, feed, ex, tdb)
    return eng


def test_funded_but_empty_account_reports_no_position(tmp_path, monkeypatch):
    """★ 只有 USDT 没有 BTC 时，position_qty 必须是 0，绝不能是 USDT 余额。"""
    feed = _Feed(78000.0)
    ex = _spot({"USDT": 100.0, "BTC": 0.0}, feed, monkeypatch)
    e = _run(ex.equity())
    assert e["balance"] == pytest.approx(100.0), u"USDT 余额读错：%r" % (e["balance"],)
    assert e["position_qty"] == pytest.approx(0.0), (
        u"position_qty = %r —— 空仓账户必须有 0 BTC。\n"
        u"如果这里读成了 quote（USDT），reconcile_live 会接管一个 100 BTC 的假仓，"
        u"然后按那个仓算止损、发卖单。" % (e["position_qty"],))
    assert e["equity"] == pytest.approx(100.0), (
        u"空仓权益应等于 USDT 余额，实际 %r" % (e["equity"],))
    p = _run(ex.position())
    assert p["qty"] == pytest.approx(0.0), u"position() 报出了 %r" % (p["qty"],)
    assert p["side"] == "", u"空仓不该有方向，实际 %r" % (p["side"],)


def test_reconcile_on_fresh_account_adopts_nothing_and_is_clean(tmp_path, monkeypatch):
    """★ 注资后第一次对账：不接管、不平仓、不写任何 risk_event。"""
    feed = _Feed(78000.0)
    ex = _spot({"USDT": 100.0, "BTC": 0.0}, feed, monkeypatch)
    tdb = TradeDB(str(tmp_path / "t.db"))
    tdb.start()
    try:
        eng = _engine(ex, feed, tdb)
        eng.pos = None
        _run(eng.reconcile_live())
        assert eng.pos is None, (
            u"空账户被接管出了仓位：%r —— 引擎会因此再也不开新仓" % (eng.pos,))
        tdb.stop()          # 必须先落盘再读，log_risk 是单写者队列
    finally:
        try:
            tdb.stop()
        except Exception:
            pass
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "t.db"))
    rows = list(c.execute("SELECT kind, detail FROM risk_events"))
    assert rows == [], (
        u"空账户对账产生了 risk_event：%r\n"
        u"哨兵靠这些事件识别故障，无事发生却报事件会让真正的故障被淹掉。" % (rows,))


def test_fresh_account_equity_survives_missing_local_price(tmp_path, monkeypatch):
    """本地还没行情就要对账时，空仓账户的权益必须仍是 USDT 全额。

    这是 main.py 的真实顺序：feed.start() 只拉起后台任务、不等第一个 tick，
    紧接着就 reconcile_live()。那一刻 last_price 还是 0。
    空仓时没有 BTC 要估值，所以权益就该等于 USDT 余额，不该被算成 0。
    """
    feed = _Feed(0.0)
    ex = _spot({"USDT": 100.0, "BTC": 0.0}, feed, monkeypatch)
    e = _run(ex.equity())
    assert e["equity"] == pytest.approx(100.0), (
        u"无本地价时空仓账户被估值成 %r（应为 100）" % (e["equity"],))
