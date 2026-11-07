# -*- coding: utf-8 -*-
"""四条闸抢着写同一格 temp_block —— 谁最后写谁赢，而且互相擦除。

为什么单独写这个文件（第 117 轮）：
  上一轮给「止损失效」接了一条新闸。装上之后立刻问一句：
  **它会不会被别的闸擦掉？**

  temp_block 的设计是【一个字符串，四条闸共用】：
      数据源 DOWN  -> 写 "数据源 DOWN 已 Ns…"    清除：startswith("数据源")
      引擎卡住      -> 写 "引擎已 Ns 无新决策…"    清除：startswith("引擎")
      当日亏损      -> 写 "当日亏损 -X%…"         清除：跨日
      止损失效      -> 写 "止损通道失效：…"        清除：计数归零

  每条闸都只擦【自己那一句】，看起来互不干扰。但它们写的是同一个变量。

  实测（第 116 轮刚加的闸，第 117 轮就漏了）：

      g.close_fail_streak_changed(6)
      -> (False, '止损通道失效：平仓已连续失败 6 次，仓位保护不住，禁止开新仓')

      # 数据源抖 33 秒（生产里很常见），然后恢复
      -> (False, '数据源 DOWN 已 33s，禁止开仓')
      -> (True,  '')                                    ← 闸没了
         temp_block = ''
         close_fail_streak = 6                          ← 失效条件还在

  也就是说：**止损失效这个事实还在（连错 6 次），但没有任何东西拦着了。**
  而且它不是短暂的——止损失效只在 _close 真正发单时才回写，
  而 _close 有 3~300 秒的退避；退避期间如果数据源抖一下，
  闸就一直是空的，直到下一次发单（最多 300 秒后）。

  更严重的一条路径：仓位被 reconcile_live 接管平掉（不走 _close），
  计数永远停在 6，闸永远回不来。

  为什么另外三条闸没暴露这个问题：它们在 update() 里【每拍重新求值】，
  错误会被下一拍自愈。只有止损失效是"推"进来的状态，丢了就丢了。

本文件钉四件事：
  1. 数据源抖一下不能擦掉止损失效；
  2. 同时有两条闸时，两条都要被记住（不能只留最后写的那条）；
  3. 解除一条闸不能顺带解除另一条；
  4. 结构约束：每条闸都必须在 temp_reasons 里有自己的格子。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.guard import Guard

T0 = 1767225600000 + 3600000   # 2026-01-01 01:00 UTC
DAY = 86400000


class _Cfg:
    pass


def _g():
    return Guard(_Cfg())


def _advance(g, start_ms, end_ms, equity=100.0, src="WS", step_ms=3000):
    """按生产节奏（3 秒一拍）推进。一步跳过去会先把「引擎卡住」撞出来。"""
    t = start_ms
    while t < end_ms:
        t += step_ms
        g.update(equity, src, t)
    return t


def test_a_feed_blip_does_not_erase_the_broken_stop_gate():
    """★ 核心缺陷：数据源抖一下，止损失效就被擦掉了。

    修之前：
        close_fail_streak_changed(6) -> 拦住
        数据源 DOWN 33 秒 + 恢复      -> (True, '')，而计数还是 6
    """
    g = _g()
    g.update(100.0, "WS", T0)
    g.close_fail_streak_changed(6)
    assert not g.can_open(now_ms=T0)[0], "构造前提：止损失效应先拦住"

    t = _advance(g, T0, T0 + 33000, src="DOWN")     # 数据源抖 33 秒
    t = _advance(g, t, t + 6000, src="WS")          # 恢复

    ok, why = g.can_open(now_ms=t)
    assert not ok, (u"数据源恢复后闸被清空了，但 close_fail_streak 仍然是 %d ——"
                    u"止损通道还坏着，系统却放行开仓"
                    % g.st.close_fail_streak)
    assert u"止损" in why, u"恢复后应该回到「止损通道失效」，实际 %r" % (why,)


def test_both_gates_are_remembered_at_the_same_time():
    """两条闸同时成立时，两条都必须被记住。

    temp_block 只有一个字符串，只能显示一条；但**存的不能只有一条**，
    否则显示哪条取决于写入顺序，另一条就永远查不到了。
    """
    g = _g()
    g.update(100.0, "WS", T0)
    g.close_fail_streak_changed(6)
    _advance(g, T0, T0 + 33000, src="DOWN")
    reasons = dict(getattr(g.st, "temp_reasons", {}) or {})
    assert len(reasons) >= 2, (
        u"只有 %r 被记住——两条闸同时成立时丢了一条" % (reasons,))
    joined = " ".join(reasons.values())
    assert u"数据源" in joined and u"止损" in joined


def test_releasing_one_gate_leaves_the_other_one_blocking():
    """解除 A 不能顺带解除 B。

    这条和第 114 轮「数据源恢复不能清掉日亏损」是同一族，
    只是那次靠 switch 顺序碰巧对了，这次是止损失效被真的擦掉了。
    """
    g = _g()
    g.update(100.0, "WS", T0)
    g.close_fail_streak_changed(6)
    t = _advance(g, T0, T0 + 33000, src="DOWN")
    t = _advance(g, t, t + 6000, src="WS")
    assert not g.can_open(now_ms=t)[0], "数据源恢复后仍应被止损失效拦住"

    # 现在把止损通道修好（计数归零）
    g.close_fail_streak_changed(0)
    ok, why = g.can_open(now_ms=t)
    assert ok, u"止损失效已解除，却还被 %r 拦着" % (why,)


def test_the_daily_loss_gate_survives_a_feed_blip():
    """顺带钉死另一条：当日亏损不能被数据源抖动擦掉。

    它现在之所以没出事，只是因为 update() 每拍按权益重算了它。
    但一旦某天权益刚好卡在阈值上方一点点，就会变成真缺口。
    """
    g = _g()
    g.update(100.0, "WS", T0)
    _advance(g, T0, T0 + 3000, equity=48.0)
    assert not g.can_open(now_ms=T0 + 3000)[0], "构造前提：当日亏损应先拦住"

    t = _advance(g, T0 + 3000, T0 + 36000, equity=48.0, src="DOWN")
    t = _advance(g, t, t + 6000, equity=48.0, src="WS")
    ok, why = g.can_open(now_ms=t)
    assert not ok and u"当日亏损" in why, u"数据源恢复把当日亏损擦掉了：%r" % (why,)


def test_the_composite_block_is_deterministic():
    """显示哪一条必须是【定死的优先级】，不能取决于谁最后写。

    看板只有一格位置。如果今天显示「数据源 DOWN」、明天显示「止损失效」
    取决于调度顺序，用户就没法判断到底哪个是真问题。
    """
    g = _g()
    g.update(100.0, "WS", T0)
    g.close_fail_streak_changed(6)
    _advance(g, T0, T0 + 33000, src="DOWN")
    shown_a = g.st.temp_block

    g2 = _g()
    g2.update(100.0, "WS", T0)
    _advance(g2, T0, T0 + 33000, src="DOWN")
    g2.close_fail_streak_changed(6)
    shown_b = g2.st.temp_block

    assert shown_a == shown_b, (
        u"同样的两条闸，写入顺序不同就显示了不同的原因：%r vs %r"
        % (shown_a, shown_b))


def test_every_gate_owns_its_own_slot():
    """结构约束：每条闸在 temp_reasons 里必须有独立的一格。

    上面那些行为测试都能被"再写一个字符串拼接"骗过。
    这里要的是结构事实：四条闸不能共用一个存储位。
    """
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ai_trader", "guard.py"), encoding="utf-8").read()
    assert "temp_reasons" in src, (
        u"guard.py 里没有 temp_reasons——四条闸仍然共用 temp_block 一个字符串，"
        u"谁最后写谁赢，且互相擦除")
    for key in ("feed_down", "stall", "daily_loss", "close_fail"):
        assert '"%s"' % key in src or "'%s'" % key in src, (
            u"闸「%s」没有自己的槽位" % key)

def test_the_shown_gate_follows_a_fixed_severity_order():
    """★ 补上来的：优先级【本身】必须被钉住，不只是"顺序无关"。

    原来的 test_the_composite_block_is_deterministic 只验了「两种写入顺序
    显示同一条」——把 TEMP_ORDER 整个反转，它照样绿（11 passed）。
    确定性是必要条件，不是充分条件：一个稳定的错误顺序也是确定的。

    这里钉住具体内容：止损失效必须压过数据源抖动。
    理由是「这条闸背后有多少不确定性」——止损失效意味着系统可能
    已经在不受控地漏钱，而数据源抖一下只是暂时看不见。
    反过来显示「数据源 DOWN」会让人去查行情，而真正的问题在交易通道。
    """
    g = _g()
    g.update(100.0, "WS", T0)
    g.close_fail_streak_changed(6)
    _advance(g, T0, T0 + 33000, src="DOWN")
    shown = g.st.temp_block
    assert u"止损" in shown, (
        u"两条闸同时成立时显示的是「%s」，但止损失效更严重——"
        u"数据源抖动只是暂时看不见，止损失效是钱正在漏" % (shown,))
    # 两条都仍然被记住（显示优先级不影响存储）
    assert len(g.st.temp_reasons) == 2


def test_reset_clears_every_gate_slot():
    """reset 之后每条闸的格子都必须清空。

    只清 temp_block 的话，下一拍 _refresh_temp_block() 会从残留的
    temp_reasons 里把旧闸（比如已失效的止损失效）重新贴回来——
    人工复位失效，而且表现得像"复位了又立刻复发"。
    """
    g = _g()
    g.update(100.0, "WS", T0)
    g.close_fail_streak_changed(6)
    assert g.st.temp_reasons, "构造前提：应有闸在挂着"

    g.reset()
    assert not g.st.temp_reasons, (
        u"reset 之后 temp_reasons 里还留着 %r —— 下一拍会被重新贴回来"
        % (g.st.temp_reasons,))

    # 推进一拍：确认没有"复活"
    t = _advance(g, T0, T0 + 3000)
    ok, why = g.can_open(now_ms=t)
    assert ok, u"reset 之后闸又复活了：%r" % (why,)
