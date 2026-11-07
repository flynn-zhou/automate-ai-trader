# -*- coding: utf-8 -*-
"""Guard 第 3 条闸「引擎卡住」到底能不能刹住车。

为什么单独写这个文件（第 113 轮）：
  guard.py 的文档把第 3 条列为六条刹车之一——
  「引擎卡住（30s 无新决策）-> 临时停开仓（主循环假死是最危险的状态）」。

  但生产调用顺序是 engine.py:428-429：
      self.guard.notify_decision(now_ms)      # 把心跳盖成 now_ms
      self.guard.update(equity, src, now_ms)  # 立刻用【同一个 now_ms】判定

  于是 now_ms - last_decision_ts 恒等于 0，这条闸在结构上永远不会成立。

  实测（本地复刻调用顺序，连跑 10 分钟）：can_open 一直是 True。
  线上佐证：risk_events 表里 guard_temp 这一类【一条都没有】，
  62 条全是 engine_error——六条闸里唯一会走 guard_temp 的告警通道从没触发过。

  「一个永远不会触发的闸」比没有这个闸更糟：
  它让人以为主循环假死会有东西兜底，从而不去看真正兜底的那一层
  （deploy/healthcheck.sh 的连续 3 次停滞才重启，15 分钟起步）。

本文件钉四件事：
  1. 心跳真的会过期——连续 60 秒没有 update，必须拦住开仓；
  2. 引擎彻底停掉（连 update 都不再调用）时，读侧也必须能说出「卡住了」；
  3. 正常运行（每 3 秒一次心跳）绝不能误报；
  4. 进程刚起、一次心跳都还没有时，绝不能凭空报「卡住」。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.guard import Guard

T = 1700000000000          # 固定基准时刻，避免任何对真实时钟的依赖


class _Cfg:
    pass


import time as _time


@pytest.fixture(autouse=True)
def _freeze_wall_clock(monkeypatch):
    """把墙钟冻在 T 上。

    这一轮改动后 Guard 有两把尺子：
      last_decision_ts —— 决策时钟，由 update() 传入的 now_ms 驱动（测试可注入）
      last_heartbeat_ms —— 真实墙钟，记录 update() 上一次真的被执行
    读侧（can_open）用墙钟判「没人再更新了」。这个测试文件里的场景都是
    人造时间，所以必须把墙钟也冻在同一基准，否则判出来的差值是几十年。
    """
    monkeypatch.setattr(_time, 'time', lambda: T / 1000.0)


def _g():
    return Guard(_Cfg())


def test_the_heartbeat_can_actually_expire():
    """★ 生产里每 3 秒一次 update，这里模拟主循环卡了 60 秒才转过来。

    这是 engine.py 的真正调用形态（只调 update，不再自己盖心跳）。
    修之前：update() 内部判定用的是同一时刻的心跳，恒不成立。
    """
    g = _g()
    g.update(100.0, "WS", T)
    assert g.can_open(now_ms=T)[0], "第一步健康时不该拦"
    g.update(100.0, "WS", T + 60000)
    ok, why = g.can_open(now_ms=T + 60000)
    assert not ok, "主循环 60 秒没有新决策，第 3 条闸必须刹住"
    assert "引擎" in why, "拦截原因必须点名是引擎卡住，不能是别的闸：%r" % (why,)


def test_a_dead_engine_is_reported_stalled_at_read_time():
    """★ 引擎彻底停了：连 update 都不再被调用。

    这种情况下没有人在写状态，只有读侧（web/app.py 的 /api/guard、
    大屏）还在问「现在能不能开」。判定必须发生在【读的那一刻】，
    否则状态永远停在最后一次 update 的样子——也就是「一切正常」。

    这是本文件最重要的一条：只修写侧还不够，主循环死了就没人在写。
    """
    g = _g()
    g.update(100.0, "WS", T)          # 最后一次心跳
    ok, why = g.can_open(now_ms=T + 600000)   # 10 分钟后再问
    assert not ok, ("引擎已经 10 分钟没有决策，can_open 却说可以开——"
                    "大屏会显示「一切正常」，而持仓的止损已经没人管了")
    assert "引擎" in why


def test_a_healthy_loop_never_reports_stall():
    """反向约束：正常 3 秒一次心跳时，绝不能因为这条闸误报。

    误报的代价是把真正的问题淹掉——这一点在第 80 轮
    （决策日志 10 分钟节流 vs 90 秒门槛）已经付过一次学费。
    """
    g = _g()
    for k in range(0, 200):
        now = T + k * 3000
        g.update(100.0, "WS", now)
        ok, why = g.can_open(now_ms=now)
        assert ok, u"第 %d 步（正常 3s 心跳）被误拦：%s" % (k, why)


def test_a_guard_that_never_saw_a_heartbeat_does_not_invent_a_stall():
    """进程刚起、一次决策都还没有：不能凭空报「引擎卡住」。

    否则重启后的头 30 秒会进入一个假的拦截态，
    而重启正是最需要它能开仓的时刻之一（等到金叉不容易）。
    """
    g = _g()
    ok, why = g.can_open(now_ms=T + 3600000)
    assert ok, u"从未有过心跳却被判定为卡住：%s" % (why,)


def test_resuming_the_loop_clears_the_stall():
    """主循环恢复后必须自动解除，否则一次抖动就永久停开仓。

    注意解除发生在【恢复后的下一次心跳】，不是恢复的那一刻：
    恢复的那次 update 回头看确实是 600 秒没有决策，按真实差值判它就是该拦。
    多拦一个 3 秒周期是安全的，早一个周期放行才是事故。
    """
    g = _g()
    g.update(100.0, "WS", T)
    g.update(100.0, "WS", T + 600000)        # 恢复的那一拍：回头看确实停了 600s
    assert not g.can_open(now_ms=T + 600000)[0], (
        u"恢复的同一拍就放行，等于把刚才那 600 秒停顿当作没发生")
    g.update(100.0, "WS", T + 603000)        # 下一拍：差值回到 3 秒
    ok, why = g.can_open(now_ms=T + 603000)
    assert ok, u"主循环恢复两拍后仍然不放行：%s" % (why,)


def test_the_engine_does_not_re_stamp_the_heartbeat_before_judging():
    """结构约束：engine.py 里不能出现「先盖心跳、再用同一时刻判定」的写法。

    这条直接盯源码，因为上面那些行为测试都可以被一种「写侧自欺」的实现骗过：
    只要在某处补一次 notify_decision(now)，判定就又恒不成立了。
    这也是它当初是怎么写坏的原因——不是逻辑写错，是调用顺序写错。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ai_trader", "trading", "engine.py")
    src = open(p, encoding="utf-8").read()
    assert "notify_decision" not in src, (
        u"engine.py 仍在调用 notify_decision：它会把心跳盖成当前时刻，"
        u"使第 3 条闸（引擎卡住）永远不成立。心跳应由 update() 自己维护。")

