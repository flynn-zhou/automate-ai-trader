# -*- coding: utf-8 -*-
"""无人值守闸的自检：六条刹车必须真的能刹住。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_trader.guard import Guard


class FakeCfg:
    pass


def mk():
    return Guard(FakeCfg())


def test_drawdown_halt():
    g = mk(); now = int(time.time() * 1000)
    g.update(100.0, "REST", now)
    g.update(39.0, "REST", now + 3000)
    ok, why = g.can_open()
    assert not ok and "回撤" in why


def test_feed_down_temp_block_and_recover():
    """数据源 DOWN 满 30 秒才拦，恢复后自动解除。

    ★ 第 113 轮起，「引擎卡住」这条闸真的会触发了（以前它结构上永不成立）。
      所以这里必须用【小于 30 秒】的步长把时间推过宽限期——
      原来一步跳 37 秒，在修好之后会先撞上"引擎 37s 无新决策"，
      于是断言 '数据源' in why 失败。那不是代码错了，是测试用了
      一个生产里不会出现的调用间隔（生产每 3 秒一次）。
    """
    g = mk(); now = int(time.time() * 1000)
    g.update(100.0, "REST", now)
    g.update(100.0, "DOWN", now + 3000)
    assert g.can_open(now_ms=now + 3000)[0], "30s 宽限期内不应拦"
    g.update(100.0, "DOWN", now + 18000)
    g.update(100.0, "DOWN", now + 33000)         # DOWN 累计 33s，单步 15s
    ok, why = g.can_open(now_ms=now + 33000)
    assert not ok and "数据源" in why, u"应报数据源：%r" % (why,)
    g.update(100.0, "REST", now + 36000)
    assert g.can_open(now_ms=now + 36000)[0], "数据源恢复后应自动解除"


def test_stall_block():
    g = mk(); now = int(time.time() * 1000)
    g.notify_decision(now)
    g.update(100.0, "REST", now + 31000)
    ok, why = g.can_open()
    assert not ok and "引擎" in why


def test_daily_loss_block():
    g = mk(); now = int(time.time() * 1000)
    g.update(100.0, "REST", now)
    g.update(48.0, "REST", now + 3000)
    ok, why = g.can_open()
    assert not ok


def test_reset_clears():
    g = mk(); now = int(time.time() * 1000)
    g.update(100.0, "REST", now); g.update(30.0, "REST", now + 3000)
    assert not g.can_open()[0]
    g.reset("test")
    assert g.can_open()[0]
