# -*- coding: utf-8 -*-
"""表 7（维度层 / 环境快照）的测试。

钉住的是这一层的**边界**，不是它的数字：

  1. 41 个维度的名字与分类必须与《维度总账》§三 一致（两份文档不许各说各话）
  2. 这一层**不许**碰判据：`shape.judge` 的入参、阈值、结论词都不变
  3. 「没算」必须是 None，不许补 0（空着可查，填错不可查）
  4. 单一量规：`side`/`recover_pct`/`close_side` 必须取**同一条线**
  5. 旁证/矛盾只用判据没读的维度（用判据自己读的列自证是废话）
  6. 增量 ≡ 全量（同根数据，没有前视窗口）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import dims as D  # noqa: E402

DB = os.path.join(ROOT, "data", "kbar.db")
DOC = os.path.join(ROOT, "docs", "K线演化维度总账.md")
SYM, IV = "BTCUSDT", 900000


def _bar(**kw):
    base = {"ts": 1, "o": 100.0, "h": 110.0, "l": 90.0, "c": 100.0,
            "vol_rel": 1.0}
    base.update(kw)
    return base


def _ctx(bar=None, reactions=None):
    return {"bar": bar or _bar(), "reactions": reactions or []}


# ---------------------------------------------------------------- 维度表本身
def test_there_are_exactly_39_dimensions():
    assert len(D.DIMS) == 39, "维度数变了：%d" % len(D.DIMS)
    assert len(set(D.DIM_NAMES)) == 39, "维度名有重复"


def test_dimension_categories_match_the_ledger():
    """41 个维度的名字和分类，必须与《维度总账》§三 逐个对上。

    两份文档各说各话是最难查的错：改了一边，读者不知道信哪边。
    """
    assert os.path.exists(DOC), "没有维度总账"
    text = open(DOC, encoding="utf-8").read()
    m = re.search(r"## 三、(.*?)## 四、", text, re.S)
    assert m, "总账里找不到 §三"
    sec = m.group(1)
    doc = {}
    for ln in sec.splitlines():
        ln = ln.strip()
        if not ln.startswith("|") or ln.startswith("|---") or "分类" in ln:
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) != 4:
            continue
        cat = re.sub(r"\*\*", "", cells[0])
        if cat in ("合计", "收割（订单块）"):
            continue
        if cat not in ("价", "量", "时", "空", "观察线的反应", "情绪"):
            continue
        # 总账里「观察线的反应」在代码里叫「反应」，映射一下
        code_cat = "反应" if cat == "观察线的反应" else cat
        for nm in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", cells[3]):
            doc[nm] = code_cat

    assert set(doc) == set(D.DIM_NAMES), (
        "总账与代码的维度名不一致。\n  只在总账：%s\n  只在代码：%s"
        % (sorted(set(doc) - set(D.DIM_NAMES)),
           sorted(set(D.DIM_NAMES) - set(doc))))
    bad = [(n, D.DIM_CAT[n], doc[n]) for n in doc if D.DIM_CAT[n] != doc[n]]
    assert not bad, "维度分类不一致（名, 代码, 总账）：%s" % bad


def test_dimension_layer_does_not_touch_judge():
    """这一层不许改判据 —— 不 import shape、不 import verify。

    反过来才行：描述层可以读判定结果（`label`），判定层不许读描述层，
    否则「加个描述字段」会悄悄改变结论。
    """
    src = open(os.path.join(ROOT, "ai_trader", "kbar", "dims.py"),
               encoding="utf-8").read()
    for banned in ("import shape", "from . import shape",
                   "import verify", "from . import verify"):
        assert banned not in src, "维度层 import 了判定层：%s" % banned
    # 也不许出现判据的阈值常量
    for c in ("PANIC_VOL_REL_MIN", "WICK_PCT_MIN", "SCENARIO_TOL_PTS",
              "STABLE_LOOKBACK_BARS"):
        assert c not in src, "维度层引用了判据阈值 %s" % c


def test_dims_hash_is_separate_from_params_hash_and_verify_hash():
    """三层的指纹必须互不相同，否则「改描述」会让判定表看起来过期。"""
    from ai_trader.kbar import shape as S
    from ai_trader.kbar import verify as V
    h = D.dims_hash()
    assert h != S.params_hash(), "维度层与判定层指纹撞了"
    assert h != V.verify_hash(), "维度层与佐证层指纹撞了"
    assert h != V.survival_hash(), "维度层与存活层指纹撞了"
    assert len(h) == 12


# ---------------------------------------------------------------- 快照
def test_missing_values_are_none_not_zero():
    """没算出来的维度必须是 None。

    补 0 会把「没算」说成「是 0」—— 例如 `pen_depth_pct=0` 会读成
    「穿了 0 深」，而真相是「没有反应行，不知道」。
    """
    snap = D.snapshot(_ctx())
    v = snap["values"]
    for name in ("vol_rel", "pen_depth_pct", "recover_pct", "close_side",
                 "side", "line_kind", "dist_pct", "zone", "event"):
        if name == "vol_rel":
            continue          # 这个 fixture 给了 1.0
        assert v[name] is None, "%s 应该是 None（没数据），实际 %r" % (name,
                                                                      v[name])
    # 没值就不给分档，不许给个空档冒充「小」
    assert snap["buckets"]["pen_depth_pct"] == ""
    assert snap["buckets"]["close_pos"] == ""


def test_single_ruler_takes_the_deepest_pen():
    """`side`/`recover_pct`/`close_side`/`line_kind` 必须来自**同一条线**。

    混着取会造出「A 线的方向 + B 线的收回」这种不存在的组合。
    """
    reactions = [
        {"line_key": "A", "line_kind": "ema20", "pen_depth_pct": 0.01,
         "recover_pct": 0.90, "side": "from_above", "close_side": "below",
         "dist_pct": -0.001, "line_price": 100.0},
        {"line_key": "B", "line_kind": "donchian_hi", "pen_depth_pct": 0.50,
         "recover_pct": 0.10, "side": "from_below", "close_side": "above",
         "dist_pct": 0.030, "line_price": 110.0},
    ]
    v = D.snapshot(_ctx(reactions=reactions))["values"]
    assert v["line_key"] == "B", "没取穿透最深的那条线"
    assert v["pen_depth_pct"] == 0.50
    # 四个字段必须都来自 B
    assert v["recover_pct"] == 0.10, "recover 取了别的线 → 混线"
    assert v["side"] == "from_below", "side 取了别的线 → 混线"
    assert v["close_side"] == "above", "close_side 取了别的线 → 混线"
    assert v["line_kind"] == "donchian_hi", "line_kind 取了别的线 → 混线"
    # dist_pct 用绝对值最小，与「最深」不是同一个量规，这是刻意的
    assert abs(v["dist_pct"] - 0.001) < 1e-9, "dist_pct 应该取绝对值最小的"


def test_buckets_are_parameter_free_cuts():
    """分档只许用 1/3、2/3、1.0、0/1~2/3+ 这几个天然切点。"""
    assert tuple(D.THIRDS) == (1.0 / 3.0, 2.0 / 3.0)
    assert tuple(D.COUNT_CUTS) == (0, 2)
    # 边界行为
    assert D._third_word(0.0) == "小"
    assert D._third_word(1.0 / 3.0) == "中"       # 等于切点算上一档
    assert D._third_word(0.999) == "大"
    assert D._mult_word(0.99) == "低于均量"
    assert D._mult_word(1.0) == "约均量"
    assert D._mult_word(2.0) == "显著放量"
    assert D._count_word(0) == "无"
    assert D._count_word(2) == "少"
    assert D._count_word(3) == "密"


def test_snapshot_covers_exactly_the_39_names():
    snap = D.snapshot(_ctx())
    assert set(snap["values"]) == set(D.DIM_NAMES)
    assert set(snap["buckets"]) == set(D.DIM_NAMES)
    assert len(snap["values"]) == 39


# ---------------------------------------------------------------- 旁证/矛盾
def test_notes_only_use_dimensions_judge_does_not_read():
    """旁证/矛盾不许用判据自己读的列来「自证」。

    用判据的输入去证明判据的结论，是循环论证 —— 条件本来就成立，
    这种「旁证」永远成立、永远没有信息。

    ⚠ `n_touch` 是**条件性**的判据输入，需要单独说清（否则这条测试会
    要么误报、要么被悄悄放过）：

    · `judge` 读 `n_touch` **只为了把「无」拆成「无·真空 / 无·贴线」**
      （`if int(n_touch) < NO_SPLIT_BY_TOUCH`）。判 插针/稳住/远离/恐慌盘 时
      完全不看它。
    · 所以在本函数里，对 `插针后稳住/针后远离` 这两个结论词，`n_touch`
      **就是判据没读的列**，「说守住却一根线都没碰到」这条矛盾不是循环论证
      —— 恰恰是判据自己发现不了的东西。
    · 反过来，若哪条规则在 `无·真空/无·贴线` 上拿 `n_touch` 当旁证，
      那就是循环论证（判据已经用 n_touch 定了这两个词），必须红。

    下面两个断言分别管这两件事。
    """
    JUDGE_DIMS = {"o", "h", "l", "c", "vol_rel", "taker_buy_ratio",
                  "line_price"}
    src = open(os.path.join(ROOT, "ai_trader", "kbar", "dims.py"),
               encoding="utf-8").read()
    m = re.search(r"def corroborate\(.*?\n(?=# ----)", src, re.S)
    assert m, "找不到 corroborate"
    body = m.group(0)
    used = set(re.findall(r'v\.get\("([a-z_]+)"\)', body))
    bad = (used & JUDGE_DIMS) - {"n_touch"}
    assert not bad, (
        "旁证用了判据自己读的维度 %s —— 那是循环论证，没有信息量" % sorted(bad))

    # 第二件：`n_touch` 只许用在不被它决定的结论词上
    # 找出所有 `label == "…"` / `label in ("…", …)` 的守卫，凡是提到
    # 无·真空/无·贴线 的块里都不许出现 v.get("n_touch")。
    no_labels = ('无·真空', '无·贴线')
    for blk in re.split(r"\n\s*# ----", body):
        if not any(x in blk for x in no_labels):
            continue
        assert 'v.get("n_touch")' not in blk, (
            "某条旁证在「无」结论上用了 n_touch —— 判据正是用它拆「无」的，"
            "这是循环论证：%r" % blk.strip()[:160])


def test_there_are_no_states_left_in_corroborate():
    """`corroborate` 不许再挂一个没人用的参数。

    早先它有 `states: Sequence[str]`，函数体一次都没引用，所有调用方都传
    `()`。挂着它等于宣称「旁证看过 states」—— 那是假账。要加状态规则就把
    `states` 加回来并真的用上。
    """
    src = open(os.path.join(ROOT, "ai_trader", "kbar", "dims.py"),
               encoding="utf-8").read()
    m = re.search(r"def corroborate\(([^)]*)\)", src, re.S)
    assert m, "找不到 corroborate 签名"
    assert "states" not in m.group(1), (
        "corroborate 又有了 states 参数：%r —— 确认它真的被用上了吗？"
        % m.group(1).strip())


def test_contradiction_when_claiming_support_with_no_line():
    """结论说「守住」，但一根线都没碰到 → 必须提示矛盾。

    这是这一层最有价值的一条：`judge` 判稳住/远离时**不读 n_touch**，
    所以这个矛盾它自己发现不了。
    """
    v = D.snapshot(_ctx())["values"]
    v["n_touch"] = 0
    notes = D.corroborate("插针后稳住", {"values": v})
    assert any(n.kind == "矛盾" and "n_touch" in n.dims for n in notes), (
        "没说「无线可守」，提示：%r" % notes)


def test_no_false_contradiction_when_lines_exist():
    """有线可碰时不许乱报矛盾（假警报会把真警报淹掉）。"""
    v = D.snapshot(_ctx())["values"]
    v["n_touch"] = 3
    notes = D.corroborate("插针后稳住", {"values": v})
    assert not any(n.kind == "矛盾" and "n_touch" in n.dims for n in notes)


def test_notes_never_change_the_label():
    """旁证/矛盾只返回提示，不许回传任何「改判」的东西。"""
    v = D.snapshot(_ctx())["values"]
    v["n_touch"] = 0
    notes = D.corroborate("插针后稳住", {"values": v})
    for n in notes:
        d = n.as_dict()
        assert set(d) == {"kind", "dims", "text"}, (
            "提示里出现了可改判的字段：%r" % d)


# ---------------------------------------------------------------- 落库
@pytest.fixture()
def small_db():
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
                    "kbar_event", "kbar_forward", "kbar_dims"):
            con.execute("DELETE FROM %s WHERE ts > ?" % tbl, (keep,))
        con.commit()
    finally:
        con.close()
    yield dst
    shutil.rmtree(tmp, ignore_errors=True)


def test_build_persists_one_row_per_bar(small_db):
    from ai_trader.kbar import build as B
    from ai_trader.kbar.store import KBarStore

    out = B.build_and_persist_dims(small_db, SYM, IV)
    assert not out.get("error"), out.get("error")
    with KBarStore(small_db) as st:
        c = st.dims_counts(SYM, IV)
        n = st.count_rows("kbar_evolution", SYM, IV)
    assert c["rows"] == n, "表 7 行数 %d ≠ 表 1 的 %d 根" % (c["rows"], n)
    assert c["dims_hashes"] == [D.dims_hash()]
    # 有值维度不许是 0 —— 全 0 说明表建了但算法没接线
    assert c["filled_max"] and c["filled_max"] >= 20, (
        "一根最多只有 %s 个维度有值，算法可能没接线" % c["filled_max"])


def test_incremental_equals_full(small_db):
    """增量重算的尾部必须与全量逐行相同。

    这一层一根只依赖自己那根的数据（没有前视窗口），所以增量 ≡ 全量
    是**设计上的必然**；如果不成立，说明取值时偷看了邻居。
    """
    from ai_trader.kbar import build as B
    from ai_trader.kbar.store import KBarStore

    B.build_and_persist_dims(small_db, SYM, IV)
    with KBarStore(small_db) as st:
        full = {r["ts"]: (r["values_json"], r["buckets_json"], r["notes_json"])
                for r in st.dims_rows(SYM, IV)}

    ts_sorted = sorted(full)
    since = ts_sorted[-3]
    out = B.build_and_persist_dims(small_db, SYM, IV, since_ts=since)
    assert not out.get("error"), out.get("error")

    with KBarStore(small_db) as st:
        inc = {r["ts"]: (r["values_json"], r["buckets_json"], r["notes_json"])
               for r in st.dims_rows(SYM, IV)}
    assert set(inc) == set(full), "增量后行数变了"
    diff = [t for t in full if full[t] != inc[t]]
    assert not diff, "增量与全量在 %d 根上不一致，例：%s" % (len(diff), diff[:3])


def test_notes_are_persisted_and_reloadable(small_db):
    """提示要能读出原样 —— 落库后读不回来等于没落。"""
    from ai_trader.kbar import build as B
    from ai_trader.kbar.store import KBarStore

    B.build_and_persist_dims(small_db, SYM, IV)
    with KBarStore(small_db) as st:
        rows = st.dims_rows(SYM, IV)
    noted = [r for r in rows if json.loads(r["notes_json"])]
    assert noted, "一根提示都没有 —— 规则可能没接线"
    for r in noted[:20]:
        for n in json.loads(r["notes_json"]):
            assert set(n) == {"kind", "dims", "text"}
            assert n["kind"] in ("旁证", "矛盾")
            assert n["text"].strip()
