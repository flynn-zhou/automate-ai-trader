# -*- coding: utf-8 -*-
"""扫 max_drawdown_pct：硬停线设在哪，才不至于把策略整个停死。

背景（第 121 轮）：
  全历史 2017-2026 用带 Guard 的真实引擎跑出来：
    不带 Guard          201 次开仓  100U -> 2300.19U  回撤 77.10%
    带 60% 硬停（线上）   14 次开仓  100U ->  118.30U  2018-03-08 停死

  原因很简单：策略历史最大回撤 77.10%，而硬停线是 60%。
  60 < 77.10 ⇒ 注定触发；触发后必须人工复位，而机器人没有复位的手段。

  两个数字各自都对，组合起来致命，而从来没人做过这个乘法。

这个脚本扫一遍硬停线，回答「设在哪才不打断策略」。
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())

from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.guard import Guard
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV = 14400000
N = 200
DB = os.environ.get("BT_DB", "/tmp/bt_full.db")

c = sqlite3.connect(DB)
BARS = c.execute("select open_time, o, h, l, c, v from klines "
                 "where interval=? order by open_time", ("4h",)).fetchall()
c.close()
TS = [int(r[0]) for r in BARS]
CL = [float(r[4]) for r in BARS]


class _Feed:
    src = "WS"
    last_price = 0.0

    def __init__(self):
        self.cfg = type("C", (), {"market": type("M", (), {
            "fapi_base": "https://fapi.binance.com", "http_proxy": ""})()})()
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self.bar_hooks = []

    @property
    def agg(self):
        return self

    def current(self, iv):
        px = _Feed.last_price
        return type("B", (), {"high": px, "low": px, "close": px,
                              "open": px, "volume": 1.0})()


class Bar:
    def __init__(self, t, cl):
        self.open_time = t
        self.open = self.high = self.low = self.close = cl
        self.volume = 1.0


async def run(dd_limit, auto_reset_days=0):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tdb = TradeDB(path)
    tdb.start()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = N
    cfg.trading.paper_balance = 100.0
    cfg.trading.trend_leverage = 1.0
    cfg.trading.trend_hysteresis_pct = float(os.environ.get("AI_TREND_HYST", "0.2"))

    g = None
    if dd_limit is not None:
        g = Guard(cfg)
        g.max_drawdown_pct = float(dd_limit)
        g.stall_grace_ms = 10 ** 13
        g.daily_loss_halt_pct = 1e9

    feed = _Feed()
    ex = PaperExecutor(feed, cfg)
    e = DecisionEngine(cfg, feed, ex, tdb, guard=g)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT", exec_market="spot")
    e.trend.seeded = True
    e.trend.need_reconcile = False
    e.pos = None
    for k in range(min(N + 2, len(CL))):
        e.trend.bars.append({"t": TS[k], "o": CL[k], "h": CL[k], "l": CL[k],
                             "c": CL[k], "v": 1.0})

    peak = mdd = 0.0
    opens = 0
    halted_at = None
    days_halted = 0.0
    cur = CL[0]
    for i in range(N + 1, len(CL)):
        e.trend.bars.append({"t": TS[i], "o": CL[i], "h": CL[i], "l": CL[i],
                             "c": CL[i], "v": 1.0})
        e.trend.last_eval_bar = TS[i]
        _Feed.last_price = CL[i]
        eq = float((await ex.equity())["equity"])
        if g is not None:
            g.update(eq, "WS", TS[i] + IV - 1)
            if g.st.halted_hard:
                if halted_at is None:
                    halted_at = TS[i]
                days_halted += 4.0 / 24.0
                # auto_reset_days>0 时模拟「人工按周期复盘复位」
                if auto_reset_days and days_halted >= auto_reset_days:
                    g.reset()
                    days_halted = 0.0
        before = e.pos is not None
        await e._step_trend({IV: [Bar(TS[i], CL[i])]}, CL[i], eq, TS[i] + IV - 1)
        if e.pos is not None and not before:
            opens += 1
        eq2 = float((await ex.equity())["equity"])
        peak = max(peak, eq2)
        if peak > 0:
            mdd = max(mdd, (peak - eq2) / peak * 100.0)
        cur = eq2
    time.sleep(0.3)
    tdb.stop()
    try:
        os.unlink(path)
    except OSError:
        pass
    return {"opens": opens, "final": cur, "mdd": mdd,
            "halted_at": halted_at, "halted": halted_at is not None}


loop = asyncio.new_event_loop()
print("%-14s %8s %10s %9s %s" % ("硬停线", "开仓", "末值", "回撤", "首次硬停"))
print("-" * 66)
r = loop.run_until_complete(run(None))
print("%-14s %8d %10.2f %8.2f%% %s" % ("不设", r["opens"], r["final"], r["mdd"], "--"))
base = r["final"]
for lim in (60, 70, 75, 78, 80, 85, 90, 95):
    r = loop.run_until_complete(run(lim))
    when = time.strftime("%Y-%m-%d", time.gmtime(r["halted_at"] / 1000)) if r["halted"] else "--"
    print("%-14s %8d %10.2f %8.2f%% %s%% %s" % (
        "%d%%" % lim, r["opens"], r["final"], r["mdd"],
        "%+.0f" % ((r["final"] - base) / base * 100.0), when))
print("")
print("--- 60%% 硬停 + 人工每隔 N 天复位 ---")
for d in (30, 90, 180, 365):
    r = loop.run_until_complete(run(60, auto_reset_days=d))
    print("  每 %3d 天复位：开仓 %3d 次  末值 %8.2fU  回撤 %.2f%%" % (
        d, r["opens"], r["final"], r["mdd"]))