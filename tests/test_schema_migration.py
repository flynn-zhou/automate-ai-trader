# -*- coding: utf-8 -*-
"""旧库缺列时，启动路径不能崩。

真实复现：本地 data/market_trade.db 是早年建的，trades 表没有
max_hold_bars / paper / open_reason 这几列，而 open_trade() 的 SELECT
里带着它们 —— TradeDB.start() 之后第一次 open_trade() 就抛
no such column: max_hold_bars。

open_trade() 被 restore_from_db（纸面恢复）和启动对账用到，
崩在这里等于整个引擎起不来。7x24 运行最怕这种"升级即宕机"。

原来的 _MIGRATIONS 只有 strategy 一列，其余全靠运气（新库才齐）。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.trade_db import TradeDB

# 早年版本的建表语句：没有 paper / open_reason / close_reason /
# decision_id / max_hold_bars / mfe / mae / strategy
OLD_TRADES = """
CREATE TABLE trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          REAL NOT NULL,
    entry_ts     INTEGER NOT NULL,
    entry_price  REAL NOT NULL,
    exit_ts      INTEGER,
    exit_price   REAL,
    stop_price   REAL,
    target_price REAL,
    pnl_gross    REAL,
    fee          REAL DEFAULT 0,
    pnl_net      REAL
);
CREATE TABLE equity (
    ts           INTEGER PRIMARY KEY,
    balance      REAL NOT NULL,
    unrealized   REAL DEFAULT 0,
    equity       REAL NOT NULL,
    position_qty REAL DEFAULT 0
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, bar_time INTEGER, feature_time INTEGER, symbol TEXT,
    price REAL, signal TEXT, action TEXT, reason TEXT, gates TEXT,
    risk TEXT, equity REAL
);
"""


def _old_db():
    """造一个真的旧库，并在里面放一笔未平仓记录。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.executescript(OLD_TRADES)
    c.execute("INSERT INTO trades (symbol,side,qty,entry_ts,entry_price)"
              " VALUES ('BTCUSDT','long',0.00127,1000,70000.0)")
    c.commit()
    c.close()
    return path


def test_open_trade_survives_old_schema():
    """旧库（缺 8 个列）启动后 open_trade() 必须能读到那笔未平仓。"""
    path = _old_db()
    tdb = TradeDB(path)
    try:
        tdb.start()                       # 这里会跑迁移
        op = tdb.open_trade()             # 以前这里抛 no such column
        assert op is not None, "迁移后应当仍能读到那笔未平仓记录"
        assert op["qty"] == pytest.approx(0.00127)
        assert op["entry_price"] == pytest.approx(70000.0)
        # 迁移补的列要有可用默认值：paper=1（这一列出现前全是纸面）
        assert op["paper"] == 1, (
            "旧数据必须被认成纸面仓（paper=1）；"
            "认成实盘会让它在切 live 时被当成真实持仓")
        assert op["max_hold_bars"] == 0, "缺省应是 0（调用方再回退到配置）"
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_migration_adds_all_columns_and_is_idempotent():
    """迁移必须把 8 个列都补上，而且可重复执行不报错。"""
    path = _old_db()
    need = ["paper", "open_reason", "close_reason", "decision_id",
            "max_hold_bars", "mfe", "mae", "strategy"]
    tdb = TradeDB(path)
    try:
        tdb.start()
        tdb.stop()
        # 再开一次：迁移应当无事可做而不是抛"列已存在"
        tdb2 = TradeDB(path)
        tdb2.start()
        tdb2.stop()
    finally:
        try:
            tdb.stop()
        except Exception:
            pass
    c = sqlite3.connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
    eq = {r[1] for r in c.execute("PRAGMA table_info(equity)")}
    c.close()
    missing = [k for k in need if k not in cols]
    assert not missing, "迁移后仍缺列：%s" % missing
    assert "paper" in eq, "equity 表也必须补上 paper 列"
    try:
        os.unlink(path)
    except OSError:
        pass


def test_closed_trades_readable_on_old_schema():
    """旧库里已经平掉的历史也必须读得出来（closed_trades 读 paper 列）。"""
    path = _old_db()
    c = sqlite3.connect(path)
    c.execute("UPDATE trades SET exit_ts=2000, exit_price=71000.0,"
              " pnl_gross=1.27, fee=0.0889, pnl_net=1.18")
    c.commit()
    c.close()
    tdb = TradeDB(path)
    try:
        tdb.start()
        rows = tdb.closed_trades(10)
        assert len(rows) == 1
        assert rows[0]["paper"] == 1, "旧记录应被认成纸面"
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass
