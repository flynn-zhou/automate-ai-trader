"""交易审计库：决策 / 挂单 / 成交 / 持仓 / 权益，全部落库，可回放。

设计原则（来自 k-trader 的教训）：
- 每条决策都要留痕，包括"什么都没做"——否则永远回答不了"你为什么没开单"。
- 一个 PnL 内核：持仓表是唯一真相，纸面与实盘都写同一张表，只是成交价来源不同。
- 决策记录里必须写清 feature_time < decision_time（防前视）。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

def strategy_start_ts_at(path: str, strategy: str) -> int:
    """当前策略第一次发出决策的时间；查不到返回 0（= 全历史）。

    为什么要按策略切分：换策略之后，旧策略赚过又亏掉的权益
    不该继续当新策略的回撤基准。用全历史 MAX 会得出
    「趋势模式一开局就回撤 28%」这种永远消不掉的假警报。

    写成模块级函数是为了让 guard（持有 TradeDB 对象）和
    sentinel（只持有库路径）用【同一份实现】。
    否则同一个「回撤」概念在两处算出两个数——
    而其中一个还是真正下闸的那个。
    """
    if not path or not os.path.exists(path):
        return 0
    like = "%s%%" % (strategy or "")
    c = sqlite3.connect(path)
    try:
        r = c.execute("SELECT MIN(ts) FROM decisions WHERE signal LIKE ?",
                      (like,)).fetchone()
    finally:
        c.close()
    return int(r[0]) if r and r[0] else 0


def equity_has_paper(path: str) -> bool:
    """equity 表是否有 paper 列。

    账本过滤是后加的，恢复一个更早的库时可能没有这一列。
    那种情况下【必须退化成不过滤】，而不是抛 OperationalError ——
    抛异常会让哨兵整个巡检崩掉，"没有警报"比"警报口径不完美"危险得多。
    """
    try:
        c = sqlite3.connect(path)
        try:
            return any(r[1] == "paper"
                       for r in c.execute("PRAGMA table_info(equity)"))
        finally:
            c.close()
    except Exception:
        return False


def peak_equity_at(path: str, strategy: Optional[str] = None,
                   paper: Optional[int] = None,
                   since_ts: int = 0) -> float:
    """当前策略时代内的最高权益；查不到返回 0.0。

    ★ paper 必须按账本过滤，否则纸面峰值会污染实盘风控。
    第 59 轮实测：库里纸面峰值 250、实盘刚注资 100，
    恢复出来的峰值就是 250 —— 实盘第一个决策周期就判
    "从峰值 250 回撤 60%"，直接硬熔断，**永远开不了仓**。
    真实数据里已经有这个偏差：服务器上 paper=1 的峰值是 104.74，
    实盘从 100 起步就等于凭空带着 4.5% 的回撤。
    纸面的钱不是真钱，不该拿来当实盘的风控基准。

    ★ 这个值必须【持久化在库里】而不是放在内存里。
    内存版一重启就归零，于是「从峰值回撤」变成「从上次重启回撤」，
    风控闸会静默失效——而那正是最需要在重启后仍然有效的东西。

    实测过的漏判：真实峰值 190、权益跌到 40（真实回撤 78.9%），
    但重启后峰值为 80，Guard 只看到 50%，60% 硬停不触发。
    """
    since = strategy_start_ts_at(path, strategy) if strategy else 0
    # 基线时刻只能把起点【往后】推，不能往前拉。
    # 资金被提走又注入之后，旧峰值属于另一笔钱，不该继续压在风控上；
    # 但这个参数绝不能用来绕过"从更早开始算"——那等于把见过的回撤藏起来。
    since = max(since, int(since_ts or 0))
    if not path or not os.path.exists(path):
        return 0.0
    sql = "SELECT MAX(equity) FROM equity WHERE ts >= ?"
    args = [since]
    if paper is not None and equity_has_paper(path):
        sql += " AND COALESCE(paper, 1) = ?"
        args.append(int(paper))
    c = sqlite3.connect(path)
    try:
        r = c.execute(sql, tuple(args)).fetchone()
    finally:
        c.close()
    return float(r[0]) if r and r[0] is not None else 0.0


TRADE_SCHEMA = """
-- 每次评估一行（包括不开仓），完整现场
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    bar_time      INTEGER NOT NULL,     -- 触发决策的那根 3s 桶起点
    feature_time  INTEGER NOT NULL,     -- 特征可见的最晚时间（必须 <= ts）
    symbol        TEXT NOT NULL,
    price         REAL NOT NULL,
    signal        TEXT,                 -- 触发的信号名（无则 NULL）
    action        TEXT NOT NULL,        -- open_long/open_short/close/hold/skip
    reason        TEXT NOT NULL,        -- 一句话人话结论
    gate_json     TEXT,                 -- 每道门的结果
    feature_json  TEXT,                 -- 决策时可见的全部特征
    risk_json     TEXT,                 -- 风险预算/仓位/杠杆/强平距离
    equity        REAL
);
CREATE INDEX IF NOT EXISTS idx_dec_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             INTEGER NOT NULL,
    client_id      TEXT NOT NULL UNIQUE,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    type           TEXT NOT NULL,
    qty            REAL NOT NULL,
    price          REAL,
    stop_price     REAL,
    reduce_only    INTEGER DEFAULT 0,
    status         TEXT NOT NULL,        -- new/filled/canceled/rejected/unknown
    exchange_id    TEXT,
    reason         TEXT,
    decision_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ord_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS fills (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER,
    ts         INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    price      REAL NOT NULL,
    qty        REAL NOT NULL,
    fee        REAL DEFAULT 0,
    fee_asset  TEXT,
    paper      INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_fill_ts ON fills(ts);

-- 一笔完整持仓（开->平），PnL 的唯一真相
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,          -- long/short
    qty          REAL NOT NULL,
    entry_ts     INTEGER NOT NULL,
    entry_price  REAL NOT NULL,
    exit_ts      INTEGER,
    exit_price   REAL,
    stop_price   REAL,
    target_price REAL,
    pnl_gross    REAL,
    fee          REAL DEFAULT 0,
    pnl_net      REAL,
    mfe          REAL,                   -- 最大有利偏移（价格）
    mae          REAL,                   -- 最大不利偏移
    open_reason  TEXT,
    close_reason TEXT,
    decision_id  INTEGER,
    max_hold_bars INTEGER,              -- 开仓时按波动反推的持仓上限，重启必须原样恢复
    paper        INTEGER DEFAULT 1,
    -- 哪套策略开的仓。没有这一列，scalp 时代的 4 笔旧仓和 trend 时代的新仓
    -- 在 paper=1 上无法区分，统计"趋势策略的真实边际"时会把它们算进来。
    -- 空串 = 未知（迁移前就存在的老数据），不猜，宁可留白。
    strategy     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trade_entry ON trades(entry_ts);

CREATE TABLE IF NOT EXISTS equity (
    ts           INTEGER PRIMARY KEY,
    balance      REAL NOT NULL,
    unrealized   REAL DEFAULT 0,
    equity       REAL NOT NULL,
    position_qty REAL DEFAULT 0,
    leverage     REAL,
    paper        INTEGER DEFAULT 1
);

-- 执行器自身状态的真相（纸面）：余额、持仓、均价。
-- 不从 trades 反推，因为反推拿不回已付手续费；实盘则由交易所对账覆盖。
CREATE TABLE IF NOT EXISTS executor_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    ts           INTEGER NOT NULL,
    balance      REAL NOT NULL,
    pos_qty      REAL DEFAULT 0,
    pos_entry    REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS risk_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT
);

