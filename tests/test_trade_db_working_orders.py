# -*- coding: utf-8 -*-
"""working_orders 表 + 挂单对账。

## 这一步防的是什么

挂单的生命周期**跨进程**：引擎重启、部署、崩溃，单子都还在交易所挂着。
只存内存的后果有两种，而且都不报错：

1. **止损单还在，引擎以为没有** → 重启后再挂一张 → 同一笔仓两个止损。
   先触发的那张平掉大部分仓，后触发的那张把剩仓**反向开出去**。
   账上多了一个方向相反的仓，而且每一步都"成功"。
2. **开仓单成交了，引擎不知道** → 本地账面没仓位、交易所有。

所以这里测的不是「表能写」，而是：
* 用 `client_order_id` 当自然键（下单前就要落库，那时还没有交易所 id）
* 对账三个方向都对：交易所多的要补、本地多的要标 LOST（**不能删**）、
  状态变了的要覆盖
* 本地独有的 purpose/strategy/round_key **不能被交易所的数据抹掉**
  —— 交易所不知道这些，丢了就补不回来
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.trade_db import TradeDB, reconcile_orders   # noqa: E402


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


def _wait(tdb, want_n, active_only=True, timeout=5.0):
    """等到库里真的出现 want_n 行，不是「有一行就算」。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = tdb.working_orders(active_only=active_only)
        if len(last) == want_n:
            return last
        time.sleep(0.05)
    raise AssertionError("等了 %.1f 秒，库里还是 %d 行：%r"
                         % (timeout, len(last or []), last))


