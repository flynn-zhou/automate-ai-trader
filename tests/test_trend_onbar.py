# -*- coding: utf-8 -*-
"""趋势策略喂 K 线时的字段契约。

2026-11-01 真事故：on_bar 里写了 bar.vol，而 Bar 的真实字段是 volume。
结果每 3 秒的决策步骤都抛 AttributeError 被打断，
系统表面在跑（日志有输出、看板有数据），实际从未做过一次有效评估。
这类"沉默的崩溃"比直接报错危险得多，必须用测试钉死。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import TradingCfg
from ai_trader.market.agg import Bar
from ai_trader.trading.trend import TrendStrategy


def _closed_bar(offset_bars=1, price=100.0):
    """造一根已经收盘的 4h K 线。"""
    iv = 14400000
    now = int(time.time() * 1000)
    t = (now // iv) * iv - iv * offset_bars
    return Bar(interval_ms=iv, open_time=t, open=price, high=price * 1.01,
               low=price * 0.99, close=price, volume=12.5,
               quote_volume=1250000.0, closed=True)


def test_on_bar_accepts_real_bar_object():
    tg = TrendStrategy(TradingCfg(strategy="trend", sma_lookback=5))
    assert tg.on_bar(_closed_bar(1, 100.0)) is True
    assert len(tg.bars) == 1
    assert tg.bars[0]["v"] == 12.5
    assert tg.bars[0]["c"] == 100.0


def test_on_bar_rejects_unclosed_bar():
    tg = TrendStrategy(TradingCfg(strategy="trend", sma_lookback=5))
    iv = 14400000
    now = int(time.time() * 1000)
    b = Bar(interval_ms=iv, open_time=(now // iv) * iv, open=100.0,
            high=100.0, low=100.0, close=100.0, volume=0.0)
    assert tg.on_bar(b) is False
    assert tg.bars == []


def test_on_bar_ignores_duplicate():
    tg = TrendStrategy(TradingCfg(strategy="trend", sma_lookback=5))
    b = _closed_bar(1, 100.0)
    assert tg.on_bar(b) is True
    assert tg.on_bar(b) is False
    assert len(tg.bars) == 1

# ---------------- 第 106 轮补：last_eval_bar 是承重的 ----------------


def test_on_bar_advances_last_eval_bar():
    """on_bar 必须推进 last_eval_bar —— 不推进会让策略永久停止开仓。

    为什么这条不是形式主义（第 106 轮用变异发现的）：
    把 trend.py:164 的 `self.last_eval_bar = int(t)` 删掉，跑 test_trend_onbar.py
    【3 条全绿】—— 没有任何测试抓住它。

    而它是承重的，链路是这样的：

        engine.py:702  熔断止损时：self._trend_block_bar = self.trend.last_eval_bar
        engine.py:742  开仓闸门：if self.trend.last_eval_bar and
                                   self.trend.last_eval_bar <= self._trend_block_bar:
                        return      # 本根 4h 内已熔断出局，等下一根再评估

    last_eval_bar 一旦不再推进，止损之后它和 _trend_block_bar 就永远是【同一个旧值】，
    于是 `last_eval_bar <= _trend_block_bar` 恒为真 —— 闸门永远不会放行。
    表现是：策略看起来在跑（每 3s 都有决策记录、看板有数据），
    但此后【再也不会开任何一单】。这和 trend.py 开头记的那类事故同型：
    沉默的失效，而不是报错。

    另外 trend.py:142（seed_from_rest）也推进它，那条已有测试
    （test_hard_stop_path.py::test_seed_from_rest_sets_last_eval_bar）。
    缺的就是 on_bar 这条——补历史走 seed，之后的每根 4h 走 on_bar。
    """
    tg = TrendStrategy(TradingCfg(), symbol="BTCUSDT")
    assert tg.last_eval_bar == 0, "新策略实例的 last_eval_bar 应该是 0"

    b = _closed_bar(1)
    assert tg.on_bar(b) is True
    assert tg.last_eval_bar == int(b.open_time), (
        u"on_bar 接了新 K 却没推进 last_eval_bar（%r，期望 %r）。"
        u"后果是熔断防重入闸门（engine.py:742/774）恒为真，"
        u"策略此后永久不再开仓，而且不报错。"
        % (tg.last_eval_bar, int(b.open_time)))


def test_reentry_gate_opens_on_the_next_bar():
    """把 on_bar 和熔断防重入闸门串起来：止损后下一根 4h 必须能再进。

    上一条证明 on_bar 推进了 last_eval_bar；这条证明【推进这件事本身有用】——
    否则它可能只是一个没人读的字段。

    闸门原文（engine.py:742-747）:
        if (self.trend.last_eval_bar
                and self.trend.last_eval_bar <= self._trend_block_bar):
            return          # 本根 4h 内已熔断出局，等下一根再评估
    """
    tg = TrendStrategy(TradingCfg(), symbol="BTCUSDT")
    b1 = _closed_bar(2)
    assert tg.on_bar(b1) is True

    block_bar = tg.last_eval_bar          # 模拟 engine.py:702 止损时记下的那根

    def _gated(strategy, block):
        return bool(strategy.last_eval_bar) and strategy.last_eval_bar <= block

    assert _gated(tg, block_bar) is True, "同一根 4h 内本该被闸门拦住"

    # 下一根 4h 收盘并喂进来
    b2 = _closed_bar(1)
    assert tg.on_bar(b2) is True
    assert _gated(tg, block_bar) is False, (
        u"下一根 4h 已经喂进来了，熔断防重入闸门却仍然拦着："
        u"last_eval_bar=%r 仍 <= block_bar=%r —— 策略会永久停止开仓"
        % (tg.last_eval_bar, block_bar))
