# -*- coding: utf-8 -*-
"""信号级 parity：线上趋势策略和回测必须在同一根 K 线上进出场。

为什么这比 PnL 级 parity 更强：
  PnL 级只比对最终收益（笔记里那个 0.88% 误差）。
  两个不同的策略也可能撞出相近的收益——那叫巧合，不叫一致。
  信号级比对的是"哪一根进、哪一根出"，一次不一致就暴露。

回测定义（tools/long_only.py 的 sma_at / sim_sma）：
  sma_at(i, n) = mean(C[i-n:i])          不含第 i 根
  进场：C[i] > sma_at(i) 且 C[i-1] <= sma_at(i-1)
  出场：C[i] < sma_at(i)

线上定义（ai_trader/trading/trend.py 的 state / want_entry）：
  above      = cl[-1] > mean(cl[-(n+1):-1])
  prev_above = cl[-2] > mean(cl[-(n+2):-2])
  want_entry = above and not prev_above

如果这两套定义有任何一处不同，线上跑的就是一个没回测过的策略。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading.trend import TrendStrategy

N = 200
IV = 14400000


def _sma(cl, i, n):
    if i < n:
        return None
    return sum(cl[i - n:i]) / n


def _backtest_signals(cl, n, h=0.0):
    """h 是滞回带半宽（小数）。必须和线上用同一个 h，否则比的不是同一个策略。

    以前这里写死 h=0，而线上默认 0.2——两条测试的 h 都靠 AppCfg() 的默认值，
    默认值一变测试就红，而且从头到尾没验证过生产配置。
    """
    entries, exits = [], []
    pos = False
    for i in range(n + 1, len(cl)):
        s, sp = _sma(cl, i, n), _sma(cl, i - 1, n)
        if s is None or sp is None:
            continue
        enter_now = cl[i] > s * (1.0 + h)
        enter_prev = cl[i - 1] > sp * (1.0 + h)
        exit_now = cl[i] <= s * (1.0 - h)      # 与引擎 want_exit 一致：跌破压低档才出
        if not pos and enter_now and not enter_prev:
            pos = True
            entries.append(i)
        elif pos and exit_now:
            pos = False
            exits.append(i)
    return entries, exits


def _live_signals(cl, n, h=0.0):
    cfg = AppCfg()
    cfg.trading.sma_lookback = n
    cfg.trading.trend_hysteresis_pct = h * 100.0
    tg = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    tg.seeded = True
    tg.need_reconcile = False
    entries, exits = [], []
    pos = False
    for i in range(len(cl)):
        if i < n + 1:
            continue
        tg.bars = [{"t": k * IV, "o": cl[k], "h": cl[k], "l": cl[k],
                    "c": cl[k], "v": 1.0} for k in range(i + 1)]
        tg.last_eval_bar = i * IV
        st = tg.state(cl[i])
        if not st["ready"]:
            continue
        if not pos:
            if tg.want_entry(cl[i]):
                pos = True
                entries.append(i)
        elif not st["exit_above"]:
            # 引擎用的是 exit_above（压低档），不是裸 above。
            # 这里以前写成 not st["above"]，在 h>0 时比引擎出得早，
            # 等于拿一个近似实现当"线上"，绿得没有意义。
            pos = False
            exits.append(i)
    return entries, exits


def _wave(n=420):
    """能触发多次交叉的合成序列：正弦叠加缓慢上行。"""
    return [round(30000.0 * (1.0 + 0.25 * math.sin(i / 11.0) + i * 0.0008), 2)
            for i in range(n)]


def _alternating(n=420):
    """更极端：快速反复穿越均线，逼出边界情况。"""
    out = []
    for i in range(n):
        out.append(30000.0 * (1.0 + (0.35 if (i // 7) % 2 else -0.35)))
    return out


# 两档都要过：h=0 是与历史一致的老口径，h=0.2 是【生产实际在跑的口径】。
# 以前只跑前者，等于最强的 parity 测试从没验证过生产配置。
HYSTS = [0.0, 0.2]


@pytest.mark.parametrize("h_pct", HYSTS)
def test_entry_exits_identical_on_wave(h_pct):
    h = h_pct / 100.0
    cl = _wave()
    be, bx = _backtest_signals(cl, N, h)
    le, lx = _live_signals(cl, N, h)
    assert be == le, u"h=%.2f%% 进场不一致：回测 %s vs 线上 %s" % (h_pct, be[:12], le[:12])
    assert bx == lx, u"h=%.2f%% 出场不一致：回测 %s vs 线上 %s" % (h_pct, bx[:12], lx[:12])
    assert len(be) >= 3, u"样本太少，这个测试证明不了什么（只有 %d 笔）" % len(be)


@pytest.mark.parametrize("h_pct", HYSTS)
def test_entry_exits_identical_on_alternating(h_pct):
    h = h_pct / 100.0
    cl = _alternating()
    be, bx = _backtest_signals(cl, N, h)
    le, lx = _live_signals(cl, N, h)
    assert be == le, u"h=%.2f%% 进场不一致：回测 %s vs 线上 %s" % (h_pct, be[:12], le[:12])
    assert bx == lx, u"h=%.2f%% 出场不一致：回测 %s vs 线上 %s" % (h_pct, bx[:12], lx[:12])


def _marginal(n=420, over=0.0005):
    """构造一次【擦边】上穿：最后一根只比均线高 over。

    为什么需要它：_wave() 那种大幅振荡序列，穿越点离均线几个百分点，
    0.2% 的滞回带根本插不上手——实测 h=0 和 h=0.2 进场笔数完全相同（都是 4 笔）。
    在那种数据上跑两档参数化，等于同一个测试跑两遍，抓不到任何东西。
    滞回带唯一能改变结果的地方就是擦边穿越，所以自检必须用这种形状。
    """
    cl = [30000.0 + (i % 3) * 0.01 for i in range(n)]
    # 让最后一根的收盘价刚好略高于 SMA*(1+over)，略低于 SMA*(1+0.002)
    sma = sum(cl[-(N + 1):-1]) / N
    cl[-1] = round(sma * (1.0 + over), 2)
    return cl


def _marginal_exit(n=420, under=0.0005):
    """构造一次【擦边下穿】：持有中，最后一根收盘落在 SMA*(1-0.2%) 与 SMA 之间。

    为什么要专门构造：_wave() 那种大幅振荡序列，跌破点远低于两条门槛，
    所以「用 exit_above」和「用裸 above」给出同样的出场点，抓不出差别。
    擦边处这两个判据才分得开：
      引擎（exit_above = cl > sma*(1-h)）：不算跌破 -> 继续持有
      裸 above（cl > sma）              ：算跌破 -> 出场
    """
    cl = [30000.0 + (i % 3) * 0.01 for i in range(n)]
    cl[-2] = 31000.0                    # 前一根明显在均线上方
    sma = sum(cl[-(N + 1):-1]) / N
    cl[-1] = round(sma * (1.0 - under), 2)
    return cl


def test_marginal_exit_separates_the_two_predicates():
    """自检：擦边出场处，引擎必须比裸均线判据【晚】出场。

    这条同时钉住 _live_signals 用的是 exit_above（引擎真值），不是裸 above。
    """
    cl = _marginal_exit()
    h = 0.002
    cfg = AppCfg()
    cfg.trading.sma_lookback = N
    cfg.trading.trend_hysteresis_pct = h * 100.0
    tg = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    tg.seeded = True
    tg.need_reconcile = False
    tg.bars = [{"t": k * IV, "o": cl[k], "h": cl[k], "l": cl[k],
                "c": cl[k], "v": 1.0} for k in range(len(cl))]
    tg.last_eval_bar = (len(cl) - 1) * IV
    st = tg.state(cl[-1])
    assert st["ready"]
    assert st["above"] is False, (
        u"擦边序列没构造对：收盘 %.2f 应当在均线 %.2f 下方" % (cl[-1], st["sma"]))
    assert st["exit_above"] is True, (
        u"擦边序列没构造对：收盘 %.2f 应当在压低档 %.2f 上方"
        % (cl[-1], st["exit_line"]))
    assert tg.want_exit(cl[-1]) is None, u"引擎在擦边处就出场了，滞回带没起作用"

    # 线上近似实现也必须同意引擎：最后一根不能被判成出场。
    # 不断言"完全没有进出场"——这个合成序列的构造本身会带来一次进场，
    # 那与本节要验的东西无关。
    last = len(cl) - 1
    _le, lx = _live_signals(cl, N, h)
    assert last not in lx, (
        u"线上近似在第 %d 根判了出场，而引擎 want_exit 返回 None —— "
        u"说明它用的是裸 above 而不是 exit_above" % last)


def test_hysteresis_actually_changes_the_band():
    """自检：两档参数化必须真的不同，否则就是在自我安慰。

    在擦边穿越上：h=0 应当进场，h=0.2% 应当不进场。
    若这条不成立，上面那些 parametrize 就是同一个测试跑两遍。
    """
    cl = _marginal()
    e0, _ = _backtest_signals(cl, N, 0.0)
    e2, _ = _backtest_signals(cl, N, 0.002)
    assert len(e0) >= 1, u"擦边序列在 h=0 时都没进场，序列没构造对"
    assert len(e2) < len(e0), (
        u"h=0.2%% 进场数 %d 不少于 h=0 的 %d —— 滞回带没起作用，"
        u"参数化测试等于自我安慰" % (len(e2), len(e0)))

    # 线上实现必须同意同样的结论
    l0, _ = _live_signals(cl, N, 0.0)
    l2, _ = _live_signals(cl, N, 0.002)
    assert l0 == e0 and l2 == e2, (
        u"擦边情形下线上与回测不一致：回测 h0=%s h2=%s，线上 h0=%s h2=%s"
        % (e0, e2, l0, l2))


def test_sma_excludes_current_bar():
    """SMA 不含被判定的那根——这是两套实现的共同前提。
    如果有一边把当前根算进去，交叉点会整体提前一根。"""
    cl = _wave()
    cfg = AppCfg()
    cfg.trading.sma_lookback = N
    tg = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    tg.seeded = True
    tg.bars = [{"t": k * IV, "o": cl[k], "h": cl[k], "l": cl[k],
                "c": cl[k], "v": 1.0} for k in range(300)]
    st = tg.state(cl[299])
    # 注意：只喂了前 300 根，期望值也必须只用这 300 根算
    fed = cl[:300]
    expect = sum(fed[-(N + 1):-1]) / N
    assert st["sma"] == pytest.approx(expect, rel=1e-12), (
        u"线上 SMA %.6f 不等于不含当前根的均值 %.6f" % (st["sma"], expect))


def test_live_decision_uses_last_closed_bar_not_live_price():
    """进场判定必须看【最后一根已收盘】的收盘价，不能看实时报价。

    这条是两套实现能对齐的根本原因：回测在第 i 根收盘时决策，
    线上也只在有新的已收盘 4h 根时才可能满足交叉条件。
    如果线上改成用实时价比较，它就会在盘中提前进场——
    那是回测里从未出现过的行为。
    """
    cl = _wave()
    cfg = AppCfg()
    cfg.trading.sma_lookback = N
    tg = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    tg.seeded = True
    tg.bars = [{"t": k * IV, "o": cl[k], "h": cl[k], "l": cl[k],
                "c": cl[k], "v": 1.0} for k in range(300)]
    # 构造一个"已收盘在上方、但实时价远低于均线"的状态
    st = tg.state(1.0)
    assert st["above"] == (cl[299] > st["sma"]), (
        u"above 被实时价影响了：传入 price=1.0 却改变了 above 判定")
    # 反过来，"已收盘在下方、实时价远高于均线"时也不能提前进场
    st2 = tg.state(1e9)
    assert st2["above"] == st["above"], (
        u"above 随传入的实时价变化了——线上会用盘中价提前进场，"
        u"而回测只在收盘决策")
