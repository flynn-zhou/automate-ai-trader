# -*- coding: utf-8 -*-
"""新鲜度在**页面上**的接线测试。

为什么单独测这个：后端算出「落后 N 小时」不等于有人会看见。
2026-11 那次停摆 6.5 天没人发现，缺的正是这一段接线。
"""
from __future__ import annotations

import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "ai_trader", "web", "kbar_api.py")
TPL = os.path.join(ROOT, "ai_trader", "web", "templates", "_evo_js.html")


def _api_src():
    return open(API, encoding="utf-8").read()


def _tpl_src():
    return open(TPL, encoding="utf-8").read()


def test_meta_endpoint_carries_freshness():
    """meta 响应必须带 freshness，否则前端无从判断。"""
    src = _api_src()
    assert "_freshness()" in src
    # 三条返回路径都要带上（空库、无数据、正常）
    assert src.count('"freshness": fresh') >= 2, \
        "meta 的返回分支里有的没带 freshness，前端会拿到 undefined"


def test_freshness_reader_tolerates_a_missing_file():
    """没装 timer 时 kbar_health.json 不存在 —— 不能因此 500。"""
    src = _api_src()
    m = re.search(r"def _freshness\(\).*?\n(?=\n\ndef |\n\n@)", src, re.S)
    assert m, "找不到 _freshness()"
    body = m.group(0)
    assert "except (OSError, ValueError)" in body
    assert "return None" in body


def _loadmeta_body(src: str) -> str:
    """截出 loadMeta() 的函数体。

    **必须截出边界**：整个文件里 `if (d.empty)` 出现两次（loadMeta 和 load 各一次），
    用 `src.index("if (d.empty)", i_fresh)` 向后找，一旦 renderFresh 被挪到后面，
    找到的是 load() 里那一个 —— 断言永远通过，等于没测。
    """
    i = src.index("function loadMeta()")
    j = src.index("\n  function ", i + len("function loadMeta()"))
    return src[i:j]


def test_template_renders_a_banner_and_a_stale_red_state():
    src = _tpl_src()
    assert 'id="evo-fresh"' in src, "页面上没有新鲜度容器"
    assert "function renderFresh" in src, "没有渲染函数"
    # 落后时必须说出「画的是旧数据」——这才是能拦住误判的那句话
    assert "图上画的是旧数据" in src

    body = _loadmeta_body(src)
    assert "renderFresh(d.freshness)" in body, "loadMeta 里没调用渲染"
    i_fresh = body.index("renderFresh(d.freshness)")
    i_empty = body.index("if (d.empty)")
    assert i_fresh < i_empty, \
        "renderFresh 被放在 empty 分支之后，空数据时看不到停摆"


def test_template_says_so_when_the_gate_is_not_installed():
    """没装闸本身也是信息，不能静默显示成「正常」。"""
    src = _tpl_src()
    assert "新鲜度闸未就绪" in src


def test_freshness_snapshot_shape_matches_what_the_template_expects():
    """后端写出来的字段，前端真的在读 —— 用一份真实快照对一遍。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "kbar_freshness", os.path.join(ROOT, "tools", "kbar_freshness.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "kbar.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE kbar_evolution (symbol TEXT, interval_ms INT,"
                    " ts INT, PRIMARY KEY (symbol, interval_ms, ts))")
        con.commit()
        con.close()
        snap = mod.check(db, now_ms=1_700_000_000_000)

    # 前端用到的字段
    assert "layers" in snap
    for lab in ("5m", "15m"):
        assert lab in snap["layers"], "前端按 5m/15m 两个键取值"
        v = snap["layers"][lab]
        for key in ("stale", "lag_hours", "max_ts_human"):
            assert key in v, "前端要读 %s，后端没给" % key

    # 空库必须 stale —— 这条在 freshness 自己的测试里也有，这里从快照形状再确认一次
    assert snap["layers"]["5m"]["stale"] is True
    assert json.dumps(snap)                      # 可序列化，能被 jsonify 送出去
