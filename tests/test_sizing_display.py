# -*- coding: utf-8 -*-
"""仓位参数展示的回归防线。

要求来自用户原话：「这些 1.615% 都放在 web 端的总览最下面的'仓位参数'里面，
仓位配置导航页加上，毕竟都是公式算出来的」。

三条不变量：
  1. 两个页面共用同一份模板片段，不许各写一份（否则迟早分叉）；
  2. 派生量必须由服务端 sizing.derive() 算，前端不许重算；
  3. 止损距离上限必须出现在页面上 —— 它是闸门的核心约束，
     藏起来等于让人不知道单子为什么被拦。
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.trading.sizing import SizingConfig, derive  # noqa: E402


def _read(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_derive_numbers_match_documented_formula():
    """四行派生必须算出文档里的那组数字。

    100U / 5% / 22x -> R=5U、爆仓距离 4.045%、止损上限 1.618%、保证金上限 40U。
    这组数是用户口径的"公式"，任何一项漂移都说明公式被改了。
    """
    d = derive(SizingConfig(capital=100.0, risk_pct=5.0, leverage=22.0))
    assert d.risk_per_trade == 5
    assert abs(d.liq_distance_pct - 4.045) < 0.001
    assert abs(d.max_stop_distance_pct - 1.618) < 0.001
    assert d.margin_cap_amount == 40.0
    assert d.lives == 20


def test_both_pages_share_one_template():
    """总览和仓位配置页必须 include 同一份片段，不许各写一份表。"""
    frag = _read("ai_trader", "web", "templates", "_sizing_table.html")
    for token in ("风险", "杠杆", "爆仓距离", "止损距离上限", "保证金上限", "可连亏"):
        assert token in frag, "共享片段缺一项: " + token
    for page in ("dash_home.html", "dash_config.html"):
        src = _read("ai_trader", "web", "templates", page)
        assert '{% include "_sizing_table.html" %}' in src, page + " 没 include 共享片段"


def test_derived_values_computed_server_side():
    """上下文必须提供 sz，且来自 sizing.derive()。"""
    src = _read("ai_trader", "web", "app.py")
    assert '"sz": _sizing_view(s)' in src, "上下文没提供 sz"
    i = src.index("def _sizing_view")
    body = src[i:i + 1400]
    assert "from ..trading.sizing import SizingConfig, derive" in body, \
        "没复用 sizing.derive，说明另算了一套"
    assert "max_stop_distance_pct" in body


def test_pages_do_not_recompute_sizing_in_js():
    """前端不许重算派生量。

    重算一遍就多一套真相：页面显示 1.618%、闸门用 1.5% 的时候，
    用户会以为是闸门坏了。
    """
    for page in ("dash_home.html", "dash_config.html", "_sizing_table.html"):
        src = _read("ai_trader", "web", "templates", page)
        assert "1.618" not in src, page + " 把派生值写死成了常量"
        assert "liq_distance" not in src.replace("sz.liq_distance_pct", ""), \
            page + " 在前端自己算爆仓距离"


def test_stop_cap_is_visible_with_explanation():
    """止损距离上限必须带说明：超过它，爆仓会先于止损触发。"""
    frag = _read("ai_trader", "web", "templates", "_sizing_table.html")
    assert "爆仓会先于止损" in frag, "没解释这个上限为什么存在"
    assert "上限" in frag, "没说明它是一个上限而不是建议值"
