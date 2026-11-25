# -*- coding: utf-8 -*-
"""「无」拆词（D-23.4）的测试。

为什么单独一个文件：别的测试读的是**已经建好的库**（`data/kbar.db`），
改 `shape.py` 里的拆词逻辑不影响它们 —— 变异测试实测 5 个变异**全部漏抓**。
所以这里必须直接打 `judge()`，让代码一改就红。

钉住四件事：
  1. `n_touch=0` → 「无·真空」；`n_touch>=1` → 「无·贴线」（含边界 0/1）
  2. 有形态时**不拆** —— 拆词只作用于「一个形态都没成立」的根
  3. 拿不到 `n_touch` 就退回「无」并写明没拆，**不许猜**
  4. 拆词不改 `states`（真空/贴线不是形态），也不改预言
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import shape as S  # noqa: E402
from ai_trader.kbar import verify as V  # noqa: E402


def bar(o, h, l, c, ts=None, vol_rel=None, taker_buy_ratio=None):
    return {"ts": ts, "o": o, "h": h, "l": l, "c": c,
            "vol_rel": vol_rel, "taker_buy_ratio": taker_buy_ratio}


def flat_bars(n=3, base=10000.0):
    """一串「什么形态都不成立」的根：实体小、无影线、无恐慌放量。

    这种根 judge 会走到底，label 落在「没有形态」这一族。
    """
    return [bar(base, base + 2, base - 2, base, ts=1000 + k)
            for k in range(n)]


# ---------------------------------------------------------------- 词表本身
def test_the_three_words_are_distinct_and_none_is_not_a_state():
    assert S.STATE_NONE == "无"
    assert S.LABEL_NO_EMPTY == "无·真空"
    assert S.LABEL_NO_NEAR == "无·贴线"
    assert len(set(S.LABEL_NO_ALL)) == 3
    # 「没有形态」这一族都不是形态，不许出现在 STATE_ORDER 里
    for w in S.LABEL_NO_ALL:
        assert w not in S.STATE_ORDER, "%s 混进了状态表" % w


def test_split_threshold_is_one():
    """切分点是 1：0 算真空，1 就算贴线。改这个数就该有人来解释。"""
    assert S.NO_SPLIT_BY_TOUCH == 1


# ---------------------------------------------------------------- 直接打 judge
@pytest.mark.parametrize("n_touch,expect", [
    (0, S.LABEL_NO_EMPTY),      # 一根线都没碰到 = 真空
    (1, S.LABEL_NO_NEAR),       # 边界：碰到 1 条就已经不是真空
    (2, S.LABEL_NO_NEAR),
    (7, S.LABEL_NO_NEAR),
])
def test_no_shape_bars_split_by_n_touch(n_touch, expect):
    bars = flat_bars()
    j = S.judge(bars, len(bars) - 1, n_touch=n_touch)
    assert not j.states, "这组数据本来就不该有形态，测试前提没了"
    assert j.label == expect, (
        "n_touch=%d 应判 %s，实际 %s" % (n_touch, expect, j.label))


def test_boundary_zero_vs_one_is_the_whole_split():
    """0 与 1 必须落在不同的词上 —— 这是切分点唯一的意义。

    如果两个数判出同一个词，说明 NO_SPLIT_BY_TOUCH 被改到别处去了。
    """
    bars = flat_bars()
    a = S.judge(bars, len(bars) - 1, n_touch=0)
    b = S.judge(bars, len(bars) - 1, n_touch=1)
    assert a.label != b.label


def test_evidence_says_which_word_and_why():
    """证据里要写明 n_touch 是几 —— 只给词不给数，复核的人无从验。"""
    bars = flat_bars()
    ev = "\n".join(S.judge(bars, len(bars) - 1, n_touch=4).evidence)
    assert "n_touch=4" in ev, "证据里没写 n_touch，没法复核：\n%s" % ev
    assert S.LABEL_NO_NEAR in ev

    ev0 = "\n".join(S.judge(bars, len(bars) - 1, n_touch=0).evidence)
    assert "n_touch=0" in ev0
    assert S.LABEL_NO_EMPTY in ev0


def test_missing_n_touch_falls_back_to_bare_word_and_says_so():
    """拿不到 n_touch 退回「无」，但**必须在证据里写明没拆**。

    这条防的是：调用方忘了传，库里悄悄多出一堆裸「无」，
    谁都不知道那些根是没拆还是本来就该是「无」。
    """
    bars = flat_bars()
    j = S.judge(bars, len(bars) - 1)          # 不传 n_touch
    assert j.label == S.STATE_NONE, "没给 n_touch 就该退回裸「无」，不许猜"
    ev = "\n".join(j.evidence)
    assert "未拆词" in ev or "没给 n_touch" in ev, (
        "退回裸「无」却没写明没拆：\n%s" % ev)


def test_split_does_not_touch_states():
    """拆词只改 label，不改 states —— 真空/贴线不是形态。"""
    bars = flat_bars()
    for nt in (0, 1, 5):
        j = S.judge(bars, len(bars) - 1, n_touch=nt)
        assert j.states == (), "n_touch=%d 时 states 被写脏了：%r" % (nt, j.states)


def test_shaped_bars_are_not_split():
    """有形态的根，不管 n_touch 是几，都不许被拆成「没有形态」那一族。

    这条防的是把拆词逻辑写成「先拆再看形态」。
    """
    # 造一根恐慌盘：放量 + 主动买极低 + 收位极低
    bars = [bar(10000.0, 10000.0, 9800.0, 9800.1,
                ts=1000, vol_rel=9.0, taker_buy_ratio=0.05)]
    for nt in (0, 1, 9):
        j = S.judge(bars, 0, n_touch=nt)
        assert S.STATE_PANIC in j.states, "这组数据本该判恐慌盘：%r" % (j.states,)
        assert j.label not in S.LABEL_NO_ALL, (
            "有形态（%s）却把 label 拆成了 %s" % (j.states, j.label))


def test_split_words_share_one_prediction_but_are_traceable():
    """两个新词共用「后无大波动」这条预言（拆的是环境描述，不是要验的话），
    但 pred_text 里要带上词名，否则表 6 里两个词分不开统计。"""
    bars = flat_bars()
    seen = {}
    for nt in (0, 3):
        j = S.judge(bars, len(bars) - 1, n_touch=nt)
        p = V.predict(bars, len(bars) - 1, j)
        assert p.kind == V.PRED_QUIET, "拆出来的词应共用 PRED_QUIET"
        seen[j.label] = p.text
    for w, text in seen.items():
        assert w in text, "pred_text 里没写词名 %s：%r" % (w, text)
    assert seen[S.LABEL_NO_EMPTY] != seen[S.LABEL_NO_NEAR], (
        "两个词的预言原文一样，表 6 里分不开")


def test_judge_rules_version_is_v2():
    """词表变了，规则版本必须跟着变 —— 这是 params_hash 抓不到的那一半。"""
    assert S.JUDGE_RULES_VERSION == "v2"


# ---------------------------------------------------------------- 端到端：走 build
# 上面那些测试直接打 judge()，所以「build 忘了把 n_touch 传进去」这种
# 「写好了但没接线」的错误抓不到（变异测试实测漏抓）。
# 这一条真跑一遍 build，从表 1 取 n_touch 一路走到落库。
DB = os.path.join(ROOT, "data", "kbar.db")


@pytest.fixture()
def small_db():
    """把生产库拷一份、只留前 300 根。不手搓 schema，测的是真那张表。"""
    import shutil
    import sqlite3
    import tempfile

    if not os.path.exists(DB):
        pytest.skip("没有 data/kbar.db")
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "kbar_small.db")
    shutil.copy2(DB, dst)
    con = sqlite3.connect(dst)
    try:
        ts_all = [r[0] for r in con.execute(
            "SELECT ts FROM kbar_evolution WHERE symbol='BTCUSDT' "
            "AND interval_ms=900000 AND o IS NOT NULL ORDER BY ts LIMIT 300")]
        keep = ts_all[-1]
        for tbl in ("kbar_evolution", "kbar_shape", "kbar_verify",
                    "kbar_verify_survival", "kbar_line", "kbar_reaction",
                    "kbar_event", "kbar_forward"):
            con.execute("DELETE FROM %s WHERE ts > ?" % tbl, (keep,))
        con.commit()
    finally:
        con.close()
    yield dst
    shutil.rmtree(tmp, ignore_errors=True)


def test_build_actually_wires_n_touch_into_judge(small_db):
    """build 必须把表 1 的 n_touch 传进 judge。

    漏了接线的话，库里会全是裸「无」—— 而别的测试读的是**已经建好的库**，
    照样全绿。这条是唯一能抓住那个的手。
    """
    from collections import Counter

    from ai_trader.kbar import build as B
    from ai_trader.kbar.store import KBarStore

    out = B.build_and_persist_shape(small_db, "BTCUSDT", 900000)
    assert out.get("rows", 0) > 0, out

    with KBarStore(small_db) as st:
        rows = st.shape_rows("BTCUSDT", 900000)
    lab = Counter(r["label"] for r in rows)

    assert lab.get(S.STATE_NONE, 0) == 0, (
        "库里还有 %d 行裸「无」—— build 没把 n_touch 传给 judge，拆词没接线。"
        "分布：%r" % (lab.get(S.STATE_NONE, 0), dict(lab)))

    # 两个词都得真出现，否则等于「拆了个空」
    assert lab.get(S.LABEL_NO_EMPTY, 0) > 0, "「无·真空」一行都没有：%r" % dict(lab)
    assert lab.get(S.LABEL_NO_NEAR, 0) > 0, "「无·贴线」一行都没有：%r" % dict(lab)

    # 拆出来的行，states 必须是空的（拆词不改 states）
    for r in rows:
        if r["label"] in S.LABEL_NO_ALL:
            assert not (r["states"] or "").strip(), (
                "%s 的行 states 不是空串：%r" % (r["label"], r["states"]))


def test_build_records_the_rule_version(small_db):
    """规则版本要落进 meta，否则「词表换过没有」在库里查不出来。"""
    import json

    from ai_trader.kbar import build as B
    from ai_trader.kbar.store import KBarStore

    B.build_and_persist_shape(small_db, "BTCUSDT", 900000)
    with KBarStore(small_db) as st:
        raw = st.get_meta("shape_rules:BTCUSDT:15m")
    assert raw, "没写 shape_rules meta"
    meta = json.loads(raw)
    assert meta.get("judge_rules_version") == S.JUDGE_RULES_VERSION, meta
    assert set(meta.get("label_vocab") or []) == set(S.LABEL_NO_ALL), meta