-- 硬停状态（第 123 轮）。
--
-- 为什么必须落库：halted_hard 原来只活在内存里，进程一重启就是 False。
-- 而重启不是理论风险——部署、服务器重启、OOM 都会重启，
-- 我自己每轮都在部署。于是"停下等人查账"这条闸的【实际有效期】
-- 只到下一次重启为止。
--
-- guard.py 里对【峰值】早就写过一模一样的教训（见 _restore_peak 上方注释），
-- 同一个道理对 halted_hard 一个字都没写、也一个字都没做。
-- 熔断出局标记（第 126 轮）。单行。
--
-- engine._trend_block_bar 原本是【内存字段】，进程一重启就归零。
-- 于是「本根 4h 已熔断出局，不许再进场」这条闸的实际有效期
-- 只到下一次重启为止——而重启最可能发生在行情剧烈波动时，
-- 正是这个闸刚被设上的时刻。
-- 后果是「止损 -> 重启 -> 启动对账立刻补回 -> 再止损」，
-- 每次 25%，两轮就把 100U 打成 56U。
CREATE TABLE IF NOT EXISTS engine_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    ts         INTEGER NOT NULL,
    block_bar  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guard_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    ts                INTEGER NOT NULL,
    halted_hard       INTEGER NOT NULL DEFAULT 0,
    halt_reason       TEXT DEFAULT '',
    halt_kind         TEXT DEFAULT '',
    halt_ts           INTEGER DEFAULT 0,
    -- 下面四列是第 127 轮补的。
    --
    -- 它们全都是「重启一次就静默解除」的同一族缺陷：
    --   day / day_start_equity 为空时，第一次 update() 会把当日基准
    --   设成【当时权益】，于是"今天已经亏了 55%"重启后变成"今天亏 0%"，
    --   日亏损闸被重启解开 —— 重启等于给这条闸按了一次人工复位。
    --   close_fail_streak 归零则让"止损失效不许开新仓"这条闸
    --   在每次部署后重新给系统一次带病开仓的机会。
    day               TEXT DEFAULT '',
    day_start_equity  REAL DEFAULT 0,
    daily_halt_day    TEXT DEFAULT '',
    close_fail_streak INTEGER DEFAULT 0
);
"""


class TradeDB:
    def __init__(self, path: str):
        self._path = path
        self._q: "queue.Queue" = queue.Queue(maxsize=100000)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._last_equity_s = 0

    def start(self) -> None:
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(TRADE_SCHEMA)
        self._migrate()
        self._conn.commit()
        self._thread = threading.Thread(target=self._writer, name="trade-writer", daemon=True)
        self._thread.start()

    # 已有的库是 CREATE TABLE IF NOT EXISTS 建的，加列不会自动生效，
    # 必须显式 ALTER。迁移写在启动时、只补缺的列，可重复执行。
    #
    # 这里曾经只有 strategy 一列，漏掉了 open_reason / paper / max_hold_bars
    # 这些【查询里真的会读】的列——而 open_trade() 的 SELECT 里带着它们，
    # 于是碰到一个早年建的库就在启动路径上直接抛 no such column。
    # 这不是理论风险：本地 data/market_trade.db 就是这样一个库，
    # 实测 TradeDB.start() 之后第一次 open_trade() 就崩。
    # 而 open_trade() 被 restore_from_db（纸面恢复）和启动对账用到，
    # 崩在这里等于整个引擎起不来——7x24 运行最怕这种"升级即宕机"。
    #
    # 加列一律给默认值，保证老数据可读：
    #   paper 默认 1，因为这一列出现之前所有记录都来自纸面模式。
    _MIGRATIONS = (
        ("trades", "strategy", "ALTER TABLE trades ADD COLUMN strategy TEXT DEFAULT ''"),
        ("trades", "open_reason", "ALTER TABLE trades ADD COLUMN open_reason TEXT"),
        ("trades", "close_reason", "ALTER TABLE trades ADD COLUMN close_reason TEXT"),
        ("trades", "decision_id", "ALTER TABLE trades ADD COLUMN decision_id INTEGER"),
        ("trades", "max_hold_bars", "ALTER TABLE trades ADD COLUMN max_hold_bars INTEGER"),
        ("trades", "mfe", "ALTER TABLE trades ADD COLUMN mfe REAL"),
        ("trades", "mae", "ALTER TABLE trades ADD COLUMN mae REAL"),
        ("trades", "paper", "ALTER TABLE trades ADD COLUMN paper INTEGER DEFAULT 1"),
        ("equity", "paper", "ALTER TABLE equity ADD COLUMN paper INTEGER DEFAULT 1"),
        # 第 127 轮。guard_state 是第 123 轮才建的表，线上那个库现在是【老表】，
        # CREATE TABLE IF NOT EXISTS 对它完全无效——加的四列一列都不会出现。
        # 实测踩到：改完部署到副本上跑，guard_state 的列还是原来那 6 个，
        # load_guard_state 读到的全是默认值，闸照样被重启解开。
        # 这正是 269-279 行这段注释写着的坑，我自己又踩了一次。
        ("guard_state", "day", "ALTER TABLE guard_state ADD COLUMN day TEXT DEFAULT ''"),
        ("guard_state", "day_start_equity",
         "ALTER TABLE guard_state ADD COLUMN day_start_equity REAL DEFAULT 0"),
        ("guard_state", "daily_halt_day",
         "ALTER TABLE guard_state ADD COLUMN daily_halt_day TEXT DEFAULT ''"),
        ("guard_state", "close_fail_streak",
         "ALTER TABLE guard_state ADD COLUMN close_fail_streak INTEGER DEFAULT 0"),
        # 实盘峰值基线（第 128 轮）。
        #
        # 起因是一条真实数据：2026-11-03 跑过约 14 分钟 live，账户 125.008778U，
        # 一笔没做，之后钱被提走。切回 live 时 _restore_peak 按 paper=0 取到
        # 这个 125.0088 当基准，而当时权益是 0.007U —— 回撤算出来 -99.99%，
        # 立刻触发 60% 硬停，且需要人工 reset。就算注资回 100U，也凭空
        # 带着 -20% 的回撤，60% 的预算只剩 40 点。
        #
        # 那 125.0088 本身没错，错在它属于【另一笔已经撤走的资金】。
        # 所以不去删历史行，而是记一个基线时刻：峰值只从此刻往后算，
        # 旧数据原样保留，可追溯。
        ("guard_state", "baseline_ts",
         "ALTER TABLE guard_state ADD COLUMN baseline_ts INTEGER DEFAULT 0"),
    )

    def _migrate(self) -> None:
        for table, column, ddl in self._MIGRATIONS:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(%s)" % table)}
            if column not in cols:
                self._conn.execute(ddl)
                logger.warning("数据库迁移：%s 表新增 %s 列", table, column)

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

    # ---------- 写 ----------
    def log_decision(self, d: dict) -> None:
        self._put(("decision", d))

    def log_order(self, o: dict) -> None:
        self._put(("order", o))

    def log_fill(self, f: dict) -> None:
        self._put(("fill", f))

    def log_trade(self, t: dict) -> None:
        self._put(("trade", t))

    def close_trade(self, t: dict) -> None:
        """平仓：更新已存在的开仓行，而不是再插一行。

        这是 2026-11-01 修掉的一个真 bug：原来 _close() 又调 log_trade()，
        trades 表里同一笔交易留下"永远 exit_ts IS NULL"的旧行，
        重启后 engine.attach() 用 open_trade() 恢复出一笔幽灵持仓。
        """
        self._put(("trade_close", t))

    EQUITY_MIN_GAP_S = 5

    def log_equity(self, e: dict) -> None:
        """把权益快照入库，但**至少间隔 5 秒**：3s 循环会把权益表写爆。

        ★ 这里曾经写的是「同秒去重」，而代码实际是 5 秒节流。
        文档和代码不一致的代价，我自己就付过一次：
        排查"引擎循环是不是变慢了"时，我拿 equity 表的相邻 ts 间隔当循环周期，
        量出稳定 6.0 秒，差点击穿"3 秒实时系统实际跑 6 秒"这个结论——
        而它测到的只是这个节流（5 秒在 3 秒粒度上取整就是 6 秒）。
        真实循环是正常的：3s 桶 30 分钟 597/600 根严格连续。
        教训：**被节流过的表不能用来推断上游节奏**。
        """
        if not hasattr(self, "_last_equity_s"):
            self._last_equity_s = 0
        now_s = int(time.time())
        if now_s - self._last_equity_s < self.EQUITY_MIN_GAP_S:
            return
        self._last_equity_s = now_s
        self._put(("equity", e))

    def save_executor(self, st: dict) -> None:
        self._put(("executor", st))

    def save_block_bar(self, bar_t: int) -> None:
        """记住「本根 4h 已熔断出局」（第 126 轮）。走单写队列，不直连。"""
        self._put(("engine_state", {"block_bar": int(bar_t or 0)}))

    def load_block_bar(self) -> int:
        """读回熔断出局标记；没有记录 / 表不存在都返回 0（= 没被拦住）。"""
        c = self._ro()
        try:
            r = c.execute("SELECT block_bar FROM engine_state WHERE id=1").fetchone()
        except Exception:
            r = None
        finally:
            c.close()
        return int(r[0] or 0) if r else 0

    def save_guard_state(self, st: dict) -> None:
        """硬停状态落库（第 123 轮）。走单写队列，不直连。"""
        self._put(("guard_state", st))

    def load_guard_state(self) -> Optional[dict]:
        """读回硬停 + 当日基准 + 平仓失败计数。没有记录 / 表不存在都返回 None。

        ★ 用 SELECT * 再按列名取，不要按位置写死。
          第 127 轮加列时，如果按位置读，老库（没有新列）会直接索引错位，
          而那个错误长得像"数据本来就是 0"，不会报错——
          正是这一族缺陷最擅长的伪装方式。
        """
        c = self._ro()
        try:
            cur = c.execute("SELECT * FROM guard_state WHERE id=1")
            row = cur.fetchone()
            names = [d[0] for d in (cur.description or [])]
        except Exception:
            row, names = None, []
        finally:
            c.close()
        if not row:
            return None
        d = dict(zip(names, row))
        return {
            "ts": int(d.get("ts") or 0),
            "halted_hard": bool(d.get("halted_hard")),
            "halt_reason": d.get("halt_reason") or "",
            "halt_kind": d.get("halt_kind") or "",
            "halt_ts": int(d.get("halt_ts") or 0),
            "day": d.get("day") or "",
            "day_start_equity": float(d.get("day_start_equity") or 0.0),
            "daily_halt_day": d.get("daily_halt_day") or "",
            "close_fail_streak": int(d.get("close_fail_streak") or 0),
            "baseline_ts": int(d.get("baseline_ts") or 0),
        }

    def realized_pnl(self, paper: Optional[int] = None) -> float:
        """已平仓的净盈亏合计（含手续费）。用于没有 executor_state 时重建余额。

        ★ paper 参数必须给。以前无差别求和，于是纸面余额会把【实盘】的
        盈亏也算进来：实测 100U 起始 + 纸面赚 10 + 实盘亏 50 -> 纸面余额 60，
        而正确值是 110。纸面账户和真钱账户是两本账，不能串。

        paper=None 保持旧的全量求和语义（只给诊断脚本用）。
        """
        c = self._ro()
        try:
            if paper is None:
                r = c.execute("SELECT COALESCE(SUM(pnl_net),0) FROM trades"
                              " WHERE exit_ts IS NOT NULL").fetchone()
            else:
                r = c.execute("SELECT COALESCE(SUM(pnl_net),0) FROM trades"
                              " WHERE exit_ts IS NOT NULL AND paper=?",
                              (int(paper),)).fetchone()
        finally:
            c.close()
        return float(r[0] or 0.0)

    def load_executor(self) -> Optional[dict]:
        c = self._ro()
        try:
            r = c.execute("SELECT ts,balance,pos_qty,pos_entry FROM executor_state"
                          " WHERE id=1").fetchone()
        except Exception:
            r = None
        finally:
            c.close()
        if not r:
            return None
        return {"ts": r[0], "balance": r[1], "pos_qty": r[2], "pos_entry": r[3]}

    def log_risk(self, kind: str, detail: str) -> None:
        self._put(("risk", {"ts": int(time.time() * 1000), "kind": kind, "detail": detail}))

    def _put(self, item) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            logger.error("trade_db 队列满，丢弃 %s", item[0])

    def _writer(self) -> None:
        batch: List[tuple] = []
        last = time.time()
        while not self._stop.is_set() or not self._q.empty():
            try:
                batch.append(self._q.get(timeout=0.2))
            except queue.Empty:
                pass
            if not batch:
                continue
            if len(batch) < 50 and time.time() - last < 0.4:
                continue
            self._flush(batch)
            batch = []
            last = time.time()
        if batch:
            self._flush(batch)

    def _flush(self, batch: List[tuple]) -> None:
        c = self._conn
        if not c:
            return
        try:
            for kind, d in batch:
                if kind == "decision":
                    c.execute(
                        "INSERT INTO decisions(ts,bar_time,feature_time,symbol,price,signal,action,"
                        "reason,gate_json,feature_json,risk_json,equity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (d["ts"], d["bar_time"], d["feature_time"], d["symbol"], d["price"],
                         d.get("signal"), d["action"], d["reason"],
                         json.dumps(d.get("gates"), ensure_ascii=False),
                         json.dumps(d.get("features"), ensure_ascii=False),
                         json.dumps(d.get("risk"), ensure_ascii=False), d.get("equity")))
                elif kind == "order":
                    c.execute(
                        "INSERT OR REPLACE INTO orders(ts,client_id,symbol,side,type,qty,price,"
                        "stop_price,reduce_only,status,exchange_id,reason,decision_id)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (d["ts"], d["client_id"], d["symbol"], d["side"], d["type"], d["qty"],
                         d.get("price"), d.get("stop_price"), int(d.get("reduce_only", 0)),
                         d["status"], d.get("exchange_id"), d.get("reason"),
                         d.get("decision_id")))
                elif kind == "fill":
                    c.execute(
                        "INSERT INTO fills(order_id,ts,symbol,side,price,qty,fee,fee_asset,paper)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (d.get("order_id"), d["ts"], d["symbol"], d["side"], d["price"],
                         d["qty"], d.get("fee", 0), d.get("fee_asset"), int(d.get("paper", 1))))
                elif kind == "trade":
                    c.execute(
                        "INSERT INTO trades(symbol,side,qty,entry_ts,entry_price,exit_ts,exit_price,"
                        "stop_price,target_price,pnl_gross,fee,pnl_net,mfe,mae,open_reason,"
                        "close_reason,decision_id,max_hold_bars,paper,strategy)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (d["symbol"], d["side"], d["qty"], d["entry_ts"], d["entry_price"],
                         d.get("exit_ts"), d.get("exit_price"), d.get("stop_price"),
                         d.get("target_price"), d.get("pnl_gross"), d.get("fee", 0),
                         d.get("pnl_net"), d.get("mfe"), d.get("mae"),
                         d.get("open_reason"), d.get("close_reason"),
                         d.get("decision_id"), d.get("max_hold_bars"),
                         int(d.get("paper", 1)), d.get("strategy", "")))
                elif kind == "trade_close":
                    # 按 entry_ts + side 精确定位那一笔未平仓，更新而不是插入。
                    #
                    # ★ qty 必须一起更新。这一列原来漏在 UPDATE 之外，于是
                    #   开仓时的【持仓量】永远留在表里，而不是【实际卖出量】。
                    #   现货卖出按 step 向下取整，两者必然不同（例：
                    #   持仓 0.00331668，实卖 0.00331，剩 0.00000668 成为卖不掉的粉尘）。
                    #   后果是这张表内部自相矛盾：
                    #     pnl_gross 用实卖量算（-9.93）
                    #     qty       却是持仓量（0.00331668）
                    #   任何人用 (exit-entry)*qty 重算都会得到 -9.95004，与库里对不上。
                    #   引擎端（engine.py 的 _close）本来就传了 qty=sold_qty，
                    #   是写入端把它丢了——参数白传。
                    c.execute(
                        "UPDATE trades SET exit_ts=?, exit_price=?, qty=?, pnl_gross=?, fee=?,"
                        " pnl_net=?, mfe=?, mae=?, close_reason=?, stop_price=?, target_price=?"
                        " WHERE id=(SELECT id FROM trades WHERE entry_ts=? AND side=?"
                        "            AND exit_ts IS NULL ORDER BY id DESC LIMIT 1)",
                        (d.get("exit_ts"), d.get("exit_price"), d.get("qty"),
                         d.get("pnl_gross"),
                         d.get("fee", 0), d.get("pnl_net"), d.get("mfe"), d.get("mae"),
                         d.get("close_reason"), d.get("stop_price"), d.get("target_price"),
                         d["entry_ts"], d["side"]))
                elif kind == "equity":
                    c.execute(
                        "INSERT OR REPLACE INTO equity(ts,balance,unrealized,equity,position_qty,"
                        "leverage,paper) VALUES(?,?,?,?,?,?,?)",
                        (d["ts"], d["balance"], d.get("unrealized", 0), d["equity"],
                         d.get("position_qty", 0), d.get("leverage"), int(d.get("paper", 1))))
                elif kind == "executor":
                    c.execute(
                        "INSERT OR REPLACE INTO executor_state(id,ts,balance,pos_qty,pos_entry)"
                        " VALUES(1,?,?,?,?)",
                        (d["ts"], d["balance"], d.get("pos_qty", 0), d.get("pos_entry", 0)))
                elif kind == "risk":
                    c.execute("INSERT INTO risk_events(ts,kind,detail) VALUES(?,?,?)",
                              (d["ts"], d["kind"], d["detail"]))
                elif kind == "engine_state":
                    c.execute(
                        "INSERT OR REPLACE INTO engine_state(id,ts,block_bar)"
                        " VALUES(1,?,?)",
                        (int(time.time() * 1000), int(d.get("block_bar") or 0)))
                elif kind == "guard_state":
                    c.execute(
                        "INSERT OR REPLACE INTO guard_state"
                        "(id,ts,halted_hard,halt_reason,halt_kind,halt_ts,"
                        " day,day_start_equity,daily_halt_day,close_fail_streak,"
                        " baseline_ts)"
                        " VALUES(1,?,?,?,?,?,?,?,?,?,?)",
                        (int(time.time() * 1000),
                         1 if d.get("halted_hard") else 0,
                         d.get("halt_reason") or "",
                         d.get("halt_kind") or "",
                         int(d.get("halt_ts") or 0),
                         d.get("day") or "",
                         float(d.get("day_start_equity") or 0.0),
                         d.get("daily_halt_day") or "",
                         int(d.get("close_fail_streak") or 0),
                         int(d.get("baseline_ts") or 0)))
            c.commit()
        except Exception:
            logger.error("trade_db flush 失败", exc_info=True)

    # ---------- 读（web 用独立连接） ----------
    def _ro(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, check_same_thread=False)

    def recent_decisions(self, limit: int = 100) -> List[dict]:
        c = self._ro()
        try:
            rows = c.execute(
                "SELECT id,ts,bar_time,symbol,price,signal,action,reason,gate_json,risk_json,equity"
                " FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            c.close()
        out = []
        for r in rows:
            out.append({"id": r[0], "ts": r[1], "bar_time": r[2], "symbol": r[3], "price": r[4],
                        "signal": r[5], "action": r[6], "reason": r[7],
                        "gates": _j(r[8]), "risk": _j(r[9]), "equity": r[10]})
        return out

    def open_trade(self) -> Optional[dict]:
        c = self._ro()
        try:
            # fee 必须查出来：开仓时写入的是【开仓手续费】，
            # 平仓时 _close 要用它算 fee_total = 开仓费 + 平仓费。
            # 不带这一列，重启恢复的持仓平仓时开仓费会被算成 0，
            # 记录的盈亏少减一笔手续费——方向是让亏损看起来更小。
            r = c.execute("SELECT id,symbol,side,qty,entry_ts,entry_price,stop_price,target_price,"
                          "open_reason,paper,COALESCE(max_hold_bars,0),"
                          "COALESCE(fee,0) FROM trades"
                          " WHERE exit_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            c.close()
        if not r:
            return None
        return {"id": r[0], "symbol": r[1], "side": r[2], "qty": r[3], "entry_ts": r[4],
                "entry_price": r[5], "stop_price": r[6], "target_price": r[7],
                "open_reason": r[8], "paper": r[9], "max_hold_bars": r[10],
                "fee": float(r[11] or 0.0)}

    def closed_trades(self, limit: int = 200) -> List[dict]:
        c = self._ro()
        try:
            rows = c.execute("SELECT id,symbol,side,qty,entry_ts,entry_price,exit_ts,exit_price,"
                             "pnl_gross,fee,pnl_net,close_reason,paper FROM trades"
                             " WHERE exit_ts IS NOT NULL ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
        finally:
            c.close()
        return [{"id": r[0], "symbol": r[1], "side": r[2], "qty": r[3], "entry_ts": r[4],
                 "entry_price": r[5], "exit_ts": r[6], "exit_price": r[7], "pnl_gross": r[8],
                 "fee": r[9], "pnl_net": r[10], "close_reason": r[11], "paper": r[12]}
                for r in rows]

    def last_equity(self) -> Optional[dict]:
        c = self._ro()
        try:
            r = c.execute("SELECT ts,balance,unrealized,equity,position_qty FROM equity"
                          " ORDER BY ts DESC LIMIT 1").fetchone()
        finally:
            c.close()
        if not r:
            return None
        return {"ts": r[0], "balance": r[1], "unrealized": r[2], "equity": r[3],
                "position_qty": r[4]}

    def strategy_start_ts(self, strategy: str) -> int:
        """当前策略第一次发出决策的时间；查不到返回 0（= 全历史）。"""
        return strategy_start_ts_at(self._path, strategy)

    def peak_equity(self, strategy: Optional[str] = None,
                    paper: Optional[int] = None,
                    since_ts: int = 0) -> float:
        """当前策略时代内的最高权益；查不到返回 0.0。paper/since 见模块级函数。"""
        return peak_equity_at(self._path, strategy, paper, since_ts)

    def equity_curve(self, limit: int = 2000) -> List[dict]:
        c = self._ro()
        try:
            rows = c.execute("SELECT ts,equity FROM equity ORDER BY ts DESC LIMIT ?",
                             (limit,)).fetchall()
        finally:
            c.close()
        rows.reverse()
        per = 1e18 if not rows else 1.0
        return [{"ts": r[0], "equity": r[1]} for r in rows]

    def stats(self, strategy: Optional[str] = None) -> dict:
        """交易统计。**必须能按策略分开**，否则会把不同策略的战绩混成一个数。

        2026-11-01 的实际问题：大屏头条显示
            【4 笔 / 胜率 25% / 净利 -20.46U】
        而这 4 笔全部来自【已经被否掉的剥头皮策略】，
        真正在跑的趋势策略是 0 笔。

        旧实现统计所有已平仓交易，于是：
          - 用户看大屏会以为这套系统在亏钱
          - 我拿"历史统计"印证趋势策略的边际时，
            其实在看另一个策略的战绩

        现在：传 strategy 就按策略过滤；不传仍是全部（兼容旧调用）；
        同时永远附带 by_strategy 分解，让"整体"和"分策略"都能看见，
        而不是用汇总数掩盖组成。
        """
        c = self._ro()
        try:
            where = " WHERE exit_ts IS NOT NULL"
            args: tuple = ()
            if strategy:
                where += " AND strategy=?"
                args = (strategy,)
            n, wins, pnl, fee = c.execute(
                "SELECT COUNT(*), SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END),"
                " SUM(pnl_net), SUM(fee) FROM trades" + where, args).fetchone()
            dec = c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            # 按策略分解：任何汇总数都要能被拆开检查
            by: dict = {}
            for s, p, cnt, w, pn in c.execute(
                    "SELECT COALESCE(strategy,'') AS st, paper, COUNT(*),"
                    " SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END), SUM(pnl_net)"
                    " FROM trades WHERE exit_ts IS NOT NULL"
                    " GROUP BY COALESCE(strategy,''), paper", ()).fetchall():
                key = s or "(未标注)"
                d = by.setdefault(key, {"trades": 0, "wins": 0, "pnl_net": 0.0})
                d["trades"] += cnt or 0
                d["wins"] += w or 0
                d["pnl_net"] = round(d["pnl_net"] + (pn or 0.0), 4)
        finally:
            c.close()
        n = n or 0
        out = {"trades": n, "wins": wins or 0,
               "win_rate": round((wins or 0) / n, 4) if n else None,
               "pnl_net": round(pnl or 0.0, 4), "fee": round(fee or 0.0, 4),
               "decisions": dec, "by_strategy": by}
        if strategy:
            out["strategy"] = strategy
        return out


def _j(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None