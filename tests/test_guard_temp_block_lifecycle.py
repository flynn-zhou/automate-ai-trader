# -*- coding: utf-8 -*-
"""Guard 的「临时停开仓」必须**每条闸都有出路**。

为什么单独写这个文件（第 114 轮）：
  guard.py 顶部把六条闸一条条列了出来，第 4 条写着：

      4. 当日亏损 >= 50%  -> 当日停开仓，**次日自动解除**

  实测：它没有解除路径。

      g.update(100.0, 'WS', T0)              # 当日基准 100
      g.update(48.0,  'WS', T0+3000)         # 当日 -52%
      -> (False, '当日亏损 -52.0% 已达上限')

      然后按生产节奏（每 3 秒一拍）一路推到第二天：
      -> (False, '当日亏损 -52.0% 已达上限，今日停止开新仓')
         day = '2026-01-02'  day_start = 48.0      # 新的一天已经开始了

  也就是说：**一旦某天亏到 50%，系统就永久不再开仓**，
  直到有人手工 /api/guard/reset。而 7x24 无人值守恰恰是这套系统的前提。

  结构上的原因很直白——temp_block 有三个写入点（数据源 / 引擎 / 日亏损），
  但只有前两个配了清除分支：

      180| 数据源 DOWN ...          <- 有清除：185 (elif temp_block.startswith("数据源"))
      204| stall                   <- 有清除：206 (elif temp_block.startswith("引擎"))
      212| 当日亏损 ...             <- ❌ 没有任何清除分支

  「三缺一」是最难自查的一类缺陷：前两条都写对了，
  第三条漏掉分支，代码读起来完全对称。

本文件钉四件事：
  1. 日亏损触发后【次日自动解除】——文档承诺的那一条；
  2. 但【当天必须一直停】，不能因为权益反弹就当天接着赌；
  3. 解除时要留下 guard_temp 事件（不然没人知道它解除了）；
  4. 每条闸解除时都不许顺手清掉别的闸。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.guard import Guard

DAY = 86400000
# 2026-01-01 01:00 UTC。挑一个确定的日内时刻，跨过它才算「第二天」。
T0 = 1767225600000 + 3600000


class _Cfg:
    pass


class _Journal:
    def __init__(self):
        self.rows = []

    def add(self, *a, **k):
        self.rows.append((a, k))


def _g():
    return Guard(_Cfg())


def _advance(g, start_ms, end_ms, equity, src="WS", step_ms=3000):
    """按生产节奏（默认 3 秒一拍）把时间推过去。

    必须按节奏推进而不是一步跳过去：一步跳 24 小时会先把
    「引擎卡住」那条闸撞出来（第 113 轮起它是真的会触发的），
    于是测的就不是日亏损这条闸了。
    """
    t = start_ms
    while t < end_ms:
        t += step_ms
        g.update(equity, src, t)
    return t


def test_daily_loss_block_clears_on_the_next_day():
    """★ 文档承诺「次日自动解除」。修之前：永久停摆。"""
    g = _g()
    g.update(100.0, "WS", T0)
    g.update(48.0, "WS", T0 + 3000)
    ok, why = g.can_open(now_ms=T0 + 3000)
    assert not ok and "当日亏损" in why, u"构造前提：应先被日亏损拦住，实际 %r" % (why,)

    t = _advance(g, T0 + 3000, T0 + DAY + 6000, 48.0)
    ok, why = g.can_open(now_ms=t)
    assert ok, (u"跨日之后仍然停在「%s」——它没有任何解除路径，"
                u"实际效果是「亏到 50%% 就永久停摆，等人工复位」" % (why,))


def test_daily_loss_block_persists_for_the_whole_day():
    """反面：当天即使在 -50% 附近反弹，也不能当天接着赌。

    文档写的是「当日停开仓」，不是「当前亏损超过 50% 时停」。
    这两条必须一起测——只测「次日解除」的话，
    一个「永远不拦」的实现也能过。
    """
    g = _g()
    g.update(100.0, "WS", T0)
    g.update(48.0, "WS", T0 + 3000)
    assert not g.can_open(now_ms=T0 + 3000)[0]
    # 当天晚些时候权益回到 60（当日 -40%，已经不到 50% 了）
    t = _advance(g, T0 + 3000, T0 + 12 * 3600000, 60.0)
    ok, why = g.can_open(now_ms=t)
    assert not ok, u"当天反弹到 -40%% 就放行了，但文档写的是「当日停开仓」：%r" % (why,)
    assert "当日亏损" in why


def test_a_recovered_feed_does_not_clear_the_daily_halt():
    """数据源恢复只能清数据源那条，不能顺手清掉日亏损。

    temp_block 是【一个字符串】三条闸共用，清错就会静默放行。
    """
    g = _g()
    g.update(100.0, "WS", T0)
    g.update(48.0, "WS", T0 + 3000)
    _advance(g, T0 + 3000, T0 + 40000, 48.0, src="DOWN")   # 数据源也 DOWN 一段
    t = _advance(g, T0 + 40000, T0 + 46000, 48.0, src="WS")  # 数据源恢复
    ok, why = g.can_open(now_ms=t)
    assert not ok and "当日亏损" in why, (
        u"数据源恢复把日亏损那条也清掉了，实际 %r" % (why,))


def test_clearing_the_daily_halt_is_announced():
    """解除必须留痕。不说的话，看板从「禁止开仓」跳回「一切正常」没有任何痕迹，
    事后复盘根本分不清是闸解除了还是监控瞎了。
    """
    j = _Journal()
    g = Guard(_Cfg(), journal=j)
    g.update(100.0, "WS", T0)
    g.update(48.0, "WS", T0 + 3000)
    events_blocked = len(g.st.history)
    t = _advance(g, T0 + 3000, T0 + DAY + 6000, 48.0)
    ok, why = g.can_open(now_ms=t)
    assert ok, u"构造前提：这一拍应当已经解除（%r）" % (why,)
    assert len(g.st.history) > events_blocked or j.rows, (
        u"日亏损解除没有留下任何事件记录")


def test_every_temp_block_has_a_way_out():
    """结构约束：每条会挡住开仓的闸，都必须有清除路径。

    ★ 这条测试在第 117 轮【改写过】，值得记一下为什么。

    原本它盯的是「每条闸都要有 startswith("<前缀>") 的清除分支」——
    那是当时"四条闸共用 temp_block 一个字符串"这个设计的产物。
    第 117 轮把存储换成了 temp_reasons（每条闸一格）之后，
    这条断言必然失败。

    失败之后要分清两种可能：
      (a) 我把好设计改坏了 -> 应该回退；
      (b) 测试钉住的是【旧机制】，新机制用另一种方式满足同一条不变量。
    这里是 (b)：不变量没变（每条闸都要能进来也能出去），
    但它现在由「写入和清除走同一个 _set_temp(key, ...)」保证，
    比 startswith 前缀匹配更硬——前缀匹配要求两处字面量保持一致，
    而这正是一个月内已经踩过两次的坑（第 114 轮、第 116 轮）。

    所以这里改成钉【新机制】，而不是删掉这条测试。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ai_trader", "guard.py")
    src = open(p, encoding="utf-8").read()

    # 1) 每条闸都必须有自己的槽位名，并且都在展示优先级表里
    assert "TEMP_ORDER" in src, u"没有 TEMP_ORDER —— 显示哪条闸取决于写入顺序"
    for key in ("feed_down", "stall", "daily_loss", "close_fail"):
        assert '"%s"' % key in src, u"闸「%s」没有自己的槽位" % key
        assert '"%s"' % key in src.split("TEMP_ORDER")[1][:200], (
            u"闸「%s」不在 TEMP_ORDER 里，看板上显示不到它" % key)

    # 2) 清除只能走 _set_temp(key, "")，不能再有"直接给 temp_block 赋空"，
    #    那种写法就是写/清各用一套字面量的老坑。
    assert "def _set_temp" in src and "def _refresh_temp_block" in src
    bad = [ln.strip() for ln in src.splitlines()
           if ln.strip().startswith("self.st.temp_block = ")
           and ln.strip().split("=", 1)[1].strip() not in ('why', '""')]
    assert not bad, (u"还有绕过 _set_temp 直接写 temp_block 的地方：%r —— "
                    u"temp_block 只能是 _refresh_temp_block() 派生出来的展示值" % (bad,))
    # 3) startswith 前缀匹配的清除分支【必须已经消失】——
    #    它正是"两处字面量必须一致"这个脆弱假设的来源。
    startswith_clears = [ln for ln in src.splitlines()
                         if ".startswith(" in ln and "temp_block" in ln]
    assert not startswith_clears, (
        u"仍然在用 startswith 前缀匹配来清除闸：%r —— "
        u"这种写法要求写入方和清除方两处字面量完全一致，"
        u"第 114 轮和第 116 轮各踩过一次" % (startswith_clears,))
