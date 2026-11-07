# -*- coding: utf-8 -*-
"""区间演化存储层：bars_1h / range_evolution / range_evolution_events。

为什么价格与四线分表
--------------------
原始行情是**事实**，四线是**模型产出**。引擎改版时要能用同一份价格重算四线，
价格不能跟着模型一起漂。两者靠 ts（UTC 毫秒）一一对齐。

三张表的分工
------------
    bars_1h                  那根 1h 的 OHLCV
    range_evolution          那根 1h 的四条线是多少
    range_evolution_events   那根 1h 发生了什么（突破/反包/成形/确认/插针/R滚动）

命名纪律（v11）
---------------
    soft_up / soft_dn   软边界 S（展示/风控位，**永不参与触发判定**）
    hard_up / hard_dn   反向边界 R（被收盘越过 ⇒ 反转）
    lo_b  / hi_b        顺向边界 P（被收盘越过 ⇒ 突破）
    v11_key             顺向关键价 K（每根 min/max，无滞后，只记录）

⚠️ 历史残留：老库里 hard_dn 的注释写成「K = 下插针最低价」，那是 v9 的旧语义。
v11 已把 K 独立成 v11_key，hard_dn 只表示 R。本文件保留列名（生产兼容），
但语义以本节为准，不再沿用旧注释。
"""
from __future__ import annotations

import os
import sqlite3
import threading

