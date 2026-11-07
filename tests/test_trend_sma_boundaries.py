# -*- coding: utf-8 -*-
"""趋势策略 SMA 的三个边界：窗口取哪几根、预热差一根、平局算不算在上方。

怎么找到的（第 107 轮）：
  对 state()/want_entry()/want_exit()/startup_entry() 做了一组【边界】变异，
  20 个趋势/信号相关测试文件跑下来，9 个变异里 6 个被杀、3 个存活：

    SMA 窗口含最后一根          KILLED (7 failed)
    above 用 >= 代替 >         SURVIVED
    进场带正负搞反              KILLED (8 failed)
    出场带正负搞反              KILLED (5 failed)
    want_entry 去掉 not        KILLED (13 failed)
    want_entry and 改 or       KILLED (12 failed)
    startup_entry 用原版 above  KILLED (1 failed)
    预热判断差一根               SURVIVED
    prev_sma 窗口错位           SURVIVED

三个存活的里，`prev_sma 窗口错位` 最要紧：

    s_prev = sum(cl[-(n + 2):-2]) / n     # 正确：到倒数第二根为止的 n 根
    变异成 sum(cl[-(n + 1):-1]) / n       # 变成 s_now 本身

  于是 prev_above 算的是 cl[-2] > s_now 而不是 cl[-2] > s_prev，
  而 want_entry 判的是「上穿」这个【事件】：
      if st["enter_above"] and not st["enter_prev_above"]:
  交叉点被算错 -> 会在错误的那一根开仓。
  这是回测与线上不一致的典型来源，而它一整轮都没被抓住。

  `预热判断差一根` 是静默算错数：len(cl)==n 时，
      cl[-(n+1):-1] 只有 n-1 个元素，却仍然除以 n
  于是得到一个偏小的 SMA，并且 ready=True —— 不报错，只是数字是错的。

  `above 用 >=` 只在 cl[-1] 恰好等于 SMA 时有差别。真实 BTC 收盘价
  永远不会恰好等于 200 根均值，所以【经济上不可达】。
  但契约就是「严格大于」（回测 above(i) = C[i] > sma_at(i,n)），
  而用构造数据（全平价格）能把它钉住，成本很低，所以也测了。

  如实记下来：这一条我没有找到它会造成实际损失的场景。
  加测试的理由是「钉住契约」，不是「修了一个 bug」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import TradingCfg
from ai_trader.trading.trend import TrendStrategy

IV = 14400000


def _tg(bars, n=5, hyst=0.0):
    cfg = TradingCfg()
    cfg.sma_lookback = n
    cfg.trend_hysteresis_pct = hyst
    tg = TrendStrategy(cfg, symbol="BTCUSDT")
    tg.bars = [{"t": i * IV, "o": c, "h": c, "l": c, "c": c, "v": 1.0}
               for i, c in enumerate(bars)]
    return tg


def test_prev_sma_ends_at_the_second_to_last_bar():
    """prev_sma 必须是「到倒数第二根为止的 n 根」，不能等于 s_now。

    变异它不会让任何测试红（第 107 轮实测），但它会让「上穿」这个事件的
    交叉点算错一根，于是 want_entry 在错误的 K 线上开仓。
    回测里 sma_at(i-1, n) 和 sma_at(i, n) 是两个不同的数，线上也必须分开。
    """
    # 让 SMA 明显移动：前 5 根 100，后面几根逐渐抬高
    bars = [100.0, 100.0, 100.0, 100.0, 100.0, 110.0, 120.0]
    tg = _tg(bars, n=5)
    st = tg.state(120.0)

    want_now = sum(bars[-5:]) / 5.0          # 含最后一根的 n 根：110,120 -> 用 -5: 即 100,100,110 ...
    # 正确窗口：cl[-(n+1):-1] = bars[-6:-1]
    correct_now = sum(bars[-6:-1]) / 5.0
    correct_prev = sum(bars[-7:-2]) / 5.0

    assert st["sma"] == pytest.approx(correct_now), (
        u"sma 窗口不对：%r，期望 %r（bars[-6:-1]）" % (st["sma"], correct_now))
    assert st["prev_sma"] == pytest.approx(correct_prev), (
        u"prev_sma 窗口不对：%r，期望 %r（bars[-7:-2]）"
        % (st["prev_sma"], correct_prev))
    assert st["prev_sma"] != pytest.approx(st["sma"]), (
        u"prev_sma 和 sma 相等（%r）—— 说明 prev_sma 用了同一个窗口，"
        u"「上穿」事件的交叉点会被算错一根" % (st["prev_sma"],))


def test_warmup_needs_one_more_bar_than_the_lookback():
    """攒够 n 根还不够：还差一根用来和 SMA 比较，必须继续等。

    变异成 len(cl) < n 时，会把只有 n-1 个数的和仍然除以 n，
    得到一个【偏小的 SMA】而且 ready=True —— 不报错，只是数字是错的。
    """
    for n in (5, 200):
        bars = [100.0] * n
        tg = _tg(bars, n=n)
        st = tg.state(100.0)
        assert st["ready"] is False, (
            u"只有 %d 根（lookback=%d）就 ready 了。"
            u"sma 会拿 %d 个数除以 %d，得到一个偏小的值却不报错。"
            % (len(bars), n, n - 1, n))

    # 补一根之后才 ready，且这时 SMA 用的是正确的 n 个数
    bars = [100.0] * 6
    tg = _tg(bars, n=5)
    st = tg.state(100.0)
    assert st["ready"] is True, "6 根、lookback=5 应该 ready"
    assert st["sma"] == pytest.approx(sum(bars[-6:-1]) / 5.0)


def test_above_is_strictly_greater_not_greater_equal():
    """above 是「严格大于」——平局不算在上方。

    契约来自回测 tools/long_only.py：above(i) = C[i] > sma_at(i, n)。
    用 >= 会让策略在平局那根提前进场。

    如实说明：真实 BTC 收盘价不可能恰好等于 200 根均值，
    所以这一条【经济上不可达】，我没有找到它造成实际损失的场景。
    加测试是为了钉住契约，不是为了修 bug。
    """
    bars = [100.0] * 5 + [100.0]        # 全平：cl[-1] == SMA，平局
    tg = _tg(bars, n=5)
    st = tg.state(100.0)
    assert st["ready"] is True
    assert st["sma"] == pytest.approx(100.0)
    assert st["above"] is False, (
        u"收盘价正好等于 SMA 时被判成「在上方」。回测的定义是 C[i] > sma(i)，"
        u"平局应当算在下方。%r" % (st["above"],))


def test_cross_detection_uses_the_previous_sma_not_the_current_one():
    """上穿事件的判定必须用【上一根的 SMA】做比较基准。

    这条把 prev_sma 和 want_entry 串起来：只测 prev_sma 的话，
    它仍可能是个没人读的字段。
    """
    # 造一个「刚刚上穿」的序列：前段在 SMA 下方，最后一根跳到上方
    n = 5
    bars = [100.0] * (n + 1) + [130.0]
    tg = _tg(bars, n=n)
    st = tg.state(130.0)
    assert st["ready"] is True
    assert st["above"] is True, u"现价 130 应在上方（sma=%r）" % (st["sma"],)
    assert st["prev_above"] is False, (
        u"上一根收盘 100 不该在 SMA 上方（prev_sma=%r）" % (st["prev_sma"],))

    sig = tg.want_entry(130.0)
    assert sig is not None, (
        u"刚从下方穿到上方，want_entry 却没给出信号。"
        u"prev_sma=%r sma=%r —— 如果 prev_sma 用了和 sma 相同的窗口，"
        u"这个上穿事件就会被漏掉" % (st["prev_sma"], st["sma"]))
    assert sig["name"] == "trend_up"
