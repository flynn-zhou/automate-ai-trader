# -*- coding: utf-8 -*-
"""平仓「抛异常」这条路必须和「返回失败」一样计入止损通道闸（第 124 轮）。

缺陷的形状
----------
第 116 轮给「止损失效」装了闸：平仓连续失败 5 次 -> 停止开新仓。
计数加在 `_close` 里 `if not fill.ok:` 那个分支。

但 `market_order()` 里有两条失败路径：

    1. 返回 FillResult(ok=False)      <- 计数会加（已覆盖）
    2. 抛异常                          <- 计数【不加】

第 2 条怎么发生的：`market_order` 的 try 只包住了下单那【一句】：

    spec = await self.spec()      # 373 行，try 外面
    acct = await self.equity()    # 400 行，try 外面
    try:
        r = await self._req("POST", ...)
    except SpotError as e:
        return FillResult(ok=False, ...)

而 `_req` 把所有异常统一转成 SpotError（网络失败、HTTP 非 200 都走这里）。
所以 `spec()` / `equity()` 抛 SpotError 时会【直接冒泡出 market_order】：

    market_order -> _close（不捕获）-> _step_trend（不捕获）
                 -> _on_bar 捕获 -> log_risk("engine_error")

于是 `_close_fail_count` 一次都不加，闸永远到不了 5。

为什么这不是理论问题：`equity()` 是每次平仓【必经】的一次 HTTP 请求
（market_order 里要按账上持币量截断卖出量）。而交易所 API 最容易超时的
时刻正是行情剧烈波动的时候——也就是最需要止损的时候。

所以真实事故形态是：行情暴动 -> equity() 超时 -> 平仓抛异常 ->
计数器不动 -> 闸不触发 -> 系统一边报 engine_error 一边继续开新仓。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.trading.engine import DecisionEngine


class _Ex:
    name = "spot"

    def __init__(self, boom):
        self.boom = boom
        self.calls = 0

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.calls += 1
        raise RuntimeError(self.boom)


class _DB:
    def __init__(self):
        self.risks = []

    def log_risk(self, kind, detail):
        self.risks.append((kind, detail))

    def log_order(self, d):
        pass


class _Feed:
    src = "WS"
    last_price = 78000.0


def _engine(boom="网络失败 /api/v3/account: timeout"):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    ex = _Ex(boom)
    db = _DB()
    g = Guard(cfg)
    g.stall_grace_ms = 10 ** 13
    e = DecisionEngine(cfg, _Feed(), ex, db, guard=g)
    e.pos = {"qty": 0.001, "entry": 80000.0, "side": "long", "best": 80000.0,
             "worst": 80000.0, "entry_ts": 0, "bars": 0}
    e.ex = ex
    e.guard = g
    return e, ex, db, g


def test_a_raising_close_is_counted_too():
    """★ 抛异常的平仓必须计入连续失败。

    这是本轮的核心：不算的话，闸永远到不了 5，
    而这条路径恰恰在行情暴动时最容易走到。
    """
    import asyncio
    e, ex, db, g = _engine()
    for _ in range(6):
        e._close_retry_ts = 0          # 绕过退避闸，模拟每次都在试
        try:
            asyncio.get_event_loop().run_until_complete(
                e._close("止损", 70000.0, 70000.0, {}))
        except Exception:
            pass
    assert ex.calls >= 6, u"前提不成立：market_order 没被调用 %d 次" % ex.calls
    assert e._close_fail_count >= 5, (
        u"平仓抛了 %d 次异常，失败计数却只有 %d —— 止损通道闸永远到不了 5"
        % (ex.calls, e._close_fail_count))
    ok, why = g.can_open(10 ** 13)
    assert ok is False, u"平仓连续失败 5 次以上，却仍然放行开仓"
    assert u"止损通道失效" in why, u"拦住的原因不对：%r" % (why,)


def test_a_raising_close_sets_the_backoff():
    """★ 抛异常也要设退避，否则会每 241ms 打一次交易所。

    变异「异常不设置退避」第一轮 SURVIVED —— 这条测试就是为它写的。

    这条路径上的后果比"少卖一次"严重得多：_step 每 ~241ms 跑一次，
    异常路径若不设退避，就会每秒对交易所发约 4 次下单+查询请求。
    币安对下单端点的限频是硬的，超了直接封 IP（418/429）——
    封禁期间【连平仓都发不出去】，止损彻底失效。
    也就是说：一个"忘了设退避"的小疏漏，会升级成"账户被锁住卖不掉"。
    """
    import asyncio
    import time as _t
    e, ex, db, g = _engine()
    assert getattr(e, "_close_retry_ts", 0) == 0, u"前提不成立：初始就该是 0"
    try:
        asyncio.get_event_loop().run_until_complete(
            e._close("止损", 70000.0, 70000.0, {}))
    except Exception:
        pass
    retry_at = int(getattr(e, "_close_retry_ts", 0) or 0)
    assert retry_at > 0, u"平仓抛异常之后没有设退避，会每 241ms 打一次交易所"
    assert retry_at > int(_t.time() * 1000), u"退避时间点不在未来：%r" % (retry_at,)


def test_the_raising_close_is_recorded_as_close_failed_not_engine_error():
    """★ 抛异常也要留下 close_failed 痕迹，不能只记 engine_error。

    engine_error 是"某个决策步骤崩了"，语义太宽；
    而"止损打不出去"是一个具体的、要人立刻处理的状况。
    只记 engine_error 的话，看板上看到的仍然只是"有个异常"。
    """
    import asyncio
    e, ex, db, g = _engine(boom="HTTP 503 /api/v3/order")
    e._close_retry_ts = 0
    try:
        asyncio.get_event_loop().run_until_complete(
            e._close("止损", 70000.0, 70000.0, {}))
    except Exception:
        pass
    kinds = [k for k, _ in db.risks]
    assert "close_failed" in kinds, (
        u"平仓抛异常没有记 close_failed，只记了：%r" % (kinds,))
