# -*- coding: utf-8 -*-
"""K 线演化 · 取数 + 落库的胶水层（第 1 步）。

三条纪律，写在这里而不靠自觉
----------------------------
1. **生产库只读。** 所有对 market.db / market_range.db 的连接都用
   `file:...?mode=ro` 打开。这不是「记得别写」，是 SQLite 层面的拒绝：
   写库的代码路径根本没有连接可用。
2. **新表只进 data/kbar.db。** 落库前校验目标不是任何一个只读源，
   防止有人把 --kbar-db 指到 market.db 上，把观察线写进生产行情库。
3. **禁止静默过滤。** 少产出的线必须有原因计数（预热不足 / 窗口不够 /
   区间行还没收盘 / 库里字段是 NULL），由调用方整份打印。

数据来源的三种形态
------------------
    load_market_bars()     data/market.db  bars 表（symbol, interval_ms, open_time, o,h,l,c,v）
    load_range_bars_1h()   data/market_range.db  bars_1h（ts, ts_h, o,h,l,c,v）
    load_range_rows()      data/market_range.db  range_evolution（四线，只读不重算）
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Sequence
from collections import Counter

from . import lines as L
from .store import KBarStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_MARKET_DB = os.path.join(ROOT, "data", "market.db")
DEFAULT_RANGE_DB = os.path.join(ROOT, "data", "market_range.db")
DEFAULT_KBAR_DB = os.path.join(ROOT, "data", "kbar.db")

FAPI = "https://fapi.binance.com/fapi/v1/klines"


# ---------------------------------------------------------------- 只读连接
def open_ro(path: str) -> sqlite3.Connection:
    """只读打开一个生产库。写操作会在 SQLite 层被拒绝（不是靠约定）。

    用 URI 形式是为了能带 mode=ro。Windows 盘符路径要转成 file:D:/...，
    且必须 URL 编码（路径里有空格/中文时不编码会打不开）。
    mode=ro 与生产正在写的 WAL 兼容：读得到 WAL 里已提交但未 checkpoint 的行，
    所以「服务在跑」不会让探针读到半截旧数据。
    """
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise FileNotFoundError("库不存在: %s" % p)
    uri = "file:%s?mode=ro" % urllib.parse.quote(p.replace("\\", "/"))
    con = sqlite3.connect(uri, uri=True)
    # 防御性自证：确认真的是只读连接，而不是"以为只读"
    try:
        con.execute("PRAGMA query_only=ON")
    except Exception:
        pass
    return con


def _assert_writable_target(kbar_db: str, sources: Sequence[str]) -> None:
    """落库目标绝不能是任何一个只读源。写错路径的代价是污染生产库。"""
    tgt = os.path.normcase(os.path.abspath(kbar_db))
    for s in sources:
        if not s:
            continue
        if os.path.normcase(os.path.abspath(s)) == tgt:
            raise ValueError("落库目标 %s 与只读源 %s 是同一个文件，拒绝写入"
                             % (kbar_db, s))


# ---------------------------------------------------------------- 取数
def load_market_bars(path: str, symbol: str, interval_ms: int,
                     limit: Optional[int] = None) -> List[L.Bar]:
    """从 market.db 读 K 线（只读）。返回按 ts 升序的 Bar 列表。"""
    con = open_ro(path)
    try:
        sql = ("SELECT open_time, o, h, l, c, v FROM bars "
               "WHERE symbol = ? AND interval_ms = ? ORDER BY open_time")
        args: List = [symbol, int(interval_ms)]
        if limit:
            # 要的是**最近** limit 根，所以先倒序取再翻回来
            sql = ("SELECT open_time, o, h, l, c, v FROM bars "
                   "WHERE symbol = ? AND interval_ms = ? "
                   "ORDER BY open_time DESC LIMIT ?")
            args.append(int(limit))
            rows = con.execute(sql, args).fetchall()
            rows.reverse()
        else:
            rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return [L.Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                  float(r[4]), None if r[5] is None else float(r[5])) for r in rows]


def load_range_bars_1h(path: str, symbol: str,
                       limit: Optional[int] = None) -> List[L.Bar]:
    """从 market_range.db 的 bars_1h 读 1h K 线（只读）。

    行序是 (ts, ts_h, open, high, low, close, volume) —— ts_h 是北京时区字符串，
    这里**丢掉不用**：库内一律用 UTC 毫秒 ts，时区只在打印时换算（engine.ts8）。
    """
    con = open_ro(path)
    try:
        if limit:
            rows = con.execute(
                "SELECT ts, open, high, low, close, volume FROM bars_1h "
                "WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                (symbol, int(limit))).fetchall()
            rows.reverse()
        else:
            rows = con.execute(
                "SELECT ts, open, high, low, close, volume FROM bars_1h "
                "WHERE symbol = ? ORDER BY ts", (symbol,)).fetchall()
    finally:
        con.close()
    return [L.Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                  float(r[4]), None if r[5] is None else float(r[5])) for r in rows]


def load_range_rows(path: str, symbol: str) -> List[Dict]:
    """从 market_range.db 的 range_evolution 读四线（只读，**不重算**）。

    行序与 RangeStore.states() 一致，交给 lines._norm_range_rows 归一。
    为什么不在这里调引擎重算：库里的四线是生产状态机的产出，
    观察线要观测的是「生产当时画在哪」，重算出来的是「我的实现认为画在哪」。
    这两个在引擎改版后会分叉，而分叉了看不出来。
    """
    con = open_ro(path)
    try:
        rows = con.execute(
            "SELECT ts, ts_h, soft_up, soft_dn, hard_up, hard_dn, v11_key, "
            "family, segment, direction, tag FROM range_evolution "
            "WHERE symbol = ? ORDER BY ts", (symbol,)).fetchall()
    finally:
        con.close()
    return [{"ts": r[0], "ts_h": r[1], "soft_up": r[2], "soft_dn": r[3],
             "hard_up": r[4], "hard_dn": r[5], "key": r[6], "family": r[7],
             "segment": r[8], "direction": r[9], "tag": r[10]} for r in rows]


def fetch_bars(symbol: str, interval: str, limit: int = 1500) -> List[L.Bar]:
    """从币安拉 K 线（探针用；等同于 web/live.py 的取数路径）。

    代理策略：跟随环境变量（urllib 默认）。不硬编码 ProxyHandler({})——
    那在「必须走代理才能出网」的机器上直接超时，而写它的人只看得到自己那台直连的机器。
    币安单次上限 1500，超过就按 endTime 往前翻页（与 tools/backfill_range_evolution.py 同法）。
    """
    opener = urllib.request.build_opener()
    out: List[L.Bar] = []
    end_time: Optional[int] = None
    while len(out) < int(limit):
        n = min(1500, int(limit) - len(out))
        url = "%s?symbol=%s&interval=%s&limit=%d" % (
            FAPI, urllib.parse.quote(symbol), urllib.parse.quote(interval), n)
        if end_time:
            url += "&endTime=%d" % end_time
        req = urllib.request.Request(url, headers={"User-Agent": "ai-trader"})
        with opener.open(req, timeout=20) as r:
            raw = json.loads(r.read().decode("utf-8"))
        if not raw:
            break
        batch = [L.Bar(int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                       float(x[4]), float(x[5])) for x in raw]
        out = batch + out
        end_time = int(raw[0][0]) - 1
        if len(raw) < n:
            break
    seen = {b.ts: b for b in out}
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------- 落库
def params_snapshot(interval_ms: int) -> Dict:
    """本次计算用的全部口径参数。写进 kbar_meta，让库自己解释自己。"""
    return {
        "interval_ms": int(interval_ms),
        "interval_label": L.period_label(interval_ms),
        "ema_periods": list(L.EMA_PERIODS),
        "ema_warmup_bars": L.EMA_WARMUP_BARS,
        "donchian_hi_bars": L.DONCHIAN_HI_BARS,
        "donchian_lo_bars": L.DONCHIAN_LO_BARS,
        "prior_hl_bars": L.PRIOR_HL_BARS,
        "swing_half": L.SWING_HALF,
        "range_period_ms": L.RANGE_PERIOD_MS,
        "range_line_kinds": list(L.RANGE_LINE_KINDS),
        "source": "docs/K线演化-实现记录.md",
    }


def _bar_ts(b):
    """取一根 bar 的时间戳。两种形态都要支持：

    - 元组 `(ts, o, h, l, c, v)` —— 第 1 步的 `L.Bar`
    - 字典 `{"ts":..., "o":..., "c":...}` —— 第 2 步的 facts 入参
    只写一种，另一层落来源时就会静默抛异常或被吞掉。
    """
    if isinstance(b, dict):
        return int(b["ts"])
    return int(b[0])


def _bar_close(b):
    if isinstance(b, dict):
        return float(b["c"])
    return float(b[4])


def _bars_fingerprint(bars: Sequence) -> str:
    """这批输入 bar 的指纹：区间 + 根数 + 收盘价序列的摘要。

    只取 (first_ts, last_ts, n, sha1(closes)) 而不是全量 hash：
    全量 hash 对内存要求高，而这里要回答的问题只是"**同一段区间、同样的价格**"
    —— 区间对得上但收盘价对不上，就说明源数据不是同一份（这正是要找的）。
    1e-6 精度足够区分真实行情差异，又不会被浮点末位噪声干扰。
    """
    if not bars:
        return "empty"
    h = hashlib.sha1()
    for b in bars:
        h.update(("%.6f;" % _bar_close(b)).encode("ascii"))
    return "%d-%d:n=%d:%s" % (_bar_ts(bars[0]), _bar_ts(bars[-1]), len(bars),
                              h.hexdigest()[:16])


def record_source(st: "KBarStore", symbol: str, interval_ms: int, layer: str,
                  bars: Sequence = (), sources: Sequence[str] = (),
                  note: str = "") -> Dict:
    """把「这批数据是从哪儿来的」写进 kbar_meta。

    ## 为什么要这个

    2026-11-09 审计发现：`kbar_meta` 的 39 个键**全是参数/指纹/时间戳**，
    没有一条记来源。后果是当时 15m 的演化层里有 2026-11-01 之后的数据，
    而本地所有库都到 2026-11-01 —— **没人能说明那段数据是怎么来的**，
    也没法重放验证。复现性就这么断了，而且断了以后没有任何线索。

    所以每层落库时顺手记一条：源是谁、覆盖哪段、多少根、输入指纹、什么命令。
    写的是"事实"不是"结论"：哪天有人怀疑某段数据不对劲，
    拿这条就能回到源头去查，而不是对着库猜。

    ⚠ 只记事实，不参与判定。`shape.judge` / `dims.snapshot` 都不读这些键，
    所以加它不会改变任何一个结论（`test_source_provenance_does_not_affect_
    judgments` 钉住这一点）。
    """
    rec = {
        "layer": layer,
        "symbol": symbol,
        "interval_ms": int(interval_ms),
        "sources": [str(s) for s in sources],
        "bars": len(bars),
        "first_ts": _bar_ts(bars[0]) if bars else None,
        "last_ts": _bar_ts(bars[-1]) if bars else None,
        "input_fp": _bars_fingerprint(bars),
        "built_at": int(time.time() * 1000),
        "command": os.environ.get("AI_KBAR_BUILD_CMD") or "",
        "note": note,
    }
    st.set_meta("source:%s:%s:%s" % (symbol, L.period_label(interval_ms), layer),
                rec)
    return rec


def build_and_persist(kbar_db: str, symbol: str, interval_ms: int,
                      bars: Sequence[L.Bar],
                      range_rows: Optional[Sequence] = None,
                      *, rebuild: bool = False,
                      sources: Sequence[str] = (),
                      now_ms: Optional[int] = None,
                      dry_run: bool = False) -> Dict:
    """算 + 落库，返回统计（含落库前后行数）。

    rebuild=True 会先删掉该 (symbol, interval_ms) 的旧行再写 —— 只用于整段重算。
    增量回填不要用 rebuild（会把已算好的历史抹掉，而且抹掉后图还照常能画）。
    """
    _assert_writable_target(kbar_db, sources)
    rows, stats = L.build_bar_lines(symbol, interval_ms, bars, range_rows,
                                    now_ms=now_ms)
    stats = dict(stats)
    stats["params"] = params_snapshot(interval_ms)

    if dry_run:
        stats["persisted"] = 0
        stats["dry_run"] = True
        return stats

    with KBarStore(kbar_db) as st:
        if rebuild:
            stats["deleted"] = st.delete_interval(symbol, interval_ms)
        before = st.counts(symbol, interval_ms)
        written = st.upsert_lines(rows)
        after = st.counts(symbol, interval_ms)
        stats["persisted"] = written
        stats["db_before"] = before
        stats["db_after"] = after
        # 口径自证：库自己记住这次是用哪套参数算的
        st.set_meta("line_params:%s:%s" % (symbol, L.period_label(interval_ms)),
                    stats["params"])
        st.set_meta("last_build:%s:%s" % (symbol, L.period_label(interval_ms)),
                    {"bars_in": stats.get("bars_in"), "lines": stats.get("lines"),
                     "persisted": written, "first_ts": after.get("first_ts"),
                     "last_ts": after.get("last_ts")})
        # 来源登记：见 record_source 的文档（补复现性，不参与判定）
        record_source(st, symbol, interval_ms, "lines", bars, sources,
                      note="表 0 观察线快照")
    return stats


# ================================================================ 第 2 步：事实层
#
# 这一步的数据现实（2026-11-07 实测，不能想当然）：
#
#   15m 结构   三处来源，覆盖不同，必须合并而不是二选一：
#              A) market.db bars   interval_ms=900000   真实 15m，带 b/s（买量/卖量）
#                                 生产 1139 根（2026-10-26 起，服务起来之后才有）
#              B) backtest.db 1m   聚合成 15m            120 天（2026-07-04 起），带 v 与主动买量
#              C) data/range_bars_15m.csv  只有 ts,o,h,l,c   180 天（2026-05-08 起），无量
#              优先级 A > B > C：越靠前越接近「生产当时看到的」，量能覆盖也越好。
#   量能       只有 A 与 B 有；C 没有量。taker_buy_ratio 用 b/v。
#              **陷阱**：backtest.db 的 s 列是「主动买报价额」，不是卖出量；
#              market.db 的 s 才是卖出量（b+s==v 实测精确）。所以只用 b/v，不碰 s。
#   内路径     只有生产 3s 桶有，而 3s 只保留 168 小时（config.StoreCfg.tick_retention_hours）。
#              所以内路径只覆盖最近约 7 天，更早的留空并计入丢弃清单。
#
# 合并时若 A 与 B 在同一 ts 上数值不符，**不静默选边**：保留 A，把冲突逐条记下来。
# 实测存在这种冲突（2026-10-31 17:45 整根不一致，与 feed 当时重启重建有关）。

TOL_REL = 1e-9          # 两源比对容差（相对）


def _rel_diff(a: float, b: float) -> float:
    m = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / m


def load_market_bars_rich(path: str, symbol: str, interval_ms: int) -> List[Dict]:
    """从 market.db 读 K 线，**带上买卖量**。返回 [{ts,o,h,l,c,v,buy_vol,sell_vol}]。

    只读打开。market.db 的 b/s 是买量/卖量（b+s==v），与 backtest.db 同名不同义。
    """
    con = open_ro(path)
    try:
        rows = con.execute(
            "SELECT open_time, o, h, l, c, v, b, s FROM bars "
            "WHERE symbol = ? AND interval_ms = ? ORDER BY open_time",
            (symbol, int(interval_ms))).fetchall()
    finally:
        con.close()
    return [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
             "c": float(r[4]), "v": None if r[5] is None else float(r[5]),
             "buy_vol": None if r[6] is None else float(r[6]),
             "sell_vol": None if r[7] is None else float(r[7])} for r in rows]


def load_1m_as_15m(path: str, bucket_ms: int = 900_000) -> List[Dict]:
    """把 backtest.db 的 1m 聚合成 15m（一遍扫，不用 SQL 窗口函数）。

    只取 o/h/l/c/v/b。**不取 s**：backtest.db 的 s 是主动买报价额，不是卖出量。
    同时统计桶内的 1m 根数：不足「应有根数」的桶标记 incomplete，
    因为那种桶的量能天然偏小（缺的分钟没有成交记录，不是零成交量）。
    """
    expect = int(bucket_ms) // 60_000
    con = open_ro(path)
    try:
        rows = con.execute(
            "SELECT open_time, o, h, l, c, v, b FROM klines "
            "WHERE interval = '1m' ORDER BY open_time").fetchall()
    finally:
        con.close()
    out: List[Dict] = []
    cur = None
    for ts, o, h, l, c, v, b in rows:
        k = int(ts) // int(bucket_ms) * int(bucket_ms)
        if k != cur:
            cur = k
            out.append({"ts": k, "o": float(o), "h": float(h), "l": float(l),
                        "c": float(c), "v": float(v or 0.0),
                        "buy_vol": float(b or 0.0), "n_min": 1})
        else:
            a = out[-1]
            a["h"] = max(a["h"], float(h))
            a["l"] = min(a["l"], float(l))
            a["c"] = float(c)
            a["v"] += float(v or 0.0)
            a["buy_vol"] += float(b or 0.0)
            a["n_min"] += 1
    for a in out:
        a["incomplete"] = a["n_min"] != expect
    return out


def load_csv_bars(path: str) -> List[Dict]:
    """读 CSV K 线。必需列 `ts,o,h,l,c`，**可选列 `v` / `buy_vol`**。

    ## 为什么后来加了可选量能列

    第一版只认结构列，注释写着「只有结构，没有量」。那是当时那份
    `range_bars_15m.csv` 的事实（它确实只有 5 列）。但把它写成**硬性**约定
    就成了缺陷：后来 `hist.db.k2` 里明明有 `v`（5m 752,126 根、15m 250,710 根，
    2019 年起），导成 CSV 时被这一行 `"v": None` 直接丢掉了。

    后果不是报错，是**整类结论静默失效**：
      - `vol_rel` 全为 NULL（生产实测 5m 51299 根里 0 根有值）
      - `恐慌盘` 一次都不出现（它的必要条件就是量能）
      - 量能类 5 个维度全是空
    `check_kbar_pipeline.py` 会报「恐慌盘是死条件」，但那是**症状**；
    真正的原因在这一行。

    所以改成：有 `v` 列就读，没有就照旧 None。旧的 5 列 CSV 行为不变。
    """
    import csv as _csv
    out: List[Dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        rd = _csv.DictReader(fh)
        for r in rd:
            v = r.get("v")
            bv = r.get("buy_vol")
            out.append({"ts": int(r["ts"]), "o": float(r["o"]),
                        "h": float(r["h"]), "l": float(r["l"]),
                        "c": float(r["c"]),
                        "v": (float(v) if v not in (None, "") else None),
                        "buy_vol": (float(bv) if bv not in (None, "")
                                    else None)})
    out.sort(key=lambda x: x["ts"])
    return out


def merge_bars(*sources: Sequence[Dict]) -> tuple:
    """按优先级合并多份 K 线（前面的优先），返回 (bars, report)。

    冲突（同一 ts 上两份都有值但不等）不静默丢：保留高优先级的，
    把每一条冲突记进 report["conflicts"]，由调用方打印。
    """
    merged: Dict[int, Dict] = {}
    origin: Dict[int, int] = {}
    conflicts: List[Dict] = []
    hits = [0] * len(sources)

    for si, src in enumerate(sources):
        for b in src:
            ts = int(b["ts"])
            if ts not in merged:
                merged[ts] = dict(b)
                origin[ts] = si
                hits[si] += 1
                continue
            cur = merged[ts]
            bad = {}
            for k in ("o", "h", "l", "c", "v"):
                a, c2 = cur.get(k), b.get(k)
                if a is None or c2 is None:
                    continue
                if _rel_diff(float(a), float(c2)) > TOL_REL:
                    bad[k] = (float(a), float(c2))
            if bad:
                conflicts.append({"ts": ts, "kept_src": origin[ts], "seen_src": si,
                                  "diff": bad})
    bars = [merged[k] for k in sorted(merged)]
    report = {"conflicts": conflicts, "hits": hits,
              "bars": len(bars),
              "with_vol": sum(1 for b in bars if b.get("v") is not None),
              "with_buy": sum(1 for b in bars if b.get("buy_vol") is not None)}
    return bars, report


def ticks_cover(path: str, symbol: str, bucket_ms: int = 900_000):
    """3s 桶的覆盖区间（对齐到桶边界），返回 (lo, hi)；没有数据返回 (None, None)。

    为什么要对齐到桶边界：内路径是「一根 K 内有哪些 3s 桶」，所以判断某根能否
    算内路径，要按**整根**是否都被 3s 覆盖，而不是看这一根里恰好有几个桶 ——
    边界上的半根会让 conc 的分母悄悄错掉。
    """
    con = open_ro(path)
    try:
        lo, hi = con.execute(
            "SELECT MIN(open_time), MAX(open_time) FROM bars "
            "WHERE symbol = ? AND interval_ms = 3000", (symbol,)).fetchone()
    finally:
        con.close()
    if not lo:
        return None, None
    return (int(lo) // int(bucket_ms) * int(bucket_ms),
            (int(hi) // int(bucket_ms) + 1) * int(bucket_ms))


def load_ticks_3s(path: str, symbol: str, lo_ts: int, hi_ts: int,
                  bucket_ms: int = 900_000) -> Dict[int, List[Dict]]:
    """读 3s 桶并按所属 K 线分组：{bar_ts: [{ts,v,o,h,l,c}]}。

    只读。范围用 lo/hi 卡住，避免把 19 万根全拉进内存（其实也能，但没必要）。
    """
    con = open_ro(path)
    try:
        rows = con.execute(
            "SELECT open_time, o, h, l, c, v FROM bars "
            "WHERE symbol = ? AND interval_ms = ? AND open_time >= ? AND open_time < ? "
            "ORDER BY open_time",
            (symbol, 3000, int(lo_ts), int(hi_ts))).fetchall()
    finally:
        con.close()
    out: Dict[int, List[Dict]] = {}
    for ts, o, h, l, c, v in rows:
        k = int(ts) // int(bucket_ms) * int(bucket_ms)
        out.setdefault(k, []).append({"ts": int(ts), "o": float(o), "h": float(h),
                                      "l": float(l), "c": float(c),
                                      "v": float(v or 0.0)})
    return out


def facts_params_snapshot(interval_ms: int) -> Dict:
    """第 2 步的口径参数。写进 kbar_meta，让库自己解释自己。"""
    from . import facts as F
    return {
        "interval_ms": int(interval_ms),
        "interval_label": L.period_label(interval_ms),
        "vol_rel_n": F.VOL_REL_N,
        # ⚠ 这两个是**按周期推的算术值**，不是写死的常量。
        #    15m 推出来正好是 96 / 300（与旧版一致，所以 15m 的 params_hash 不变）；
        #    5m 是 288 / 100。以前这里直接写 F.SLOTS_PER_DAY（96），
        #    换任何非 15m 周期都会记下一个错的数。
        "slots_per_day": F.slots_per_day_for(interval_ms),
        "vol_slot_min_samples": F.VOL_SLOT_MIN_SAMPLES,
        "tick_interval_ms": F.TICK_INTERVAL_MS,
        "ticks_per_bar": F.ticks_per_bar_for(interval_ms),
        "range_period_ms": L.RANGE_PERIOD_MS,
        "undefined_by_design": ["path_shape", "max_tick_share"],
        "undefined_reason": {
            "path_shape": "设计稿未给词表与阈值；分类属判断标准，第 5 步前不碰",
            "max_tick_share": "需要逐笔成交，现有数据只有 3s 桶",
        },
        "later_steps": ["n_touch", "n_break", "event", "prev_event", "on_line",
                        "since_event_idx", "state"],
        "later_steps_status": {
            "n_touch": "第 3 步已填（100%）",
            "n_break": "**至今没有生产者**，填充率 0%；口径待定（D-10）",
            "event/prev_event/on_line/since_event_idx/state": "第 4 步已填",
        },
        "source": "docs/K线演化-实现记录.md",
    }


def build_and_persist_facts(kbar_db: str, symbol: str, interval_ms: int,
                            bars: Sequence[Dict],
                            range_rows: Optional[Sequence] = None,
                            ticks_by_bar: Optional[Dict[int, List[Dict]]] = None,
                            *, rebuild: bool = False,
                            sources: Sequence[str] = (),
                            dry_run: bool = False) -> Dict:
    """算事实层 + 落库（表 1）。行数与填充率由库自己数，不信调用方。

    n_lines 直接来自第 1 步的 kbar_line（那根上有几条观察线），
    这样两张表天然对齐；第 3 步回填 n_touch 时也是同一个基准。

    ⚠ `rebuild=True` 会 delete_evolution 整表 —— 第 3 步的 n_touch 和第 4 步那五列
    会一起被删掉（表 1 看起来仍有 O/H/L/C，不说不会有人发现）。所以第 2 步整段重算后，
    必须按 2 → 3 → 4 的顺序把后两步重跑一遍。
    """
    from . import facts as F
    _assert_writable_target(kbar_db, sources)

    with KBarStore(kbar_db) as st:
        line_counts = st.line_counts_by_ts(symbol, interval_ms)
        if rebuild:
            deleted = st.delete_evolution(symbol, interval_ms)
        else:
            deleted = 0
        rows, stats = F.bar_facts(bars, interval_ms,
                                  range_rows=range_rows, ticks_by_bar=ticks_by_bar,
                                  line_counts=line_counts,
                                  slots_per_day=F.slots_per_day_for(interval_ms),
                                  symbol=symbol)
    stats = dict(stats)
    stats["params"] = facts_params_snapshot(interval_ms)
    stats["deleted"] = deleted

    if dry_run:
        stats["persisted"] = 0
        stats["dry_run"] = True
        return stats

    with KBarStore(kbar_db) as st:
        before = st.evolution_counts(symbol, interval_ms)
        written = st.upsert_evolution(rows)
        after = st.evolution_counts(symbol, interval_ms)
        stats["persisted"] = written
        stats["db_before"] = before
        stats["db_after"] = after
        st.set_meta("facts_params:%s:%s" % (symbol, L.period_label(interval_ms)),
                    stats["params"])
        st.set_meta("last_build_facts:%s:%s" % (symbol, L.period_label(interval_ms)),
                    {"bars_in": stats.get("bars_in"), "rows": stats.get("rows"),
                     "persisted": written, "first_ts": after.get("first_ts"),
                     "last_ts": after.get("last_ts"),
                     "filled": after.get("filled")})
        record_source(st, symbol, interval_ms, "facts", bars,
                      [DEFAULT_MARKET_DB] if not sources else sources,
                      note="表 1 每根读数")
    return stats


# ================================================================ 第 3 步：反应层
#
# 反应层完全从库里派生：表 0 给「线在哪」，表 1 给「这根长什么样」。
# 不从原始行情重算，是因为「线当时画在哪」只能以表 0 为准 ——
# 重算一遍得到的是「我的实现现在认为画在哪」，参数一改就分叉，而分叉看不出来。

def reaction_params_snapshot(interval_ms: int) -> Dict:
    """第 3 步的口径参数。写进 kbar_meta，让库自己解释自己。"""
    from . import reaction as R
    return {
        "interval_ms": int(interval_ms),
        "interval_label": L.period_label(interval_ms),
        "touch_tol_pct": R.TOUCH_TOL_PCT,
        "touch_rule": "l <= line_price <= h（无阈值）",
        "line_value_rule": "取「上一根 K 线」那一行（reaction.pick_prev）；"
                           "那根没有这条线 ⟺ 那根上它不存在，不往前带",
        "line_value_rule_why": "这一条同时满足两件事：防偷未来（t-15m 的收盘 == t 的开盘）"
                               "与防带前（线消失后不许再用它）。所以不需要对 line_ready_ms 做二分",
        "line_value_rule_superseded":
            "第一版用「line_ready_ms <= 本根 ts 的最后一个值」（as_of）。它会无限期往前带 —— "
            "实测 2026-08-05 区间演化转 PRE 后，05:15 的 K 线仍在用 02:00 的 range_key。"
            "已废弃，只保留 as_of 作独立核对工具；见实现记录 §十九",
        "side_basis": "本根开盘价与线价比",
        "close_side_at": "c == p 记为 at，不塞进 above/below",
        "pen_recover_scale": "比例 0~1（设计稿公式不带 x100）；dist_pct 才是百分数",
        "undefined_by_design": ["dwell_bars"],
        "undefined_reason": {
            "dwell_bars": "「贴着」需要距离阈值，属判断标准（待确定 D-8）",
        },
        "nth_touch_rule": "键 (fam, seg, line_key)，按区间演化换段从 1 重数；"
                          "段缺失的根留空并计 nth_touch:no_segment（D-9）",
        "source": "docs/K线演化-待确定清单.md",
    }


def build_and_persist_reactions(kbar_db: str, symbol: str, interval_ms: int,
                                *, rebuild: bool = False, dry_run: bool = False) -> Dict:
    """从表 0 + 表 1 派生表 2，并把 n_touch 回填到表 1。"""
    from . import reaction as R
    stats: Dict = {"rows": 0, "persisted": 0}
    with KBarStore(kbar_db) as st:
        series = st.line_series(symbol, interval_ms)
        meta = st.line_meta(symbol, interval_ms)
        facts = st.evolution_rows(symbol, interval_ms)
        if not series:
            stats["error"] = "表 0 没有任何线，先跑 tools/backfill_kbar_lines.py"
            return stats
        if not facts:
            stats["error"] = "表 1 没有事实行，先跑 tools/backfill_kbar_facts.py"
            return stats

        bars = [{"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
                 "vol_rel": r["vol_rel"], "taker_buy_ratio": r["taker_buy_ratio"]}
                for r in facts if r["o"] is not None]
        bars.sort(key=lambda x: int(x["ts"]))
        # nth_touch 要按「区间演化换段」重置：段来自表 1 自己的 (fam, seg)
        seg_by_ts = {int(r["ts"]): (r["fam"], r["seg"]) for r in facts
                     if r.get("fam") is not None and r.get("seg") is not None}
        rows, rstats = R.build_reactions(symbol, interval_ms, bars, series, meta,
                                         seg_by_ts=seg_by_ts)
        R.assert_no_lookahead(rows, interval_ms)
        stats.update(rstats)
        stats["params"] = reaction_params_snapshot(interval_ms)
        stats["facts_bars"] = len(facts)
        stats["reaction_input_bars"] = len(bars)

        if dry_run:
            stats["dry_run"] = True
            return stats

        if rebuild:
            stats["deleted"] = st.delete_reactions(symbol, interval_ms)
        before = st.reaction_counts(symbol, interval_ms)
        written = st.upsert_reactions(rows)
        counts = R.touch_counts_by_bar(rows)
        # 评估过的根全都要有值：碰到 0 条就写 0，不能留 NULL。
        # 否则「这根没碰到线」和「这根没评估」在库里长得一样 —— 那种混淆无法事后补救。
        for b in bars:
            counts.setdefault(int(b["ts"]), 0)
        stats["n_touch_bars"] = st.set_n_touch(symbol, interval_ms, counts)
        after = st.reaction_counts(symbol, interval_ms)
        stats["persisted"] = written
        stats["db_before"] = {k: before[k] for k in ("rows", "bars")}
        stats["db_after"] = {k: after[k] for k in ("rows", "bars")}
        st.set_meta("reaction_params:%s:%s" % (symbol, L.period_label(interval_ms)),
                    stats["params"])
        st.set_meta("last_build_reaction:%s:%s" % (symbol, L.period_label(interval_ms)),
                    {"bars_in": stats.get("bars_in"), "rows": stats.get("rows"),
                     "persisted": written, "touch": stats.get("touch"),
                     "bars_without_touch": stats.get("bars_without_touch")})
    return stats


# ================================================================ 第 4 步：事件层
# 事件层从「表 0（线位）+ 表 1（K 线事实）+ 表 2（碰到几条线）」派生。
# 它**不**读原始行情重算线位 —— 理由同第 3 步：线当时画在哪只能以表 0 为准。


def event_params_snapshot(interval_ms: int) -> Dict:
    """第 4 步的口径参数。写进 kbar_meta，让库自己解释自己。"""
    from . import event as E
    return {
        "interval_ms": int(interval_ms),
        "interval_label": L.period_label(interval_ms),
        "vocab_source": "ai_trader/range/engine.py + 生产库 range_evolution_events",
        "vocab_all": list(E.EVENT_VOCAB_RANGE),
        "vocab_used": list(E.EVENT_VOCAB_USED),
        "vocab_not_applicable": list(E.EVENT_VOCAB_RANGE_ONLY),
        "vocab_not_applicable_reason":
            "这些事件的主语是「区间自身的生命周期」（成形/确认/定格/插针/R 滚动/反转），"
            "对一条 EMA、唐奇安、结构位线没有意义；而任何一条线被越过，就是突破本身",
        "cross_rule": "收盘价相对线价换边才给事件；同日不动不给（否则每根都是事件）",
        "revert_window_bars": E.REVERT_WINDOW_BARS,
        "revert_window_source": "沿用 range/engine.py 的 FORM_BARS=3（规范 v11 §2 成形窗口）；"
                                "15m 上是否该沿用见待确定 D-16",
        "prev_event_rule": "同一条线上一次的事件名（不是「前一根」）—— 回溯动态，"
                           "中间无意义的 K 不打断链条（设计稿 §8.1）",
        "since_event_idx_rule": "本根 idx - 上次事件 idx，idx = ts // interval_ms",
        "neutral_rule": "本根没有任何事件、且表 2 里没碰到任何线 → state=neutral。"
                        "§8.2 定义里还有「量正常、在空间中间」，那两项要阈值（判断标准），"
                        "本步不做（D-17）",
        "bar_event_rule": E.BAR_EVENT_RULE,
        "event_reaction_contract":
            "事件**不保证**在表 2 里有同 (根, 线) 的反应明细：跳空穿越时本根根本没碰到那条线，"
            "而表 2 按定义只收「碰到」的 (根,线)。要取 side/pen_depth/recover 必须 "
            "LEFT JOIN kbar_reaction 并按 (ts, line_key) 对齐，**不能 INNER JOIN** —— "
            "INNER JOIN 会静默丢掉这类事件（实测 716/18,813 条事件无明细，"
            "其中 377/7,299 根的表 1 on_line 落在无明细的那一侧）",
        "reaction_detail_coverage": "96.19%（18,097/18,813 条事件在表 2 有明细）",
        "undefined_by_design": ["pen_depth_pct(反包且未触线时)", "state(非 neutral 时)"],
        "undefined_reason": {
            "pen_depth_pct(反包且未触线时)":
                "跳空回到另一侧时这根没碰到线，穿透深度无定义，留空不硬算",
            "state(非 neutral 时)":
                "只区分「无事件无接触=neutral」与「有事件」；中间的「有接触无事件」"
                "留空而不是硬贴标签（设计稿 §8.2「强行贴标签就是制造噪声」）",
        },
        "source": "docs/K线演化-设计讨论稿.md §8 + docs/K线演化-待确定清单.md",
    }


def build_and_persist_events(kbar_db: str, symbol: str, interval_ms: int,
                             *, rebuild: bool = False, dry_run: bool = False) -> Dict:
    """从表 0 + 表 1 + 表 2 派生表 2.5（kbar_event），并把那几列写回表 1。"""
    from . import event as E
    stats: Dict = {"rows": 0, "persisted": 0}
    with KBarStore(kbar_db) as st:
        series = st.line_series(symbol, interval_ms)
        meta = st.line_meta(symbol, interval_ms)
        facts = st.evolution_rows(symbol, interval_ms)
        if not series:
            stats["error"] = "表 0 没有任何线，先跑 tools/backfill_kbar_lines.py"
            return stats
        if not facts:
            stats["error"] = "表 1 没有事实行，先跑 tools/backfill_kbar_facts.py"
            return stats

        bars = [{"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
                 "vol_rel": r["vol_rel"], "taker_buy_ratio": r["taker_buy_ratio"]}
                for r in facts if r["o"] is not None]
        bars.sort(key=lambda x: int(x["ts"]))

        # ---- 前置一致性：第 3 步到底在不在这些行上跑过？----
        # 为什么必须查：`build_and_persist_facts(rebuild=True)` 会 delete_evolution 整表，
        # 把第 3 步的 n_touch 和第 4 步这五列一起删掉。删完表 1 仍然"有数"
        # （O/H/L/C 都在），只有这几列变 NULL —— 和"测试绿但功能没接线"是同一个陷阱。
        # n_touch 的契约是「评估过的根必须有值（碰到 0 条写 0）」，所以空 == 没跑过。
        #
        # 注意：这里必须读 `facts`（SELECT * 的原始行），不能读下面自己挑键重建的
        # `bars` —— 那里面没有 n_touch，检查会永远通过（等于没检查）。
        missing_nt = [int(r["ts"]) for r in facts
                      if r["o"] is not None and r.get("n_touch") is None]
        if missing_nt:
            stats["error"] = (
                "表 1 有 %d/%d 根的 n_touch 是空的 —— 第 3 步没在这批行上跑过"
                "（典型原因：第 2 步用了 --rebuild 整表重算，把那几列一起删了）。"
                "先重跑 tools/backfill_kbar_reactions.py，再跑本步。例：ts=%s"
                % (len(missing_nt), len(bars), missing_nt[0]))
            return stats

        # neutral 判定要用「这根碰到几条线」，那个事实只存在于表 2
        touch_by_bar = st.touch_counts_by_bar(symbol, interval_ms)
        if not touch_by_bar:
            stats["error"] = ("表 2 没有反应行 —— 先跑 tools/backfill_kbar_reactions.py。"
                              "没有它就算不出「这根碰到几条线」，neutral 会被整体算错，"
                              "所以这里拒绝而不是猜")
            return stats
        # 第 3 步重跑过、但档数对不上 → 表 2 换过，n_touch 是新的，事件层却是旧的
        touched_bars = len(touch_by_bar)
        nt_positive = sum(1 for r in facts
                          if r["o"] is not None and (r.get("n_touch") or 0) > 0)
        if nt_positive != touched_bars:
            stats["error"] = (
                "表 1 里 n_touch>0 的根 = %d，表 2 覆盖的根 = %d，两者应相等。"
                "说明表 2 换过而表 1 的 n_touch 没跟着更新（或反过来）——"
                "第 3 步请整段重跑一次再跑本步" % (nt_positive, touched_bars))
            return stats

        rows, estats = E.build_events(symbol, interval_ms, bars, series, meta,
                                      touch_by_bar=touch_by_bar)
        stats.update(estats)
        stats["params"] = event_params_snapshot(interval_ms)
        stats["facts_bars"] = len(facts)
        stats["reaction_bars_with_touch"] = len(touch_by_bar)
        # 输入指纹：以后第 2/3 步若重跑而本步没重跑，靠它就能查出来（见 check_kbar_pipeline）
        fingerprint = {
            "line_rows": int(st.count("kbar_line", symbol, interval_ms)),
            "evolution_bars": len(facts),
            "reaction_rows": int(st.count("kbar_reaction", symbol, interval_ms)),
            "reaction_bars": touched_bars,
            "event_rows": int(len(rows)),
        }
        stats["input_fingerprint"] = fingerprint

        if dry_run:
            stats["dry_run"] = True
            return stats

        if rebuild:
            stats["deleted"] = st.delete_events(symbol, interval_ms)
        written = st.upsert_events(rows)
        stats["persisted"] = written

        by_ts = E.bar_event_columns(rows, bars, touch_by_bar=touch_by_bar)
        stats["bar_cols_written"] = st.set_bar_events(symbol, interval_ms, by_ts)
        after = st.event_counts(symbol, interval_ms)
        stats["db_after"] = after
        st.set_meta("event_params:%s:%s" % (symbol, L.period_label(interval_ms)),
                    stats["params"])
        st.set_meta("event_input_fingerprint:%s:%s"
                    % (symbol, L.period_label(interval_ms)), fingerprint)
        st.set_meta("last_build_event:%s:%s" % (symbol, L.period_label(interval_ms)),
                    {"bars_in": stats.get("bars_in"), "rows": written,
                     "by_event": stats.get("by_event"),
                     "bars_neutral": stats.get("bars_neutral"),
                     "bars_touch_no_event": stats.get("bars_touch_no_event")})
    return stats


# ================================================================ 第 5 步：事后层
# 事后层从「表 1（K 线 OHLC + 事件五列）+ 表 2.5（事件的判定线价）」派生。
# 它**允许**用之后的 K 线 —— 那是它的全部工作内容；也正因为如此，
# 它必须与当下判定路径物理隔开：单独一张表、单独一个进程、单独一个模块。
#
# 本函数只被 tools/backfill_kbar_forward.py 调用。
# 当下判定路径（第 1~4 步）不许引它，也不许读表 3 —— 有测试钉着这条红线。


def forward_params_snapshot(interval_ms: int) -> Dict:
    """第 5 步的口径参数。写进 kbar_meta，让库自己解释自己。"""
    from . import forward as W
    return {
        "interval_ms": int(interval_ms),
        "interval_label": L.period_label(interval_ms),
        "horizons": list(W.FORWARD_HORIZONS),
        "ret_basis": W.RET_BASIS,
        "ret_formula": "ret_k = c[t + k*interval] / c[t] - 1",
        "ret_fee": W.RET_FEE,
        "ret_fee_note": "当前不扣费（D-12 占位）。要扣费就改 forward.RET_FEE 并重跑本步",
        "ret_null_rule": "后面不足 k 根的留空 —— 不拿已走过的凑、不用最后一根顶替",
        "held_bars": int(W.HELD_BARS),
        "held_tol_pct": float(W.HELD_TOL_PCT),
        "held_rule": "之后 held_bars 根的收盘是否一直接在事件判定那一侧。"
                     "突破用事件自己的方向；反包用当根收盘所在侧",
        "held_ref_price_rule": "kbar_event.line_price（事件判定当时用的那个线价），"
                               "对之后的根**固定不变** —— 问的是「价格有没有回到这条水平线的"
                               "另一侧」，不是「线自己走到哪了」。另一种问法见 D-20",
        "held_null_rule": "后面不足 held_bars 根、或拿不到参照价、或当根收盘正好压在线上"
                          "（容差 0）时留空并计数",
        "isolation": "表 3 禁止参与当下判定：当下判定路径不 import forward 模块、不读本表。"
                     "由 tests/test_kbar_forward.py 扫代码验证",
        "audit_columns": {
            "held_line": "held 说的是哪条线（设计稿表 3 没有这列，为了可审计而加）",
            "held_ref_price": "判定用的固定线价",
            "held_side": "拿哪一侧去比（above / below）",
        },
        "undefined_by_design": ["held(无事件的根)"],
        "undefined_reason": {
            "held(无事件的根)": "「守住」的主语是某条线；这根没有事件就没有线，所以留空而不是填 0",
        },
        "source": "docs/K线演化-设计讨论稿.md §六 表 3 + docs/K线演化-待确定清单.md D-12/D-13/D-20",
    }


def build_and_persist_forward(kbar_db: str, symbol: str, interval_ms: int,
                              *, rebuild: bool = False, dry_run: bool = False,
                              horizons=None, held_bars: int = None) -> Dict:
    """算表 3（事后层）并落库。

    **独立进程调用**：`tools/backfill_kbar_forward.py`。
    不要从第 1~4 步的代码里调它（那就是让回测偷未来）。
    """
    from . import forward as W
    stats: Dict = {"rows": 0, "persisted": 0}
    hs = tuple(int(x) for x in (horizons or W.FORWARD_HORIZONS))
    hb = int(held_bars if held_bars is not None else W.HELD_BARS)

    with KBarStore(kbar_db) as st:
        facts = st.evolution_rows(symbol, interval_ms)
        if not facts:
            stats["error"] = "表 1 没有事实行，先跑 tools/backfill_kbar_facts.py"
            return stats

        bars = [{"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
                for r in facts if r["o"] is not None]
        bars.sort(key=lambda x: int(x["ts"]))
        if not bars:
            stats["error"] = "表 1 里没有一根有 OHLC 的 K 线"
            return stats

        # 间隙检查：ret_k = c[t+k*interval]/c[t] 只有等间隔才成立。
        # 缺一根就会把「隔了两根」算成「隔了一根」，收益整体偏大，且不报错。
        bad_gaps = []
        for i in range(1, len(bars)):
            if int(bars[i]["ts"]) - int(bars[i - 1]["ts"]) != int(interval_ms):
                bad_gaps.append((int(bars[i - 1]["ts"]), int(bars[i]["ts"])))
        if bad_gaps:
            stats["error"] = ("K 线不是等间隔（%d 处断口，例：%s → %s）。"
                              "ret_k 假设 t+k 根就是 t+k*interval，断了就不能算"
                              % (len(bad_gaps), bad_gaps[0][0], bad_gaps[0][1]))
            return stats

        # 事件列来自表 1（第 4 步的产物）；held 的参照价来自表 2.5。
        # ⚠ 只收 `event` 非空的根。`state='neutral'` 的根 event 是空的 ——
        #   它们**没有线**，也就没有「守住」可言。早先错写成 `or state is not None`，
        #   结果 6,067 根 neutral 被当成「有事件但拿不到参照价」，真值被埋掉。
        bar_events = {}
        missing_event_cols = 0
        for r in facts:
            if r["o"] is None:
                continue
            if r.get("event") is None:
                continue
            ts = int(r["ts"])
            bar_events[ts] = {"event": r.get("event"), "on_line": r.get("on_line")}
            if r.get("on_line") is None:
                missing_event_cols += 1
        if missing_event_cols:
            stats["error"] = ("有 %d 根 event 非空但 on_line 是空的 —— 表 1 的事件列不完整，"
                              "先重跑 tools/backfill_kbar_events.py" % missing_event_cols)
            return stats

        event_rows = st.events(symbol, interval_ms)
        if bar_events and not event_rows:
            stats["error"] = ("表 1 有事件但表 2.5 是空的 —— held 拿不到参照价。"
                              "先跑 tools/backfill_kbar_events.py")
            return stats

        rows, wstats = W.build_forward(symbol, interval_ms, bars, bar_events,
                                       event_rows=event_rows, horizons=hs,
                                       held_bars=hb)
        W.assert_no_lookahead(rows, bars)
        stats.update(wstats)
        stats["params"] = forward_params_snapshot(interval_ms)
        stats["bars_with_event"] = len(bar_events)

        if dry_run:
            stats["dry_run"] = True
            return stats

        if rebuild:
            stats["deleted"] = st.delete_forward(symbol, interval_ms)
        written = st.upsert_forward(rows)
        stats["persisted"] = written
        after = st.forward_counts(symbol, interval_ms)
        stats["db_after"] = {k: after[k] for k in ("rows", "ret1", "ret3", "ret5",
                                                   "held", "by_held")}
        st.set_meta("forward_params:%s:%s" % (symbol, L.period_label(interval_ms)),
                    stats["params"])
        st.set_meta("last_build_forward:%s:%s" % (symbol, L.period_label(interval_ms)),
                    {"bars_in": stats.get("bars_in"), "rows": written,
                     "held_1": stats.get("held_1"), "held_0": stats.get("held_0"),
                     "held_null": stats.get("held_null"),
                     "held_ref_missing": stats.get("held_ref_missing")})
    return stats


# ================================================================ 表 5：读数层
# 用户 2026-11-08：「所有演化结论字段或者描述都要入 db 库」。
# 判定本身在 shape.py（纯函数、不碰 IO、不 import forward）；这里只负责落库。
#
# ⚠ 与第 5 步的区别：本层**不**使用未来数据，所以它跟第 1~4 步在同一侧，
#   不受「不许 import forward」那条红线约束 —— 但它自己也**不许** import。


def shape_params_snapshot(interval_ms: int) -> Dict:
    """读数层的口径参数。写进 kbar_meta，让库自己解释自己。

    `params_hash` 是每行都带的那份指纹，方法在这里是为了「唯一出处」：
    指纹由 `shape.params()` 算，快照由它派生，改阈值不会出现两处不一致。
    """
    from . import shape as S
    return {
        "interval_ms": int(interval_ms),
        "interval_label": L.period_label(interval_ms),
        "params_hash": S.params_hash(),
        "thresholds": S.params(),
        "states": list(S.STATE_ORDER) + [S.STATE_NONE],
        "state_order_rule": "label 取 STATE_ORDER 里最靠前的那个；states 保留全部成立的，"
                            "按同一顺序排；都不成立时 label='无'",
        "pin_kinds": {
            S.PIN_KIND_WICK: "单根长影线（影线占幅 >= wick_pct_min 且 >= wick_pts_min）",
            S.PIN_KIND_SWING: "已确认的局部极值（往回 swing_lookback_bars 根的最值，"
                              "且冲进去的落差 >= wick_pts_min）。"
                              "⚠ 只用「到今天为止」的数据，不看之后",
        },
        "panic_rule": "量 >= panic_vol_rel_min 且 主动性占优 且 价格被打到一侧。"
                      "**不要求本根是针** —— 收在极值的大阴线没有影线，"
                      "但它照样是被按住了（2026-11-04 10:45 就是这根）",
        "panic_side_rule": "有影线听影线的，没影线才看收位。"
                           "2026-11-07 23:45 收位 77% 但下影 166 点 -> 判下压",
        "recovered_rule": "同根内：影线穿透某条线又收回（用上一根的线快照）",
        "cross_bar_recovered": "默认关闭。开着时全库 37% 的根都会被判「插针收回」，"
                               "那不是少数事件，说明测到的是「相邻根收在线另一侧」，"
                               "不是「针被收回」。见 D-21",
        "not_persisted_by_design": [],
        "lookahead_rule": "判第 i 根只许用 bars[0..i]。"
                          "工具/探针里各有独立复算验证这一条",
        "provisional": "全部阈值都是临时值（D-21），用户只确认过场景容差 20。"
                       "改 shape.py 顶部常量后必须重跑本步，否则表里混着两套口径",
        "source": "docs/K线演化-实现记录.md §三十八 + docs/K线演化-待确定清单.md D-21",
    }


def build_and_persist_shape(kbar_db: str, symbol: str, interval_ms: int,
                            *, rebuild: bool = False, dry_run: bool = False,
                            allow_swing: bool = True,
                            allow_cross_bar: bool = False) -> Dict:
    """算表 5（读数层结论）并落库。

    输入：表 1 的 OHLC + 量能、表 0 的线快照（判「穿透后收回」用）。
    输出：每根 K 线一行结论（label / states / 证据 / 口径指纹）。

    为什么从库里读表 0 而不是重算线：判「收回」必须用**当时那根线价**，
    重算会拿到「今天这组参数下的线」，与表 0 里那份历史快照未必一致。
    """
    from . import shape as S
    stats: Dict = {"rows": 0, "persisted": 0}

    with KBarStore(kbar_db) as st:
        facts = st.evolution_rows(symbol, interval_ms)
        if not facts:
            stats["error"] = "表 1 没有事实行，先跑 tools/backfill_kbar_facts.py"
            return stats

        bars = [{"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
                 "vol_rel": r.get("vol_rel"),
                 "taker_buy_ratio": r.get("taker_buy_ratio")}
                for r in facts if r["o"] is not None]
        bars.sort(key=lambda x: int(x["ts"]))
        if not bars:
            stats["error"] = "表 1 里没有一根有 OHLC 的 K 线"
            return stats

        ts_list = [int(b["ts"]) for b in bars]
        # 表 0 按 ts 分组；lines_at_bar[j] = 判第 j 根时能看到的那份快照
        #   = 表 0 里 ts = bars[j-1].ts 那一行（第 1 根没有前一根，给空）
        by_ts: Dict[int, Dict[str, float]] = {}
        for r in st.lines(symbol, interval_ms):
            p = r.get("line_price")
            if p is None:
                continue
            by_ts.setdefault(int(r["ts"]), {})[r["line_key"]] = float(p)
        lines_at_bar: Dict[int, Dict[str, float]] = {}
        for j in range(len(bars)):
            lines_at_bar[j] = {} if j == 0 else by_ts.get(ts_list[j - 1], {})

        # 「无」拆词（D-23.4）要 n_touch，**取表 1 落库值**，不在判定层重数
        # （自己数与落库值实测 14.3% 不一致）。bars 就是从 facts 过滤出来的，
        # 顺序一致，可以按下标对齐。
        nt = [r.get("n_touch") for r in facts if r["o"] is not None]
        js = [S.judge(bars, i, prev_lines=lines_at_bar.get(i),
                      lines_at_bar=lines_at_bar,
                      allow_swing=allow_swing,
                      allow_cross_bar=allow_cross_bar,
                      n_touch=nt[i] if i < len(nt) else None)
              for i in range(len(bars))]

        ph = S.params_hash()
        rows = []
        bad = [i for i, jd in enumerate(js) if any(
            x in S.LABEL_NO_ALL for x in jd.states)]
        assert not bad, "states 里混进了「无」：%d 根，例 ring %s" % (len(bad), bad[:3])
        for i, jd in enumerate(js):
            b, g = bars[i], S.geometry(bars[i]["o"], bars[i]["h"], bars[i]["l"],
                                       bars[i]["c"])
            # 收回的线：同根内那份 + （可选的）跨根那份，合并去重
            rec = S.recovered_lines(g, lines_at_bar.get(i))
            n_rec = len(rec)
            rec_txt = "|".join("%s@%.4f:%s" % (k, p, s) for k, p, s in rec) or None
            rows.append({
                "symbol": symbol, "interval_ms": int(interval_ms),
                "ts": int(b["ts"]),
                "label": jd.label,
                # states 里本来就不含「无」（见 shape.judge 的注释）。
                # 这里用断言而不是过滤：过滤会把上游的错悄悄吃掉。
                "states": ",".join(jd.states),
                "n_states": len(jd.states),
                "side": jd.side,
                "pin_kind": jd.pin_kind,
                "ref_pin_ts": jd.pin_ts,
                "ref_pin_kind": jd.ref_pin_kind,
                "ref_pin_side": jd.ref_pin_side,
                "rng": g.rng,
                "body_pct": g.body / g.rng,
                "up_wick_pct": g.up_pct,
                "dn_wick_pct": g.dn_pct,
                "close_pos": g.close_pos,
                "vol_rel": b["vol_rel"],
                "taker_buy_ratio": b["taker_buy_ratio"],
                "n_recovered": n_rec,
                "recovered": rec_txt,
                "evidence": "\n".join(jd.evidence),
                "params_hash": ph,
            })

        stats["bars_in"] = len(bars)
        stats["rows"] = len(rows)
        stats["params_hash"] = ph
        stats["by_label"] = S.counts_by_label(js)
        stats["pins"] = {
            "wick": sum(1 for j in js if j.pin_kind == S.PIN_KIND_WICK),
            "swing": sum(1 for j in js if j.pin_kind == S.PIN_KIND_SWING),
        }
        stats["ref_pins"] = {
            "wick": sum(1 for j in js if j.ref_pin_kind == S.PIN_KIND_WICK),
            "swing": sum(1 for j in js if j.ref_pin_kind == S.PIN_KIND_SWING),
        }
        stats["multi_state"] = sum(1 for j in js if len(
            [s for s in j.states if s != S.STATE_NONE]) > 1)
        stats["allow_cross_bar"] = bool(allow_cross_bar)
        stats["allow_swing"] = bool(allow_swing)
        stats["params"] = shape_params_snapshot(interval_ms)

        if dry_run:
            stats["dry_run"] = True
            return stats

        if rebuild:
            stats["deleted"] = st.delete_shape(symbol, interval_ms)
        written = st.upsert_shape(rows)
        stats["persisted"] = written
        after = st.shape_counts(symbol, interval_ms)
        stats["db_after"] = after
        if after["rows"] != stats["bars_in"]:
            stats["error"] = ("写完对不上：库里 %d 行 ≠ 表 1 的 %d 根"
                              % (after["rows"], stats["bars_in"]))
        if len(after["params_hashes"]) > 1:
            stats["error"] = ("表里混着 %d 套阈值指纹（%s）—— 必须 --rebuild 全表重算"
                              % (len(after["params_hashes"]),
                                 ",".join(after["params_hashes"])))
        st.set_meta("shape_params:%s:%s" % (symbol, L.period_label(interval_ms)),
                    stats["params"])
        # 判定规则版本（词表/规则）。params_hash 只认阈值、刻意不含逻辑，
        # 所以这一项单独记，否则「词表变了」在库里查不出来。
        st.set_meta("shape_rules:%s:%s" % (symbol, L.period_label(interval_ms)),
                    {"judge_rules_version": S.JUDGE_RULES_VERSION,
                     "label_vocab": list(S.LABEL_NO_ALL)})
        st.set_meta("last_build_shape:%s:%s" % (symbol, L.period_label(interval_ms)),
                    {"bars_in": stats["bars_in"], "rows": written,
                     "params_hash": ph,
                     "judge_rules_version": S.JUDGE_RULES_VERSION,
                     "by_label": stats["by_label"]})
    return stats


def load_shape(kbar_db: str, symbol: str, interval_ms: int,
               ts: Optional[int] = None) -> List[Dict]:
    """读表 5。`ts` 给了就读那一根。"""
    with KBarStore(kbar_db) as st:
        return st.shape_rows(symbol, interval_ms, ts=ts)


def build_and_persist_verify(kbar_db: str, symbol: str, interval_ms: int,
                             horizons=None, rebuild: bool = False,
                             dry_run: bool = False,
                             since_ts: Optional[int] = None) -> Dict:
    """第 6 步：拿表 5 的结论 + 后面的 K 线，回填表 6 `kbar_verify`。

    ⚠ 这是**唯一**允许读 `bars[i+1..]` 的地方。它读表 5 的判定结果，
    但判定路径（shape.py）从来不读它 —— 单向，表 5 是输入，表 6 是旁注。

    结论从**表 5 读回来**，不在这个函数里重判：重判会引入
    「表 6 说的结论和表 5 里的不是同一个」这种最坏的情况。

    `since_ts`：只重算这一段之后的行（增量）。旧行的未来 K 线已经固定，
    重算结果必然一样，所以跳过它们不损失任何信息 —— 但 `update_verify_tail`
    会拿全量结果对一遍，证明这个假设不是嘴说的。
    """
    from . import shape as S
    from . import verify as V
    stats: Dict = {"rows": 0, "persisted": 0,
                   "horizons": list(horizons or V.VERIFY_HORIZONS),
                   "since_ts": since_ts}

    with KBarStore(kbar_db) as st:
        facts = st.evolution_rows(symbol, interval_ms)
        if not facts:
            stats["error"] = "表 1 没有事实行"
            return stats
        bars = [{"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
                 "vol_rel": r.get("vol_rel"),
                 "taker_buy_ratio": r.get("taker_buy_ratio")}
                for r in facts if r["o"] is not None]
        bars.sort(key=lambda x: int(x["ts"]))
        if not bars:
            stats["error"] = "表 1 里没有一根有 OHLC 的 K 线"
            return stats

        shp = st.shape_rows(symbol, interval_ms)
        if not shp:
            stats["error"] = ("表 5 是空的，先跑 tools/backfill_kbar_shape.py "
                              "—— 佐证层读的是落库的判定，不自己重判")
            return stats
        by_ts = {int(r["ts"]): r for r in shp}
        idx_of = {int(b["ts"]): i for i, b in enumerate(bars)}

        rows = []
        no_pred = 0
        for b in bars:
            i = idx_of[int(b["ts"])]
            row = by_ts.get(int(b["ts"]))
            if row is None:
                continue
            if since_ts is not None and int(b["ts"]) < int(since_ts):
                continue
            # 用落库的状态搭一个轻量判定对象，避免重判导致前后不一致
            jd = S.Judgement(
                states=tuple(x for x in (row["states"] or "").split(",") if x),
                label=row["label"], side=row["side"],
                evidence=tuple((row["evidence"] or "").splitlines()),
                pin_ts=row["ref_pin_ts"], pin_kind=row["pin_kind"],
                ref_pin_kind=row["ref_pin_kind"],
                ref_pin_side=row["ref_pin_side"])
            pred = V.predict(bars, i, jd, ref_pin_side=row["ref_pin_side"])
            for h in stats["horizons"]:
                res = V.verify(bars, i, pred, int(h))
                if res.verdict == V.VERDICT_NO_PRED:
                    no_pred += 1
                rows.append({
                    "symbol": symbol, "interval_ms": int(interval_ms),
                    "ts": int(b["ts"]), "horizon": int(h),
                    "label": row["label"], "states": row["states"] or "",
                    "pred_kind": pred.kind, "pred_text": pred.text,
                    "verdict": res.verdict,
                    "ref_price": pred.ref_price,
                    "actual_price": res.actual_price,
                    "diff_pts": res.diff_pts,
                    "n_bars": res.n_bars,
                    "tol_pts": V.VERIFY_TOL_PTS,
                    "params_hash": row["params_hash"],
                    "verify_hash": V.verify_hash(),
                })

        stats["bars_in"] = len(bars)
        stats["shape_rows"] = len(shp)
        stats["rows"] = len(rows)
        stats["no_pred"] = no_pred
        stats["verify_hash"] = V.verify_hash()
        stats["tol_pts"] = V.VERIFY_TOL_PTS

        if dry_run:
            stats["dry_run"] = True
            return stats

        for h in stats["horizons"]:
            st.delete_verify(symbol, interval_ms, int(h), since_ts=since_ts)
        written = st.upsert_verify(rows)
        stats["persisted"] = written
        after = st.verify_counts(symbol, interval_ms)
        stats["db_after"] = after
        # 增量时：没动的旧行 + 本次重算的行 = 全量
        # ⚠ expect 不能拿 len(rows) 当基准 —— 那样是**同义反复**：
        #   rows 就是刚写进去的东西，拿它乘 N 去比自己永远相等，
        #   少写一根、丢掉一整段都拦不住（变异测试抓出来的）。
        #   基准必须取**表 5 里本该有结论的根数**，与本轮写了什么无关。
        if since_ts is None:
            before_rows = 0
        else:
            before_rows = st.count_rows("kbar_verify", symbol, interval_ms,
                                        older_than_ts=int(since_ts))
        want_bars = st.count_rows("kbar_shape", symbol, interval_ms,
                                  since_ts=since_ts)
        expect = before_rows + want_bars * len(stats["horizons"])
        stats["expect_rows"] = expect
        stats["want_bars"] = want_bars
        if after["rows"] != expect:
            stats["error"] = ("写完对不上：库里 %d 行 ≠ 旧 %d + 应有 %d 根 × %d 个 N = %d"
                              % (after["rows"], before_rows, want_bars,
                                 len(stats["horizons"]), expect))
        if len(after["verify_hashes"]) > 1:
            stats["error"] = ("表 6 混着 %d 套本层口径（%s）—— 必须清理重算"
                              % (len(after["verify_hashes"]),
                                 ",".join(after["verify_hashes"])))
        st.set_meta("verify_params:%s:%s" % (symbol, L.period_label(interval_ms)),
                    V.verify_params())
        st.set_meta("last_build_verify:%s:%s" % (symbol, L.period_label(interval_ms)),
                    {"bars_in": stats["bars_in"], "rows": written,
                     "verify_hash": V.verify_hash()})
    return stats


def load_verify(kbar_db: str, symbol: str, interval_ms: int,
                ts: Optional[int] = None,
                horizon: Optional[int] = None) -> List[Dict]:
    """读表 6。`ts` 给了就读那一根的各 N。"""
    with KBarStore(kbar_db) as st:
        return st.verify_rows(symbol, interval_ms, ts=ts, horizon=horizon)


def build_and_persist_survival(kbar_db: str, symbol: str, interval_ms: int,
                               max_n: Optional[int] = None,
                               dry_run: bool = False,
                               since_ts: Optional[int] = None) -> Dict:
    """第 6 步之二：回填表 6b `kbar_verify_survival` —— 一句话「活了多久」。

    和表 6 的关系：表 6 在固定的 N=1/3 上给裁决，本表给**第几根第一次破**。
    后者信息量更大，也修掉一个结构性偏差：

        守住型（不该破）在短 N 下偏松 —— 还没来得及破就算成立
        会破型（该破）  在短 N 下偏严 —— 还没来得及破就算推翻

    两类预言放在同一个 N 上比命中率本来就不成立，存活根数把这个混淆消掉。

    `since_ts`：只重算这一段（增量）。前面的根未来已固定，重算结果必然一样。
    """
    from . import shape as S
    from . import verify as V
    mn = int(max_n or V.SURVIVAL_MAX_N)
    stats: Dict = {"rows": 0, "persisted": 0, "max_n": mn,
                   "since_ts": since_ts}

    with KBarStore(kbar_db) as st:
        facts = st.evolution_rows(symbol, interval_ms)
        bars = [{"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
                 "vol_rel": r.get("vol_rel"),
                 "taker_buy_ratio": r.get("taker_buy_ratio")}
                for r in (facts or []) if r["o"] is not None]
        bars.sort(key=lambda x: int(x["ts"]))
        if not bars:
            stats["error"] = "表 1 里没有一根有 OHLC 的 K 线"
            return stats
        shp = st.shape_rows(symbol, interval_ms)
        if not shp:
            stats["error"] = ("表 5 是空的，先跑 tools/backfill_kbar_shape.py")
            return stats
        by_ts = {int(r["ts"]): r for r in shp}
        idx_of = {int(b["ts"]): i for i, b in enumerate(bars)}

        rows = []
        by_status: Dict = {}
        for b in bars:
            ts = int(b["ts"])
            if since_ts is not None and ts < int(since_ts):
                continue
            row = by_ts.get(ts)
            if row is None:
                continue
            i = idx_of[ts]
            jd = S.Judgement(
                states=tuple(x for x in (row["states"] or "").split(",") if x),
                label=row["label"], side=row["side"],
                evidence=tuple((row["evidence"] or "").splitlines()),
                pin_ts=row["ref_pin_ts"], pin_kind=row["pin_kind"],
                ref_pin_kind=row["ref_pin_kind"],
                ref_pin_side=row["ref_pin_side"])
            pred = V.predict(bars, i, jd, ref_pin_side=row["ref_pin_side"])
            sv = V.first_break(bars, i, pred, max_n=mn)
            by_status[sv.status] = by_status.get(sv.status, 0) + 1
            rows.append({
                "symbol": symbol, "interval_ms": int(interval_ms), "ts": ts,
                "label": row["label"], "pred_kind": pred.kind,
                "must_hold": 1 if pred.must_hold else 0,
                "status": sv.status, "break_n": sv.break_n,
                "n_seen": sv.n_seen, "max_n": sv.max_n,
                "ref_price": pred.ref_price,
                "actual_at_break": sv.actual_at_break,
                "tol_pts": V.VERIFY_TOL_PTS,
                "params_hash": row["params_hash"],
                "survival_hash": V.survival_hash(),
            })

        stats["rows"] = len(rows)
        stats["by_status"] = by_status
        stats["survival_hash"] = V.survival_hash()
        if dry_run:
            stats["dry_run"] = True
            return stats

        st.delete_survival(symbol, interval_ms, since_ts=since_ts)
        stats["persisted"] = st.upsert_survival(rows)
        after = st.survival_counts(symbol, interval_ms)
        stats["db_after"] = after
        before_rows = (0 if since_ts is None else
                       st.count_rows("kbar_verify_survival", symbol,
                                     interval_ms, older_than_ts=int(since_ts)))
        # 同上：基准取表 5 应有的根数，不拿 len(rows) 自我比对
        want_bars = st.count_rows("kbar_shape", symbol, interval_ms,
                                  since_ts=since_ts)
        expect = before_rows + want_bars
        stats["expect_rows"] = expect
        stats["want_bars"] = want_bars
        if after["rows"] != expect:
            stats["error"] = ("写完对不上：库里 %d 行 ≠ 旧 %d + 应有 %d 根 = %d"
                              % (after["rows"], before_rows, want_bars, expect))
        if len(after["survival_hashes"]) > 1:
            stats["error"] = ("表 6b 混着 %d 套口径（%s）—— 必须清理重算"
                              % (len(after["survival_hashes"]),
                                 ",".join(after["survival_hashes"])))
        st.set_meta("survival_params:%s:%s"
                    % (symbol, L.period_label(interval_ms)),
                    V.survival_params())
    return stats


def build_and_persist_dims(kbar_db: str, symbol: str, interval_ms: int,
                           *, dry_run: bool = False,
                           since_ts: Optional[int] = None) -> Dict:
    """算表 7（维度层 / 环境快照）并落库。

    **这一层不参与判定**：它不改结论词、不改阈值、不改表 5/6/6b 任何一行。
    它只把 39 个维度摊成一根一行，让人能读到「那根 K 线当时的环境」。

    输入：表 1（事实）+ 表 2（反应，取「穿透最深的那条线」）+ 表 5（label，只用来
    带上结论词便于并排看，**不回写**）。

    为什么按 `since_ts` 增量是安全的：一根的维度只依赖它自己的表 1 行、
    它自己的表 0 线快照、它自己的表 2 反应行 —— 全是同根数据，没有前视窗口。
    所以「重算一根」和「重算全部」对这根的结果必然相同，不像表 6/6b 需要尾部。
    """
    from . import dims as D
    stats: Dict = {"rows": 0, "persisted": 0, "n_dims": len(D.DIMS)}
    dh = D.dims_hash()
    stats["dims_hash"] = dh

    with KBarStore(kbar_db) as st:
        facts = st.evolution_rows(symbol, interval_ms)
        if not facts:
            stats["error"] = "表 1 没有事实行，先跑 tools/backfill_kbar_facts.py"
            return stats
        bars = [r for r in facts if r["o"] is not None]
        bars.sort(key=lambda x: int(x["ts"]))
        if not bars:
            stats["error"] = "表 1 里没有一根有 OHLC 的 K 线"
            return stats

        # 表 2 按 ts 分组 —— 每根只需要自己那几行反应
        by_ts_react: Dict[int, List[Dict]] = {}
        for r in st.reactions(symbol, interval_ms):
            by_ts_react.setdefault(int(r["ts"]), []).append(r)

        labels = {}
        for r in st.shape_rows(symbol, interval_ms):
            labels[int(r["ts"])] = r.get("label")

        rows = []
        for b in bars:
            ts = int(b["ts"])
            if since_ts is not None and ts < int(since_ts):
                continue
            ctx = {"bar": b, "reactions": by_ts_react.get(ts, [])}
            snap = D.snapshot(ctx)
            lab = labels.get(ts)
            notes = ([n.as_dict() for n in
                      D.corroborate(lab, snap)] if lab else [])
            vals = snap["values"]
            rows.append({
                "symbol": symbol, "interval_ms": int(interval_ms), "ts": ts,
                "label": lab,
                "n_dims": len(vals),
                "n_filled": sum(1 for x in vals.values() if x is not None),
                "values_json": json.dumps(vals, ensure_ascii=False,
                                          sort_keys=True),
                "buckets_json": json.dumps(snap["buckets"],
                                           ensure_ascii=False, sort_keys=True),
                "notes_json": json.dumps(notes, ensure_ascii=False,
                                         sort_keys=True),
                "dims_hash": dh,
            })
        stats["rows"] = len(rows)
        stats["by_label"] = dict(Counter(r["label"] for r in rows))
        stats["n_notes"] = sum(len(json.loads(r["notes_json"])) for r in rows)
        if dry_run:
            return stats

        # 增量时把这段先删再插，避免 upsert 覆盖不到已不存在的行（例如某根
        # 的反应行被重建后变少了，upsert 只会更新、不会把旧值清掉）。
        if since_ts is not None:
            st.delete_dims(symbol, interval_ms, since_ts=since_ts)
        st.upsert_dims(rows)

        after = st.dims_counts(symbol, interval_ms)
        # 基准必须是「有 OHLC 的根数」，不是表 1 的全部行 —— 本层只给有 OHLC 的
        # 根做快照（没 OHLC 算不出几何），两者在数据没缺口时相等，但不能假设。
        want = len(bars)
        stats["persisted"] = len(rows)
        stats["total"] = after["rows"]
        # 不变量：表 7 一根一行，所以行数必须等于表 1 的根数。
        # ⚠ 不要写成「旧行数 + 本次行数」—— upsert 是幂等的，重复跑全量时
        #   旧行数已经是满的，那样算会得出 600 ≠ 300 + 300 这种假告警
        #   （本层第一版就是这么错的，被 test_build_persists_one_row_per_bar 抓到）。
        if after["rows"] != want:
            stats["error"] = ("表 7 行数 %d ≠ 表 1 的 %d 根 —— "
                              "一根 K 线只该有一份快照" % (after["rows"], want))
        if len(after["dims_hashes"]) > 1:
            stats["error"] = ("表 7 混着 %d 套口径（%s）"
                              % (len(after["dims_hashes"]),
                                 ",".join(after["dims_hashes"])))
        st.set_meta("dims_params:%s:%s" % (symbol, L.period_label(interval_ms)),
                    D.dims_params())
    return stats


def update_verify_tail(kbar_db: str, symbol: str, interval_ms: int) -> Dict:
    """**实时跟随**：新 K 线进来后，只重算「还可能变」的那几根。

    为什么只有尾部会变：
        表 6 / 表 6b 的行只依赖 `bars[i+1 .. i+N]`。一旦 i+N 已经落在
        历史里，这个窗口就固定了，重算必然得到同一个结果。
        所以只有 `i + N > 最后一根` 的行还可能从「无法判定/未成熟」
        翻成定局 —— 那正好是最后 N 根。

    两条断言，防止「只算尾部」变成「悄悄漏了一段」：
        1. 尾部入口必须连续（不能有空档）
        2. 增量跑完后的全量行数必须等于「旧行 + 本次重算行数」
    """
    from . import verify as V
    tail = max(list(V.VERIFY_HORIZONS) + [int(V.SURVIVAL_MAX_N)])
    out: Dict = {"tail_bars": tail}
    with KBarStore(kbar_db) as st:
        facts = st.evolution_rows(symbol, interval_ms)
        ts_all = sorted(int(r["ts"]) for r in (facts or []) if r["o"] is not None)
        if not ts_all:
            out["error"] = "表 1 里没有一根有 OHLC 的 K 线"
            return out
        n = len(ts_all)
        # 窗口完整的条件：i + tail <= n-1  ⇔  i <= n-1-tail。
        # 所以「最后一根已经定型的根」正好是 i = n-1-tail，而它是在
        # **最新这一根到达的那一刻**才刚定型的 —— 必须算进去，不许排除在外。
        # 边界因此是 n-tail-1，不是 n-tail：
        #   取 n-tail   → 把刚定型的那根漏掉，它会永远停在「未成熟」；
        #   取 n-tail-2 → 只是白算一根，无害。
        since_idx = max(0, n - (tail + 1))
        since_ts = ts_all[since_idx]
        out["bars"] = n
        out["since_ts"] = since_ts
        out["recompute_bars"] = n - since_idx
    out["verify"] = build_and_persist_verify(
        kbar_db, symbol, interval_ms, since_ts=since_ts)
    out["survival"] = build_and_persist_survival(
        kbar_db, symbol, interval_ms, since_ts=since_ts)
    return out


def load_survival(kbar_db: str, symbol: str, interval_ms: int,
                  ts: Optional[int] = None,
                  status: Optional[str] = None) -> List[Dict]:
    """读表 6b。`status` 给了就只读那一态。"""
    with KBarStore(kbar_db) as st:
        return st.survival_rows(symbol, interval_ms, ts=ts, status=status)
