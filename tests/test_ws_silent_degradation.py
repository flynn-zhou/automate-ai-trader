# -*- coding: utf-8 -*-
"""WS 连上但一条数据都不推 —— 必须看得出来，且要能用能推的那条流。

起因（2026-11-02）：
  快照一直报 ws_connected: true / ws_idle_ms 等于进程运行时长。
  从服务器上用原生 aiohttp 直连同一 URL 验证过，不是我方代码问题：

      wss://fstream.binance.com/ws/btcusdt@aggTrade   45s  -> 0 条
      wss://fstream.binance.com/ws/btcusdt@trade      45s  -> 195 条

  也就是说：设计里的「WS 快车道」从上线到现在一 tick 都没走过，
  系统一直靠 REST 兜底（约 6.5 tick/s），而看板上最显眼的那个指标说"已连接"。

这类"指标在撒谎"比没有指标更危险：它让人相信一条从来没跑过的路径是健康的。
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.market.ws import AggTradeWS


def _mk(**kw):
    async def noop(d): pass
    return AggTradeWS("wss://fstream.binance.com/ws", "BTCUSDT", noop, **kw)


def test_stream_name_is_configurable():
    """流名必须可配，否则永远只能订阅 aggTrade——而它可能不推。"""
    a = _mk()
    assert a.url.endswith("/btcusdt@aggTrade"), a.url
    b = _mk(stream="trade")
    assert b.url.endswith("/btcusdt@trade"), b.url


def test_accepts_both_aggTrade_and_trade():
    """两条流的载荷字段是兼容的（p/q/T/m 都在），都要能收。

    以前只认 e == "aggTrade"，换成 @trade 会一条都收不进来，
    而且是静默丢弃——和这次的现象一模一样。
    """
    for ev in ("aggTrade", "trade"):
        assert _mk()._is_trade_event({"e": ev}), ev
    assert not _mk()._is_trade_event({"e": "kline"})
    assert not _mk()._is_trade_event({})


def test_silence_is_detectable():
    """连上了但很久没消息，必须能被判定出来。

    只看 connected 会把"连着但不推"当成健康——这就是本次的坑。
    """
    a = _mk()
    a.connected = True
    a.last_msg_ts = 0                 # 从未收到过消息
    assert a.silent_seconds() >= 999, "从未收到消息时应返回极大值"

    a.last_msg_ts = 10_000            # 很久以前
    assert a.silent_seconds() > 60
    assert a.is_silent(60.0) is True

    # ★ 关键区分点：连着且刚收到消息，不算静默。
    # 少了这一条，"只看 connected" 的实现也能通过——
    # 变异验证时正是它漏了网。必须有一个"健康"的样本。
    import time as _t
    a.connected = True
    a.last_msg_ts = int(_t.time() * 1000)
    assert a.silent_seconds() < 5
    assert a.is_silent(60.0) is False, u"刚收到消息却判成静默"

    # 不在连接状态时不算"静默"，那是另一种故障（断线）
    a.connected = False
    a.last_msg_ts = 10_000
    assert a.is_silent(60.0) is False
