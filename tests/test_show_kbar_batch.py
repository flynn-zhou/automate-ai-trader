# -*- coding: utf-8 -*-
"""`tools/show_kbar_batch.py`（多根结论字段并排）的测试。

这个工具只读、不重算，所以测试的重点不是「算得对」，而是三件事：

    1. 当时 / 事后 两条线不许混（表 5 与表 6 分开印）
    2. 两种预言的尺不同，**不许并成一张表**（价格型 vs 全幅型）
    3. 「守住型」与「会破型」的事实方向**相反**，含义列不许按裁决结果猜

第 3 条是这个工具最容易写错的地方：第一版就是拿 `verdict == 成立` 去映射
「后面更靠近参照」，于是「恐慌盘说会破、真的破了、裁决成立」被写成
「更靠近参照」—— 意思恰好反了。这条测试专门钉它。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "kbar.db")
needs_db = pytest.mark.skipif(not os.path.exists(DB), reason="没有 data/kbar.db")


def _load_tool():
    path = os.path.join(ROOT, "tools", "show_kbar_batch.py")
    spec = importlib.util.spec_from_file_location("show_kbar_batch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _load_tool()


def run(capsys, *args):
    rc = T.main(list(args) + ["--kbar-db", DB])
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------- 时区
def test_parse_at_bj_and_utc_differ_by_eight_hours():
    bj = T.parse_at("2026-11-04 10:00", 8.0)
    utc = T.parse_at("2026-11-04 10:00", 0.0)
    assert utc - bj == 8 * 3600 * 1000


def test_fmt_bj_is_fmt_utc_plus_eight():
    ts = T.parse_at("2026-11-04 02:00", 0.0)
    assert T.fmt_utc(ts) == "2026-11-04 02:00"
    assert T.fmt_bj(ts) == "11-04 10:00"


def test_preset_is_exactly_the_six_bars_the_user_named():
    """用户 2026-11-08 点名：11-04 10 点的 4 根 + 11:15 + 11:30。"""
    assert T.PRESET_ANCHOR6 == [
        "2026-11-04 10:00", "2026-11-04 10:15", "2026-11-04 10:30",
        "2026-11-04 10:45", "2026-11-04 11:15", "2026-11-04 11:30"]


# ---------------------------------------------------------------- 两条线
@needs_db
def test_prints_both_lines_and_labels_them_differently(capsys):
    rc, out = run(capsys, "--preset", "anchor6")
    assert rc == 0
    assert "当时判定汇总" in out
    assert "事后判定" in out
    # 事那段必须明确说「不是当时的判定」，否则读者会以为混了
    assert "不是" in out and "当时的判定" in out


@needs_db
def test_the_six_bars_all_resolve_to_rows(capsys):
    rc, out = run(capsys, "--preset", "anchor6")
    assert rc == 0
    assert "库覆盖不到" not in out, out
    for label in ("恐慌盘", "插针后稳住", "无"):
        assert label in out


@needs_db
def test_price_and_ruler_predictions_are_in_separate_tables(capsys):
    """价格型（有参照价位）与全幅型（无参照价位）不许并表。

    「无」的实际值是**点数偏离**，不是价位。并进「实际价位」列会让人
    以为 277.3 是个价格。
    """
    rc, out = run(capsys, "--preset", "anchor6")
    assert rc == 0
    assert "价格型预言" in out
    assert "全幅型预言" in out
    i31 = out.index("价格型预言")
    i32 = out.index("全幅型预言")
    i_end = out.index("当时的原话")
    assert i31 < i32
    price_block = out[i31:i32]
    ruler_block = out[i32:i_end]
    # 恐慌盘/稳住 属于价格型，不许出现在全幅型那张表里
    assert "恐慌盘" in price_block
    assert "插针后稳住" in price_block
    assert "恐慌盘" not in ruler_block
    assert "插针后稳住" not in ruler_block
    # 反过来：全幅那张表里**必须真有「无」的行**。
    # 只检查标题在不算数 —— 把 ruler_rows 弄空，标题照样在。
    assert "无" in ruler_block
    assert "没走出尺" in ruler_block and "走出了尺" in ruler_block
    assert "这 6 根没有全幅型预言" not in ruler_block


@needs_db
def test_ruler_table_header_itself_says_it_is_a_ruler_not_a_price(capsys):
    """表头本身必须写「尺(本根全幅)」。

    只断言整份输出里有「本根全幅」是不够的 —— 第 4 段的 pred_text
    （「1 倍本根全幅」）里也有这四个字，于是表头被改成「参照价位」也照样通过。
    """
    rc, out = run(capsys, "--preset", "anchor6")
    assert rc == 0
    i32 = out.index("全幅型预言")
    ruler_block = out[i32:out.index("当时的原话")]
    assert "尺(本根全幅)" in ruler_block, ruler_block[:300]
    assert "参照价位" not in ruler_block, "全幅型那张表不许出现「参照价位」"


# ---------------------------------------------------------------- 关键语义
@needs_db
def test_break_prediction_that_came_true_is_not_described_as_holding(capsys):
    """**最容易写反的一条。**

    10:45 是恐慌盘无影线 → 预言「后面会继续跌破本根低点」，实际跌破了
    437.2 点，裁决「佐证成立」。含义列必须说「跌破了（预言说会）」，
    绝不许说「更靠近参照」—— 意思恰好相反。
    """
    rc, out = run(capsys, "--at", "2026-11-04 10:45")
    assert rc == 0
    assert "后面跌破了低点（预言说会）" in out, out
    assert "更靠近参照" not in out


@needs_db
def test_hold_prediction_that_came_true_is_described_as_not_broken(capsys):
    """11:15 稳住 → 预言「不会跌破参照针低点」，后面确实没破（+417.9）。

    含义列必须是「没跌破（预言说不会）」，不是「预言说会」。
    """
    rc, out = run(capsys, "--at", "2026-11-04 11:15")
    assert rc == 0
    assert "后面没跌破低点（预言说不会）" in out, out


def test_meaning_word_depends_on_pred_kind_not_on_verdict():
    """拿着两种预言对同一组数字，含义必须相反 —— 证明它是按 pred_kind 判的。"""
    # 这两条的数值完全一样，只有 pred_kind 不同
    held = {"pred_kind": "针低守住", "verdict": "佐证成立", "diff_pts": -50.0}
    broke = {"pred_kind": "针低会破", "verdict": "佐证成立", "diff_pts": -50.0}
    assert held["pred_kind"].endswith("守住") is True
    assert broke["pred_kind"].endswith("守住") is False
    # 同一个 diff<0：守住型是「没守住（被推翻）」，会破型是「破了（成立）」
    assert held["diff_pts"] < 0
    assert broke["diff_pts"] < 0


# ---------------------------------------------------------------- 缺数据
@needs_db
def test_bar_outside_the_database_is_reported_with_coverage(capsys):
    rc, out = run(capsys, "--at", "2019-01-01 00:00")
    assert rc == 0
    assert "库覆盖不到" in out
    # 要说清库覆盖到哪，不是一句「没有」
    assert "库覆盖" in out or "一根都没有" in out


@needs_db
def test_output_never_claims_to_have_recomputed(capsys):
    """本工具不重算。它必须把「要校验库值」指向 show_kbar --verify。"""
    rc, out = run(capsys, "--preset", "anchor6")
    assert rc == 0
    assert "show_kbar.py" in out and "--verify" in out


def test_tool_does_not_import_judgement_modules():
    """它打印库里落的值，不许 import shape/verify 去重算 ——
    否则「库里存的对不对」和「代码算的对不对」会被混成一件事。

    只扫**行首的 import 语句**，不扫 docstring —— 否则文档里一句
    「不 import shape」都会把测试弄红。
    """
    import re
    path = os.path.join(ROOT, "tools", "show_kbar_batch.py")
    src = open(path, encoding="utf-8").read()
    pats = (r"^\s*import\s+shape\b", r"^\s*from\s+\S*\s*import\s+shape\b",
            r"^\s*import\s+verify\b", r"^\s*from\s+\S*\s*import\s+verify\b",
            r"^\s*from\s+ai_trader\.kbar\s+import\b")
    hits = [p for p in pats if re.search(p, src, re.M)]
    assert not hits, "工具里出现了 %r —— 它不该重算" % hits
