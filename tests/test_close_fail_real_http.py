# -*- coding: utf-8 -*-
"""端到端：真 HTTP 下让 equity() 失败，止损通道闸必须真的锁上（第 124 轮）。

单元测试里我用的是一个直接 raise 的假执行器。
那个假执行器证明的是"_close 的 try 有效"，
但证明不了【真实的 SpotExecutor 在真实 HTTP 故障下会不会走到这条路径】。

这两件事不一样：
  - 假执行器：raise RuntimeError  —— 什么异常都会被我兜住
  - 真执行器：equity() 内部走 _req，网络失败 -> SpotError

所以我用 FakeBinance 起一个真 HTTP 服务，然后把它的地址指到一个
【已关闭的端口】上，让 SpotExecutor 的每一次请求都真的失败，
验证整条链路：market_order -> equity() 抛错 -> 冒泡 -> _close 兜住 -> 计数。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor
from ai_trader.guard import Guard
from ai_trader.trading.engine import DecisionEngine
from tools.fake_binance import FakeBinance


class _Feed:
    src = "WS"

    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []


class _DB:
    def __init__(self):
        self.risks = []

    def log_risk(self, kind, detail):
        self.risks.append((kind, detail))

    def log_order(self, d):
        pass


@pytest.mark.asyncio
async def test_a_real_http_failure_counts_toward_the_close_fail_gate():
    """真 HTTP 全挂时，平仓必须照样计入连续失败，闸必须锁上。

    做法：起 FakeBinance 拿到它的端口，关掉，然后让执行器去连那个端口。
    这样 spec() / equity() / 下单全都会真的失败，走的正是生产里的路径。
    """
    f = await FakeBinance(usdt=100.0, btc=0.002, price=78000.0).start()
    base_url = f.base()
    key, secret = f.api_key, f.api_secret
    await f.stop()                      # 现在就关掉：端口上没人监听了

    cfg = AppCfg()
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.taker_fee = 0.001
    cfg.market.spot_base = base_url
    cfg.market.http_proxy = ""

    ex = SpotExecutor(_Feed(78000.0), cfg, key, secret, base=base_url)
    db = _DB()
    g = Guard(cfg)
    g.stall_grace_ms = 10 ** 13
    e = DecisionEngine(cfg, _Feed(78000.0), ex, db, guard=g)
    e.ex = ex
    e.guard = g
    e.pos = {"qty": 0.002, "entry": 80000.0, "side": "long", "best": 80000.0,
             "worst": 80000.0, "entry_ts": 0, "bars": 0}

    for _ in range(6):
        e._close_retry_ts = 0
        try:
            await e._close("止损", 70000.0, 70000.0, {})
        except Exception as exc:
            pytest.fail(u"_close 不该向外抛异常，实际抛了：%r" % (exc,))

    assert e._close_fail_count >= 5, (
        u"真 HTTP 全挂下平仓试了 6 次，失败计数只有 %d" % e._close_fail_count)
    ok, why = g.can_open(10 ** 13)
    assert ok is False, u"止损通道已经打不出去，却仍然放行开仓"
    assert u"止损通道失效" in why, u"拦住的原因不对：%r" % (why,)
    kinds = [k for k, _ in db.risks]
    assert "close_failed" in kinds, u"没有记 close_failed，只记了：%r" % (kinds,)
    # 说明这条路径确实是"网络失败"造成的，不是我构造的别的东西
    blob = " ".join(d for _, d in db.risks)
    assert (u"网络失败" in blob or u"HTTP" in blob or u"超时" in blob
            or u"timeout" in blob.lower() or u"Errno" in blob
            or u"拒绝" in blob or u"refused" in blob.lower()), (
        u"失败原因不像网络故障，测试前提可疑：%s" % blob[:400])
