# -*- coding: utf-8 -*-
"""从 Binance REST 补 5m K 线到 market.db 的 `bars` 表。

## 为什么需要这个工具

`market.db` 是 K 线演化的**唯一输入源**。5m 直到 2026-11-09 才进
`config.INTERVALS_MS` 成为原生周期（由实时采集器从 3s 桶聚合后落库），
在那之前的 5m 从来没有被实时写过 —— 只能从 1m 聚合（而 1m 只有约 21 小时）
或者从交易所拉。所以 11-01 之前那段 5m **必须回填**，否则：

* `ingest_kbar --interval-ms 300000` 的输入源是空的（现在会硬失败，见 P10）；
* 或者更糟：等 11-09 之后原生 5m 出现，增量会把 `ts > 11-01` 的行全灌进去，
  5m 演化层从「截断」变成「中间 7 天空洞」，而 EMA/唐奇安跨洞递推
  **不报错、只算错**。

## 为什么不直接补 kbar.db

因为源库仍是断的：下一次增量会再撞同一个洞。补源库才是一次性的。

## 用法

    # 看会补哪些洞（不联网、不写库）
    python tools/backfill_5m_bars.py --dry-run

    # 真正回填：补齐 market.db 里 5m 的所有洞，一直补到最新
    python tools/backfill_5m_bars.py --fill-gaps

    # 只补某一段（UTC 毫秒）
    python tools/backfill_5m_bars.py --from 1793400000000 --to 1794000000000

## 设计要点（都是踩过的坑）

1. **fetcher 可注入**。本地/CI 没有外网，测试用假 fetcher；
   生产用 `BinanceFetcher`。网络失败要说人话，不许甩栈。
2. **幂等**。按 `(symbol, interval_ms, open_time)` upsert，
   重复跑不会凭空多行，也不会改动已有行的 OHLC。
3. **翻页**。`limit` 最大 1500，按 `startTime` 递增翻，直到拿满或到 `endTime`。
4. **写完自检**。补完必须验证「等间隔」，并把「还剩几处洞」打印出来 ——
   不验证的话，这个工具本身就会变成下一个「看起来成功」。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B            # noqa: E402
from ai_trader.kbar import lines as L           # noqa: E402

IV_5M = 300_000
MAX_LIMIT = 1500            # Binance 单次上限


# ------------------------------------------------------------------ fetcher

class FetchError(RuntimeError):
    """拿不到数据。生产是网络问题，测试是假 fetcher 故意失败。"""


class BinanceFetcher:
    """最小 REST 实现。只依赖标准库，不引入新依赖。"""

    BASE = "https://fapi.binance.com/fapi/v1/klines"

    def __init__(self, base: str = BASE, timeout: float = 15.0,
                 retries: int = 3, sleep=time.sleep):
        self.base = base
        self.timeout = float(timeout)
        self.retries = int(retries)
        self._sleep = sleep

    def fetch(self, symbol: str, interval: str, start_ms: int, end_ms: int,
              limit: int = MAX_LIMIT):
        """返回 [[open_time, o, h, l, c, v, close_time, ...], ...]，按时间升序。

        Binance 的 klines 返回**包含** startTime 那根，endTime 是闭区间。
        """
        url = ("%s?symbol=%s&interval=%s&startTime=%d&endTime=%d&limit=%d"
               % (self.base, symbol, interval, int(start_ms), int(end_ms),
                  int(min(limit, MAX_LIMIT))))
        last_err = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:                     # pragma: no cover
                last_err = e
                if attempt + 1 < self.retries:
                    self._sleep(1.0 * (attempt + 1))
        raise FetchError("拉 %s %s 失败（startTime=%d）：%s: %s"
                         % (symbol, interval, start_ms,
                            type(last_err).__name__, last_err))


def _parse_klines(raw, interval_ms: int, now_ms=None):
    """Binance 行 → (ts, o, h, l, c, v)。顺手丢掉还没收盘的那根。

    ★ 未收盘的 K 线绝不能落库：它的 h/l/c 还会变，进了库就成了
      「最后一根永远不对」的假数据，而且下次增量不会重算它
      （增量只处理 `ts > since`）。Binance 每行的 close_time 给的是
      「收盘时刻 - 1ms」，所以 close_time + 1 == 该根的开盘 ts + 周期。

    now_ms 可注入：不注入就没法测「未来那根被丢掉」，
    而这条恰好是「测试没法断言」的典型。
    """
    out = []
    skipped_forming = 0
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    for row in raw or ():
        try:
            ts = int(row[0])
            o, h, lo, c, v = (float(row[1]), float(row[2]), float(row[3]),
                              float(row[4]), float(row[5]))
            close_time = int(row[6])
        except (IndexError, TypeError, ValueError):
            continue
        if close_time + 1 > now:
            skipped_forming += 1
            continue
        out.append((ts, o, h, lo, c, v))
    out.sort(key=lambda x: x[0])
    return out, skipped_forming


# ------------------------------------------------------------------ market.db

def _open_rw(path: str) -> sqlite3.Connection:
    """建表语句必须和**生产 schema 一致**，否则新建库能写、生产库写不进。

    ★ `updated_at INTEGER NOT NULL` 且**没有默认值**（生产就是这样）。
      这个字段漏写会触 NOT NULL 违反 —— 而 `INSERT OR IGNORE` 会把它
      静默吞掉，然后报告「已存在跳过 N 行」。实测踩过：
      2023-11-15 那根「新增 0 行，已存在跳过 1 行」，库里其实一根都没进。
      所以下面写库用 `ON CONFLICT DO NOTHING`（只忽略主键冲突），
      其它约束违反一律抛出。
    """
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS bars ("
        " symbol TEXT NOT NULL DEFAULT 'BTCUSDT',"
        " interval_ms INTEGER NOT NULL,"
        " open_time INTEGER NOT NULL,"
        " o REAL, h REAL, l REAL, c REAL,"
        " v REAL, q REAL, n INTEGER, b REAL, s REAL,"
        " updated_at INTEGER NOT NULL,"
        " PRIMARY KEY (symbol, interval_ms, open_time))")
    return con


def existing_5m_ts(path: str, symbol: str, interval_ms: int = IV_5M):
    """已有行的 ts 集合（只读，用来算洞和算「不覆盖已有行」）。"""
    con = B.open_ro(path)
    try:
        rows = con.execute(
            "SELECT open_time FROM bars WHERE symbol=? AND interval_ms=? "
            "ORDER BY open_time", (symbol, int(interval_ms))).fetchall()
    finally:
        con.close()
    return {int(r[0]) for r in rows}


def find_holes(path: str, symbol: str, interval_ms: int = IV_5M):
    """返回洞列表 [(洞前一根 ts, 洞后一根 ts, 缺几根)]。

    只在**已有数据范围之内**找洞；首尾之外不算洞（那是「补到最新」的事）。
    """
    ts_list = sorted(existing_5m_ts(path, symbol, interval_ms))
    holes = []
    for i in range(1, len(ts_list)):
        d = ts_list[i] - ts_list[i - 1]
        if d != int(interval_ms):
            missing = d // int(interval_ms) - 1
            if missing > 0:
                holes.append((ts_list[i - 1], ts_list[i], missing))
    return holes


def write_bars(path: str, symbol: str, interval_ms: int, rows,
               overwrite: bool = False, now_ms=None) -> dict:
    """幂等写入。返回 {written, skipped_existing}。

    默认 `ON CONFLICT ... DO NOTHING`：已有行**一律不动**。理由是这些行可能是
    实时采集器写的等价数据，而已落库的下游已经基于它算过；回填工具的职责是
    「补空」，不是「重写历史」。真要重写请显式 `--overwrite`。

    ★ 这里**绝对不能**用 `INSERT OR IGNORE`：OR IGNORE 会把 NOT NULL /
      CHECK 违反也一起吞掉，于是「一根都没写进去」会被报告成
      「已存在跳过」—— 工具自己在骗人。实测踩过一次（updated_at NOT NULL）。
    """
    if not rows:
        return {"written": 0, "skipped_existing": 0}
    updated_at = int(now_ms if now_ms is not None else time.time() * 1000)
    con = _open_rw(path)
    try:
        before = con.total_changes
        if overwrite:
            sql = ("INSERT INTO bars"
                   "(symbol,interval_ms,open_time,o,h,l,c,v,updated_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?)"
                   " ON CONFLICT(symbol,interval_ms,open_time) DO UPDATE SET"
                   " o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c,"
                   " v=excluded.v, updated_at=excluded.updated_at")
        else:
            sql = ("INSERT INTO bars"
                   "(symbol,interval_ms,open_time,o,h,l,c,v,updated_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?)"
                   " ON CONFLICT(symbol,interval_ms,open_time) DO NOTHING")
        con.executemany(
            sql,
            [(symbol, int(interval_ms), int(ts), o, h, lo, c, v, updated_at)
             for ts, o, h, lo, c, v in rows])
        con.commit()
        written = con.total_changes - before
    finally:
        con.close()
    return {"written": written,
            "skipped_existing": len(rows) - written}


# ------------------------------------------------------------------ 编排

def _plan_ranges(holes, interval_ms, from_ms, to_ms, pad_bars=2):
    """把洞（和显式区间）变成拉取区间。

    * 洞：前后各留 `pad_bars` 根，这样补回来的接缝能和已有行对上（便于验证）。
    * 显式 --from/--to：**不加 pad**。用户要哪段就是哪段，
      多写几根用户没要的行会让「written」对不上账，也让 --overwrite 变得危险。
    """
    iv = int(interval_ms)
    pad = int(pad_bars) * iv
    out = []
    for a, b, _n in holes:
        out.append((max(0, a + iv - pad), b - iv + pad))
    if from_ms is not None or to_ms is not None:
        lo = int(from_ms) if from_ms is not None else 0
        hi = int(to_ms) if to_ms is not None else lo
        out.append((lo, hi))
    return out


def run(fetcher, market_db: str, symbol: str, interval: str = "5m",
        interval_ms: int = IV_5M, *, from_ms=None, to_ms=None,
        fill_gaps: bool = False, overwrite: bool = False,
        dry_run: bool = False, max_pages: int = 2000,
        now_ms=None, out=print) -> dict:
    """主流程。fetcher 只要有 `.fetch(symbol, interval, start, end, limit)`。

    返回统计字典；`ok=False` 表示**没有达到目标**（洞还在 / 没拉到数据）。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    # from / to 归一：只给 from 就是「补到现在」；只给 to 就是「从头补到 to」。
    # 不归一的话 to=None 会被当成 hi=lo，等于只补一根 —— 不报错，只是没干活。
    if from_ms is not None and to_ms is None:
        to_ms = now_ms
    if to_ms is not None and from_ms is None:
        from_ms = 0

    stats = {"ok": False, "pages": 0, "fetched": 0, "written": 0,
             "skipped_existing": 0, "skipped_forming": 0,
             "holes_before": [], "holes_after": [], "target_ranges": [],
             "error": ""}

    if not os.path.exists(market_db):
        stats["error"] = "找不到 market.db：%s" % market_db
        out("✗ %s" % stats["error"])
        return stats

    holes = find_holes(market_db, symbol, interval_ms)
    stats["holes_before"] = holes
    out("现有 5m 洞：%d 处" % len(holes))
    for a, b, n in holes[:10]:
        out("  %s → %s 缺 %d 根" % (_fmt(a), _fmt(b), n))
    if len(holes) > 10:
        out("  …共 %d 处" % len(holes))

    ts_all = sorted(existing_5m_ts(market_db, symbol, interval_ms))
    ranges = []
    if fill_gaps:
        ranges.extend(_plan_ranges(holes, interval_ms, None, None))
        # ★ --fill-gaps 的承诺是「补洞并一直补到最新」。
        #   只补洞的话，尾巴仍然是旧的，等于把「停了 7 天」修成「停在今天」，
        #   而新鲜度闸会因此变绿 —— 那是最糟的一种「修好了」。
        if ts_all:
            tail_start = ts_all[-1] + int(interval_ms)
            if tail_start < now_ms:
                ranges.append((ts_all[-1], now_ms))
                out("尾巴：%s 之后要补到最新" % _fmt(ts_all[-1]))
        else:
            # 只有「没给 --from/--from-kbar」时才算无从下手；否则下面会从
            # 那个起点起补，这时候说「无从下手」是自相矛盾。
            if from_ms is None:
                out("⚠ 表里一根 5m 都没有，--fill-gaps 无从下手；"
                    "请用 --from/--to 或 --from-kbar。")
    if from_ms is not None or to_ms is not None:
        ranges.extend(_plan_ranges([], interval_ms, from_ms, to_ms))
    if not ranges:
        stats["error"] = ("没有什么可补：要么没有洞且尾巴已到最新，"
                          "要么没给 --from/--to。")
        out("✗ %s" % stats["error"])
        return stats

    # 区间合并，避免重复拉
    ranges.sort()
    merged = []
    for a, b in ranges:
        if merged and a <= merged[-1][1] + interval_ms:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    stats["target_ranges"] = merged
    out("目标区间：%d 段" % len(merged))
    for a, b in merged[:10]:
        out("  %s → %s" % (_fmt(a), _fmt(b)))

    if dry_run:
        out("--dry-run：不联网、不写库。")
        stats["ok"] = True
        return stats

    all_rows = []
    forming_ts = set()      # 去重：翻页会重复取到同一根未收盘的 K
    seen_ts = set()         # 本次已经写过的 ts，避免跨页重复计数
    cursor = merged[0][0] if merged else 0
    pages = 0
    try:
        for a, b in merged:
            cursor = a
            while cursor <= b and pages < int(max_pages):
                raw = fetcher.fetch(symbol, interval, cursor, b, MAX_LIMIT)
                pages += 1
                stats["pages"] = pages
                rows, forming = _parse_klines(raw, interval_ms, now_ms)
                if forming:
                    for r in raw or ():
                        try:
                            if int(r[6]) + 1 > now_ms:
                                forming_ts.add(int(r[0]))
                        except (IndexError, TypeError, ValueError):
                            continue
                stats["skipped_forming"] = len(forming_ts)
                if not rows:
                    break
                stats["fetched"] += len(rows)
                # ★ 边拉边写。36 页的任务在抖动网络上跑到一半断了，
                #   攒在内存里最后一次性写 = 前面拉到的全丢（实测踩过一次：
                #   第 N 页 URLError 之后整轮 written=0）。逐页落库之后这个工具
                #   天然可重入：再跑一次接着补，幂等 insert 不会多一行。
                fresh = [r for r in rows if r[0] not in seen_ts]
                seen_ts.update(r[0] for r in fresh)
                if fresh:
                    all_rows.extend(fresh)
                    w = write_bars(market_db, symbol, interval_ms, fresh,
                                   overwrite=overwrite, now_ms=now_ms)
                    stats["written"] += w["written"]
                    stats["skipped_existing"] += w["skipped_existing"]
                last = rows[-1][0]
                if last <= cursor:      # 防死循环：没有前进就停
                    break
                cursor = last + int(interval_ms)
    except FetchError as e:
        stats["error"] = str(e)
        stats["stopped_at"] = cursor
        out("✗ %s" % e)
        out("  已落库 %d 行（逐页写入），重新跑一次会接着补剩下的。"
            % stats["written"])
        # 不 return：下面仍要查一遍洞，好让人知道现在缺多少、缺在哪。

    out("拉取：%d 页 / %d 根（丢掉未收盘 %d 根）"
        % (stats["pages"], stats["fetched"], stats["skipped_forming"]))
    out("写库：新增 %d 行，已存在跳过 %d 行"
        % (stats["written"], stats["skipped_existing"]))

    # ---- 自检：洞还在不在 ----
    after = find_holes(market_db, symbol, interval_ms)
    stats["holes_after"] = after
    if stats["error"]:
        # 网络中断的轮次不算成功，但**已经拉到的数据也不能算没做**。
        stats["ok"] = False
        out("→ 本轮未完成（%s），但已落库的数据是有效的。" % stats["error"])
        return stats
    if after:
        out("✗ 补完仍有 %d 处洞（这个工具本身不能「看起来成功」）：" % len(after))
        for a, b, n in after[:10]:
            out("  %s → %s 缺 %d 根" % (_fmt(a), _fmt(b), n))
        stats["ok"] = False
    else:
        ts_list = sorted(existing_5m_ts(market_db, symbol, interval_ms))
        if ts_list:
            out("✓ 5m 已连续：%d 根，%s → %s"
                % (len(ts_list), _fmt(ts_list[0]), _fmt(ts_list[-1])))
        stats["ok"] = True
    return stats


