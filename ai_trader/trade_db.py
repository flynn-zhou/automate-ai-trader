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

#: 「这笔仓不是策略开的」在 trades.open_reason 里的唯一标记。
#:
#: 定义放在这里，是因为它有【两个】消费方，而它们分处不同的层：
#:   - engine.reconcile_live() 接管孤儿仓时把这个字面量写进 open_reason
#:   - stats() 用它把这类仓排除出「策略战绩」
#: 两边各写一份字符串，某天改了一处就会静默对不上——而表现出来的样子是
#: 「统计里又开始混进别人的手工单」，不会报任何错。
ADOPT_REASON = u"实盘对账接管"

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

-- 真实持仓回合（第 130 轮）。一张【只认交易所】的账。
--
-- 为什么不能靠上面那张 trades 表
-- ----------------------------
-- trades 只记「引擎经手过的」仓。生产实证（11-11 09:04 → 11-12 22:57）：
--     交易所 /fapi/v1/userTrades：25 笔成交，
--         已实现盈亏合计 +109.80，手续费 -16.92，资金费 -0.30  →  净 ≈ +92.59
--     trades 表同一区间：只有 4 条记录                          →  净 -20.46
-- 差 113U。差在两头：
--   - 用户手工来回买卖的绝大多数根本没进库（引擎只在"交易所与库不一致"时
--     才写一条接管记录，而那时已经错过了开仓）
--   - 手续费曾经全记 0（第 129 轮修的是 live.py 的 newOrderRespType）
-- 所以这张表的数据来源只能是交易所，不读 trades、也不和它合并——
-- 两套账各自独立，对不上才看得见差异。对不上就是信息，合并掉就没了。
--
-- 为什么要有 round_key
-- -------------------
-- 每个对账周期（300s）都会重新抓一遍成交。没有自然键的话，
-- 同一个回合每个周期都会多插一行，一天就是 288 条重复。
-- round_key = symbol|entry_ts|side|qty，由 live_ledger 生成，UNIQUE 兜底。
CREATE TABLE IF NOT EXISTS live_rounds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    round_key     TEXT NOT NULL UNIQUE,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,          -- long/short
    qty           REAL NOT NULL,          -- 开仓总量（应等于平仓总量）
    entry_ts      INTEGER NOT NULL,       -- 首笔开仓成交的交易所 time
    entry_price   REAL NOT NULL,          -- 开仓 VWAP（按成交量加权，不是首笔价）
    exit_ts       INTEGER,                -- 最后一笔平仓成交时间；NULL = 还开着
    exit_price    REAL,
    -- 毛盈亏直接取交易所 realizedPnl 之和。不要自己用 (exit-entry)*qty 再算一遍：
    -- 币安在部分平仓时按【自己的】持仓均价结算，和我们的 VWAP 会有微小偏差，
    -- 而差额会全部落到"其他"里去，最后没人对得上账。
    gross         REAL DEFAULT 0,
    fee           REAL DEFAULT 0,         -- commission 之和，正数（支出）
    funding       REAL DEFAULT 0,         -- 资金费之和，负数表示付出
    net           REAL DEFAULT 0,         -- gross - fee + funding
    hold_sec      INTEGER DEFAULT 0,
    n_fills       INTEGER DEFAULT 0,
    notional      REAL DEFAULT 0,         -- qty * entry_price：成本率与打分的分母
    -- 这笔仓是谁开的。strategy = 引擎开的；adopted = 对账接管的手工仓；
    -- manual = 纯手工（引擎从没经手）。判断依据是 trades 表的 open_reason，
    -- 而不是"猜"——猜出来的来源没法追责。
    source        TEXT DEFAULT 'manual',
    trade_id      INTEGER,                -- 关联 trades.id（若引擎也记过这一笔）
    open_reason   TEXT DEFAULT '',
    -- 持仓期间的最大有利/不利偏移，口径与 trades 表一致：
    -- 价格偏移、相对入场价、mfe 为正、mae 为负。
    mfe           REAL,
    mae           REAL,
    score_pnl     INTEGER,
    score_entry   INTEGER,
    score_exit    INTEGER,
    score_cost    INTEGER,
    score_total   INTEGER,
    grade         TEXT DEFAULT '',        -- A/B/C/D
    analysis      TEXT DEFAULT '',        -- 逐项数字与判定：给解释，不只给分
    analyzed_ts   INTEGER DEFAULT 0,      -- 0 = 还没算过 MFE/MAE 和分数
    synced_ts     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_round_exit ON live_rounds(exit_ts DESC);
