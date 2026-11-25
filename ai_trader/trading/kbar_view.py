# -*- coding: utf-8 -*-
"""K 线演化的**只读视图**：把「某根 5m 发生了什么」一次取全，供出场判据用。

为什么需要这一层
----------------

`thesis.py` 里的出场判据是纯函数，它们只想知道「这一根 K 的事实」，
不该知道 `kbar.db` 长什么样。回放和实盘必须走**同一条取值路径** ——
否则回测里能触发、实盘里静默不触发，而两边都不报错。

所以视图就是那一条路径：`run(kbar=view)` / `step(kbar=view)` 都收它，
测试里塞一个字典也长得一样（`thesis._kbar_at` 支持三种形态）。

口径（每一条都量过，不是猜的）
------------------------------

- `label` 取 **`kbar_shape`**，每根一行，填充率 **100%**（34,561/34,561）。
- `side` 取 **`kbar_reaction`**，填充率 **100%**（47,341/47,341）。
  ★ **不能用 `kbar_shape.side`** —— 它只有 **9.2%** 非空。
  用了它，`恐慌盘` 顺向判据会在 90% 的情况下拿到 None 然后静默不出场，
  而「静默不出场」和「这道闸没触发」在日志里长得一模一样。
- 一根 5m 可能有 **0~8 条** `kbar_reaction`（每碰一条线一条记录：
  9,000 根 1 条、6,486 根 2 条、…、44 根 8 条，另有 12,561 根一条都没有）。
  同根多条时取**穿透最深**的那条 —— 与 `dims.py` 同口径。
  并列时按 `line_key` 排，**保证同一根每次取到同一条**；
  否则数据库返回顺序一变，回放结果就跟着变，而这种不确定性不会报错。

只读
----

连接以 `mode=ro` 打开，**不建表、不写一行**。视图不负责回填 ——
数据缺了要由 `tools/backfill_*` / `tools/ingest_kbar.py` 补，
在这里顺手写库会让「读」和「写」两条链纠缠在一起。
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Dict, Optional

#: 生产库路径（相对仓库根）。
DEFAULT_KBAR_DB = os.path.join("data", "kbar.db")

#: 出场判据读的周期。用户 2026-11-17 定：K 线演化用已上线周期，5m 是主判。
DEFAULT_INTERVAL_MS = 300_000

#: 同一份视图最多用多久（毫秒）。
#: 引擎每根 5m 都要问一次，而生产管道现在每小时才推进一次，
#: 所以 TTL 取 5 分钟：重载不会比数据更新更频繁，
#: 也不会让视图在一次行情里变老。
DEFAULT_VIEW_TTL_MS = 300_000


def _ro_connect(path: str) -> sqlite3.Connection:
    """只读打开。库不存在时**抛错**，不返回空视图。

    ★ 静默返回空视图是这里最坏的选择：S5 的三道闸会全部变成
      「永不触发」，而看板上显示的是一笔一笔正常持仓 ——
      没人会去怀疑「闸根本没数据」。
    """
    if not os.path.exists(path):
        raise FileNotFoundError("kbar 库不存在：%s" % path)
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


class KBarView:
    """`ts -> {label, side, vol_rel, ...}` 的只读映射。"""

    __slots__ = ("_rows", "interval_ms", "path", "bars")

    def __init__(self, rows: Dict[int, Dict[str, Any]], interval_ms: int,
                 path: str = ""):
        self._rows = rows
        self.interval_ms = int(interval_ms)
        self.path = path
        self.bars = len(rows)

    #: `thesis._kbar_at()` 认这个接口。
    def at(self, ts_ms: int) -> Optional[Dict[str, Any]]:
        return self._rows.get(int(ts_ms))

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, ts_ms: object) -> bool:
        try:
            return int(ts_ms) in self._rows
        except (TypeError, ValueError):
            return False

    def ts_range(self):
        if not self._rows:
            return (0, 0)
        keys = sorted(self._rows)
        return (keys[0], keys[-1])

    def coverage(self) -> Dict[str, Any]:
        """给运维/测试看的自检：有多少根带方向、有多少根带量比。"""
        n = len(self._rows)
        with_side = sum(1 for d in self._rows.values() if d.get("side"))
        with_vol = sum(1 for d in self._rows.values()
                       if d.get("vol_rel") is not None)
        first, last = self.ts_range()
        return {
            "bars": n,
            "interval_ms": self.interval_ms,
            "first_ts": first,
            "last_ts": last,
            "with_label": sum(1 for d in self._rows.values() if d.get("label")),
            "with_side": with_side,
            "with_vol_rel": with_vol,
            "path": self.path,
        }


def load_kbar_view(path: str = DEFAULT_KBAR_DB,
                   interval_ms: int = DEFAULT_INTERVAL_MS,
                   symbol: Optional[str] = None) -> KBarView:
    """从 `kbar.db` 装一个只读视图。

    ★ 只取 `label` / `side` / `vol_rel` / `pen_depth_pct` 这几列 ——
      出场判据只问「这根是什么、朝哪边、量多大」，不问几何细节。
      要几何细节的调用方应该自己去查，而不是把整张表塞进内存。
    """
    con = _ro_connect(path)
    try:
        rows: Dict[int, Dict[str, Any]] = {}
        sym_clause = " and symbol=?" if symbol else ""
        args = (int(interval_ms),) + ((symbol,) if symbol else ())

        for ts, label, vol_rel in con.execute(
                "select ts, label, vol_rel from kbar_shape "
                "where interval_ms=?" + sym_clause + " order by ts", args):
            rows[int(ts)] = {"ts": int(ts), "label": label,
                             "vol_rel": vol_rel, "side": None,
                             "pen_depth_pct": None, "line_key": None}

        # 同根取穿透最深的一条；并列按 line_key 排 —— 见模块 docstring。
        for ts, side, vol_rel, pen, lk in con.execute(
                "select ts, side, vol_rel, pen_depth_pct, line_key "
                "from kbar_reaction where interval_ms=?"
                + sym_clause + " order by ts, pen_depth_pct desc, line_key",
                args):
            d = rows.get(int(ts))
            if d is None or d.get("side") is not None:
                continue          # 没有 shape 行的根不造事实；已有则取首条
            d["side"] = side
            d["vol_rel"] = d.get("vol_rel") if d.get("vol_rel") is not None \
                else vol_rel
            d["pen_depth_pct"] = pen
            d["line_key"] = lk
        return KBarView(rows, interval_ms, path)
    finally:
        con.close()


class RefreshableKBarView:
    """按需重载的视图代理。**引擎长跑时必须用这个。**

    它对外面跟 `KBarView` 一模一样（都有 `.at()`），
    所以 `thesis._kbar_at()` 不用改。

    何时重载（两个条件任一成立）
    ------------------------------
    1. TTL 过期 —— 防「库在长，我不知道」。
    2. 被问到比已加载的**最后一根还要新**的 K ——
       说明库已经往前走了，不重载就会把新 K 当成「没事实」。

    重载失败怎么办
    --------------
    **保留旧视图**并告警，不返回 None。
    回 None 会让三道闸立即全部失效，而一次临时的
    `database is locked` 不该把所有持仓的出场判据都关掉。
    """

    __slots__ = ("path", "interval_ms", "symbol", "ttl_ms",
                 "_view", "_loaded_at", "_fail_logged")

    def __init__(self, path: str = DEFAULT_KBAR_DB,
                 interval_ms: int = DEFAULT_INTERVAL_MS,
                 symbol: Optional[str] = None,
                 ttl_ms: int = DEFAULT_VIEW_TTL_MS):
        self.path = path
        self.interval_ms = int(interval_ms)
        self.symbol = symbol
        self.ttl_ms = int(ttl_ms)
        self._fail_logged = False
        self._view = load_kbar_view(path, interval_ms, symbol)
        self._loaded_at = time.time()

    # ---- 内部 ----
    def _reload(self) -> None:
        try:
            v = load_kbar_view(self.path, self.interval_ms, self.symbol)
        except Exception as e:                     # noqa: BLE001
            if not self._fail_logged:
                self._fail_logged = True
                import logging
                logging.getLogger(__name__).error(
                    "K 线视图重载失败，继续用上一份"
                    "（最新 %s）：%s",
                    self._view.ts_range()[1], e)
            return
        self._view = v
        self._loaded_at = time.time()
        self._fail_logged = False

    def _maybe_reload(self, ts_ms: int = 0) -> None:
        if (time.time() - self._loaded_at) * 1000.0 >= self.ttl_ms:
            self._reload()
            return
        if ts_ms:
            last = self._view.ts_range()[1]
            if last and int(ts_ms) > last:
                self._reload()

    # ---- 对外接口（与 KBarView 一致）----
    def at(self, ts_ms: int) -> Optional[Dict[str, Any]]:
        self._maybe_reload(ts_ms)
        return self._view.at(ts_ms)

    def __contains__(self, ts_ms: object) -> bool:
        self._maybe_reload()
        return ts_ms in self._view

    def __len__(self) -> int:
        self._maybe_reload()
        return len(self._view)

    def ts_range(self):
        self._maybe_reload()
        return self._view.ts_range()

    def coverage(self) -> Dict[str, Any]:
        self._maybe_reload()
        return self._view.coverage()

    #: 让测试和运维能直接看当前那份快照。
    @property
    def view(self) -> KBarView:
        return self._view
