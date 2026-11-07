# -*- coding: utf-8 -*-
"""纸面模式和回测用的手续费不是同一个数。

这是我在核对线上配置时发现的：

  config.py 默认       taker_fee = 0.0005
  线上环境            没覆盖，所以是 0.0005
  所有测试             显式设成 0.001
  tools/replay_engine  显式设成 0.001
  回测                按 0.001 算

为什么这是错的：币安【现货】真实 taker 就是 0.1%（0.0010）。
0.0005 是永续的费率。我们现在跑的是现货，于是：

  纸面每笔按 0.05% 收，现实收 0.10% —— 少收了一半。

为什么这很重要：策略 6.84 年 213 笔交易，每笔两次收费。
成本敏感性实测：
  每边 0.05% -> 累计 +1298%
  每边 0.10% -> 累计 +1032%
差 266 个百分点。而这正好就是「纸面费率」和「现货真实费率」之间的距离。

后果：线上纸面的权益曲线系统性偏乐观，而且和回测（0.001）不可比。
拿它当「纸面样本」证据，等于用一个比现实便宜一半的世界来证明我能赚钱。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.exchange.paper import PaperExecutor


class _Agg:
    def __init__(self, px):
        from ai_trader.market.agg import Bar
        self._b = Bar(interval_ms=3000, open_time=0, open=px, high=px,
                      low=px, close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._b


class _Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.agg = _Agg(px)


SPOT_TAKER = 0.0010   # 币安现货真实 taker
PERP_TAKER = 0.0005   # 币安永续常见 taker（VIP0 约 0.04-0.05%）


def _fee_for(market, px=78000.0, qty=0.001):
    cfg = AppCfg()
    cfg.trading.exec_market = market
    cfg.trading.slippage_bps = 0.0        # 去掉滑点，只看手续费
    feed = _Feed(px)
    ex = PaperExecutor(feed, cfg)

    import asyncio
    r = asyncio.get_event_loop().run_until_complete(
        ex.market_order("buy", qty, reason="测试"))
    assert r.ok, r.reason
    return r.fee


def test_spot_paper_fee_matches_real_spot_fee():
    """现货模式下，纸面必须按现货真实费率收（0.1%），不是永续的 0.05%。"""
    fee = _fee_for("spot")
    expected = 78000.0 * 0.001 * SPOT_TAKER
    assert fee == pytest.approx(expected, rel=1e-6), (
        u"现货纸面收 %.6fU，现货真实应为 %.6fU（0.1%%）。"
        u"收少了会让纸面权益曲线系统性偏乐观——"
        u"每边 0.05%% 和 0.10%% 在 6.84 年里差 266 个百分点。"
        % (fee, expected))


def test_perp_paper_fee_unchanged():
    """永续模式的费率不能被改坏。"""
    fee = _fee_for("perp")
    expected = 78000.0 * 0.001 * PERP_TAKER
    assert fee == pytest.approx(expected, rel=1e-6), (
        u"永续纸面收 %.6fU，应为 %.6fU" % (fee, expected))


def test_paper_fee_is_comparable_to_backtest():
    """纸面费率和回测/测试用的 0.001 必须是同一个数量级。

    否则「纸面样本」和「回测结果」说的不是同一件事。
    """
    fee = _fee_for("spot")
    notional = 78000.0 * 0.001
    rate = fee / notional
    assert rate == pytest.approx(0.001, rel=1e-3), (
        u"现货纸面实际费率 %.5f，而回测按 0.001 算。"
        u"两者不可比，纸面样本不能用来印证回测。" % rate)