CREATE INDEX IF NOT EXISTS idx_live_round_entry ON live_rounds(entry_ts DESC);

-- 挂在交易所里的单（第 131 轮）。「提前挂单」机制的本地账。
--
-- 为什么不能只存在内存里
-- --------------------
-- 挂单的生命周期是**跨进程**的：引擎重启、部署、崩溃，单子都还在交易所挂着。
-- 只存内存的话，重启后进程不知道自己挂过什么 —— 于是
--   * 止损单还挂着，但引擎以为没有，可能再挂一张 → 同一笔仓两个止损；
--   * 开仓单成交了，但引擎不知道 → 账面上没仓位，交易所里有。
-- 而且这两种都**不会报错**，只是账慢慢对不上。
--
-- client_order_id 为什么是主键
-- -------------------------
-- orderId 是交易所分配的，只有下单成功之后才知道。挂单意图必须在**发请求之前**
-- 就落库（否则请求超时的那一刻就丢了），那时本地只有 client_order_id。
-- 所以用本地生成的 id 做自然键，交易所 id 只是一个可空列。
-- 格式约定：`{strategy}-{purpose}-{ts}-{rand}`，前缀让对账能按用途分组。
CREATE TABLE IF NOT EXISTS working_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id   TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT DEFAULT '',
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,          -- BUY / SELL
    otype             TEXT NOT NULL,          -- LIMIT / STOP_MARKET / ...
    qty               REAL NOT NULL,
    executed_qty      REAL DEFAULT 0,
    price             REAL DEFAULT 0,         -- 限价；止损单为 0
    stop_price        REAL DEFAULT 0,         -- 触发价
    avg_price         REAL DEFAULT 0,
    reduce_only       INTEGER DEFAULT 0,
    -- 交易所原生状态 + 两个本地态：
    --   LOST    = 本地以为挂着，交易所查不到（可能成交了、也可能被手工撤了）
    --   STALE   = 已终结但还没归档
    status            TEXT NOT NULL,
    purpose           TEXT DEFAULT '',        -- entry / stop / target
    strategy          TEXT DEFAULT '',
    round_key         TEXT DEFAULT '',        -- 关联 live_rounds.round_key
    created_ts        INTEGER NOT NULL,
    updated_ts        INTEGER NOT NULL,
    synced_ts         INTEGER DEFAULT 0,      -- 0 = 还没和交易所对过账
    note              TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wo_status ON working_orders(status, updated_ts DESC);
