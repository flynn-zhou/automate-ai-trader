# -*- coding: utf-8 -*-
"""`pending_theses`：把一条预期（thesis）的推进状态落库，重启能接上。

★ 为什么必须有这张表

  `thesis.run()` 是一条纯函数：给它一段 K，它返回终态。整个生命周期只活在
  一次调用里 —— **生产环境里这不够**：

    · 引擎每根 K 推进一次，两次推进之间进程可能重启；
    · 挂单价已经发出去了（交易所那头有一张真实的单），本地却把状态忘了，
      重启后要么重复挂单（同一笔仓两个止损 → 后触发的把剩仓反向开出去），
      要么永远等一个不存在的「成交」；
    · 纸面模式同理，而且更隐蔽：重启后 `PaperExecutor` 从库里恢复仓位，
      但没人知道那条 thesis 推进到哪了，于是它永远不会被平仓。

★ 幂等怎么做

  不用「重放后去重」（那要求动作可比较、且重放必须逐字一致），而是
  两条更硬的规矩：

    1. **游标**：`next_index` 是「已判过的最后一根 + 1」。续跑从它开始，
       所以同一根 K 不会被判两次。
    2. **动作 id**：每个动作带 `thesis_id:bar_ts:kind` 的稳定 id，
       真的重复了也能在落库时用主键挡住（`INSERT OR IGNORE`）。

  两条加起来，重复推进是**幂等**的；而「跳过一根」比「重复一根」危险得多，
  所以游标只在动作真的落地之后才前移（见 `commit`）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_theses (
    thesis_id       TEXT PRIMARY KEY,
    -- 计划本身（重放时要能原样重建 Thesis，所以这些字段必须存全）
    kind            TEXT NOT NULL DEFAULT '',
    direction       TEXT NOT NULL DEFAULT '',
    family          TEXT NOT NULL DEFAULT '',
    order_price     REAL NOT NULL DEFAULT 0,
    stop_price      REAL NOT NULL DEFAULT 0,
    target_price    REAL NOT NULL DEFAULT 0,
    order_type      TEXT NOT NULL DEFAULT 'LIMIT',
    horizon_bars    INTEGER NOT NULL DEFAULT 0,
    stale_distance  REAL NOT NULL DEFAULT 0,
    surprise_pts    REAL NOT NULL DEFAULT 0,
    bar_ts          INTEGER NOT NULL DEFAULT 0,
    -- 状态机游标
    state           TEXT NOT NULL DEFAULT 'EXPECTED',
    next_index      INTEGER NOT NULL DEFAULT 0,
    armed_at_bar    INTEGER,
    armed_ref_close REAL,
    -- 挂单那一刻的 5m 子 K 时间戳（成交守卫的主锚点，见 thesis.step）
    --
    -- ★ 2026-11-25 补：这一列以前**不存在**，`armed_at_sub_ts` 只活在内存里，
    --   重启/重建 `Thesis` 后永远是 None，于是守卫的时间戳分支在生产是
    --   死代码 —— 只剩 `armed_at_bar` 这个**数组下标**在扛。
    --   下标会随缓冲前移而漂移（事故现场就是 next_index 被重算到 386
    --   而 armed_at_bar 是 399），时间戳不会。所以必须落库。
    armed_at_sub_ts INTEGER,
    -- 成交与出场的事实（引擎侧写，状态机侧只读）
    filled_ts       INTEGER NOT NULL DEFAULT 0,
    filled_price    REAL NOT NULL DEFAULT 0,
    exit_ts         INTEGER NOT NULL DEFAULT 0,
    exit_price      REAL NOT NULL DEFAULT 0,
    net_r           REAL NOT NULL DEFAULT 0,
    -- 记账
    ambiguous       INTEGER NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    updated_ts      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending_thesis_actions (
    action_id   TEXT PRIMARY KEY,
    thesis_id   TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    state_after TEXT NOT NULL DEFAULT '',
    bar_ts      INTEGER NOT NULL DEFAULT 0,
    price       REAL NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL DEFAULT '{}',
    applied     INTEGER NOT NULL DEFAULT 0,
    created_ts  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pta_thesis ON pending_thesis_actions(thesis_id);
"""