from ..dbutil import synchronized as _synchronized
from typing import Iterable, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS range_evolution (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      VARCHAR(20) NOT NULL,
    ts          INTEGER NOT NULL,
    ts_h        VARCHAR(20) NOT NULL,
    soft_up     REAL,
    soft_dn     REAL,
    hard_up     REAL,
    hard_dn     REAL,
    v11_key     REAL,
    family      INTEGER,
    segment     INTEGER,
    direction   VARCHAR(8),
    tag         VARCHAR(16),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_range_evolution_ts ON range_evolution(symbol, ts);

CREATE TABLE IF NOT EXISTS range_evolution_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      VARCHAR(20) NOT NULL,
    ts          INTEGER NOT NULL,
    ts_h        VARCHAR(20) NOT NULL,
    event       VARCHAR(32) NOT NULL,
    event_ts    TEXT NOT NULL,
    trigger     REAL,
    family      INTEGER,
    segment     INTEGER,
    ref_name    VARCHAR(32),
    ref_value   REAL,
    margin_pct  REAL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_range_evo_ev_ts ON range_evolution_events(symbol, ts);

CREATE TABLE IF NOT EXISTS bars_1h (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      VARCHAR(20) NOT NULL,
    ts          INTEGER NOT NULL,
    ts_h        VARCHAR(20) NOT NULL,
    open        REAL, high REAL, low REAL, close REAL,
    volume      REAL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_1h_ts ON bars_1h(symbol, ts);
"""


def rows_from_states(states: Iterable[Sequence], ts8) -> List[Tuple]:
    """引擎 states 元组 -> range_evolution 行（11 字段，含 ts_h）。

    引擎元组（v9.1，前 9 位与 v9 一致，新字段追加尾部）：
        (ts, lo_b, hi_b, seg_serial, direction, hard, soft, key, tag, family, segment)
    方向映射（与 api._row 唯一一套口径，不得另写一份）：
        DOWN: soft_up=S,       soft_dn=lo_b(P), hard_up=hi_b(R), hard_dn=None
        UP  : soft_up=hi_b(P), soft_dn=S,       hard_up=None,     hard_dn=lo_b(R)
    """
    out = []
    for s in states:
        ts, lo_b, hi_b, _ss, d, hard, soft, key, tag, fam, seg = s[:11]
        if d == "DOWN":
            su, sd, hu, hd = soft, lo_b, hi_b, None
        elif d == "UP":
            su, sd, hu, hd = hi_b, soft, None, lo_b
        else:                                   # PRE / 未成形
            su = sd = hu = hd = None
        out.append((ts, ts8(ts), su, sd, hu, hd, key, fam, seg, d, tag))
    return out


def rows_from_bars(bars: Iterable[Sequence], ts8) -> List[Tuple]:
    """引擎 1h bars -> bars_1h 行。bars 为 6 元组 (ts,o,h,l,c,v) 或 5 元组。"""
    out = []
    for b in bars:
        ts, o, h, l, c = b[0], b[1], b[2], b[3], b[4]
        v = b[5] if len(b) > 5 else None
        out.append((ts, ts8(ts), o, h, l, c, v))
    return out


def rows_from_events(events: Iterable[Sequence], lab) -> List[Tuple]:
    """引擎 events 元组 -> range_evolution_events 行。

    引擎元组：(时间, 事件, 价格, family, segment, 参照量名, 参照量值, 裕度%)
    其中「时间」是北京时区串，反解成 UTC ms 作为 ts。
    """
    out = []
    for e in events:
        ts_h, event, trigger, fam, seg = e[0], e[1], e[2], e[3], e[4]
        ref_name = e[5] if len(e) > 5 else None
        ref_value = e[6] if len(e) > 6 else None
        margin = e[7] if len(e) > 7 else None
        try:
            ts = lab(ts_h)
        except Exception:
            ts = 0
        out.append((ts, ts_h, event, ts_h, trigger, fam, seg,
                    ref_name, ref_value, margin))
    return out


class RangeStore:
    """区间演化三表的最小仓储。批量写、只增不改、按 (symbol, ts) 幂等。"""

    _STATE_COLS = ("symbol, ts, ts_h, soft_up, soft_dn, hard_up, hard_dn, "
                   "v11_key, family, segment, direction, tag")

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # check_same_thread=False 是项目既有约定；线程安全由 _lock 保证
        self._lock = threading.RLock()
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)
        self._con.commit()

    @_synchronized
    def close(self) -> None:
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---------- 写入 ----------
    @_synchronized
    def upsert_bars_1h(self, symbol: str, rows: Iterable[Tuple]) -> int:
        payload = [(symbol,) + tuple(r) for r in rows]
        if not payload:
            return 0
        cur = self._con.executemany(
            "INSERT INTO bars_1h (symbol, ts, ts_h, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, ts) DO UPDATE SET "
            "  ts_h=excluded.ts_h, open=excluded.open, high=excluded.high, "
            "  low=excluded.low, close=excluded.close, volume=excluded.volume",
            payload)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def upsert_states(self, symbol: str, rows: Iterable[Tuple]) -> int:
        payload = [(symbol,) + tuple(r) for r in rows]
        if not payload:
            return 0
        cur = self._con.executemany(
            "INSERT INTO range_evolution (" + self._STATE_COLS + ") "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, ts) DO UPDATE SET "
            "  ts_h=excluded.ts_h, soft_up=excluded.soft_up, "
            "  soft_dn=excluded.soft_dn, hard_up=excluded.hard_up, "
            "  hard_dn=excluded.hard_dn, v11_key=excluded.v11_key, "
            "  family=excluded.family, segment=excluded.segment, "
            "  direction=excluded.direction, tag=excluded.tag",
            payload)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def replace_events(self, symbol: str, rows: Iterable[Tuple]) -> int:
        payload = [(symbol,) + tuple(r) for r in rows]
        self._con.execute("DELETE FROM range_evolution_events WHERE symbol = ?", (symbol,))
        if not payload:
            self._con.commit()
            return 0
        cur = self._con.executemany(
            "INSERT INTO range_evolution_events "
            "(symbol, ts, ts_h, event, event_ts, trigger, family, segment, "
            " ref_name, ref_value, margin_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            payload)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def append_events(self, symbol: str, rows: Iterable[Tuple]) -> int:
        """纯追加事件，不动已有行。

        为什么不用 replace_events：那是给"整段重建"用的（先 DELETE 再全写）。
        实时推进是增量的，用 replace 会把回填出来的历史事件整段抹掉——
        而且抹掉之后看不出来，因为图还是照常能画。
        去重由调用方按 (ts, event) 判好再传进来；这里只负责写。
        """
        payload = [(symbol,) + tuple(r) for r in rows]
        if not payload:
            return 0
        cur = self._con.executemany(
            "INSERT INTO range_evolution_events "
            "(symbol, ts, ts_h, event, event_ts, trigger, family, segment, "
            " ref_name, ref_value, margin_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            payload)
        self._con.commit()
        return cur.rowcount

    # ---------- 读取 ----------
    @_synchronized
    def bars_1h(self, symbol: str, since_ts: Optional[int] = None) -> List[Tuple]:
        sql = ("SELECT ts, ts_h, open, high, low, close, volume FROM bars_1h "
               "WHERE symbol = ?")
        args: List = [symbol]
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        return self._con.execute(sql + " ORDER BY ts", args).fetchall()

    @_synchronized
    def states(self, symbol: str, since_ts: Optional[int] = None) -> List[Tuple]:
        sql = ("SELECT ts, ts_h, soft_up, soft_dn, hard_up, hard_dn, v11_key, "
               "family, segment, direction, tag FROM range_evolution WHERE symbol = ?")
        args: List = [symbol]
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        return self._con.execute(sql + " ORDER BY ts", args).fetchall()

    @_synchronized
    def events(self, symbol: str, since_ts: Optional[int] = None) -> List[Tuple]:
        sql = ("SELECT ts, ts_h, event, event_ts, trigger, family, segment, "
               "ref_name, ref_value, margin_pct FROM range_evolution_events "
               "WHERE symbol = ?")
        args: List = [symbol]
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        return self._con.execute(sql + " ORDER BY ts", args).fetchall()

    # ---------- 按时间点查询（功能型方法的主入口）----------
    @_synchronized
    def state_at(self, symbol: str, ts: int) -> Optional[Tuple]:
        """返回 <= ts 的最后一根四线状态。早于第一根时返回 None。

        这是「给指定时间，说出当时区间」的权威实现：直接查库，不重算。
        重算（引擎 as_of）只在库没数据时才用——两者口径必须一致，测试里对齐过。
        """
        return self._con.execute(
            "SELECT ts, ts_h, soft_up, soft_dn, hard_up, hard_dn, v11_key, "
            "family, segment, direction, tag FROM range_evolution "
            "WHERE symbol = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (symbol, int(ts))).fetchone()

    @_synchronized
    def bar_at(self, symbol: str, ts: int) -> Optional[Tuple]:
        """返回 <= ts 的最后一根 1h K 线。"""
        return self._con.execute(
            "SELECT ts, ts_h, open, high, low, close, volume FROM bars_1h "
            "WHERE symbol = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (symbol, int(ts))).fetchone()

    @_synchronized
    def state_at_dict(self, symbol: str, ts: int) -> Optional[dict]:
        """state_at + bar_at 合成一个可直接 JSON 化的字典。"""
        s = self.state_at(symbol, ts)
        if s is None:
            return None
        b = self.bar_at(symbol, ts)
        return {
            "ts": s[0], "ts_h": s[1],
            "soft_up": s[2], "soft_dn": s[3],
            "hard_up": s[4], "hard_dn": s[5], "key": s[6],
            "family": s[7], "segment": s[8], "direction": s[9], "tag": s[10],
            "label": ("#%d.%d" % (s[7], s[8])) if s[7] else "",
            "bar": ({"ts": b[0], "open": b[2], "high": b[3],
                     "low": b[4], "close": b[5], "volume": b[6]}
                    if b else None),
        }

    @_synchronized
    def counts(self, symbol: str) -> dict:
        def n(t):
            return self._con.execute(
                "SELECT COUNT(*) FROM %s WHERE symbol = ?" % t, (symbol,)).fetchone()[0]
        return {"bars_1h": n("bars_1h"),
                "range_evolution": n("range_evolution"),
                "range_evolution_events": n("range_evolution_events")}


def persist_run(store: RangeStore, symbol: str, bars, events, states, ts8, lab) -> dict:
    """把一次完整引擎运行的结果落库（bars / 四线 / 事件），返回各表写入量。"""
    nb = store.upsert_bars_1h(symbol, rows_from_bars(bars, ts8))
    ns = store.upsert_states(symbol, rows_from_states(states, ts8))
    ne = store.replace_events(symbol, rows_from_events(events, lab))
    return {"bars_1h": nb, "range_evolution": ns, "range_evolution_events": ne}