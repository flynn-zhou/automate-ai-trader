# -*- coding: utf-8 -*-
"""大屏在【持仓中】这一整天，从来没被渲染过一次。

为什么这是个真问题（第 119 轮）：
  趋势策略上线至今 0 笔交易 —— BTC 一直在 SMA200 下方。
  也就是说大屏的「持仓」分支（上一轮的 renderTrend 第 239 行）
  从来没有喂过任何真数据，一次都没有。

  而用户的诉求原话是「我要知道你在做什么」。
  最需要看见系统的时刻，恰恰是【有仓位、钱在场上】的时候。
  如果那一刻大屏抛异常或者显示空白，用户在最要命的时候是瞎的。

  这不是假想：上一轮真跑 JS 已经证明，光看源码判断「显示了没有」
  会错。所以持仓分支也只能真跑。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_board_renders_for_real as H


def _holding_state():
    st = H._base_state()
    t = st["trend"]
    t.update({
        "hold": True,
        "pos": {"side": "LONG", "qty": 0.00091, "entry": 78000.0,
                "stop": 61620.0, "target": 0.0},
        "pos_pnl_pct": 0.62,
        "hold_h": 3.4,
    })
    return st


def test_the_board_renders_a_live_position():
    """★ 持仓时必须画得出来，而且要画对。

    大屏从没跑过这条路径 —— 这是它第一次被执行。
    """
    out = H._render_with_errors(_holding_state())
    assert not out["errors"], (
        u"持仓状态下板子渲染抛异常了：%r —— 有仓位时反而是瞎的" % (out["errors"],))
    blob = " ".join(out["seen"])
    assert u"持仓" in blob, u"持仓状态没有显示出来：%s" % blob[:600]


def test_a_position_shows_its_stop_and_pnl():
    """止损价和浮盈浮亏必须在屏幕上。

    止损价是这道仓位唯一的硬保护。用户看不见它，就没法判断
    「这个仓到底有没有在受保护」。
    """
    out = H._render_with_errors(_holding_state())
    blob = " ".join(out["seen"])
    assert u"61620" in blob.replace(",", ""), (
        u"止损价没显示：%s" % blob[:600])
    assert (u"+0.62" in blob) or (u"0.62%" in blob), (
        u"浮动盈亏没显示：%s" % blob[:600])

    # ★ 数量/开仓价/盈亏这三项必须【限定在持仓那一句里】查。
    #
    # 我已经在同一个坑里掉了三次：断言 u"78000" in blob，
    # 而页面别处本来就有「现价 78000.0」——
    # 把持仓那一步的开仓价删掉，测试照样绿（SURVIVED）。
    # 只要断言的是「整页包含某个字符串」，页面任意一处出现它就会顶替。
    # 所以这里先定位到 "持仓：" 开头的这一段，再在这段里查。
    idx = blob.find(u"持仓")
    assert idx >= 0, u"没有持仓那一句：%s" % blob[:600]
    seg = blob[idx: idx + 200]
    assert u"78000" in seg.replace(",", ""), (
        u"持仓那一句里没有开仓价（用户不知道 +0.62%% 是从哪个价算起的）：%s" % seg)
    assert u"0.0009" in seg, (
        u"持仓那一句里没有数量（用户不知道仓位多大）：%s" % seg)


def test_a_losing_position_shows_a_minus_sign():
    """反向：亏损时必须是负号，不能只看绝对值。

    只测盈利方向的话，一个「永远显示正数」的实现也能过，
    而那正好是最糟的：亏钱显示赚钱。
    """
    st = _holding_state()
    st["trend"]["pos_pnl_pct"] = -3.7
    blob = " ".join(H._render_with_errors(st)["seen"])
    assert u"-3.70" in blob or u"-3.7" in blob, (
        u"亏损没有显示负号：%s" % blob[:600])


def test_the_board_does_not_crash_on_a_half_filled_position():
    """真实事故的形状：pos 有、但某个字段是 None。

    持仓来自数据库，重启恢复时字段可能是 NULL（第 91 轮真踩过：
    stop_price 为 NULL，止损静默消失）。前端也要扛得住这种情况，
    不能因为一个 None 就整块空白。
    """
    st = _holding_state()
    st["trend"]["pos"]["stop"] = None
    st["trend"]["pos_pnl_pct"] = None
    out = H._render_with_errors(st)
    assert not out["errors"], (
        u"字段是 None 时板子抛异常了：%r" % (out["errors"],))
    assert out["seen"], u"字段是 None 时板子整块空白"


def test_a_loss_shows_exactly_one_minus_sign():
    """★ 补的：亏损不能显示成「+-3.70%」。

    变异「浮亏也强制加正号」第一轮 SURVIVED（4 passed）——
    因为我只断言了 "-3.70" in blob，而 "+-3.70" 里【也含】"-3.70"。
    「包含」这种断言对"多打了一个前缀"完全不敏感。

    这是符号拼接这一类 bug 的共同形状：
    条件表达式写成无条件时，亏损会变成 "+-3.70"，
    人一眼看过去先看到加号，会误以为在赚钱。
    """
    st = _holding_state()
    st["trend"]["pos_pnl_pct"] = -3.7
    blob = " ".join(H._render_with_errors(st)["seen"])
    assert "+-" not in blob, u"亏损显示成了「+-3.70%」这种形状：%s" % blob[:600]
    # 盈利方向也要钉：不能靠"永远不加号"来躲开上面那条
    st2 = _holding_state()
    st2["trend"]["pos_pnl_pct"] = 3.7
    blob2 = " ".join(H._render_with_errors(st2)["seen"])
    assert "+3.70" in blob2, u"盈利没有正号：%s" % blob2[:600]


def test_a_position_tells_you_what_will_close_it():
    """★ 补的：持仓时必须说出【什么条件下会出场】。

    变异「持仓时不说出场条件」第一轮 SURVIVED——我只测了止损价，没测出场条件。

    但用户的原话是「我要知道你在做什么」。有仓位的时候，"我在做什么"
    不止是"我拿着一个仓"，还包括"我打算什么时候放下它"。
    只显示浮盈浮亏，用户只能看见结果，看不见计划。
    """
    blob = " ".join(H._render_with_errors(_holding_state())["seen"])
    assert u"出场" in blob, u"持仓时没有说什么时候出场：%s" % blob[:600]
    # ★ 断言必须【限定在这一句上】。
    #
    # 第一版写的是 u"200" in blob，而页面别处本来就有「SMA200 80000.0」，
    # 于是把「出场条件：4h 收盘跌破 SMA」里的 4h 删掉，测试照样绿（SURVIVED）。
    # 和上一轮同一个坑：断言被【页面上另一段文字】满足了。
    idx = blob.find(u"出场")
    seg = blob[idx: idx + 200]
    assert u"200" in seg, (
        u"出场条件没带出 SMA 周期（SMA200），用户无法核对：%s" % seg)
    assert u"4h" in seg, (
        u"出场条件没说是 4h 收盘 —— 用户会以为 3s 或 1m 收盘也算数：%s" % seg)


def test_a_position_says_how_long_it_has_been_held():
    """★ 补的：持仓要显示已经持有多久。

    变异「持仓时不显示持有小时」第一轮 SURVIVED——因为我没测这一项。

    趋势策略没有最长持有时间（`max_hold_bars` 设成 10 亿），
    所以「持有小时」是用户判断「这笔单处在什么阶段」的唯一线索。
    少了它，用户只知道自己有仓位，不知道这个仓位已经挂了三天。
    """
    st = _holding_state()
    st["trend"]["hold_h"] = 61.5
    blob = " ".join(H._render_with_errors(st)["seen"])
    idx = blob.find(u"已持有")
    assert idx >= 0, u"没有显示「已持有」：%s" % blob[:600]
    seg = blob[idx: idx + 200]
    assert u"61.5" in seg, u"持有小时没有显示出来（应为 61.5）：%s" % seg
