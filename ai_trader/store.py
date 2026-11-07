"""SQLite 存储：单写线程 + 队列（抄 k-trader 的单写者模式），WAL，按周期滚动保留。

只做三件事：存 K 线桶、存数据源健康事件、存缺口记录。交易相关表在阶段 2 再加。
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

# 注意：索引不写在这里。旧库的 bars 还没有 symbol 列，
# 直接 CREATE INDEX ON bars(symbol, ...) 会报 no such column。
# 索引统一放到 _migrate() 里，等表结构就位后再建。
SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT NOT NULL DEFAULT 'BTCUSDT',
    interval_ms INTEGER NOT NULL,
    open_time   INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL,
    v REAL, q REAL, n INTEGER, b REAL, s REAL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval_ms, open_time)
);

CREATE TABLE IF NOT EXISTS feed_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    level  TEXT NOT NULL,
    source TEXT NOT NULL,
    event  TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_feed_ts ON feed_events(ts);

CREATE TABLE IF NOT EXISTS bar_gaps (
    symbol      TEXT NOT NULL DEFAULT 'BTCUSDT',
    interval_ms INTEGER NOT NULL,
    open_time   INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval_ms, open_time)
);
"""


DEFAULT_SYMBOL = "BTCUSDT"


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute("PRAGMA table_info(%s)" % table))


