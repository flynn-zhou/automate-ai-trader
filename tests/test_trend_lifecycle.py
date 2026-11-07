# -*- coding: utf-8 -*-
"""整条生命周期：开仓 -> 持有 -> 出场 -> 平仓 -> 记账。

为什么需要这条测试
------------------
前几轮分别修过出场信号（want_exit）、下单路径、记账口径，
每处都有单元测试。但【整条链路作为整体】从没跑过——
趋势策略至今 0 笔成交。

一个从没跑通过的整体流程，各部分分别"测试通过"并不等于它真的能跑。
2026-11-01 出过这个教训：读 bar.vol（真实字段是 volume），
每 3 秒抛一次 AttributeError，被上层 except 吞成"决策步骤异常"——
系统看起来在跑、看板有数据、决策在写，实际上从未评估过。

所以这条测试用真实引擎 + 真实纸面执行器 + 真实数据库，
把一段合成的 4h 序列走完一整个来回，检查每一步都真的发生了。
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV = 14400000
N = 20          # 缩短预热，20 根就够


class _Agg:
    @staticmethod
    def current(iv):
        px = _Feed.last_price
        return type("B", (), {"high": px, "low": px, "close": px,
                              "open": px, "volume": 1.0})()


class _Feed:
    src = "WS"
    last_price = 0.0

    def __init__(self):
        self.cfg = type("C", (), {"market": type("M", (), {
            "fapi_base": "https://fapi.binance.com", "http_proxy": ""})()})()
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self.bar_hooks = []
        self.agg = _Agg()


class _Bar:
    def __init__(self, t, px):
        self.open_time = t
        self.open = px
        self.high = px
        self.low = px
        self.close = px
        self.volume = 1.0


def _build():
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
    feed = _Feed()
    ex = PaperExecutor(feed, cfg)
    e = DecisionEngine(cfg, feed, ex, tdb, guard=None)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT", exec_market="spot")
    e.trend.seeded = True
    e.trend.need_reconcile = False
    e.pos = None
    return e, ex, tdb, path


def _drive(e, ex, closes, t0=1600000000000):
    """把 closes 逐根喂进去，返回每次喂完是否有持仓。"""
    loop = asyncio.new_event_loop()
    states = []
    try:
        e.trend.bars = [{"t": t0 + k * IV, "o": closes[k], "h": closes[k],
                         "l": closes[k], "c": closes[k], "v": 1.0}
                        for k in range(min(N + 2, len(closes)))]
        for i in range(N + 1, len(closes)):
            px = closes[i]
            e.trend.bars.append({"t": t0 + i * IV, "o": px, "h": px, "l": px,
                                 "c": px, "v": 1.0})
            e.trend.last_eval_bar = t0 + i * IV
            _Feed.last_price = px
            eq = float(loop.run_until_complete(ex.equity())["equity"])
            loop.run_until_complete(
                e._step_trend({}, px, eq, t0 + i * IV + IV - 1))
            states.append(e.pos is not None)
    finally:
        loop.close()
    return states


def _flat_then_up_then_down():
    """一段能产生"上穿 -> 持有 -> 下穿"的价格序列。

    ★ 这里踩过一个坑，必须记下来。
    第一版写成 30x100 -> 5x120 -> 25x90，看起来没问题：
    确实开了仓。但它不是被 SMA 出场信号平掉的，而是被
    25% 熔断止损平掉的（决策日志：'平仓 熔断止损 90.0（跌破 25.0%）'）。

    后果：把 want_exit 改成恒不触发，测试依然全绿——
    因为测试从头到尾没走过 SMA 出场那条路。
    一个"通过"了的测试，其实在验证另一条路径。

    所以下穿必须【温和】：跌破均线，但离入场价远不到 25%。
    100 走平 -> 110 拉高形成上穿 -> 106 跌破均线（相对 110 只跌 3.6%）。
    """
    return [100.0] * 30 + [110.0] * 5 + [106.0] * 25


def test_full_cycle_opens_holds_and_closes():
    """一整条生命周期必须真的跑通：开仓、持有、平仓、记账。"""
    e, ex, tdb, path = _build()
    try:
        closes = _flat_then_up_then_down()
        states = _drive(e, ex, closes)
        # 中途必须有过持仓
        assert any(states), (
            u"整段价格里一次都没持仓——上穿信号没有变成真实仓位。"
            u"状态序列：%r" % states)
        # 最后必须已经平掉
        assert states[-1] is False, (
            u"价格已跌破均线，仓位却还挂着：%r" % states)
        import time
        time.sleep(1.0)          # 等单写入队列
        st = tdb.stats("trend") if hasattr(tdb, "stats") else {}
        n = (st.get("count") or st.get("trades") or 0) if isinstance(st, dict) else 0
        assert n >= 1, u"跑完了却没有任何已平仓交易记录：%r" % st
    finally:
        tdb.stop()
        os.unlink(path)


def test_closed_trade_has_sane_pnl_and_fees():
    """平仓记录必须自洽：毛盈亏 - 手续费 = 净盈亏。"""
    e, ex, tdb, path = _build()
    try:
        _drive(e, ex, _flat_then_up_then_down())
        import time
        time.sleep(1.0)
        import sqlite3
        c = sqlite3.connect(path)
        rows = c.execute("SELECT pnl_gross, fee, pnl_net, qty, entry_price,"
                         " exit_price, mfe, mae FROM trades WHERE strategy='trend'").fetchall()
        c.close()
        assert rows, u"没有交易记录"
        for g, fee, net, qty, ep, xp, mfe, mae in rows:
            assert abs((g - fee) - net) < 1e-4, (
                u"毛 %.6f - 费 %.6f != 净 %.6f" % (g, fee, net))
            assert fee > 0, u"手续费为 0：%r" % fee
            assert qty > 0, u"数量为 0：%r" % qty
            assert ep > 0 and xp > 0
            assert mfe >= 0, u"MFE 应该 >= 0（有利方向最大偏移）：%r" % mfe
            assert mae <= 0, u"MAE 应该 <= 0（不利方向最大偏移）：%r" % mae
    finally:
        tdb.stop()
        os.unlink(path)


def test_never_opens_below_sma():
    """反向约束：价格一直在均线下方时，一分钱都不该动。"""
    e, ex, tdb, path = _build()
    try:
        closes = [200.0] * 30 + [150.0] * 25      # 一路下跌，始终在均线下方
        states = _drive(e, ex, closes)
        assert not any(states), (
            u"价格始终在均线下方却开了仓：%r" % states)
    finally:
        tdb.stop()
        os.unlink(path)
