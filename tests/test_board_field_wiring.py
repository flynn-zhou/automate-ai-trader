# -*- coding: utf-8 -*-
"""后端算出来的状态，前端到底渲染了几个？

为什么单独写这个文件（第 118 轮）：
  上一轮把四条闸拆成了各自一格之后，立刻问一句：
  **那看板看得见吗？**

  实测（把 state()/guard_state()/executor_state() 里声明的字段逐个
  拿到 board.js 里去找有没有被读取）：

      === guard_state ===
         block_reason             渲染
         can_open                 渲染
         drawdown_pct             渲染
         equity                   渲染
         rules                    渲染
         halted_hard              ❌ 前端从不读取      <- 硬停！
         halt_reason              ❌ 前端从不读取      <- 硬停原因！
         temp_block               ❌ 前端从不读取      <- 临时闸原文
         peak_equity              ❌ 前端从不读取
         day_start_equity         ❌ 前端从不读取
         note                     ❌ 前端从不读取

      === executor_state ===
         halted                   渲染
         reason                   渲染
         name                     渲染
         close_fail_count         ❌ 前端从不读取      <- 第 116 轮刚加的！

  也就是说：**第 116 轮说「上大屏」，第 117 轮说「各自一格」，
  两次都只做到后端。前端从来没读过这些字段。**

  这不是「少写了一个 if」。这是【承诺了却没兑现】：
  代码里写着「必须上大屏」「看板要显示」，而看板上一片空白。
  用户的原话是「我要知道你在做什么」，这句话在 guard_state 的
  docstring 里就引着，但 halted_hard 从来没出现在屏幕上。

本文件钉四件事：
  1. 每条会拦住开仓的闸，都必须有对应的前端读取（不是后端字段存在）；
  2. 硬停必须有独立展示，不能只靠 block_reason 那一句话；
  3. 前向约束：新增的闸字段必须在 board.js 里出现，否则算没接完；
  4. 反向约束：值健康时不能乱报警（否则用户学会无视警报）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD_JS = os.path.join(ROOT, "ai_trader", "web", "static", "board.js")


def _js():
    with open(BOARD_JS, encoding="utf-8") as f:
        return f.read()


def _reads(js, field):
    """前端是否【读取】了这个字段（属性访问或字面量取值）。"""
    if re.search(r"[.\[]" + re.escape(field) + r"\b", js):
        return True
    return ('"%s"' % field) in js or ("'%s'" % field) in js


#: 会拦住开仓的闸，对应的后端字段 -> 前端必须读它。
#:
#: 这张表是【前向约束】：以后再加一条闸，把字段加进来，
#: 忘了改前端就会红。比"记得去看一眼 board.js"可靠。
GATE_FIELDS = [
    ("guard", "can_open"),          # 能不能开
    ("guard", "block_reason"),      # 为什么不能开
    ("guard", "halted_hard"),       # 硬停（要人复位）
    ("guard", "halt_reason"),       # 硬停原因
    ("guard", "temp_block"),        # 临时闸原文（第 117 轮拆格后的合成值）
    ("executor", "halted"),         # 执行器熔断
    ("executor", "close_fail_count"),   # 止损失效（第 116 轮加的后端字段）
]


def test_board_reads_every_gate_field_the_backend_exposes():
    """★ 核心：后端暴露的每一条闸，前端都要真的读。

    修之前 halted_hard / halt_reason / temp_block / close_fail_count
    四项全部「前端从不读取」。
    """
    js = _js()
    missing = ["%s.%s" % (grp, f) for grp, f in GATE_FIELDS if not _reads(js, f)]
    assert not missing, (
        u"后端暴露了、前端从来不读的闸字段：%r —— "
        u"这些状态在屏幕上等于不存在" % (missing,))


def test_hard_halt_has_its_own_visible_step():
    """硬停必须有独立的一步，不能只靠 block_reason 那句话。

    区别在处置方式：
      临时闸 -> 等它自己恢复
      硬停   -> 必须人工复位（/api/guard/reset）
    混成一句话，用户会一直等一个不会自己好的状态。
    """
    js = _js()
    assert "halted_hard" in js, u"board.js 里完全没有 halted_hard"
    # 出现 halted_hard 的地方附近必须带"人工/复位/reset"这类提示
    for m in re.finditer(r"halted_hard", js):
        ctx = js[max(0, m.start() - 400): m.start() + 700]
        if (u"人工" in ctx) or (u"复位" in ctx) or ("reset" in ctx):
            return
    pytest.fail(u"halted_hard 出现了，但没有任何一处告诉用户"
                u"「这是硬停、要人工复位」——用户会一直等它自己好")


def test_close_fail_count_is_rendered_when_nonzero():
    """止损失效次数必须显示出来（第 116 轮的承诺）。

    只在 >0 时显示：正常运行时挂一个"平仓失败 0 次"是噪音，
    噪音会让人学会无视这一块，等于没有这块。
    """
    js = _js()
    assert "close_fail_count" in js, u"board.js 从不读 close_fail_count"
    # 必须有 >0 的条件判断（否则就是一直显示，变成噪音）
    ctx = js[js.find("close_fail_count") - 300: js.find("close_fail_count") + 300]
    assert (">0" in ctx.replace(" ", "")) or ("!==0" in ctx.replace(" ", "")), (
        u"close_fail_count 没有按「>0 才显示」处理，会变成常驻噪音")


def test_temp_block_is_shown_verbatim_not_reinterpreted():
    """临时闸显示的是后端原话，前端不能自己编一句。

    第 117 轮把四条闸拆格之后，优先级由 Guard 的 TEMP_ORDER 定。
    前端如果再按自己的顺序拼一遍，就会出现「后端说 A、屏幕说 B」。
    """
    js = _js()
    assert "temp_block" in js, u"board.js 从不读 temp_block"
    # 前端不能自己拼闸的名字（出现哪条闸的文案就是越权）
    for word in (u"止损通道失效", u"数据源 DOWN 已", u"当日亏损"):
        assert word not in js, (
            u"board.js 里出现了「%s」——这是后端的闸文案，"
            u"前端自己拼一套就会出现前后端各说各的" % word)

# 注：「健康状态不能出警报」这条【不在这里测】。
# 源码正则读不出"这一行在不在某个分支里"——我第一版就在这里误判过：
#     html: (g.temp_block || g.block_reason || "原因未提供")
# 明明整行在 else if (guardBlocked) 里面，正则看不出来。
# 那条不变量搬到了 test_board_renders_for_real.py：那里把真的 board.js
# 拉起来执行，看 DOM 里最后出现的是哪些字。本文件只保留"字段有没有被接上"
# 这种结构层面的事实。
