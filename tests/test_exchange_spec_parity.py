# -*- coding: utf-8 -*-
"""假交易所的规格副本必须和文档/代码里的假定一致——否则测试全绿而线上用错步长。

为什么（第 112 轮）：
  tools/fake_binance.py 里 SPOT_SYMBOL 的 stepSize / minNotional / baseAssetPrecision
  是我【手抄】的：0.00001 / 5.0 / 8。所有现货测试都跑在这个副本上。
  如果真实币安改了规格，或者我抄错了，测试依然全绿，
  而线上会用错的步长下单——要么被拒，要么仓位算成 0。

  测试能证明「代码符合我以为的世界」，不能证明「我以为的世界是对的」。
  所以这轮向真交易所要了一次（tools/spec_live_check.py，实测 6 项全一致），
  而这里钉住的是【三方一致】：假交易所副本 == 引擎默认值 == 文档结论。

  spec_live_check.py 是只读、无需密钥的在线核对；它不能进测试套件
  （测试不该依赖网络），所以用这条静态测试守住副本本身，
  需要核对真实值时就跑那个工具。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import load
from tools.fake_binance import SPOT_SYMBOL, PERP_SYMBOL

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spot_filter(name):
    return {f["filterType"]: f for f in SPOT_SYMBOL["filters"]}[name]


def test_fake_spot_spec_matches_the_documented_real_spec():
    """★ 假币安的现货规格必须等于实测的真实规格（2026-11-02 核对）。"""
    lot = _spot_filter("LOT_SIZE")
    assert float(lot["stepSize"]) == pytest.approx(1e-05), (
        u"假交易所 stepSize=%r，实测真实值是 0.00001" % (lot["stepSize"],))
    assert float(lot["minQty"]) == pytest.approx(1e-05)
    assert float(_spot_filter("NOTIONAL")["minNotional"]) == pytest.approx(5.0), (
        u"假交易所 minNotional=%r，实测真实值是 5.0"
        % (_spot_filter("NOTIONAL")["minNotional"],))
    assert int(SPOT_SYMBOL["baseAssetPrecision"]) == 8, (
        u"假交易所 baseAssetPrecision=%r，实测真实值是 8（不是 6）"
        % (SPOT_SYMBOL["baseAssetPrecision"],))


def test_engine_defaults_agree_with_the_fake_exchange():
    """引擎侧的交易所作假定必须和假交易所一致。

    ★ 2026-11-17 只做合约：对照对象从现货规格换成 `PERP_SYMBOL`。
    两边不一致时，测试里的仓位计算和线上用的是两套规格，
    而这种偏差只会在真钱上暴露。

    ★ 注意：引擎的最小名义用的是“一手的市值”而不是交易所的
      MIN_NOTIONAL=50U（两者差 55 vs 50）。这个差值是**有意**的：50U 是交易所的硬门槛，
      55U 是我们自己对一手 0.001 BTC 在 5.5 万下的估值下限。这里钉的是“不会被
      改成 5U 那种现货规格”，不钉它等于 50。
    """
    cfg = load()
    cfg.trading.exec_market = "perp"
    from ai_trader.trading.engine import DecisionEngine
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = cfg
    eng.tcfg = cfg.trading
    eng.ex = type("E", (), {"name": "live"})()
    lot = {f["filterType"]: f for f in PERP_SYMBOL["filters"]}["LOT_SIZE"]
    assert eng._qty_step() == pytest.approx(float(lot["stepSize"])), (
        u"引擎步长 %r 与假交易所 %r 不一致"
        % (eng._qty_step(), lot["stepSize"]))
    mn = {f["filterType"]: f for f in PERP_SYMBOL["filters"]}["MIN_NOTIONAL"]
    assert eng._min_notional() >= float(mn["notional"]), (
        u"引擎最小名义 %r 低于交易所硬门槛 %r：会发出必被拒的单"
        % (eng._min_notional(), mn["notional"]))
    assert eng._min_notional() < 100.0, (
        u"最小名义 %r 太高；它是一手的市值下限，不应该 100U"
        % eng._min_notional())


def test_fake_perp_spec_is_now_the_only_spec():
    """★ 永续的最小名义 50U + 步长 0.001 以前是“被否掉的理由”，
    现在是**唯一的规格** —— 所以这里钉的是真实交易所数字。
    """
    lot = {f["filterType"]: f for f in PERP_SYMBOL["filters"]}["LOT_SIZE"]
    assert float(lot["stepSize"]) == pytest.approx(0.001)
    mn = {f["filterType"]: f for f in PERP_SYMBOL["filters"]}["MIN_NOTIONAL"]
    assert float(mn["notional"]) == pytest.approx(50.0), (
        u"永续最小名义 %r，实测真实值是 50（不是 55）" % (mn["notional"],))
    # 8 万价下一手 0.001 BTC = 80U，100U 账户一开仓就吃掉 80%
    assert 0.001 * 80000.0 > 50.0
