# -*- coding: utf-8 -*-
"""K 线演化 · 存储层（独立库 data/kbar.db）。

为什么必须是**独立库**
-----------------------
生产在用的 `data/market.db`（行情）与 `data/market_range.db`（区间演化）只读不写。
把新表塞进去有两种后果，都会真的发生：
  1. 一次 `CREATE TABLE` 就写了生产库，而写库的时机是「服务正在跑」；
  2. 回填要 REPLACE 大段历史时，DBA 分不清动的是行情还是新能力。
所以第 1 步起就建 `data/kbar.db`，与两个生产库物理隔离。

本章第 1 步只有一张表：`kbar_line`（每根 K × 每条线一行）
------------------------------------------------------------
设计稿 **§6 表 0 = kbar_line**（2026-11-07 用户拍定 A 方案补入设计稿 v3.1）。
原稿 §6 只写三张表（kbar_evolution / kbar_reaction / kbar_forward，分别是
「事实层 / 反应层 / 事后层」，属于第 2、3、5 步），但第 1 步要求「落库」，
§13 第 1 步验收又引用「观察线表里 1h 的值」——三张表没有一张能装
「每条观察线在每根 K 上的价格」，所以是**设计漏了一张表**。
     kbar_reaction 只记「碰到的线」，所以它落不下「没碰到的线在哪」——
     而「差一点碰到」和「离得很远」是两件事，第 3 步要用。
第 1 步先落全量快照 kbar_line（表 0）；第 3 步的 kbar_reaction 从它派生。

字段
----
    line_key       "ema200@15m" / "range_hard_up@1h" —— 下游唯一引用键
    line_kind      不带周期的种类名（ema200 / range_hard_up ...）
    line_period_ms 这条线自己的周期。EMA200(15m) 与 EMA200(1h) 靠它区分
    line_price     线在那一刻的价格
    bar_close      本根收盘价（算 dist_pct 用，也便于脱离 bars 表独立核对）
    dist_pct       (bar_close - line_price) / line_price * 100
    src_ts         线值来源那根 K 的开盘时间
    line_ready_ms  该值最早可用时刻 = src_ts + line_period_ms（防偷未来的物证）

幂等
----
UNIQUE(symbol, interval_ms, ts, line_key) + ON CONFLICT DO UPDATE：
重跑同一段区间得到同一批行，回填中断可以从任意位置续。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..dbutil import synchronized as _synchronized

SCHEMA = """
CREATE TABLE IF NOT EXISTS kbar_line (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT    NOT NULL,
    interval_ms    INTEGER NOT NULL,
    ts             INTEGER NOT NULL,
    line_key       TEXT    NOT NULL,
    line_kind      TEXT    NOT NULL,
    line_period_ms INTEGER NOT NULL,
    line_price     REAL    NOT NULL,
    bar_close      REAL,
    dist_pct       REAL,
    src_ts         INTEGER NOT NULL,
    line_ready_ms  INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, interval_ms, ts, line_key)
);
CREATE INDEX IF NOT EXISTS idx_kbar_line_ts  ON kbar_line(symbol, interval_ms, ts);
CREATE INDEX IF NOT EXISTS idx_kbar_line_key ON kbar_line(symbol, interval_ms, line_key, ts);

CREATE TABLE IF NOT EXISTS kbar_evolution (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    interval_ms     INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    idx             INTEGER NOT NULL,
    -- 区间演化空间位置（1h 维度，只是四维之一）
    range_ts_h      INTEGER,
    fam             INTEGER,
    seg             INTEGER,
    seg_idx         INTEGER,
    direction       TEXT,
    zone            TEXT,
    -- K 线结构
    o REAL, h REAL, l REAL, c REAL,
    dir             INTEGER,
    body_pct        REAL,
    up_wick_pct     REAL,
    dn_wick_pct     REAL,
    close_pos       REAL,
    -- 量能
    vol             REAL,
    vol_rel         REAL,
    vol_slot_rel    REAL,
    taker_buy_ratio REAL,
    conc            REAL,
    -- 内路径（3s）
    path_shape      TEXT,
    max_tick_share  REAL,
    t_low_min       INTEGER,
    t_high_min      INTEGER,
    -- 观察线反应汇总：第 3 步填 n_touch；n_break 至今没有生产者（待确定 D-10）
    n_lines         INTEGER,
    n_touch         INTEGER,
    n_break         INTEGER,
    -- 事件（第 4 步填）
    event           TEXT,
    prev_event      TEXT,
    on_line         TEXT,
    since_event_idx INTEGER,
    state           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, interval_ms, ts)
);
CREATE INDEX IF NOT EXISTS idx_kbar_evo_ts ON kbar_evolution(symbol, interval_ms, ts);
CREATE INDEX IF NOT EXISTS idx_kbar_evo_ev ON kbar_evolution(symbol, interval_ms, event, ts);

-- 表 2：反应层。只有「碰到」的 (根, 线) 对进这张表 —— 没碰到不是一种反应，
-- 它的事实记录在表 0。所以这里没有 touch 列。
-- ⚠ 正因为如此：**事件不保证在这里有对应行**（跳空穿越时本根没碰到线）。
--   下游取 side/pen_depth/recover 必须 LEFT JOIN 并按 (ts, line_key) 对齐，
--   INNER JOIN 会静默丢掉这类事件（实测 716/18,813 条）。
CREATE TABLE IF NOT EXISTS kbar_reaction (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    interval_ms     INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    line_key        TEXT    NOT NULL,
    line_kind       TEXT,
    line_period_ms  INTEGER,
    line_price      REAL    NOT NULL,
    dist_pct        REAL,
    side            TEXT,
    pen_depth_pct   REAL,
    recover_pct     REAL,
    close_side      TEXT,
    dwell_bars      INTEGER,
    vol_rel         REAL,
    taker_buy_ratio REAL,
    nth_touch       INTEGER,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, interval_ms, ts, line_key)
);
CREATE INDEX IF NOT EXISTS idx_kbar_rx_ts  ON kbar_reaction(symbol, interval_ms, ts);
CREATE INDEX IF NOT EXISTS idx_kbar_rx_key ON kbar_reaction(symbol, interval_ms, line_key, ts);

-- 表 2.5：事件层（第 4 步）。每个 (根, 线, 事件) 一行。
-- 为什么不只写表 1 的 event 列：表 1 一根 K 线只有一组 event/on_line，
-- 而一根 K 线可能同时突破了三条线。只写表 1 就等于把另外两个事件丢掉，
-- 而设计稿 §8.3 明确要求「事件必须带 on_line」—— 丢事件就是丢事实。
-- 这张表是事件的**权威**来源；表 1 的 event 列只是它的一个（有损的）读法。
-- ⚠ 本表不保证在表 2 有对应明细（跳空穿越时本根没碰到线），见 kbar_reaction 上方注释。
CREATE TABLE IF NOT EXISTS kbar_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    interval_ms     INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    idx             INTEGER NOT NULL,
    line_key        TEXT    NOT NULL,
    line_kind       TEXT,
    line_period_ms  INTEGER,
    -- ⚠ 这里是**判断用的那个值** = 上一根 (ts - interval) 那一行的线价，
    --   不是本根的线价。想拿本根线价要 join kbar_line 的同一个 ts。
    --   直接按 ts join 会拿到另一个数（实测抽样 400 条里只有 63% 恰好相等）。
    line_price      REAL    NOT NULL,
    event           TEXT    NOT NULL,
    prev_event      TEXT,
    since_event_idx INTEGER,
    close_side      TEXT,
    pen_depth_pct   REAL,
    vol_rel         REAL,
    taker_buy_ratio REAL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, interval_ms, ts, line_key, event)
);
CREATE INDEX IF NOT EXISTS idx_kbar_ev_ts    ON kbar_event(symbol, interval_ms, ts);
CREATE INDEX IF NOT EXISTS idx_kbar_ev_key   ON kbar_event(symbol, interval_ms, line_key, ts);
CREATE INDEX IF NOT EXISTS idx_kbar_ev_event ON kbar_event(symbol, interval_ms, event, ts);

