# -*- coding: utf-8 -*-
"""止损失效（平仓连续失败）必须被别人看见。

为什么单独写这个文件（第 116 轮）：
  前三轮（113/114/115）查的都是「闸有没有接线」。
  这轮问一个不同的问题：
  **六道闸都接上线了，那「闸拦不住、钱却在漏」的情况有没有出口？**

  止损失效就是这种情况。它的完整链路是：

      价格跌破熔断线
        -> _close() 发市价卖出
        -> 交易所 5xx / 网络抖 / 熔断中只放行减仓但余额对不上
        -> FillResult(ok=False)
        -> 退避 3s / 6s / 12s ... 上限 300s
        -> 下一轮再来
        -> 仓位仍然在手上，价格继续跌

  实测（真引擎 + 一个永远返回 500 的执行器替身，连续 10 次）：

      _close -> False，退避 3 秒
      _close -> False，退避 6 秒
      ...
      risk_events 里 10 条 close_failed

      看板 /api/trade/state 的 executor 字段 = {'name':'spot','halted':False,'reason':''}
      state 里没有任何 close 失败相关字段
      Guard: can_open = (True, '')   halted_hard = False
      看板/哨兵/健康自检/Guard —— 四个监控面【一个都不读】close_failed

  也就是说：退避逻辑本身是对的（该重试的在重试），
  但**「止损打不出去」这个事实没有任何出口**。
  它不是靠 bug 静默，是靠「没有人订阅这个事件」静默。

  这和前面几轮的区别值得写下来：
    前几轮是「闸没接线」——查的是代码路径；
    这轮是「接线了但没有人看」——查的是**观察者**。

本文件钉四件事：
  1. 平仓连续失败到一定次数，Guard 必须真的下闸（不是告警，是停止开新仓）；
  2. 少数几次失败不许下闸（网络抖动很正常，误停代价更大）；
  3. 平仓一旦成功，计数必须清零（否则下一次正常失败会直接顶到阈值）；
  4. 这个失败次数必须出现在看板拿到的 state 里（否则用户只能靠日志考古）。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.base import FillResult
from ai_trader.guard import Guard
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

PX = 78000.0


class _Hub:
    def publish(self, *a, **k):
        pass


class _Feed:
    def __init__(self, px=PX):
        self.last_price = px
        self.cfg = AppCfg()
        self.hub = _Hub()
        self.src = "WS"

        class _A:
            def current(self, iv):
                class _B:
                    close = px
                return _B()
        self.agg = _A()


class _DB:
    def __init__(self):
        self.risks = []

    def log_risk(self, k, m):
        self.risks.append((k, m))

    def log_decision(self, *a, **k):
        pass

    def log_order(self, d):
        pass

    def log_fill(self, d):
        pass

    def log_trade(self, d):
        return 1

    def log_equity(self, d):
        pass

    def close_trade(self, d):
        pass


class _Ex:
    """永远平仓失败的执行器替身。"""

    name = "spot"

    def __init__(self, fail=True):
        self.halted = ""
        self.fail = fail

    async def equity(self):
        return {"balance": 0.0, "unrealized": 0.0, "equity": 53.0,
                "position_qty": 0.00068, "position_side": "long"}

    async def mark_price(self):
        return PX

    async def position(self):
        return {"qty": 0.00068, "entry": PX, "side": "long"}

    async def market_order(self, side, qty, reason="", reduce_only=False):
        if self.fail:
            return FillResult(ok=False, reason="HTTP 500 /api/v3/order: Internal Server Error")
        return FillResult(ok=True, price=PX, qty=qty, fee=0.08, status="filled")


def _eng(fail=True):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    feed = _Feed()
    db = _DB()
    g = Guard(cfg)
    e = DecisionEngine(cfg, feed, _Ex(fail=fail), db, guard=g)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    e.trend.seeded = True
    e.pos = {"side": "long", "qty": 0.00068, "entry": PX, "stop": PX * 0.75,
             "target": 0.0, "entry_ts": int(time.time() * 1000) - 3600000,
             "bars": 0, "best": PX, "worst": PX, "trade_id": 1,
             "max_hold_bars": 10 ** 9, "open_reason": "", "fee": 0.05,
             "signal": "trend", "dust": 0.0}
    return e, db, g


async def _fail_close(e, times):
    """连续打 times 次平仓（每次清掉退避，模拟"时间过去了"）。"""
    for _ in range(times):
        e._close_retry_ts = 0
        await e._close("熔断止损", PX * 0.75, PX * 0.75, {})


@pytest.mark.asyncio
async def test_repeated_close_failures_trip_the_guard():
    """★ 止损打不出去，且不是偶发 —— 必须停止开新仓。

    修之前：退避照跑、risk_events 照写、Guard 一无所知，
    看板显示 halted=False、can_open=(True,'')。
    """
    e, db, g = _eng()
    await _fail_close(e, 8)
    ok, why = g.can_open()
    assert not ok, (u"平仓连续失败 8 次，仓位还裸在下跌里，"
                    u"Guard 却仍然放行开仓：%r" % (ok,))
    assert u"平仓" in why or u"止损" in why, u"闸的理由要能看出是平仓失败：%r" % (why,)


@pytest.mark.asyncio
async def test_a_couple_of_close_failures_do_not_trip_the_guard():
    """反向约束：偶发失败不能停摆。

    交易所 5xx / 网络抖动，重试一次常常就好了。
    把 2 次失败也当成"止损失效"，会把正常抖动升级成停机。
    这条和第 1 条必须成对存在——只测"失败要停"，一个"总是停"的实现也能过。
    """
    e, db, g = _eng()
    await _fail_close(e, 2)
    ok, why = g.can_open()
    assert ok, u"才失败 2 次就把系统停了，正常网络抖动会被误判成止损失效：%r" % (why,)


@pytest.mark.asyncio
async def test_a_successful_close_clears_the_streak():
    """中途成功一次 = 止损通道是通的，计数必须回到 0。

    否则计数单调累加，实盘跑久了（每次都有零星失败）
    迟早会顶到阈值，而那时候根本没有止损失效。
    """
    e, db, g = _eng()
    await _fail_close(e, 4)
    assert e._close_fail_count >= 4
    # 通道恢复
    e.ex.fail = False
    e._close_retry_ts = 0
    await e._close("熔断止损", PX * 0.75, PX * 0.75, {})
    assert e._close_fail_count == 0, u"平仓成功后失败计数没清零"
    assert e.pos is None, "构造前提：成功平仓后应已空仓"

    # 再开一笔，然后失败几次，也不该立刻触发（计数从 0 重新开始）
    e.pos = {"side": "long", "qty": 0.00068, "entry": PX, "stop": PX * 0.75,
             "target": 0.0, "entry_ts": int(time.time() * 1000) - 3600000,
             "bars": 0, "best": PX, "worst": PX, "trade_id": 1,
             "max_hold_bars": 10 ** 9, "open_reason": "", "fee": 0.05,
             "signal": "trend", "dust": 0.0}
    e.ex.fail = True
    await _fail_close(e, 2)
    ok, why = g.can_open()
    assert ok, u"计数没有真正重置，零星失败被累加成止损失效：%r" % (why,)


@pytest.mark.asyncio
async def test_a_recovered_close_channel_releases_the_guard():
    """通道恢复后闸必须自动解除。

    这条是补上来的：上面那条「成功清零」只验证了【计数】回到 0，
    而当时闸根本没触发过（只失败 4 次，没到 5），
    所以「解除」这条路径一次都没被走过。

    变异测试抓到了这一点：把解除分支改成 elif False，4 条测试全绿。
    一个永久停摆的实现也能过——正是这个文件开头警告过的那种假绿。
    """
    e, db, g = _eng()
    await _fail_close(e, 8)
    assert not g.can_open()[0], "构造前提：应先被止损失效拦住"

    # 交易所恢复
    e.ex.fail = False
    e._close_retry_ts = 0
    await e._close("熔断止损", PX * 0.75, PX * 0.75, {})
    assert e.pos is None, "构造前提：这一拍应已平掉"

    ok, why = g.can_open()
    assert ok, (u"平仓已经成功、通道恢复了，闸却还挂着「%s」——"
                u"那就是永久停摆，和文档写的自动解除相反" % (why,))


@pytest.mark.asyncio
async def test_the_close_failure_count_is_visible_on_the_board():
    """失败次数必须出现在 state 里。

    看板是用户唯一会看的地方。只写进 risk_events 的话，
    用户要发现"止损打不出去"就得去翻库。
    """
    e, db, g = _eng()
    await _fail_close(e, 3)
    st = e.state()
    assert "close_fail_count" in st.get("executor", {}), (
        u"/api/trade/state 的 executor 字段里没有 close_fail_count，"
        u"看板无法显示「止损正在失败」")
    assert st["executor"]["close_fail_count"] == 3
