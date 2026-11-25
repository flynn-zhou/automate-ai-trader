# -*- coding: utf-8 -*-
"""引擎启动/周期对账时，挂单账和交易所的对齐（第 131 轮）。

## 为什么这一步不能省

P0–P2 把「能挂单 / 能撤单 / 有本地账」都做出来了，但**没有任何东西去调它**。
`working_orders` 表建了也不会被填 —— 而这张表唯一的价值就是
「重启之后还知道自己挂过什么」。

三条路径都要走通，每条对应一个会真金白银亏钱的场景：

1. **交易所有、本地没有** → 补。漏掉的后果：引擎不知道自己还挂着一张止损单，
   重启后再挂一张 → 同一笔仓两个止损 → 先触发的平掉大部分仓，
   后触发的那张把剩仓**反向开出去**。
2. **本地有、交易所没有** → 标 LOST，再逐张 `query_order()` 查真实终态。
   LOST 是中间态不是结论：它可能已经成交了（那是一笔真实成交！），
   也可能被手工撤了。直接当「没有」= 账面上少一笔真实的平仓。
3. **两边都有但状态不同** → 用交易所的覆盖。

再加一条**失败路径**：拉不到挂单列表时只记日志 + risk，绝不冒泡。
理由是它在 `reconcile_live()` 的同一个调用链里 —— 抛出去会让
**持仓对账**也一并失败，那是比「挂单账暂时不同步」严重得多的事。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.config import AppCfg                    # noqa: E402
from ai_trader.exchange.base import FillResult, PendingOrder   # noqa: E402
from ai_trader.trade_db import TradeDB                 # noqa: E402
from ai_trader.trading.engine import DecisionEngine    # noqa: E402

PX = 75000.0


class _Bar(object):
    def __init__(self, ot, c, iv=14400000):
        self.interval_ms = iv
        self.open_time = ot
        self.o = self.open = c
        self.h = self.high = c * 1.001
        self.l = self.low = c * 0.999
        self.c = self.close = c
        self.v = self.volume = 1.0


class _Feed(object):
    src = "WS"

    def __init__(self, px=PX):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self._cur = _Bar(0, px, iv=3000)
        self.agg = type("A", (), {"current": staticmethod(lambda iv: self._cur)})()


class _Live(object):
    """假的实盘执行器：只实现对账用得到的那几个接口。

    name 必须是 live/spot，否则 `_reconcile_positions` 直接 return，
    整个对账路径都不会被走到 —— 那样测试会「通过」但什么都没验。
    """
    name = "live"

    def __init__(self, remote=None):
        self.remote = list(remote or [])
        self.queries = {}
        self.open_orders_calls = 0
        self.open_orders_raises = None
        self.query_raises = None

    async def position(self):
        return {"qty": 0.0, "entry": None, "side": "long"}

    async def equity(self):
        return {"balance": 100.0, "equity": 100.0, "position_qty": 0.0}

    async def market_order(self, side, qty, reason="", reduce_only=False,
                           client_id=""):
        return FillResult(ok=True, price=PX, qty=qty, fee=0.0, order_id="1",
                          status="filled", reason="")

    async def open_orders(self, symbol=None):
        self.open_orders_calls += 1
        if self.open_orders_raises:
            raise RuntimeError(self.open_orders_raises)
        return list(self.remote)

    async def query_order(self, order_id="", client_id="", symbol=None):
        if self.query_raises:
            raise RuntimeError(self.query_raises)
        return self.queries.get(client_id or order_id)


class _NoOrderApi(object):
    """没有挂单接口的执行器（纸面 / 现货）。"""
    name = "paper"

    async def position(self):
        return {"qty": 0.0, "entry": None, "side": "long"}

    async def equity(self):
        return {"balance": 100.0, "equity": 100.0, "position_qty": 0.0}


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    t = TradeDB(path)
    t.start()
    return t, path


def _cleanup(tdb, path):
    tdb.stop()
    try:
        os.unlink(path)
    except OSError:
        pass


def _engine(tdb, ex):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    return DecisionEngine(cfg, _Feed(), ex, tdb)


def _local_row(cid, status="NEW", executed=0.0, **kw):
    d = {"client_order_id": cid, "symbol": "BTCUSDT", "side": "SELL",
         "otype": "STOP_MARKET", "qty": 0.010, "executed_qty": executed,
         "stop_price": 73000.0, "status": status, "reduce_only": 1,
         "purpose": "stop", "strategy": "trend",
         "exchange_order_id": "9001"}
    d.update(kw)
    return d


def _remote(cid, status="NEW", executed=0.0, oid="9001"):
    return PendingOrder(order_id=oid, client_id=cid, symbol="BTCUSDT",
                        side="SELL", type="STOP_MARKET", qty=0.010,
                        executed_qty=executed, stop_price=73000.0,
                        status=status, reduce_only=True)


def _risk_kinds(path, wait_for=None, timeout=3.0):
    """读 risk_events 里的 kind 列表（可等某一条出现）。

    ★ 别写 `tdb.risk_events()` / `tdb.recent_risk()` —— TradeDB 没有这两个方法，
    只有写入口 `log_risk()`。这在本项目已经栽过两次（tests/test_takeover_no_entry.py
    的注释里记着同一个坑）：测试里调用一个不存在的方法，抛的是 AttributeError，
    看着像"没记 risk"，实际是测试自己写错了。所以这里直接走 sqlite。
    """
    end = time.time() + timeout
    kinds = []
    while True:
        c = sqlite3.connect(path)
        try:
            kinds = [r[0] for r in c.execute("SELECT kind FROM risk_events")]
        finally:
            c.close()
        if not wait_for or wait_for in kinds or time.time() > end:
            return kinds
        time.sleep(0.05)


def _wait(tdb, pred, timeout=5.0, msg="条件"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = tdb.working_orders(active_only=False)
        if pred(last):
            return last
        time.sleep(0.05)
    raise AssertionError("等了 %.1f 秒，%s 还没满足：%r" % (timeout, msg, last))


def _run(engine):
    """跑一次完整的 reconcile_live（持仓 + 真账 + 挂单）。"""
    return asyncio.get_event_loop().run_until_complete(engine.reconcile_live())


# ---------------------------------------------------------------- 补单

def test_exchange_only_order_is_adopted_into_local_book():
    """交易所有、本地没有 → 必须补进来。

    这是最危险的一条：漏掉之后引擎不知道自己还挂着一张止损单，
    重启后再挂一张 = 同一笔仓两个止损。
    """
    tdb, path = _db()
    try:
        ex = _Live(remote=[_remote("cid-1")])
        eng = _engine(tdb, ex)
        _run(eng)
        rows = _wait(tdb, lambda r: len(r) == 1, msg="补回 1 张")
        assert rows[0]["client_order_id"] == "cid-1"
        assert rows[0]["status"] == "NEW"
        assert rows[0]["exchange_order_id"] == "9001"
        # 补单必须是显眼的，不能只是安静地多一行
        assert "orders_adopted" in _risk_kinds(path, "orders_adopted"), "补单没有记 risk 事件"
    finally:
        _cleanup(tdb, path)


def test_local_only_order_is_marked_lost():
    tdb, path = _db()
    try:
        tdb.upsert_working_orders([_local_row("cid-lost")])
        _wait(tdb, lambda r: len(r) == 1, msg="本地 1 张")
        ex = _Live(remote=[])
        ex.queries["cid-lost"] = None       # 查不到 → 保持 LOST
        _run(_engine(tdb, ex))
        rows = _wait(tdb, lambda r: r and r[0]["status"] == "LOST",
                     msg="标 LOST")
        assert len(rows) == 1, "LOST 的行必须留着，不能删"
        assert "对账" in (rows[0]["note"] or "")
    finally:
        _cleanup(tdb, path)


def test_vanished_order_that_actually_filled_is_recorded_as_filled():
    """本地以为挂着、交易所查不到，但查出来是 FILLED。

    这条如果处理成「当它不存在」，账面上就少了一笔**真实的平仓**。
    """
    tdb, path = _db()
    try:
        tdb.upsert_working_orders([_local_row("cid-filled")])
        _wait(tdb, lambda r: len(r) == 1, msg="本地 1 张")
        ex = _Live(remote=[])
        ex.queries["cid-filled"] = PendingOrder(
            order_id="9001", client_id="cid-filled", symbol="BTCUSDT",
            side="SELL", type="STOP_MARKET", qty=0.010, executed_qty=0.010,
            avg_price=72990.0, status="FILLED", reduce_only=True)
        _run(_engine(tdb, ex))
        rows = _wait(tdb, lambda r: r and r[0]["status"] == "FILLED",
                     msg="查清为 FILLED")
        assert rows[0]["executed_qty"] == pytest.approx(0.010)
        assert rows[0]["avg_price"] == pytest.approx(72990.0)
        assert "已成交" in (rows[0]["note"] or "")
    finally:
        _cleanup(tdb, path)


def test_query_failure_keeps_lost_instead_of_faking_canceled():
    """查真实终态时抛异常 → 保持 LOST，不许改写成 CANCELED。

    把「不知道」写成「已撤销」是这里最容易犯的错：
    那会让一张可能已经成交的单从此消失。
    """
    tdb, path = _db()
    try:
        tdb.upsert_working_orders([_local_row("cid-err")])
        _wait(tdb, lambda r: len(r) == 1, msg="本地 1 张")
        ex = _Live(remote=[])
        ex.query_raises = "查询接口 500"
        _run(_engine(tdb, ex))
        rows = _wait(tdb, lambda r: r and r[0]["status"] == "LOST",
                     msg="保持 LOST")
        assert rows[0]["status"] == "LOST"
    finally:
        _cleanup(tdb, path)


def test_status_change_is_overwritten_by_exchange():
    """两边都有但状态不同 → 用交易所的覆盖。"""
    tdb, path = _db()
    try:
        tdb.upsert_working_orders([_local_row("cid-1", status="NEW")])
        _wait(tdb, lambda r: len(r) == 1, msg="本地 1 张")
        ex = _Live(remote=[_remote("cid-1", status="FILLED", executed=0.010)])
        _run(_engine(tdb, ex))
        rows = _wait(tdb, lambda r: r and r[0]["status"] == "FILLED",
                     msg="被覆盖成 FILLED")
        assert rows[0]["executed_qty"] == pytest.approx(0.010)
    finally:
        _cleanup(tdb, path)


def test_consistent_orders_are_not_duplicated():
    """两边一致 → 不许产生第二行。

    重复行会让「这笔仓有几个止损」这个问题的答案变成 2，
    而实际只有 1 张。
    """
    tdb, path = _db()
    try:
        tdb.upsert_working_orders([_local_row("cid-1")])
        _wait(tdb, lambda r: len(r) == 1, msg="本地 1 张")
        ex = _Live(remote=[_remote("cid-1")])
        _run(_engine(tdb, ex))
        rows = _wait(tdb, lambda r: r and r[0]["synced_ts"] > 0,
                     msg="已对过账")
        assert len(rows) == 1
        # 再对一次也不能多
        _run(_engine(tdb, ex))
        time.sleep(0.3)
        assert len(tdb.working_orders(active_only=False)) == 1
    finally:
        _cleanup(tdb, path)


# ---------------------------------------------------------------- 失败路径

def test_fetch_failure_does_not_bubble_out_of_reconcile_live():
    """拉挂单列表失败时只记日志/risk，**不能冒泡**。

    它和持仓对账在同一条链上，抛出去会让持仓对账也一并失败 ——
    那是比「挂单账暂时不同步」严重得多的事。
    """
    tdb, path = _db()
    try:
        ex = _Live(remote=[])
        ex.open_orders_raises = "交易所 502"
        eng = _engine(tdb, ex)
        _run(eng)                      # 不许抛
        assert "orders_fetch_failed" in _risk_kinds(path, "orders_fetch_failed"), "拉取失败没有记 risk 事件"
    finally:
        _cleanup(tdb, path)


def test_executor_without_order_api_is_skipped_silently():
    """纸面/现货执行器没有 open_orders → 静默跳过，不许报错。

    用 hasattr 判断而不是 try/except AttributeError：后者会把
    open_orders 内部的真故障也误当成「这个执行器不支持」吞掉。
    """
    tdb, path = _db()
    try:
        eng = _engine(tdb, _NoOrderApi())
        _run(eng)
        assert tdb.working_orders() == []
    finally:
        _cleanup(tdb, path)


def test_reconcile_is_reentrant_guarded():
    """上一轮还没跑完时不许重入 —— 否则同一批单会被写两遍日志和 risk。"""
    tdb, path = _db()
    try:
        ex = _Live(remote=[_remote("cid-1")])
        eng = _engine(tdb, ex)
        eng._orders_busy = True
        asyncio.get_event_loop().run_until_complete(eng._sync_working_orders())
        assert ex.open_orders_calls == 0, "重入锁没生效"
    finally:
        _cleanup(tdb, path)


def test_orders_sync_runs_as_part_of_reconcile_live():
    """启动时 `reconcile_live()` 必须真的走到挂单对账。

    这条守的是「接线」本身：方法写好了但没人调，等于没写。
    """
    tdb, path = _db()
    try:
        ex = _Live(remote=[_remote("cid-1")])
        eng = _engine(tdb, ex)
        _run(eng)
        assert ex.open_orders_calls == 1, "reconcile_live 没有调 open_orders"
        assert len(_wait(tdb, lambda r: len(r) == 1, msg="补回 1 张")) == 1
    finally:
        _cleanup(tdb, path)


def test_sync_crash_is_recorded_as_risk_not_just_logged():
    """对账中途抛异常 → 除了日志还必须落 risk 事件。

    P3 的验收原话是「失败只记 risk 和日志」，而通用的那条 `except Exception`
    原来**只写日志**。第 131 轮的 PendingOrder/dict 接口断裂就是被它吞掉的：
    日志里有一行 ERROR，看板和 risk_events 里一条都没有 —— 等于没人看得见。
    这条钉死「日志 + risk 两条都要有」，否则同样的静默还会再来一次。
    """
    tdb, path = _db()
    try:
        ex = _Live(remote=[_remote("cid-1")])
        eng = _engine(tdb, ex)

        def _boom(*a, **kw):
            raise RuntimeError("落库炸了")

        tdb.apply_order_reconciliation = _boom
        _run(eng)                       # 不许抛出去（不能连带持仓对账一起失败）
        assert "orders_sync_failed" in _risk_kinds(path, "orders_sync_failed"), "对账异常没有落 risk 事件"
    finally:
        _cleanup(tdb, path)


def test_vanished_order_can_be_matched_by_exchange_id():
    """本地没有 client_order_id 时，用 exchange_order_id 也能查清。

    真实场景：早期版本只落了交易所 id（或者手工插入的行），
    这种单不能用「没有 client id」当作查不了的理由。
    """
    tdb, path = _db()
    try:
        row = _local_row("cid-x")
        row["client_order_id"] = "cid-x"
        row["exchange_order_id"] = "5555"
        tdb.upsert_working_orders([row])
        _wait(tdb, lambda r: len(r) == 1, msg="本地 1 张")
        ex = _Live(remote=[])
        ex.queries["cid-x"] = PendingOrder(
            order_id="5555", client_id="cid-x", status="CANCELED",
            qty=0.010, executed_qty=0.0)
        _run(_engine(tdb, ex))
        rows = _wait(tdb, lambda r: r and r[0]["status"] == "CANCELED",
                     msg="查清为 CANCELED")
        assert rows[0]["exchange_order_id"] == "5555"
    finally:
        _cleanup(tdb, path)
