# -*- coding: utf-8 -*-
"""用【真实引擎】跑历史回放，验证生产代码能复现回测。

为什么需要这个：tools/verify_parity.py 比的是两个我自己写的模型，
从没验证过【真正跑在服务器上的 DecisionEngine】。

注意（踩过的坑）：必须像生产一样先补满历史再开始评估。
第一次写这个脚本时从 i=200 才开始累积 K 线，策略在前段还没 ready，
漏掉了第 1 笔 +73.7%，结果只有 +224%——差点误判成"引擎和回测不一致"。
生产是靠 seed_from_rest(420) 补数的，不会缺，所以回放也必须先喂满。
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.getcwd())
import tools.backtest as bt

from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

NL = 200


class _Hub:
    def publish(self, *a, **k): pass


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg(); self.hub = _Hub(); self.src = "WS"
        outer = self

        class _B:
            def __init__(self, c): self.close = c
        class _Agg:
            def current(self, iv): return _B(outer.last_price)
        self.agg = _Agg()


def _mkbar(r):
    return {"t": int(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}


async def main():
    DB_PATH = os.environ.get("BT_DB", "data/backtest.db")
    rows = [b for b in bt.load(DB_PATH, "4h") if b[5] > 0]
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.sma_lookback = NL
    cfg.trading.trend_stop_pct = 25.0
    cfg.trading.trend_leverage = 1.0
    cfg.trading.taker_fee = 0.001
    cfg.trading.paper_balance = 100.0
    cfg.trading.exec_market = "spot"      # 与生产部署一致：现货 1x

    feed = Feed(rows[NL][4]); feed.cfg = cfg
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db")); tdb.start()
    ex = PaperExecutor(feed, cfg); ex.restore_from_db(tdb)
    ex._slip = lambda s, p: p            # 回放期间不计滑点，与回测同口径
    eng = DecisionEngine(cfg, feed, ex, tdb)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.seeded = True
    eng.trend.need_reconcile = False     # 从空仓开始，不额外补进
    eng._seed_retry_ts = 2**31           # 回放时别去 REST 补数

    # 关键：先像生产一样喂满历史，再开始评估
    eng.trend.bars = [_mkbar(r) for r in rows[:NL + 1]]

    n_close = 0
    orig_close = eng._close
    async def wrapped(reason, exit_px, price, bars_by_iv):
        nonlocal n_close
        n_close += 1
        return await orig_close(reason, exit_px, price, bars_by_iv)
    eng._close = wrapped

    for i in range(NL + 1, len(rows)):
        r = rows[i]; px = r[4]
        feed.last_price = px
        eng.trend.bars.append(_mkbar(r))
        if len(eng.trend.bars) > 1000:
            eng.trend.bars = eng.trend.bars[-1000:]
        eng.trend.last_eval_bar = int(r[0])
        eq = await ex.equity()
        await eng._step_trend({}, px, float(eq["equity"]), int(r[0]))

    eq = await ex.equity()
    final = float(eq["equity"])
    tdb.stop()
    print("真实引擎回放：100.00U -> %.2fU   累计 %+.1f%%   平仓 %d 笔"
          % (final, (final / 100.0 - 1) * 100, n_close))
    # 基准必须【现算】，不能写死。
    # 写死的话换数据回放时会印出一个永远不变的"基准"，
    # 让人误以为引擎和回测对上了——那是假的一致性。
    bh = (rows[-1][4] / rows[0][4] - 1) * 100
    print("买入持有基准  ：100.00U -> %.2fU   累计 %+.1f%%"
          % (100.0 * rows[-1][4] / rows[0][4], bh))
    print("（引擎与回测是否一致，请看 tools/long_only.py 在同库同参数下的输出）")

asyncio.run(main())