def _migrate(conn: sqlite3.Connection) -> None:
    """把老的单品种库升级成带 symbol 的多品种库。

    为什么必须重建表而不是 ALTER：SQLite 不支持改主键。
    老表 PK 是 (interval_ms, open_time)，新表要 (symbol, interval_ms, open_time)，
    只能建新表 -> 搬数据 -> 删旧表 -> 改名。

    迁移的语义决定（写下来避免以后有人"优化"掉）：
      老数据全部归到 DEFAULT_SYMBOL。这是**事实**而不是猜测——
      这个库在多品种之前只收过一个品种，就是它。
    整个过程在一个事务里，失败就整体回滚，不留半张表。
    """
    todo = []
    if not _has_column(conn, "bars", "symbol"):
        todo.append("bars")
    if not _has_column(conn, "bar_gaps", "symbol"):
        todo.append("bar_gaps")
    if todo:
        logger.warning("bars 表结构升级中（加 symbol 列）: %s", ",".join(todo))
        conn.execute("BEGIN")
        try:
            if "bars" in todo:
                conn.execute("""
                    CREATE TABLE bars_new (
                        symbol      TEXT NOT NULL DEFAULT 'BTCUSDT',
                        interval_ms INTEGER NOT NULL,
                        open_time   INTEGER NOT NULL,
                        o REAL, h REAL, l REAL, c REAL,
                        v REAL, q REAL, n INTEGER, b REAL, s REAL,
                        updated_at  INTEGER NOT NULL,
                        PRIMARY KEY (symbol, interval_ms, open_time)
                    )""")
                conn.execute(
                    "INSERT INTO bars_new "
                    "(symbol,interval_ms,open_time,o,h,l,c,v,q,n,b,s,updated_at) "
                    "SELECT ?,interval_ms,open_time,o,h,l,c,v,q,n,b,s,updated_at "
                    "FROM bars", (DEFAULT_SYMBOL,))
                n = conn.execute("SELECT COUNT(*) FROM bars_new").fetchone()[0]
                conn.execute("DROP TABLE bars")
                conn.execute("ALTER TABLE bars_new RENAME TO bars")
                logger.warning("  bars 已迁移 %d 行 -> symbol=%s", n, DEFAULT_SYMBOL)
            if "bar_gaps" in todo:
                conn.execute("""
                    CREATE TABLE bar_gaps_new (
                        symbol      TEXT NOT NULL DEFAULT 'BTCUSDT',
                        interval_ms INTEGER NOT NULL,
                        open_time   INTEGER NOT NULL,
                        detected_at INTEGER NOT NULL,
                        PRIMARY KEY (symbol, interval_ms, open_time)
                    )""")
                conn.execute(
                    "INSERT INTO bar_gaps_new "
                    "(symbol,interval_ms,open_time,detected_at) "
                    "SELECT ?,interval_ms,open_time,detected_at FROM bar_gaps",
                    (DEFAULT_SYMBOL,))
                conn.execute("DROP TABLE bar_gaps")
                conn.execute("ALTER TABLE bar_gaps_new RENAME TO bar_gaps")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_time ON bars(symbol, open_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gaps_time ON bar_gaps(symbol, open_time)")
    conn.commit()


class Store:
    def __init__(self, path: str, symbol: str = DEFAULT_SYMBOL):
        self._path = path
        # 默认品种：交易品种。已有的单品种调用方不需要改一行代码。
        # 多品种写入时显式传 symbol。
        self.symbol = symbol
        self._q: "queue.Queue" = queue.Queue(maxsize=200000)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._conn: Optional[sqlite3.Connection] = None
        self.written = 0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        _migrate(self._conn)          # 老库升级：加 symbol 列、重建主键
        self._thread = threading.Thread(target=self._writer, name="store-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._conn:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass

    # ---------- 写入接口（非阻塞） ----------
    def put_bar(self, bar, symbol: Optional[str] = None) -> None:
        self._enqueue(("bar", (symbol or self.symbol,
                               bar.interval_ms, bar.open_time, bar.open, bar.high,
                               bar.low, bar.close, bar.volume, bar.quote_volume,
                               bar.trades, bar.buy_base, bar.sell_base,
                               int(time.time() * 1000))))

    def put_event(self, level: str, source: str, event: str, detail: str = "") -> None:
        self._enqueue(("event", (int(time.time() * 1000), level, source, event, detail)))

    def put_gap(self, interval_ms: int, open_time: int,
                symbol: Optional[str] = None) -> None:
        self._enqueue(("gap", (symbol or self.symbol, interval_ms, open_time,
                               int(time.time() * 1000))))

    def _enqueue(self, item) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            logger.error("store 队列已满，丢弃一条写入")

    def _writer(self) -> None:
        batch: List[tuple] = []
        last_flush = time.time()
        while not self._stop.is_set() or not self._q.empty():
            try:
                batch.append(self._q.get(timeout=0.2))
            except queue.Empty:
                pass
            if not batch:
                continue
            if len(batch) < 200 and (time.time() - last_flush) < 0.5:
                continue
            self._flush(batch)
            batch = []
            last_flush = time.time()
        if batch:
            self._flush(batch)

    def _flush(self, batch: List[tuple]) -> None:
        if not self._conn:
            return
        try:
            bars = [x[1] for x in batch if x[0] == "bar"]
            evs = [x[1] for x in batch if x[0] == "event"]
            gaps = [x[1] for x in batch if x[0] == "gap"]
            if bars:
                self._conn.executemany(
                    "INSERT INTO bars(symbol,interval_ms,open_time,o,h,l,c,v,q,n,b,s,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(symbol,interval_ms,open_time) DO UPDATE SET"
                    " o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,v=excluded.v,"
                    " q=excluded.q,n=excluded.n,b=excluded.b,s=excluded.s,"
                    " updated_at=excluded.updated_at", bars)
                self.written += len(bars)
            if evs:
                self._conn.executemany(
                    "INSERT INTO feed_events(ts,level,source,event,detail) VALUES(?,?,?,?,?)", evs)
            if gaps:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO bar_gaps"
                    "(symbol,interval_ms,open_time,detected_at) VALUES(?,?,?,?)", gaps)
            self._conn.commit()
        except Exception:
            logger.error("store flush 失败", exc_info=True)

    # ---------- 读取（只读连接，供 web 用） ----------
    def query_bars(self, interval_ms: int, limit: int = 500, before: int = 0,
                   symbol: Optional[str] = None) -> List[dict]:
        # symbol 缺省用 store 自己的品种，所以旧的单品种调用方一行都不用改。
        # 但**必须**带 symbol 过滤：不带的话多品种写进来后会混着查出来，
        # 画出来的 K 线是两个品种交错的，看不出错还以为行情疯了。
        sym = symbol or self.symbol
        conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            if before:
                rows = conn.execute(
                    "SELECT open_time,o,h,l,c,v,q,n,b,s FROM bars"
                    " WHERE symbol=? AND interval_ms=? AND open_time<?"
                    " ORDER BY open_time DESC LIMIT ?",
                    (sym, interval_ms, before, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT open_time,o,h,l,c,v,q,n,b,s FROM bars"
                    " WHERE symbol=? AND interval_ms=?"
                    " ORDER BY open_time DESC LIMIT ?",
                    (sym, interval_ms, limit)).fetchall()
            rows.reverse()
            return [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                     "v": r[5], "q": r[6], "n": r[7], "b": r[8], "s": r[9]} for r in rows]
        finally:
            conn.close()

    def recent_events(self, limit: int = 50) -> List[dict]:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            rows = conn.execute(
                "SELECT ts,level,source,event,detail FROM feed_events"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{"ts": r[0], "level": r[1], "source": r[2], "event": r[3], "detail": r[4]}
                    for r in rows]
        finally:
            conn.close()

    def prune(self, keep_hours: int) -> int:
        """按周期滚动保留：3s/15s/1m 只保留最近 N 小时，3m/15m 全留。"""
        cutoff = int(time.time() * 1000) - keep_hours * 3600 * 1000
        conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            cur = conn.execute("DELETE FROM bars WHERE interval_ms < 180000 AND open_time < ?",
                               (cutoff,))
            conn.execute("DELETE FROM feed_events WHERE ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