-- 表 3：事后确认（第 5 步）。**禁止参与当下判定。**
-- 「之后涨了」是事后信息，混进表 1 的宽表就等于让回测偷未来。
-- 物理隔开比靠自觉可靠：当下判定路径（lines/facts/reaction/event）不许 import forward.py，
-- tools/probe_kbar_forward 之外也不许把它读进任何实时决策——有测试钉着这条。
-- ⚠ held 那三列不在设计稿的表结构里，是为了让 held **可审计**：
--   设计稿只说「held INT」，没说是哪条线守住了。不写下来就没人能回答。
CREATE TABLE IF NOT EXISTS kbar_forward (
    symbol          TEXT    NOT NULL,
    interval_ms     INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    -- ret_k = c[ts + k*interval] / c[ts] - 1，收盘对收盘、不扣费（D-12 占位）
    ret1            REAL,
    ret3            REAL,
    ret5            REAL,
    -- held：之后 N 根的收盘是否一直守在「事件判定那一侧」。
    -- 1=守住 0=没守住 NULL=后面不足 N 根（算不出来就留空，不猜）
    held            INTEGER,
    -- 下面三列是 held 的审计信息，没有事件（或拿不到参照价）的根全空
    held_line       TEXT,
    held_ref_price  REAL,
    held_side       TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, interval_ms, ts)
);
CREATE INDEX IF NOT EXISTS idx_kbar_fw_ts   ON kbar_forward(symbol, interval_ms, ts);
CREATE INDEX IF NOT EXISTS idx_kbar_fw_held ON kbar_forward(symbol, interval_ms, held, ts);

-- 表 5（kbar_shape）：读数层的**结论**，每根 K 线一行。
-- 用户 2026-11-08 明确要求：「所有演化结论字段或者描述都要入 db 库」。
-- 与表 0-4 的差别：表 0-4 存**客观值**，本表存**判断**（哪个状态词、为什么）。
-- ⚠ params_hash 是这张表的安全阀：阈值是临时的（D-21），一旦落库就等于把
--   「临时」冻成「历史」。每行带阈值指纹，谁改了 shape.py 顶部常量而没重跑，
--   tools/check_kbar_pipeline.py 立刻红，show_kbar 也会在标题上打「旧口径」。
CREATE TABLE IF NOT EXISTS kbar_shape (
    symbol           TEXT    NOT NULL,
    interval_ms      INTEGER NOT NULL,
    ts               INTEGER NOT NULL,
    -- 结论
    label            TEXT    NOT NULL,   -- 状态词之一；「无」也存字面 '无'，不留空
    states           TEXT    NOT NULL,   -- 同时成立的全部状态，逗号分隔，按 STATE_ORDER；'无' 时是空串
    n_states         INTEGER NOT NULL,
    side             TEXT,               -- 本根自己是针时的方向 down/up
    pin_kind         TEXT,               -- 本根自己是针时的形态 wick/swing
    ref_pin_ts       INTEGER,            -- 稳住/远离时参照的那根针
    ref_pin_kind     TEXT,               -- 参照针的形态
    ref_pin_side     TEXT,               -- 参照针的方向 down/up（佐证层靠它判「守住没」）
    -- 生产结论的证据（客观值再抄一份，便于只读这张表就能复核）
    rng              REAL,
    body_pct         REAL,
    up_wick_pct      REAL,
    dn_wick_pct      REAL,
    close_pos        REAL,
    vol_rel          REAL,
    taker_buy_ratio  REAL,
    n_recovered      INTEGER NOT NULL DEFAULT 0,
    recovered        TEXT,               -- 'line_key@price:side' 用 | 分隔
    -- 人读的那段描述（show_kbar 打印的 · 行，多行用换行符连接，不用逗号）
    evidence         TEXT    NOT NULL,
    -- 口径指纹
    params_hash      TEXT    NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, interval_ms, ts)
);
CREATE INDEX IF NOT EXISTS idx_kbar_shp_ts    ON kbar_shape(symbol, interval_ms, ts);
CREATE INDEX IF NOT EXISTS idx_kbar_shp_label ON kbar_shape(symbol, interval_ms, label, ts);

-- 表 6（kbar_verify）：佐证层。**只有这张表允许看后面的 K 线。**
-- 它回答的不是「之后涨没涨」（那是表 3），而是「当时那句话对不对」。
--   表 3 ret1/ret3/ret5  = 客观涨跌，与判断无关，同一根涨了也可能判错
--   表 6 verdict         = 把结论词翻译成一句**可被推翻的话**，再看后面 N 根有没有打脸
-- ⚠ 红线：ai_trader/kbar/shape.py 永远不许 import verify —— 上表一读，
--   判定就变成偷看未来。与表 3 完全同一条红线，有测试扫源码钉着。
-- ⚠ 结论词若给不出可推翻的预言，就记 '无预言'，**不许**凑一个成立率出来：
--   那会把「我没说法」伪装成「我说对了」。
CREATE TABLE IF NOT EXISTS kbar_verify (
    symbol        TEXT    NOT NULL,
    interval_ms   INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    horizon       INTEGER NOT NULL,   -- 后面看几根 N
    -- 被佐证的那句话
    label         TEXT    NOT NULL,   -- 表 5 当时给的 label
    states        TEXT    NOT NULL,   -- 表 5 当时给的全部状态
    pred_kind     TEXT    NOT NULL,   -- 预言种类：守住低点/破低点/守住高点/破高点/无预言…
    pred_text     TEXT    NOT NULL,   -- 人读的那句话（含具体价位，可复核）
    -- 裁决
    verdict       TEXT    NOT NULL,   -- 佐证成立/被推翻/无法判定/无预言
    ref_price     REAL,               -- 这句话针对的那个价位（针低/针高/本根极值）
    actual_price  REAL,               -- 后面 N 根实际打到的极值
    diff_pts      REAL,               -- actual - ref（正=没破，负=破了）
    n_bars        INTEGER NOT NULL,   -- 实际取到的后续根数（凑不满 N 时 < N）
    -- 口径
    tol_pts       REAL    NOT NULL,   -- 佐证容差。0 = 碰到就算破
    params_hash   TEXT    NOT NULL,   -- 表 5 的阈值指纹（对齐表 5 那一行）
    verify_hash   TEXT    NOT NULL,   -- 本层自己的口径指纹（N + 容差 + 预言规则版本）
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, interval_ms, ts, horizon)
);
CREATE INDEX IF NOT EXISTS idx_kbar_vfy_ts   ON kbar_verify(symbol, interval_ms, ts);
CREATE INDEX IF NOT EXISTS idx_kbar_vfy_vd   ON kbar_verify(symbol, interval_ms, verdict, horizon);

-- 表 6b（kbar_verify_survival）：一句话「活了多久」。
-- 为什么另开一张表而不是在表 6 上加一列：
--   表 6 的行是 (ts, horizon)，一根能有多行；存活是「一根一个数」，粒度不同。
--   而且表 6 的 verify_hash 被 §7.7 钉着，往里加口径会把全库指纹改掉。
-- 三种状态必须分开（详见 verify.Survival）：
--   已破     → 第 break_n 根被打脸/被证实，定型
--   活到最后 → 满 max_n 根都没破，**看完了**
--   未成熟   → 后面的根不够 max_n，**还没看完** ← 表尾，不许当结论
-- ⚠ 「未成熟」与「活到最后」都表现为 break_n IS NULL。只按 break_n 是否为空
--   去统计，会把表尾的「还没验」算成「验完了没破」—— status 列就是为了防这个。
CREATE TABLE IF NOT EXISTS kbar_verify_survival (
    symbol        TEXT    NOT NULL,
    interval_ms   INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    label         TEXT    NOT NULL,
    pred_kind     TEXT    NOT NULL,
    must_hold     INTEGER NOT NULL,   -- 1=「不该破」 0=「该破」
    status        TEXT    NOT NULL,   -- 已破/活到最后/未成熟
    break_n       INTEGER,            -- 第几根第一次破；未破为 NULL
    n_seen        INTEGER NOT NULL,   -- 实际看到几根（未成熟时 < max_n）
    max_n         INTEGER NOT NULL,   -- 本层截断到几根
    ref_price     REAL,
    actual_at_break REAL,
    tol_pts       REAL    NOT NULL,
    params_hash   TEXT    NOT NULL,
    survival_hash TEXT    NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, interval_ms, ts)
);
CREATE INDEX IF NOT EXISTS idx_kbar_svv_st ON kbar_verify_survival(symbol, interval_ms, status);
CREATE INDEX IF NOT EXISTS idx_kbar_svv_bn ON kbar_verify_survival(symbol, interval_ms, break_n);

