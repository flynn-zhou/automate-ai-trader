# -*- coding: utf-8 -*-
"""挂单的三种形状 → 一份 dict（第 131 轮补的接口层测试）。

## 这条测试守的是什么

第 131 轮的真实缺陷：`LiveExecutor.open_orders()` 返回 `List[PendingOrder]`
（dataclass，没有 `.get`），而 `reconcile_orders()` 只吃 dict ——
于是挂单对账**每次都在第一张单上抛 AttributeError**，被引擎的宽 except
吞成一行日志。表面上「启动对账跑了」，实际一张单都没对上：
`working_orders` 表永远不填，交易所有、本地没有的止损单永远补不回来，
重启后同一笔仓会挂出第二个止损。

P2 的测试全部只喂 dict（模拟币安 JSON），所以这个断裂在 P2 全绿的前提下
完好无损地活到了 P3 —— **测试喂的形状和生产返回的形状不是一回事**，
这是本项目反复出现的假信心来源。这里补的就是「生产真实形状」那一半。
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

from ai_trader.exchange.base import PendingOrder          # noqa: E402
from ai_trader.trade_db import TradeDB, order_row, reconcile_orders  # noqa: E402


def _pending(**kw):
    d = dict(order_id="7001", client_id="trend-stop-1", symbol="BTCUSDT",
             side="SELL", type="STOP_MARKET", qty=0.010, executed_qty=0.0,
             price=0.0, stop_price=73000.0, avg_price=0.0,
             status="NEW", reduce_only=True)
    d.update(kw)
    return PendingOrder(**d)


def _local(**kw):
    d = {"client_order_id": "trend-stop-1", "exchange_order_id": "7001",
         "symbol": "BTCUSDT", "side": "SELL", "otype": "STOP_MARKET",
         "qty": 0.010, "executed_qty": 0.0, "price": 0.0,
         "stop_price": 73000.0, "avg_price": 0.0, "reduce_only": 1,
         "status": "NEW", "purpose": "stop", "strategy": "trend",
         "round_key": "r-1", "created_ts": 1, "updated_ts": 1,
         "synced_ts": 0, "note": ""}
    d.update(kw)
    return d


# ---------------------------------------------------------------- order_row

def test_order_row_reads_pending_order_attributes():
    """PendingOrder 是 dataclass，只能按属性取。"""
    row = order_row(_pending(status="PARTIALLY_FILLED", executed_qty=0.004))
    assert row["client_id"] == "trend-stop-1"
    assert row["order_id"] == "7001"
    assert row["type"] == "STOP_MARKET"
    assert row["executed_qty"] == pytest.approx(0.004)
    assert row["stop_price"] == pytest.approx(73000.0)
    assert row["status"] == "PARTIALLY_FILLED"


def test_order_row_wraps_dict_without_mutating_it():
    src = {"clientOrderId": "x-1", "status": "NEW", "origQty": 0.02}
    row = order_row(src)
    row["status"] = "FILLED"
    assert src["status"] == "NEW", "order_row 不该改调用方的 dict"


def test_order_row_tolerates_unknown_objects():
    """认不出来的对象归一化成空 dict，交给调用方按「没有 client id」跳过。"""
    assert order_row(object()) == {}
    assert order_row(None) == {}


# ---------------------------------------------------------------- 对账两侧

def test_reconcile_pairs_pending_order_remote_with_local_dict():
    """★ 回归点：远端是 PendingOrder 时也必须能配对。

    修之前这里抛 AttributeError: 'PendingOrder' object has no attribute 'get'，
    修之后是 same —— 一条线两边都有的正常情形。
    """
    res = reconcile_orders([_local()], [_pending()])
    assert res["same"] and len(res["same"]) == 1
    assert res["adopted"] == [] and res["vanished"] == []


def test_reconcile_adopts_pending_order_the_local_book_never_saw():
    res = reconcile_orders([], [_pending()])
    assert len(res["adopted"]) == 1
    assert res["adopted"][0]["client_id"] == "trend-stop-1"


def test_reconcile_still_accepts_binance_json_rows():
    """币安原始 JSON 那套键（clientOrderId / origQty）不能因为这次改动退回去。"""
    raw = [{"clientOrderId": "trend-stop-1", "orderId": 7001,
            "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET",
            "origQty": "0.010", "executedQty": "0.010",
            "stopPrice": "73000.0", "status": "FILLED", "reduceOnly": True}]
    res = reconcile_orders([_local(status="NEW")], raw)
    assert len(res["changed"]) == 1
    assert res["changed"][0]["remote"]["status"] == "FILLED"


def test_reconcile_ignores_rows_without_any_client_id():
    """没有 client id 的行不能凭空配对（否则会把 A 单的状态写到 B 单上）。"""
    res = reconcile_orders([_local()], [{"status": "NEW"}])
    assert res["same"] == [] and res["changed"] == [] and res["adopted"] == []
    assert len(res["vanished"]) == 1


# ---------------------------------------------------------------- 落库那一侧

def test_from_remote_accepts_pending_order_and_keeps_local_metadata():
    """交易所不知道 purpose/strategy/round_key，落库时必须从本地行保住。"""
    row = TradeDB._from_remote(_pending(status="FILLED", executed_qty=0.010,
                                       avg_price=72990.0),
                               1700000000000, _local())
    assert row["client_order_id"] == "trend-stop-1"
    assert row["exchange_order_id"] == "7001"
    assert row["status"] == "FILLED"
    assert row["executed_qty"] == pytest.approx(0.010)
    assert row["avg_price"] == pytest.approx(72990.0)
    assert row["purpose"] == "stop"
    assert row["strategy"] == "trend"
    assert row["round_key"] == "r-1"
    assert row["created_ts"] == 1, "created_ts 必须沿用本地值，不能被对账时间覆盖"


def test_apply_reconciliation_round_trip_with_pending_orders():
    """整条路走一遍：PendingOrder → reconcile → 落库 → 读回来。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    t = TradeDB(path)
    t.start()
    try:
        remote = [_pending(status="PARTIALLY_FILLED", executed_qty=0.004)]
        out = t.apply_order_reconciliation(reconcile_orders([], remote))
        assert out["adopted"] == 1
        # 写入走队列 + 后台线程，读要等一下（这不是缺陷，是 TradeDB 的既定结构）
        rows = []
        for _ in range(100):
            rows = t.working_orders(active_only=False)
            if rows:
                break
            time.sleep(0.02)
        assert len(rows) == 1
        assert rows[0]["client_order_id"] == "trend-stop-1"
        assert rows[0]["status"] == "PARTIALLY_FILLED"
        assert rows[0]["executed_qty"] == pytest.approx(0.004)
        assert rows[0]["synced_ts"] > 0
    finally:
        t.stop()
        try:
            os.unlink(path)
        except OSError:
            pass
