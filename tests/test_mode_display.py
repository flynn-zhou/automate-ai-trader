# -*- coding: utf-8 -*-
"""下单模式的显示防线。

为什么要单独锁这个：paper 和 live 只差一个环境变量，
页面上如果显示得含糊，最危险的情况是「你以为在下真单，其实在记账」，
或者反过来「你以为在模拟，其实动了真钱」。所以模式必须显眼且不可含糊。
"""
from __future__ import annotations

import io
import os

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "ai_trader")


def test_home_shows_mode_explicitly():
    src = io.open(os.path.join(APP, "web", "templates", "dash_home.html"),
                  encoding="utf-8").read()
    assert "下单模式" in src, "总览没有显示下单模式"
    assert "实盘 live" in src and "纸面 paper" in src, \
        "模式文案含糊，必须明确写出 live / paper"
    assert "shop_mode == 'live'" in src, "实盘时要高亮区分"


def test_home_has_single_price_source():
    """总览只能有一个现价来源。

    页面上出现过两个价格：一格是服务端渲染时的快照（不动），
    一格是图表标题里每秒刷新的实时价。同页两个价格且差很多，
    用户会以为"数据不更新了"。
    """
    src = io.open(os.path.join(APP, "web", "templates", "dash_home.html"),
                  encoding="utf-8").read()
    assert 'id="tile-price"' not in src, \
        "总览又出现了第二个价格来源（tile-price）"
    assert 'id="last-price"' in src, "缺实时价"


def test_live_mode_requires_credentials():
    """实盘模式缺凭证必须拒绝启动，不能静默降级成纸面。"""
    src = io.open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    assert "缺少实盘凭证" in src or "拒绝启动" in src, \
        "live 模式下缺凭证没有拒绝启动"
