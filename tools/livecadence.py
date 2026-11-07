# -*- coding: utf-8 -*-
"""实盘模式下，3 秒的决策节拍真的跑得到了吗？

纸面执行器 equity() 是纯内存计算（微秒级）；实盘 SpotExecutor.equity()
是一次真实的签名 HTTP 请求（/api/v3/account，12s 超时）。
两者在 3 秒周期里的占比完全不同——而纸面永远测不出这件事。

本脚本用带真实延迟分布的假交易所驱动【真实引擎的 _step】，
不睡眠、只测耗时，然后按"K 线每 3 秒来一根、上一轮没跑完就丢"的
语义解析地统计丢轮率。
"""
import asyncio
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.getcwd())

from ai_trader.config import AppCfg
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading import trend as trend_mod


class _Bar:
    def __init__(self, px):
        self.close = self.high = self.low = self.open = px
        self.volume = 1.0
        # engine._log_decision 会读 bar[-1].open_time 当决策时间戳，
        # 少了它整步抛异常 —— 第一版就漏了，于是"测出来的耗时"其实是
        # 异常路径的耗时，比真实路径短得多。
        self.open_time = int(time.time() * 1000)
        self.interval_ms = 3000
        self.closed = True


class _Agg:
    def __init__(self, px):
        self.px = px

    def current(self, iv):
        return _Bar(self.px)


class _Feed:
    src = "WS"

    def __init__(self, cfg, px):
        self.cfg = cfg
        self.agg = _Agg(px)
        self.last_price = px
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()


class _SlowSpot:
    """模拟真实币安现货：每次 equity() 都是一次签名 HTTP 请求。"""

    name = "spot"

    def __init__(self, cfg, px):
        self.cfg = cfg
        self.px = px
        self.calls = 0
        self.fails = 0
        self.hard = 0

    async def equity(self):
        self.calls += 1
        d = random.uniform(0.08, 0.40)
        if random.random() < 0.05:          # 剧烈行情时 /account 会到 1~2 秒
            d = random.uniform(1.0, 2.0)
        if random.random() < 0.002:         # 偶发超时（12s 超时线上直接抛）
            self.hard += 1
            await asyncio.sleep(0.02)
            raise TimeoutError("ClientTimeout(total=12)")
        await asyncio.sleep(d)
        if random.random() < 0.01:
            self.fails += 1
            raise RuntimeError("HTTP 500 /api/v3/account")
        return {"balance": 100.0, "unrealized": 0.0, "equity": 100.0,
                "position_qty": 0.0, "position_side": ""}

    async def position(self):
        await asyncio.sleep(random.uniform(0.08, 0.40))
        return {"qty": 0.0, "entry": None, "side": "", "leverage": 1.0}

    async def spec(self):
        return {"step": 0.00001, "min_qty": 0.00001, "min_notional": 5.0,
                "qty_precision": 8, "base": "BTC", "quote": "USDT"}

    def round_qty(self, q, step):
        return q


class _DB:
    def __init__(self):
        self.eq_rows = 0
        self.risks = []

    def log_equity(self, e):
        self.eq_rows += 1

    def log_decision(self, *a, **k):
        pass

    def log_risk(self, kind, detail):
        self.risks.append((kind, detail[:70]))

    def log_order(self, *a, **k):
        pass

    def log_fill(self, *a, **k):
        pass

    def open_trade(self):
        return None


async def main():
    random.seed(7)
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    px = 78000.0
    feed = _Feed(cfg, px)
    ex = _SlowSpot(cfg, px)
    db = _DB()
    eng = DecisionEngine(cfg, feed, ex, db, guard=None)

    import sqlite3
    conn = sqlite3.connect("data/backtest.db")
    rows = conn.execute("SELECT open_time,c FROM klines WHERE interval='4h'"
                        " ORDER BY open_time DESC LIMIT 420").fetchall()
    conn.close()
    rows = sorted([r for r in rows if r[1] and r[1] > 0])
    tst = trend_mod.TrendStrategy(cfg.trading, symbol=cfg.trading.symbol,
                                  exec_market="spot")
    tst.bars = [{"t": r[0], "o": r[1], "h": r[1], "l": r[1],
                 "c": r[1], "v": 1.0} for r in rows]
    tst.seeded = True
    eng.trend = tst

    bars = {iv: [_Bar(px)] for iv in (3000, 15000, 60000, 180000, 900000)}
    bars[14400000] = [_Bar(rows[-1][1])]

    N = 300
    durs, stale = [], 0
    for _ in range(N):
        t0 = time.perf_counter()
        await eng._on_bar(bars)
        durs.append(time.perf_counter() - t0)
        if getattr(eng, "_equity_stale", False):
            stale += 1

    ms = [d * 1000 for d in durs]
    ms_sorted = sorted(ms)
    # 解析丢轮：K 线每 3 秒来；若某轮耗时 > 3000ms，其后的节拍会被吞掉。
    # 一轮耗时 6.8 秒 -> 覆盖 3 个节拍 -> 丢 2 个。
    covered = sum(max(1, int(round(d / 3.0))) for d in durs)
    dropped = covered - N

    print("=== 实盘节奏（%d 轮真实 _step，假交易所带真实延迟）===" % N)
    print("每轮耗时 ms： 中位 %.0f   P90 %.0f   P99 %.0f   最大 %.0f"
          % (statistics.median(ms), ms_sorted[int(N * .9)],
             ms_sorted[int(N * .99)], max(ms)))
    print("超过 3000ms 的轮次：%d (%.1f%%)"
          % (sum(1 for d in ms if d > 3000),
             100.0 * sum(1 for d in ms if d > 3000) / N))
    print()
    print("equity() 调用 %d 次；失败 %d 次；超时 %d 次"
          % (ex.calls, ex.fails, ex.hard))
    print("判定「权益过期」的轮次：%d (%.1f%%)" % (stale, 100.0 * stale / N))
    print()
    print(">>> 按 3 秒节拍解析：%d 轮覆盖 %d 个节拍，被吞掉 %d 个（%.1f%%）"
          % (N, covered, dropped, 100.0 * dropped / covered))
    print()
    print("风控事件样本:", db.risks[:4], "共", len(db.risks), "条")
    print("权益落库 %d 行" % db.eq_rows)


asyncio.run(main())
