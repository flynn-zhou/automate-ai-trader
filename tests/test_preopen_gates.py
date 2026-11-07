# -*- coding: utf-8 -*-
"""开仓闸门的回归防线。**当前只有一道：仓位参数。**

为什么要重写这组测试
------------------
上一版测的是 `build_plan()` 里的六道 gate，全绿。但那六道门
**根本不在 donchian 的生产路径上** —— `_trend_plan()` 返回 gates=[]，
一次都没走。测试全绿而生产裸奔，这是测试本身在撒谎。

所以现在只测一条链路：`position_gate()` -> `_trend_plan()` -> 下单量。
其余五道门（强平距离/成本门/波动门/可达性/止损结构）按决定已弃用，
这里也不留它们的测试，免得哪天有人看到绿色就以为它们还在。
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.trading.gates import position_gate  # noqa: E402


def _read(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _gate(**kw):
    base = dict(equity=100.0, price=75894.16, leverage=22.0, qty_step=0.001,
                min_qty=0.001, min_notional=50.0, max_leverage=60.0,
                capital=100.0, risk_pct=5.0, margin_cap_pct=100.0)
    base.update(kw)
    return position_gate(**base)


def test_normally_funded_account_passes():
    """100U 账户应该在 BTC 7.5 万价位上能开仓。"""
    g = _gate()
    assert g.ok, "100U 账户被误拒: " + g["detail"]
    assert g["qty"] >= 0.001, "数量低于最小一手"
    assert g["notional"] >= 50.0, "名义低于交易所最小值"


def test_empty_account_is_rejected_with_reason():
    """账户只有 0.007U 必须被拒，且理由要能直接看懂。

    这是生产里的真实数字（2026-11-07 实测）。
    要求理由里同时出现权益和取整结果 —— 只写"不能开仓"
    会让运维以为是信号问题，去等一个永远不会来的成交。
    """
    g = _gate(equity=0.007127)
    assert not g.ok
    assert "0.0071" in g["detail"] or "0.01" in g["detail"]
    assert "取整" in g["detail"]


def test_min_notional_rejected():
    """名义不够交易所最小额时必须拒。"""
    g = _gate(min_notional=999999.0)
    assert not g.ok
    # 文案里必须保留"最小下单"四个字：看板和哨兵按文案分流故障原因。
    assert "最小下单" in g["detail"], g["detail"]


def test_leverage_cap_enforced():
    """配置杠杆超过硬上限必须拒。"""
    g = _gate(leverage=100.0, max_leverage=60.0)
    assert not g.ok and "超过硬上限" in g["detail"]


def test_margin_cap_truncates_position():
    """保证金上限必须真的截断仓位，不能只写在页面上。"""
    full = _gate(margin_cap_pct=100.0)
    capped = _gate(margin_cap_pct=10.0)
    assert full.ok and capped.ok
    assert capped["notional"] < full["notional"] * 0.5, \
        "保证金上限没有截断仓位"


def test_rounding_is_floor_never_up():
    """数量必须向下取整。向上取整会让实际杠杆超过配置杠杆。"""
    g = _gate()
    assert g.ok
    assert g["notional"] / 100.0 <= 60.0 + 1e-9
    # 步长必须整除
    steps = round(g["qty"] / 0.001)
    assert abs(steps * 0.001 - g["qty"]) < 1e-9, "数量没对齐步长"


def test_missing_data_rejected_not_defaulted():
    """缺数据一律判否。

    "数据没到"和"条件满足"在布尔上长得一样，这是静默失效最常见的来源。
    """
    for kw in (dict(price=0), dict(equity=0), dict(qty_step=0),
               dict(min_notional=0), dict(leverage=0)):
        g = _gate(**kw)
        assert not g.ok, "缺数据 %r 时居然放行了" % (kw,)


def test_trend_plan_uses_the_gate():
    """`_trend_plan` 必须真的调用闸门，而不是自己再算一遍。

    老实现就是自己算 + 自己内联判断，闸门和生产两条路。
    """
    src = _read("ai_trader", "trading", "engine.py")
    m = re.search(r"def _trend_plan\(.*?\n(?=    def )", src, re.S)
    assert m, "找不到 _trend_plan"
    body = m.group(0)
    assert "position_gate(" in body, "_trend_plan 没调用闸门"
    assert "_sizing_params()" in body, "_trend_plan 没读 4 个仓位参数"
    assert "gates=[dict(g)]" in body, "闸门结论没进决策日志"


def test_engine_requires_settings():
    """引擎不传 settings 时必须拒绝开仓，不能按默认值下单。"""
    src = _read("ai_trader", "trading", "engine.py")
    assert "self.settings = settings" in src
    assert "仓位参数不可用" in src
    main = _read("ai_trader", "main.py")
    assert "settings=settings" in main, "main 没把 settings 传给引擎"
