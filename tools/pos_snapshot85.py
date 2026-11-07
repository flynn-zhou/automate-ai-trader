# -*- coding: utf-8 -*-
"""在真实历史里抓一个"持仓中"的时刻，核对快照/记账的内在一贯性。

为什么做这个：趋势策略线上 0 笔成交，看板的 pos 永远 null。
「有持仓时快照长什么样」从来没被任何真实数据验证过——
而它恰恰是用户要看的"交易因果"里最关键的一段（我在哪个价进的、
止损在哪、现在浮盈多少、为什么还没出）。
"""
import asyncio, json, os, sqlite3, sys, tempfile, time, urllib.request

sys.path.insert(0, os.getcwd())
from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV, N = 14400000, 200
PROXY = os.environ.get("AI_HTTP_PROXY") or ""
opener = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
          if PROXY else urllib.request.build_opener())

def fetch_all():
    rows, end = [], None
    for _ in range(4):
        q = "symbol=BTCUSDT&interval=4h&limit=1000"
        if end:
            q += "&endTime=%d" % end
        req = urllib.request.Request("https://api.binance.com/api/v3/klines?" + q,
                                     headers={"User-Agent": "pos85/1.0"})
        with opener.open(req, timeout=30) as r:
            page = json.loads(r.read())
        if not page:
            break
        rows = page + rows
        end = page[0][0] - 1
    return rows

rows = fetch_all()[-900:]          # 只取近 900 根，够触发几笔
ts = [int(r[0]) for r in rows]
cl = [float(r[4]) for r in rows]
vol = [float(r[5]) for r in rows]
print("样本 %d 根 %s ~ %s" % (len(cl),
      time.strftime('%Y-%m-%d', time.gmtime(ts[0]/1000)),
      time.strftime('%Y-%m-%d', time.gmtime(ts[-1]/1000))))

class _Agg:
    @staticmethod
    def current(iv):
        px = Feed.last_price
        return type("B", (), {"high": px, "low": px, "close": px, "open": px, "volume": 1.0})()

class Feed:
    src = "WS"
    last_price = 0.0
    def __init__(self):
        self.cfg = type("C", (), {"market": type("M", (), {
            "fapi_base": "https://fapi.binance.com", "http_proxy": ""})()})()
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self.bar_hooks = []
        self.agg = _Agg()

class Bar:
    def __init__(self, t, o, h, l, c, v):
        self.open_time, self.open, self.high, self.low, self.close, self.volume = t, o, h, l, c, v

fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
tdb = TradeDB(path); tdb.start()
cfg = AppCfg()
cfg.trading.strategy = "trend"; cfg.trading.exec_market = "spot"
cfg.trading.sma_lookback = N; cfg.trading.paper_balance = 100.0
cfg.trading.trend_leverage = 1.0
cfg.trading.trend_hysteresis_pct = float(os.environ.get("AI_TREND_HYST", "0.2"))
feed = Feed(); ex = PaperExecutor(feed, cfg)
e = DecisionEngine(cfg, feed, ex, tdb, guard=None)
e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT", exec_market="spot")
e.trend.seeded = True; e.trend.need_reconcile = False; e.pos = None

snaps = []
open_bar_ts = [None]          # 模块级：断言循环在 run() 外面还要用
loop = asyncio.new_event_loop()

async def run():
    for k in range(min(N + 2, len(cl))):
        e.trend.bars.append({"t": ts[k], "o": cl[k], "h": cl[k], "l": cl[k], "c": cl[k], "v": vol[k]})
    held_for = 0
    for i in range(N + 1, len(cl)):
        e.trend.bars.append({"t": ts[i], "o": cl[i], "h": cl[i], "l": cl[i], "c": cl[i], "v": vol[i]})
        e.trend.last_eval_bar = ts[i]
        Feed.last_price = cl[i]
        before = e.pos is not None
        eq = float((await ex.equity())["equity"])
        await e._step_trend({IV: [Bar(ts[i], cl[i], cl[i], cl[i], cl[i], vol[i])]},
                            cl[i], eq, ts[i] + IV - 1)
        after = e.pos is not None
        snap = dict(e._trend_snapshot or {})
        if not before and after:
            # 记住开仓发生在哪一根：后面持仓中/平仓时刻的 entry_ts 都要对到这一根，
            # 不能拿"当前这一根"去比（第一版就是这么错的）。
            open_bar_ts[0] = ts[i]
            snaps.append(("开仓", i, snap))
            held_for = 0
        elif before and after:
            held_for += 1
            if held_for == 1:
                snaps.append(("持仓中", i, snap))
        elif before and not after:
            snaps.append(("平仓", i, snap))
        if len(snaps) >= 3:
            break
    return snaps

snaps = loop.run_until_complete(run())
_ = None
time.sleep(1.2); tdb.stop()

print("\n抓到 %d 个时刻" % len(snaps))
ok_all = True
for tag, i, s in snaps:
    print("\n=== %s  (第 %d 根, %s, 收盘 %.1f) ===" % (
        tag, i, time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts[i]/1000)), cl[i]))
    p = s.get("pos")
    keys = ("sma", "dist_pct", "above", "hyst_pct", "pos", "pos_pnl_pct", "hold_h",
            "plan_notional", "plan_qty", "plan_blocked", "stop_pct", "bar_src")
    for k in keys:
        if k in s:
            print("   %-14s = %r" % (k, s[k]))
    if p:
        entry, stop, qty = float(p["entry"]), float(p["stop"]), float(p["qty"])
        notional = entry * qty
        checks = [
            ("side 是 long", p["side"] == "long"),
            ("qty > 0", qty > 0),
            ("entry > 0", entry > 0),
            ("stop < entry（多头）", stop < entry),
            ("止损距 ~25%%（实际 %.2f%%）" % ((entry-stop)/entry*100),
             abs((entry-stop)/entry*100 - cfg.trading.trend_stop_pct) < 0.5),
            ("步长对齐 1e-05", abs(qty/1e-05 - round(qty/1e-05)) < 1e-6),
            ("名义 >= 5U（实际 %.2f）" % notional, notional >= 5.0),
            # 成交发生在【该 4h 的收盘时刻】ts[i]+IV-1，不是开盘时刻。
            # 我第一版断言写成 ts[i]，于是报"不自洽"——错的断言不是发现，是噪音。
            ("entry_ts = 开仓那根的收盘时刻",
             open_bar_ts[0] is not None
             and int(p["entry_ts"]) == int(open_bar_ts[0]) + IV - 1),
            ("pos_pnl_pct 与价格自洽",
             abs(float(s.get("pos_pnl_pct", 0)) - (cl[i]-entry)/entry*100) < 1e-6),
        ]
        for name, good in checks:
            print("     [%s] %s" % ("OK" if good else "!!", name))
            ok_all = ok_all and good
    else:
        print("     pos = None")

print("\n注：hold_h 在回放里用的是真实墙钟，回放的是历史时间，所以数值无意义，不作断言。")
print("注：平仓那一刻快照里仍有 pos，是【决策前快照】的有意设计（engine.py:655 在持仓分支之前），")
print("    下一个 3 秒心跳就会自我纠正。挪到分支之后会重新踩中 638-654 行记录的那个坑，不要改。")
print("\n结论:", "快照在持仓期各项自洽" if ok_all else "有不自洽项")
