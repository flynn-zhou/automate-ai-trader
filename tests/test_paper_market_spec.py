# -*- coding: utf-8 -*-
"""纸面模式必须按【配置的】市场规格撮合，不能默认走永续。

实测代价：配置是现货，但 paper 执行器让 _is_spot() 返回 False，
于是用永续规格（步长 0.001、最小名义 50U）撮合一个现货策略：
权益 71.77U 时"一手"要 77.80U，系统判定"买不起"；
而现货步长 0.00001 时一手只要 0.78U。
同一笔钱，两种规格给出完全相反的结论。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load
from ai_trader.trading.engine import DecisionEngine


def _eng(exec_market, ex_name):
    cfg = load()
    cfg.trading.exec_market = exec_market
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = cfg
    eng.tcfg = cfg.trading
    eng.ex = type("E", (), {"name": ex_name})()
    return eng


def test_paper_honors_configured_market():
    """paper + 配置现货 -> 按现货规格。"""
    assert _eng("spot", "paper")._is_spot() is True
    assert _eng("perp", "paper")._is_spot() is False


def test_real_executors_are_source_of_truth():
    """真正下单的执行器说了算，配置错了也以执行器为准。"""
    assert _eng("perp", "spot")._is_spot() is True, "现货执行器必须按现货"
    assert _eng("spot", "live")._is_spot() is False, "永续执行器必须按永续"


def test_paper_spot_uses_spot_step():
    """纸面现货必须用 0.00001 步长，而不是永续的 0.001。"""
    eng = _eng("spot", "paper")
    assert eng._qty_step() < 1e-4, "纸面现货步长是 %.6f，应远小于 0.001" % eng._qty_step()


def test_paper_spot_can_afford_small_account():
    """71.77U 在纸面现货下必须能建仓（一手只要 0.78U）。"""
    eng = _eng("spot", "paper")
    note = eng._affordability_note(77749.0, 71.77)
    assert note == "", "纸面现货 71.77U 被误判为买不起：%s" % note
