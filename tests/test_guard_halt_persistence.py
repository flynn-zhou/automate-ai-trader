# -*- coding: utf-8 -*-
"""硬停必须能扛过进程重启（第 123 轮）。

要守的不变量
------------
1. 账实不符硬停【必须】在重启后还在。
2. 回撤硬停【必须】在重启后还在，且冷却期从原触发时刻继续算，
   不能因为重启就把 30 天重新归零。
3. 解除/复位之后重启，不能再自己变回"停着"。
4. 恢复失败不能把进程搞挂（退回旧行为 + 明确日志）。

为什么这轮才做
--------------
guard.py 第 136-147 行的注释专门写过这件事——但只写了【峰值】：

    # 峰值必须从库里恢复，不能只活在内存里。
    # 这里曾经只在 update() 里累加，进程一重启峰值就归零，
    # 第一次 update() 把它设成【当时权益】——
    # 于是第 1 条闸度量的不是"从峰值回撤"，而是"从上次重启回撤"。

同一段道理对 halted_hard 一个字都没写，也一个字都没做。
而部署、服务器重启、OOM 都会重启进程（我自己每轮都在部署）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.guard import Guard
from ai_trader.trade_db import TradeDB

DAY = 86400000
T0 = 1600000000000


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    t = TradeDB(path)
    t.start()
    return t, path


def _restart(tdb):
    """模拟进程重启：新建一个 Guard，同一个库。"""
    g = Guard(None, tdb=tdb)
    g.stall_grace_ms = 10 ** 13
    return g


def test_a_mismatch_halt_survives_a_restart():
    """★ 账实不符是这里最要命的一条。

    账实不符意味着我们不知道真实敞口。如果重启就把它清掉，
    那么"停下等人查账"这条闸实际有效期只到下一次部署为止——
    而部署是我自己做的，随时都会发生。
    """
    tdb, path = _db()
    try:
        g = _restart(tdb)
        g.update(100.0, "WS", T0)
        g.position_mismatch(u"账实不符：库里 0.001 BTC，交易所 0.002 BTC")
        assert g.st.halted_hard is True
        # 不手动写库：必须由 Guard 自己落库，否则测的是我而不是接线。
        import time
        time.sleep(0.6)

        g2 = _restart(tdb)
        assert g2.st.halted_hard is True, (
            u"重启把账实不符硬停清掉了——这条闸的有效期只到下次部署")
        assert g2.st.halt_kind == "mismatch"
        ok, why = g2.can_open(T0 + 60000)
        assert ok is False, u"重启之后账实不符就放行开仓了"
        assert u"账实不符" in why
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_a_drawdown_halt_keeps_its_cooldown_across_a_restart():
    """★ 回撤硬停重启后，冷却期必须从【原触发时刻】继续算。

    如果重启把 halt_ts 归零，冷却期就变成"从这次重启起算"，
    看起来无害，但只要进程重启得比 30 天更频繁
    （我自己每轮都在部署），冷却期【永远不会到期】——
    系统又变回永久停摆，只是这次是被重启"续命"的。
    """
    tdb, path = _db()
    try:
        g = _restart(tdb)
        g.update(100.0, "WS", T0)
        g.update(35.0, "WS", T0 + 60000)     # 65% 回撤 -> 硬停
        assert g.st.halted_hard is True
        assert g.st.halt_kind == "drawdown"
        import time
        time.sleep(0.6)

        g2 = _restart(tdb)
        assert g2.st.halted_hard is True, u"重启把回撤硬停清掉了"
        assert g2.st.halt_ts == g.st.halt_ts, (
            u"重启后 halt_ts 变了（%r -> %r），冷却期被重置"
            % (g.st.halt_ts, g2.st.halt_ts))
        # 第 29 天仍然停着
        g2.update(35.0, "WS", T0 + 60000 + int(29 * DAY))
        assert g2.st.halted_hard is True, u"重启后第 29 天就解除了"
        # 第 31 天解除
        g2.update(35.0, "WS", T0 + 60000 + int(31 * DAY))
        assert g2.st.halted_hard is False, (
            u"重启之后冷却期没有继续走，系统永远不会自动恢复")
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_an_auto_release_survives_the_restart_too():
    """★ 自动解除之后重启，不能把硬停读回来。

    变异「解除不落库」第一轮 SURVIVED —— 因为下一条测试走的是 reset()，
    而不是自动解除这条路。两条路的落库是两行代码，
    只测一条就等于只测了一半。
    """
    import time
    tdb, path = _db()
    try:
        g = _restart(tdb)
        g.update(100.0, "WS", T0)
        g.update(35.0, "WS", T0 + 60000)
        assert g.st.halted_hard is True
        # 走【冷却期自动解除】这条路，不是 reset()
        g.update(35.0, "WS", T0 + 60000 + int(31 * DAY))
        assert g.st.halted_hard is False, u"自动解除没生效，前提不成立"
        time.sleep(0.6)

        g2 = _restart(tdb)
        assert g2.st.halted_hard is False, (
            u"自动解除没有落库，重启后硬停被读回来了——系统会永久停摆")
        assert g2.st.halt_kind == ""
        ok, why = g2.can_open(T0 + 60000 + int(31 * DAY))
        assert ok is False or u"回撤" not in why, (
            u"重启后又被旧的回撤硬停拦住了：%r" % (why,))
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_a_release_survives_the_restart_too():
    """反向：解除/复位之后重启，不能自己变回"停着"。

    只测"停住了要恢复"，一个"永远从库里读出停着"的实现也能过——
    而那会让系统再也起不来。
    """
    tdb, path = _db()
    try:
        g = _restart(tdb)
        g.update(100.0, "WS", T0)
        g.update(35.0, "WS", T0 + 60000)
        assert g.st.halted_hard is True
        g.reset()
        assert g.st.halted_hard is False
        import time
        time.sleep(0.6)

        g2 = _restart(tdb)
        assert g2.st.halted_hard is False, (
            u"人工复位之后重启，硬停又回来了——系统再也起不来")
        assert g2.st.halt_kind == ""
        ok, why = g2.can_open(T0 + 120000)
        assert ok is True, u"复位后重启仍开不了仓：%r" % (why,)
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass