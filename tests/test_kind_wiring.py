# -*- coding: utf-8 -*-
"""KIND-1 接线：`PlanSource.plans()` 必须真的只用 `ENABLED_KINDS`。

★ 为什么单独钉这一条：
  常量写对了不代表生产用上了 —— `plans()` 里少传一个 `kinds=`，
  就会静默回退到 `detect_all` 的默认值 `KINDS`（三条全开），
  于是已停用的三引线又被放回生产。
  本项目反复吃亏的正是这一类「同一件事两个来源」，
  所以测试必须**从生产入口**验，而不是只验常量。
"""
from __future__ import annotations

from typing import Any, List

from ai_trader.trading import pending_plan as P
from ai_trader.trading.plan_source import PlanSource


class _FakeSrc(PlanSource):
    """只替换 `context()`，其它走真实实现。"""

    def __init__(self):
        super().__init__(book_provider=None, kbar_db="x", range_db="y",
                         symbol="BTCUSDT")

    def context(self) -> Any:      # pragma: no cover - 由用例覆盖
        return self._ctx


def _detect_calls(monkeypatch) -> List[dict]:
    calls: List[dict] = []
    real = P.detect_all

    def spy(ctx, params=None, **kw):
        calls.append(dict(kw))
        return real(ctx, params, **kw)

    monkeypatch.setattr(P, "detect_all", spy)
    return calls


def test_plans_passes_enabled_kinds(monkeypatch):
    """生产入口必须显式传 `kinds=ENABLED_KINDS`。"""
    src = _FakeSrc()
    src._ctx = P.PlanContext(bars15=[], book=None, bars_1h=[], bars_5m=[],
                             windows=[])
    calls = _detect_calls(monkeypatch)
    src.plans(None)
    assert calls, "plans() 没有调用 detect_all"
    kw = calls[0]
    assert "kinds" in kw, "plans() 没传 kinds → 会静默回退到三条全开"
    assert tuple(kw["kinds"]) == tuple(P.ENABLED_KINDS)
    assert P.KIND_WICKS not in kw["kinds"], "三引线不许出现在生产启用集合里"
    assert kw["require_window"] is True, "生产必须要求开仓窗口"
    assert kw["include_mirror"] is True, "上涨镜像必须打开"


def test_enabled_kinds_is_a_subset_of_kinds():
    """启用集合必须是全集的子集（停用 ≠ 删除）。"""
    assert set(P.ENABLED_KINDS) <= set(P.KINDS)
    assert len(P.ENABLED_KINDS) == 2
