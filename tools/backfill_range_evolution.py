# -*- coding: utf-8 -*-
"""区间演化回填：从币安拉 15m -> 聚合成 1h -> 跑引擎 -> 落三表。

为什么不复用 engine.load_1h() 的 CSV 路径
----------------------------------------
load_1h 读的是 range_bars_15m 那个 CSV（回测夹具），而生产要的是**当前品种
真实最近的历史**。用夹具回填生产库等于把回测数据当成实盘数据，是 A→B→A 的
自我证明。所以这里直接拉币安、直接聚合、直接落库。

必须遵守的一条（engine.py 里的原话）
-----------------------------------
WARMUP_1H_BARS 是**全仓唯一口径**，实时管线与回填工具必须共用同一个值。
两边不一致时，实时管线的 diff_many 会把回填好的行逐批改回去
（实测 11 小时改了 15 行）。所以这里 import 引擎的常量，不自己写死。

用法：
    python tools/backfill_range_evolution.py                 # 默认 BTCUSDT，拉 6000 根 15m
    python tools/backfill_range_evolution.py --bars 9000
    python tools/backfill_range_evolution.py --dry-run       # 只算不落库
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.range.engine import WARMUP_1H_BARS, build, lab, ts8   # noqa: E402
from ai_trader.range.store import RangeStore, persist_run            # noqa: E402

FAPI = "https://fapi.binance.com/fapi/v1/klines"
INTERVAL_MS_15M = 900_000
BUCKET_MS = 3_600_000


def fetch_15m(symbol: str, want: int, chunk: int = 1500) -> list:
    """翻页拉 15m。币安单次上限 1500，所以按 endTime 往前翻。

    代理策略：跟随环境变量（urllib 默认行为）。
    不硬编码 ProxyHandler({}) —— 那在「必须走代理才能出网」的机器上直接超时，
    而写这段代码的人当时只看得到自己那台直连的服务器。默认行为两边都对：
    没设 proxy 变量 = 直连；设了 = 走代理。
    """
    opener = urllib.request.build_opener()
    out = []
    end_time = None
    while len(out) < want:
        n = min(chunk, want - len(out))
        url = ("%s?symbol=%s&interval=15m&limit=%d" % (FAPI, symbol, n))
        if end_time:
            url += "&endTime=%d" % end_time
        req = urllib.request.Request(url, headers={"User-Agent": "ai-trader"})
        with opener.open(req, timeout=20) as r:
            rows = json.loads(r.read().decode("utf-8"))
        if not rows:
            break
        out = rows + out
        end_time = int(rows[0][0]) - 1
        if len(rows) < n:
            break
        time.sleep(0.25)          # 别把配额打爆
    # 去重 + 按时间排序
    seen = {}
    for r in out:
        seen[int(r[0])] = r
    return [seen[k] for k in sorted(seen)]


def aggregate_1h(rows15: list) -> tuple:
    """15m -> 1h 完整桶。每桶必须正好 4 根，不足的**丢弃并报数**，绝不补齐。

    补齐会凭空造出一根不存在的 K 线，而区间演化是要拿这根线判突破的——
    伪造的线会产生一个从没发生过的"突破事件"。
    """
    buckets = {}
    for r in rows15:
        ts = int(r[0])
        k = ts // BUCKET_MS * BUCKET_MS
        o, hi, lo, c, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        if k in buckets:
            b = buckets[k]
            b[1] = max(b[1], hi)
            b[2] = min(b[2], lo)
            b[3] = c
            b[4] += v
            b[5] += 1
        else:
            buckets[k] = [o, hi, lo, c, v, 1]
    full, dropped = [], []
    for k in sorted(buckets):
        b = buckets[k]
        if b[5] == 4:
            full.append((k, b[0], b[1], b[2], b[3], b[4]))
        else:
            dropped.append((k, b[5]))
    return full, dropped


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--bars", type=int, default=6000,
                    help="拉多少根 15m（4 根 = 1 个 1h 桶）")
    ap.add_argument("--range-db", default="data/market_range.db")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    need_1h = WARMUP_1H_BARS
    print("[回填] 品种=%s 目标 15m=%d 根（约 %d 个 1h 桶，引擎预热需要 %d）"
          % (a.symbol, a.bars, a.bars // 4, need_1h))

    rows15 = fetch_15m(a.symbol, a.bars)
    print("[回填] 实拉 15m=%d 根，范围 %s ~ %s"
          % (len(rows15), ts8(int(rows15[0][0])), ts8(int(rows15[-1][0]))))

    bars, dropped = aggregate_1h(rows15)
    print("[回填] 1h 完整桶=%d，丢弃不完整桶=%d" % (len(bars), len(dropped)))
    for k, n in dropped[:5]:
        print("[回填]   丢弃 %s（只有 %d 根 15m）" % (ts8(k), n))
    if len(bars) < need_1h:
        print("[回填] 警告：1h 桶 %d 个 < 预热 %d，引擎可能算不出结果。"
              "请加大 --bars。" % (len(bars), need_1h))

    t0 = time.time()
    events, states = build(bars)
    print("[回填] 引擎完成：states=%d events=%d 耗时 %.1fs"
          % (len(states), len(events), time.time() - t0))
    if not states:
        print("[回填] 失败：states 为空，没有可落库的数据")
        return 2

    if a.dry_run:
        print("[回填] --dry-run，不落库")
        return 0

    store = RangeStore(a.range_db)
    try:
        got = persist_run(store, a.symbol, bars, events, states, ts8, lab)
        print("[回填] 落库: %s" % got)
        print("[回填] 当前计数: %s" % store.counts(a.symbol))
    finally:
        store.close()
    print("[回填] 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
