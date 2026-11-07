# -*- coding: utf-8 -*-
"""看板显示的计划必须和引擎真正执行的计划是同一份计算。

用户的原话是「我要知道你在做什么」。这句话有个隐含前提：
大屏上的数字必须就是系统马上要用的数字，不能是另算一遍的近似值。

以前 _build_trend_snapshot 是自己重算的：
  notional = equity * lev            <- _trend_plan 还要除以 (1 + 手续费缓冲)
  qty      = round(notional/price/step) * step   <- _trend_plan 是向下取整
于是看板显示的仓位比引擎实际下的单更大。
0.3% 的差价不算大，但用户看到的不是系统要做的事。

这条测试把「两者一致」变成硬约束：将来谁在展示层再算一遍，立刻炸。
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.market.agg import Bar
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000
IV3S = 3000


class _Ex:
    name = "spot"

    async def equity(self):
        return {"equity": 100.0, "balance": 100.0, "unrealized": 0.0,
                "position_qty": 0.0}

    async def position(self):
        return {"qty": 0.0, "side": "", "entry": None}


class _Hub:
    def publish(self, m):
        pass


class _Agg:
    def __init__(self, px):
        self._b = Bar(interval_ms=IV3S, open_time=0, open=px, high=px,
                      low=px, close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._b if iv == IV3S else None


class _Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self.agg = _Agg(px)
        self.src = "ws"


def _eng():
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_min_notional = 5.0
    eng = DecisionEngine(cfg, _Feed(78000.0), _Ex(), None, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.bars = [{"t": i * IV4H, "o": 70000.0, "h": 70000.0,
                       "l": 70000.0, "c": 70000.0, "v": 1.0}
                      for i in range(10)]
    eng.trend.seeded = True
    return eng


def test_board_plan_matches_engine_plan():
    """看板的 plan_qty / plan_notional 必须等于 _trend_plan 的真实结果。"""
    eng = _eng()
    price = 78000.0
    equity = 100.0

    real = eng._trend_plan(price, equity, {"name": "x", "reason": "y", "side": "long"})
    assert real.ok, "前提：这个价格和权益应该能建仓，否则测不到东西"

    snap = eng._build_trend_snapshot(price, equity)

    assert snap["plan_qty"] == pytest.approx(real.qty, abs=1e-12), (
        u"看板 qty %.8f 与引擎 qty %.8f 不一致。"
        u"看板必须是同一份计算，不能自己再算一遍。"
        % (snap["plan_qty"], real.qty))
    assert snap["plan_notional"] == pytest.approx(round(float(real.notional), 2),
                                                 abs=0.01), (
        u"看板名义 %.2f 与引擎 %.2f 不一致", snap["plan_notional"], real.notional)


def test_board_shows_why_plan_is_blocked():
    """计划不可行时，看板必须说明原因，不能显示一个"看起来会有"的仓位。"""
    eng = _eng()
    # 权益小到建不了一手
    snap = eng._build_trend_snapshot(78000.0, 1.0)
    assert snap["plan_qty"] == 0.0, "计划不可行时不能显示仓位"
    assert snap.get("plan_blocked"), (
        u"计划被拒时要在看板上说明原因，否则用户只看到 0 不知道为什么")


def test_board_plan_never_exceeds_equity():
    """看板的计划仓位也不能超过权益（这是向上取整会犯的错）。"""
    eng = _eng()
    for eq in (50.0, 77.0, 100.0, 133.0, 250.0):
        snap = eng._build_trend_snapshot(78000.0, eq)
        assert snap["plan_notional"] <= eq * 1.0001, (
            u"权益 %.0fU 时看板显示名义 %.2fU，超过权益了"
            % (eq, snap["plan_notional"]))