#: 落库时跟着 thesis 一起存的字段名（顺序即建表顺序）
FIELDS = ("kind", "direction", "family", "order_price", "stop_price",
          "target_price", "order_type", "horizon_bars", "stale_distance",
          "surprise_pts", "bar_ts")


def action_id(thesis_id: str, bar_ts: int, kind: str) -> str:
    """★ 稳定的动作 id。

    为什么要「稳定」而不是 uuid：重启后同一条动作会被重新算出来一次，
    两次必须是同一个 id，否则「已经做过」这件事永远判不出来，
    止损会被平两次（第二次变成反向开仓）。
    """
    return "%s:%d:%s" % (thesis_id, int(bar_ts), kind or "")


class PendingThesisStore:
    """一张表的增删查改。刻意不碰 TradeDB 的连接池 —— 引擎那条写入是
    异步队列、这一条要同步读回，混在一起会互相拖死（第 131 轮 same lesson）。
    """

    def __init__(self, path: str):
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """给**已存在**的库补新列。幂等：重复跑不出错。

        ★ 为什么不能只改 `SCHEMA`：`CREATE TABLE IF NOT EXISTS` 对已经存在的
          表没有任何作用，生产库是 11-25 之前建的，改完 SCHEMA 那一列
          依然不存在 —— 而 `upsert_thesis` 会因为「表里没这列」直接报错。
          所以必须显式 ALTER。

        ★ 为什么用 ALTER 补而不是重建表：补一列可以保留全部历史行
          （这些预期的历史状态是事故复盘和重放量具的输入，不能丢）。
        """
        have = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(pending_theses)")}
        if "armed_at_sub_ts" not in have:
            self._conn.execute(
                "ALTER TABLE pending_theses ADD COLUMN armed_at_sub_ts INTEGER")
            self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- 写
    def upsert_thesis(self, row: Dict[str, Any]) -> None:
        """把一条 thesis 的当前状态写进去（按 thesis_id 覆盖）。"""
        cols = ", ".join(FIELDS)
        marks = ", ".join("?" for _ in FIELDS)
        sets = ", ".join("%s=excluded.%s" % (f, f) for f in FIELDS)
        extra = ("state", "next_index", "armed_at_bar", "armed_at_sub_ts",
                 "armed_ref_close", "ambiguous", "note", "updated_ts")
        cols2 = cols + ", " + ", ".join(extra)
        marks2 = marks + ", " + ", ".join("?" for _ in extra)
        sets2 = sets + ", " + ", ".join("%s=excluded.%s" % (f, f) for f in extra)
        vals = [row.get(f, 0) for f in FIELDS]
        vals += [row.get("state", "EXPECTED"), int(row.get("next_index", 0)),
                 row.get("armed_at_bar"), row.get("armed_at_sub_ts"),
                 row.get("armed_ref_close"),
                 1 if row.get("ambiguous") else 0, row.get("note", ""),
                 int(row.get("updated_ts") or time.time() * 1000)]
        self._conn.execute(
            "INSERT INTO pending_theses (thesis_id, %s) VALUES (?, %s) "
            "ON CONFLICT(thesis_id) DO UPDATE SET %s"
            % (cols2, marks2, sets2), [row["thesis_id"]] + vals)
        self._conn.commit()

    def set_fill(self, thesis_id: str, ts: int, price: float) -> None:
        self._conn.execute(
            "UPDATE pending_theses SET filled_ts=?, filled_price=?, "
            "updated_ts=? WHERE thesis_id=?", (int(ts), float(price),
                                               int(time.time() * 1000), thesis_id))
        self._conn.commit()

    def set_exit(self, thesis_id: str, ts: int, price: float, net_r: float,
                 note: str = "") -> None:
        self._conn.execute(
            "UPDATE pending_theses SET exit_ts=?, exit_price=?, net_r=?, "
            "note=?, updated_ts=? WHERE thesis_id=?",
            (int(ts), float(price), float(net_r), note,
             int(time.time() * 1000), thesis_id))
        self._conn.commit()

    def put_action(self, *, thesis_id: str, kind: str, state_after: str,
                   bar_ts: int, price: float = 0.0,
                   payload: Optional[Dict[str, Any]] = None) -> bool:
        """记一条待执行动作。返回 True = **这次是新插入的**。

        ★ 返回值是幂等的关键：False 表示「这条动作以前算出来过」，
          调用方据此**跳过执行**（否则止损会被平第二次）。
        """
        aid = action_id(thesis_id, bar_ts, kind)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO pending_thesis_actions "
            "(action_id, thesis_id, kind, state_after, bar_ts, price, payload, "
            " applied, created_ts) VALUES (?,?,?,?,?,?,?,0,?)",
            (aid, thesis_id, kind, state_after, int(bar_ts), float(price),
             json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
             int(time.time() * 1000)))
        self._conn.commit()
        return cur.rowcount > 0

    def mark_applied(self, thesis_id: str, kind: str, bar_ts: int) -> None:
        self._conn.execute(
            "UPDATE pending_thesis_actions SET applied=1 WHERE action_id=?",
            (action_id(thesis_id, bar_ts, kind),))
        self._conn.commit()

    # ---------------------------------------------------------------- 读
    def get(self, thesis_id: str) -> Optional[Dict[str, Any]]:
        r = self._conn.execute(
            "SELECT * FROM pending_theses WHERE thesis_id=?",
            (thesis_id,)).fetchone()
        return dict(r) if r else None

    def live(self, limit: int = 200) -> List[Dict[str, Any]]:
        """还没进终态的那些 —— 引擎每根 K 要推进的就是它们。"""
        # ★★ 终态必须从 `thesis.TERMINAL_STATES` 来，
        # 不能这里再拄一份。
        #
        #   这里曾经是硬编码的六个状态。
        #   S5 加了 `CLOSED_BREAKEVEN` 之后，被保本棘轮平掉的仓位
        #   **不在这个名单里** → `live()` 仍然把它们当成活的，
        #   于是每根 5m 都会去推进一个已经平掉的仓位，
        #   并且把重算出的动作再执行一次。
        #
        #   这正是项目里反复出事的「同一事实两个来源」。
        #   现在只保留一个来源。
        from .thesis import TERMINAL_STATES
        terms = tuple(sorted(TERMINAL_STATES))
        marks = ", ".join("?" for _ in terms)
        rows = self._conn.execute(
            "SELECT * FROM pending_theses WHERE state NOT IN (%s) "
            "ORDER BY bar_ts LIMIT ?" % marks, list(terms) + [int(limit)])
        return [dict(r) for r in rows]

    def add_if_new(self, row: Dict[str, Any]) -> bool:
        """只在不存在时插入。返回 True = 真的新增了。

        ★★ 为什么不能用 `upsert_thesis()`
          `upsert_thesis` 是 `ON CONFLICT … DO UPDATE`，它的用途是“推进之后
          更新游标”。拿它来插新计划，一旦 thesis_id 碰巧重复
          （比如重启后重算同一根 K），就会把一条**已经平掉的**
          预期重置回 EXPECTED，然后重新挂单、重新成交 —— 一次开仓变两次。
        """
        cols = list(row.keys())
        sql = ("INSERT OR IGNORE INTO pending_theses (%s) VALUES (%s)"
               % (", ".join(cols), ", ".join("?" for _ in cols)))
        cur = self._conn.execute(sql, [row[c] for c in cols])
        self._conn.commit()
        return cur.rowcount > 0

    def all(self, limit: int = 1000) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM pending_theses ORDER BY bar_ts LIMIT ?", (int(limit),))
        return [dict(r) for r in rows]

    def actions(self, thesis_id: str = "",
                applied: Optional[bool] = None, limit: int = 500
                ) -> List[Dict[str, Any]]:
        """动作流水，**包含已执行的**。``applied=None`` = 全部。

        ★ 为什么需要它（2026-11-17 补）：`pending_actions()` 只返回
          `applied=0`，那是「还没做的」。而 dry-run（只出计划不挂单）
          会把动作**立刻标成 applied**，于是它在 `pending_actions()` 里
          查不到 —— 用户第一轮就跑 dry-run，结果**没有任何地方能看到
          「它本来会做什么」**。那就不是「只出计划」，是「什么都没发生」。
        """
        sql = "SELECT * FROM pending_thesis_actions WHERE 1=1"
        args: List[Any] = []
        if thesis_id:
            sql += " AND thesis_id=?"
            args.append(thesis_id)
        if applied is not None:
            sql += " AND applied=?"
            args.append(1 if applied else 0)
        sql += " ORDER BY bar_ts, action_id LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, tuple(args))]

    def pending_actions(self, thesis_id: str = "") -> List[Dict[str, Any]]:
        if thesis_id:
            rows = self._conn.execute(
                "SELECT * FROM pending_thesis_actions WHERE thesis_id=? "
                "AND applied=0 ORDER BY bar_ts", (thesis_id,))
        else:
            rows = self._conn.execute(
                "SELECT * FROM pending_thesis_actions WHERE applied=0 "
                "ORDER BY bar_ts")
        return [dict(r) for r in rows]


