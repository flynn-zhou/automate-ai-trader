# -*- coding: utf-8 -*-
"""计划源：从库里**产出**新计划，而不只是推进已有预期。

这个缺口有多严重
----------------

`engine._step_pending()` 只做一件事：把**已经存在**的 live 预期往前推一根。
它从不产出新预期。也就是说，如果没有人另外往 `pending_theses` 里插行，
系统会「完美地管理一个空集合」——

  · 引擎每根 5m 都在跑，日志正常，看板正常
  · 永远没有新计划 → 永远不下单 → 用户看到的只是「策略没信号」

这和「策略确实没信号」在事后**完全无法区分**。所以计划源不能被省掉。

为什么放在包里而不是 tools 里
-----------------------------

`tools/_smoke_pending_plan.py` 里已经有一份等价的加载逻辑（离线冒烟用）。
生产要用的必须是**同一份**：两份 SQL 各自演化，迟早一份改了列名、
另一份还在读旧列 —— 而这个项目已经因为「同一事实两个来源」出过 14 次事。
所以：
  · 数据加载与窗口构建的**唯一实现**在 `plan_source.py`
  · `tools/_smoke_pending_plan.py` 反过来 import 它

口径提醒（都不是我定的，是既有实现）
------------------------------------

- 1h 来自**区间库**的 `bars_1h`，列名是 `open/high/low/close/volume`
  （不是 `o/h/l/c/v`）。读错不报错，只会全变成 NaN。
- 15m / 5m 来自 **kbar 库**的 `kbar_evolution`（列名 `o/h/l/c/vol`）。
- 开仓窗口的 `tag` 来自**区间库**的 `range_evolution`，方向与「区间是否形成」
  由区间引擎写库时算好，这里**不重新推导**。
- `LinePoint` 的字段顺序是 `(price, src_ts, ready_ms)` —— **一律关键字传**，
  按位置传不会报错，只会把 price 变成时间戳，于是所有形态静默归零。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import pending_plan as P
from . import thesis as T

#: 生产库默认路径（相对仓库根）。
DEFAULT_KBAR_DB = os.path.join("data", "kbar.db")
#: ★ 权威口径：区间库在生产上叫 `market_range.db`
#:   （见 `ai_trader/kbar/build.py: DEFAULT_RANGE_DB`）。
#:   `market_range_rebuilt.db` 是**本地分析产物**，不是生产文件。
#:   ★ 我第一版把它当默认值，直接导致计划源在
#:     服务器上返回 None —— 本地跑得通，上线就哑。
DEFAULT_RANGE_DB = os.path.join("data", "market_range.db")

#: 区间库候选名（按优先序）。生产用第一个；
#: 第二个只是本地分析副本，为了让本地回放能跑。
RANGE_DB_CANDIDATES = ("market_range.db", "market_range_rebuilt.db")

#: 回看根数。**None = 取全量历史**。
#:
#: ★ 这里碰到过一个非常典型的陷阱：
#:   最初拿 240 根 15m（= 2.5 天）当回看，结果产出 **0 条计划**。
#:   原因不是代码错，是开仓窗口本来就稀有：
#:   180 天里只有 **7 个**，而最后一个距今 **43 天**。
#:   于是“回看 2.5 天”在数据上是合法的，却永远不可能碰到窗口。
#:   这种失败不报错，只表现为「策略没信号」—— 和真没信号一模一样。
#:
#:   全量也不贵：1h 4319 行 + 15m 6000 行，内存量级完全可以。
#:   真要限制就显式传，不要用一个“看起来合理”的默认值。
LOOKBACK_15M: Optional[int] = None

#: 建窗口用的量能口径（与 `PendingParams` 一致，不许在这里另调）。
WINDOW_VOL_MULT = 1.5
WINDOW_VOL_WIN = 24


def resolve_range_db(directory: str) -> str:
    """在 `directory` 里找区间库，返回绝对路径；找不到返回 ""。

    ★ 为什么要一个函数而不是一个常量：
      区间库的**目录**由交易库位置推导（与 kbar 库同一个口径），
      不能再开一个 env（多一个 env 就多一个“代码读了但模板没写”的缺口）。
    """
    if not directory:
        return ""
    for name in RANGE_DB_CANDIDATES:
        cand = os.path.join(directory, name)
        if os.path.exists(cand):
            return cand
    return ""


def _ro(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FileNotFoundError("库不存在：%s" % path)
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


class PlanSource:
    """从 kbar / 区间两个库产出待挂单计划。

    用法（生产）：

        src = PlanSource(book_provider=self.pending_lines, symbol="BTCUSDT")
        plans, stats = src.plans(now_ms, self._pending_params())
    """

    def __init__(self, book_provider=None,
                 kbar_db: str = DEFAULT_KBAR_DB,
                 range_db: str = DEFAULT_RANGE_DB,
                 symbol: str = "BTCUSDT",
                 lookback_15m: Optional[int] = LOOKBACK_15M):
        self.book_provider = book_provider
        self.kbar_db = kbar_db
        self.range_db = range_db
        self.symbol = symbol
        self.lookback_15m = (None if lookback_15m is None
                             else int(lookback_15m))

    # ------------------------------------------------------------ 数据加载

    def _evolution(self, interval_ms: int,
                   limit: Optional[int] = None) -> List[Any]:
        """kbar 库里的演化 K 线（列名 `o/h/l/c/vol`）。"""
        con = _ro(self.kbar_db)
        sql = ("select ts,o,h,l,c,vol from kbar_evolution"
               " where symbol=? and interval_ms=? order by ts desc")
        args = [self.symbol, int(interval_ms)]
        if limit is not None:
            sql += " limit ?"
            args.append(int(limit))
        try:
            rows = con.execute(sql, args).fetchall()
        finally:
            con.close()
        rows.reverse()                     # 取最近 N 根，再按时间正序
        return [P._Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                       float(r[4])) for r in rows]

    def _bars_1h(self, limit: Optional[int] = None) -> List[Any]:
        """区间库的 1h（★ 列名是完整单词，不是 o/h/l/c/v）。"""
        con = _ro(self.range_db)
        sql = ("select ts,open,high,low,close,volume from bars_1h"
               " where symbol=? order by ts desc")
        args = [self.symbol]
        if limit is not None:
            sql += " limit ?"
            args.append(int(limit))
        try:
            rows = con.execute(sql, args).fetchall()
        finally:
            con.close()
        rows.reverse()
        return [P._Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                       float(r[4])) for r in rows]

    def _tags(self) -> Dict[int, Tuple[str, str]]:
        """`range_evolution` 的 `(tag, direction)`，按 1h 整点索引。"""
        con = _ro(self.range_db)
        try:
            rows = con.execute(
                "select ts,tag,direction from range_evolution"
                " where symbol=? order by ts", (self.symbol,)).fetchall()
        finally:
            con.close()
        return {int(r[0]): ((r[1] or ""), (r[2] or "")) for r in rows}

    def _h1_volumes(self) -> Dict[int, float]:
        """1h 量能：由 15m 演化行按小时聚合（`bars_1h.volume` 常为 0）。"""
        con = _ro(self.kbar_db)
        try:
            rows = con.execute(
                "select (ts/3600000)*3600000 as h, sum(vol) from kbar_evolution"
                " where symbol=? and interval_ms=900000 group by h",
                (self.symbol,)).fetchall()
        finally:
            con.close()
        return {int(r[0]): float(r[1] or 0.0) for r in rows}

    # ------------------------------------------------------------ 上下文

    def context(self) -> P.PlanContext:
        """把三个库拼成一个 `PlanContext`（含开仓窗口）。"""
        n = self.lookback_15m
        b1h = self._bars_1h(None if n is None else max(96, n // 4 + 96))
        b15 = self._evolution(900_000, n)
        b5 = self._evolution(300_000, None if n is None else n * 4)
        book = self.book_provider() if self.book_provider else T.LineBook()
        if book is None:
            book = T.LineBook()
        windows = self._windows(b1h, book)
        return P.PlanContext(bars15=b15, book=book, bars_1h=b1h, bars_5m=b5,
                             windows=windows)

    def _windows(self, b1h: Sequence[Any], book: Any) -> List[Any]:
        """开仓窗口：只在「顺向带量突破 → 下一个区间形成过程」里出计划。"""
        tags = self._tags()
        vols = self._h1_volumes()

        def tag_of(ts):
            return tags.get(((int(ts) // 3_600_000) * 3_600_000), ("", ""))

        def line_of(ts, key):
            pt = book.at(key, int(ts) + 3_600_000)   # 该 1h 收盘时已就绪
            return None if pt is None else float(pt.price)

        class _H1(object):
            __slots__ = ("ts", "o", "h", "l", "c", "v")

            def __init__(self, b):
                self.ts, self.o, self.h, self.l = b.ts, b.o, b.h, b.l
                self.c = b.c
                self.v = vols.get(int(b.ts), float(getattr(b, "v", 0.0) or 0.0))

        return P.build_open_windows(
            [_H1(b) for b in b1h], tag_of, line_of,
            vol_mult=WINDOW_VOL_MULT, vol_win=WINDOW_VOL_WIN,
            extra_bars=P.WINDOW_EXTRA_BARS)

    # ------------------------------------------------------------ 产出

    def plans(self, params: Optional[P.PendingParams] = None,
              ctx: Optional[P.PlanContext] = None
              ) -> Tuple[List[P.Plan], Dict[str, int]]:
        """产出计划。`require_window=True` 是**硬要求**，见 `detect_all`。"""
        ctx = ctx if ctx is not None else self.context()
        return P.detect_all(ctx, params, require_window=True,
                            include_mirror=True)

    @staticmethod
    def plan_key(plan: P.Plan) -> str:
        """计划的稳定身份 —— 去重/幂等靠它。

        ★ `Plan` 上**没有** `plan_id` 字段，所以键必须由内容构造。
          用 `(kind, direction, order_price, bar_ts)`：
          同一形态、同一方向、同一挂单价、同一根 K = 同一条计划。
          不要用 `bar_index` —— 它属于某条序列的下标，序列一改就漂。
        """
        return "%s|%s|%s|%.4f" % (plan.kind, plan.direction,
                                  int(plan.effective_claim_ts),
                                  float(plan.order_price))
