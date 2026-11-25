# -*- coding: utf-8 -*-
"""把这笔交易的 5m 走法摊开：收盘跌破/有效跌破到底会在哪一步、哪个价上触发。

只读。目的是用真实 K 线回答：
  1. 成交那根（21:30-21:35）5m 收盘在买点哪一侧？
  2. 之后有没有回抽到买点附近？（决定"挂价等回抽"是否可行）
  3. 结构位/止损位在扫盘区里的位置：57454 那一插之后是不是立刻反弹？
  4. 按「收盘跌破才走」的话，实际会在哪根、哪个价出场？
"""
import asyncio
import os
import sqlite3
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, "/home/cryptobot/ai-trader")

from ai_trader.config import load                       # noqa: E402
from ai_trader.exchange.live import LiveExecutor        # noqa: E402

PEND = "/home/cryptobot/ai-trader/data/market_pending.db"
ENTRY = 58437.0
THESIS = "下跌三引线假突破限价单|up|1795606500000|58437.0000"


def env(name):
    v = os.environ.get(name)
    if v:
        return v
    try:
        for line in open("/etc/ai-trader/env", encoding="utf-8"):
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def t(ms):
    return time.strftime("%m-%d %H:%M", time.localtime(int(ms) / 1000.0))


def thesis_row():
    c = sqlite3.connect("file:%s?mode=ro" % PEND, uri=True)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(pending_theses)")]
        row = c.execute("select * from pending_theses where thesis_id=?",
                         (THESIS,)).fetchone()
        if not row:
            return {}
        return dict(zip(cols, row))
    finally:
        c.close()


async def main():
    d = thesis_row()
    print("=" * 88)
    print("① 这条预期的参数（它自己的结构位在哪）")
    print("=" * 88)
    if d:
        for k in ("thesis_id", "kind", "direction", "state", "order_price",
                  "stop_price", "target_price", "range_lo", "range_hi",
                  "key_level", "note"):
            if k in d:
                print("  %-14s %s" % (k, d[k]))
        print("  其余非空字段：")
        for k, v in d.items():
            if v not in (None, "", 0) and k not in (
                    "thesis_id", "kind", "direction", "state", "order_price",
                    "stop_price", "target_price", "range_lo", "range_hi",
                    "key_level", "note"):
                print("    %-16s %s" % (k, v))

    key, sec = env("AI_API_KEY"), env("AI_API_SECRET")
    cfg = load()
    cfg.trading.symbol = "BTCUSDT"
    ex = LiveExecutor(SimpleNamespace(last_price=0.0), cfg, key, sec)
    try:
        t0 = int(time.mktime(time.strptime("2026-11-25 20:30:00",
                                           "%Y-%m-%d %H:%M:%S")) * 1000)
        bars = await ex._req("GET", "/fapi/v1/klines",
                             {"symbol": "BTCUSDT", "interval": "5m",
                              "startTime": t0, "limit": 40})
        print("\n" + "=" * 88)
        print("② 5m K 线（★ = 收盘在买点 %.2f 下方；+ = 那根收在买点上方）"
              % ENTRY)
        print("=" * 88)
        print("   时间        开        高        低        收   收盘vs买点   量")
        lo_so_far = 9e9
        anchor = None
        for b in bars:
            ts, o, h, l, c = (int(b[0]), float(b[1]), float(b[2]),
                              float(b[3]), float(b[4]))
            if ts > int(time.mktime(time.strptime("2026-11-25 23:05:00",
                                                  "%Y-%m-%d %H:%M:%S")) * 1000):
                break
            lo_so_far = min(lo_so_far, l)
            mark = "★" if c < ENTRY else "+"
            if c < ENTRY and anchor is None:
                anchor = ts
            print("   %s %9.2f %9.2f %9.2f %9.2f   %s %+8.2f   %8.1f"
                  % (t(ts), o, h, l, c, mark, c - ENTRY, float(b[5])))
        print("\n  期间最低价 = %.2f（对入场 %.2f 是 %.2f%%）"
              % (lo_so_far, ENTRY, (lo_so_far / ENTRY - 1) * 100))

        print("\n" + "=" * 88)
        print("③ 成交那根（21:30-21:35）的分辨率：1m")
        print("=" * 88)
        t1 = int(time.mktime(time.strptime("2026-11-25 21:25:00",
                                           "%Y-%m-%d %H:%M:%S")) * 1000)
        m1 = await ex._req("GET", "/fapi/v1/klines",
                           {"symbol": "BTCUSDT", "interval": "1m",
                            "startTime": t1, "limit": 25})
        for b in m1:
            print("   %s  开 %9.2f 高 %9.2f 低 %9.2f 收 %9.2f  %s"
                  % (t(int(b[0])), float(b[1]), float(b[2]), float(b[3]),
                     float(b[4]), "★收盘在买点下" if float(b[4]) < ENTRY else ""))

        print("\n" + "=" * 88)
        print("④ 止损被扫那一刻（22:40-22:50）的 1m：扫完有没有立刻反弹")
        print("=" * 88)
        t2 = int(time.mktime(time.strptime("2026-11-25 22:38:00",
                                           "%Y-%m-%d %H:%M:%S")) * 1000)
        m2 = await ex._req("GET", "/fapi/v1/klines",
                           {"symbol": "BTCUSDT", "interval": "1m",
                            "startTime": t2, "limit": 14})
        for b in m2:
            print("   %s  开 %9.2f 高 %9.2f 低 %9.2f 收 %9.2f"
                  % (t(int(b[0])), float(b[1]), float(b[2]), float(b[3]),
                     float(b[4])))
    finally:
        await ex.close()


asyncio.get_event_loop().run_until_complete(main())
