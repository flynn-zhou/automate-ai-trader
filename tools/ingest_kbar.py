#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K 线演化 · 增量编排器（把「库里长到哪了」接到「重新算到哪」）。

## 这个工具补的是哪个洞

2026-11-09 审计列出的第 3 个生产缺口：

    没有增量编排器。`build.update_verify_tail()` 只刷表 6/6b 的**最后 12 根**，
    **不摄新 bar、不重算其他层**。所以「库里多了一批新 K 线」这件事，
    不会自己传导到演化层 —— 得有人手工按顺序跑 9 个 `backfill_kbar_*.py`。

后果不是报错，是**看起来正常**：看板上凤蜡烛一直在动，但结论色带停在
某一天。这种"静默停更"比报错难发现得多。

## 设计取舍

**1. 只认「库里已有的 bar」，不自己联网。**
   取数（币安/回填）是另一件事，混进来会让"到底哪一步慢/错"分不清。
   要新数据先把 market.db 喂满，再跑这个。

**2. 逐层往下，任一层报错就停。**
   演化层是链式依赖（表1 依赖表0，表2 依赖 0+1…）。跳过一层继续跑，
   下游会拿着旧数据算出"看起来合理"的新结论 —— 那是最坏的情况。

**3. 增量优先，但**不假装**全增量。**
   有 `since_ts` 的层（verify / survival / dims）就传增量；
   只有 `rebuild` 参数的层（reactions / events）会整表重算 —— 慢，但正确。
   工具在报告里**明确写出每一层是增量还是全量**，不让人误以为整条链都是增量。

**4. 来源登记。**
   每层跑完写一条 `source:<sym>:<label>:<layer>` 到 kbar_meta
   （见 `build.record_source`）。这样下次有人问"这段数据哪来的"，
   库自己能回答，而不是对着 kbar.db 猜。

## 用法

    # 看看会做什么（不写库）
    python tools/ingest_kbar.py --interval-ms 900000 --dry-run

    # 真跑
    python tools/ingest_kbar.py --interval-ms 900000

    # 5m：market.db 没有原生 5m，从 1m 聚合
    python tools/ingest_kbar.py --interval-ms 300000 --from-1m

    # 只跑前几层（调试用）
    python tools/ingest_kbar.py --interval-ms 900000 --until shape
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B            # noqa: E402
from ai_trader.kbar import facts as F           # noqa: E402
from ai_trader.kbar import lines as L           # noqa: E402
from ai_trader.kbar.store import KBarStore      # noqa: E402

# 层的顺序 = 依赖顺序。改这个列表前请先看 build.py 里各层的 docstring：
# 表1 依赖表0；表2 依赖 0+1；表2.5 依赖 0+1+2；表3/5 依赖上面全部；
# 表6/6b/7 依赖表5。顺序错了会拿旧数据算出"看起来合理"的结论。
LAYERS = ["lines", "facts", "reactions", "events", "forward",
          "shape", "verify", "survival", "dims"]

# 哪些层真能按 since_ts 只算尾部。不在这儿的层会被整表重算。
INCREMENTAL = {"verify", "survival", "dims"}

# 哪些层靠"只喂新增 bar"实现增量（upsert 语义，不删旧行）。
# 这两层必须和 INCREMENTAL 分开标注：它们的增量来自**输入被裁剪**，
# 而不是函数内部有 since_ts。混成一个说法，运维就会以为
# "标了增量就一定很快"，而实际差异很大。
INPUT_INCREMENTAL = {"lines", "facts"}

# 整表重算的层（只有 rebuild 开关，没有 since_ts）。
FULL_REBUILD = {"reactions", "events", "forward", "shape"}


class InputSourceMissing(RuntimeError):
    """输入源本身是空的 —— 不是「没有新 bar」，是**根本没东西可跑**。

    ★ 为什么值得单独一个异常类：这两件事以前混在一起，结果一模一样
      （打印一行、继续跑、退出码 0），而后果完全不同：
        「没有新 bar」   = 正常，库里已经是最新的
        「输入源是空的」 = 故障，这一轮什么都没做，但对外报告成功

      实测代价（2026-11-17 查）：`kbar_meta` 里 `last_build:BTCUSDT:5m`
      记着 `bars_in=34561 / last_ts=11-01`，而写入时间是 11-09 ——
      11-09 那次构建**一根都没进来**，5m 演化层因此停在 11-01，
      比 15m 少了 7 天，而这 7 天里每小时的任务都「成功」退出。
    """