def _fmt(ms) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime(
            "%Y-%m-%d %H:%M")
    except Exception:                                  # pragma: no cover
        return str(ms)


def kbar_earliest_ts(kbar_db: str, symbol: str, interval_ms: int = IV_5M):
    """kbar.db 里「下游最早需要 5m 的地方」—— 取 5m 与 15m 里更早的那个。

    ★ 为什么不是「5m 有就取 5m 的起点」：本地 5m 演化层从 07-04 开始、
      15m 从 05-08 开始。只补到 07-04，前面那两个月里的 15m 计划
      算 amp5 / 止损下限时就没有 5m 可用，会静默退化成只有 1h 口径。
      起点必须覆盖到 15m 的历史，才谈得上「补平」。
    """
    if not os.path.exists(kbar_db):
        return None
    con = sqlite3.connect("file:%s?mode=ro" % kbar_db, uri=True)
    try:
        got = []
        for iv in (int(interval_ms), 900_000):
            row = con.execute(
                "SELECT MIN(ts) FROM kbar_evolution WHERE symbol=? AND interval_ms=?",
                (symbol, iv)).fetchone()
            if row and row[0]:
                got.append(int(row[0]))
    finally:
        con.close()
    return min(got) if got else None


def _argp(argv=None):
    ap = argparse.ArgumentParser(
        description="从 Binance REST 补 5m K 线到 market.db.bars")
    ap.add_argument("--market-db", default=B.DEFAULT_MARKET_DB)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval-ms", type=int, default=IV_5M,
                    help="目前只建议 300000（5m）；其它周期先确认实时源在写")
    ap.add_argument("--fill-gaps", dest="fill_gaps", action="store_true",
                    help="补齐已有的所有洞，并一直补到最新")
    ap.add_argument("--from", dest="from_ms", type=int, default=None,
                    help="起始 open_time（UTC 毫秒）")
    ap.add_argument("--to", dest="to_ms", type=int, default=None,
                    help="结束 open_time（UTC 毫秒）；不给就是「补到现在」")
    ap.add_argument("--kbar-db", default=B.DEFAULT_KBAR_DB or "data/kbar.db")
    ap.add_argument("--from-kbar", dest="from_kbar", action="store_true",
                    help="起点取 kbar.db 里该周期最早的一根 ts（"
                         "不想到手算毫秒就用这个）")
    ap.add_argument("--overwrite", action="store_true",
                    help="★ 覆盖已有行（默认不动已有行，只补空）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印要补什么，不联网不写库")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _argp(argv)
    label = L.period_label(args.interval_ms)
    print("=" * 72)
    print("补 %s K 线  %s  %s" % (label, args.symbol, args.market_db))
    print("=" * 72)
    if args.interval_ms != IV_5M:
        print("⚠ 目前只有 5m 需要回填（其它周期本来就有实时源）。"
              "继续会往 market.db 写 %s 行，确认这是你要的。" % label)

    from_ms, to_ms = args.from_ms, args.to_ms
    if args.from_kbar and from_ms is None:
        from_ms = kbar_earliest_ts(args.kbar_db, args.symbol, args.interval_ms)
        if from_ms is None:
            print("✗ --from-kbar：kbar.db 里查不到 %s 的历史（%s）"
                  % (label, args.kbar_db))
            return 2
        print("--from-kbar：起点取 kbar.db 最早一根 %s" % _fmt(from_ms))
        if to_ms is None:
            to_ms = int(time.time() * 1000)

    stats = run(BinanceFetcher(), args.market_db, args.symbol,
                interval=L.period_label(args.interval_ms),
                interval_ms=args.interval_ms,
                from_ms=from_ms, to_ms=to_ms,
                fill_gaps=args.fill_gaps, overwrite=args.overwrite,
                dry_run=args.dry_run)
    print("\n" + "=" * 72)
    if stats["ok"]:
        print("完成。补后洞：%d 处" % len(stats["holes_after"]))
        return 0
    print("✗ 没达到目标：%s" % (stats["error"] or "洞还在"))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
