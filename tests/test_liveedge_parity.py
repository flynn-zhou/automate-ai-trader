# -*- coding: utf-8 -*-
"""回测工具和线上策略必须做同一个决定。

为什么需要这个测试（2026-11-02，第 94 轮）：
  tools/liveedge.py 是产出「这个策略能赚多少」头条数字的工具，
  而它长期用【裸均线】进出：  if C[i] > s and C[i-1] <= sp
  线上 TrendStrategy 用的却是带滞回的两条线：
       进  cl[-1] >  sma*(1+h)
       出  cl[-1] <= sma*(1-h)

  于是这个工具量出来的「真实边际」，量的是另一套规则的边际。
  实测差得不小：裸均线 +472% / 181 笔，线上带子 +548% / 126 笔。

  只靠「记得改两边」是不够的 —— 已经忘过一次了。
  这个测试让它们【行为上】必须一致：同一份收盘序列，
  一边只由 TrendStrategy.state() 驱动，一边由 liveedge.simulate() 驱动，
  进场/出场发生在哪一根必须逐笔相同。

  它抓的是【算符走散】：> 写成 >=、+h 写成 -h、拿裸均线比、
  或者两边对「上一根」的定义差一根。这些都不是看一眼能看出来的。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from ai_trader.trading import trend as trend_mod  # noqa: E402
import liveedge                                    # noqa: E402


def _series(n=1100, seed=12345, period=20, amp=0.004, drift=0.006):
    """造一段会反复穿越均线的序列（确定性的，不用 random 模块的状态）。

    参数是量出来定的，不是拍的：n=1100/period=20 给出 23 笔进出、0 次止损。
    太少不行（分支走不到，测试假通过）；触发止损也不行
    （止损是 liveedge 独有的分支，混进来会让对照失去意义）。
    这两个数字由断言守着，样本退化了测试会自己叫。
    """
    x = seed
    out = []
    px = 100.0
    for i in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        noise = (x % 2000 - 1000) / 1000.0        # -1 .. 1
        d = drift if (i // period) % 2 == 0 else -drift
        px = max(1.0, px * (1 + d + noise * amp))
        out.append(px)
    return out


def _trend_signals(closes):
    """只用线上策略的 state() 推演一遍，返回 [(i, action, price)]。"""
    from ai_trader.config import TradingCfg
    st = trend_mod.TrendStrategy(TradingCfg(), symbol='BTCUSDT')
    sig = []
    holding = False
    for i, px in enumerate(closes):
        st.bars.append({'t': i * 14400000, 'o': px, 'h': px, 'l': px, 'c': px})
        s = st.state()
        if not s['ready'] or s['prev_above'] is None:
            continue
        if not holding:
            if s['enter_above'] and not s['enter_prev_above']:
                holding = True
                sig.append((i, 'enter', px))
        else:
            if not s['exit_above']:
                holding = False
                sig.append((i, 'exit', px))
    return sig


def test_liveedge_pling_plings_match_strategy():
    """同一份序列，两边必须逐笔同点。"""
    closes = _series()
    hyst = liveedge.HYST
    got = liveedge.simulate(closes, closes, hyst)['signals']
    want = _trend_signals(closes)
    # 止损线 25% 在这份合成数据上不该被触发；触发了说明数据造得太极端，
    # 那样对比就没意义了（止损是 liveedge 独有的分支）。
    assert got and all(a[1] != 'stop' for a in got), '合成数据触发了止损，样本不合适'
    assert len(want) >= 6, ('对照样本太少（%d 笔），测试会假通过：序列没穿越几次'
                            % len(want))
    assert [(i, a) for i, a, _ in got] == [(i, a) for i, a, _ in want], (
        '回测工具和线上策略的进出点不一致：\n  工具 %r\n  策略 %r'
        % ([(i, a) for i, a, _ in got][:20], [(i, a) for i, a, _ in want][:20]))


def test_band_comes_from_config_not_a_literal():
    """带子必须来自线上配置，不能是工具里自己写的数。"""
    from ai_trader.config import TradingCfg
    assert liveedge.HYST == pytest.approx(
        TradingCfg().trend_hysteresis_pct / 100.0), (
        'liveedge 的滞回带（%r）和线上配置（%r）不一致'
        % (liveedge.HYST, TradingCfg().trend_hysteresis_pct / 100.0))


def test_zero_band_reduces_to_bare_sma():
    """反向守卫：h=0 时带子的两条线必须退化成裸均线。

    这一条防的是「把 h 用错地方」——比如误写成 sma*1+h 而不是 sma*(1+h)，
    h=0.002 时会得到 1.002 倍而不是 1.0002 倍，差 200 倍。
    """
    closes = _series()
    zero = liveedge.simulate(closes, closes, 0.0)
    # h=0 时，进 = C[i] > sma 且 C[i-1] <= sma_prev；和 state() 的 above/prev_above 一致
    from ai_trader.config import TradingCfg
    st = trend_mod.TrendStrategy(TradingCfg(), symbol='BTCUSDT')
    st.hyst = 0.0
    sig = []
    holding = False
    for i, px in enumerate(closes):
        st.bars.append({'t': i * 14400000, 'o': px, 'h': px, 'l': px, 'c': px})
        s = st.state()
        if not s['ready'] or s['prev_above'] is None:
            continue
        if not holding:
            if s['above'] and not s['prev_above']:
                holding = True
                sig.append((i, 'enter', px))
        else:
            if not s['above']:
                holding = False
                sig.append((i, 'exit', px))
    assert [(i, a) for i, a, _ in zero['signals']] == [(i, a) for i, a, _ in sig], (
        'h=0 时带子没有退化成裸均线：\n  工具 %r\n  裸均线 %r'
        % ([(i, a) for i, a, _ in zero['signals']][:12], [(i, a) for i, a, _ in sig][:12]))

def _flat_then(target_mult, n=201):
    """造一段「前 n 根恒为 100，最后一根是 100*(1+h)*倍数」的序列。

    用途：把价格【恰好】放在阈值线上，逼出 > 和 >= 的区别。
    随机序列永远碰不到相等的情况，所以边界必须单独造。

    为什么是 201 根：simulate 里 i >= nl+1 才开始判，nl=200，
    所以第一根被判定的是 i=201，它的 SMA 窗口是 C[1:201]。
    要让那 200 根全是 100，前缀就得有 201 根。少一根的话 SMA 会把这根
    大价格算进均值，条件永远不成立 —— 我第一版就错在这里，被断言抓住了。
    """
    base = [100.0] * n
    return base + [100.0 * target_mult]


def test_strictly_greater_at_the_entry_line():
    """收盘【恰好等于】进场线时不许进——线上是严格大于。

    trend.py 那句注释写的是「严格高于，与 enter_above 一致」。
    如果哪天有人把 > 写成 >=，随机数据是发现不了的（浮点几乎不会相等），
    这一条就是专门盯着它的。
    """
    h = liveedge.HYST
    line = 1.0 + h
    just_at = liveedge.simulate(_flat_then(line), _flat_then(line), h)
    just_over = liveedge.simulate(_flat_then(line * (1 + 1e-9)),
                                  _flat_then(line * (1 + 1e-9)), h)
    assert just_at['trades'] == 0, (
        '收盘恰好等于进场线时也进场了，说明用的是 >= 而不是 >')
    assert just_over['trades'] == 1, (
        '收盘只高出进场线一点点却没进场，进场判定过严：trades=%d'
        % just_over['trades'])


def test_strictly_at_the_exit_line_still_exits():
    """收盘【恰好等于】出场线时必须出场——线上是 <= 不是 <。

    和进场那条是一对：进场严格大于（等于不进），出场小于等于（等于要出）。
    两条都钉死，才不会被一次性改反。
    """
    h = liveedge.HYST
    n = liveedge.NL
    above = 100.0 * (1 + h) * 1.02      # 先让它进场（窗口全 100 -> 进场线 100.2）
    closes = [100.0] * 201 + [above]
    # 关键：出场线取决于那时的 SMA，而 SMA 已经把进场那根高价算进去了。
    # 所以不能想当然地用 100*(1-h)，要先把窗口定下来、算出真实的出场线，
    # 再把这一根的价格【精确】放到线上。
    # （第一版我就是用 100*(1-h)，结果它压根没落在线上，缺陷就漏过去了。）
    s = sum(closes[2:202]) / n
    closes = closes + [s * (1 - h)]
    assert closes[202] == s * (1 - h), '构造失败：价格没有精确落在出场线上'
    r = liveedge.simulate(closes, closes, h)
    acts = [(i, a) for i, a, _ in r['signals']]
    assert acts == [(201, 'enter'), (202, 'exit')], (
        '收盘恰好等于出场线时应出场（<=），实际 %r；'
        '不出场说明写成了 <' % (acts,))


def test_uses_previous_bar_not_two_bars_back():
    """穿越判定必须比【上一根】，不能比上上根。

    构造：i-2 在线上、i-1 掉到线下、i 回到线上。
      比上一根  -> 是新的穿越，应该进场
      比上上根  -> i-2 本来就在线上，会被当成早就在上面，不进场
    这样两种写法结果不同，才验得出来。
    """
    h = liveedge.HYST
    up = 100.0 * (1 + h) * 1.01      # 明确在进场线上方  (101.202)
    dn = 100.0 * (1 + h) * 0.98      # 明确在下方        ( 98.196)
    closes = [100.0] * 200 + [up, up, dn, up]
    r = liveedge.simulate(closes, closes, h)
    acts = [(i, a) for i, a, _ in r['signals']]
    # 必须精确落在 203：那时 C[202]=dn（线下）、C[203]=up（线上）。
    # 若比的是上上根，i=203 会去看 C[201]=up（线上），判成"本来就在上面"，
    # 于是整段不进场 —— 只断言"有进场"是抓不住的，必须钉住索引。
    assert acts == [(203, 'enter')], (
        '穿越判定应落在 i=203（i-1 在线下、i 在线上），实际 %r；'
        '落在别处说明比的不是上一根' % (acts,))