def _source_hint(args) -> str:
    """针对当前周期，说清楚「该往哪儿喂数据」。"""
    if args.interval_ms == 300_000:
        return ("  5m 在 market.db 里没有原生行时，两条路：\n"
                "    a) 加 --from-1m，从 1m 聚合（要求 1m 自己连续）；\n"
                "    b) 先跑 tools/backfill_5m_bars.py 把 5m 补进 market.db.bars。\n"
                "  注意 5m 直到 2026-11-09 才进 config.INTERVALS_MS 成为原生周期，\n"
                "  在那之前的 5m 只能靠回填。")
    if args.interval_ms == 1_800_000:
        return "  30m 没有原生 bar，用 --from-kbar-15m。"
    return ("  market.db 的 bars 表里没有 %s 的行 —— 先确认实时采集器在写这个周期。"
            % L.period_label(args.interval_ms))


def _check_continuity(bars, interval_ms: int, since_ts: int):
    """查输入 K 线是否等间隔，以及「新段接缝」是否严丝合缝。

    返回 ``(断口列表, 接缝)``：

    * 断口：相邻两根 ts 差 != interval_ms（输入内部有洞）
    * 接缝：第一根新 bar 的 ts != since_ts + interval_ms（与库里已有的尾巴之间有洞）

    ★ 为什么这是**硬闸**而不是警告：`lines.build_bar_lines` 的 EMA / 唐奇安
      是沿序列递推的，根数错了值就全错，而算出来的数**看起来完全合法**。
      `forward`（表3）早就有这道检查（build.py 里那条「K 线不是等间隔」），
      但最上游的 lines 没有 —— 于是洞从最底层就开始污染。
    """
    iv = int(interval_ms)
    ts_list = [int(b.ts) for b in bars]
    gaps = []
    for i in range(1, len(ts_list)):
        d = ts_list[i] - ts_list[i - 1]
        if d != iv:
            gaps.append((ts_list[i - 1], ts_list[i], d))
    seam = None
    if since_ts:
        first_new = next((t for t in ts_list if t > since_ts), None)
        if first_new is not None and first_new != since_ts + iv:
            seam = (int(since_ts), first_new)
    return gaps, seam


def _fmt_ts_ms(ms: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime(
        "%Y-%m-%d %H:%M")


def _kind(name: str, has_new_bars: bool) -> str:
    if name in INPUT_INCREMENTAL:
        return "增量（只喂新 bar）" if has_new_bars else "跳过（无新 bar）"
    if name in INCREMENTAL:
        return "增量（since_ts）"
    if name in FULL_REBUILD:
        return "全表重算"
    return "?"


def _argp():
    ap = argparse.ArgumentParser(
        description="K 线演化增量编排器：把 market.db 的新 bar 传导到 kbar.db")
    ap.add_argument("--kbar-db", default=B.DEFAULT_KBAR_DB)
    ap.add_argument("--market-db", default=B.DEFAULT_MARKET_DB)
    ap.add_argument("--range-db", default=B.DEFAULT_RANGE_DB)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval-ms", type=int, default=900_000,
                    choices=[300_000, 900_000, 1_800_000])
    ap.add_argument("--from-1m", action="store_true",
                    help="market.db 没有该周期原生 bar 时，用 1m 聚合出来")
    ap.add_argument("--from-kbar-15m", dest="from_kbar_15m",
                    action="store_true",
                    help="用 kbar.db 已落库的 15m 合成更大周期。30m 必须走这条："
                         "market.db 的 1m 只有 0.9 天，撑不起 30m 的 1500 根预热")
    ap.add_argument("--until", default="", choices=[""] + LAYERS,
                    help="只跑[到]这一层（含），调试用")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印计划，不写库")
    ap.add_argument("--force", action="store_true",
                    help="没有新 bar 也照样重算（默认跳过）")
    ap.add_argument("--allow-gaps", dest="allow_gaps", action="store_true",
                    help="★ 危险：允许在带洞的输入上硬算。EMA/唐奇安跨洞递推会得到"
                         "「看起来合法」的错值，只在明确知道后果时用。")
    return ap.parse_args()