-- 表 7（kbar_dims）：一根 K 线一份「环境快照」——39 个维度摊开。
--
-- ⚠ **这一层不参与判定。** 它不改变任何结论词，也不改变任何阈值。
--   作用是把「判据没读、但库里一直躺着」的那些维度（时/空/情绪 19 个 +
--   观察线反应里没读的 7 个）摊到人眼前。见 dims.py 模块文档。
--
-- 为什么不塞进表 5：
--   ① 表 5 是「判定」，39 个描述列塞进去会把判定表淹没；
--   ② 表 5 的 params_hash 契约是「阈值指纹」，加描述列会让它名不副实；
--   ③ 这一层要能单独重算、单独版本化（dims_hash），和判定解耦。
--
-- values_json / buckets_json：39 个维度值放 JSON 一列，不摊成 39 个物理列。
--   理由：维度会继续加（用户已批准的那 8 个候选还没进），每加一个就 ALTER
--   一次表不现实；而且这一层只读不查（不走 SQL 过滤某个维度）。
--   查询靠 dims.py 的 DIM_NAMES 解释，不靠 schema。
-- notes_json：旁证 / 矛盾提示（列表）。同样只提示，不改结论。
CREATE TABLE IF NOT EXISTS kbar_dims (
    symbol      TEXT    NOT NULL,
    interval_ms INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    label       TEXT,                 -- 对应表 5 的结论词（便于并排看）
    n_dims      INTEGER NOT NULL,     -- 实际算出几个维度（应等于 dims.DIMS 长度）
    n_filled    INTEGER NOT NULL,     -- 其中几个有值（None 不算）
    values_json TEXT    NOT NULL,
    buckets_json TEXT   NOT NULL,
    notes_json  TEXT    NOT NULL,
    dims_hash   TEXT    NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(symbol, interval_ms, ts)
);
CREATE INDEX IF NOT EXISTS idx_kbar_dims_lb ON kbar_dims(symbol, interval_ms, label);

-- 表 8（kbar_holdout_log）：留出集评测的**预登记日志**。
--
-- 为什么要有这张表：留出集只能用一次。第一次在上面评测完，那一段样本就
-- 「脏」了。如果评测不留痕，就可以反复跑、挑最好看的那次报出来 ——
-- 这不是假设，是研究里最常见的作弊方式。
--
-- 所以每次评测落一行：用的哪个切分、测了多少格、几格过线、什么时候测的。
-- 第二次、第三次都会留下行号。谁都能看见「这个结果是在第几次尝试里出现的」。
--
-- ⚠ 这张表**只增不改**。想改切分 = 新 split_version，旧结论全部作废。
CREATE TABLE IF NOT EXISTS kbar_holdout_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    interval_ms   INTEGER NOT NULL,
    split_version TEXT    NOT NULL,
    split_hash    TEXT    NOT NULL,
    dims_hash     TEXT    NOT NULL,
    train_n       INTEGER NOT NULL,
    holdout_n     INTEGER NOT NULL,
    embargo_n     INTEGER NOT NULL,
    n_tested      INTEGER NOT NULL,   -- 一共验了多少格
    n_pass_cluster INTEGER,           -- 几格 z_cluster（主口径）过 1.96
    n_pass_block  INTEGER NOT NULL,   -- 几格 z_block 过 1.96（对照口径）
    n_pass_naive  INTEGER NOT NULL,   -- 几格 z_naive 过 1.96
    n_expected    REAL    NOT NULL,   -- 纯靠运气该有几格过线
    z_bonferroni  REAL    NOT NULL,
    n_pass_bonf   INTEGER NOT NULL,
    note          TEXT,               -- 这次评测想验什么（人填）
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_kbar_hl_sp ON kbar_holdout_log(symbol, interval_ms, split_hash);

CREATE TABLE IF NOT EXISTS kbar_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

COLS = ("symbol, interval_ms, ts, line_key, line_kind, line_period_ms, "
        "line_price, bar_close, dist_pct, src_ts, line_ready_ms")

# 表 1（kbar_evolution）的列分成两组，这样重跑第 2 步**不会**把第 3/4 步填的
# 字段抹成 NULL。踩过这个坑的代价是：回填一次就把反应层/事件层清空，
# 而且不报错，只是数据变空。
EVO_FACT_COLS = ("symbol, interval_ms, ts, idx, range_ts_h, fam, seg, seg_idx, "
                 "direction, zone, o, h, l, c, dir, body_pct, up_wick_pct, "
                 "dn_wick_pct, close_pos, vol, vol_rel, vol_slot_rel, "
                 "taker_buy_ratio, conc, path_shape, max_tick_share, "
                 "t_low_min, t_high_min, n_lines")
EVO_LATE_COLS = ("n_touch, n_break, event, prev_event, on_line, since_event_idx, state")
# 表 1 里由**第 4 步**填的那几列。注意 n_touch 不在这里 —— 它是第 3 步填的；
# n_break 也不在 —— 它至今没有任何生产者（填充率 0%，口径待定 D-10）。
EVO_EVENT_COLS = "event, prev_event, on_line, since_event_idx, state"
EVO_EVENT_KEY = ("event", "prev_event", "on_line", "since_event_idx", "state")

_EVO_KEY = ("symbol", "interval_ms", "ts")
_EVO_UPDATE = ", ".join(
    "%s=excluded.%s" % (c.strip(), c.strip())
    for c in EVO_FACT_COLS.split(",") if c.strip() not in _EVO_KEY)
_EVO_NPLACE = len([c for c in EVO_FACT_COLS.split(",") if c.strip()])

# 表 2（kbar_reaction）的可写列。dwell_bars / nth_touch 也在里面 ——
# 它们是「第 3 步的口径还没定」而不是「别的步骤的字段」，所以留 NULL 写入即可。
RX_COLS = ("symbol, interval_ms, ts, line_key, line_kind, line_period_ms, "
           "line_price, dist_pct, side, pen_depth_pct, recover_pct, close_side, "
           "dwell_bars, vol_rel, taker_buy_ratio, nth_touch")
_RX_KEY = ("symbol", "interval_ms", "ts", "line_key")
_RX_UPDATE = ", ".join(
    "%s=excluded.%s" % (c.strip(), c.strip())
    for c in RX_COLS.split(",") if c.strip() not in _RX_KEY)
_RX_NPLACE = len([c for c in RX_COLS.split(",") if c.strip()])

# 表 2.5（kbar_event）的可写列。唯一键含 event ——
# 同一根同一根线上「突破」与「反包」可以同时存在，所以 event 必须进键。
EV_COLS = ("symbol, interval_ms, ts, idx, line_key, line_kind, line_period_ms, "
           "line_price, event, prev_event, since_event_idx, close_side, "
           "pen_depth_pct, vol_rel, taker_buy_ratio")
_EV_KEY = ("symbol", "interval_ms", "ts", "line_key", "event")
_EV_UPDATE = ", ".join(
    "%s=excluded.%s" % (c.strip(), c.strip())
    for c in EV_COLS.split(",") if c.strip() not in _EV_KEY)
_EV_NPLACE = len([c for c in EV_COLS.split(",") if c.strip()])

# 表 3（kbar_forward）的可写列。唯一键是 (symbol, interval_ms, ts) —— 每根一行。
# 注意：**不在这里出现的列**就别写，尤其不要有 line_key —— 表的粒度是「根」不是「根×线」。
FD_COLS = ("symbol, interval_ms, ts, ret1, ret3, ret5, held, "
           "held_line, held_ref_price, held_side")
_FD_KEY = ("symbol", "interval_ms", "ts")
_FD_UPDATE = ", ".join(
    "%s=excluded.%s" % (c.strip(), c.strip())
    for c in FD_COLS.split(",") if c.strip() not in _FD_KEY)
_FD_NPLACE = len([c for c in FD_COLS.split(",") if c.strip()])

# 表 5（kbar_shape）的可写列。唯一键是 (symbol, interval_ms, ts) —— 每根一行。
# `created_at` 不进列表：它由 DEFAULT 填，重跑时不该被改写。
SH_COLS = ("symbol, interval_ms, ts, label, states, n_states, side, pin_kind, "
           "ref_pin_ts, ref_pin_kind, ref_pin_side, rng, body_pct, up_wick_pct, "
           "dn_wick_pct, close_pos, vol_rel, taker_buy_ratio, n_recovered, "
           "recovered, evidence, params_hash")
_SH_KEY = ("symbol", "interval_ms", "ts")
_SH_UPDATE = ", ".join(
    "%s=excluded.%s" % (c.strip(), c.strip())
    for c in SH_COLS.split(",") if c.strip() not in _SH_KEY)
