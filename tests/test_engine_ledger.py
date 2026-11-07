# -*- coding: utf-8 -*-
"""引擎级账目自洽：一笔趋势往返之后，trades 表里的数必须能对上。

为什么必须单独测：
  线上库里有 4 笔剥头皮交易，它们的 fee 只记了一边手续费，
  而 balance 又和「本金+已实现」差 7.77U（100U 账户的 7.8%）。
  那批数据是旧代码留下的，但「账对不对」这件事必须现在就锁住——
  因为错的方向永远是【看起来赚了】：少记手续费 = 净利润偏高 =
  纸面样本偏乐观 = 我误判策略有边际。

这三个数是同一个事实的三种表述，必须相等：
  A) 执行器余额的变化
  B) trades.pnl_net
  C) trades.pnl_gross - trades.fee
且 trades.fee 必须等于【开仓费 + 平仓费】。
"""
import asyncio
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy


class _Hub:
    def publish(self, *a, **k):
        pass


class Feed:
    def __init__(self, px, cfg):
        self.last_price = px
        self.cfg = cfg
        self.hub = _Hub()
        self.src = "WS"
        self.bar_hooks = []
        outer = self

        class _B:
            def __init__(self, c):
                self.close = c

        class _Agg:
            def current(self, iv):
                return _B(outer.last_price)

        self.agg = _Agg()


def _cfg():
    cfg = load()
    cfg.trading.strategy = "trend"
    cfg.trading.sma_lookback = 200
    cfg.trading.trend_stop_pct = 25.0
    cfg.trading.trend_leverage = 1.0
    cfg.trading.paper_balance = 100.0
    cfg.trading.exec_market = "spot"
    return cfg


def _make(cfg, dbpath, px):
    feed = Feed(px, cfg)
    tdb = TradeDB(dbpath)
    tdb.start()
    ex = PaperExecutor(feed, cfg)
    ex.restore_from_db(tdb)
    eng = DecisionEngine(cfg, feed, ex, tdb)
    return eng, ex, tdb, feed


def _seed_trend(eng, px):
    tg = TrendStrategy(eng.tcfg, symbol="BTCUSDT")
    tg.seeded = True
    tg.need_reconcile = False
    bars = []
    for i in range(201):
        bars.append({"t": i * 14400000, "o": px * 0.8, "h": px * 0.8,
                     "l": px * 0.8, "c": px * 0.8, "v": 100.0})
    bars.append({"t": 201 * 14400000, "o": px, "h": px, "l": px, "c": px, "v": 100.0})
    tg.bars = bars
    tg.last_eval_bar = bars[-1]["t"]
    eng.trend = tg
    eng._seed_retry_ts = 2 ** 31
    return tg


def _trade_row(dbpath):
    import sqlite3
    c = sqlite3.connect(dbpath)
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM trades WHERE exit_ts IS NOT NULL"
                  " ORDER BY id DESC LIMIT 1").fetchone()
    c.close()
    return r


def _fills(dbpath):
    import sqlite3
    c = sqlite3.connect(dbpath)
    rows = c.execute("SELECT side, price, qty, fee FROM fills ORDER BY id").fetchall()
    c.close()
    return rows


@pytest.mark.asyncio
async def test_round_trip_ledger_reconciles():
    d = tempfile.mkdtemp()
    dbpath = os.path.join(d, "ledger.db")
    cfg = _cfg()
    eng, ex, tdb, feed = _make(cfg, dbpath, 100000.0)
    try:
        bal0 = ex.balance
        _seed_trend(eng, 100000.0)
        eq = await ex.equity()
        await eng._step_trend({}, 100000.0, float(eq["equity"]), 1000)
        assert eng.pos is not None, "没开仓，测试前提不成立"

        # 价格下跌触发 25% 熔断止损
        feed.last_price = 70000.0
        bars = {}
        for i, sl in enumerate(eng.pos["stop"], 0) if False else []:
            pass
        ok = await eng._close("熔断止损", eng.pos["stop"], 70000.0, {})
        assert ok, "平仓失败"
        bal1 = ex.balance
        await asyncio.sleep(0.6)   # 单写队列落盘

        import sqlite3 as _sq
        _c = _sq.connect(dbpath)
        _n_all = _c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        _n_closed = _c.execute("SELECT COUNT(*) FROM trades WHERE exit_ts IS NOT NULL").fetchone()[0]
        _n_fill = _c.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        _c.close()
        assert _n_all > 0, ("trades 表为空（fills=%d）—— 开仓都没记录" % _n_fill)
        assert _n_closed > 0, (
            "trades 有 %d 笔但 0 笔已平仓（fills=%d）—— _close 没有写回 exit_ts"
            % (_n_all, _n_fill))

        row = _trade_row(dbpath)
        assert row is not None, "trades 表没有已平仓记录"

        fills = _fills(dbpath)
        fill_fee_sum = sum((f[3] or 0.0) for f in fills)
        assert len(fills) >= 2, "fills 不足两笔，无法核对双边手续费"

        # 1) trades.fee 必须等于两边手续费之和
        assert row["fee"] == pytest.approx(fill_fee_sum, abs=2e-6), (
            u"trades.fee=%.6f，而两笔 fill 的手续费之和=%.6f。"
            u"少记一边等于净利润偏高，纸面样本会偏乐观。"
            % (row["fee"], fill_fee_sum))

        # 2) pnl_net = pnl_gross - fee
        assert row["pnl_net"] == pytest.approx(row["pnl_gross"] - row["fee"], abs=2e-6), (
            u"pnl_net=%.6f 不等于 pnl_gross-fee=%.6f"
            % (row["pnl_net"], row["pnl_gross"] - row["fee"]))

        # 3) 余额变化 = pnl_net
        assert (bal1 - bal0) == pytest.approx(row["pnl_net"], abs=2e-6), (
            u"余额变化 %.6f 不等于 trades.pnl_net %.6f。"
            u"这两者不一致，纸面权益曲线就和交易记录说的不是同一件事。"
            % (bal1 - bal0, row["pnl_net"]))
    finally:
        tdb.stop()
