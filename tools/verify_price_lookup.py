# -*- coding: utf-8 -*-
"""验证 B1 的查询链在生产里真的能查到已成交单（只读 + 一次查询）。

做法：从 orders 表里取最近一个 `AI-` 前缀、且已在交易所成交的 client_id，
用**部署后的** `LiveExecutor.closed_order_by_client_id` 去查，
看能不能拿回成交价。查得到 = 还原链路通；查不到 = B1 是空转。
"""
import asyncio
import os
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, "/home/cryptobot/ai-trader")

from ai_trader.config import load                       # noqa: E402
from ai_trader.exchange.live import LiveExecutor        # noqa: E402

TRADE = "/home/cryptobot/ai-trader/data/market_trade.db"


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


def recent_ai_cids(n=6):
    c = sqlite3.connect("file:%s?mode=ro" % TRADE, uri=True)
    try:
        return list(c.execute(
            "select client_id, status, qty, price, ts from orders "
            "where client_id like 'AI-%' order by id desc limit ?", (n,)))
    finally:
        c.close()


async def main():
    key, sec = env("AI_API_KEY"), env("AI_API_SECRET")
    if not key:
        print("!! 没有 API key")
        return 1
    cfg = load()
    cfg.trading.symbol = "BTCUSDT"
    feed = SimpleNamespace(last_price=0.0)
    ex = LiveExecutor(feed, cfg, key, sec)
    try:
        rows = recent_ai_cids()
        if not rows:
            print("库里没有 AI- 开头的单，无法验证")
            return 1
        for cid, status, qty, px, ts in rows:
            print("\n=== %s（本地记录 status=%s %.6f @ %s）"
                  % (cid, status, qty, px))
            o = await ex.closed_order_by_client_id(cid)
            if o is None:
                print("  查不到")
                continue
            print("  查到：status=%s type=%s 成交 %.6f 均价 %.2f 触发 %.2f"
                  % (o.status, o.type, o.executed_qty, o.avg_price,
                     o.stop_price))
        print("\n=== 条件单簿（应包含当前真挂着的保护单，若为空则没有） ===")
        algos = await ex.open_algo_orders("BTCUSDT")
        print("  %d 张" % len(algos))
        for o in algos:
            print("  %s %s %.6f 触发 %.2f 状态 %s"
                  % (o.client_id, o.type, o.qty, o.stop_price, o.status))
    finally:
        await ex.close()
    return 0


sys.exit(asyncio.get_event_loop().run_until_complete(main()))