CREATE INDEX IF NOT EXISTS idx_wo_round ON working_orders(round_key);
"""


# 一张挂单可能以三种形状出现，必须都能归一化成 dict：
#   * `PendingOrder`（执行器接口，P1 定的返回类型，是 dataclass，没有 .get）
#   * 币安原始 JSON（clientOrderId / origQty / stopPrice 这套键）
#   * 本地 working_orders 行（本来就是 dict）
_ORDER_FIELDS = (
    "client_order_id", "client_id", "clientOrderId",
    "order_id", "orderId",
    "symbol", "side", "otype", "type",
    "qty", "origQty", "executed_qty", "executedQty",
    "price", "stop_price", "stopPrice", "avg_price", "avgPrice",
    "status", "reduce_only", "reduceOnly", "update_time",
)


def order_row(o) -> dict:
    """把「一张挂单」统一成 dict。纯函数，不碰 DB/网络。

    ★ 为什么必须有这一层（第 131 轮的缺陷根源）：
    P2 的 `reconcile_orders()` 与 `_from_remote()` 文档都写着「两种入参都吃，
    `PendingOrder` 有 `client_id`、币安原始 JSON 有 `clientOrderId`」，
    但实现里只有 `o.get(...)`。而 `LiveExecutor.open_orders()` 返回的是
    `PendingOrder` 数据类（dataclass）—— 它没有 `.get`。
    后果不是「少字段」，是**每张单在挂单对账第一步就抛 AttributeError**，
    被引擎的宽 except 吞成一行日志：表永远填不上、交易所有本地没有的单永远补不回来。

    反证一次：这会不会是「调用方该自己转 dict」？不是。
    `reconcile_orders` 的入参签名写的是 `List[dict]`，而交易所接口的返回类型
    由 P1 定义为 `PendingOrder`，两者之间**没有别的转换点** —— 不在这里归一化，
    就只能在引擎里再写一份（P4 会用同一条路，必然漏）。
    """
    if isinstance(o, dict):
        return dict(o)
    out = {}
    for k in _ORDER_FIELDS:
        v = getattr(o, k, None)
        if v is not None:
            out[k] = v
    return out


def _order_cid(o: dict) -> str:
    """配对键。三种形状的键名都认：client_id / client_order_id / clientOrderId。

    只认其中一两种的后果不是"少配对一张"，是那批单全部掉进 vanished
    → 被标成 LOST。所以键名容错在这里是必需的，不是好看。
    """
    return (o.get("client_id") or o.get("client_order_id")
            or o.get("clientOrderId") or "")


def _exec_qty(o: dict) -> float:
    """已成交量。币安原始 JSON 叫 executedQty，本地行叫 executed_qty。

    这个值参与"变没变"的判断：键名认错就会把一张已经部分成交的单
    判成"没变"，于是本地账上的 executed_qty 永远停在 0。
    """
    return float(o.get("executed_qty") or o.get("executedQty") or 0)


def reconcile_orders(local: List[dict], remote: List[dict]) -> dict:
    """把「本地以为挂着的单」和「交易所真的挂着的单」对起来。纯函数，不碰 DB/网络。

    返回四组：

    * `adopted`   — 交易所有、本地没有。**必须补进本地**，否则引擎不知道
      自己还挂着一张止损单，重启后可能再挂一张 → 同一笔仓两个止损，
      先触发的那张平掉大部分仓，后触发的那张把剩仓反向开出去。
    * `vanished`  — 本地认为挂着、交易所查不到。**不能直接判「没有」**：
      它可能已经成交了（那就是一笔真实成交，必须记账），也可能被手工撤了。
      所以交给调用方去 `query_order()` 查真实终态，再决定记成交还是标记失效。
      这里只负责把它们挑出来 —— 静默当成「不存在」是最坏的处理方式。
    * `changed`   — 两边都有但状态/成交量变了（成交了、部分成交了、被撤了）。
    * `same`      — 两边一致。

    配对键是 `client_order_id`，不是 `exchange_order_id`：
    挂单意图在**发请求之前**就要落库，那一刻本地还没有交易所 id。
    用交易所 id 配对会让「下单请求超时但单子其实建成了」这一类
    永远对不上。
    """
    loc = {}
    for o in local or ():
        o = order_row(o)
        cid = _order_cid(o)
        if cid:
            loc[cid] = o
    rem = {}
    for o in remote or ():
        o = order_row(o)
        cid = _order_cid(o)
        if cid:
            rem[cid] = o

    out = {"adopted": [], "vanished": [], "changed": [], "same": []}
    for cid, r in rem.items():
        l = loc.get(cid)
        if l is None:
            out["adopted"].append(r)
            continue
        if (str(l.get("status") or "") != str(r.get("status") or "")
                or abs(_exec_qty(l) - _exec_qty(r)) > 1e-12):
            out["changed"].append({"local": l, "remote": r})
        else:
            out["same"].append({"local": l, "remote": r})
    for cid, l in loc.items():
        if cid not in rem:
            out["vanished"].append(l)
    return out


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
                elif kind == "working_order":
                    # 幂等 upsert，按 client_order_id。挂单会被反复更新
                    # （挂上 → 部分成交 → 成交/撤单），每次都是一次 upsert。
                    #
                    # exchange_order_id 用 COALESCE 保住已有的：本地先落库时
                    # 这一列是空的（那时交易所还没给 id），后来的更新如果
                    # 又没带 id，不能把之前拿到的 id 抹掉 —— 抹掉之后就
                    # 再也查不到这张单了。
                    c.execute(
                        "INSERT INTO working_orders(client_order_id,"
                        "exchange_order_id,symbol,side,otype,qty,executed_qty,"
                        "price,stop_price,avg_price,reduce_only,status,purpose,"
                        "strategy,round_key,created_ts,updated_ts,synced_ts,note)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(client_order_id) DO UPDATE SET"
                        " exchange_order_id=COALESCE(NULLIF(excluded.exchange_order_id,''),"
                        "                              working_orders.exchange_order_id),"
                        " executed_qty=excluded.executed_qty,"
                        " avg_price=excluded.avg_price,"
                        " status=excluded.status,"
                        " updated_ts=excluded.updated_ts,"
                        " synced_ts=excluded.synced_ts,"
                        " note=excluded.note",
                        (d["client_order_id"],
                         d.get("exchange_order_id") or "",
                         d.get("symbol") or "",
                         d.get("side") or "",
                         d.get("otype") or d.get("type") or "",
                         float(d.get("qty") or 0),
                         float(d.get("executed_qty") or 0),
                         float(d.get("price") or 0),
                         float(d.get("stop_price") or 0),
                         float(d.get("avg_price") or 0),
                         int(d.get("reduce_only") or 0),
                         d.get("status") or "NEW",
                         d.get("purpose") or "",
                         d.get("strategy") or "",
                         d.get("round_key") or "",
                         int(d.get("created_ts") or 0) or int(time.time() * 1000),
                         int(d.get("updated_ts") or 0) or int(time.time() * 1000),
                         int(d.get("synced_ts") or 0),
                         d.get("note") or ""))
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
                elif kind == "live_round":
                    # 幂等 upsert。同一个回合每个对账周期都会重抓一次，
                    # 没有 ON CONFLICT 就会一天写 288 条重复（见建表注释）。
                    #
                    # 为什么用 ON CONFLICT DO UPDATE 而不是 INSERT OR REPLACE：
                    # REPLACE 是「删掉再插」，id 每次翻新；看板按 id 锚行时
                    # 那一行会跳。ON CONFLICT 保留原 id。
                    #
                    # 唯一不能覆盖的是 trade_id：引擎可能比交易所晚一拍才写出
                    # trades 行，先抓到的 round 没有 trade_id。用 COALESCE 保住
                    # 已经关联上的那个，否则会被后来的空值抹掉。
                    c.execute(
                        "INSERT INTO live_rounds(round_key,symbol,side,qty,"
                        "entry_ts,entry_price,exit_ts,exit_price,gross,fee,funding,"
                        "net,hold_sec,n_fills,notional,source,trade_id,open_reason,"
                        "mfe,mae,score_pnl,score_entry,score_exit,score_cost,"
                        "score_total,grade,analysis,analyzed_ts,synced_ts)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(round_key) DO UPDATE SET"
                        " exit_ts=excluded.exit_ts,"
                        " exit_price=excluded.exit_price,"
                        " qty=excluded.qty, gross=excluded.gross,"
                        " fee=excluded.fee, funding=excluded.funding,"
                        " net=excluded.net, hold_sec=excluded.hold_sec,"
                        " n_fills=excluded.n_fills, notional=excluded.notional,"
                        " source=excluded.source,"
                        " trade_id=COALESCE(excluded.trade_id, live_rounds.trade_id),"
                        " open_reason=excluded.open_reason,"
                        " mfe=excluded.mfe, mae=excluded.mae,"
                        " score_pnl=excluded.score_pnl,"
                        " score_entry=excluded.score_entry,"
                        " score_exit=excluded.score_exit,"
                        " score_cost=excluded.score_cost,"
                        " score_total=excluded.score_total,"
                        " grade=excluded.grade, analysis=excluded.analysis,"
                        " analyzed_ts=excluded.analyzed_ts,"
                        " synced_ts=excluded.synced_ts",
                        (d["round_key"], d["symbol"], d["side"], d["qty"],
                         d["entry_ts"], d["entry_price"], d.get("exit_ts"),
                         d.get("exit_price"), d.get("gross", 0),
                         d.get("fee", 0), d.get("funding", 0), d.get("net", 0),
                         d.get("hold_sec", 0), d.get("n_fills", 0),
                         d.get("notional", 0), d.get("source", "manual"),
                         d.get("trade_id"), d.get("open_reason", ""),
                         d.get("mfe"), d.get("mae"),
                         d.get("score_pnl"), d.get("score_entry"),
                         d.get("score_exit"), d.get("score_cost"),
                         d.get("score_total"), d.get("grade", ""),
                         d.get("analysis", ""), int(d.get("analyzed_ts") or 0),
                         int(d.get("synced_ts") or time.time() * 1000)))
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

    def open_trades(self, limit: int = 5) -> List[dict]:
        """**所有**未平仓记录，按 id 倒序。

        ★ 为什么需要复数版本（2026-11-24）：
          引擎现在把持仓拆成两个来源记账 —— `self.pos`（系统仓）和
          `self.adopted_pos`（接管来的人工仓）。两笔仓可能同时在账上开着，
          而 `open_trade()` 只返回最新的一行（`LIMIT 1`）。
          只用它恢复，重启后必然丢掉其中一个来源，而且丢哪个取决于 id 顺序
          —— 这正是「同一件事两个来源」的又一次变形。

        ★ 保持 `open_trade()` 原样不动：它是别处（close_trade 记账等）的
          既有契约，改它的返回值等于静默改变所有调用方的行为。
        """
        c = self._ro()
        try:
            rows = c.execute(
                "SELECT id,symbol,side,qty,entry_ts,entry_price,stop_price,target_price,"
                "open_reason,paper,COALESCE(max_hold_bars,0),"
                "COALESCE(fee,0) FROM trades"
                " WHERE exit_ts IS NULL ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        finally:
            c.close()
        return [{"id": r[0], "symbol": r[1], "side": r[2], "qty": r[3],
                 "entry_ts": r[4], "entry_price": r[5], "stop_price": r[6],
                 "target_price": r[7], "open_reason": r[8], "paper": r[9],
                 "max_hold_bars": r[10], "fee": float(r[11] or 0.0)}
                for r in rows]

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
            # 「对账接管」的仓不是策略开的，绝不能被算进策略战绩（第 129 轮）。
            #
            # 生产实证（11-11）：用户手工来回买卖 BTC，每一次数量变化
            # 都被对账接管、又被策略平掉，一天之内写出 12 条
            # pnl_gross=0 / fee=0 / pnl_net=0 的空记录。
            # 这 12 条如果算进 donchian 的战绩：
            #   交易数 12 笔全"记账"、胜率按 pnl_net>0 判全部算负、
            #   而它们既不是策略开的、也不是策略平的。
            # 用户看大屏会以为策略在密集交易并且全亏。
            #
            # 为什么用 open_reason 而不是新加一列：这 12 条历史记录已经写进库了，
            # 加列只能影响未来，改不掉"历史统计里混着手工单"这件事。
            # open_reason 是当时就写下去的，能回溯。
            where += " AND COALESCE(open_reason,'') <> ?"
            args: tuple = (ADOPT_REASON,)
            if strategy:
                where += " AND strategy=?"
                args = args + (strategy,)
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
                    "   AND COALESCE(open_reason,'') <> ?"
                    " GROUP BY COALESCE(strategy,''), paper",
                    (ADOPT_REASON,)).fetchall():
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

    # ---------------- 真实持仓回合（第 130 轮） ----------------
    def upsert_live_rounds(self, rows: List[dict]) -> None:
        """把从交易所还原出来的回合写进去。幂等（按 round_key）。"""
        now = int(time.time() * 1000)
        for r in rows or ():
            d = dict(r)
            d.setdefault("synced_ts", now)
            self._put(("live_round", d))

    # ---------------- 挂单账本 ----------------
    WORKING_ORDER_COLS = (
        "client_order_id", "exchange_order_id", "symbol", "side", "otype",
        "qty", "executed_qty", "price", "stop_price", "avg_price",
        "reduce_only", "status", "purpose", "strategy", "round_key",
        "created_ts", "updated_ts", "synced_ts", "note")

    def upsert_working_orders(self, rows: List[dict]) -> None:
        """写/更新挂单。幂等（按 client_order_id）。"""
        now = int(time.time() * 1000)
        for r in rows or ():
            d = dict(r)
            d.setdefault("created_ts", now)
            d.setdefault("updated_ts", now)
            if not d.get("client_order_id"):
                raise ValueError("working_orders 必须有 client_order_id")
            self._put(("working_order", d))

    def working_orders(self, active_only: bool = True,
                       limit: int = 0) -> List[dict]:
        """本地记着的挂单。active_only 只返回「还挂着」的。"""
        c = self._ro()
        try:
            sql = ("SELECT " + ", ".join(self.WORKING_ORDER_COLS)
                   + " FROM working_orders")
            args: List = []
            if active_only:
                sql += " WHERE status IN ('NEW','PARTIALLY_FILLED')"
            sql += " ORDER BY updated_ts DESC, id DESC"
            if limit:
                sql += " LIMIT ?"
                args.append(limit)
            rows = c.execute(sql, tuple(args)).fetchall()
        finally:
            c.close()
        return [dict(zip(self.WORKING_ORDER_COLS, r)) for r in rows]

    def find_working_order(self, client_order_id: str) -> Optional[dict]:
        got = self.working_orders(active_only=False)
        for o in got:
            if o["client_order_id"] == client_order_id:
                return o
        return None

    def apply_order_reconciliation(self, result: dict, now_ms: int = 0
                                   ) -> dict:
        """把 `reconcile_orders()` 的结果落到本地账上。

        * `adopted` → 补进本地（status 取交易所的）
        * `changed` → 用交易所的状态覆盖本地
        * `vanished` → 标记 `LOST`，**不删**。
          删掉就等于认定「它从没存在过」，而它可能是一笔已经成交的单。
          留成 LOST 才能被后续 `query_order()` 查清真实终态，
          也才能在对账报表里被看见。
        * `same` → 只更新 `synced_ts`（表示"这一轮对过了"）
        """
        now_ms = now_ms or int(time.time() * 1000)
        n_adopt = n_changed = n_lost = n_same = 0
        for r in result.get("adopted") or ():
            self.upsert_working_orders([self._from_remote(r, now_ms)])
            n_adopt += 1
        for pair in result.get("changed") or ():
            self.upsert_working_orders([self._from_remote(pair["remote"], now_ms,
                                                          pair["local"])])
            n_changed += 1
        for l in result.get("vanished") or ():
            d = dict(l)
            d["status"] = "LOST"
            d["updated_ts"] = now_ms
            d["synced_ts"] = now_ms
            d["note"] = (d.get("note") or "") + " | 对账：交易所查不到"
            self.upsert_working_orders([d])
            n_lost += 1
        for pair in result.get("same") or ():
            d = self._from_remote(pair["remote"], now_ms, pair["local"])
            d["synced_ts"] = now_ms
            self.upsert_working_orders([d])
            n_same += 1
        return {"adopted": n_adopt, "changed": n_changed,
                "lost": n_lost, "same": n_same}

    @staticmethod
    def _from_remote(r: dict, now_ms: int, local: Optional[dict] = None) -> dict:
        """把交易所返回的订单（或 PendingOrder）转成 working_orders 的一行。

        两种入参都吃：`PendingOrder` 有 `client_id`，币安原始 JSON 有
        `clientOrderId`。本地原有的 purpose/strategy/round_key 要保留 ——
        那些信息交易所不知道，丢了就再也补不回来。
        """
        r = order_row(r)
        loc = local or {}
        cid = (r.get("client_id") or r.get("client_order_id")
               or r.get("clientOrderId") or loc.get("client_order_id") or "")
        return {
            "client_order_id": cid,
            "exchange_order_id": str(r.get("order_id") or r.get("orderId")
                                     or loc.get("exchange_order_id") or ""),
            "symbol": r.get("symbol") or loc.get("symbol") or "",
            "side": r.get("side") or loc.get("side") or "",
            "otype": (r.get("type") or r.get("otype")
                      or loc.get("otype") or ""),
            "qty": float(r.get("qty") or r.get("origQty")
                         or loc.get("qty") or 0),
            "executed_qty": float(r.get("executed_qty") or r.get("executedQty")
                                  or 0),
            "price": float(r.get("price") or loc.get("price") or 0),
            "stop_price": float(r.get("stop_price") or r.get("stopPrice")
                                or loc.get("stop_price") or 0),
            "avg_price": float(r.get("avg_price") or r.get("avgPrice") or 0),
            "reduce_only": int(bool(r.get("reduce_only")
                                    or r.get("reduceOnly")
                                    or loc.get("reduce_only"))),
            "status": r.get("status") or loc.get("status") or "NEW",
            "purpose": loc.get("purpose") or "",
            "strategy": loc.get("strategy") or "",
            "round_key": loc.get("round_key") or "",
            "created_ts": int(loc.get("created_ts") or now_ms),
            "updated_ts": now_ms,
            "synced_ts": now_ms,
            "note": loc.get("note") or "",
        }

    def recent_live_rounds(self, limit: int = 20,
                           closed_only: bool = False) -> List[dict]:
        """最近 N 单。默认「只看已平仓」——还没平的仓没有出场价，
        也没有 MFE/MAE 和分数，混在表里会让"平均分"失真。"""
        c = self._ro()
        try:
            where = " WHERE 1=1"
            if closed_only:
                where += " AND exit_ts IS NOT NULL"
            rows = c.execute(
                "SELECT round_key,symbol,side,qty,entry_ts,entry_price,exit_ts,"
                "exit_price,gross,fee,funding,net,hold_sec,n_fills,notional,"
                "source,trade_id,open_reason,mfe,mae,score_pnl,score_entry,"
                "score_exit,score_cost,score_total,grade,analysis,analyzed_ts,"
                "synced_ts FROM live_rounds" + where +
                " ORDER BY COALESCE(exit_ts, entry_ts) DESC, id DESC LIMIT ?",
                (limit,)).fetchall()
        finally:
            c.close()
        keys = ("round_key", "symbol", "side", "qty", "entry_ts", "entry_price",
                "exit_ts", "exit_price", "gross", "fee", "funding", "net",
                "hold_sec", "n_fills", "notional", "source", "trade_id",
                "open_reason", "mfe", "mae", "score_pnl", "score_entry",
                "score_exit", "score_cost", "score_total", "grade", "analysis",
                "analyzed_ts", "synced_ts")
        return [dict(zip(keys, r)) for r in rows]

    def live_rounds_summary(self, limit: int = 20) -> dict:
        """最近 N 单的汇总。分开给「已平仓」和「含未平仓」两个口径，
        因为未平仓的 net 是浮的，混进胜率就是错的。"""
        c = self._ro()
        try:
            rows = c.execute(
                "SELECT net, score_total, exit_ts, fee, funding, gross"
                " FROM (SELECT net, score_total, exit_ts, fee, funding, gross"
                "       FROM live_rounds"
                "       ORDER BY COALESCE(exit_ts, entry_ts) DESC, id DESC LIMIT ?)",
                (limit,)).fetchall()
        finally:
            c.close()
        closed = [r for r in rows if r[2] is not None]
        scored = [r for r in closed if r[1] is not None]
        net = sum(r[0] or 0.0 for r in closed)
        return {
            "n": len(rows),
            "n_closed": len(closed),
            "n_open": len(rows) - len(closed),
            "n_win": sum(1 for r in closed if (r[0] or 0.0) > 0),
            "win_rate": (round(sum(1 for r in closed if (r[0] or 0.0) > 0)
                               / len(closed), 4) if closed else None),
            "net": round(net, 4),
            "gross": round(sum(r[5] or 0.0 for r in closed), 4),
            "fee": round(sum(r[3] or 0.0 for r in closed), 4),
            "funding": round(sum(r[4] or 0.0 for r in closed), 4),
            "avg_score": (round(sum(r[1] for r in scored) / len(scored), 1)
                          if scored else None),
        }

    def live_round_keys(self) -> dict:
        """已入库的回合：round_key -> 上一次的分析结果与来源标注。

        两个用途：
        1. 同步时跳过「已经算过、成交笔数没变」的回合，
           否则每个对账周期都要为每一单重拉一次 1m K 线去算 MFE/MAE。
        2. **把旧的分析结果搬回来**。同步是整行覆盖，而重新还原出来的行
           没有分数（分数要额外拉 K 线才算）。不搬的话，已经算过的单
           每 5 分钟就会被清空一次，看板上刚出现的分数又消失。
        """
        c = self._ro()
        try:
            out = {}
            for r in c.execute(
                    "SELECT round_key, n_fills, analyzed_ts, mfe, mae,"
                    " score_pnl, score_entry, score_exit, score_cost,"
                    " score_total, grade, analysis, trade_id, open_reason"
                    " FROM live_rounds").fetchall():
                out[r[0]] = {
                    "n_fills": r[1] or 0, "analyzed_ts": r[2] or 0,
                    "mfe": r[3], "mae": r[4],
                    "score_pnl": r[5], "score_entry": r[6],
                    "score_exit": r[7], "score_cost": r[8],
                    "score_total": r[9], "grade": r[10] or "",
                    "analysis": r[11] or "", "trade_id": r[12],
                    "open_reason": r[13] or "",
                }
            return out
        finally:
            c.close()

    def trade_ids_for_adoption(self) -> dict:
        """trades 表里「引擎经手过」的仓：entry_ts -> {id, open_reason, strategy}。

        用来给回合标 source。按 entry_ts 匹配，因为引擎写 trades 时
        entry_ts 就是它认定的开仓时刻；差一秒就标不上，
        宁可标 manual（不知道）也不猜。
        """
        c = self._ro()
        try:
            out = {}
            for tid, ets, side, why, stg in c.execute(
                    "SELECT id, entry_ts, COALESCE(side,''),"
                    " COALESCE(open_reason,''), COALESCE(strategy,'')"
                    " FROM trades WHERE entry_ts IS NOT NULL").fetchall():
                out.setdefault(int(ets), {"id": tid, "side": side,
                                          "open_reason": why,
                                          "strategy": stg})
            return out
        finally:
            c.close()


def _j(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None