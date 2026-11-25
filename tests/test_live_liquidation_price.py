# -*- coding: utf-8 -*-
"""BUS-D：`LiveExecutor.position()` 必须暴露真实爆仓价，且不许捏造字段。

为什么单独一个文件：这个函数的返回值是 `position_guard` 整套 P0 保命逻辑
的**唯一事实来源**（"离爆仓还有几个止损宽度"）。它多一个假字段、少一个真字段，
都会让守卫在没有任何报错的情况下给出错误结论。
"""
from __future__ import annotations

import pytest

from ai_trader.exchange.live import LiveExecutor


class _Cfg(object):
    class trading(object):
        symbol = "BTCUSDT"


class _Ex(object):
    """只替掉 HTTP 与配置，`position()` 本身用**产品实现**。"""

    cfg = _Cfg()
    # ★ 直接绑定产品方法：这样测的是 `LiveExecutor.position` 的解析，
    #   不是我在这里重写的一份逻辑（重写就等于"同一事实两个来源"）。
    position = LiveExecutor.position

    def __init__(self, payload):
        self._payload = payload

    async def _req(self, method, path, params=None, signed=True):
        return self._payload


def _run(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# 2026-11-24 只读探针实测到的真实返回形状（22x 逐仓，空仓时）
_FLAT = [{"symbol": "BTCUSDT", "positionAmt": "0.000", "entryPrice": "0.0",
          "liquidationPrice": "0", "unRealizedProfit": "0.00000000",
          "leverage": "22", "marginType": "isolated"}]

_LONG = [{"symbol": "BTCUSDT", "positionAmt": "0.032",
          "entryPrice": "60880.0", "liquidationPrice": "58356.2",
          "unRealizedProfit": "12.34", "leverage": "22",
          "marginType": "isolated"}]


def test_position_exposes_the_real_liquidation_price():
    """★ 核心：真实爆仓价必须能取到 —— 守卫的 P0 全靠它。"""
    p = _run(_Ex.position(_Ex(_LONG)))
    assert p["liq_price"] == pytest.approx(58356.2)
    assert p["qty"] == pytest.approx(0.032)
    assert p["side"] == "long"


def test_short_position_reports_liquidation_above_entry():
    """空单的爆仓价在上方。符号写反会让 P0 整条静默失效。"""
    short = [{"symbol": "BTCUSDT", "positionAmt": "-0.032",
              "entryPrice": "60880.0", "liquidationPrice": "63403.8",
              "unRealizedProfit": "-5.0"}]
    p = _run(_Ex.position(_Ex(short)))
    assert p["side"] == "short"
    assert p["liq_price"] > p["entry"], "空单爆仓价必须在入场价上方"


def test_flat_position_reports_zero_not_a_missing_key():
    """空仓时 `liquidationPrice` 是字符串 "0"，必须落成 float 0.0。

    ★ 键必须**存在**且为 0，不能是缺键：调用方约定用 `> 0` 判有效性，
      缺键会变成 KeyError 或 None，两种都会让"没有爆仓价"与
      "爆仓价是 0" 区分不开。
    """
    p = _run(_Ex.position(_Ex(_FLAT)))
    assert p["qty"] == 0.0
    assert "liq_price" in p
    assert p["liq_price"] == 0.0


def test_position_does_not_invent_fields_the_exchange_never_sends():
    """★★ 返回字段必须都是**实测存在**的，不许凭空捏造。

    这条来自一次真实验证：曾经写过 `p.get("maintMargin", 0)`，
    注释写着"守卫可用于核对维持保证金"，听起来像个数据源；
    而 2026-11-24 只读探针打真实 `/fapi/v2/positionRisk` 后确认
    **该字段不存在** —— 于是那行永远返回 0，是一行装成数据的假字段。

    "装成数据源"比"缺字段"更危险：没有任何报错，读代码的人会以为
    守卫真的在核对维持保证金。
    """
    for payload in (_FLAT, _LONG):
        p = _run(_Ex.position(_Ex(payload)))
        assert "maint_margin" not in p, (
            "立场风险接口没有 maintMargin 字段，不许假装有")
        assert "maintMargin" not in p
        # 真实存在的那些字段必须在
        for k in ("qty", "entry", "side", "liq_price", "unrealized"):
            assert k in p


def test_unrealized_pnl_is_exposed():
    """未实现盈亏也取出来：逐仓下它会让真实爆仓价随浮盈移动。"""
    p = _run(_Ex.position(_Ex(_LONG)))
    assert p["unrealized"] == pytest.approx(12.34)
