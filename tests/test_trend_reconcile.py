# -*- coding: utf-8 -*-
"""启动对账：站在 SMA 上方却没持仓时必须补进。

不补的话，进程只要在上涨途中重启过一次，就得等到下一次完整交叉才能上车，
回测赚的钱实盘全错过——这是最隐蔽的一类"回测与实盘断裂"。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import TradingCfg
from ai_trader.trading.trend import TrendStrategy


def make(prices, n=5):
    tg = TrendStrategy(TradingCfg(strategy="trend", sma_lookback=n))
    tg.bars = [{"t": i * 1000, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(prices)]
    return tg


def test_reconcile_enters_when_already_above():
    # 一路从 100 涨到 130：prev_above 也是 True，普通信号不会触发
    prices = [100.0, 101, 102, 103, 104, 105, 120, 130]
    tg = make(prices)
    assert tg.want_entry(130.0) is None, "常规信号不该触发（这是原 bug 的表现）"
    sig = tg.startup_entry(130.0)
    assert sig is not None and sig["name"] == "trend_reconcile"


def test_reconcile_stays_flat_when_below():
    prices = [100.0] * 6 + [70.0]
    tg = make(prices)
    assert tg.startup_entry(70.0) is None


def test_reconcile_persists_until_confirmed():
    """对账必须保留到【真的开成】，不能因为"这次没开成"就永久放弃。

    旧行为是先消费后判断：只要第一次评估时价格在均线上方，
    标志就被清掉，不管那笔单开没开成。于是账户资金不足时，
    对账机会被用掉，等钱到了也不会补——只能等下一次上穿。
    """
    prices = [100.0] * 6 + [130.0]
    tg = make(prices)
    assert tg.startup_entry(130.0) is not None
    # 还没开成：必须继续提，否则资金到位后就永远错过了
    assert tg.startup_entry(131.0) is not None, "没开成就放弃对账，资金到位后不会补"
    # 真开成之后才算完成
    tg.mark_reconciled()
    assert tg.startup_entry(131.0) is None, "开成之后还在反复提补仓"
