# -*- coding: utf-8 -*-
"""对账不一致的告警通道炸了，不能把对账本身也炸掉。

真实事故（第 129 轮）：
  engine.reconcile_live() 里写的是

      self.guard.notify("实盘对账不一致，已按交易所持仓修正（详见日志）")

  只传了一个参数。而生产上的 notifier 是 feishu_notifier() 返回的
  _send(title, body)，两个位置参数都必填 —— 于是【每一次】对账不一致都抛

      feishu_notifier.<locals>._send() missing 1 required positional argument: 'body'

  生产实证：logs/ai-trader.log.1 里 11-12 21:12:21 与 22:57:51 各一次，
  11-11 09:05 -> 23:35 共 14 次；risk_events 里 16 条 reconcile_failed
  全是这同一个字符串。

  损害不是"少发一条消息"：异常从 reconcile_live() 冒出去，
  下面那句 trend.mark_reconciled() 永远执行不到 —— 于是 Donchian 的
  need_reconcile 标志一直挂着。它一旦在"已平仓 + 价格还在上轨上方"
  的时候被 startup_entry 看见，就会绕过"必须等下一根"的规则立刻补开仓。

  为什么 CI 从来没红过：tests/test_spot_reconcile.py 里的 Guard 桩写的是
  `def notify(self, *a, **k)` —— 星号把参数个数错误整个吞掉了。
  桩比真实对象宽松，就等于这条路径从来没被测过。

本文件钉四件事：
  1. 所有参数的 notifier 下，对账都跑得完，且 mark_reconciled() 真的执行了；
  2. notifier 抛任意异常时，对账【仍然】跑得完（账必须先修对）；
  3. 硬停照旧踩下去 —— 修通知不等于放宽闸门；
  4. 结构约束：engine 里不许再出现单参数的 guard.notify(...) 调用。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

PX = 78000.0
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Hub:
    def publish(self, *a, **k):
        pass


class _Feed:
    def __init__(self, px=PX):
        self.last_price = px
        self.cfg = AppCfg()
        self.hub = _Hub()
        self.src = "WS"

        class _Agg:
            def current(self, iv):
                class _B:
                    close = px
                return _B()
        self.agg = _Agg()


class _DB:
    """只记内存。open_trade 返回 None（数据库空仓）。"""

    def __init__(self):
        self.risks = []
        self.trades = []

    def log_risk(self, k, m):
        self.risks.append((k, m))

    def log_decision(self, *a, **k):
        pass

    def log_order(self, d):
        pass

    def log_fill(self, d):
        pass

    def log_trade(self, d):
        self.trades.append(d)
        return 1

    def log_equity(self, d):
        pass

    def close_trade(self, d):
        pass

    def open_trade(self):
        return None


class _Ex:
    name = "spot"

    def __init__(self, qty=0.001, entry=PX):
        self.qty = qty
        self.entry = entry
        self.mark = PX

    async def position(self):
        return {"qty": self.qty, "entry": self.entry,
                "side": "long" if self.qty else ""}

    async def equity(self):
        return {"balance": 100.0, "unrealized": 0.0,
                "equity": 100.0 + self.qty * self.mark,
                "position_qty": self.qty,
                "position_side": "long" if self.qty else ""}

    async def market_order(self, side, qty, reason="", reduce_only=False):
        raise AssertionError("对账路径不该下单")


def _eng(notify):
    """数据库空仓、交易所有 0.001 BTC —— 必然走接管 + 硬停。"""
    cfg = AppCfg()
    cfg.trading.strategy = "donchian"
    cfg.trading.exec_market = "spot"
    feed = _Feed()
    db = _DB()
    g = Guard(cfg, tdb=db)
    g.notify = notify
    e = DecisionEngine(cfg, feed, _Ex(), db, guard=g)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    e.trend.seeded = True
    e.trend.need_reconcile = True      # 默认就是 True，显式写出来
    return e, db, g


def _strict_two_arg(title, body):
    """和 feishu_notifier()._send 完全一样的签名：两个位置参数都必填。

    这就是生产上的 notifier。少传一个参就 TypeError —— 也正是原来的 bug。
    """
    return None


@pytest.mark.asyncio
async def test_reconcile_completes_with_a_two_arg_notifier():
    """★ 生产同款 notifier（两个必填参数）下，对账必须整个跑完。"""
    e, db, g = _eng(_strict_two_arg)
    await e.reconcile_live()          # 修之前这里直接抛 TypeError
    assert e.adopted_pos is not None, "孤儿仓没被接管"
    assert g.st.halted_hard is True, "对账不一致必须硬停"
    assert e.trend.need_reconcile is False, (
        u"mark_reconciled() 没执行 —— 这正是通知抛异常把 reconcile 中断后的症状。"
        u"标志挂着会让 startup_entry 绕过「必须等下一根」立刻补开仓")


@pytest.mark.asyncio
async def test_a_failing_notifier_cannot_abort_the_reconcile():
    """告警通道抛什么异常都不许把对账搞崩。

    现实里的失败方式不止"参数个数错"一种：webhook 超时、DNS 失败、
    飞书返回错误码后 urllib 抛 HTTPError —— 都会从这里冒出去。
    对账修的是本地账本，是全系统唯一敢说"我到底有多少仓"的地方。
    """
    def boom(title, body):
        raise RuntimeError("webhook 超时")

    e, db, g = _eng(boom)
    await e.reconcile_live()
    assert e.adopted_pos is not None, "通知失败竟导致仓位没被接管"
    assert g.st.halted_hard is True, "通知失败竟导致硬停没踩下去"
    assert e.trend.need_reconcile is False, "通知失败竟导致 mark_reconciled 被跳过"


@pytest.mark.asyncio
async def test_the_halt_still_happens_after_the_notify_fix():
    """反向约束：修通知不等于放宽闸门。

    只测"不抛异常"的话，把 position_mismatch 整个删掉也能全过 ——
    而那样对账不一致就彻底不告警了，比原来的 bug 更糟。
    """
    e, db, g = _eng(_strict_two_arg)
    await e.reconcile_live()
    ok, why = g.can_open()
    assert not ok, u"对账不一致却还能开仓：%r" % (why,)
    assert g.st.halt_kind == "mismatch", \
        u"硬停类型应是 mismatch，实际 %r" % (g.st.halt_kind,)
    # 告警必须真的发出去过一次（哪怕通道是坏的，_event 也会落 risk_events）
    kinds = [k for k, _ in db.risks]
    assert "guard_halt" in kinds, \
        u"硬停没有落 risk_events，实际 %r" % (kinds,)


def test_engine_never_calls_notify_with_one_argument():
    """结构约束：engine 里不许再出现单参数的 guard.notify(...)。

    行为测试可以被"换个写法绕开"，这条钉的是源码事实：
    notifier 的契约是 (title, body)，调用点必须两个人参。
    已经不需要单独 notify 的前提是 position_mismatch() 内部会走
    _event(alert=True) —— 那里用的是正确的两个参数。
    """
    p = os.path.join(HERE, "ai_trader", "trading", "engine.py")
    src = open(p, encoding="utf-8").read()
    bad = []
    for m in re.finditer(r"\.notify\(", src):
        # 注释里写着"曾经是 guard.notify(单参)"是正常的——这里只查真代码。
        line_start = src.rfind("\n", 0, m.start()) + 1
        hash_at = src.find("#", line_start, m.start())
        if hash_at != -1:
            continue
        # 从匹配点往后取到配对的右括号
        i = m.end()
        depth = 1
        while i < len(src) and depth:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        call = src[m.end():i - 1]
        # 逗号在顶层才算"第二个参数"（不算元组/函数参数里的逗号）
        depth2 = 0
        commas = 0
        for ch in call:
            if ch in "([{":
                depth2 += 1
            elif ch in ")]}":
                depth2 -= 1
            elif ch == "," and depth2 == 0:
                commas += 1
        if commas == 0:
            bad.append(src[max(0, m.start() - 60):i].strip().replace("\n", " "))
    assert not bad, (
        u"engine.py 里还有单参数的 notify() 调用 —— notifier 的契约是"
        u"(title, body)，少传一个必然 TypeError：\n  " + u"\n  ".join(bad))
