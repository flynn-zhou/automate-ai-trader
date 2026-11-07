"""研究日志：把「思考 -> 假设 -> 实验 -> 验证 -> 转折 -> 再验证」写进库，并在大屏可见。

为什么单独一张表：
k-trader 留了 454 份文档，但没人知道当时"为什么改这个参数"，最后连作者自己都追不回来。
这里把推理过程和交易数据放在同一个库里，任何一条参数修改都能追溯到它的动机与验证结果。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    kind    TEXT NOT NULL,      -- hypothesis|experiment|result|pivot|verify|note|alert
    title   TEXT NOT NULL,
    body    TEXT NOT NULL,
    data    TEXT,               -- JSON 证据
    author  TEXT DEFAULT 'ai'
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);
"""


class Journal:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def start(self) -> None:
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def stop(self) -> None:
        if self._conn:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass

    def add(self, kind: str, title: str, body: str, data: Optional[str] = None,
            author: str = "ai") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO journal(ts,kind,title,body,data,author) VALUES(?,?,?,?,?,?)",
                (int(time.time() * 1000), kind, title, body, data, author))
            self._conn.commit()
            return cur.lastrowid

    def recent(self, limit: int = 100) -> List[dict]:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            rows = conn.execute(
                "SELECT id,ts,kind,title,body,data,author FROM journal"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        return [{"id": r[0], "ts": r[1], "kind": r[2], "title": r[3], "body": r[4],
                 "data": r[5], "author": r[6]} for r in rows]