def test_a_frozen_wall_clock_alone_proves_the_engine_stopped(monkeypatch):
    """★ 真正的读侧：不注入任何参数，只有墙钟在走。

    上面那条「引擎彻底停了」用的是 can_open(now_ms=...)，走的是【写侧那把尺子】。
    主循环死掉时，最要紧的恰恰是【不带参数】的这条路径——
    web/app.py 的 /api/guard、以及大屏，就是那么调的。

    这条测试单独存在是因为变异「墙钟不更新」在上面那一组里存活了：
    last_heartbeat_ms 永远为 0 时，读侧因为"从未有过心跳"而沉默，
    而写侧靠注入的 now_ms 依然能判出停滞——两边合起来看像是好的，
    实际上"没人再更新了"这件事根本没被测到。
    """
    import time as _t
    clock = [T / 1000.0]
    monkeypatch.setattr(_t, "time", lambda: clock[0])

    g = _g()
    g.update(100.0, "WS", T)
    assert g.can_open()[0], "刚更新过，不该拦"

    clock[0] = (T + 600000) / 1000.0          # 十分钟没人再调 update()
    ok, why = g.can_open()
    assert not ok, ("墙钟已经走了 10 分钟而 update() 一次都没再被执行，"
                    "can_open() 却说可以开——主循环假死时没有任何东西兜底")
    assert "引擎" in why
