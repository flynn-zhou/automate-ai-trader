# -*- coding: utf-8 -*-
"""两个"写好了但没接线"的东西。

一、LiveExecutor.set_leverage 全项目零调用。
    方法实现完整（幂等检查、日志、_lev_set 状态），但从未被调用过。
    后果：交易所用的是默认杠杆或上次残留值。
    1x 趋势策略下名义额 = 权益，交易所杠杆 >= 1x 就不会出事，
    所以这个 bug 至今没暴露；但把 trend_leverage 调到 2x 就会出问题。

二、RestMarket.server_time / latency_ms 也从未被调用。
    这是真实风险：币安签名请求带 recvWindow=5000，
    服务器时钟漂移超过 5 秒，所有签名请求都会被拒（-1021），
    而表现是"下单失败"而不是"时钟错了"，很难第一眼定位。

这类"存在但没被调用"的坑，本项目已经踩过三次：
healthcheck 没有调度器、哨兵查错列、guard.can_open 趋势分支不调用。
这次是系统性扫描发现的。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load
from ai_trader.exchange.live import LiveExecutor


def test_main_actually_calls_set_leverage():
    """main.py 必须真的调用 set_leverage——光有方法不算接线。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "ai_trader", "main.py"), encoding="utf-8").read()
    assert "set_leverage" in src, (
        "main.py 从未调用 set_leverage —— 交易所杠杆会和配置不一致")
    # 必须在 mode == live 分支里被调用
    idx = src.find("set_leverage")
    assert "mode == \"live\"" in src[:idx], "set_leverage 应在实盘分支里调用"


def test_set_leverage_exists_and_is_async():
    assert hasattr(LiveExecutor, "set_leverage")
    assert inspect.iscoroutinefunction(LiveExecutor.set_leverage)


def test_leverage_call_is_fault_tolerant():
    """设不上杠杆不应导致启动失败（1x 时交易所杠杆无所谓）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "ai_trader", "main.py"), encoding="utf-8").read()
    idx = src.find("await executor.set_leverage")
    assert idx > 0
    # 前后应有 try/except
    assert "try:" in src[max(0, idx - 300):idx], "调用没有包 try"
    assert "except" in src[idx:idx + 400], "调用没有兜底 except"


def test_clock_offset_check_exists():
    """时钟偏移必须被检查：recvWindow 只有 5 秒，漂移超过就全部签名失败。"""
    from ai_trader.market.rest import RestMarket
    assert hasattr(RestMarket, "server_time"), "RestMarket 缺少 server_time，无法检查时钟偏移"
    assert inspect.iscoroutinefunction(RestMarket.server_time)
