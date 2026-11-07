# -*- coding: utf-8 -*-
"""下载币安合约历史 K 线，用于回测。

为什么必须做这件事：现在库里只有 10.5 小时的 3s 数据，用这个调参等于用噪音解释噪音。
先用一年 1m 数据把"到底有没有边际"这个问题回答掉，再谈 3s 优化。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://fapi.binance.com/fapi/v1/klines"
INTERVAL_MS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "1h": 3600000}


def fetch(symbol: str, interval: str, start_ms: int, end_ms: int, proxy):
    out = []
    cur = start_ms
    opener = (urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})) if proxy
        else urllib.request.build_opener())
    while cur < end_ms:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": interval,
                                    "startTime": cur, "limit": 1500})
        req = urllib.request.Request(BASE + "?" + q, headers={"User-Agent": "ai-trader/1.0"})
        for attempt in range(4):
            try:
                with opener.open(req, timeout=25) as r:
                    rows = json.loads(r.read())
                break
            except Exception as e:
                if attempt == 3:
                    print("  放弃:", e)
                    return out
                time.sleep(1.5 * (attempt + 1))
        if not rows:
            break
        out.extend(rows)
        cur = rows[-1][0] + 1
        print("  %s ... %s (%d)" % (interval,
              time.strftime("%Y-%m-%d %H:%M", time.localtime(cur / 1000)), len(out)), flush=True)
        if len(rows) < 1500:
            break
        time.sleep(0.12)
    return out


def save(db_path: str, interval: str, rows) -> int:
    c = sqlite3.connect(db_path)
    c.execute("""CREATE TABLE IF NOT EXISTS klines(
        interval TEXT NOT NULL, open_time INTEGER NOT NULL,
        o REAL, h REAL, l REAL, c REAL, v REAL, q REAL, n INTEGER,
        b REAL, s REAL,
        PRIMARY KEY(interval, open_time))""")
    n = 0
    for r in rows:
        try:
            c.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (interval, r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                       float(r[5]), float(r[7]), int(r[8]), float(r[9]), float(r[10])))
            n += 1
        except Exception:
            pass
    c.commit()
    c.close()
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--db", default="")
    ap.add_argument("--proxy", default=os.environ.get("AI_HTTP_PROXY", ""))
    a = ap.parse_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = a.db or os.path.join(root, "data", "backtest.db")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - a.days * 86400000
    print("下载 %s %s 近 %d 天 ..." % (a.symbol, a.interval, a.days))
    rows = fetch(a.symbol, a.interval, start_ms, end_ms, a.proxy or None)
    n = save(db, a.interval, rows)
    print("完成：%d 根，写入 %s" % (n, db))


if __name__ == "__main__":
    main()
