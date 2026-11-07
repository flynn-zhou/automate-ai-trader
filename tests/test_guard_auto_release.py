# -*- coding: utf-8 -*-
"""回撤硬停的自动解除通道（第 122 轮）。

要守的不变量
------------
1. 回撤硬停【会】在冷却期后自己解除——否则系统停一次就永远停着。
   实测代价：不带 Guard 100U -> 2300.19U，带 60% 硬停 -> 118.30U
   （2018-03-08 停死，之后 8 年 0 次开仓）。
2. 账实不符硬停【永远不】自动解除——那是钱可能放错了地方，必须人看。
3. 解除时峰值必须重新基线，否则同一拍立刻又停回去，
   通道"存在"但一次都走不通。
4. 解除只清硬停，不清其他闸——顺手清掉会放行止损通道失效。
5. 冷却期设 0 等于关闭该通道，可回退到"只能人工复位"。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.guard import Guard

DAY = 86400000
T0 = 1600000000000


def mk(cooldown=30.0):
    g = Guard(None)
    g.hard_halt_cooldown_days = cooldown
    # 关掉「引擎卡住」闸，理由写在这里，不藏在别处：
    #
    # 这些测试要在两拍之间跳 30 天来熬过冷却期。而 stall 闸判的是
    # 【距上一次 update 的间隔】（stall_grace_ms=30s），跳 30 天必然触发。
    # 生产和测试在这里有本质矛盾：真实运行时 update 每 ~241ms 一次，
    # 要让冷却期走完 30 天，就得跑约 1000 万拍。
    # 所以这里显式关掉这条【与被测对象无关】的闸，
    # 它自己的行为由 tests/test_guard_stall_is_real.py 的 7 条测试覆盖。
    g.stall_grace_ms = 10 ** 13
    return g


def drive_to_halt(g, t0=T0, peak=100.0, drop=0.65):
    """把权益从 peak 打到 peak*(1-drop)，触发回撤硬停。"""
    g.update(peak, "WS", t0)
    g.update(peak * (1.0 - drop), "WS", t0 + 60000)
    assert g.st.halted_hard, "没有触发硬停，测试前提不成立"
    return t0 + 60000


def tick(g, eq, ts):
    """模拟生产的一拍：先 update 再 can_open，用【同一个】now_ms。

    为什么必须这样（第一版测试就是在这里红的）：
      这些测试要在两个时刻之间跳 30 天，而 can_open(now_ms) 会用
      now_ms - last_decision_ts 判「引擎卡住」。跳 30 天就等于
      "31 天没有新决策"，那条闸当然拦住。
      生产里 update() 每 ~241ms 跑一次，调用 can_open() 时心跳刚盖过。
      所以测试要照着生产的顺序走：先 update(同一个 ts)，再 can_open(ts)。
      直接跳时间再单独调 can_open，测的就不是被测对象了。
    """
    g.update(eq, "WS", ts)
    return g.can_open(ts)


def test_a_drawdown_halt_releases_itself_after_the_cooldown():
    """★ 冷却期到了必须自己解除。

    这一条是整轮的核心：没有它，系统在第一次 60% 回撤之后永久停摆。
    """
    g = mk(30.0)
    t = drive_to_halt(g)
    assert g.can_open(t)[0] is False, "硬停期间不该能开仓"
    # 第 29 天：还没到，必须仍然停着（否则冷却期形同虚设）
    ok29, why29 = tick(g, 35.0, t + int(29 * DAY))
    assert g.st.halted_hard is True, "第 29 天就解除了，冷却期太短等于没有"
    assert ok29 is False
    assert u"回撤" in why29, "第 29 天拦住的原因不是回撤：%r" % (why29,)
    # 第 31 天：必须解除
    ok31, why31 = tick(g, 35.0, t + int(31 * DAY))
    assert g.st.halted_hard is False, (
        "冷却期已过却仍然硬停——恢复通道不存在，系统会永久停摆")
    assert ok31 is True, "已解除却仍然开不了仓：%r" % (why31,)


def test_the_release_rebases_the_peak_so_it_does_not_immediately_rehalt():
    """★ 解除必须把峰值重新基线到当前权益。

    否则下一拍的回撤判定仍然从旧峰值（100U）算：
    35U 对 100U 是 65% 回撤 >= 60%，立刻又停回去。
    通道"存在"，但一辈子只走一次、也就是没走通。
    """
    g = mk(30.0)
    t = drive_to_halt(g)
    g.update(35.0, "WS", t + int(31 * DAY))
    assert g.st.halted_hard is False
    assert abs(g.st.peak_equity - 35.0) < 1e-6, (
        "峰值没有重新基线（还是 %.2f），下一拍会立刻重新触发硬停" % g.st.peak_equity)
    # 再走一拍确认真的没被重新触发
    ok2, why2 = tick(g, 35.0, t + int(31 * DAY) + 60000)
    assert g.st.halted_hard is False, "解除后又被立刻停回去了"
    assert ok2 is True, "解除之后仍然开不了仓：%r" % (why2,)


def test_a_position_mismatch_halt_never_releases_itself():
    """★ 反向最重要的那条：账实不符不许自动解除。

    回撤是市场走得差，市场会变；账实不符是我们不知道真实敞口，
    冷却期过后依然不知道。自动放行等于赌"这一年间没人动过仓位"。
    """
    g = mk(30.0)
    g.update(100.0, "WS", T0)
    g.position_mismatch(u"账实不符：库里 0.001 BTC，交易所 0.002 BTC")
    assert g.st.halted_hard is True
    assert g.st.halt_kind == "mismatch", (
        "kind 不是 mismatch，会被冷却期放行：%r" % g.st.halt_kind)
    for d in (31, 90, 365, 3650):
        g.update(100.0, "WS", T0 + int(d * DAY))
        assert g.st.halted_hard is True, (
            "账实不符在第 %d 天被自动放行了" % d)
        assert g.can_open(T0 + int(d * DAY))[0] is False


def test_the_release_does_not_clear_other_gates():
    """★ 解除硬停不能顺手清掉别的闸。

    止损通道失效（平仓连续失败 >= 5）和回撤是两回事。
    如果自动解除走的是 reset()，temp_reasons 会被一并清空，
    于是"止损打不出去"这个更危险的状态被冷却期抹掉了。
    """
    g = mk(30.0)
    t = drive_to_halt(g)
    g.close_fail_streak_changed(6)
    assert g.st.temp_reasons.get("close_fail"), "前提不成立：临时闸没写上"
    g.update(35.0, "WS", t + int(31 * DAY))
    assert g.st.halted_hard is False, "硬停应该解除"
    assert g.st.temp_reasons.get("close_fail"), (
        "自动解除把止损通道失效一起清掉了——它比回撤更危险")
    ok, why = g.can_open(t + int(31 * DAY))
    assert ok is False, "止损通道还坏着却放行开仓"
    assert u"止损通道失效" in why


def test_a_manual_reset_restarts_the_cooldown_clock():
    """★ 人工复位必须把冷却期时钟清零。

    变异「reset 不清 halt_ts」第一轮 SURVIVED —— 这条测试就是为它写的。

    不清会怎样：假设 2020-01 停过一次，人工复位后 2026 年又停一次。
    新硬停的 halt_ts 会被写成 2026 的时刻（_halt_hard 每次都会覆盖），
    但 reset() 之后若残留旧值，且某条路径先读后写，
    冷却期就可能从很久以前算起 —— 刚停下就被判「已满 30 天」立刻放行。
    那等于这条闸在人工复位之后彻底失效。

    所以复位后必须能重新累积满一整个冷却期。
    """
    g = mk(30.0)
    t = drive_to_halt(g)
    # 人工复位（不是等到期）
    g.reset()
    assert g.st.halted_hard is False
    assert g.st.halt_ts == 0, "复位后还留着 halt_ts：%r" % (g.st.halt_ts,)
    assert g.st.halt_kind == "", "复位后还留着 halt_kind：%r" % (g.st.halt_kind,)

    # 复位之后重新触发一次硬停：必须重新走满 30 天，不能立刻解除
    t2 = t + int(1 * DAY)
    g.update(100.0, "WS", t2)
    g.update(30.0, "WS", t2 + 60000)
    assert g.st.halted_hard is True, "第二次没停住"
    ok, _ = tick(g, 30.0, t2 + 60000 + int(1 * DAY))
    assert ok is False, (
        "人工复位后新一轮硬停在 1 天就被自动放行了——冷却期没有重新起算")
    assert g.st.halted_hard is True
    ok2, _ = tick(g, 30.0, t2 + 60000 + int(31 * DAY))
    assert ok2 is True, "满 30 天却没有解除"


def test_a_zero_cooldown_means_manual_only():
    """冷却期 <= 0 = 关闭自动解除，可以回退到旧行为。"""
    g = mk(0.0)
    t = drive_to_halt(g)
    g.update(35.0, "WS", t + int(3650 * DAY))
    assert g.st.halted_hard is True, "cooldown=0 却自动解除了"


def test_the_release_is_recorded_where_a_human_would_look():
    """解除必须留痕：history 里一条 + 通过 _event 发出去。

    "自己悄悄恢复了"和"一直停着"一样糟：事后没人能解释
    为什么账户在某个时点又开始交易了。
    """
    events = []
    g = mk(30.0)
    g._event = lambda kind, detail, alert=False, title=None: events.append((kind, detail))
    t = drive_to_halt(g)
    g.update(35.0, "WS", t + int(31 * DAY))
    kinds = [k for k, _ in events]
    assert "guard_auto_release" in kinds, (
        "自动解除没有留下任何事件：%r" % (kinds,))
    assert any(h.get("what") == "auto_release" for h in g.st.history), (
        "history 里没有 auto_release 记录")


def test_the_snapshot_says_whether_it_will_release_itself():
    """看板要能区分「等 30 天自己好」和「必须人来处理」。

    这两种情况的应对方式完全相反：一个什么都不用做，一个必须立刻查账。
    只显示"硬停"两个字，用户无从判断。
    """
    g = mk(30.0)
    t = drive_to_halt(g)
    g.update(35.0, "WS", t + int(1 * DAY))
    s = g.snapshot()
    assert s["halted_hard"] is True
    assert s["halt_kind"] == "drawdown"
    assert s["release_in_days"] is not None, "回撤硬停没说还要等几天"
    assert 28.0 <= s["release_in_days"] <= 30.0, (
        "剩余天数不对：%r" % (s["release_in_days"],))

    g2 = mk(30.0)
    g2.update(100.0, "WS", T0)
    g2.position_mismatch(u"账实不符")
    s2 = g2.snapshot()
    assert s2["halt_kind"] == "mismatch"
    assert s2["release_in_days"] is None, (
        "账实不符报了自动解除倒计时，会让人以为等着就好")