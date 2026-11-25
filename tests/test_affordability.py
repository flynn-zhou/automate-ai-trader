# -*- coding: utf-8 -*-
"""资金不足必须和「信号没到」分开说。

实况：账户只有 0.53 USDT，而系统日志一直写「空仓等待：现价 X < SMA200 Y」。
这句话没撒谎，但它是误导——用户会以为「在等信号」，
实际上就算金叉来了也买不起。两种状态的处置完全不同：
信号没到 -> 继续等；钱不够 -> 去充值 / 检查钱包。

★ 2026-11-17：只做合约。这份测试原来按「现货 5U 生死线」写，
  现在唯一的门槛是**合约一手**：0.001 BTC × 价格，再加保证金余量。
  所以 20U / 71.77U 这些当年「现货够用」的量级，现在会正确地被判为不够。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load
from ai_trader.trading.engine import DecisionEngine


class _Feed:
    last_price = 77749.0


def _engine(market="perp"):
    cfg = load()
    cfg.trading.exec_market = market
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = cfg
    eng.tcfg = cfg.trading
    eng.feed = _Feed()
    eng.ex = type("E", (), {"name": market})()
    return eng


def _one_lot_usd(price=77749.0):
    """建一手需要的权益（含手续费缓冲），与引擎同一套取数。"""
    eng = _engine()
    return 0.001 * price * (1.0 + eng._fee_buffer())


def test_zero_equity_says_wallet_hint():
    """余额为 0 时必须提示检查钱包——钱充错钱包是最常见的运营错误。"""
    eng = _engine()
    note = eng._affordability_note(77749.0, 0.0)
    assert note, "余额为 0 却没有给出任何说明"
    assert "合约钱包" in note, "合约模式下应提示确认合约钱包：%s" % note
    assert "现货钱包" not in note, "仍在提示一个我们不用的钱包：%s" % note


def test_tiny_equity_is_reported_as_insufficient():
    """0.53U 建不了一手，必须明说资金不足，而不是只说在等信号。"""
    eng = _engine()
    note = eng._affordability_note(77749.0, 0.53)
    assert note, "0.53U 明显不够，却没有提示"
    assert "资金不足" in note, "提示里没说是资金问题：%s" % note
    assert "0.53" in note


def test_funded_account_is_not_blocked():
    """一手够的钱必须放行——只会拦坏状态的闸门才可信。"""
    eng = _engine()
    note = eng._affordability_note(77749.0, _one_lot_usd() * 1.5)
    assert note == "", "钱明显够却被误判为资金不足：%s" % note


@pytest.mark.parametrize("equity,expect_blocked", [
    (0.0, True), (1.0, True), (4.99, True), (20.0, True), (50.0, True),
    (77.0, True), (100.0, False), (1000.0, False)])
def test_affordability_threshold_is_one_contract_lot(equity, expect_blocked):
    """门槛就是**一手合约**：0.001 BTC ≈ 77.75U（价 77749 时）。

    ★ 这条把「当年现货的 5U 门槛」正式作废。反证过：
      如果门槛还是 5U，那么 20U 会被判为能开仓，而实际下一手 77.75U 的单
      会被交易所按最小名义/保证金拒掉 —— 提示说「能」，下单失败。
    """
    eng = _engine()
    note = eng._affordability_note(77749.0, equity)
    assert bool(note) is expect_blocked, (
        "权益 %.1f 的判断不符合预期：%s" % (equity, note or "（无提示）"))


def _boundary(eng, price):
    """能开仓的最小权益。

    两个条件同时成立（与 `_affordability_note` 同源）：
      * 一手的**保证金** ≤ 权益：  step × price ÷ 杠杆
      * 名义 ≥ 最小下单：  权益 × 杠杆 ≥ min_notional
    所以边界 = max(一手保证金, 最小名义 ÷ 杠杆)。
    ★ 不能只算一手价值：价 3 万时一手只有 30U，但交易所最小名义 55U是硬门槛，
      相信“30U 就够”的人会在真盘上碰到一个不报错的拒单。
    """
    lev = eng._plan_leverage()
    return max(eng._qty_step() * price / lev, eng._min_notional() / lev)


def test_the_boundary_is_one_lot_or_min_notional_whichever_bites():
    """边界必须落在 max(一手, 最小名义) 上，两侧都要翻转。

    ★ 这条同时钉住两个错法：
      * 只用一手价值 -> 价 3 万时会把 30U 判成够用，实际撞上 55U 最小名义；
      * 只用写死的 55U -> 价 12 万时（一手 120U）会把 75U 判成够用。
    """
    eng = _engine()
    for price in (30000.0, 77749.0, 120000.0):
        need = _boundary(eng, price)
        assert eng._affordability_note(price, need * 1.02) == "", (
            "价 %.0f：边界 %.2fU，给了 %.2fU 却仍被判买不起"
            % (price, need, need * 1.02))
        assert eng._affordability_note(price, need * 0.95) != "", (
            "价 %.0f：给 %.2fU 还不到边界 %.2fU，却没提示"
            % (price, need * 0.95, need))


def test_there_is_no_spot_market_left():
    """★ 反向锁：无论配置/执行器名字怎么写，口径都不能变回现货。

    `_affordability_note` 以前按 `_is_spot()` 分支给两套文案和两套门槛。
    这条测试证明那个分支真的没了。
    """
    eng = _engine()
    eng.tcfg.exec_market = "spot"
    eng.ex = type("E", (), {"name": "spot"})()
    note = eng._affordability_note(77749.0, 5.0)
    assert "现货钱包" not in note
    assert "合约" in note, note
    # 5U 在任何口径下都不该被当成够用
    assert eng._affordability_note(77749.0, 5.0) != ""