def _last_ts(kbar_db: str, symbol: str, interval_ms: int) -> int:
    """kbar.db 里这个周期已经算到哪一根了。"""
    if not os.path.exists(kbar_db):
        return 0
    with KBarStore(kbar_db) as st:
        row = st._con.execute(
            "SELECT MAX(ts) FROM kbar_line WHERE symbol=? AND interval_ms=?",
            (symbol, int(interval_ms))).fetchone()
    return int(row[0]) if row and row[0] else 0


def _agg_from_1m(market_db: str, interval_ms: int) -> list:
    """从 market.db 的 `bars` 表按 1m 聚合成任意周期。

    ## 为什么不用 build.load_1m_as_15m

    那个函数查的表叫 `klines`，而 `data/market.db` 里**没有**这张表
    （实测只有 bar_gaps / bars / feed_events）。它的数据源是另一种库
    （CSV 回填那一套）。拿它读 market.db 会直接
    `sqlite3.OperationalError: no such table: klines` —— 报错信息还看不出
    是"表名不对"还是"库不对"。

    这里就按 `bars` 表的真实形状聚合：
        open_time 桶对齐 → o=首 o, h=max h, l=min l, c=末 c, v=sum v
    """
    import sqlite3
    con = B.open_ro(market_db)
    try:
        rows = con.execute(
            "SELECT open_time, o, h, l, c, v FROM bars "
            "WHERE interval_ms=60000 ORDER BY open_time").fetchall()
    finally:
        con.close()
    return _bucket_rows(rows, interval_ms)