def row_of(thesis, *, state: str, next_index: int,
           armed_at_bar: Optional[int] = None,
           armed_at_sub_ts: Optional[int] = None,
           armed_ref_close: Optional[float] = None,
           bar_ts: int = 0, ambiguous: bool = False,
           note: str = "") -> Dict[str, Any]:
    """把 `Thesis` + 游标拼成一行。字段名与 `FIELDS` 一一对应。

    `bar_ts` 要**显式给**：`Thesis` 里没有这个字段（它属于计划，
    `run()` 只认「从第几根开始」）。默认 0 只是为了让单测好写，
    生产路径必须传计划那根的时间戳 —— 否则重启后按时间查不到它。
    """
    return {
        "thesis_id": thesis.thesis_id,
        "kind": thesis.kind, "direction": thesis.direction,
        "family": thesis.family, "order_price": thesis.order_price,
        "stop_price": thesis.stop_price, "target_price": thesis.target_price,
        "order_type": thesis.order_type,
        "horizon_bars": int(thesis.horizon_bars),
        "stale_distance": float(thesis.stale_distance),
        "surprise_pts": float(getattr(thesis, "surprise_pts", 0.0) or 0.0),
        "bar_ts": int(bar_ts or 0),
        "state": state, "next_index": int(next_index),
        "armed_at_bar": armed_at_bar, "armed_at_sub_ts": armed_at_sub_ts,
        "armed_ref_close": armed_ref_close,
        "ambiguous": ambiguous, "note": note,
    }


def thesis_of(row: Dict[str, Any]):
    """从库里的一行重建 `Thesis`。**必须是原样重建**，否则重放结果会变。"""
    from . import thesis as T
    return T.Thesis(
        thesis_id=row["thesis_id"], kind=row["kind"],
        direction=row["direction"], order_price=float(row["order_price"]),
        stop_price=float(row["stop_price"]),
        target_price=float(row["target_price"]),
        order_type=row["order_type"],
        horizon_bars=int(row["horizon_bars"]),
        stale_distance=float(row["stale_distance"]),
        surprise_pts=float(row.get("surprise_pts") or 0.0),
        family=row["family"])


def progress_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """库里的行 → `thesis.step()` 认的 progress 字典。"""
    return {"state": row["state"], "next_index": int(row["next_index"]),
            "armed_at_bar": row["armed_at_bar"],
            # ★ 旧行没有这一列值时补 None：`step()` 里 None 会退回
            #   `armed_at_bar` 判据（见 thesis.step 的 elif 分支），
            #   也就是补列之前的行为 —— 向后兼容，不会把老预期判错。
            "armed_at_sub_ts": row.get("armed_at_sub_ts"),
            "armed_ref_close": row["armed_ref_close"]}
