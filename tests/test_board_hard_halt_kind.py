# -*- coding: utf-8 -*-
"""看板必须说清「硬停会不会自己好」（第 122 轮）。

背景
----
第 122 轮给回撤硬停加了自动解除通道，于是 hard halt 分成了两种：

    drawdown  回撤熔断  -> 冷却期（30 天）到了自己解除
    mismatch  账实不符  -> 永远不会自己解除，必须人去看账

而 board.js 当时写的是：

    <b>硬停：已停止开仓，且不会自动恢复</b>

这在加了自动解除之后【就是错的】：用户会以为要永远等下去，
跑去手工复位一个本来自己会好的状态。

反过来更糟：如果账实不符也显示"还有 N 天自动解除"，
用户会以为"等着就好"，而实际必须立刻查账。

两种错误的处置方式完全相反，所以屏幕必须区分。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_board_renders_for_real import _render_with_errors, _base_state


def _blob(**over):
    st = _base_state()
    st["guard"].update(over)
    return " ".join(_render_with_errors(st)["seen"])


def test_a_drawdown_halt_says_it_will_release_itself():
    """★ 回撤硬停必须说"还会自动解除 + 还剩几天"。

    第 122 轮之前的文案是「且不会自动恢复」，加了冷却期之后这句就是错的：
    用户会跑去手工复位一个本来自己会好的状态。
    """
    blob = _blob(can_open=False, halted_hard=True, halt_kind="drawdown",
                 halt_reason=u"从峰值 305.32U 回撤 61.2%（>60%）",
                 release_in_days=17.5)
    assert u"硬停" in blob
    assert u"不会自动恢复" not in blob, (
        u"回撤硬停明明会自动解除，屏幕还说不会：%s" % blob[:900])
    assert u"自动解除" in blob, u"没说会自己解除：%s" % blob[:900]
    assert u"17.5" in blob, u"没报剩余天数：%s" % blob[:900]
    # 「不用人工干预」这句话是真正的用户指令：它阻止用户跑去乱按复位。
    # 少了它，"还有 17.5 天自动解除"容易被读成"17.5 天后需要你做什么"。
    assert u"不用人工干预" in blob, (
        u"没告诉用户这条什么都不用做：%s" % blob[:900])


def test_a_mismatch_halt_says_it_needs_a_human():
    """★ 反向：账实不符必须说"要人看"，且不能给倒计时。

    这条更重要。如果账实不符也显示"还有 N 天自动解除"，
    用户会以为等着就好 —— 而实际上钱可能放错了地方，必须立刻查。
    """
    blob = _blob(can_open=False, halted_hard=True, halt_kind="mismatch",
                 halt_reason=u"账实不符：库里 0.001 BTC，交易所 0.002 BTC",
                 release_in_days=None)
    assert u"硬停" in blob
    assert u"账实不符" in blob
    assert u"自动解除" not in blob, (
        u"账实不符不是自动解除的，屏幕却在说自动解除：%s" % blob[:900])
    assert u"人工" in blob or u"复位" in blob, (
        u"没告诉用户这条必须人来处理：%s" % blob[:900])
    # ★ 光断「自动解除」不够 —— 变异「账实不符也报自动解除」第一轮 SURVIVED：
    #   把它当成 drawdown 时 release_in_days 仍是 None，代码走兜底分支，
    #   文案依旧是「不会自动恢复」，于是"没有自动解除"这条断言照样成立。
    #   真正能区分两者的，是【回撤那条专用措辞】有没有串到账实不符上。
    assert u"回撤熔断" not in blob, (
        u"账实不符用上了回撤硬停的措辞：%s" % blob[:900])
    assert u"不用人工干预" not in blob, (
        u"账实不符却告诉用户不用人工干预：%s" % blob[:900])
    # 「等不会好」是这条闸最关键的一句：用户最容易犯的错就是干等。
    assert u"等不会好" in blob, (
        u"没明确告诉用户干等没有用：%s" % blob[:900])