# -*- coding: utf-8 -*-
"""趋势交易的 MFE/MAE 必须是真的最大偏移，不能是占位值。

问题（已确认）：
  趋势模式下 _step_trend 提前 return，_manage 永远不跑。
  而涨跌幅跟踪（p["best"]）只写在 _manage 里。
  结果：
    mfe 永远是 0.0              （best 从未更新，恒等于 entry）
    mae 是「出场价的反向移动」   （用 exit_px 冒充最大不利偏移）

  这两个值会写进 trades 表。将来谁用它标定止损（比如那个从没触发过的
  25% 熔断档），就会基于假数据做决定。数据说谎比没数据更糟。

正确行为：
  持仓期间持续记录最高价和最低价，
  平仓时 mfe = (最高 - 开仓) * 方向，mae = (最低 - 开仓) * 方向。
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
ENTRY = 100.0


class Fill:
    def __init__(self, qty, px):
        self.ok = True
        self.price = px
        self.qty = qty
        self.fee = 0.0
        self.order_id = "1"
        self.status = "filled"
        self.reason = ""


class Ex:
    name = "spot"

    def __init__(self):
        self.orders = []
        self.held = 0.0

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty))
        if side == "buy":
            self.held += qty
        else:
            self.held = max(0.0, self.held - qty)
        return Fill(qty, self.held if False else ENTRY)

    async def equity(self):
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
                "position_qty": self.held}

    async def position(self):
        return {"qty": self.held, "side": "buy" if self.held else "",
                "entry": ENTRY if self.held else None}

    async def verify_price(self, px):
        return px


class _Hub:
    def publish(self, m):
        pass


class _Agg:
    def __init__(self, px):
        self.set(px)

    def set(self, px, hi=None, lo=None):
        self._b = Bar(interval_ms=IV3S, open_time=0, open=px,
                      high=hi if hi is not None else px,
                      low=lo if lo is not None else px,
                      close=px, volume=1.0, closed=True)

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


@pytest_asyncio.fixture
async def env():
    tmp = tempfile.mkdtemp()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    ex = Ex()
    eng = DecisionEngine(cfg, Feed(ENTRY), ex, tdb, guard=None)
    eng.ex = ex
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
                      for i, p in enumerate([90.0, 95, 98, 99, 100, 101, 105.0])]
    eng.trend.seeded = True
    try:
        yield eng, tdb, ex
    finally:
        tdb.stop()


def _snap():
    s = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    s[IV4H] = []
    return s


def _open_pos(eng):
    # 交易所必须如实持有这 0.001，否则周期对账会判定"交易所没仓位"，
    # 把本地仓位清掉——之后走的是空仓开新仓的分支，测的就不是持有了。
    eng.ex.held = 0.001
    eng.pos = {"side": "buy", "qty": 0.001, "entry": ENTRY, "stop": 50.0,
               "target": 0.0, "entry_ts": 0, "bars": 0, "best": ENTRY,
               "worst": ENTRY, "trade_id": None, "max_hold_bars": 600,
               "open_reason": "t", "fee": 0.0, "signal": "trend_entry"}


@pytest.mark.asyncio
async def test_mae_tracks_true_adverse_excursion(env):
    """持多期间价格先跌到 85 再涨到 120 平仓：MAE 必须是 -15，不是出场移动。"""
    eng, tdb, ex = env
    _open_pos(eng)

    # 关键：high/low 必须和 close 不同，否则"用 close 当极值"的实现
    # 会得到一样的结果，测试就区分不出真假跟踪。
    # 这里收在 88，但最低插到 85——只有真正读 low 才能拿到 -15。
    eng.feed.agg.set(88.0, hi=101.0, lo=85.0)
    await eng._step(_snap())
    p = eng.pos
    assert p is not None, "提前平仓了，测试前提不成立"
    assert p["worst"] == 85.0, (
        "持仓期间没有跟踪最低价：worst 还是 %.2f。"
        "趋势模式不调用 _manage，所以 best/worst 都得在这里自己更新。" % p["worst"])

    # 收在 115，但最高摸到 120——只有真正读 high 才能拿到 +20。
    eng.feed.agg.set(115.0, hi=120.0, lo=118.0)
    await eng._step(_snap())
    p = eng.pos
    assert p is not None
    assert p["best"] == 120.0, "没有跟踪最高价：best 还是 %.2f" % p["best"]

    # 让一根 4h K 线【收盘】在均线下方 -> 触发出场。
    # 注意不能用实时价来触发出场：趋势策略按已收盘的 4h K 线判断
    # （state() 用的是 bars[-1].close，不是当前价），这是有意的前视安全设计。
    # 出场那根的 low 必须高于 85，否则"跌到更低才出场"本身就是新的不利偏移，
    # worst 会跟着变大——那是事实，不是 bug。
    hist = eng.trend.bars + [{"t": 7 * IV4H, "o": 92.0, "h": 92.0,
                              "l": 89.0, "c": 90.0, "v": 1.0}]
    eng.trend.bars = hist
    # 出场这根：收 92（低于均线 98.6），但没跌破前面的 85，所以 MAE 应保持 -15
    eng.feed.agg.set(92.0, hi=92.0, lo=89.0)
    snap = _snap()
    snap[IV4H] = [Bar(interval_ms=IV4H, open_time=7 * IV4H, open=92.0,
                      high=92.0, low=89.0, close=90.0, volume=1.0, closed=True)]
    await eng._step(snap)

    tdb.stop()          # 异步写队列必须先停，否则新连接读不到
    import sqlite3
    c = sqlite3.connect(eng.db._path)
    row = c.execute("SELECT mfe, mae, pnl_net FROM trades ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "平仓没有写 trades"
    mfe, mae, pnl = row
    assert abs(mfe - 20.0) < 0.01, \
        "MFE 应为 (120-100)=+20，实际 %s。趋势模式不跑 _manage，best 一直是开仓价，" \
        "所以 MFE 恒为 0——这个字段在骗人。" % mfe
    assert abs(mae - (-15.0)) < 0.01, \
        "MAE 应为 (85-100)=-15，实际 %s。旧实现用出场价冒充，" \
        "会得到 -40（60-100），那是出场移动不是最大不利偏移。" % mae
