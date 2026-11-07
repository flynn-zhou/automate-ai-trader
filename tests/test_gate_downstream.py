# -*- coding: utf-8 -*-
"""闸门作为「最后一道」的校验。

用户原话：
  「这些都接上，闸门参数定义好，有有效入参才校验，干嘛不接……
    接到闸门，以免后面忘了！」

以及：
  「22的作用在于不要超过22，闸门是最后一道吧？你都算好了交给最后一道，
    发现不符合拦下来阻止开仓，所以倍数等几个值有的是开仓信号算出来一个……」

所以闸门的职责是【否决上游算好的计划】，不是自己定价。
每条校验都遵守：有有效入参才校验。上游没给（0）就跳过，
绝不拿编出来的默认值去拦 —— 那会在策略还没接上时把一切都拦死。
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.trading.gates import position_gate  # noqa: E402
from ai_trader.trading.sizing import SizingConfig, derive  # noqa: E402

BASE = dict(equity=100.0, price=75894.16, leverage=22.0, qty_step=0.001,
            min_qty=0.001, min_notional=50.0, max_leverage=22.0,
            capital=100.0, risk_pct=5.0, margin_cap_pct=40.0)


def _g(**kw):
    a = dict(BASE)
    a.update(kw)
    return position_gate(**a)


def test_stop_cap_blocks_when_over():
    """止损距离超过上限必须拒——爆仓会先于止损触发。"""
    cap = derive(SizingConfig(capital=100.0, risk_pct=5.0, leverage=22.0))
    assert abs(cap.max_stop_distance_pct - 1.618) < 0.001
    g = _g(stop_distance_pct=1.8, max_stop_distance_pct=cap.max_stop_distance_pct)
    assert not g.ok and "爆仓会先于止损" in g["detail"]


def test_stop_cap_passes_when_under():
    g = _g(stop_distance_pct=1.2, max_stop_distance_pct=1.618)
    assert g.ok, g["detail"]


def test_stop_cap_skipped_when_no_input():
    """策略还没定、止损距离给不出来时必须跳过，不能拿默认值拦死。

    这条是整个「有有效入参才校验」原则的守门测试。
    """
    g = _g(stop_distance_pct=0.0, max_stop_distance_pct=1.618)
    assert g.ok, "没给止损距离却被拦了：" + g["detail"]


def test_margin_cap_enforced():
    """保证金超过本金 x margin_cap_pct% 必须拒。"""
    g = _g(margin_cap_pct=10.0)
    # 10% 时闸门会先截断名义，截断后应在限内
    assert g.ok
    assert g["margin"] <= 100.0 * 0.10 + 1e-9, "保证金上限没有生效"
    # 1% 太小，连一手都开不出 —— 被拒是正确行为，理由要说清是"取整后为 0"
    g2 = _g(margin_cap_pct=1.0)
    assert not g2.ok
    assert "取整后为 0" in g2["detail"], g2["detail"]


def test_plan_qty_mismatch_rejected():
    """上游计划量与闸门复核不一致必须拒，并同时打出两个数。"""
    g = _g(plan_qty=0.05)
    assert not g.ok
    assert "0.05" in g["detail"] and "不一致" in g["detail"]


def test_plan_qty_match_accepted():
    base = _g()
    assert base.ok
    same = _g(plan_qty=base["qty"])
    assert same.ok, "上游量恰好等于复核值却被拒：" + same["detail"]


def test_leverage_cap_blocks_over_22():
    """实际杠杆超过 22x 必须拒。"""
    # 权益压到很低，让 名义/权益 超过 22
    g = _g(equity=30.0)
    if g.ok:
        assert g["leverage"] <= 22.0 + 1e-9
    else:
        assert "超过硬上限" in g["detail"] or "最小" in g["detail"]


def test_engine_passes_downstream_params():
    """引擎必须把上游参数位置留好并传给闸门，即使现在传 0。"""
    src = io.open(os.path.join(ROOT, "ai_trader", "trading", "engine.py"),
                  encoding="utf-8").read()
    assert "stop_distance_pct=0.0" in src, "没给止损距离留位置"
    assert "max_stop_distance_pct=self._max_stop_distance_pct(params)" in src
    assert "_max_stop_distance_pct" in src
    i = src.index("def _max_stop_distance_pct")
    body = src[i:i + 900]
    assert "derive" in body, "上限没复用 sizing.derive，说明另算了一套"


def test_stop_cap_comes_from_same_formula_as_page():
    """闸门的止损上限和页面显示必须同源（sizing.derive）。"""
    d = derive(SizingConfig(capital=100.0, risk_pct=5.0, leverage=22.0))
    assert abs(d.max_stop_distance_pct - 1.618) < 0.001
    frag = io.open(os.path.join(ROOT, "ai_trader", "web", "templates",
                                "_sizing_table.html"), encoding="utf-8").read()
    assert "sz.max_stop_distance_pct" in frag, "页面没用同一个派生值"
