# -*- coding: utf-8 -*-
"""API 抖动时，出场检查不能被跳过。

为什么这是最危险的一类缺陷：
  _step() 第一行就是 eq = await self.ex.equity()。
  实盘现货里这是 HTTP 请求 /api/v3/account，12 秒超时。
  它一抛异常，整个 _step 中断——后面的出场判断、周期对账、风控全部跳过。

  而交易所 API 最容易超时的时刻，正是行情剧烈波动的时候，
  也就是【最需要按时出场】的时候。持仓被套住不是因为策略错，而是因为查余额失败。

正确的安全不对称：
  权益数据过期 -> 绝不能阻挡【卖出】（卖多少是已知的，不需要权益）
  权益数据过期 -> 必须阻挡【买入】（不知道有多少钱，就不能下注）
"""
import os
import sys
import tempfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.market.agg import Bar
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000
IV3S = 3000


class Fill:
    def __init__(self, qty, px):
        self.ok = True
        self.price = px
        self.qty = qty
        self.fee = 0.0
        self.order_id = "1"
        self.status = "filled"
        self.reason = ""


class FlakyEx:
    """equity() 会抛异常，但下单还能成功——模拟交易所 API 抖动。"""
    name = "spot"

    def __init__(self, px):
        self.px = px
        self.eq_fails = False
        self.orders = []
        self.held = 0.0
        self.entry_px = 0.0

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty))
        if side == "buy":
            self.held += qty
            self.entry_px = self.px
        else:
            self.held = max(0.0, self.held - qty)
        return Fill(qty, self.px)

    async def equity(self):
        if self.eq_fails:
            raise RuntimeError("HTTP 503 /api/v3/account 超时")
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
                "position_qty": 0.0}

    async def position(self):
        # 假交易所必须如实报告它"持有"的仓位，否则周期对账会把本地仓位清掉，
        # 测出来的就不是"出场被跳过"，而是"对账把仓位修正成了空仓"——
        # 那是另一个（正确的）行为，会掩盖真正要测的东西。
        return {"qty": self.held, "side": "buy" if self.held else "",
                "entry": self.entry_px if self.held else None}

    async def verify_price(self, px):
        return px


class _Hub:
    def publish(self, m):
        pass


class _Agg:
    def __init__(self, px):
        self._b = Bar(interval_ms=IV3S, open_time=0, open=px, high=px,
                      low=px, close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._b if iv == IV3S else None


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self.agg = _Agg(px)
        self.src = "ws"


def _bars(prices, px=None):
    return [Bar(interval_ms=IV4H, open_time=i * IV4H, open=p, high=p,
                low=p, close=p, volume=1.0, closed=True)
            for i, p in enumerate(prices)]


@pytest_asyncio.fixture
async def env():
    tmp = tempfile.mkdtemp()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_min_notional = 5.0
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    px = 100.0
    ex = FlakyEx(px)
    eng = DecisionEngine(cfg, Feed(px), ex, tdb, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    try:
        yield eng, tdb, ex, cfg
    finally:
        tdb.stop()


@pytest.mark.asyncio
async def test_exit_still_happens_when_equity_api_fails(env):
    """持仓中 + 价格跌破均线 + 查余额失败 -> 仍然必须卖出。"""
    eng, tdb, ex, cfg = env
    hist = [100.0, 101, 102, 103, 104, 105, 110.0]
    eng.trend.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
                      for i, p in enumerate(hist)]
    eng.trend.seeded = True

    # 假装持有多仓（价格已跌破均线，应该出场）
    # ★ side 必须是 "long"，不是 "buy"：引擎自己记账时写的是 `plan.side`
    #   （"long"/"short"），"buy"/"sell" 只出现在下单接口参数里。
    #   原来这里写 "buy" 能过，是因为对账把那笔仓判成"方向不一致"、
    #   走接管重写成 long —— 也就是说这条测试一直在测接管路径的副作用，
    #   而不是它自己声称的「查余额失败时出场照常执行」。
    #   2026-11-24 修好 side 规范化之后真相露出来了，这里改正夹具。
    #
    # ★ stop 必须给一个真实值。唐奇安刻意 stop=0（不设硬止损），
    #   而策略出场要等【4h 收盘】，现价跌到 50 而最后一根 4h 还收在 110
    #   时 `want_exit` 按设计返回 None —— 那不是 bug，是回测口径。
    #   这条测试要钉的是「出场判断被跳过了」，所以让它走熔断这条必然触发的
    #   路：stop=82.5（110 的 25% 熔断）< 现价 50 之外。
    eng.pos = {"side": "long", "qty": 0.001, "entry": 110.0, "stop": 82.5,
               "target": 0.0, "entry_ts": 0, "bars": 0, "best": 110.0,
               "trade_id": None, "max_hold_bars": 600,
               "open_reason": "t", "fee": 0.0, "signal": "trend_entry"}
    ex.held = 0.001          # 交易所那边确实持着这 0.001
    ex.entry_px = 110.0

    ex.eq_fails = True
    eng.feed.agg._b = Bar(interval_ms=IV3S, open_time=0, open=50.0,
                          high=50.0, low=50.0, close=50.0, volume=1.0,
                          closed=True)

    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(hist)

    try:
        await eng._step(snap)
    except Exception:
        pass    # 异常本身可以接受，但出场不能因此被跳过

    # ★ 大小写都要认：走趋势 `_close` 时 side 是小写 "sell"，
    #   走人工仓出场 `_adopted_close` 时是大写 "SELL"（与 `_pending_close`
    #   同约定；生产执行器 `market_order` 内部会 `.upper()/.lower()` 规范化，
    #   两种写法都能正确下单）。这条测试要钉的是「出场没被跳过」，
    #   不是「用了哪种大小写」——按大小写判会变成一条假红。
    sells = [o for o in ex.orders if str(o[0]).lower() == "sell"]
    assert sells, (
        "查余额失败导致出场被跳过：价格已从 110 跌破均线到 50，"
        "但一笔卖单都没发。崩盘时交易所 API 恰好最容易超时，"
        "持仓会因此被套住——这不是策略错，是实现错。")


@pytest.mark.asyncio
async def test_entry_blocked_when_equity_unknown(env):
    """空仓 + 想买 + 查余额失败 -> 绝对不能买（不知道自己有多少钱）。"""
    eng, tdb, ex, cfg = env
    hist = [100.0, 101, 102, 103, 104, 105, 200.0]
    eng.trend.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
                      for i, p in enumerate(hist)]
    eng.trend.seeded = True
    eng.trend.need_reconcile = False

    eng.pos = None
    ex.eq_fails = True

    snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    snap[IV4H] = _bars(hist)

    try:
        await eng._step(snap)
    except Exception:
        pass

    buys = [o for o in ex.orders if o[0] == "buy"]
    assert not buys, (
        "权益未知却下了买单：不知道自己有多少钱就下注，"
        "可能超买后被交易所拒单，或按错误规模建仓。")
