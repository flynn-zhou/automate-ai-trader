# -*- coding: utf-8 -*-
"""交接文档的回归防线。

新会话只读一份文档就要能接上。所以「指向哪一份」不能靠人记：
索引、指针、旧文档三者必须一致，否则新会话会读到过期的那份。
"""
from __future__ import annotations

import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _p(*parts):
    return os.path.join(ROOT, *parts)


def _read(*parts):
    return io.open(_p(*parts), encoding="utf-8").read()


def test_dsh_handoff_exists_and_is_substantial():
    src = _read("docs", "HANDOFF-DSH-SESSION.md")
    assert len(src.splitlines()) > 120, "交接文档太薄，接不上"
    for must in ("区间演化", "闸门", "部署", "禁止", "下一步"):
        assert must in src, "交接文档缺关键章节: " + must


def test_index_points_to_dsh_handoff():
    idx = _read("docs", "README.md")
    assert "docs/HANDOFF-DSH-SESSION.md" in idx, "索引没指向新交接文档"
    i = idx.index("## 交接入口")
    head = idx[i:i + 200]
    assert "HANDOFF-DSH-SESSION.md" in head, "入口段落没指向新文档"


def test_old_handoff_is_pointer_not_content():
    """旧交接文档必须变薄并指向新的，不能两份都自称主文档。"""
    src = _read("docs", "HANDOFF-NEXT-SESSION.md")
    assert len(src.splitlines()) < 40, "旧交接文档还很厚"
    assert "HANDOFF-DSH-SESSION.md" in src


def test_old_handoff_archived_with_warning():
    arch = _read("docs", "handoff", "r133-旧主线-唐奇安与B路径.md")
    assert "过期" in arch, "归档没标注已过期"
    assert "HANDOFF-DSH-SESSION.md" in arch, "归档没说以哪份为准"
    # 有价值的结论要留着，否则等于丢了两轮研究
    assert "限价" in arch and ("t=5.57" in arch or "0.1228" in arch), \
        "剥头皮 B 路径的结论没保留"


def test_kline_evolution_design_is_marked_draft():
    src = _read("docs", "K线演化-设计讨论稿.md")
    assert "未定稿" in src, "没标注未定稿"
    assert "待定" in src, "没把待定项列出来"
    # 核心概念：观察线，不是支撑线
    assert "观察线" in src, "缺观察线这个概念 —— 这是整个能力的定位"
    assert "不是支撑" in src or "支撑不是线的属性" in src, \
        "没写清观察线与支撑线的区别"
    # 每条线必须带自己的周期，否则 EMA200(3m) 和 EMA200(1h) 会混成一个
    assert "自己的周期" in src
    # 路径依赖：存上一件同类事件，不是前一根
    assert "since_last" in src, "缺 since_last_* —— 动态回溯的落地形式"
    # 允许无意义，否则会给 neutral 的 K 强行贴标签
    assert "neutral" in src
    # 不挂区间演化
    assert "不挂区间演化" in src or "只是观察位之一" in src, \
        "没写清「区间演化只是一维、不是框架」"
    # 防偷未来
    assert "at_bar" in src and "forward" in src, "没把当下读数与事后确认隔开"
    # 独有的数据优势
    assert "3s" in src and "is_buy" in src
    # 口径与判断标准必须分开
    assert "口径" in src and "判断标准" in src


def test_kline_design_supersedes_old_draft():
    """旧稿必须删掉，避免两份讨论稿各说一套。"""
    assert not os.path.isfile(_p("docs", "K线识别-讨论稿.md")), \
        "旧讨论稿还在，会和新稿冲突"
    assert os.path.isfile(_p("docs", "K线演化-设计讨论稿.md"))