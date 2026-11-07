# -*- coding: utf-8 -*-
"""哨兵新检查的失败测试：该报的必须报，不该报的必须不报。

没做过失败测试的监控等于没有监控。
这条检查最容易犯的错是【误报】——
趋势策略每天大部分时间都在"空仓等待"，如果误报，报警就会被无视。

所以两个方向都要测：
  正例：信号想开仓被挡住 + 空仓  -> 必须报警
  反例 1：正常空仓等待（没被挡） -> 不能报
  反例 2：被挡了但有持仓         -> 不能报（有仓位说明在场上）
"""
import os
import sqlite3
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.sentinel import Sentinel

NOW = int(time.time() * 1000)


def _mkdb(tmp, decisions, trades):
    p = os.path.join(tmp, "t.db")
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE decisions(id INTEGER PRIMARY KEY, ts INTEGER,
                 bar_time INTEGER, feature_time INTEGER, symbol TEXT, price REAL,
                 signal TEXT, action TEXT, reason TEXT, gate_json TEXT,
                 feature_json TEXT, risk_json TEXT, equity REAL)""")
    c.execute("""CREATE TABLE trades(id INTEGER PRIMARY KEY, symbol TEXT, side TEXT,
                 qty REAL, entry_ts INTEGER, entry_price REAL, exit_ts INTEGER,
                 exit_price REAL, stop_price REAL, target_price REAL,
                 pnl_gross REAL, fee REAL, pnl_net REAL, mfe REAL, mae REAL,
                 open_reason TEXT, close_reason TEXT, decision_id INTEGER,
                 paper INTEGER, max_hold_bars INTEGER, strategy TEXT)""")
    c.execute("""CREATE TABLE equity(id INTEGER PRIMARY KEY, ts INTEGER,
                 equity REAL, balance REAL)""")
    c.execute("INSERT INTO equity(ts,equity,balance) VALUES(?,?,?)", (NOW, 100.0, 100.0))
    for d in decisions:
        c.execute("INSERT INTO decisions(ts,signal,action,reason,equity) VALUES(?,?,?,?,?)", d)
    for t_ in trades:
        c.execute("INSERT INTO trades(side,qty,entry_ts,entry_price,exit_ts,strategy)"
                  " VALUES(?,?,?,?,?,?)", t_)
    c.commit()
    c.close()
    return p


def _market(tmp):
    p = os.path.join(tmp, "m.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE bars(interval_ms INTEGER, open_time INTEGER)")
    for iv in (3000,):
        c.execute("INSERT INTO bars VALUES(?,?)", (iv, NOW - 3000))
    c.commit()
    c.close()
    return p


def _run(tmp, decisions, trades):
    tdb = _mkdb(tmp, decisions, trades)
    mdb = _market(tmp)
    s = Sentinel(trade_db=tdb, market_db=mdb)
    return s.check()


@pytest.mark.asyncio
async def test_blocked_entry_while_flat_alerts():
    """信号想开仓被挡住，且空仓 -> 必须报警。"""
    tmp = tempfile.mkdtemp()
    r = _run(tmp, [
        (NOW - 60000, "trend_entry", "skip", "计划被拒：名义不足", 100.0),
    ], [])
    assert not r["ok"], "被挡住且空仓，却没有报警"
    assert any("挡住" in i for i in r["issues"]), r["issues"]


@pytest.mark.asyncio
async def test_normal_watch_does_not_alert():
    """正常空仓等待（价格在均线下方）-> 不能报警。"""
    tmp = tempfile.mkdtemp()
    r = _run(tmp, [
        (NOW - 60000, "trend_watch", "watch",
         "空仓等待：现价 77800.0 <  SMA200 82365.4（差 -5.45%）", 71.77),
    ], [])
    assert not any("挡住" in i for i in r["issues"]), (
        "正常等待被误报成故障：" + str(r["issues"]))


@pytest.mark.asyncio
async def test_blocked_but_holding_does_not_alert():
    """被挡住但已有持仓 -> 不报（有仓位说明在场上）。"""
    tmp = tempfile.mkdtemp()
    r = _run(tmp, [
        (NOW - 60000, "trend_entry", "skip", "计划被拒：名义不足", 100.0),
    ], [("long", 0.001, NOW - 3600000, 77000.0, None, "trend")])
    assert not any("挡住" in i for i in r["issues"]), (
        "有持仓还报'被挡住'：" + str(r["issues"]))


@pytest.mark.asyncio
async def test_old_blocked_record_does_not_alert():
    """2 小时前的旧记录 -> 不报（只看最近窗口）。"""
    tmp = tempfile.mkdtemp()
    r = _run(tmp, [
        (NOW - 3 * 3600 * 1000, "trend_entry", "skip", "计划被拒：名义不足", 100.0),
    ], [])
    assert not any("挡住" in i for i in r["issues"]), (
        "过了 2 小时还在报：" + str(r["issues"]))