_SH_NPLACE = len([c for c in SH_COLS.split(",") if c.strip()])

# 表 6（kbar_verify）的可写列。唯一键是 (symbol, interval_ms, ts, horizon) ——
# 一根 K 线在不同 N 下各有一行，因为「后面 1 根」和「后面 3 根」是两个问题。
VF_COLS = ("symbol, interval_ms, ts, horizon, label, states, pred_kind, pred_text, "
           "verdict, ref_price, actual_price, diff_pts, n_bars, tol_pts, "
           "params_hash, verify_hash")
_VF_KEY = ("symbol", "interval_ms", "ts", "horizon")
_VF_UPDATE = ", ".join(
    "%s=excluded.%s" % (c.strip(), c.strip())
    for c in VF_COLS.split(",") if c.strip() not in _VF_KEY)
_VF_NPLACE = len([c for c in VF_COLS.split(",") if c.strip()])

# 表 6b（kbar_verify_survival）的可写列。唯一键是 (symbol, interval_ms, ts) ——
# 「活了多久」一根只有一个答案，不像表 6 要按 N 分行。
SV_COLS = ("symbol, interval_ms, ts, label, pred_kind, must_hold, status, "
           "break_n, n_seen, max_n, ref_price, actual_at_break, tol_pts, "
           "params_hash, survival_hash")
_SV_KEY = ("symbol", "interval_ms", "ts")
_SV_UPDATE = ", ".join(
    "%s=excluded.%s" % (c.strip(), c.strip())
    for c in SV_COLS.split(",") if c.strip() not in _SV_KEY)
_SV_NPLACE = len([c for c in SV_COLS.split(",") if c.strip()])

# 建表之后又加的列。CREATE TABLE IF NOT EXISTS 对已存在的表不会补列，
# 光改 SCHEMA 会让老库停在旧结构上（写新列直接报 no such column）。
# 这里做幂等的补列，不删不改，只 ALTER TABLE ADD COLUMN。
_ADDED_COLUMNS = (
    ("kbar_shape", "ref_pin_side", "TEXT"),
    # 表 8 第一版漏了主口径的计数列，只记了对照口径（n_pass_block）。
    # 结果是「登记了，但登记的不是主口径的数」—— 补列，旧行留 NULL
    # （缺口要看得见，不许编一个数填进去）。
    ("kbar_holdout_log", "n_pass_cluster", "INTEGER"),
)


