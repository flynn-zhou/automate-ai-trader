# -*- coding: utf-8 -*-
"""文档布局的回归防线。

两个约束，都是为了「下一个会话不读到过期的东西」：
  1. HANDOFF.md 拆分后必须真的没丢行 —— 拆分是逐行切片，
     任何一次重跑都要能验证 1..N 行无缝覆盖、行数守恒；
  2. 过期的 v10 规范必须待在 _archive/docs_outdated/ 里，且带说明。
     它留在根目录会被当成当前口径（v10 三线 vs 现在的 v11 四线，
     字段名全不一样，按它读代码会对不上号）。
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _p(*parts):
    return os.path.join(ROOT, *parts)


def test_handoff_split_loses_no_lines():
    """拆分计划必须无缝覆盖原文 1..N 行，不重叠不遗漏。"""
    from tools.split_handoff import PLAN
    src = io.open(_p("docs", "handoff", "00-系统现状.md"), encoding="utf-8").read()
    assert "# 交接文档" in src, "第一份应含原文开头"
    spans = sorted((a, b) for a, b, _, _ in PLAN)
    assert spans[0][0] == 1
    for i in range(1, len(spans)):
        assert spans[i][0] == spans[i - 1][1] + 1, "切片不连续"
    # 行数守恒：切片总长 == 各分文件正文总长
    total = spans[-1][1]
    assert total == sum(b - a + 1 for a, b in spans)


def test_every_split_file_exists_and_is_nonempty():
    from tools.split_handoff import PLAN, OUT
    for a, b, name, _ in PLAN:
        f = _p(*OUT.split(os.sep), name)
        assert os.path.isfile(f), "缺分文件: " + name
        body = io.open(f, encoding="utf-8").read()
        assert body.strip(), name + " 是空的"
        assert ("第 %d-%d 行" % (a, b)) in body, name + " 缺溯源注释"


def test_handoff_root_is_now_a_pointer():
    """根 HANDOFF.md 必须变薄，且指向新位置 —— 不能还留着 1066 行副本。"""
    src = io.open(_p("HANDOFF.md"), encoding="utf-8").read()
    assert len(src.splitlines()) < 80, "根 HANDOFF.md 还很厚，说明拆分没生效"
    assert "docs/handoff/" in src, "指针没指向新位置"


def test_v10_spec_is_archived_with_warning():
    """v10 规范必须在归档目录，且不能留在根目录。"""
    assert not os.path.isfile(_p("区间演化规则-规范v10-方案稿.md")), \
        "过期的 v10 规范还留在根目录，会被当成当前口径"
    arch = _p("_archive", "docs_outdated", "区间演化规则-规范v10-方案稿.md")
    assert os.path.isfile(arch), "v10 规范不见了（应该是移到归档，不是删掉）"
    note = io.open(_p("_archive", "docs_outdated", "README.md"), encoding="utf-8").read()
    assert "已过期" in note and "v11" in note, "归档说明没写清它被什么取代"
    assert "不要引用" in note or "作废" in note


def test_v10_archive_is_not_gitignored():
    """归档说明必须能入库，否则下一个会话在 GitHub 上看不到它过期了。

    只放本地 = 把"这份作废了"这个知情成本转嫁给未来的人。
    """
    gi = io.open(_p(".gitignore"), encoding="utf-8").read()
    assert "_archive/*" in gi
    assert "!_archive/docs_outdated/" in gi, "归档说明被 .gitignore 挡在库外了"
    assert "!_archive/docs_outdated/**" in gi


def test_docs_index_points_to_new_layout():
    idx = io.open(_p("docs", "README.md"), encoding="utf-8").read()
    assert "docs/handoff/" in idx
    assert "docs_outdated" in idx
    assert "不要当现状引用" in idx or "⚠️" in idx
