# -*- coding: utf-8 -*-
"""强平距离必须明显大于止损线，否则止损永远来不及。

背景（第 98 轮）：
  第 97 轮我写过一个过强的结论：「永续在 100U 上结构性不可用」。
  那句只在 1x 下成立 —— 杠杆会把名义放大，一手就买得起了。

  但杠杆不是随便调的旋钮。实测（BTC 77749，账户 100U，止损线 25%）：
      杠杆  生死线    强平距离
      1x    77.8U    128.2%
      2x    38.9U     63.9%
      3x    25.9U     42.5%
      4x    19.5U     25.3%   <- 强平距离约等于止损线
      5x    15.6U     21.0%   <- 强平距离小于止损线，先爆仓

  5x 时强平距离 21.0% 小于止损 25% —— 价格还没跌到止损位，仓已经没了。
  那种情况下止损线是一个安慰剂：它永远不会被执行，因为账户先死。

  所以这里钉住两条：
    1) 现役配置必须让强平距离明显大于止损线；
    2) 杠杆往上调时，代码给的强平距离必须诚实地往下掉。

  第 2 条是守卫测试本身：如果 liq_distance_pct 被写成常数或公式改错，
  第 1 条可能碰巧还是绿的。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load
from ai_trader.trading.engine import DecisionEngine

PX = 77749.0


class _Feed:
    last_price = PX


def _engine(lev, exec_market='perp'):
    cfg = load()
    cfg.trading.exec_market = exec_market
    cfg.trading.trend_leverage = lev
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = cfg
    eng.tcfg = cfg.trading
    eng.feed = _Feed()
    eng.ex = type('E', (), {'name': 'paper'})()
    return eng


def _floor(eng, price=PX, hi=5000.0):
    """二分找最小可建仓权益。"""
    if not eng._trend_plan(price, hi, {'name': 'x'}).ok:
        raise AssertionError('权益 %.0fU 都开不出仓' % hi)
    lo = 0.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if eng._trend_plan(price, mid, {'name': 'x'}).ok:
            hi = mid
        else:
            lo = mid
    return hi


def _plan(lev, equity=100.0):
    return _engine(lev)._trend_plan(PX, equity, {'name': 'x'})


def test_configured_leverage_would_be_safe_if_switched_to_perp():
    """配置里的 trend_leverage，一旦切去永续，强平够不够远？

    ★ 这条一开始写成 if exec_market != perp: pytest.skip(...)，
      于是现货模式下它永远跳过 —— 一个永远不跑的守卫等于没有。
      改成：不管当前在哪个市场，都按【永续】算一遍配置里的杠杆。
      这样它在现货模式下也是活的：谁把 trend_leverage 调到 4x、5x，
      将来一旦切永续就直接踩雷，而现在就能红。
    """
    t = load().trading
    p = _plan(float(t.trend_leverage))
    assert p.ok, p.reason
    gap = p.liq_distance_pct - p.stop_pct
    assert gap > 10.0, (
        '配置杠杆 %.2fx 切到永续后，强平距离 %.1f%% 只比止损 %.1f%% 多 %.1f 个百分点；'
        '缓冲不够，极端行情会先爆仓再止损'
        % (p.leverage, p.liq_distance_pct, p.stop_pct, gap))


def test_high_leverage_liquidation_lands_inside_the_stop():
    """5x 时强平距离必须落到止损线【里面】—— 可算出来的危险区。

    如果这条突然变绿，说明 liq_distance_pct 的公式被改了，
    所有依赖它的判断都要重看。
    """
    p5 = _plan(5.0)
    assert p5.ok, p5.reason
    assert p5.liq_distance_pct < p5.stop_pct, (
        '5x 时强平距离 %.1f%% 竟比止损 %.1f%% 还远，与推算不符；先查公式是否被改'
        % (p5.liq_distance_pct, p5.stop_pct))


def test_liquidation_distance_shrinks_as_leverage_rises():
    """杠杆越高，强平距离必须越小（守卫公式本身没被写死）。"""
    prev = None
    for lev in (1.0, 2.0, 3.0):
        p = _plan(lev)
        assert p.ok, '杠杆 %.1fx 开不出仓：%s' % (lev, p.reason)
        if prev is not None:
            assert p.liq_distance_pct < prev, (
                '杠杆从低到高，强平距离没有变小（%.1f%% -> %.1f%%）'
                % (prev, p.liq_distance_pct))
        prev = p.liq_distance_pct


def test_perp_tradeable_floor_drops_with_leverage():
    """第 97 轮那句「永续在 100U 上不可用」只在 1x 下成立。

    精确限定：1x 生死线接近 78U，2x 降到约 39U。
    不是「永续不能用」，是「永续 1x 不能用」。
    """
    f1 = _floor(_engine(1.0))
    f2 = _floor(_engine(2.0))
    assert f1 > 70.0, '1x 生死线 %.1fU，与 77.8U 实测不符' % f1
    assert f2 < 45.0, '2x 生死线 %.1fU，与 38.9U 实测不符' % f2
    assert f2 < f1 / 1.5, '加杠杆后生死线没明显下降（%.1fU -> %.1fU）' % (f1, f2)
