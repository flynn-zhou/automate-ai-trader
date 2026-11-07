# -*- coding: utf-8 -*-
"""行情订阅表：哪些品种要收数据，是 DB 里的一张清单，不是硬编码。

为什么需要它
------------
用户要「订阅列表 = 总开关」：加一个品种，系统就开始收它的行情并回填；
去掉，就停止。硬编码在 config.yaml 里做不到「运行时开关」，改一次要重启。

边界（务必分清）
----------------
**订阅 = 收行情。不 = 交易该品种。**
引擎只交易一个品种（cfg.trading.symbol），其余品种仅用于观察与区间演化。
多品种行情与多品种交易是两件事，混在一起会让「订阅了五个币」被误读成
「同时在五个币上开仓」。

已知限制（写在这里，避免被当成 bug）
------------------------------------
本模块只负责清单本身。要让行情管道真正收多品种，还差：
  1. bars 表加 symbol 列（现在主键是 (interval_ms, open_time)，无 symbol）
  2. Feed 支持多实例或一个多路 WS
  3. Store 的查询按 symbol 过滤
这三步是架构改造，涉及现有 766 项测试，必须单独评估后再动。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading

from ..dbutil import synchronized
from typing import List

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_subscriptions (
    symbol      TEXT PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

# 币安永续合约的常见品种，供页面下拉/校验参考。
# 不在此列表里的品种不拒绝——交易所才是最终裁判，这里只做形状校验。
KNOWN_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT",
)


def normalize(symbol: str) -> str:
    """把用户输入规整成交易所的写法，非法则抛 ValueError。"""
    s = (symbol or "").strip().upper()
    if not s:
        raise ValueError("品种不能为空")
    if not s.isalnum():
        raise ValueError("品种只能包含字母和数字: %r" % symbol)
    if not s.endswith("USDT"):
        raise ValueError("只支持 USDT 本位合约（如 BTCUSDT）: %r" % symbol)
    if len(s) < 6 or len(s) > 20:
        raise ValueError("品种长度不合理: %r" % symbol)
    return s


class SubscriptionStore:
    """订阅清单的增删查。DB 是唯一权威，与仓位参数同一个原则。"""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # 跨线程使用，线程安全由 _lock 保证
        self._lock = threading.RLock()
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)
        self._con.commit()

    @synchronized
    def close(self) -> None:
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---------- 读 ----------
    @synchronized
    def all(self) -> List[dict]:
        rows = self._con.execute(
            "SELECT symbol, enabled, note, created_at FROM market_subscriptions "
            "ORDER BY symbol").fetchall()
        return [{"symbol": r[0], "enabled": bool(r[1]), "note": r[2],
                 "created_at": r[3]} for r in rows]

    @synchronized
    def symbols(self, only_enabled: bool = True) -> List[str]:
        """给行情管道用的品种列表。默认只要启用的。"""
        sql = "SELECT symbol FROM market_subscriptions"
        if only_enabled:
            sql += " WHERE enabled = 1"
        return [r[0] for r in self._con.execute(sql + " ORDER BY symbol")]

    @synchronized
    def is_subscribed(self, symbol: str) -> bool:
        r = self._con.execute(
            "SELECT enabled FROM market_subscriptions WHERE symbol = ?",
            (normalize(symbol),)).fetchone()
        return bool(r and r[0])

    # ---------- 写 ----------
    @synchronized
    def add(self, symbol: str, note: str = "") -> str:
        s = normalize(symbol)
        self._con.execute(
            "INSERT INTO market_subscriptions (symbol, enabled, note) VALUES (?,1,?) "
            "ON CONFLICT(symbol) DO UPDATE SET enabled=1, "
            "  note=CASE WHEN excluded.note<>'' THEN excluded.note ELSE note END",
            (s, note))
        self._con.commit()
        logger.info("订阅品种已启用: %s", s)
        return s

    @synchronized
    def remove(self, symbol: str) -> bool:
        s = normalize(symbol)
        cur = self._con.execute(
            "DELETE FROM market_subscriptions WHERE symbol = ?", (s,))
        self._con.commit()
        return cur.rowcount > 0

    @synchronized
    def set_enabled(self, symbol: str, enabled: bool) -> bool:
        """开关而不删除：保留历史与备注，只是停止收数据。"""
        s = normalize(symbol)
        cur = self._con.execute(
            "UPDATE market_subscriptions SET enabled = ? WHERE symbol = ?",
            (1 if enabled else 0, s))
        self._con.commit()
        return cur.rowcount > 0

    @synchronized
    def seed(self, symbols: List[str]) -> int:
        """首次启动把 config 里的品种播种进来（只增不改已有开关状态）。"""
        n = 0
        for s in symbols:
            try:
                self._con.execute(
                    "INSERT INTO market_subscriptions (symbol, enabled) VALUES (?,1) "
                    "ON CONFLICT(symbol) DO NOTHING", (normalize(s),))
                n += 1
            except ValueError:
                logger.warning("忽略非法品种: %r", s)
        self._con.commit()
        return n
