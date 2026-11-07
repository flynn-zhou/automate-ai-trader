# -*- coding: utf-8 -*-
"""资金不足必须和"信号没到"分开说。

实况：账户只有 0.53 USDT，而系统日志一直写「空仓等待：现价 X < SMA200 Y」。
这句话没撒谎，但它是误导——用户会以为"在等信号"，
实际上就算金叉来了也买不起。两种状态的处置完全不同：
信号没到 -> 继续等；钱不够 -> 去充值 / 检查钱包。
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


def test_zero_equity_says_wallet_hint():
    """余额为 0 时必须提示检查钱包——钱充错钱包是最常见的运营错误。"""
    eng = _engine("spot")
    note = eng._affordability_note(77749.0, 0.0)
    assert note, "余额为 0 却没有给出任何说明"
    assert "现货钱包" in note, "现货模式下应提示确认现货钱包：%s" % note


def test_tiny_equity_is_reported_as_insufficient():
    """0.53U 建不了一手，必须明说资金不足，而不是只说在等信号。"""
    eng = _engine("spot")
    note = eng._affordability_note(77749.0, 0.53)
    assert note, "0.53U 明显不够，却没有提示"
    assert "资金不足" in note, "提示里没说是资金问题：%s" % note
    assert "0.53" in note


def test_funded_account_is_not_blocked():
    """100U 足够建仓，不能误报资金不足。"""
    eng = _engine("spot")
    note = eng._affordability_note(77749.0, 100.0)
    assert note == "", "100U 被误判为资金不足：%s" % note


@pytest.mark.parametrize("equity,expect_blocked", [
    (0.0, True), (1.0, True), (4.99, True), (5.0, False), (100.0, False), (1000.0, False)])
def test_affordability_threshold_spot(equity, expect_blocked):
    """现货：一手 0.00001 BTC ≈ 0.78U，最小名义 5U。
    所以门槛约等于 5U（受最小名义约束），低于它必须拦。"""
    eng = _engine("spot")
    note = eng._affordability_note(77749.0, equity)
    assert bool(note) is expect_blocked, (
        "权益 %.1f 的判断不符合预期：%s" % (equity, note or "（无提示）"))


def test_perp_needs_more_than_spot():
    """永续最小名义 50U，比现货 5U 高得多——同样 20U 在现货能开、永续不能。"""
    spot = _engine("spot")._affordability_note(77749.0, 20.0)
    perp = _engine("perp")._affordability_note(77749.0, 20.0)
    assert spot == "", "20U 在现货应该够：%s" % spot
    assert perp, "20U 在永续不够（最小名义 50U），却没提示"