def _wait_value(tdb, col, want, active_only=True, timeout=5.0):
    """等到某一列真的变成 want。

    ★ 只等「行数到了」是错的：upsert 是异步落库，行数在**第一次**写入后
    就已经是 1 了，第二次更新还没落盘时断言就会读到旧值。
    这个坑很隐蔽 —— 断言失败看起来像 COALESCE 写错了，
    实际只是读写时序。
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        rows = tdb.working_orders(active_only=active_only)
        if len(rows) == 1:
            last = rows[0].get(col)
            if last == want:
                return rows[0]
        time.sleep(0.05)
    raise AssertionError("等了 %.1f 秒，%s 还是 %r（期望 %r）"
                         % (timeout, col, last, want))


def _row(cid="cid-1", status="NEW", executed=0.0, **kw):
    d = {"client_order_id": cid, "symbol": "BTCUSDT", "side": "SELL",
         "otype": "STOP_MARKET", "qty": 0.010, "executed_qty": executed,
         "price": 0.0, "stop_price": 73000.0, "status": status,
         "reduce_only": 1, "purpose": "stop", "strategy": "trend",
         "round_key": "BTCUSDT|1700000000000|short|0.01",
         "exchange_order_id": "9001"}
    d.update(kw)
    return d


# ---------------------------------------------------------------- 纯函数

def test_reconcile_pairing_uses_client_order_id():
    """配对键是 client_order_id —— 因为下单前就要落库，那时还没有交易所 id。

    用交易所 id 配对，会让「请求超时但单子其实建成了」这一类永远对不上：
    本地那一行 id 是空的。
    """
    local = [{"client_order_id": "c1", "status": "NEW", "executed_qty": 0.0,
              "exchange_order_id": ""}]
    remote = [{"client_id": "c1", "status": "NEW", "executed_qty": 0.0,
               "order_id": "123"}]
    r = reconcile_orders(local, remote)
    assert len(r["same"]) == 1
    assert not r["adopted"] and not r["vanished"] and not r["changed"]


def test_reconcile_adopts_exchange_only_orders():
    """交易所有、本地没有 → 必须补进本地。

    漏掉它的后果：引擎不知道自己还挂着一张止损单，重启后再挂一张，
    同一笔仓两个止损，后触发的那张把剩仓反向开出去。
    """
    local = []
    remote = [{"client_id": "c9", "status": "NEW", "qty": 0.02}]
    r = reconcile_orders(local, remote)
    assert [x["client_id"] for x in r["adopted"]] == ["c9"]


def test_reconcile_reports_vanished_but_does_not_drop_them():
    """本地以为挂着、交易所查不到 → 报 `vanished`，**不删**。

    它可能是已经成交了（那就是一笔真实成交，必须记账），
    也可能被手工撤了。静默当成「不存在」是最坏的处理方式。
    """
    local = [{"client_order_id": "c2", "status": "NEW", "executed_qty": 0.0}]
    r = reconcile_orders(local, [])
    assert [x["client_order_id"] for x in r["vanished"]] == ["c2"]


def test_reconcile_detects_status_change_and_partial_fill():
    local = [{"client_order_id": "c1", "status": "NEW", "executed_qty": 0.0},
             {"client_order_id": "c2", "status": "PARTIALLY_FILLED",
              "executed_qty": 0.004}]
    remote = [{"client_id": "c1", "status": "FILLED", "executed_qty": 0.010},
              {"client_id": "c2", "status": "PARTIALLY_FILLED",
               "executed_qty": 0.006}]
    r = reconcile_orders(local, remote)
    assert len(r["changed"]) == 2
    assert not r["same"]


def test_reconcile_ignores_rows_without_client_id():
    """没有 client_order_id 的行配不了对，不能让它伪造出 adopted/vanished。"""
    r = reconcile_orders([{"status": "NEW"}], [{"status": "NEW"}])
    assert r == {"adopted": [], "vanished": [], "changed": [], "same": []}


# ---------------------------------------------------------------- 落库

def test_working_order_round_trips_through_db():
    t, path = _db()
    try:
        t.upsert_working_orders([_row()])
        got = _wait(t, 1)
        o = got[0]
        assert o["client_order_id"] == "cid-1"
        assert o["otype"] == "STOP_MARKET"
        assert o["stop_price"] == pytest.approx(73000.0)
        assert o["reduce_only"] == 1
        assert o["purpose"] == "stop"
        assert o["status"] == "NEW"
    finally:
        _cleanup(t, path)


def test_upsert_is_idempotent_by_client_order_id():
    """同一张单更新多次只能有一行 —— 否则重启后同一张单被数成好几张。"""
    t, path = _db()
    try:
        t.upsert_working_orders([_row(status="NEW")])
        t.upsert_working_orders([_row(status="PARTIALLY_FILLED", executed=0.004)])
        t.upsert_working_orders([_row(status="FILLED", executed=0.010)])
        got = _wait(t, 1, active_only=False)
        assert got[0]["status"] == "FILLED"
        assert got[0]["executed_qty"] == pytest.approx(0.010)
    finally:
        _cleanup(t, path)


def test_active_only_filters_out_finished_orders():
    t, path = _db()
    try:
        t.upsert_working_orders([_row(cid="a", status="NEW"),
                                 _row(cid="b", status="FILLED")])
        _wait(t, 2, active_only=False)
        assert [o["client_order_id"] for o in t.working_orders()] == ["a"]
    finally:
        _cleanup(t, path)


def test_exchange_order_id_is_not_erased_by_a_later_update():
    """先落库（没有交易所 id）→ 拿到 id 回填 → 后续更新不许把 id 抹掉。

    抹掉之后就再也查不到这张单了，而且**不报错**。
    """
    t, path = _db()
    try:
        t.upsert_working_orders([_row(exchange_order_id="")])
        _wait(t, 1)
        t.upsert_working_orders([_row(exchange_order_id="9001")])
        _wait_value(t, "exchange_order_id", "9001")
        # 再更新一次，这次不带 id
        t.upsert_working_orders([_row(exchange_order_id="", status="FILLED",
                                      executed=0.010)])
        # 注意 active_only=False：状态变成 FILLED 之后它就不再算「挂着」，
        # 用 active_only=True 会一行都读不到，等成一个永远不满足的条件。
        _wait_value(t, "status", "FILLED", active_only=False)
        assert t.working_orders(active_only=False)[0]["exchange_order_id"] == "9001"
    finally:
        _cleanup(t, path)


def test_purpose_and_strategy_survive_reconciliation():
    """交易所不知道 purpose/strategy/round_key，对账不许把它们抹掉。

    这些字段是本地独有的：丢了之后「这张止损单是哪一笔仓的」
    就再也查不回来了。
    """
    t, path = _db()
    try:
        t.upsert_working_orders([_row(cid="c1")])
        _wait(t, 1)
        local = t.working_orders()
        remote = [{"client_id": "c1", "order_id": "9001", "symbol": "BTCUSDT",
                   "side": "SELL", "type": "STOP_MARKET", "qty": 0.010,
                   "executed_qty": 0.0, "status": "NEW"}]
        res = t.apply_order_reconciliation(reconcile_orders(local, remote))
        assert res["same"] == 1
        # 等落库
        end = time.time() + 5
        while time.time() < end:
            o = t.working_orders()[0]
            if o["synced_ts"] > 0:
                break
            time.sleep(0.05)
        o = t.working_orders()[0]
        assert o["purpose"] == "stop", o
        assert o["strategy"] == "trend", o
        assert o["round_key"].startswith("BTCUSDT|"), o
    finally:
        _cleanup(t, path)


def test_vanished_orders_are_marked_lost_not_deleted():
    t, path = _db()
    try:
        t.upsert_working_orders([_row(cid="c1")])
        _wait(t, 1)
        res = t.apply_order_reconciliation(
            reconcile_orders(t.working_orders(), []))
        assert res["lost"] == 1
        end = time.time() + 5
        while time.time() < end:
            all_rows = t.working_orders(active_only=False)
            if all_rows and all_rows[0]["status"] == "LOST":
                break
            time.sleep(0.05)
        rows = t.working_orders(active_only=False)
        assert len(rows) == 1, "LOST 的行必须留着，不能删"
        assert rows[0]["status"] == "LOST"
        assert "对账" in (rows[0]["note"] or "")
        # 也不能再算「挂着」
        assert t.working_orders() == []
    finally:
        _cleanup(t, path)


def test_adopted_order_keeps_exchange_state():
    t, path = _db()
    try:
        remote = [{"client_id": "new-1", "order_id": "777", "symbol": "BTCUSDT",
                   "side": "BUY", "type": "LIMIT", "qty": 0.005,
                   "executed_qty": 0.0, "price": 70000.0, "status": "NEW"}]
        res = t.apply_order_reconciliation(reconcile_orders([], remote))
        assert res["adopted"] == 1
        got = _wait(t, 1)
        assert got[0]["client_order_id"] == "new-1"
        assert got[0]["exchange_order_id"] == "777"
        assert got[0]["price"] == pytest.approx(70000.0)
        assert got[0]["side"] == "BUY"
    finally:
        _cleanup(t, path)


def test_missing_client_order_id_is_rejected_loudly():
    """没有 client_order_id 就写不进去 —— 必须当场报错。

    静默丢掉的话，这张单在重启后就彻底消失了，
    而对账也无从发现（它连键都没有）。
    """
    t, path = _db()
    try:
        with pytest.raises(ValueError):
            t.upsert_working_orders([{"symbol": "BTCUSDT", "status": "NEW"}])
    finally:
        _cleanup(t, path)


def test_working_orders_survive_a_restart():
    """重启后本地还知道自己挂过什么 —— 这是整张表存在的理由。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    t1 = TradeDB(path)
    t1.start()
    try:
        t1.upsert_working_orders([_row(cid="persist-1", purpose="stop")])
        _wait(t1, 1)
    finally:
        t1.stop()
    t2 = TradeDB(path)
    t2.start()
    try:
        got = _wait(t2, 1)
        assert got[0]["client_order_id"] == "persist-1"
        assert got[0]["purpose"] == "stop"
    finally:
        _cleanup(t2, path)
