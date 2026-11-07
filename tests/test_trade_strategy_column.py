# -*- coding: utf-8 -*-
"""trades 表的 strategy 列 + 旧库迁移。

为什么必须有这一列：scalp 时代留下的 4 笔旧仓和 trend 时代的新仓，
在 paper=1 上完全一样。要统计"趋势策略的真实边际"，就必须能区分它们——
否则算出来的边际里混着另一套已被否决的策略，数字再漂亮也是假的。

迁移是最容易出事的环节：生产库已经有数据、且是用
CREATE TABLE IF NOT EXISTS 建的，加列不会自动生效。
这里专门构造一个"老 schema + 有数据"的库来验证迁移。
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trade_db import TradeDB

OLD_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
    entry_ts INTEGER NOT NULL, entry_price REAL NOT NULL,
    exit_ts INTEGER, exit_price REAL, stop_price REAL, target_price REAL,
    pnl_gross REAL, fee REAL DEFAULT 0, pnl_net REAL, mfe REAL, mae REAL,
    open_reason TEXT, close_reason TEXT, decision_id INTEGER,
    max_hold_bars INTEGER, paper INTEGER DEFAULT 1
);
"""


def _old_db(path):
    c = sqlite3.connect(path)
    c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO trades(symbol,side,qty,entry_ts,entry_price,paper,"
              "open_reason,close_reason,pnl_net) VALUES(?,?,?,?,?,?,?,?,?)",
              ("BTCUSDT", "short", 0.068, 1000, 77676.0, 1,
               "scalp", "scalp exit", -5.11))
    c.commit()
    c.close()


def test_migration_adds_strategy_column_to_old_db():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "old.db")
    _old_db(p)
    db = TradeDB(p)
    db.start()
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(trades)")}
        assert "strategy" in cols, "旧库没有加上 strategy 列"
    finally:
        db.stop()


def test_migration_preserves_existing_rows():
    """迁移绝不能动老数据。老数据留空串（未知），不猜。"""
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "old.db")
    _old_db(p)
    db = TradeDB(p)
    db.start()
    try:
        c = sqlite3.connect(p)
        rows = list(c.execute("SELECT side, qty, pnl_net, strategy FROM trades"))
        c.close()
        assert len(rows) == 1, "迁移过程中丢了数据"
        assert rows[0][0] == "short"
        assert rows[0][1] == pytest.approx(0.068)
        assert rows[0][2] == pytest.approx(-5.11)
        assert rows[0][3] in ("", None), "给老数据编了一个策略名"
    finally:
        db.stop()


def test_migration_is_idempotent():
    """重复启动不能报错（每次重启都会跑）。"""
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "old.db")
    _old_db(p)
    for _ in range(3):
        db = TradeDB(p)
        db.start()
        db.stop()


def test_new_trade_records_strategy():
    tmp = tempfile.mkdtemp()
    db = TradeDB(os.path.join(tmp, "new.db"))
    db.start()
    try:
        db.log_trade({"symbol": "BTCUSDT", "side": "long", "qty": 0.00128,
                      "entry_ts": 2000, "entry_price": 77815.0,
                      "open_reason": "trend", "paper": 1, "strategy": "trend"})
    finally:
        # 写入是单写者队列 + 后台线程，且小批量要攒 0.4 秒才落盘。
        # 必须先 stop()（它会把剩余批次 flush 掉并 join），
        # 之后再开新连接读——直接在同一个库上抢读会读到"还没写进去"的空表。
        db.stop()
    c = sqlite3.connect(os.path.join(tmp, "new.db"))
    try:
        row = c.execute("SELECT strategy FROM trades WHERE entry_ts=2000").fetchone()
        assert row and row[0] == "trend", "新仓没有记策略名（实际 %r）" % (row,)
    finally:
        c.close()


def test_fresh_db_has_column_without_migration():
    """新库由 CREATE TABLE 直接带上该列，迁移应为空操作。"""
    tmp = tempfile.mkdtemp()
    db = TradeDB(os.path.join(tmp, "f.db"))
    db.start()
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(trades)")}
        assert "strategy" in cols
    finally:
        db.stop()
