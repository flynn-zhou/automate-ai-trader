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
    """引擎侧的现货假定（_qty_step=1e-5、_min_notional=5.0）必须和假交易所一致。

    两边不一致时，测试里的仓位计算和线上用的是两套规格，
    而这种偏差只会在真钱上暴露。
    """
    cfg = load()
    cfg.trading.exec_market = "spot"
    from ai_trader.trading.engine import DecisionEngine
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = cfg
    eng.tcfg = cfg.trading
    eng.ex = type("E", (), {"name": "paper"})()
    assert eng._qty_step() == pytest.approx(float(_spot_filter("LOT_SIZE")["stepSize"])), (
        u"引擎步长 %r 与假交易所 %r 不一致"
        % (eng._qty_step(), _spot_filter("LOT_SIZE")["stepSize"]))
    assert eng._min_notional() == pytest.approx(
        float(_spot_filter("NOTIONAL")["minNotional"])), (
        u"引擎最小名义 %r 与假交易所 %r 不一致"
        % (eng._min_notional(), _spot_filter("NOTIONAL")["minNotional"]))


def test_documented_floor_is_reachable_at_a_normal_price():
    """100U 账户在正常 BTC 价格下必须能建仓——这是选现货的第一理由。

    DECISION_MARKET 里写的生死线是 5.5U（一个 step 0.781U × 7 个 step × 1.003）。
    这里按 8 万美元算一遍，钉住「100U 远远够用」这个结论。
    """
    step = float(_spot_filter("LOT_SIZE")["stepSize"])
    min_notional = float(_spot_filter("NOTIONAL")["minNotional"])
    import math
    for px in (30000.0, 80000.0, 150000.0):
        one_step_u = step * px
        if one_step_u <= 0:
            continue
        steps = math.ceil(min_notional / one_step_u)
        floor = steps * one_step_u * 1.003          # 含 0.3% 手续费缓冲
        assert floor < 100.0, (
            u"价 %.0f 时最小可交易权益 %.2fU >= 100U，100U 账户建不了仓"
            % (px, floor))
    # 8 万时应当约 5.5U，和 DECISION_MARKET / test_account_floor 的口径一致
    px = 80000.0
    one_step_u = step * px
    steps = math.ceil(min_notional / one_step_u)
    assert steps * one_step_u * 1.003 == pytest.approx(5.62, abs=0.2), (
        u"8 万价下的最小可交易权益变了：%.2fU" % (steps * one_step_u * 1.003,))


def test_fake_perp_spec_is_the_headwind_we_documented():
    """永续的最小名义 50U + 步长 0.001 是他被否掉的原因，也钉住。"""
    lot = {f["filterType"]: f for f in PERP_SYMBOL["filters"]}["LOT_SIZE"]
    assert float(lot["stepSize"]) == pytest.approx(0.001)
    mn = {f["filterType"]: f for f in PERP_SYMBOL["filters"]}["MIN_NOTIONAL"]
    assert float(mn["notional"]) == pytest.approx(50.0), (
        u"永续最小名义 %r，实测真实值是 50（不是 55）" % (mn["notional"],))
    # 8 万价下一手 0.001 BTC = 80U，100U 账户一开仓就吃掉 80%
    assert 0.001 * 80000.0 > 50.0
