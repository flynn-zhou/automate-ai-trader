# -*- coding: utf-8 -*-
"""因果链上「为什么不开仓」的答案，有两个根本没上屏。

第 120 轮，接着上一轮的探针往下做：这次查的是 `_build_trend_snapshot`
（趋势快照，18 个字段，整个因果链最集中的地方）。

结果有 5 个前端从不读取，其中两个是【作者自己写明要让它可见】的：

1. `plan_blocked`

   engine.py:966-970 —— 计划不可行时 `plan_notional` / `plan_qty` 都置 0，
   原因塞进 `plan_blocked`。而 board.js 第 298 行是这样的：

       '届时仓位：' + target_leverage + 'x 名义 ' + plan_notional + 'U / qty ' + plan_qty

   于是屏幕上显示「届时仓位：1.0x 名义 0U / qty 0」，
   **为什么是 0，一个字都没有。**

   而这正是用户问得最多的一句：「为什么不开仓？」

2. `bar_src` / `bar_vol` / `bar_vol_local`

   engine.py:880-883 的注释原话：

       最近一根 4h K 是从哪来的、量是多少。
       收盘价两种口径一致，所以 SMA 区分不出用的是官方还是本地；
       只有成交量能区分（官方 8558 vs 本地 117）。不报出来，
       「回退到本地 bar」在线上就完全不可见。

   **注释写明了「不报出来就不可见」，字段也确实加进快照了 ——
   然后前端从来没读过它。** 这不是假设，是写代码的人自己定下的要求，
   只完成了一半。

   回退到本地 bar 意味着 SMA200 里有一根是本地拼出来的、量不对，
   均线本身可能因此偏移。用户看到的所有判断都建立在它上面。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_board_renders_for_real as H


def _st(**kw):
    st = H._base_state()
    st["trend"].update(kw)
    return st


def test_an_infeasible_plan_says_why():
    """★ 计划不可行时，必须把原因说出来，不能只显示 0。

    100 USDT 的账户在现货上本来就贴着最小下单额的门槛走，
    「名义 0U / qty 0」是会真的出现的一种状态。
    只显示 0 等于让用户自己猜。
    """
    reason = u"资金不足：可用 4.21U，最少需要 5.50U（含最小下单额 5U）"
    blob = " ".join(H._render_with_errors(_st(
        plan_notional=0.0, plan_qty=0.0, plan_blocked=reason))["seen"])
    assert reason in blob, (
        u"计划不可行，但屏幕上一个字都没说为什么：%s" % blob[:700])


def test_a_feasible_plan_does_not_show_a_block_reason():
    """反向约束：计划正常时不能凭空多出一句「不可行」。

    只测「有问题要显示」，一个「永远显示计划不可行」的实现也能过 ——
    而那会让用户以为系统永远开不了仓。
    """
    blob = " ".join(H._render_with_errors(_st(
        plan_notional=71.0, plan_qty=0.00091))["seen"])
    # ★ 断言必须落在【真实渲染出来的那句话】上。
    #
    # 第一版写的是 assert u"不可行" not in blob —— 而那句提示实际是
    # 「届时不会开仓：…」，「不可行」三个字在【两种情况下都不出现】。
    # 这条测试因此永远为真，一个「永远显示不会开仓」的实现照样绿
    # （变异「可行时也说不可行」SURVIVED 就是这么来的）。
    #
    # 现在两头都钉：不能出现那句警告，而且必须真的显示了仓位数字。
    assert u"届时不会开仓" not in blob, (
        u"计划是可行的，却显示了「不会开仓」：%s" % blob[:700])
    assert u"届时仓位" in blob, (
        u"计划可行时没有显示届时仓位：%s" % blob[:700])
    assert u"71" in blob, u"显示的却不是 71U：%s" % blob[:700]


def test_a_local_bar_fallback_is_visible():
    """★ 回退到本地 bar 必须在屏幕上看得见。

    engine.py 的注释自己写着「不报出来，回退到本地 bar 在线上就完全不可见」。
    这正是那句话要防的事。
    """
    blob = " ".join(H._render_with_errors(_st(
        bar_src=u"local(回退)", bar_vol=8558.0, bar_vol_local=117.0))["seen"])
    assert u"本地" in blob, (
        u"SMA 用的是本地拼的 4h K，屏幕上完全没提：%s" % blob[:700])
    # 光说「本地」不够：用户需要看见【两个成交量差多少】才能判断这根 bar
    # 有多不可信。官方口径 8558 vs 本地 117 是 73 倍，
    # 而屏幕上只写一句「回退到本地」，用户无从知道差别有多大。
    # 变异「回退提示不报成交量」第一轮 SURVIVED，就是因为只断了「本地」。
    assert u"117" in blob, u"没报本地成交量：%s" % blob[:700]
    assert u"8558" in blob or u"8,558" in blob, (
        u"没报官方口径成交量，用户无从判断差多少：%s" % blob[:700])


def test_an_official_bar_does_not_cry_wolf():
    """反向约束：官方 bar 正常时不能报警。

    只有「回退」才值得提醒。官方 bar 也提醒的话，
    这个提示会常驻，用户就学会无视它了。
    """
    blob = " ".join(H._render_with_errors(_st(
        bar_src=u"official", bar_vol=8558.0, bar_vol_local=117.0))["seen"])
    assert u"回退" not in blob, (
        u"用的是官方 bar，却提示了回退：%s" % blob[:700])


def test_the_block_reason_is_escaped():
    """★ 补的：不可行原因必须转义后再写进 HTML。

    `plan_blocked` 有两个来源，其中一个直接来自异常消息：

        snap["plan_blocked"] = "计划计算失败：%s" % e

    异常消息里出现 `<` `>` `&` 是完全可能的（比如某个 <module> 名），
    不转义就会破坏网页结构，甚至注入标签。

    变异「不可行原因不转义」第一轮 SURVIVED —— 因为我用的测试原因是
    纯中文，没有任何需要转义的字符。**测试数据又一次把问题遮住了。**
    """
    reason = (u"计划计算失败：" + u"<module> " + chr(39) + "binance" + chr(39) +
              u" has no &amp;")
    blob = " ".join(H._render_with_errors(_st(
        plan_notional=0.0, plan_qty=0.0, plan_blocked=reason))["seen"])
    assert u"<module>" not in blob, (
        u"原因里的 <module> 原样写进了 HTML，会破坏页面结构：%s" % blob[:700])
    assert u"&lt;module&gt;" in blob, (
        u"没有转义（应为 &lt;module&gt;）：%s" % blob[:700])
