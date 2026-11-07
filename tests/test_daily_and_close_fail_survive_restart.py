# -*- coding: utf-8 -*-
"""日亏损闸和平仓失败闸，同样是"重启一次就静默解除"（第 127 轮）。

要守的不变量
------------
1. 当日亏损闸触发后，进程重启仍然锁着（当天之内）。
2. 第二天到了必须自己解除——不能变成永久停摆。
3. 平仓连续失败闸触发后，进程重启仍然锁着。
4. 通道恢复后必须能解除。

为什么这轮做
------------
第 123 轮修了 halted_hard，第 126 轮修了 _trend_block_bar，
两次都是"内存状态扛不过重启"。

而 GuardState 里【还有四个字段没落库】：

    day                ""            <- 重启后是空串
    day_start_equity   0.0           <- 重启后是 0
    daily_halt_day     ""            <- 重启后是空串
    close_fail_streak  0             <- 重启后是 0

于是第一次 update() 会把 day_start_equity 设成【当时权益】：

    今天已经亏了 55% -> 日亏损闸锁上 -> 我去部署 -> 进程重启
    -> day_start_equity = 当前权益 -> 当日亏损 0% -> 闸自动解除

**重启等于给日亏损闸按了一次人工复位。**
而重启是我自己每轮都在做的事，OOM 和哨兵重启也会触发。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.trade_db import TradeDB

DAY = 86400000
T0 = 1600000000000          # 2020-09-13 12:26:40 UTC


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    t = TradeDB(path)
    t.start()
    return t, path


def _guard(tdb):
    g = Guard(None, tdb=tdb)
    g.stall_grace_ms = 10 ** 13
    return g


def _cleanup(tdb, path):
    tdb.stop()
    try:
        os.unlink(path)
    except OSError:
        pass


def test_an_old_database_gets_the_new_guard_columns():
    """★ 老库必须被 ALTER 补列，而不是靠 CREATE TABLE IF NOT EXISTS。

    前六条测试全用【新建】的库，于是这个缺陷一个都没抓到：
    新建的库走的是 CREATE TABLE，四列本来就在。

    线上那个库的 guard_state 是第 123 轮建的（6 列），
    对 CREATE TABLE IF NOT EXISTS 完全免疫——
    部署上去之后 load_guard_state 读到的全是默认值，闸照样被重启解开。

    实测踩到：改完代码部署到线上库副本上跑，报「老库上没补出新列」。
    这正是 trade_db.py:269-279 那段注释写着的坑，我自己又踩了一次。
    """
    import sqlite3
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # 造一个第 123 轮形态的老库：guard_state 只有 6 列，且已有一行
        c = sqlite3.connect(path)
        c.executescript(
            "CREATE TABLE guard_state ("
            "  id INTEGER PRIMARY KEY CHECK (id = 1),"
            "  ts INTEGER NOT NULL,"
            "  halted_hard INTEGER NOT NULL DEFAULT 0,"
            "  halt_reason TEXT DEFAULT '',"
            "  halt_kind TEXT DEFAULT '',"
            "  halt_ts INTEGER DEFAULT 0);"
            "INSERT INTO guard_state(id,ts) VALUES(1,0);")
        c.commit()
        c.close()

        t = TradeDB(path)
        t.start()
        try:
            c = sqlite3.connect(path)
            cols = [r[1] for r in c.execute("PRAGMA table_info(guard_state)")]
            c.close()
            for want in ("day", "day_start_equity", "daily_halt_day",
                         "close_fail_streak"):
                assert want in cols, (
                    u"老库的 guard_state 没补出 %s 列（现有：%r）。\n"
                    u"CREATE TABLE IF NOT EXISTS 对已存在的表无效，必须 ALTER。"
                    % (want, cols))
            # 补完还得能读，且默认值是"没触发过"
            st = t.load_guard_state()
            assert st is not None, u"补列之后读不出状态"
            assert st["day"] == "" and st["day_start_equity"] == 0.0
            assert st["close_fail_streak"] == 0
        finally:
            t.stop()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_the_daily_loss_gate_survives_a_restart():
    """★ 当日亏损 55% 锁上之后，重启不能把它解开。

    60% 硬停线比 55% 高，所以这里【只会】触发日亏损闸，
    这样测的就是这一条闸，不会被硬停掩盖。
    """
    tdb, path = _db()
    try:
        g = _guard(tdb)
        g.update(100.0, "WS", T0)
        g.update(45.0, "WS", T0 + 60000)      # 当日 -55%
        assert g.st.halted_hard is False, u"前提不成立：意外触发了硬停"
        ok, why = g.can_open(T0 + 61000)
        assert ok is False, u"前提不成立：日亏损闸没生效"
        assert u"当日亏损" in why

        import time
        time.sleep(0.6)

        g2 = _guard(tdb)
        # 重启后的第一次 update：此时还没有任何当日基准
        g2.update(45.0, "WS", T0 + 120000)
        ok2, why2 = g2.can_open(T0 + 121000)
        assert ok2 is False, (
            u"日亏损闸被重启解开了。重启前：%r；重启后：%r\n"
            u"重启等于给这条闸按了一次人工复位。" % (why, why2))
    finally:
        _cleanup(tdb, path)


def test_the_daily_latch_survives_a_restart_even_after_a_bounce():
    """★ 闸是【当天】的锁，不是"当前亏损超 55%"。

    这一条是我写完前一条之后自己发现漏的：
    前一条里重启后权益仍是 45（还是 -55%），所以 update() 会【重新触发】一次，
    测试就绿了——但那测的是"重新触发"，不是"latch 扛过重启"。

    真实情形是：亏到 -55% 锁上 -> 行情反弹 -> 权益回到 80（当日 -20%）
    -> 此时重启 -> day_start_equity 恢复了、daily_halt_day 也恢复了，
    但 temp_reasons 是空的、亏损也没到 -55%，于是【两个分支都不走】，
    闸静默打开。

    而文档写的是「触发之后当天即使反弹回 -40% 也不许接着赌」。
    """
    tdb, path = _db()
    try:
        g = _guard(tdb)
        g.update(100.0, "WS", T0)
        g.update(45.0, "WS", T0 + 60000)
        assert g.can_open(T0 + 61000)[0] is False

        import time
        time.sleep(0.6)

        # 重启 + 行情反弹回 80（当日 -20%，远未到 55% 线）
        g2 = _guard(tdb)
        g2.update(80.0, "WS", T0 + 3600000)
        ok, why = g2.can_open(T0 + 3601000)
        assert ok is False, (
            u"当日已经触发过日亏损闸，反弹到 -20%% 再重启，闸就被解开了（%r）。\n"
            u"这条闸的语义是【当日】而不是【当前】，latch 没能扛过重启。" % (why,))
    finally:
        _cleanup(tdb, path)


def test_the_next_day_does_release_the_daily_loss_gate():
    """★ 反向：第二天必须自己解除——不能变成永久停摆。

    只测"重启后还锁着"，一个"永远锁着"的实现也能过，
    而那正是第 121 轮那个永久停摆的形状。
    """
    tdb, path = _db()
    try:
        g = _guard(tdb)
        g.update(100.0, "WS", T0)
        g.update(45.0, "WS", T0 + 60000)
        assert g.can_open(T0 + 61000)[0] is False

        # 重启 + 第二天
        g2 = _guard(tdb)
        g2.update(45.0, "WS", T0 + DAY + 60000)
        ok, why = g2.can_open(T0 + DAY + 61000)
        assert ok is True, (
            u"到了第二天日亏损闸仍然锁着（%r）——这条闸会变成永久停摆" % (why,))
    finally:
        _cleanup(tdb, path)


def test_the_day_baseline_is_restored_not_rebuilt():
    """★ 重启后当日基准必须还是【今天开盘时】那个数，不是重启那一刻的权益。

    变异「不存 day」第一轮 SURVIVED —— 因为它被下面那条重新挂闸的分支掩盖了：
    只要 latch 还在，闸就仍然锁着，测试看不出区别。

    但 day 丢了意味着 update() 认为"今天是新的一天"，
    于是 day_start_equity 被重设成【当时权益】。闸还锁着（靠 latch），
    可"今天从多少开始、现在亏了多少"这个数已经错了——
    而它正是报告里给我看的那个数。
    """
    tdb, path = _db()
    try:
        g = _guard(tdb)
        g.update(100.0, "WS", T0)
        g.update(45.0, "WS", T0 + 60000)
        assert g.st.day_start_equity == 100.0

        import time
        time.sleep(0.6)

        g2 = _guard(tdb)
        assert g2.st.day == g.st.day, (
            u"重启后 day 没恢复（%r vs %r）——update() 会把基准重建成当时权益"
            % (g2.st.day, g.st.day))
        assert g2.st.day_start_equity == 100.0, (
            u"重启后当日基准被重建成 %r，应该是 100.0" % (g2.st.day_start_equity,))
    finally:
        _cleanup(tdb, path)


def test_the_daily_gate_releases_across_days_without_a_restart():
    """★ 反向：同一个 Guard 跨日必须自己解除（不重启也算）。

    变异「跨日不解除」第一轮 SURVIVED —— 因为我的跨日测试里【重启了】，
    新 Guard 的 temp_reasons 本来是空的，字母面上"解除了"，
    实际上根本没走到解除那一支。

    真实情形是进程跑着不重启跨过零点：latch 还在，亏损也没到线，
    没有解除分支的话这条闸就是单程票——
    正是第 114 轮修过的那个"亏到 50% 就永久停摆"。
    """
    tdb, path = _db()
    try:
        g = _guard(tdb)
        g.update(100.0, "WS", T0)
        g.update(45.0, "WS", T0 + 60000)
        assert g.can_open(T0 + 61000)[0] is False
        assert g.st.daily_halt_day

        # 同一个实例跨日（不重启）
        g.update(45.0, "WS", T0 + DAY + 60000)
        ok, why = g.can_open(T0 + DAY + 61000)
        assert ok is True, (
            u"跨日之后日亏损闸仍然锁着（%r）——这条闸会变成单程票" % (why,))
        assert g.st.daily_halt_day == "", u"跨日之后 latch 没清"
    finally:
        _cleanup(tdb, path)


def test_the_close_fail_gate_survives_a_restart():
    """★ 平仓连续失败 5 次锁上之后，重启不能把它解开。

    止损失效时"不许开新仓"是本金保护的最后一道。
    重启把它清成 0，等于每次部署都重新给系统一次带病开仓的机会。
    """
    tdb, path = _db()
    try:
        g = _guard(tdb)
        g.update(100.0, "WS", T0)
        g.close_fail_streak_changed(6)
        assert g.can_open(T0 + 1000)[0] is False, u"前提不成立：闸没锁上"

        import time
        time.sleep(0.6)

        g2 = _guard(tdb)
        g2.update(100.0, "WS", T0 + 120000)
        ok, why = g2.can_open(T0 + 121000)
        assert ok is False, (
            u"平仓连续失败 6 次的闸被重启解开了：%r" % (why,))
    finally:
        _cleanup(tdb, path)


def test_a_recovered_close_channel_still_releases_after_a_restart():
    """★ 反向：通道恢复之后必须能解除（重启过也算）。"""
    tdb, path = _db()
    try:
        g = _guard(tdb)
        g.update(100.0, "WS", T0)
        g.close_fail_streak_changed(6)
        assert g.can_open(T0 + 1000)[0] is False

        g2 = _guard(tdb)
        g2.update(100.0, "WS", T0 + 120000)
        g2.close_fail_streak_changed(0)
        ok, why = g2.can_open(T0 + 121000)
        assert ok is True, (
            u"平仓通道已经恢复，闸却解不开（%r）——只会锁死不会解除" % (why,))
    finally:
        _cleanup(tdb, path)
