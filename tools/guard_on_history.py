# -*- coding: utf-8 -*-
"""把完整历史喂进【带 Guard 的真实引擎】，看硬停会不会触发。

为什么必须做这件事（第 121 轮）：

  HANDOFF 六点五十四 用部署代码测出真实最大回撤是 **75.53%**（逐根盯市、全历史）：

      逐根盯市   75.53%     <- 真实体验
      逐笔已实现 61.15%     <- 系统性低估 14.38 个百分点
      买入持有   83.91%     <- 基准

  而 Guard 的 max_drawdown_pct = 60.0，到点就 _halt_hard()，
  之后【必须人工复位】，不会自己恢复。

  75.53% > 60% ⇒ 这条硬停一定会在历史里触发。
  问题不是"会不会触发"，而是"触发之后还剩什么"。

  历史回测（+1175%、MDD 46.1%）是不带 Guard 的，
  而线上是带 Guard 的。两个数字回答的是两个不同系统。

复现口径（必须说清楚）：
  1. 步进是 4h，而线上 _step 每 ~241ms 跑一次。
     「引擎卡住」闸比的是决策时钟间隔（stall_grace_ms=30s），
     4h 步进下它每步都触发——那是【回放造成的假象】，不是被测对象。
     这里显式关掉，并在这里写明。
  2. 「当日亏损」闸按日历日切分，而 4h 步进跨日边界是近似的，同样关掉。
  3. 被测的是【回撤硬停】：它只看峰值权益与当前权益，回放里语义完全成立。
  4. 数据源恒为 WS，不改。
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
DB = os.environ.get("BT_DB", "/home/cryptobot/ai-trader/data/backtest.db")


def load_bars():
    c = sqlite3.connect(DB)
    return c.execute(
        "select open_time, o, h, l, c, v from klines "
        "where interval=? order by open_time", ("4h",)).fetchall()


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
        # 注意必须是【属性】而不是方法：paper.py 里写的是 feed.agg.current(3000)，
        # 定义成方法就会变成对函数对象取 .current —— 第一版就这么炸的。
        return self

    def current(self, iv):
        px = _Feed.last_price
        return type("B", (), {"high": px, "low": px, "close": px,
                              "open": px, "volume": 1.0})()


class Bar:
    def __init__(self, t, c):
        self.open_time = t
        self.open = self.high = self.low = self.close = c
        self.volume = 1.0


async def run_once(bars, guard_on, cooldown=None):
    """跑一遍。guard_on=True 时按线上配置挂真 Guard。

    cooldown: 覆盖 hard_halt_cooldown_days。
              None = 用 guard.py 里的默认值（= 线上实际值）。
              0    = 关闭自动解除（旧行为，永久停摆）。
    """
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
    if guard_on:
        g = Guard(cfg)
        g.stall_grace_ms = 10 ** 13          # 4h 步进下无意义，见模块 docstring
        g.daily_loss_halt_pct = 1e9          # 跨日边界近似，同上
        if cooldown is not None:
            g.hard_halt_cooldown_days = float(cooldown)

    feed = _Feed()
    ex = PaperExecutor(feed, cfg)
    e = DecisionEngine(cfg, feed, ex, tdb, guard=g)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT", exec_market="spot")
    e.trend.seeded = True
    e.trend.need_reconcile = False
    e.pos = None

    ts = [int(r[0]) for r in bars]
    cl = [float(r[4]) for r in bars]
    for k in range(min(N + 2, len(cl))):
        e.trend.bars.append({"t": ts[k], "o": cl[k], "h": cl[k], "l": cl[k],
                             "c": cl[k], "v": 1.0})

    peak = 0.0
    mdd = 0.0
    halt_at = None
    halt_eq = None
    n_open = 0
    prev_pos = False
    curve = []

    for i in range(N + 1, len(cl)):
        e.trend.bars.append({"t": ts[i], "o": cl[i], "h": cl[i], "l": cl[i],
                             "c": cl[i], "v": 1.0})
        e.trend.last_eval_bar = ts[i]
        _Feed.last_price = cl[i]
        eq = float((await ex.equity())["equity"])
        if g is not None:
            g.update(eq, "WS", ts[i] + IV - 1)
        before = e.pos is not None
        await e._step_trend({IV: [Bar(ts[i], cl[i])]}, cl[i], eq, ts[i] + IV - 1)
        after = e.pos is not None
        if not before and after:
            n_open += 1
        prev_pos = after
        eq2 = float((await ex.equity())["equity"])
        curve.append(eq2)
        peak = max(peak, eq2)
        if peak > 0:
            dd = (peak - eq2) / peak * 100.0
            if dd > mdd:
                mdd = dd
        if g is not None and g.st.halted_hard and halt_at is None:
            halt_at = ts[i]
            halt_eq = eq2

    time.sleep(0.5)
    tdb.stop()
    try:
        os.unlink(path)
    except OSError:
        pass
    return {"opens": n_open, "final": curve[-1] if curve else 0.0,
            "mdd": mdd, "halt_at": halt_at, "halt_eq": halt_eq,
            "halted": bool(g and g.st.halted_hard)}


bars = load_bars()
print("载入 4h K：%d 根（%s）" % (len(bars), DB))
if len(bars) < N + 10:
    print("数据不足，退出")
    raise SystemExit(1)
t0 = time.strftime("%Y-%m-%d", time.gmtime(bars[0][0] / 1000))
t1 = time.strftime("%Y-%m-%d", time.gmtime(bars[-1][0] / 1000))
print("区间：%s -> %s" % (t0, t1))

loop = asyncio.new_event_loop()
a = loop.run_until_complete(run_once(bars, guard_on=False))
b = loop.run_until_complete(run_once(bars, guard_on=True))
c = loop.run_until_complete(run_once(bars, guard_on=True, cooldown=0))

print("")
print("=== 不带 Guard（= 历史回测口径）===")
print("  开仓 %d 次  末值 %.2fU  最大回撤 %.2f%%" % (a["opens"], a["final"], a["mdd"]))
print("=== 带线上 Guard（max_drawdown_pct=60）===")
print("  开仓 %d 次  末值 %.2fU  最大回撤 %.2f%%" % (b["opens"], b["final"], b["mdd"]))
if b["halted"]:
    print("  ★ 硬停触发于 %s，当时权益 %.2fU" % (
        time.strftime("%Y-%m-%d", time.gmtime(b["halt_at"] / 1000)), b["halt_eq"]))
    print("  ★ 硬停之后【需要人工复位】，回放里没有任何人复位它")
else:
    print("  未触发硬停")
print("")
print("=== 带 Guard 但关闭自动解除（旧行为，cooldown=0）===")
print("  开仓 %d 次  末值 %.2fU  最大回撤 %.2f%%  %s" % (
    c["opens"], c["final"], c["mdd"],
    "硬停后永久停摆" if c["halted"] else "未触发硬停"))
print("")
print("对照：")
print("  不带 Guard          %.2fU" % a["final"])
print("  带 Guard + 自动解除  %.2fU  (%+.1f%%)" % (
    b["final"], (b["final"] - a["final"]) / a["final"] * 100.0 if a["final"] else 0.0))
print("  带 Guard 不解除      %.2fU  (%+.1f%%)" % (
    c["final"], (c["final"] - a["final"]) / a["final"] * 100.0 if a["final"] else 0.0))