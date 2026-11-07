# -*- coding: utf-8 -*-
"""区间演化的实时推进。

为什么需要这个模块
------------------
engine.build(bars) 是纯函数，只认已收盘的 1h 桶，自己没有"定时重算"的能力。
只要没人喂数据，range_evolution 表就永远停在最后一次回填的时刻 ——
用户看到的就是"区间演化 K 线只有昨天 23 点，少了两根"。

数据从哪来（这里踩过坑，写清楚）
------------------------------
两个库各管一段，不能只读一个：
  market_range.db 的 bars_1h：回填 + 历次推进累积的 **已收盘 1h**，是历史底座（2000+ 根）。
  market.db 的 bars：实时采集的原始行情，里面有**当前还没收盘的那根 1h**。
曾经只读 market.db，那里 1h 只有 100 来根（刚上线），低于 build 需要的最小
历史长度，于是推进每次都空转、一行都不写，而且只留一条 warning —— 表面上看
服务是好的，实际区间演化根本没动。

关于"未完成的那根"
------------------
未收盘的 1h 桶每 3 秒都在变（close 就是现价），它对应的区间状态也在变。
但它不能写进历史表：历史表一旦写进一个尚未定稿的桶，收盘后实际值和表里
就不一致了，而且再也纠正不回来（只增不改）。

所以对未完成桶的做法是：作尾行附加在返回结果里交给页面（live 标记），
库里只写已收盘的桶。页面把它画成跳动的最后一根，收盘后它自然被正式写库。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Tuple

from .engine import build, ts8
from .store import rows_from_bars, rows_from_events, rows_from_states

logger = logging.getLogger(__name__)

H1_MS = 3_600_000
# build() 需要一定历史才有意义；实测 150 个桶就能产出 states/events。
MIN_BARS = 150


# 直接复用引擎的 ts8（引擎内部用的是 gmt+TZ，即北京时间）。
# 自己另写一份 utcfromtimestamp 会得到 UTC 字符串 —— 库里一半北京一半 UTC，
# 时间轴看上去"差 8 小时"，而这种错不会报任何异常。
ts8_of = ts8


def _forming_from_market(market_store, symbol: str, cur_open: int):
    """从实时行情库取当前这个 1h 桶（还没收盘的那根）。取不到返回 None。"""
    if market_store is None:
        return None
    try:
        rows = market_store.query_bars(H1_MS, limit=10, symbol=symbol)
    except Exception as e:
        logger.warning("读实时 1h 失败: %s", str(e)[:100])
        return None
    for r in rows:
        # Store.query_bars 返回的字典键是 t/o/h/l/c/v —— 不是 open_time。
        # 写成 open_time 会 KeyError，而它被外层 except 吞成一条 warning，
        # 表现为"推进在跑但一行都没写"。
        ts = int(r["t"]) if isinstance(r, dict) else int(r[0])
        if ts != cur_open:
            continue
        if isinstance(r, dict):
            return (ts, r["o"], r["h"], r["l"], r["c"], r.get("v"))
        return (ts, r[2], r[3], r[4], r[5], r[6] if len(r) > 6 else None)
    return None


def load_series(range_db, market_store, symbol: str):
    """返回 (已收盘 1h 列表, 当前未收盘那根或 None)。

    两个来源必须合并，缺一个都会出问题：
      market_range.db/bars_1h —— 回填来的历史底座（2000+ 根），但回填之后就再不更新；
      market.db/bars(1h)      —— 实时采集，只有最近 100 来根，但一直在长。

    只读前者：时间永远停在回填时刻（用户看到的"只有昨天 23 点"就是这个）。
    只读后者：历史长度不够 build() 的最小要求，每次都空转。
    """
    by_ts = {}

    for r in range_db.bars_1h(symbol):
        # bars_1h 行 = (ts, ts_h, open, high, low, close, volume)
        ts, o, h, l, c = r[0], r[2], r[3], r[4], r[5]
        by_ts[int(ts)] = (int(ts), o, h, l, c, r[6] if len(r) > 6 else None)

    if market_store is not None:
        try:
            rows = market_store.query_bars(H1_MS, limit=4000, symbol=symbol)
        except Exception as e:
            logger.warning("读实时 1h 失败: %s", str(e)[:100])
            rows = []
        for r in rows:
            if isinstance(r, dict):
                # Store.query_bars 的键是 t/o/h/l/c/v，不是 open_time。
                ts = int(r["t"])
                by_ts[ts] = (ts, r["o"], r["h"], r["l"], r["c"], r.get("v"))
            else:
                ts = int(r[0])
                by_ts[ts] = (ts, r[2], r[3], r[4], r[5],
                             r[6] if len(r) > 6 else None)

    cur_open = (int(time.time() * 1000) // H1_MS) * H1_MS
    closed = [b for ts, b in sorted(by_ts.items()) if ts < cur_open]
    forming = by_ts.get(cur_open)
    return closed, forming


def compute(range_db, market_store, symbol: str, forming=None):
    """算一次：返回 (已收盘 states 行, events 行, 未完成桶的附加状态)。

    forming 可以由调用方传入。**这一条是必须的**：1h 聚合只在收盘时才落库，
    所以整点之后的这一整小时里，本地行情库里根本没有当前这根 1h。
    只查本地库的结果就是 filing 永远是 None、页面永远少最后一根
    （用户看到的就是"8 点了，还是只有 7 点那根"）。
    旧版的做法是直接向币安要：/api/live_klines?interval=1h&limit=2。
    """
    closed, local_forming = load_series(range_db, market_store, symbol)
    if forming is None:
        forming = local_forming
    if len(closed) < MIN_BARS:
        logger.info("区间演化推进暂缓: 已收盘 1h 只有 %d 根，需要 %d",
                    len(closed), MIN_BARS)
        return [], [], None

    events, states = build(closed)
    st_rows = rows_from_states(states, ts8_of)
    ev_rows = rows_from_events(events, ts8_of)

    tail = None
    if forming is not None and states:
        # 未完成桶：接在已收盘序列后面再算一次，取最后一行。
        # 这不是猜 —— build 是纯函数，喂什么算什么；只是这一行要到收盘才定稿，
        # 所以只给页面看，不写库。
        _ev2, st2 = build(closed + [forming])
        if st2:
            last = rows_from_states(st2, ts8_of)[-1]
            tail = {
                "ts": last[0], "ts_h": last[1],
                "soft_up": last[2], "soft_dn": last[3],
                "hard_up": last[4], "hard_dn": last[5],
                "key": last[6], "family": last[7], "segment": last[8],
                "direction": last[9],
                "ohlc": [forming[1], forming[2], forming[3], forming[4]],
                "live": True,
            }
    return st_rows, ev_rows, tail


def advance(range_db, market_store, symbol: str) -> int:
    """跑一次并写库。返回写入的状态行数。"""
    closed, forming = load_series(range_db, market_store, symbol)
    if len(closed) < MIN_BARS:
        return 0

    events, states = build(closed)
    if not states:
        return 0
    # 把已收盘的桶落进区间库：这些桶可能是从实时库合并来的（区间库自己没有），
    # 不写的话 API 的 K 线永远少最后一根 —— 状态在动、蜡烛不动，很怪。
    range_db.upsert_bars_1h(symbol, rows_from_bars(closed, ts8_of))
    n = range_db.upsert_states(symbol, rows_from_states(states, ts8_of))
    # events 表没有唯一约束（同一根上可以记录多个事件），所以仓储层只提供
    # replace_events（整段重写）。这里是增量推进，绝不能 replace —— 那会把
    # 回填出来的历史事件整段抹掉。所以先读现有 (ts, event) 键，只写新的。
    if events:
        new_rows = rows_from_events(events, ts8_of)
        try:
            have = range_db.events(symbol)
        except Exception:
            have = []
        seen = set((r[0], r[3]) for r in have if len(r) > 3)
        fresh = [r for r in new_rows if (r[0], r[3]) not in seen]
        if fresh:
            range_db.append_events(symbol, fresh)
    # 未完成桶**一律不写库**，连 OHLC 也不写。
    # 实测写进去以后它会以"未来时刻"的 ts 落在表尾，把"最新已收盘"判断搞乱，
    # 于是下一次推进又把它当成已收盘算一遍 —— 自激。未完成桶只走 tail 给页面看。
    return n


async def run_forever(range_db, market_store, symbol: str,
                      interval_s: float) -> None:
    """主循环。异常绝不能让推进停掉 —— 记完继续下一轮。"""
    logger.info("区间演化实时推进启动: %s 每 %.0f 秒", symbol, interval_s)
    warned = False
    while True:
        try:
            n = advance(range_db, market_store, symbol)
            if n:
                logger.debug("区间演化推进: 写入 %d 行", n)
                warned = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not warned:
                logger.warning("区间演化推进失败: %s", str(e)[:200])
                warned = True
        await asyncio.sleep(interval_s)