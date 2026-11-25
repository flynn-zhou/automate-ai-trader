# -*- coding: utf-8 -*-
"""《K线演化维度总账.md》与 `data/kbar.db` 必须逐列对得上。

为什么要有这条测试：这份文档是「加维度前先改这里」的唯一出处。
文档一旦比库旧，后来的人会照着不存在的列写代码；比库新，会以为某列已经填了。
两种都必须红。

钉住五件事：
  1. 库里的**每一列**都在总账里出现，且只出现一次（不许漏登记）
  2. 总账里的**每一列**都真的在库里（不许写幻觉列）
  3. 填充率与库一致（差 > 0.15 个百分点就红）
  4. `★`（判据真的读了）的集合与代码一致 —— 这是防「文档说用了、代码没读」
  5. 分类只许出现白名单里的词，且空壳列必须真的是 0
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "kbar.db")
DOC = os.path.join(ROOT, "docs", "K线演化维度总账.md")
SYM, IV = "BTCUSDT", 900000

# 不做列普查的表。每一张都要说清为什么排除 —— 否则「排除」就成了藏匿。
SKIP_TABLES = {
    # 键值表，不是数据表
    "kbar_meta",
    # 表 7 是**描述层**：它的 41 个维度全部是表 0/1/1.5/2 里那些列的重新投影，
    # 加上 values_json/buckets_json/notes_json 三个 JSON 包装和 4 个簿记列。
    # 它**不新增任何维度**，所以列普查排除它，避免把同一份维度数两遍。
    # 「表 7 不新增维度」这件事本身由 test_table7_adds_no_new_dimension 钉住。
    "kbar_dims",
    # 表 8 是**评测日志**，一个维度都没有：全是凭证（切分/哈希）、
    # 计数（验了几格、过几格）和一个 note。它不是数据表，
    # 把它算进列普查只会逼着在总账里登记 18 个与维度无关的列。
    # 「表 8 不新增维度」由 test_table8_adds_no_new_dimension 钉住。
    "kbar_holdout_log",
}

CATS = {"价", "量", "时", "空", "反应", "情绪", "收割",
        "簿记", "输出", "事后", "空壳"}


def _judge_reads():
    """从 `shape.py` 源码 AST 求「判据实际读了哪些维度」，返回 {(表, 列)}。

    实现在 `ai_trader/kbar/ledger.py::judge_reads` —— 这里只是转发，
    **刻意不在测试里再抄一份**：Web 的快照面板要标「哪 8 个是判据」，
    如果它和测试各写一份，就又回到「两份手抄互相印证」的老路上。

    ## 为什么必须从代码求（这条历史不能忘）

    这里原来是手写的：

        JUDGE = {("kbar_evolution", c) for c in
                 ("o", "h", "l", "c", "vol_rel", "taker_buy_ratio")} | {
                     ("kbar_line", "line_price")}

    问题不在它**当时**对不对 —— 它对，但它和 `shape.py` 之间**没有任何连接**。
    实测过（2026-11 审计）：往 `judge` 里插一行 `_x = b.get("zone")`，
    整个 `test_dims_ledger.py` 照样全绿。也就是说「★ 必须等于代码实际读的列」
    这条测试，防的是「文档 vs 手抄的代码」，不是「文档 vs 真代码」——
    手抄那份一过时，测试就变成了自我确认。

    三步求法的细节见 `ledger.judge_reads` 的模块文档。
    """
    from ai_trader.kbar import ledger as LG
    return LG.judge_reads()



def _doc_block():
    if not os.path.exists(DOC):
        pytest.skip("没有维度总账文档")
    text = open(DOC, encoding="utf-8").read()
    m = re.search(r"```ledger\n(.*?)```", text, re.S)
    assert m, "总账里找不到 ```ledger 块 —— 机器可核对的那段被删了？"
    return text, m.group(1)


def _parse():
    """→ [(表, 列, 分类, 判据, 填充率, 备注)], 重复项单独返回。"""
    _text, block = _doc_block()
    rows = []
    for ln in block.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [p.strip() for p in ln.split("|")]
        assert len(parts) == 5, "总账这一行的列数不对（要 5 段）：%r" % ln
        tbl_col, cat, star, pct, note = parts
        assert "." in tbl_col, "这一行不是 表.列 的形式：%r" % tbl_col
        tbl, col = tbl_col.split(".", 1)
        rows.append((tbl, col, cat, star, float(pct), note))
    return rows


@pytest.fixture(scope="module")
def db_cols():
    if not os.path.exists(DB):
        pytest.skip("没有 data/kbar.db")
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    out = {}
    for (t,) in con.execute(
            "select name from sqlite_master where type='table' "
            "and name like 'kbar_%' order by name"):
        if t in SKIP_TABLES:
            continue
        n = con.execute("select count(*) from %s where symbol=? and "
                        "interval_ms=?" % t, (SYM, IV)).fetchone()[0]
        cols = {}
        for c in [r[1] for r in con.execute("pragma table_info(%s)" % t)]:
            k = con.execute("select count(*) from %s where symbol=? and "
                            "interval_ms=? and %s is not null" % (t, c),
                            (SYM, IV)).fetchone()[0]
            cols[c] = (100.0 * k / n) if n else 0.0
        out[t] = cols
    con.close()
    return out


def test_every_documented_row_is_unique():
    rows = _parse()
    seen = {}
    dup = []
    for r in rows:
        key = (r[0], r[1])
        if key in seen:
            dup.append(key)
        seen[key] = r
    assert not dup, "总账里有重复登记：%s" % dup


def test_no_column_in_db_is_missing_from_the_ledger(db_cols):
    """库里每一列都要登记。漏一个，后来的人就不知道它存在。"""
    documented = {(r[0], r[1]) for r in _parse()}
    actual = {(t, c) for t, cols in db_cols.items() for c in cols}
    missing = sorted(actual - documented)
    assert not missing, (
        "这些列在库里但总账没登记（加列没更新文档）：%s" % missing)


def test_no_column_in_the_ledger_is_absent_from_db(db_cols):
    """总账里不许出现库里没有的列 —— 那是幻觉列。"""
    documented = {(r[0], r[1]) for r in _parse()}
    actual = {(t, c) for t, cols in db_cols.items() for c in cols}
    extra = sorted(documented - actual)
    assert not extra, "总账登记了库里不存在的列：%s" % extra


def test_fill_rates_match_the_database(db_cols):
    """填充率必须与库一致。文档写 100% 而实际 0%，是最坏的一种骗。"""
    bad = []
    for tbl, col, cat, star, pct, note in _parse():
        real = db_cols[tbl][col]
        if abs(real - pct) > 0.15:
            bad.append("%s.%s 文档 %.1f%% 实际 %.1f%%" % (tbl, col, pct, real))
    assert not bad, "填充率漂了：\n  " + "\n  ".join(bad)


def test_starred_columns_are_exactly_what_judge_reads():
    """★ 的集合必须等于代码实际读的列。

    这条防的是「文档说判据用了 zone，代码根本没读」这种假账。
    ★ 集合不是手抄的，是从 `shape.py` 的 AST 求出来的（见 `_judge_reads`），
    所以往 `judge` 里新读一列、却没在总账标 ★，这里立刻红。
    """
    judge = _judge_reads()
    assert judge, "AST 没从 judge 求出任何维度 —— 求法坏了，不能静默通过"
    starred = {(r[0], r[1]) for r in _parse() if r[3] == "★"}
    assert starred == judge, (
        "★ 与代码不一致。\n  文档多标：%s\n  代码多读：%s"
        % (sorted(starred - judge), sorted(judge - starred)))


def test_categories_are_from_the_whitelist():
    bad = [(r[0], r[1], r[2]) for r in _parse() if r[2] not in CATS]
    assert not bad, "总账里有白名单外的分类：%s" % bad[:8]


def test_shell_columns_are_actually_empty(db_cols):
    """标成空壳的列必须真的一根都没填。填上了就该改分类。"""
    bad = []
    for tbl, col, cat, star, pct, note in _parse():
        if cat == "空壳" and db_cols[tbl][col] > 0:
            bad.append("%s.%s 标了空壳但填充率 %.1f%%"
                       % (tbl, col, db_cols[tbl][col]))
    assert not bad, "空壳列其实有值了：%s" % bad


def test_dimension_counts_in_the_doc_match_the_block():
    """正文里写的总数必须能从块里数出来 —— 正文数字不许脱离证据。

    断言**「标签 + 数值」成对**，不只断言孤立的 `**61**`：
    那个数在文档里出现两次（总数表 + 去重推导表），只钉裸数字的话
    改一处、另一处还能让测试通过（变异测试抓到过这个错）。
    """
    text, _block = _doc_block()
    rows = _parse()
    inputs = [r for r in rows if r[2] in ("价", "量", "时", "空", "反应", "情绪", "收割")]
    shells = [r for r in rows if r[2] == "空壳"]
    after = [r for r in rows if r[2] == "事后"]
    outs = [r for r in rows if r[2] == "输出"]
    book = [r for r in rows if r[2] == "簿记"]
    stars = [r for r in rows if r[3] == "★"]

    got = {"总列数": len(rows), "输入列": len(inputs), "空壳": len(shells),
           "事后": len(after), "输出": len(outs), "簿记": len(book),
           "判据用了": len(stars)}
    assert got["总列数"] == 153, "总列数变了：%d" % got["总列数"]
    assert got["输入列"] == 61, "输入列数变了：%d" % got["输入列"]
    assert got["空壳"] == 4, "空壳列数变了：%d" % got["空壳"]
    assert got["事后"] == 32, "事后列数变了：%d" % got["事后"]
    assert got["输出"] == 18, "输出列数变了：%d" % got["输出"]
    assert got["簿记"] == 38, "簿记列数变了：%d" % got["簿记"]
    assert got["判据用了"] == 8, "判据列数变了：%d" % got["判据用了"]
    # 四类相加必须等于总列数 —— 分类跑漏了在这里露出来
    assert (len(inputs) + len(shells) + len(after) + len(outs) + len(book)
            == len(rows)), "分类没覆盖全部列"

    # 正文的总数表：每一行都要「标签 + 数字」对得上
    # ⚠ 这里是**字符串列表**，不是元组列表。早先写成 `("..." % n)` 当成单元素
    # 元组用，结果 `phrase[0]` 取出的是第一个字符 "|" —— 断言恒真，
    # 变异测试（把 61 改成 62）照样全绿。别再用下标取。
    pairs = [
        "| 8 张表的总列数 | **%d** |" % got["总列数"],
        "| 其中 `t` 时刻可观测的输入列 | **%d** |" % got["输入列"],
        "| 判定层（`shape.judge`）实际用了 | **%d** |" % got["判据用了"],
        "| 空壳列 | **%d** |" % got["空壳"],
        "| 事后列（不许进表 5） | **%d** |" % got["事后"],
        "| 输出列 | **%d** |" % got["输出"],
        "| 簿记列 | **%d** |" % got["簿记"],
    ]
    for phrase in pairs:
        assert phrase in text, "正文总数表这一行对不上或有改动：%r" % phrase

    # 去重推导表的合计行
    assert "| **合计** | **%d** | **39** |" % got["输入列"] in text, \
        "去重推导表的合计行与实测输入列数（%d）对不上" % got["输入列"]


def test_ledger_is_registered_in_the_readme_index():
    """新文档要进 README 索引，否则没人知道该维护它。"""
    idx = os.path.join(ROOT, "docs", "README.md")
    if not os.path.exists(idx):
        pytest.skip("没有 docs/README.md")
    text = open(idx, encoding="utf-8").read()
    assert "K线演化维度总账.md" in text, "总账没进 docs/README.md 索引"


def test_table7_adds_no_new_dimension():
    """表 7 是描述层：它只重新投影那 41 个维度，**不许新增维度**。

    为什么要把这条钉住：`kbar_dims` 被排除在列普查之外（否则同一份维度
    会数两遍）。「排除」必须配一条反向约束 —— 否则以后往表 7 里加了列、
    真造出新维度，普查看不见，总账也看不见，维度总数就悄悄变了。
    """
    if not os.path.exists(DB):
        pytest.skip("没有 data/kbar.db")
    from ai_trader.kbar import dims as D

    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        has = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='kbar_dims'").fetchone()[0]
        if not has:
            pytest.skip("还没建表 7")
        cols = {r[1] for r in con.execute("pragma table_info(kbar_dims)")}
        row = con.execute(
            "SELECT values_json, n_dims FROM kbar_dims "
            "WHERE symbol=? AND interval_ms=? ORDER BY ts LIMIT 1",
            (SYM, IV)).fetchone()
    finally:
        con.close()

    # 物理列里不许出现新的「维度列」—— 只许这 10 个
    allowed = {"symbol", "interval_ms", "ts", "label", "n_dims", "n_filled",
               "values_json", "buckets_json", "notes_json", "dims_hash",
               "created_at"}
    assert cols == allowed, (
        "表 7 的物理列变了。若确实要加维度列，必须先改总账 §十二 并说明"
        "「这是新维度还是重新投影」。\n  多了：%s\n  少了：%s"
        % (sorted(cols - allowed), sorted(allowed - cols)))

    if row is None:
        pytest.skip("表 7 里没有这一品种的行")
    import json
    vals = json.loads(row[0])
    assert set(vals) == set(D.DIM_NAMES), (
        "表 7 存进去的维度名与 dims.DIMS 不一致：\n  多了：%s\n  少了：%s"
        % (sorted(set(vals) - set(D.DIM_NAMES)),
           sorted(set(D.DIM_NAMES) - set(vals))))
    assert int(row[1]) == len(D.DIM_NAMES), (
        "n_dims 写的是 %s，代码里是 %d 个维度（表 7 重建过吗？）"
        % (row[1], len(D.DIM_NAMES)))


def test_table8_adds_no_new_dimension():
    """表 8 是评测日志，**一个维度都不许有**。

    表 8 也被排除在列普查之外。同样要配一条反向约束：
    「评测日志」这个身份意味着它只能记凭证和计数。一旦有人往里塞
    `zone`/`vol_rel` 这种列，它就变成了「记着哪个维度赢了」的表 ——
    那正是留出集最容易被污染的地方（评测日志本身在泄露答案）。

    所以：表 8 的物理列必须与 41 个维度名**零交集**。
    """
    if not os.path.exists(DB):
        pytest.skip("没有 data/kbar.db")
    from ai_trader.kbar import dims as D

    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        has = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='kbar_holdout_log'").fetchone()[0]
        if not has:
            pytest.skip("还没建表 8")
        cols = {r[1] for r in
                con.execute("pragma table_info(kbar_holdout_log)")}
    finally:
        con.close()

    allowed = {"id", "symbol", "interval_ms", "split_version", "split_hash",
               "dims_hash", "train_n", "holdout_n", "embargo_n", "n_tested",
               "n_pass_cluster", "n_pass_block", "n_pass_naive", "n_expected",
               "z_bonferroni", "n_pass_bonf", "note", "created_at"}
    assert cols == allowed, (
        "表 8 的物理列变了。它是评测日志，不是数据表；"
        "要记新东西先改总账 §十一。\n  多了：%s\n  少了：%s"
        % (sorted(cols - allowed), sorted(allowed - cols)))

    overlap = cols & set(D.DIM_NAMES)
    assert not overlap, (
        "表 8 里出现了维度列 %s —— 评测日志不许记「哪个维度赢了」，"
        "那会让留出集本身变成答案的一部分" % sorted(overlap))


# ---------------------------------------------------------------- §三 分类表
CAT_SECTION = ("## 三、", "## 四、")


def _cat_rows():
    """解析 §三 的分类表 → [(分类, 个数, 判据数, [维度名...], ★个数), ...]。

    为什么要逐个核维度名：首版把 `src_ts`/`line_ready_ms`/`state`/`line_key`
    漏在分类之外，写成 时4/反应11/情绪2 —— 总数 41 和 ★7 都还是对的，
    总和自洽，所以**只核总数发现不了**。必须核到名字。

    维度名一律从反引号里抽，不在分隔符上做文章 ——
    §三 里 `★\\`o\\` ★\\`h\\`` 之间是空格，其余是顿号，混着来。
    """
    text = open(DOC, encoding="utf-8").read()
    m = re.search(re.escape(CAT_SECTION[0]) + r"(.*?)" + re.escape(CAT_SECTION[1]),
                  text, re.S)
    assert m, "总账里找不到 §三 分类表"
    sec = m.group(1)
    out = []
    for ln in sec.splitlines():
        ln = ln.strip()
        if not ln.startswith("|") or ln.startswith("|---") or "分类" in ln:
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) != 4:
            continue
        cat_raw, n, used, names = cells
        cat = re.sub(r"\*\*", "", cat_raw)
        if cat == "合计" or cat not in ("价", "量", "时", "空", "观察线的反应",
                                        "情绪", "收割（订单块）"):
            continue
        # 维度名：反引号里的标识符，去掉括号说明
        nm = re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", names)
        starred = names.count("★")
        out.append((cat, int(re.sub(r"\D", "", n) or 0),
                    int(re.sub(r"\D", "", used) or 0), nm, starred))
    return out


def test_each_category_lists_exactly_its_own_dimensions():
    """§三 表里：名字个数 == 写的个数；★ 个数 == 写的判据数。"""
    rows = _cat_rows()
    assert rows, "§三 分类表一条都没解析出来"
    for cat, n, used, names, starred in rows:
        assert n == len(names), (
            "%s 写「%d 个」但列了 %d 个名字：%s"
            % (cat, n, len(names), names))
        assert used == starred, (
            "%s 写「判据用 %d 个」但标了 %d 个 ★" % (cat, used, starred))


def test_category_dimensions_add_up_to_39_and_8():
    from ai_trader.kbar import dims as D

    rows = _cat_rows()
    total = sum(r[1] for r in rows)
    used = sum(r[2] for r in rows)
    assert total == 39, "§三 各分类维度数相加 = %d，应为 39" % total
    assert used == 8, "§三 各类判据数相加 = %d，应为 8" % used
    # 也不能与代码的维度总数脱节（两份文档各说各话是最难查的错）
    assert total == len(D.DIM_NAMES), (
        "§三 合计 %d 个维度，代码里 %d 个" % (total, len(D.DIM_NAMES)))
    # 名字也不能有重复（同一维度被摊到两个分类里，是另一种错）
    allnames = [x for r in rows for x in r[3]]
    dup = sorted({x for x in allnames if allnames.count(x) > 1})
    assert not dup, "§三 里同一个维度名出现在多个分类：%s" % dup
    assert len(allnames) == 39, "§三 列出的维度名共 %d 个，应为 39" % len(allnames)

    # 合计行本身也要对：只核各分类相加、不核合计行的话，
    # 把合计行的 39 改成 40 测试照样绿（变异测试抓到过）。
    # ⚠ 文档里有**两张**合计行（§二 去重推导表 61/39、§三 分类表 39/8），
    #   所以必须限定在 §三 里找，否则会匹配到 §二 那张。
    text = open(DOC, encoding="utf-8").read()
    m3 = re.search(re.escape(CAT_SECTION[0]) + r"(.*?)"
                   + re.escape(CAT_SECTION[1]), text, re.S)
    assert m3, "总账里找不到 §三"
    m = re.search(r"\|\s*\*\*合计\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|"
                  r"\s*\*\*(\d+)\*\*\s*\|", m3.group(1))
    assert m, "§三 找不到合计行"
    assert int(m.group(1)) == total, (
        "§三 合计行写 %s 个维度，各分类相加是 %d" % (m.group(1), total))
    assert int(m.group(2)) == used, (
        "§三 合计行写判据 %s 个，各分类相加是 %d" % (m.group(2), used))


def test_every_category_dimension_name_exists_as_a_column(db_cols):
    """§三 列出的每个维度名，必须真的在库里有这一列（不许编名字）。"""
    allcols = {c for cols in db_cols.values() for c in cols}
    missing = [x for r in _cat_rows() for x in r[3] if x not in allcols]
    assert not missing, "§三 列了库里不存在的维度名：%s" % missing


def test_category_starred_names_are_exactly_the_judge_columns():
    """§三 标的 ★ 维度名集合，必须等于代码实际读的那 8 个概念。"""
    judge = _judge_reads()
    concepts = {col for _tbl, col in judge}
    doc_starred = {x for r in _cat_rows() for x in r[3]
                   if x in _starred_names()}
    assert doc_starred == concepts, (
        "§三 的 ★ 与代码不一致。\n  文档多标：%s\n  代码多读：%s"
        % (sorted(doc_starred - concepts), sorted(concepts - doc_starred)))


def _starred_names():
    """§三 里有 ★ 前缀的那些名字。"""
    text = open(DOC, encoding="utf-8").read()
    m = re.search(re.escape(CAT_SECTION[0]) + r"(.*?)" + re.escape(CAT_SECTION[1]),
                  text, re.S)
    sec = m.group(1)
    out = set()
    for x in re.findall(r"★`([A-Za-z_][A-Za-z0-9_]*)`", sec):
        out.add(x)
    return out
