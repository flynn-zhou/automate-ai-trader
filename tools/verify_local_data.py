# -*- coding: utf-8 -*-
"""拉回本地之后，先验数据，再谈分析（用户 2026-11-17 定的规程）。

为什么必须有这个脚本 —— 不是「小心一点就行」的问题：

    文件级 `cp` 在 WAL 模式下能拷出**结构完整但内容截断**的库。
    `sqlite3` 打开它不报错，`count(*)` 也照样返回数字，只是少了一整段。
    本项目已经两次栽在「看起来成功了」上：
      · `INSERT OR IGNORE` 让补数工具报告 written=N 实际写了 0；
      · 攒着最后一次性写，网络一断全丢（written=0）。
    所以拉回来的数据必须过一遍**机器检查**，不能靠眼睛看一眼 K 线图。

检查五件事，缺一件就红：

    1. 完整性   `pragma quick_check` / `integrity_check`
    2. 连续性   每个周期按自己的 interval_ms 推，缺格要报出来
    3. 记录断口 `market.db` 的 `bar_gaps` 表（生产自己记的断口）
    4. 周期跨越 细周期（5m）不该比粗周期（15m）短太多
    5. 新鲜度   最新一根距现在多久；本地数据停在 11-01 就该大叫
    6. 线对齐   `kbar_line` 的 ready_ms 与 ts+period_ms 是否自洽

退出码：0 = 没有 FAIL（WARN 允许）；1 = 有 FAIL。
用法：
    python tools/verify_local_data.py                # 用默认路径
    python tools/verify_local_data.py --json         # 给机器读
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

DEFAULT_MARKET = "data/market.db"
DEFAULT_KBAR = "data/kbar.db"
DEFAULT_RANGE = "data/market_range_rebuilt.db"

M5 = 300_000
M15 = 900_000
H1 = 3_600_000

#: 各周期的「多久没更新就不对劲」阈值（小时）。
#: 5m 是实时节奏，落后 2 小时就该怀疑；15m 给到 3 小时；1h 给到 6 小时。
MAX_LAG_H = {M5: 2.0, M15: 3.0, H1: 6.0}
DEFAULT_MAX_LAG_H = 6.0


def fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ms) / 1000.0))


def fmt_iv(iv: int) -> str:
    if iv % H1 == 0:
        return "%dh" % (iv // H1)
    if iv % 60_000 == 0:
        return "%dm" % (iv // 60_000)
    return "%dms" % iv


class Check(object):
    __slots__ = ("level", "title", "detail")

    def __init__(self, level: str, title: str, detail: str = ""):
        self.level, self.title, self.detail = level, title, detail

    def __repr__(self):      # pragma: no cover
        return "Check(%s,%s)" % (self.level, self.title)

    def as_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "title": self.title, "detail": self.detail}


# ---------------------------------------------------------------- 序列检查

def series_stats(times: List[int], interval: int) -> Dict[str, Any]:
    """一串等间隔时间戳的完整性统计。

    ★ 缺格按「等差应该有几根」反推，不是按「相邻差 != interval」数一次就完 ——
      跳掉 5 格和跳掉 1 格都是断，但严重程度差 5 倍，报告里要能看出来。

    ★ `interval <= 0` 是**硬错误**，不许静默返回「0 缺失」：
      一个算不出节奏的序列，最糟的结果是被告知「它没问题」。
    """
    if int(interval) <= 0:
        raise ValueError(
            "series_stats 的 interval 必须 > 0（拿到 %r）。"
            "没有周期列的表要靠调用方传 default_interval；"
            "给 0 会让「应有几根」算不出来，静默当成 0 缺失就等于假通过。"
            % (interval,))
    out: Dict[str, Any] = {"rows": len(times), "first": None, "last": None,
                           "expect": 0, "missing": 0, "gaps": []}
    if not times:
        return out
    ts = sorted(int(t) for t in times)
    out["first"], out["last"] = ts[0], ts[-1]
    expect = (ts[-1] - ts[0]) // int(interval) + 1
    out["expect"] = expect
    out["missing"] = max(0, expect - len(ts))
    gaps = []
    for a, b in zip(ts, ts[1:]):
        step = b - a
        if step != interval:
            gaps.append({"after": a, "before": b,
                         "missing_bars": max(0, step // int(interval) - 1)})
    out["gaps"] = gaps
    return out


def _connect(path: str) -> Optional[sqlite3.Connection]:
    if not os.path.exists(path):
        return None
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    row = c.execute("select 1 from sqlite_master where type='table' and name=?",
                    (name,)).fetchone()
    return row is not None


def check_integrity(path: str, label: str) -> List[Check]:
    out: List[Check] = []
    c = _connect(path)
    if c is None:
        return [Check(FAIL, "%s 打不开" % label, "文件不存在：%s" % path)]
    try:
        got = c.execute("pragma quick_check").fetchone()
        msg = got[0] if got else "no result"
        if str(msg).lower() == "ok":
            out.append(Check(OK, "%s 完整性" % label, "quick_check ok"))
        else:
            out.append(Check(FAIL, "%s 完整性" % label, str(msg)[:300]))
    except sqlite3.DatabaseError as exc:
        out.append(Check(FAIL, "%s 完整性" % label, "打不开：%s" % exc))
    finally:
        c.close()
    return out


def check_series(path: str, label: str, table: str, ts_col: str,
                 interval_col: Optional[str], now_ms: int,
                 max_lag_h: Optional[Dict[int, float]] = None,
                 require_fresh: bool = True,
                 default_interval: int = 0
                 ) -> Tuple[List[Check], Dict[int, Dict[str, Any]]]:
    """按周期检查连续性与新鲜度。返回 (checks, {interval: stats})。

    `interval_col=None` 的表（`bars_1h` / `range_evolution`）没有周期列，
    必须靠 `default_interval` 告诉检查者「这张表本来是什么节奏」；
    给 0 会让等差反推变成除零 —— 这不是可以静默跳过的参数。
    """
    checks: List[Check] = []
    stats: Dict[int, Dict[str, Any]] = {}
    c = _connect(path)
    if c is None:
        return [Check(FAIL, "%s 打不开" % label, path)], stats
    try:
        if not _has_table(c, table):
            return [Check(FAIL, "%s 缺表" % label, "%s.%s" % (path, table))], stats
        if interval_col:
            ivs = [int(r[0]) for r in c.execute(
                "select distinct %s from %s where %s is not null"
                % (interval_col, table, interval_col)).fetchall()]
        else:
            if not default_interval:
                return [Check(FAIL, "%s 判不了节奏" % label,
                              "没有 interval 列，必须传 default_interval")], stats
            ivs = [int(default_interval)]
        if not ivs:
            return [Check(FAIL, "%s 没有数据" % label, "%s 是空表" % table)], stats
        lag_h = dict(MAX_LAG_H)
        lag_h.update(max_lag_h or {})
        for iv in sorted(ivs):
            if interval_col:
                rows = c.execute(
                    "select %s from %s where %s=? order by %s"
                    % (ts_col, table, interval_col, ts_col), (iv,)).fetchall()
            else:
                rows = c.execute("select %s from %s order by %s"
                                 % (ts_col, table, ts_col)).fetchall()
            times = [int(r[0]) for r in rows if r[0] is not None]
            st = series_stats(times, iv)
            stats[iv] = st
            tag = "%s %s" % (label, fmt_iv(iv) if interval_col else "")
            if not times:
                checks.append(Check(FAIL, "%s 无数据" % tag, "0 根"))
                continue
            if st["missing"] > 0:
                worst = max((g["missing_bars"] for g in st["gaps"]), default=0)
                head = ", ".join(
                    "%s 后缺 %d" % (fmt_ms(g["after"]), g["missing_bars"])
                    for g in st["gaps"][:3])
                checks.append(Check(
                    FAIL, "%s 不连续" % tag,
                    "应 %d 根 / 实 %d 根 / 缺 %d 根；断口 %d 处，最大跳空 %d 根。%s"
                    % (st["expect"], st["rows"], st["missing"],
                       len(st["gaps"]), worst, head)))
            else:
                checks.append(Check(OK, "%s 连续" % tag,
                                    "%d 根 %s → %s" % (st["rows"],
                                                       fmt_ms(st["first"]),
                                                       fmt_ms(st["last"]))))
            if interval_col and require_fresh:
                lag = (now_ms - (st["last"] + iv)) / 3_600_000.0
                limit = lag_h.get(iv, DEFAULT_MAX_LAG_H)
                detail = ("最新 %s，落后 %.1f 小时（阈值 %.1f）"
                          % (fmt_ms(st["last"]), lag, limit))
                if lag > limit:
                    checks.append(Check(FAIL, "%s 不新鲜" % tag, detail))
                else:
                    checks.append(Check(OK, "%s 新鲜" % tag, detail))
    except sqlite3.DatabaseError as exc:
        checks.append(Check(FAIL, "%s 读不了" % label, str(exc)[:300]))
    finally:
        c.close()
    return checks, stats


def check_recorded_gaps(path: str) -> List[Check]:
    """生产自己记的断口表 `bar_gaps`。有记录不等于数据坏，但必须让人看见。"""
    c = _connect(path)
    if c is None or not _has_table(c, "bar_gaps"):
        return []
    try:
        rows = c.execute(
            "select symbol,interval_ms,open_time,detected_at from bar_gaps"
            " order by open_time desc limit 12").fetchall()
        total = c.execute("select count(*) from bar_gaps").fetchone()[0]
    except sqlite3.DatabaseError:
        return []
    finally:
        c.close()
    if not rows:
        return [Check(OK, "记录断口", "bar_gaps 为空")]
    head = "; ".join("%s %s %s" % (r[0], fmt_iv(int(r[1])), fmt_ms(int(r[2])))
                     for r in rows[:5])
    return [Check(WARN, "记录断口", "共 %d 条（生产侧登记过），最近：%s"
                  % (total, head))]


def check_cross_interval(stats: Dict[str, Dict[str, Any]]) -> List[Check]:
    """细周期不该比粗周期短 —— 短了说明某一侧没拉到最新。"""
    checks: List[Check] = []
    fine, coarse = M5, M15
    if fine in stats and coarse in stats:
        a = stats[fine].get("last")
        b = stats[coarse].get("last")
        if a is None or b is None:
            return [Check(WARN, "周期跨越", "有一侧没有数据，跳过")]
        behind_h = (b - a) / 3_600_000.0
        if behind_h > 1.0:
            checks.append(Check(
                FAIL, "周期跨越",
                "5m 比 15m 落后 %.1f 小时（5m 到 %s，15m 到 %s）—— "
                "细周期没拉到最新，入场时钟会静默退回粗周期"
                % (behind_h, fmt_ms(a), fmt_ms(b))))
        else:
            checks.append(Check(OK, "周期跨越",
                                "5m/15m 尾部对齐（差 %.1f 小时）" % behind_h))
    return checks


def check_line_ready(path: str) -> List[Check]:
    """`kbar_line.ready_ms` 是否等于 `ts + period_ms`（线的就绪时刻自洽）。

    ★ 只看**分布**，不硬断言：已知仓库里既存在 `+period` 也存在负偏移的线，
      硬断言会把正常的线判死。这里只报告「有多少条不符合 ts+period」。
    """
    c = _connect(path)
    if c is None or not _has_table(c, "kbar_line"):
        return []
    try:
        rows = c.execute(
            "select line_key, count(*),"
            " sum(case when line_ready_ms = ts + line_period_ms then 1 else 0 end)"
            " from kbar_line group by line_key").fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        c.close()
    bad = [(k, n, ok) for k, n, ok in rows if n and ok != n]
    if not bad:
        return [Check(OK, "线就绪时刻", "全部符合 ready = ts + period")]
    head = "; ".join("%s %d/%d 符合" % (k, ok, n) for k, n, ok in bad[:4])
    return [Check(WARN, "线就绪时刻", "%d 条线存在非 ts+period 的就绪时刻：%s"
                  % (len(bad), head))]


# ---------------------------------------------------------------- 主流程

def verify(market_db: str = DEFAULT_MARKET, kbar_db: str = DEFAULT_KBAR,
           range_db: str = DEFAULT_RANGE, now_ms: Optional[int] = None,
           require_fresh: bool = True) -> List[Check]:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    checks: List[Check] = []

    for path, label in ((market_db, "market.db"), (kbar_db, "kbar.db"),
                        (range_db, "range")):
        if not os.path.exists(path):
            checks.append(Check(FAIL, "%s 不存在" % label, path))
    if any(c.level == FAIL and "不存在" in c.title for c in checks):
        return checks

    checks += check_integrity(market_db, "market.db")
    checks += check_integrity(kbar_db, "kbar.db")

    # 1) market.db/bars —— 真实生产表，按 interval_ms 分
    cs, st = check_series(market_db, "bars", "bars", "open_time",
                          "interval_ms", now, require_fresh=require_fresh)
    checks += cs
    checks += check_recorded_gaps(market_db)

    # 2) kbar.db/kbar_evolution —— K 线演化落库
    cs2, st2 = check_series(kbar_db, "kbar_evolution", "kbar_evolution", "ts",
                            "interval_ms", now, require_fresh=require_fresh)
    checks += cs2

    # 3) 1h 区间演化（range 库自己带 bars_1h）
    cs3, _ = check_series(range_db, "bars_1h", "bars_1h", "ts", None, now,
                          require_fresh=False, default_interval=H1)
    checks += cs3
    cs4, _ = check_series(range_db, "range_evolution", "range_evolution", "ts",
                          None, now, require_fresh=False, default_interval=H1)
    checks += cs4

    # 4) 周期跨越（优先用 bars，退到 kbar_evolution）
    checks += check_cross_interval(st or st2)

    # 5) 线就绪时刻
    checks += check_line_ready(kbar_db)
    return checks


def summarize(checks: List[Check]) -> Dict[str, int]:
    out = {OK: 0, WARN: 0, FAIL: 0}
    for c in checks:
        out[c.level] = out.get(c.level, 0) + 1
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="拉回本地后校验数据完整性")
    ap.add_argument("--market-db", default=DEFAULT_MARKET)
    ap.add_argument("--kbar-db", default=DEFAULT_KBAR)
    ap.add_argument("--range-db", default=DEFAULT_RANGE)
    ap.add_argument("--no-fresh", action="store_true",
                    help="跳过新鲜度检查（拿历史快照做分析时用）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    checks = verify(args.market_db, args.kbar_db, args.range_db,
                    require_fresh=not args.no_fresh)
    tally = summarize(checks)

    if args.json:
        print(json.dumps({"summary": tally,
                          "checks": [c.as_dict() for c in checks]},
                         ensure_ascii=False, indent=2))
    else:
        print("=" * 78)
        print("本地数据校验（拉回生产快照之后必跑）")
        print("=" * 78)
        order = {FAIL: 0, WARN: 1, OK: 2}
        for c in sorted(checks, key=lambda x: (order.get(x.level, 9), x.title)):
            mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}.get(c.level, "  ?   ")
            print("%s %-28s %s" % (mark, c.title, c.detail))
        print("-" * 78)
        print("OK %d / WARN %d / FAIL %d" % (tally[OK], tally[WARN], tally[FAIL]))
        if tally[FAIL]:
            print("\n★ 有 FAIL —— 不要基于这份数据下结论。先重新拉快照。")
        elif tally[WARN]:
            print("\n★ 只有 WARN，可以继续分析，但要知道它们在。")
        else:
            print("\n全部通过。")
    return 1 if tally[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