def _bucket_rows(rows, interval_ms: int) -> list:
    """把任意更小周期的 bar 行按 interval_ms 桶对齐成更大周期。

    契约：rows 必须**按 ts 升序**（调用方的 SQL 都带 ORDER BY）。
    顺序不是小事 —— o 取桶内第一根、c 取桶内最后一根，顺序反了就变成
    「随便挑一根」，而算出来的 bar 依然看起来完全合法，不会报错。

    这是 30m 层的基础（30m = 2 根 15m），也是 1m 那条老路的实现。
    抽出来是因为「聚合」这件事只该有一份实现：两份就会有一份先被修。
    """
    interval_ms = int(interval_ms)
    buckets = {}
    order = []
    for ts, o, h, l, c, v in rows:
        k = (int(ts) // interval_ms) * interval_ms
        if k not in buckets:
            buckets[k] = [k, float(o), float(h), float(l), float(c), float(v or 0)]
            order.append(k)
        else:
            b = buckets[k]
            b[2] = max(b[2], float(h))
            b[3] = min(b[3], float(l))
            b[4] = float(c)
            b[5] += float(v or 0)
    return [buckets[k] for k in order]


def _agg_from_kbar_15m(kbar_db: str, symbol: str, interval_ms: int) -> list:
    """把 kbar.db 里**已落库的 15m 演化行**合成更大周期（30m / 1h）。

    ## 为什么需要这条路，而不是继续用市场库的 1m

    实测（2026-11-17，本地库）：

        market.db 的 1m : 1263 根，跨度 0.9 天
        kbar.db 的 15m  : 17654 行，跨度约 184 天

    而 `lines.build_bar_lines` 的口径是「这根之前必须有 EMA_WARMUP_BARS 根，
    否则一根都不落」。30m 的 1500 根预热 = 45000 分钟 ≈ 31 天 ——
    1m 那条路**差的不是一点，是差了 30 天**。而 15m 合成 30m 有 8827 根，
    预热绰绰有余。

    所以 30m 这层的数据源就是「库里已经算好的 15m」，不联网、不重拉。
    这也符合本工具「只认库里已有的 bar」的既有设计。
    """
    import sqlite3
    if int(interval_ms) % 900_000:
        raise ValueError(
            "只能从 15m 合成 15m 的整数倍周期（15m/30m/45m/1h…），收到 %d"
            % interval_ms)
    con = sqlite3.connect(
        "file:%s?mode=ro" % kbar_db.replace("\\", "/"), uri=True)
    try:
        rows = con.execute(
            "SELECT ts, o, h, l, c, vol FROM kbar_evolution"
            " WHERE symbol=? AND interval_ms=900000 ORDER BY ts",
            (symbol,)).fetchall()
    finally:
        con.close()
    return _bucket_rows(rows, interval_ms)


def _norm_bar(b):
    """把三种输入形态统一成 L.Bar。

    这个工具的三个输入源给的形态**各不相同**：
        load_market_bars -> L.Bar 命名元组
        _agg_from_1m     -> 纯 list [ts, o, h, l, c, v]
        （将来若有）     -> dict
    而下游两层的契约也不一样（lines 要 L.Bar，facts 要 dict）。
    所以归一化只做一次，就放在这里 —— 否则「谁要什么类型」会散在调用点，
    facts 层那次 `.get` 崩溃就是这么来的。
    """
    if isinstance(b, L.Bar):
        return b
    if isinstance(b, dict):
        return L.Bar(int(b["ts"]), float(b["o"]), float(b["h"]),
                     float(b["l"]), float(b["c"]),
                     None if b.get("v") is None else float(b["v"]))
    seq = tuple(b)
    return L.Bar(int(seq[0]), float(seq[1]), float(seq[2]),
                 float(seq[3]), float(seq[4]),
                 None if seq[5] is None else float(seq[5]))


def _as_fact_bars(bars):
    """facts 层要的是 dict（见 ai_trader/kbar/facts.py::bar_facts 的 docstring
    `bars: [{ts, o, h, l, c, v, buy_vol}]`），而 lines 层要 L.Bar。

    同一批输入、两种形态 —— 在这里做那一次转换，别让每个调用点各自猜。
    L.Bar 的字段名正好就是 ts/o/h/l/c/v，所以 _asdict() 直接可用。
    """
    return [dict(b._asdict()) for b in bars]


def _load_bars(args, since_ts: int, warmup_bars: int = 0):
    """取输入 bar。返回 (新增 bar, 含预热的历史 bar, note)。

    **两个列表必须分清**：
      new_bars  —— `ts > since_ts` 的真新增，facts/下游层只该看这个
      warm_bars —— 额外带上 warmup_bars 根历史，**只给 lines 层用**
                   （EMA 是递推量，没有前置序列就算不出来）
    
    三种来源，明确报出用的是哪一种 —— 不然"为什么 5m 的根数和 15m 一样"
    这类问题只能靠猜。

    取不到就给**能看懂**的结论，不抛 sqlite 原始栈：
    这个工具的输入是"本地库里的历史"，库不够就是库不够，
    那是运维要做的事（先把 market.db 喂满），不是这里能猜出来的。
    """
    try:
        native = B.load_market_bars(args.market_db, args.symbol, args.interval_ms)
    except Exception as e:
        native = []
        print("⚠ 读原生 %s 失败：%s" % (L.period_label(args.interval_ms), e))

    if args.from_kbar_15m:
        # 显式指定的来源优先于「原生」判断：调用方说要什么就给什么。
        # 30m 在 market.db 里没有原生 bar，而 1m 又不够预热，只有这条路。
        try:
            bars = _agg_from_kbar_15m(args.kbar_db, args.symbol, args.interval_ms)
            note = "kbar.db 15m 聚合为 %s（%d 根）" % (
                L.period_label(args.interval_ms), len(bars))
        except Exception as e:
            bars, note = [], "kbar 15m 聚合失败：%s" % e
    elif native and len(native) > 100:
        note = "market.db 原生 %s（%d 根）" % (
            L.period_label(args.interval_ms), len(native))
        bars = native
    elif args.from_1m:
        try:
            bars = _agg_from_1m(args.market_db, args.interval_ms)
            note = "market.db 1m 聚合为 %s（%d 根）" % (
                L.period_label(args.interval_ms), len(bars))
        except Exception as e:
            bars, note = [], "1m 聚合失败：%s" % e
    else:
        bars = native
        note = "market.db 原生（稀疏，%d 根）" % len(bars)

    if not bars:
        # ★ 以前这里只给 note 加一句话就继续跑，退出码 0。
        #   后果实测：2026-11-09 那次 5m 构建，kbar_meta 记着
        #   bars_in=34561 / last_ts=11-01，跟上一轮一模一样 ——
        #   任务「成功」了，但一根都没进来。见 InputSourceMissing 的注释。
        raise InputSourceMissing(
            "输入源是空的，这一轮什么都不会算：%s\n%s" % (note, _source_hint(args)))

    bars = [_norm_bar(b) for b in bars]
    new_bars = [b for b in bars if int(b.ts) > since_ts]

    # ---- 预热窗口 ----
    # lines 层是递推量（EMA 尤其），`lines.build_bar_lines` 的口径是
    # 「序列里这根之前必须有 EMA_WARMUP_BARS 根」。只喂新增 bar 的后果实测过：
    # 连补 622 根 15m 后，donchian（预热 150 根）有 473 根值、
    # 而 ema*（预热 1500 根）**一根都没有** —— range_* 照常有，因为那是外部给的
    # range_rows，不依赖序列。于是「区间突破看得见、EMA20 全空」这种半残状态
    # 会安静地通过所有下游分析。
    #
    # 所以这里额外给 lines 层一段历史。注意它**只是输入**：
    # 落库仍是 upsert，重复算不会多出行。
    if warmup_bars > 0 and since_ts > 0:
        lo = since_ts - int(warmup_bars) * int(args.interval_ms)
        warm_bars = [b for b in bars if int(b.ts) > lo]
    else:
        warm_bars = new_bars

    return new_bars, warm_bars, note


def main() -> int:
    args = _argp()
    label = L.period_label(args.interval_ms)
    cutoff = LAYERS.index(args.until) + 1 if args.until else len(LAYERS)

    # 30m 目前只落 lines 层。facts / reactions / events / forward / shape 这些层
    # **从来没有在 30m 上验证过口径**（比如 facts.py 的 TICKS_PER_BAR 是按 15m
    # 算出来的常量，换周期就错）。让它们默默跑，产出的是一批"看起来合理"的
    # 30m 结论 —— 而那正是本项目最吃过亏的一种失败方式（见 §十五 静默停摆）。
    # 所以这里宁可硬停，让人显式写 --until lines。
    if args.interval_ms == 1_800_000 and cutoff > LAYERS.index("lines") + 1:
        print("✗ 30m 目前只支持到 lines 层（ema20@30m 等观察线）。")
        print("  其余层没有在 30m 上验证过口径，不能默默往下跑。请显式指定：")
        print("    python tools/ingest_kbar.py --interval-ms 1800000 "
              "--from-kbar-15m --until lines")
        return 2

    print("=" * 72)
    print("K 线演化增量编排  %s %s" % (args.symbol, label))
    print("=" * 72)

    if not os.path.exists(args.market_db):
        print("✗ 找不到 market.db：%s" % args.market_db)
        return 2
    if not os.path.exists(args.kbar_db):
        print("✗ 找不到 kbar.db：%s（先跑一次全量回填）" % args.kbar_db)
        return 2

    since = _last_ts(args.kbar_db, args.symbol, args.interval_ms)
    print("库里已算到：%s" % (since or "（空，会按全量算）"))

    # ---- --force 的真正用途 ----
    # 光跳过「没有新 bar」那句提示没用：库已经算到最新时 new_bars=0，
    # 预热窗口恰好只有 warmup 根，而 EMA 要求「序列里它之前有 warmup 根」，
    # 于是一根都算不出来 —— 修完预热 bug 也补不回已经写进库的缺口。
    # 所以 --force 在这里把**输入窗口**往前推 warmup 根，让它重算已算过的区间。
    # 注意 since 本身不动：verify/survival/dims 的 since_ts 仍按真实进度走。
    load_since = since
    if args.force and since > 0:
        load_since = since - int(L.EMA_WARMUP_BARS) * int(args.interval_ms)
        print("--force：输入窗口往前推 %d 根，重算已算过的区间（since 本身不变）"
              % L.EMA_WARMUP_BARS)

    try:
        bars, warm_bars, note = _load_bars(args, load_since,
                                           warmup_bars=L.EMA_WARMUP_BARS)
    except InputSourceMissing as e:
        # 「没东西可跑」必须是失败。以前这里静默继续，于是 5m 停了 7 天
        # 而每小时任务都报成功（见 InputSourceMissing 注释）。
        print("✗ %s" % e)
        return 2
    print("输入来源：%s" % note)
    print("新增 bar：%d 根" % len(bars))
    if len(warm_bars) > len(bars):
        print("预热窗口：额外带上 %d 根历史（lines 层算 EMA 用，不落新行）"
              % (len(warm_bars) - len(bars)))

    # ---- K 线连续性闸 ----
    # EMA / 唐奇安沿序列递推，跨洞算出来的值**看起来完全合法**。
    # 这道闸挡的是「11-09 之后原生 5m 突然出现，于是把 ts>11-01 的行全灌进来，
    # 中间 7 天空洞」这一类事故 —— 它不会报错，只会让 5m 的 EMA 全错。
    gaps, seam = _check_continuity(bars, args.interval_ms, since)
    if gaps or seam:
        print("\n✗ 输入 K 线不连续，拒绝增量（不允许跨洞算 EMA/唐奇安）：")
        if seam:
            missing = (seam[1] - seam[0]) // int(args.interval_ms) - 1
            print("  接缝断口：库里算到 %s，而来源里第一根新 bar 是 %s —— 中间缺 %d 根 %s"
                  % (_fmt_ts_ms(seam[0]), _fmt_ts_ms(seam[1]),
                     missing, label))
        for a, b, d in gaps[:5]:
            print("  段内断口：%s → %s（间隔 %d ms，应为 %d ms）"
                  % (_fmt_ts_ms(a), _fmt_ts_ms(b), d, args.interval_ms))
        if len(gaps) > 5:
            print("  …共 %d 处段内断口" % len(gaps))
        if not args.allow_gaps:
            print("\n  先把洞补上再跑：")
            if args.interval_ms == 300_000:
                print("    python tools/backfill_5m_bars.py --fill-gaps")
            else:
                print("    补满 market.db 里 %s 的行，或检查实时采集器有没有断过。" % label)
            print("  （确认要在带洞的数据上硬算，加 --allow-gaps，风险自负）")
            return 2
        print("  --allow-gaps：继续跑，但上面这些位置的递推值是错的。")

    if not bars and not args.force:
        print("\n没有新 bar。用 --force 可强制重算已算过的区间。")
        # 没有新 bar 也不代表没事：表6/6b 的尾部会随窗口推进而变化
        # （最后一根从「未成熟」变成「已破/活到最后」）。这一步很便宜，
        # 所以照跑 —— 这正好补上 update_verify_tail 只管 12 根的那个洞。
        print("→ 仍会刷新表 6/6b 尾部（窗口推进会让旧判决成熟）")

    # facts 层的预热：vol_rel 只需要前 VOL_REL_N 根，不像 lines 要 1500 根。
    # 从 warm_bars 尾部切够用的一段，避免顺手重写 1500 行旧数据。
    _need = len(bars) + int(F.VOL_REL_N) + 1
    fact_bars = warm_bars[-_need:] if len(warm_bars) > _need else warm_bars
    if len(fact_bars) > len(bars):
        print("facts 预热：多带 %d 根历史（vol_rel = vol/前 %d 根中位数）"
              % (len(fact_bars) - len(bars), F.VOL_REL_N))

    range_rows = []
    if os.path.exists(args.range_db):
        try:
            range_rows = B.load_range_rows(args.range_db, args.symbol)
        except Exception as e:                       # pragma: no cover
            print("⚠ 区间四线读取失败（继续，表0 会少 line_price 列）：%s" % e)

    steps = []
    for name in LAYERS[:cutoff]:
        steps.append((name, _kind(name, bool(bars))))

    print("\n计划：")
    for i, (name, kind) in enumerate(steps, 1):
        print("  %d. %-10s %s" % (i, name, kind))

    if args.dry_run:
        print("\n--dry-run：没有写库。")
        return 0

    # ---- 逐层执行。任一层失败就停，不带着旧数据往下跑。 ----
    done = []
    try:
        # 注意：这里的循环变量绝对不能叫 _kind。main() 内部一旦出现对 _kind 的
        # 赋值，Python 就会把 _kind 判成局部名，上面 L240 那次「调用模块级 _kind」
        # 会变成 UnboundLocalError —— 而且跟走不走得到这一行无关（编译期定性）。
        # 这个 bug 让本工具从 2026-11-10 09:40 起任何调用都必崩，
        # kbar 因此静默停摆 6.5 天。见 tests/test_ingest_kbar_cli.py。
        for name, _kind_label in steps:
            print("\n[%s] 开始…" % name)
            if name == "lines":
                if not warm_bars:
                    print("  跳过：没有新 bar")
                    continue
                # 注意传的是 warm_bars（含预热历史），不是 bars —— 见 _load_bars 注释
                st = B.build_and_persist(args.kbar_db, args.symbol,
                                         args.interval_ms, warm_bars, range_rows,
                                         sources=[os.path.basename(args.market_db)])
            elif name == "facts":
                if not bars:
                    print("  跳过：没有新 bar")
                    continue
                # 和 lines 层同一个坑：facts 里的 vol_rel = vol / median(前 20 根)
                # （facts.py:44 VOL_REL_N=20）。只喂新 bar 时前 20 根算不出来，
                # 而 bar_facts 只是把它记进 stats["skipped"]["vol_rel:warmup"] 就跳过
                # —— 不报错、不出声，最新的 bar 上 vol_rel 就是 NULL。
                # 实测：每轮增量都会在最后几根留 NULL（11-10/11-11 各 20 根永久残缺）。
                # 所以喂 fact_bars：在 bars 前面补够 VOL_REL_N 根历史。
                # 只补到够用为止，不像 lines 那样补 1500 根 —— facts 没有 EMA 那种
                # 长预热，补多了只是白重写旧行。
                st = B.build_and_persist_facts(args.kbar_db, args.symbol,
                                               args.interval_ms,
                                               _as_fact_bars(fact_bars),
                                               range_rows,
                                               sources=[os.path.basename(args.market_db)])
            elif name == "reactions":
                st = B.build_and_persist_reactions(args.kbar_db, args.symbol,
                                                   args.interval_ms)
            elif name == "events":
                st = B.build_and_persist_events(args.kbar_db, args.symbol,
                                                args.interval_ms)
            elif name == "forward":
                st = B.build_and_persist_forward(args.kbar_db, args.symbol,
                                                 args.interval_ms)
            elif name == "shape":
                st = B.build_and_persist_shape(args.kbar_db, args.symbol,
                                               args.interval_ms)
            elif name == "verify":
                st = B.build_and_persist_verify(args.kbar_db, args.symbol,
                                                args.interval_ms,
                                                since_ts=since or None)
            elif name == "survival":
                st = B.build_and_persist_survival(args.kbar_db, args.symbol,
                                                  args.interval_ms,
                                                  since_ts=since or None)
            elif name == "dims":
                st = B.build_and_persist_dims(args.kbar_db, args.symbol,
                                              args.interval_ms,
                                              since_ts=since or None)
            else:                                     # pragma: no cover
                raise AssertionError("未知层 %s" % name)
            n = st.get("persisted", st.get("rows", "?"))
            print("  完成：persisted=%s" % n)
            done.append((name, n))
    except Exception as e:
        print("\n✗ 第 %d 层（%s）失败，已停止：%s: %s"
              % (len(done) + 1, steps[len(done)][0], type(e).__name__, e))
        print("  已完成的层：%s" % ("、".join(n for n, _ in done) or "无"))
        print("  ⚠ 下游层拿的是上一轮的旧数据，不要当成这次的结果。")
        return 1

    print("\n" + "=" * 72)
    print("全部完成，共 %d 层：" % len(done))
    for name, n in done:
        print("  %-10s persisted=%s" % (name, n))

    with KBarStore(args.kbar_db) as st:
        new_last = _last_ts(args.kbar_db, args.symbol, args.interval_ms)
        print("\n现在库里算到：%s" % new_last)
        if new_last == since and bars and not args.force:
            print("⚠ 算完了但最后时间戳没变 —— 检查输入 bar 是否真的被写入")
        elif new_last == since and args.force:
            print("（--force 按其语义不推进 since，时间戳不变是预期的）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
