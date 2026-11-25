# -*- coding: utf-8 -*-
"""只读：还原这笔交易的真实事实链（交易所口径 vs 本地记录）。

用户说「entry 记的数据肯定是错的」，所以必须用**交易所成交记录**当唯一真相：
  1. 交易所真实成交：谁在什么时刻、什么价、买了多少？
  2. 本地 trades 记了几笔、entry 各是多少？
  3. 差额来自哪一步（接管？合并？加权？）
"""
import asyncio
import os
import re
import sqlite3
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, "/home/cryptobot/ai-trader")

from ai_trader.config import load                      # noqa: E402
from ai_trader.exchange.live import LiveExecutor       # noqa: E402

TRADE = "/home/cryptobot/ai-trader/data/market_trade.db"
LOG = "/home/cryptobot/ai-trader/logs/ai-trader.log"
# 只看今天 20:00 之后
SINCE = time.mktime(time.strptime("2026-11-25 20:00:00", "%Y-%m-%d %H:%M:%S"))
SINCE_MS = int(SINCE * 1000)


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
    try:
        return time.strftime("%H:%M:%S", time.localtime(int(ms) / 1000.0))
    except (TypeError, ValueError):
        return str(ms)


async def main():
    key, sec = env("AI_API_KEY"), env("AI_API_SECRET")
    cfg = load()
    cfg.trading.symbol = "BTCUSDT"
    ex = LiveExecutor(SimpleNamespace(last_price=0.0), cfg, key, sec)
    try:
        print("=" * 82)
        print("① 交易所真实成交（userTrades，20:00 之后）")
        print("=" * 82)
        fills = await ex._req("GET", "/fapi/v1/userTrades",
                              {"symbol": "BTCUSDT", "limit": 100})
        rows = [f for f in fills if int(f.get("time") or 0) >= SINCE_MS]
        rows.sort(key=lambda f: int(f.get("time") or 0))
        if not rows:
            print("  （20:00 之后没有成交）")
        tot = 0.0
        for f in rows:
            q = float(f.get("qty") or 0)
            p = float(f.get("price") or 0)
            side = "BUY " if f.get("side") == "BUY" else "SELL"
            tot += q if f.get("side") == "BUY" else -q
            print("  %s %s %.4f @ %-10.2f 净 %+.4f｜单 %s｜%s"
                  % (t(f["time"]), side, q, p, tot,
                     f.get("orderId"), (f.get("clientOrderId")
                                        or "(无 clientOrderId)")))
        print("\n  净持仓（按成交累加）= %+.4f" % tot)

        print("\n" + "=" * 82)
        print("② 交易所订单（allOrders，20:00 之后）")
        print("=" * 82)
        orders = await ex._req("GET", "/fapi/v1/allOrders",
                               {"symbol": "BTCUSDT", "limit": 100})
        o_rows = [o for o in orders if int(o.get("time") or 0) >= SINCE_MS]
        o_rows.sort(key=lambda o: int(o.get("time") or 0))
        for o in o_rows:
            print("  %s %-4s %-9s %-8s 价 %-10s 成交 %-8s 均价 %-10s %s"
                  % (t(o["time"]), o.get("side"), o.get("type"),
                     o.get("status"), o.get("price"), o.get("executedQty"),
                     o.get("avgPrice"), (o.get("clientOrderId") or "")[:30]))

        print("\n" + "=" * 82)
        print("③ 交易所当前持仓")
        print("=" * 82)
        pos = await ex._req("GET", "/fapi/v2/positionRisk",
                            {"symbol": "BTCUSDT"})
        for p in pos:
            amt = float(p.get("positionAmt") or 0)
            if abs(amt) > 1e-9:
                print("  %+0.6f @ %.4f（标记 %.2f）" % (
                    amt, float(p.get("entryPrice") or 0),
                    float(p.get("markPrice") or 0)))
        if not [p for p in pos if abs(float(p.get("positionAmt") or 0)) > 1e-9]:
            print("  空仓")
    finally:
        await ex.close()

    print("\n" + "=" * 82)
    print("④ 本地 trades（今天 20:00 之后开仓/平仓的）")
    print("=" * 82)
    c = sqlite3.connect("file:%s?mode=ro" % TRADE, uri=True)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(trades)")]
        for r in c.execute("select * from trades where entry_ts>=? or "
                           "exit_ts>=? order by id", (SINCE_MS, SINCE_MS)):
            d = dict(zip(cols, r))
            print("  id=%s %s qty=%.4f entry=%.2f exit=%.2f 毛=%.3f"
                  % (d["id"], d["side"], float(d["qty"] or 0),
                     float(d["entry_price"] or 0),
                     float(d["exit_price"] or 0),
                     float(d["pnl_gross"] or 0)))
            print("     开 %s → 平 %s｜%s"
                  % (t(d["entry_ts"]), t(d["exit_ts"]),
                     (d.get("close_reason") or "")[:60]))
    finally:
        c.close()

    print("\n" + "=" * 82)
    print("⑤ 日志关键行（20:00 之后：成交落账 / 接管 / 平仓 / 出场）")
    print("=" * 82)
    pat = re.compile(r"成交落账|接管|平仓|出场|对账|挂单\]")
    lo = time.strftime("%Y-%m-%d 20:00:00")
    hi = time.strftime("%Y-%m-%d 23:59:59")
    n = 0
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line[:19] < lo or line[:19] > hi:
                continue
            if pat.search(line):
                n += 1
                if n <= 90:
                    print("  " + line.rstrip()[:150])
    print("  （共 %d 行匹配，只显示前 90 行）" % n)


asyncio.get_event_loop().run_until_complete(main())