class KBarStore:
    """kbar.db 的最小仓储。批量写、按 (symbol, interval_ms, ts, line_key) 幂等。"""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)
        self._migrate()
        self._con.commit()

    def _migrate(self) -> None:
        """给已存在的老库补上后来新增的列（幂等，只 ADD COLUMN）。"""
        for table, col, decl in _ADDED_COLUMNS:
            try:
                have = set(r[1] for r in
                           self._con.execute("PRAGMA table_info(%s)" % table))
            except sqlite3.Error:
                continue
            if not have:
                continue          # 表还不存在，SCHEMA 会建，不用补
            if col not in have:
                self._con.execute("ALTER TABLE %s ADD COLUMN %s %s"
                                  % (table, col, decl))

    @_synchronized
    def close(self) -> None:
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---------- 写入 ----------
    @_synchronized
    def upsert_lines(self, rows: Iterable[Dict]) -> int:
        """批量写观察线。行是 dict（lines.build_bar_lines 的产物）。"""
        payload = []
        for r in rows:
            payload.append((r["symbol"], int(r["interval_ms"]), int(r["ts"]),
                            r["line_key"], r["line_kind"], int(r["line_period_ms"]),
                            float(r["line_price"]),
                            None if r.get("bar_close") is None else float(r["bar_close"]),
                            None if r.get("dist_pct") is None else float(r["dist_pct"]),
                            int(r["src_ts"]), int(r["line_ready_ms"])))
        if not payload:
            return 0
        cur = self._con.executemany(
            "INSERT INTO kbar_line (" + COLS + ") VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, interval_ms, ts, line_key) DO UPDATE SET "
            "  line_kind=excluded.line_kind, line_period_ms=excluded.line_period_ms, "
            "  line_price=excluded.line_price, bar_close=excluded.bar_close, "
            "  dist_pct=excluded.dist_pct, src_ts=excluded.src_ts, "
            "  line_ready_ms=excluded.line_ready_ms",
            payload)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def delete_interval(self, symbol: str, interval_ms: int) -> int:
        """整段重建用（配合 --rebuild）。增量回填不要调它。"""
        cur = self._con.execute(
            "DELETE FROM kbar_line WHERE symbol = ? AND interval_ms = ?",
            (symbol, int(interval_ms)))
        self._con.commit()
        return cur.rowcount

    # ---------- 表 1：事实层 ----------
    @_synchronized
    def upsert_evolution(self, rows: Iterable[Dict]) -> int:
        """批量写表 1（事实层）。只更新第 2 步拥有的列，不碰第 3/4 步的列。"""
        payload = []
        for r in rows:
            payload.append(tuple(
                r.get(c.strip()) if c.strip() not in ("interval_ms", "ts", "idx")
                else int(r.get(c.strip()))
                for c in EVO_FACT_COLS.split(",")))
        if not payload:
            return 0
        cur = self._con.executemany(
            "INSERT INTO kbar_evolution (" + EVO_FACT_COLS + ") VALUES ("
            + ",".join("?" * _EVO_NPLACE) + ") "
            "ON CONFLICT(symbol, interval_ms, ts) DO UPDATE SET " + _EVO_UPDATE,
            payload)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def delete_evolution(self, symbol: str, interval_ms: int) -> int:
        """整段重算事实层（配合 --rebuild）。"""
        cur = self._con.execute(
            "DELETE FROM kbar_evolution WHERE symbol = ? AND interval_ms = ?",
            (symbol, int(interval_ms)))
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def evolution_rows(self, symbol: str, interval_ms: int,
                       ts: Optional[int] = None) -> List[Dict]:
        """读回表 1。探针用它打印真值，不信内存变量。"""
        sql = "SELECT * FROM kbar_evolution WHERE symbol = ? AND interval_ms = ?"
        args: List = [symbol, int(interval_ms)]
        if ts is not None:
            sql += " AND ts = ?"
            args.append(int(ts))
        sql += " ORDER BY ts"
        cur = self._con.execute(sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur]

    @_synchronized
    def evolution_counts(self, symbol: str, interval_ms: Optional[int] = None) -> Dict:
        """表 1 的行数、覆盖范围，以及**每个字段的填充率**。

        填充率是这一步的验收核心：`conc` 只有 3s 覆盖窗内才有值，
        `n_touch`/`event` 要等第 3/4 步。不报这个，就等于默认全填上了。
        """
        sql = ("SELECT COUNT(*), MIN(ts), MAX(ts) FROM kbar_evolution WHERE symbol = ?")
        args: List = [symbol]
        if interval_ms is not None:
            sql += " AND interval_ms = ?"
            args.append(int(interval_ms))
        n, lo, hi = self._con.execute(sql, args).fetchone()
        fill = {}
        for col in ("range_ts_h", "zone", "vol_rel", "vol_slot_rel", "taker_buy_ratio",
                    "conc", "path_shape", "max_tick_share", "t_low_min", "n_lines",
                    "n_touch", "n_break", "event", "state"):
            sql2 = ("SELECT COUNT(%s) FROM kbar_evolution WHERE symbol = ?" % col)
            a2: List = [symbol]
            if interval_ms is not None:
                sql2 += " AND interval_ms = ?"
                a2.append(int(interval_ms))
            fill[col] = self._con.execute(sql2, a2).fetchone()[0]
        return {"bars": n, "first_ts": lo, "last_ts": hi, "filled": fill}

    # ---------- 表 2：反应层 ----------
    @_synchronized
    def upsert_reactions(self, rows: Iterable[Dict]) -> int:
        """批量写表 2。行里的内部字段（_line_ready_ms 等）不会进库。"""
        # 这几列必须转 int；dwell_bars / nth_touch 允许 None（口径未定，故意留空）
        must_int = ("interval_ms", "ts", "line_period_ms")
        maybe_int = ("dwell_bars", "nth_touch")
        payload = []
        for r in rows:
            vals = []
            for c in RX_COLS.split(","):
                k = c.strip()
                v = r.get(k)
                if k in must_int:
                    vals.append(int(v))
                elif k in maybe_int:
                    vals.append(None if v is None else int(v))
                else:
                    vals.append(v)
            payload.append(tuple(vals))
        if not payload:
            return 0
        cur = self._con.executemany(
            "INSERT INTO kbar_reaction (" + RX_COLS + ") VALUES ("
            + ",".join("?" * _RX_NPLACE) + ") "
            "ON CONFLICT(symbol, interval_ms, ts, line_key) DO UPDATE SET " + _RX_UPDATE,
            payload)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def delete_reactions(self, symbol: str, interval_ms: int) -> int:
        cur = self._con.execute(
            "DELETE FROM kbar_reaction WHERE symbol = ? AND interval_ms = ?",
            (symbol, int(interval_ms)))
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def reactions(self, symbol: str, interval_ms: int,
                  ts: Optional[int] = None, line_key: Optional[str] = None,
                  limit: Optional[int] = None) -> List[Dict]:
        sql = "SELECT * FROM kbar_reaction WHERE symbol = ? AND interval_ms = ?"
        args: List = [symbol, int(interval_ms)]
        if ts is not None:
            sql += " AND ts = ?"
            args.append(int(ts))
        if line_key is not None:
            sql += " AND line_key = ?"
            args.append(line_key)
        sql += " ORDER BY ts, line_key"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        cur = self._con.execute(sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur]

    @_synchronized
    def reaction_counts(self, symbol: str, interval_ms: Optional[int] = None) -> Dict:
        """表 2 的行数、覆盖的根数、按线种类分布、以及各字段填充率。"""
        sql = "SELECT COUNT(*), COUNT(DISTINCT ts), MIN(ts), MAX(ts) FROM kbar_reaction WHERE symbol = ?"
        args: List = [symbol]
        if interval_ms is not None:
            sql += " AND interval_ms = ?"
            args.append(int(interval_ms))
        n, nbars, lo, hi = self._con.execute(sql, args).fetchone()
        by_kind = dict(self._con.execute(
            "SELECT line_kind, COUNT(*) FROM kbar_reaction WHERE symbol = ?"
            + (" AND interval_ms = ?" if interval_ms is not None else "")
            + " GROUP BY line_kind ORDER BY line_kind", args))
        fill = {}
        for col in ("side", "pen_depth_pct", "recover_pct", "close_side",
                    "dwell_bars", "vol_rel", "taker_buy_ratio", "nth_touch"):
            sql2 = "SELECT COUNT(%s) FROM kbar_reaction WHERE symbol = ?" % col
            a2: List = [symbol]
            if interval_ms is not None:
                sql2 += " AND interval_ms = ?"
                a2.append(int(interval_ms))
            fill[col] = self._con.execute(sql2, a2).fetchone()[0]
        return {"rows": n, "bars": nbars, "first_ts": lo, "last_ts": hi,
                "by_kind": by_kind, "filled": fill}

    @_synchronized
    def line_series(self, symbol: str, interval_ms: int) -> Dict[str, Dict[int, Tuple]]:
        """把表 0 整理成反应层要的形状：{line_key: {bar_ts: (price, ready, src_ts)}}。

        为什么按 **bar_ts** 索引而不是按 ready 排成序列：反应只能用「上一根那一行」。
        表 0 对每条存在的线在每根 K 线上都无条件落行，所以某根没有行 ⟺ 那根这条线
        不存在（区间演化转 PRE 时四线全空）。按 ready 排成序列再「取最近就绪值」
        会把一条已经消失的线无限期往前带 —— 实测 2026-08-05 01:00 转 PRE，
        而 05:15 的 K 线仍在拿 02:00 那条线做反应。
        """
        out: Dict[str, Dict[int, Tuple]] = {}
        for key, ts, price, ready, src_ts in self._con.execute(
                "SELECT line_key, ts, line_price, line_ready_ms, src_ts FROM kbar_line "
                "WHERE symbol = ? AND interval_ms = ? ORDER BY line_key, ts",
                (symbol, int(interval_ms))):
            out.setdefault(key, {})[int(ts)] = (float(price), int(ready), int(src_ts))
        return out

    @_synchronized
    def line_meta(self, symbol: str, interval_ms: int) -> Dict[str, Tuple[str, int]]:
        """{line_key: (line_kind, line_period_ms)}，从表 0 取一次即可。"""
        return {k: (kind, int(per)) for k, kind, per in self._con.execute(
            "SELECT DISTINCT line_key, line_kind, line_period_ms FROM kbar_line "
            "WHERE symbol = ? AND interval_ms = ?", (symbol, int(interval_ms)))}

    @_synchronized
    def set_n_touch(self, symbol: str, interval_ms: int,
                    counts: Dict[int, int]) -> int:
        """把反应层数出来的「每根碰到几条」写回表 1 的 n_touch。

        为什么从反应层回填而不是第 2 步自己算：n_touch 的定义就是「表 2 里有几行」，
        两处各算一遍迟早对不上，而对不上时没人会发现。
        """
        if not counts:
            return 0
        payload = [(int(n), symbol, int(interval_ms), int(ts))
                   for ts, n in counts.items()]
        cur = self._con.executemany(
            "UPDATE kbar_evolution SET n_touch = ? "
            "WHERE symbol = ? AND interval_ms = ? AND ts = ?", payload)
        self._con.commit()
        return cur.rowcount

    # ---------- 第 4 步：事件层 ----------
    @_synchronized
    def upsert_events(self, rows: Iterable[Dict]) -> int:
        """批量写表 2.5（kbar_event）。每个 (根, 线, 事件) 一行。"""
        must_int = ("interval_ms", "ts", "idx", "line_period_ms")
        maybe_int = ("since_event_idx",)
        payload = []
        for r in rows:
            vals = []
            for c in EV_COLS.split(","):
                k = c.strip()
                v = r.get(k)
                if k in must_int:
                    vals.append(int(v))
                elif k in maybe_int:
                    vals.append(None if v is None else int(v))
                else:
                    vals.append(v)
            payload.append(tuple(vals))
        if not payload:
            return 0
        cur = self._con.executemany(
            "INSERT INTO kbar_event (" + EV_COLS + ") VALUES ("
            + ",".join("?" * _EV_NPLACE) + ") "
            "ON CONFLICT(symbol, interval_ms, ts, line_key, event) DO UPDATE SET "
            + _EV_UPDATE,
            payload)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def delete_events(self, symbol: str, interval_ms: int) -> int:
        cur = self._con.execute(
            "DELETE FROM kbar_event WHERE symbol = ? AND interval_ms = ?",
            (symbol, int(interval_ms)))
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def events(self, symbol: str, interval_ms: int, ts: Optional[int] = None,
               line_key: Optional[str] = None, event: Optional[str] = None,
               limit: Optional[int] = None) -> List[Dict]:
        sql = "SELECT * FROM kbar_event WHERE symbol = ? AND interval_ms = ?"
        args: List = [symbol, int(interval_ms)]
        if ts is not None:
            sql += " AND ts = ?"
            args.append(int(ts))
        if line_key is not None:
            sql += " AND line_key = ?"
            args.append(line_key)
        if event is not None:
            sql += " AND event = ?"
            args.append(event)
        sql += " ORDER BY ts, line_key, event"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        cur = self._con.execute(sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur]

    @_synchronized
    def event_counts(self, symbol: str, interval_ms: Optional[int] = None) -> Dict:
        """表 2.5 的规模、事件分布、以及表 1 里 event/state 的填充率。"""
        if interval_ms is None:
            where = "symbol = ?"
            args: List = [symbol]
        else:
            where = "symbol = ? AND interval_ms = ?"
            args = [symbol, int(interval_ms)]
        n, bars = self._con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT ts) FROM kbar_event WHERE " + where,
            args).fetchone()
        by_event = dict(self._con.execute(
            "SELECT event, COUNT(*) FROM kbar_event WHERE " + where
            + " GROUP BY event ORDER BY COUNT(*) DESC", args))
        by_kind = dict(self._con.execute(
            "SELECT line_kind, COUNT(*) FROM kbar_event WHERE " + where
            + " GROUP BY line_kind ORDER BY COUNT(*) DESC", args))
        out = {"rows": int(n or 0), "bars": int(bars or 0),
               "by_event": by_event, "by_line_kind": by_kind}
        # 表 1 里那几列的填充率（从表 1 读，不从内存变量算）
        fw = where.replace("kbar_event", "kbar_evolution")
        evo = self._con.execute(
            "SELECT COUNT(*), SUM(event IS NOT NULL), SUM(state = 'neutral'), "
            "SUM(state IS NOT NULL) FROM kbar_evolution WHERE " + fw, args).fetchone()
        out["evolution"] = {
            "bars": int(evo[0] or 0),
            "with_event": int(evo[1] or 0),
            "neutral": int(evo[2] or 0),
            "with_state": int(evo[3] or 0),
        }
        return out

    @_synchronized
    def set_bar_events(self, symbol: str, interval_ms: int,
                       by_ts: Dict[int, Dict]) -> int:
        """把事件层汇总出来的那几列写回表 1（每根一行）。

        只动 `EVO_EVENT_COLS` 那五列，不碰第 2 步的事实列和第 3 步的 n_touch ——
        回填第 4 步不该把前两步的成果抹掉（变异体 M17 守的就是这条）。

        列名由常量生成而不是手写：手写一份 SQL 列名 + 一个常量，两者迟早漂移，
        而漂移的表现是「写进了别的列」或「漏写一列」，都不报错。
        """
        if not by_ts:
            return 0
        cols = [c.strip() for c in EVO_EVENT_COLS.split(",")]
        assert tuple(cols) == EVO_EVENT_KEY, "EVO_EVENT_COLS 与 EVO_EVENT_KEY 不一致"
        payload = []
        for ts, v in by_ts.items():
            vals = []
            for c in cols:
                val = v.get(c)
                if c == "since_event_idx":
                    vals.append(None if val is None else int(val))
                else:
                    vals.append(val)
            vals.extend([symbol, int(interval_ms), int(ts)])
            payload.append(tuple(vals))
        cur = self._con.executemany(
            "UPDATE kbar_evolution SET "
            + ", ".join("%s = ?" % c for c in cols)
            + " WHERE symbol = ? AND interval_ms = ? AND ts = ?", payload)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def count(self, table: str, symbol: str,
              interval_ms: Optional[int] = None) -> int:
        """数某张表有多少行。只允许五张已知的表名（不做字符串拼接兜底）。"""
        if table not in ("kbar_line", "kbar_evolution", "kbar_reaction",
                         "kbar_event", "kbar_forward", "kbar_shape",
                         "kbar_verify"):
            raise ValueError("不认识的表：%s" % table)
        sql = "SELECT COUNT(*) FROM %s WHERE symbol = ?" % table
        args: List = [symbol]
        if interval_ms is not None:
            sql += " AND interval_ms = ?"
            args.append(int(interval_ms))
        return int(self._con.execute(sql, args).fetchone()[0])

    @_synchronized
    def touch_counts_by_bar(self, symbol: str, interval_ms: int) -> Dict[int, int]:
        """{ts: 这根 K 线碰到几条线}，直接数表 2。

        事件层的 neutral 判定要用它（「没碰到任何观察线」是无阈值的那一半）。
        数的时候从表 2 数，**不重算** —— n_touch 的定义就是「表 2 里有几行」。
        """
        return {int(ts): int(n) for ts, n in self._con.execute(
            "SELECT ts, COUNT(*) FROM kbar_reaction WHERE symbol = ? AND interval_ms = ? "
            "GROUP BY ts", (symbol, int(interval_ms)))}

    # ---------- 第 5 步：事后层 ----------
    #
    # ⚠ 这组方法是**事后**的。当下判定路径（lines / facts / reaction / event，
    #   以及它们对应的 build_and_persist_*）不许调用它们 —— 一旦调了，
    #   回测就偷未来，而且不会报错。tests/test_kbar_forward.py 里有一条测试
    #   专门扫这个（`test_current_judgement_path_never_imports_forward`）。

    @_synchronized
    def upsert_forward(self, rows: Iterable[Dict]) -> int:
        """批量写表 3。每根一行，按 (symbol, interval_ms, ts) 幂等。"""
        rows = list(rows)
        if not rows:
            return 0
        cols = [c.strip() for c in FD_COLS.split(",")]
        payload = []
        for r in rows:
            vals = []
            for c in cols:
                v = r.get(c)
                if c in ("interval_ms", "ts", "held") and v is not None:
                    v = int(v)
                elif c in ("ret1", "ret3", "ret5", "held_ref_price") and v is not None:
                    v = float(v)
                vals.append(v)
            payload.append(tuple(vals))
        sql = ("INSERT INTO kbar_forward (%s) VALUES (%s) "
               "ON CONFLICT(symbol, interval_ms, ts) DO UPDATE SET %s"
               % (FD_COLS, ", ".join("?" * len(cols)), _FD_UPDATE))
        self._con.executemany(sql, payload)
        self._con.commit()
        return len(payload)

    @_synchronized
    def delete_forward(self, symbol: str, interval_ms: int) -> int:
        """整段重算事后层（配合 --rebuild）。"""
        cur = self._con.execute(
            "DELETE FROM kbar_forward WHERE symbol = ? AND interval_ms = ?",
            (symbol, int(interval_ms)))
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def forward_rows(self, symbol: str, interval_ms: int,
                     ts: Optional[int] = None) -> List[Dict]:
        """读回表 3。探针用它打印真值，不信内存变量。"""
        sql = "SELECT * FROM kbar_forward WHERE symbol = ? AND interval_ms = ?"
        args: List = [symbol, int(interval_ms)]
        if ts is not None:
            sql += " AND ts = ?"
            args.append(int(ts))
        sql += " ORDER BY ts"
        cur = self._con.execute(sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur]

    @_synchronized
    def forward_counts(self, symbol: str, interval_ms: Optional[int] = None) -> Dict:
        """表 3 的行数、覆盖范围、以及每个字段的填充率。"""
        sql = "SELECT COUNT(*), MIN(ts), MAX(ts) FROM kbar_forward WHERE symbol = ?"
        args: List = [symbol]
        if interval_ms is not None:
            sql += " AND interval_ms = ?"
            args.append(int(interval_ms))
        n, lo, hi = self._con.execute(sql, args).fetchone()
        out: Dict = {"rows": int(n or 0), "first_ts": lo, "last_ts": hi}
        for col in ("ret1", "ret3", "ret5", "held", "held_line", "held_ref_price"):
            sql2 = "SELECT COUNT(%s) FROM kbar_forward WHERE symbol = ?" % col
            a2: List = [symbol]
            if interval_ms is not None:
                sql2 += " AND interval_ms = ?"
                a2.append(int(interval_ms))
            out[col] = int(self._con.execute(sql2, a2).fetchone()[0] or 0)
        # held 的三态分布
        sql3 = ("SELECT held, COUNT(*) FROM kbar_forward WHERE symbol = ?"
                + (" AND interval_ms = ?" if interval_ms is not None else "")
                + " GROUP BY held")
        a3: List = [symbol]
        if interval_ms is not None:
            a3.append(int(interval_ms))
        out["by_held"] = {("null" if h is None else int(h)): int(c)
                          for h, c in self._con.execute(sql3, a3)}
        return out

    # ---------- 表 5：读数层（判断落库） ----------
    #
    # 用户 2026-11-08 的要求：「所有演化结论字段或者描述都要入 db 库」。
    # 这组方法只做读写，**判定逻辑不在这里** —— 在 shape.py（纯函数、无 IO）。
    # 分开的理由：判定要能被单测和探针独立复算，落库只是持久化。

    @_synchronized
    def upsert_shape(self, rows: Iterable[Dict]) -> int:
        """批量写表 5。唯一键 (symbol, interval_ms, ts)，重跑幂等。"""
        sql = ("INSERT INTO kbar_shape (" + SH_COLS + ") VALUES ("
               + ",".join("?" * _SH_NPLACE) + ") ON CONFLICT(symbol, interval_ms, ts) "
               "DO UPDATE SET " + _SH_UPDATE)
        buf = [tuple(r.get(c.strip()) for c in SH_COLS.split(",")) for r in rows]
        if not buf:
            return 0
        self._con.executemany(sql, buf)
        self._con.commit()
        return len(buf)

    @_synchronized
    def delete_shape(self, symbol: str, interval_ms: int) -> int:
        cur = self._con.execute(
            "DELETE FROM kbar_shape WHERE symbol = ? AND interval_ms = ?",
            (symbol, int(interval_ms)))
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def shape_rows(self, symbol: str, interval_ms: int,
                   ts: Optional[int] = None, label: Optional[str] = None,
                   limit: Optional[int] = None) -> List[Dict]:
        """读判定。`ts` 给了就是那一根的结论。"""
        sql = "SELECT * FROM kbar_shape WHERE symbol = ? AND interval_ms = ?"
        args: List = [symbol, int(interval_ms)]
        if ts is not None:
            sql += " AND ts = ?"
            args.append(int(ts))
        if label is not None:
            sql += " AND label = ?"
            args.append(label)
        sql += " ORDER BY ts"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        cur = self._con.execute(sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur]

    @_synchronized
    def shape_counts(self, symbol: str, interval_ms: Optional[int] = None) -> Dict:
        """行数、时间范围、label 分布、以及**表里到底有几种 params_hash**。

        `params_hashes` 是这个方法最要紧的返回值：出现一个以上就说明表里
        混着两套阈值的结论，那种表拿去用就是自欺。
        """
        where = " WHERE symbol = ?"
        args: List = [symbol]
        if interval_ms is not None:
            where += " AND interval_ms = ?"
            args.append(int(interval_ms))
        n, lo, hi = self._con.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM kbar_shape" + where,
            args).fetchone()
        by_label = dict(self._con.execute(
            "SELECT label, COUNT(*) FROM kbar_shape" + where
            + " GROUP BY label ORDER BY COUNT(*) DESC", args))
        hashes = [h for (h,) in self._con.execute(
            "SELECT DISTINCT params_hash FROM kbar_shape" + where, args)]
        return {"rows": int(n or 0), "first_ts": lo, "last_ts": hi,
                "by_label": by_label, "params_hashes": hashes}

    # ---------- 表 6：佐证层（用后面的 K 线验当时的判断） ----------
    #
    # 这组方法读写的是**事后**结论，与 shape.py 严格分开：
    # shape.py 不许 import verify，本组只做持久化。

    @_synchronized
    def upsert_verify(self, rows: Iterable[Dict]) -> int:
        buf = [tuple(r.get(c.strip()) for c in VF_COLS.split(",")) for r in rows]
        if not buf:
            return 0
        sql = ("INSERT INTO kbar_verify (" + VF_COLS + ") VALUES ("
               + ",".join("?" * _VF_NPLACE) + ") "
               "ON CONFLICT(symbol, interval_ms, ts, horizon) "
               "DO UPDATE SET " + _VF_UPDATE)
        self._con.executemany(sql, buf)
        self._con.commit()
        return len(buf)

    @_synchronized
    def delete_verify(self, symbol: str, interval_ms: int,
                      horizon: Optional[int] = None,
                      since_ts: Optional[int] = None) -> int:
        """删表 6 的行。`since_ts` 给了就只删这段 —— 增量更新靠它。"""
        sql = "DELETE FROM kbar_verify WHERE symbol = ? AND interval_ms = ?"
        args: List = [symbol, int(interval_ms)]
        if horizon is not None:
            sql += " AND horizon = ?"
            args.append(int(horizon))
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        cur = self._con.execute(sql, args)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def count_rows(self, table: str, symbol: str, interval_ms: int,
                   since_ts: Optional[int] = None,
                   older_than_ts: Optional[int] = None) -> int:
        """数某张表的行数，可按 ts 卡一段。表名走白名单，防注入。

        增量更新要用它算「没动的旧行 + 本次重算的行 = 全量」，否则
        「只重算尾部」很容易悄悄漏掉中间一段还看不出来。
        """
        if table not in ("kbar_verify", "kbar_verify_survival", "kbar_shape",
                         "kbar_dims", "kbar_evolution"):
            raise ValueError("不认的表名 %r" % (table,))
        sql = "SELECT COUNT(*) FROM %s WHERE symbol = ? AND interval_ms = ?" % table
        args: List = [symbol, int(interval_ms)]
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        if older_than_ts is not None:
            sql += " AND ts < ?"
            args.append(int(older_than_ts))
        return int(self._con.execute(sql, args).fetchone()[0])

    @_synchronized
    def verify_rows(self, symbol: str, interval_ms: int,
                    ts: Optional[int] = None, horizon: Optional[int] = None,
                    verdict: Optional[str] = None,
                    limit: Optional[int] = None) -> List[Dict]:
        sql = "SELECT * FROM kbar_verify WHERE symbol = ? AND interval_ms = ?"
        args: List = [symbol, int(interval_ms)]
        if ts is not None:
            sql += " AND ts = ?"
            args.append(int(ts))
        if horizon is not None:
            sql += " AND horizon = ?"
            args.append(int(horizon))
        if verdict is not None:
            sql += " AND verdict = ?"
            args.append(verdict)
        sql += " ORDER BY ts, horizon"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        cur = self._con.execute(sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur]

    @_synchronized
    def verify_counts(self, symbol: str,
                      interval_ms: Optional[int] = None) -> Dict:
        """按 horizon 与 verdict 的交叉计数，以及表里有几套口径指纹。"""
        where = " WHERE symbol = ?"
        args: List = [symbol]
        if interval_ms is not None:
            where += " AND interval_ms = ?"
            args.append(int(interval_ms))
        n = self._con.execute("SELECT COUNT(*) FROM kbar_verify" + where,
                              args).fetchone()[0]
        cross = {}
        for h, v, c in self._con.execute(
                "SELECT horizon, verdict, COUNT(*) FROM kbar_verify" + where
                + " GROUP BY horizon, verdict ORDER BY horizon", args):
            cross.setdefault(int(h), {})[v] = int(c)
        hashes = [h for (h,) in self._con.execute(
            "SELECT DISTINCT verify_hash FROM kbar_verify" + where, args)]
        return {"rows": int(n or 0), "by_horizon": cross,
                "verify_hashes": hashes}

    # ---------- 表 6b：存活层（一句话活了多久） ----------
    @_synchronized
    def upsert_survival(self, rows: Iterable[Dict]) -> int:
        buf = [tuple(r.get(c.strip()) for c in SV_COLS.split(",")) for r in rows]
        if not buf:
            return 0
        sql = ("INSERT INTO kbar_verify_survival (" + SV_COLS + ") VALUES ("
               + ",".join("?" * _SV_NPLACE) + ") "
               "ON CONFLICT(symbol, interval_ms, ts) DO UPDATE SET "
               + _SV_UPDATE)
        self._con.executemany(sql, buf)
        self._con.commit()
        return len(buf)

    @_synchronized
    def delete_survival(self, symbol: str, interval_ms: int,
                        since_ts: Optional[int] = None) -> int:
        """删存活行。`since_ts` 给了就只删那段 —— 增量更新靠的就是它。"""
        sql = ("DELETE FROM kbar_verify_survival "
               "WHERE symbol = ? AND interval_ms = ?")
        args: List = [symbol, int(interval_ms)]
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        cur = self._con.execute(sql, args)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def survival_rows(self, symbol: str, interval_ms: int,
                      ts: Optional[int] = None,
                      status: Optional[str] = None) -> List[Dict]:
        sql = ("SELECT * FROM kbar_verify_survival "
               "WHERE symbol = ? AND interval_ms = ?")
        args: List = [symbol, int(interval_ms)]
        if ts is not None:
            sql += " AND ts = ?"
            args.append(int(ts))
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY ts"
        cur = self._con.execute(sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur]

    @_synchronized
    def survival_counts(self, symbol: str,
                        interval_ms: Optional[int] = None) -> Dict:
        """三态计数 + 指纹。**只有「已破」「活到最后」算定型。**

        「未成熟」是表尾还没看完的，把它算进分母就是把没验过的当验过了。
        """
        where = " WHERE symbol = ?"
        args: List = [symbol]
        if interval_ms is not None:
            where += " AND interval_ms = ?"
            args.append(int(interval_ms))
        n = self._con.execute(
            "SELECT COUNT(*) FROM kbar_verify_survival" + where,
            args).fetchone()[0]
        by_status = dict(self._con.execute(
            "SELECT status, COUNT(*) FROM kbar_verify_survival" + where
            + " GROUP BY status", args))
        by_kind = dict(self._con.execute(
            "SELECT pred_kind, COUNT(*) FROM kbar_verify_survival" + where
            + " GROUP BY pred_kind ORDER BY COUNT(*) DESC", args))
        hashes = [h for (h,) in self._con.execute(
            "SELECT DISTINCT survival_hash FROM kbar_verify_survival" + where,
            args)]
        settled = (by_status.get("已破", 0) + by_status.get("活到最后", 0))
        return {"rows": int(n or 0), "by_status": by_status,
                "by_pred_kind": by_kind,
                "settled": int(settled),
                "pending": int(by_status.get("未成熟", 0)),
                "survival_hashes": hashes}

    # ---------- 表 7：维度层（环境快照，不参与判定） ----------
    @_synchronized
    def upsert_dims(self, rows: Iterable[Dict]) -> int:
        cols = ("symbol, interval_ms, ts, label, n_dims, n_filled, "
                "values_json, buckets_json, notes_json, dims_hash")
        nplace = len([c for c in cols.split(",")])
        upd = ", ".join("%s = excluded.%s" % (c.strip(), c.strip())
                        for c in cols.split(",")
                        if c.strip() not in ("symbol", "interval_ms", "ts"))
        buf = [tuple(r.get(c.strip()) for c in cols.split(",")) for r in rows]
        if not buf:
            return 0
        sql = ("INSERT INTO kbar_dims (" + cols + ") VALUES ("
               + ",".join("?" * nplace) + ") "
               "ON CONFLICT(symbol, interval_ms, ts) DO UPDATE SET " + upd)
        self._con.executemany(sql, buf)
        self._con.commit()
        return len(buf)

    @_synchronized
    def delete_dims(self, symbol: str, interval_ms: int,
                    since_ts: Optional[int] = None) -> int:
        sql = "DELETE FROM kbar_dims WHERE symbol = ? AND interval_ms = ?"
        args: List = [symbol, int(interval_ms)]
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        cur = self._con.execute(sql, args)
        self._con.commit()
        return cur.rowcount

    @_synchronized
    def dims_rows(self, symbol: str, interval_ms: int,
                  ts: Optional[int] = None) -> List[Dict]:
        sql = "SELECT * FROM kbar_dims WHERE symbol = ? AND interval_ms = ?"
        args: List = [symbol, int(interval_ms)]
        if ts is not None:
            sql += " AND ts = ?"
            args.append(int(ts))
        sql += " ORDER BY ts"
        cur = self._con.execute(sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur]

    @_synchronized
    def dims_counts(self, symbol: str,
                    interval_ms: Optional[int] = None) -> Dict:
        """行数 / 指纹 / 「有值维度」的分布。

        `n_filled` 是每根实际算出几个维度 —— 这个数字最能说明「这一层是不是
        真接线了」：如果全是 0，等于表建了但没算法填。
        """
        where = " WHERE symbol = ?"
        args: List = [symbol]
        if interval_ms is not None:
            where += " AND interval_ms = ?"
            args.append(int(interval_ms))
        n = self._con.execute("SELECT COUNT(*) FROM kbar_dims" + where,
                              args).fetchone()[0]
        hashes = [h for (h,) in self._con.execute(
            "SELECT DISTINCT dims_hash FROM kbar_dims" + where, args)]
        fill = self._con.execute(
            "SELECT MIN(n_filled), MAX(n_filled), AVG(n_filled) "
            "FROM kbar_dims" + where, args).fetchone()
        return {"rows": int(n or 0), "dims_hashes": hashes,
                "filled_min": fill[0], "filled_max": fill[1],
                "filled_avg": (round(float(fill[2]), 2)
                               if fill[2] is not None else None)}

    # ---------- 表 8：留出集评测预登记 ----------
    @_synchronized
    def add_holdout_log(self, rec: Dict) -> int:
        """记一次留出集评测。**只增不改** —— 撤回一条就是抹掉一次尝试。"""
        cols = ("symbol, interval_ms, split_version, split_hash, dims_hash, "
                "train_n, holdout_n, embargo_n, n_tested, n_pass_cluster, "
                "n_pass_block, n_pass_naive, n_expected, z_bonferroni, "
                "n_pass_bonf, note")
        cs = [c.strip() for c in cols.split(",")]
        self._con.execute(
            "INSERT INTO kbar_holdout_log (" + cols + ") VALUES ("
            + ",".join("?" * len(cs)) + ")",
            tuple(rec.get(c) for c in cs))
        self._con.commit()
        return int(self._con.execute(
            "SELECT last_insert_rowid()").fetchone()[0])

    @_synchronized
    def holdout_log(self, symbol: str,
                    interval_ms: Optional[int] = None) -> List[Dict]:
        sql = "SELECT * FROM kbar_holdout_log WHERE symbol = ?"
        args: List = [symbol]
        if interval_ms is not None:
            sql += " AND interval_ms = ?"
            args.append(int(interval_ms))
        sql += " ORDER BY id"
        cur = self._con.execute(sql, args)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur]

    @_synchronized
    def holdout_run_count(self, symbol: str, interval_ms: int,
                          split_hash: str) -> int:
        """这个切分已经被评测过几次。≥1 就说明留出集已经脏了。"""
        return int(self._con.execute(
            "SELECT COUNT(*) FROM kbar_holdout_log "
            "WHERE symbol=? AND interval_ms=? AND split_hash=?",
            (symbol, int(interval_ms), split_hash)).fetchone()[0])

    @_synchronized
    def set_meta(self, key: str, value) -> None:
        self._con.execute(
            "INSERT INTO kbar_meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (key, value if isinstance(value, str) else json.dumps(value,
                                                                  ensure_ascii=False,
                                                                  sort_keys=True)))
        self._con.commit()

    # ---------- 读取 ----------
    @_synchronized
    def get_meta(self, key: str) -> Optional[str]:
        row = self._con.execute("SELECT value FROM kbar_meta WHERE key = ?",
                                (key,)).fetchone()
        return row[0] if row else None

    @_synchronized
    def lines(self, symbol: str, interval_ms: int, ts: Optional[int] = None,
              line_key: Optional[str] = None,
              limit: Optional[int] = None) -> List[Dict]:
        """按需读线。ts 给了就是「那一根上的所有线」。"""
        sql = ("SELECT symbol, interval_ms, ts, line_key, line_kind, line_period_ms, "
               "line_price, bar_close, dist_pct, src_ts, line_ready_ms "
               "FROM kbar_line WHERE symbol = ? AND interval_ms = ?")
        args: List = [symbol, int(interval_ms)]
        if ts is not None:
            sql += " AND ts = ?"
            args.append(int(ts))
        if line_key is not None:
            sql += " AND line_key = ?"
            args.append(line_key)
        sql += " ORDER BY ts, line_key"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        keys = ("symbol", "interval_ms", "ts", "line_key", "line_kind",
                "line_period_ms", "line_price", "bar_close", "dist_pct",
                "src_ts", "line_ready_ms")
        return [dict(zip(keys, r)) for r in self._con.execute(sql, args)]

    @_synchronized
    def counts(self, symbol: str, interval_ms: Optional[int] = None) -> Dict:
        """行数与覆盖范围。回填的「落库行数」就报这个，别靠调用方自己数。"""
        sql = ("SELECT COUNT(*), COUNT(DISTINCT ts), MIN(ts), MAX(ts) "
               "FROM kbar_line WHERE symbol = ?")
        args: List = [symbol]
        if interval_ms is not None:
            sql += " AND interval_ms = ?"
            args.append(int(interval_ms))
        n, nbars, lo, hi = self._con.execute(sql, args).fetchone()
        by_kind = dict(self._con.execute(
            "SELECT line_kind, COUNT(*) FROM kbar_line "
            "WHERE symbol = ?" + (" AND interval_ms = ?" if interval_ms is not None else "") +
            " GROUP BY line_kind ORDER BY line_kind", args))
        return {"lines": n, "bars": nbars, "first_ts": lo, "last_ts": hi,
                "by_kind": by_kind}

    @_synchronized
    def max_ready_ms(self, symbol: str, interval_ms: int) -> Optional[int]:
        """已落库线的最大 ready 时刻。续跑时用它判断从哪里接着算。"""
        row = self._con.execute(
            "SELECT MAX(line_ready_ms) FROM kbar_line "
            "WHERE symbol = ? AND interval_ms = ?",
            (symbol, int(interval_ms))).fetchone()
        return row[0] if row else None

    @_synchronized
    def line_counts_by_ts(self, symbol: str, interval_ms: int) -> Dict[int, int]:
        """每根 K 上有几条观察线（第 2 步填 n_lines 用）。

        为什么从库里数而不是传参数进来：两张表必须用**同一个基准**对齐。
        传参数的话，一旦调用方算的线和库里的不一致，n_lines 就会和 kbar_line
        对不上，而且不会报错。
        """
        return {int(ts): int(n) for ts, n in self._con.execute(
            "SELECT ts, COUNT(*) FROM kbar_line "
            "WHERE symbol = ? AND interval_ms = ? GROUP BY ts",
            (symbol, int(interval_ms)))}
