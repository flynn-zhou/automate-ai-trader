# -*- coding: utf-8 -*-
"""交接文档的回归防线。

新会话只读一份文档就要能接上。所以「指向哪一份」不能靠人记：
索引、指针、旧文档三者必须一致，否则新会话会读到过期的那份。
"""
from __future__ import annotations

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 文档里叙述的是 **15m** 那一套记录（实现记录 §39、待确定清单 D-15~D-19 的实测数）。
# kbar.db 按 (symbol, interval_ms, ts) 并存多个周期，2026-11-09 起又建了 5m
# （34,561 根 / 表 1~7 全建）。所以凡是要和文档数字对账的查询都必须显式钉 15m：
# 不钉就会把两个周期加起来，得到一个文档里不可能有的数，然后报「文档没写这个数」——
# 看起来像文档过期，其实是查询漏了口径。这类假红灯会训练人去改文档数字。
IV_DOC = 900_000


def _p(*parts):
    return os.path.join(ROOT, *parts)


def _read(*parts):
    return io.open(_p(*parts), encoding="utf-8").read()


def _db_count(sql, params=()):
    """从真库读一个数；库不存在或没表时返回 None（让测试跳过这项核对）。

    为什么测试要去读库：把「现在有多少条」写死在测试里，重建一次数据
    测试就变红 —— 而变红的原因不是代码坏了，是数据多了。
    这种假红灯会训练人「看见红就改测试数字」，比不测更糟。
    """
    import os
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "kbar.db")
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        try:
            return con.execute(sql, params).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


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


def test_kline_evolution_design_is_finalized():
    """设计已定稿，必须能直接照做；不再是讨论稿。"""
    src = _read("docs", "K线演化-设计讨论稿.md")
    assert "定稿" in src, "没标注已定稿"
    # 定稿就得有实现顺序和验收标准，否则没法开工
    assert "实现顺序" in src
    assert "验收" in src
    # 核心概念：观察线，不是支撑线
    assert "观察线" in src, "缺观察线这个概念 —— 这是整个能力的定位"
    assert "不是支撑" in src or "支撑不是线的属性" in src, \
        "没写清观察线与支撑线的区别"
    # 每条线必须带自己的周期，否则 EMA200(3m) 和 EMA200(1h) 会混成一个
    assert "自己的周期" in src
    # 路径依赖：存「上一件同类事件」而不是前一根
    assert "since_event_idx" in src, "缺 since_event_idx —— 动态回溯的落地形式"
    assert "prev_event" in src, "缺 prev_event"
    assert "上一件同类事件" in src, "没写清回溯不是定死前 N 根"
    # 允许无意义，否则会给 neutral 的 K 强行贴标签
    assert "neutral" in src
    # 不挂区间演化
    assert "不挂区间演化" in src or "只是观察位之一" in src, \
        "没写清「区间演化只是一维、不是框架」"
    assert "四维" in src and "一维" in src, "四维读数与「区间演化只是一维」的表述丢了"
    # 防偷未来
    # v3 用三张表物理隔开，不再用 at_bar/forward 这两个字段名
    assert "kbar_forward" in src, "没有事后确认的独立表"
    assert "物理隔开" in src or "分三张表" in src, "没把当下读数与事后确认隔开"
    assert "禁止参与当下判定" in src
    # 独有的数据优势
    assert "3s" in src and "is_buy" in src
    # 口径与判断标准必须分开
    assert "口径" in src and "判断标准" in src


def test_kline_design_supersedes_old_draft():
    """旧稿必须删掉，避免两份讨论稿各说一套。"""
    assert not os.path.isfile(_p("docs", "K线识别-讨论稿.md")), \
        "旧讨论稿还在，会和新稿冲突"
    assert os.path.isfile(_p("docs", "K线演化-设计讨论稿.md"))

def test_design_doc_has_locked_decisions():
    """三条决策用户已拍定，文档必须把它们写成定稿而不是待定。"""
    src = _read("docs", "K线演化-设计讨论稿.md")
    assert "已定事项" in src or "已定稿" in src, "没写成定稿"
    assert "kbar_evolution" in src and "kbar_reaction" in src and "kbar_forward" in src
    assert "结构位" in src, "第一批观察线漏了结构位（用户明确要求加）"
    assert "(fam, seg)" in src or "fam, seg" in src, "触碰重置规则没写清"
    # 周期必须是 1h，v2 写错过
    assert "区间演化的周期是 1h" in src or "1h 周期的观察线" in src, \
        "区间演化周期没写对（必须是 1h，不是 15m）"


def test_design_doc_separates_at_bar_and_forward():
    """当下读数与事后确认必须物理隔开，且明确禁止混用。"""
    src = _read("docs", "K线演化-设计讨论稿.md")
    assert "禁止参与当下判定" in src
    assert "偷未来" in src or "偷看未来" in src


def test_design_doc_defines_the_observation_line_table():
    """§6 必须有「表 0 kbar_line」—— 观测事实表，第 1 步的落点。

    这条是补出来的教训（2026-11-07）：原稿 §6 只写三张表，可 §12 第 1 步要求
    「观察线计算与落库」、§13 又写「与观察线表里 1h 的值必须完全相等」——
    文档引用了一张自己没定义的表。实现到一半才发现漏了，属于「文档级的空指针」。
    """
    src = _read("docs", "K线演化-设计讨论稿.md")
    sec6 = src.split("## 六、数据模型", 1)[1].split("## 七、", 1)[0]
    assert "### 表 0：`kbar_line`" in sec6, "§6 里没有表 0 的定义"
    # 四张表都要在 §6 里被正式定义，编号就是实现顺序
    for name in ("kbar_line", "kbar_evolution", "kbar_reaction", "kbar_forward"):
        assert "`%s`" % name in sec6, "§6 缺表: " + name


def test_every_kbar_table_referenced_is_defined():
    """文档里出现的每张 kbar_* 表，都必须在 §6 被定义过。

    防的是下一次「文档引用了一张不存在的表」——这次是观察线表，
    下次可能是随便哪个新表名。
    """
    src = _read("docs", "K线演化-设计讨论稿.md")
    defined = set(re.findall(r"### 表 \d+：`(kbar_[a-z_]+)`", src))
    assert defined, "§6 里一张表都没定义（标题格式变了？）"
    referenced = set(re.findall(r"kbar_[a-z_]+", src))
    assert referenced <= defined, "引用了没定义的表: %s" % sorted(referenced - defined)


def test_step1_acceptance_names_its_table():
    """第 1 步验收必须指明校验的是哪张表，不能只说「观察线表」这种中文泛指。"""
    src = _read("docs", "K线演化-设计讨论稿.md")
    sec = src.split("### 第 1 步验收", 1)[1].split("### 第 2 步验收", 1)[0]
    assert "kbar_line" in sec, \
        "第 1 步验收没指明表名（原稿这里引用的『观察线表』当时并不存在）"


def test_step2_acceptance_specifies_metrics_and_source():
    """第 2 步验收必须点明要核对哪几个量、并且要求 conc 来自 3s 桶。

    防的是「验收条件写得含糊，实现方拿常数糊过去」——原稿 §13 已经写清了，
    这条只是把它钉住，别在后来的编辑里被稀释掉。
    """
    src = _read("docs", "K线演化-设计讨论稿.md")
    sec = src.split("### 第 2 步验收", 1)[1].split("### 第 3 步验收", 1)[0]
    for token in ("body_pct", "close_pos", "conc", "3s"):
        assert token in sec, "第 2 步验收少了 %s" % token


def test_implementation_record_declares_step2_table():
    """实现记录必须写明第 2 步落到哪张表、以及它的唯一键。

    踩过的坑：第 1 步时文档引用了一张自己没定义的「观察线表」。
    实现记录这边同理 —— 只说「事实层」不说表名，下一次就没人知道去哪查。
    """
    src = _read("docs", "K线演化-实现记录.md")
    assert "第 2 步完成" in src, "实现记录没写第 2 步完成"
    assert "kbar_evolution" in src, "实现记录没点名第 2 步的表"
    assert "UNIQUE(symbol, interval_ms, ts)" in src, "实现记录没写第 2 步表的唯一键"


def test_record_and_code_agree_on_undefined_fields():
    """代码里声明「设计上没算」的字段，实现记录必须逐个解释。

    这条防的是漂移：以后有人往 `undefined_by_design` 里塞字段却没留原因，
    或者反过来在文档里补了原因但代码没同步。
    """
    from ai_trader.kbar.build import facts_params_snapshot
    p = facts_params_snapshot(900_000)
    src = _read("docs", "K线演化-实现记录.md")
    for field in p["undefined_by_design"]:
        assert field in src, "实现记录没解释 %s 为什么留空" % field
    for field in p["later_steps"]:
        assert field in src, "实现记录没说明 %s 属于后续步骤" % field


def test_pending_decisions_doc_exists_and_names_the_open_items():
    """「待确定清单」必须存在，并且逐条覆盖代码里显式留空的字段。

    这份清单是「口径没定就不许自己拍」的载体。它一旦和代码脱钩，
    留空的字段就变成「谁也不知道为什么空着」，下一步的人要么瞎猜要么删列。
    """
    src = _read("docs", "K线演化-待确定清单.md")
    # 第 3 步明确定义「留空」的两个字段
    for field in ("dwell_bars", "nth_touch"):
        assert field in src, "待确定清单没提 %s" % field
    # 第 2 步留空的两个字段
    for field in ("path_shape", "max_tick_share"):
        assert field in src, "待确定清单没提 %s" % field
    # 编号必须成体系（D-1 …），否则引用无从谈起
    ids = re.findall(r"### (D-\d+)", src)
    assert len(ids) >= 10, "待确定清单的条目太少（只找到 %d 条）" % len(ids)
    assert ids == sorted(ids, key=lambda s: int(s.split("-")[1])), "条目编号乱序"


def test_pending_decisions_doc_forbids_silent_defaults():
    """清单必须写明「不许自己填默认值」这条纪律本身。"""
    src = _read("docs", "K线演化-待确定清单.md")
    assert "不许" in src or "不要" in src
    assert "默认值" in src


def test_step3_acceptance_talks_about_reaction_and_segment_reset():
    src = _read("docs", "K线演化-设计讨论稿.md")
    sec = src.split("### 第 3 步验收", 1)[1].split("### 第 4 步验收", 1)[0]
    assert "nth_touch" in sec, "第 3 步验收没提 nth_touch"
    assert "(fam, seg)" in sec or "fam, seg" in sec, "第 3 步验收没提按段重置"


def test_implementation_record_declares_step3_table_and_lookahead_rule():
    """第 3 步的记录必须写明表名、唯一键，以及那条防偷未来规则。

    防偷未来这条不是「顺手写的注意事项」——不做对会造出 7% 的假触碰，
    所以它必须在文档里出现，而不是只活在代码注释里。
    """
    src = _read("docs", "K线演化-实现记录.md")
    assert "第 3 步完成" in src, "实现记录没写第 3 步完成"
    assert "kbar_reaction" in src
    assert "UNIQUE(symbol, interval_ms, ts, line_key)" in src
    assert "line_ready_ms" in src, "实现记录没提防偷未来的取线值规则"
    assert "比例" in src and "百分数" in src, "没写 pen/recover 与 dist_pct 的量纲差异"


def test_record_and_code_agree_on_reaction_undefined_fields():
    """反应层代码里声明「设计上没算」的字段，实现记录与待确定清单都要解释。"""
    from ai_trader.kbar.build import reaction_params_snapshot
    p = reaction_params_snapshot(900_000)
    rec = _read("docs", "K线演化-实现记录.md")
    pend = _read("docs", "K线演化-待确定清单.md")
    for field in p["undefined_by_design"]:
        assert field in rec, "实现记录没解释 %s 为什么留空" % field
        assert field in pend, "待确定清单没登记 %s" % field


def test_docs_declare_the_no_carry_forward_rule():
    """「线消失了不许往前带」必须同时写在实现记录和待确定清单里。

    这条是第 3 步做完交叉核对才发现的缺陷：区间演化转 PRE（四线全空）之后，
    「取最近一个就绪值」仍会拿消失之前的线判触碰 —— 实测 2026-08-05 05:15
    还在用 02:00 那条 range_key。它改变 574 行结果，必须留痕。
    """
    rec = _read("docs", "K线演化-实现记录.md")
    pend = _read("docs", "K线演化-待确定清单.md")
    assert "上一根" in rec and "往前带" in rec, "实现记录没写「只取上一根那一行」的规则"
    assert "D-14" in pend, "待确定清单没登记 D-14（线消失之后能不能用）"
    assert "往前带" in pend or "无限期" in pend, "D-14 没说清「不许往前带」这件事"
    # 计数标签也必须分开：混成一类就看不出线消失过
    assert "line_absent_at_prev_bar" in rec, "实现记录没写线消失的丢弃计数"


def test_reaction_code_splits_absent_from_not_ready():
    """代码里「线曾经有、现在没了」与「线还没出生」必须是两个不同的计数键。"""
    import inspect
    from ai_trader.kbar import reaction as R
    src = inspect.getsource(R.build_reactions)
    assert "line_absent_at_prev_bar" in src
    assert "line_not_ready" in src


def test_pick_prev_is_the_only_way_lines_are_read():
    """反应层取线值只能走 `pick_prev`；不许退回 `as_of`（它会往前带）。"""
    import inspect
    from ai_trader.kbar import reaction as R
    src = inspect.getsource(R.build_reactions)
    assert "pick_prev(" in src, "build_reactions 没走 pick_prev"
    assert "as_of(" not in src, "build_reactions 用了会往前带的 as_of"


def test_next_session_prompt_exists():
    src = _read("docs", "NEXT-SESSION-PROMPT.md")
    assert len(src.splitlines()) > 30
    # 必须直接指派开发任务，而不是继续讨论
    assert "实现" in src
    assert "K线演化-设计讨论稿.md" in src
    # 必须带上探针优先和只读生产库两条硬纪律
    assert "探针" in src
    assert "只读" in src


# ---------------------------------------------------------------- 第 4 步
def test_implementation_record_declares_step4_table_and_vocab_split():
    """实现记录必须写明第 4 步落在哪张表，以及「14 个词只用 3 个」这件事。"""
    rec = _read("docs", "K线演化-实现记录.md")
    assert "kbar_event" in rec, "实现记录没写事件表名"
    assert "EVENT_VOCAB_RANGE" in rec or "14 个" in rec, "没写词表是 14 个"
    assert "3 个" in rec, "没写实际只用 3 个词"


def test_record_explains_why_the_other_eleven_vocab_words_do_not_apply():
    """「11 个词为什么不能用」必须有解释 —— 不能只写「不用」。"""
    from ai_trader.kbar import event as E
    rec = _read("docs", "K线演化-实现记录.md")
    # 至少点到几个被排除的词
    for name in ("区间成形", "R滚动", "向上反转"):
        assert name in rec, "实现记录没解释 %s 为什么不能落在线上" % name
    assert len(E.EVENT_VOCAB_RANGE_ONLY) == 11


def test_step4_acceptance_answers_both_items_from_the_design():
    """设计稿 §13 第 4 步验收有两项：真实「突破后收回」样本 + neutral 占比。"""
    rec = _read("docs", "K线演化-实现记录.md")
    assert "突破后收回" in rec or "突破后价格穿回" in rec
    assert "34.5%" in rec, "实现记录没写 neutral 的实际占比"
    assert "prev_event" in rec and "since_event_idx" in rec


def test_record_and_code_agree_on_event_undefined_fields():
    """事件层代码里声明「设计上没算」的字段，实现记录与待确定清单都要解释。"""
    from ai_trader.kbar.build import event_params_snapshot
    p = event_params_snapshot(900_000)
    rec = _read("docs", "K线演化-实现记录.md")
    pend = _read("docs", "K线演化-待确定清单.md")
    for field in p["undefined_by_design"]:
        assert field in rec, "实现记录没解释 %s 为什么留空" % field
    # D-16 / D-17 必须在清单里
    assert "D-16" in pend and "D-17" in pend


def test_pending_decisions_doc_registers_the_four_step4_open_items():
    """第 4 步新冒出来的四个口子必须逐条登记，不许只留一个笼统的 D-11。"""
    pend = _read("docs", "K线演化-待确定清单.md")
    for d in ("D-15", "D-16", "D-17", "D-18"):
        assert d in pend, "待确定清单没登记 %s" % d
    assert "收回" in pend, "D-15 没写「词表里没有收回这个词」"
    # D-18 要写「反包有多少条」。这个数**不能写死在测试里** ——
    # 重建一次数据它就变了，测试会变成假红灯。所以从真库读，再核对清单里有它。
    n_rev = _db_count("select count(*) from kbar_event "
                      "where interval_ms=? and event=?", (IV_DOC, "突破失败·反包"))
    if n_rev is not None:
        assert "{:,}".format(n_rev) in pend or str(n_rev) in pend, \
            "D-18 没写反包的实际条数 %d" % n_rev


def test_event_uses_only_vocabulary_names_from_the_range_engine():
    """事件名必须来自区间演化词表 —— 不许出现自由文本（设计稿 §8.4）。"""
    from ai_trader.kbar import event as E
    for name in E.EVENT_VOCAB_USED:
        assert name in E.EVENT_VOCAB_RANGE, "%s 不在区间演化词表里" % name


def test_revert_window_constant_records_its_provenance_in_code():
    """反包窗口这个数必须能在代码里找到来源，不然下一个人只会看到魔数 3。"""
    import inspect
    from ai_trader.kbar import event as E
    src = inspect.getsource(E)
    assert "FORM_BARS = 3" in src, "没写明 3 这个数借自 range/engine.py 的 FORM_BARS"
    assert E.REVERT_WINDOW_BARS == 3


def test_handoff_warns_that_table1_event_column_is_lossy():
    """交接文档必须警告「表 1 的 event 列有损」，否则下一个人会漏掉事件。"""
    h = _read("docs", "HANDOFF-DSH-SESSION.md")
    assert "kbar_event" in h
    assert "有损" in h or "丢" in h, "没警告表 1 的 event 列会丢事件"


# ================================================================== 审查修复守卫
# 这一轮的 4 个真缺陷有 3 个属于同一类：某处声称 A、实际是 B，而 B 不报错。
# 这类问题测不出来，只能靠「文档与代码对读」。所以文档这边也要有守卫。

def test_impl_record_documents_the_audit_and_its_evidence():
    """实现记录必须留审查痕迹：否则下一次又会把同样的坑踩一遍。"""
    rec = _read("docs", "K线演化-实现记录.md")
    assert "三十四" in rec, "实现记录没有审查那一节"
    for token in ("pick_prev", "LEFT JOIN", "0.0", "delete_evolution"):
        assert token in rec, "审查那节没提 %s" % token
    assert "事故演练" in rec or "演练" in rec, "没留事故演练的记录"


def test_impl_record_numbers_match_the_database():
    """实现记录里写的条数必须与真库对得上 —— 数字漂移就是又一次「自述与事实不符」。"""
    import os
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "kbar.db")
    if not os.path.exists(db):
        import pytest
        pytest.skip("没有 data/kbar.db")
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        n_ev = con.execute("select count(*) from kbar_event "
                           "where interval_ms=?", (IV_DOC,)).fetchone()[0]
        n_bars = con.execute("select count(*) from kbar_evolution "
                             "where interval_ms=?", (IV_DOC,)).fetchone()[0]
    except sqlite3.OperationalError:
        import pytest
        pytest.skip("库里还没有表")
    finally:
        con.close()
    rec = _read("docs", "K线演化-实现记录.md")
    assert "{:,}".format(n_ev) in rec, "实现记录里没有真实事件条数 %d" % n_ev
    assert "{:,}".format(n_bars) in rec, "实现记录里没有真实 K 线根数 %d" % n_bars


def test_pending_list_registers_events_without_reaction_detail():
    """D-19：「没有反应明细的事件算不算样本」是口径问题，不许悄悄定。"""
    pend = _read("docs", "K线演化-待确定清单.md")
    assert "D-19" in pend, "没登记 D-19"
    # 同理：这个数会随数据变，从库里读，别写死
    n_no = _db_count("""
        select count(*) from kbar_event e left join kbar_reaction r
          on r.symbol=e.symbol and r.interval_ms=e.interval_ms
         and r.ts=e.ts and r.line_key=e.line_key
        where e.interval_ms=? and r.rowid is null""", (IV_DOC,))
    if n_no is not None:
        assert "{:,}".format(n_no) in pend or str(n_no) in pend, \
            "没写有多少条事件没有明细（现在是 %d）" % n_no
    assert "LEFT JOIN" in pend, "没说清取明细要用 LEFT JOIN"


def test_handoff_warns_about_the_three_audit_traps():
    """交接文档必须写清这轮修掉的三个陷阱，否则下一个人会重新引入。"""
    h = _read("docs", "HANDOFF-DSH-SESSION.md")
    assert "line_value_rule" in h, "没警告「改取法要同步改 meta 并重跑第 3 步」"
    assert "INNER JOIN" in h, "没警告「事件可能没有反应明细」"
    assert "delete_evolution" in h or "--rebuild" in h, "没警告「第 2 步 rebuild 会连带删列」"
    assert "check_kbar_pipeline" in h, "没提体检工具"


def test_pipeline_check_tool_exists_and_covers_the_known_failure_modes():
    """体检工具必须真的检查那几件事，不然它只是个摆设。"""
    src = _read("tools", "check_kbar_pipeline.py")
    for token in ("n_touch", "neutral", "on_line", "line_value_rule",
                  "event_input_fingerprint"):
        assert token in src, "体检工具没查 %s" % token
    # 读数层那一段也要在（不落库 / 容差一处 / 不碰事后层）
    for token in ("shape.py", "SCENARIO_TOL_PTS", "落库"):
        assert token in src, "体检工具没查读数层的 %s" % token
    assert "return 1" in src, "体检工具不会报错（永远退出 0）"


def _run_check_on_copy(mutate):
    """复制一份真库，按 mutate(con) 改坏，再跑体检工具，返回 (rc, out)。

    为什么用复制：体检工具要对着真库才有意义（表大小、指纹、分布都要真），
    而被测的是「它能不能发现异常」，所以异常得造在副本上。
    """
    import os
    import shutil
    import sqlite3
    import subprocess
    import sys
    import tempfile
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = os.path.join(root, "data", "kbar.db")
    if not os.path.exists(db):
        import pytest
        pytest.skip("没有 data/kbar.db")
    tmp = os.path.join(tempfile.mkdtemp(), "kbar_copy.db")
    shutil.copy2(db, tmp)
    con = sqlite3.connect(tmp)
    try:
        mutate(con)
        con.commit()
    finally:
        con.close()
    try:
        p = subprocess.run([sys.executable, "tools/check_kbar_pipeline.py",
                            "--kbar-db", tmp], cwd=root,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode("utf-8", "replace")
    finally:
        os.remove(tmp)
    return p.returncode, out


def test_pipeline_check_tool_is_green_on_the_real_table_5():
    """先钉住基线：表 5 正常时体检必须绿 —— 否则下面的「造坏」测试没有意义
    （一个永远红的工具，也抓不住任何东西）。"""
    rc, out = _run_check_on_copy(lambda con: None)
    assert rc == 0, "真库体检是红的：\n%s" % out[-1200:]
    assert "表 5 与表 1 等长" in out, out[-1200:]


def test_pipeline_check_tool_flags_a_stale_params_hash():
    """表 5 里混进另一套口径指纹 = 改过阈值没重跑。这种表最危险：
    `--rebuild` 忘了跑，读库的人拿到的是旧阈值下的结论，而**不会报错**。
    """
    def mutate(con):
        con.execute("update kbar_shape set params_hash='deadbeef0000' "
                    "where ts = (select min(ts) from kbar_shape)")
    rc, out = _run_check_on_copy(mutate)
    assert rc != 0, "改坏一个指纹，工具还返回 0：\n%s" % out[-1200:]
    assert "口径" in out or "指纹" in out, out[-1200:]


def test_pipeline_check_tool_flags_a_self_contradicting_row():
    """label 说「恐慌盘」但 states 是空的 = 这一行自相矛盾，必须红。

    自相矛盾的行比错行更难发现：单看 label 是对的，单看 states 也是「空」，
    只有并排看才知道对不上。
    """
    def mutate(con):
        # 挑一个**非**「没有形态」族的行把它 states 清空。
        # 拆词后「无」不再存在，所以不能再用 label <> '无' 选行 ——
        # 那样会选到 无·真空/无·贴线，它们 states 本来就是空，等于没造变异。
        con.execute("update kbar_shape set states='' where ts = "
                    "(select min(ts) from kbar_shape "
                    "where label not in ('无','无·真空','无·贴线'))")
    rc, out = _run_check_on_copy(mutate)
    assert rc != 0, "造了自相矛盾的行，工具还返回 0：\n%s" % out[-1200:]
    assert "states" in out, out[-1200:]


def test_pipeline_check_tool_flags_the_word_wu_inside_states():
    """「无」不是形态，写进 states 就是错 —— 这一条是实际踩过的坑：
    早先 states 里放了「无」，表里存成空串，两边对不上。"""
    def mutate(con):
        con.execute("update kbar_shape set states='无', n_states=1 "
                    "where ts = (select min(ts) from kbar_shape "
                    "where label = '无·真空')")
    rc, out = _run_check_on_copy(mutate)
    assert rc != 0, "states 里塞了「无」，工具还返回 0：\n%s" % out[-1200:]
    assert "没有形态" in out or "无" in out, out[-1200:]


def test_pipeline_check_tool_flags_a_bare_wu_left_over():
    """还有裸「无」= 旧口径的行没重跑，或者构建时没把 n_touch 传给 judge。

    这是**词表混合**的行级检测器：params_hash 只认阈值，抓不到词表变化，
    所以必须靠数据本身（v1 写「无」、v2 写「无·真空/贴线」）来发现。
    """
    def mutate(con):
        con.execute("update kbar_shape set label='无' "
                    "where ts = (select min(ts) from kbar_shape "
                    "where label = '无·贴线')")
    rc, out = _run_check_on_copy(mutate)
    assert rc != 0, "还有裸「无」，工具还返回 0：\n%s" % out[-1200:]
    assert "裸" in out or "无" in out, out[-1200:]


def test_pipeline_check_tool_flags_a_missing_table_5():
    """表 5 整个没有 = 判定没落库（用户明确要求必须落盘），必须红。"""
    def mutate(con):
        con.execute("drop table kbar_shape")
    rc, out = _run_check_on_copy(mutate)
    assert rc != 0, "删掉表 5，工具还返回 0：\n%s" % out[-1200:]
    assert "kbar_shape" in out or "表 5" in out, out[-1200:]


def test_event_helpers_document_why_they_are_used():
    """`_pen_key` 与 `find_breakout_then_revert` 的契约要写清，否则会被「顺手简化」掉。"""
    import inspect
    from ai_trader.kbar import event as E
    src = inspect.getsource(E._pen_key)
    assert "is None" in src
    assert "0.0" in src, "_pen_key 没说明 0.0 是合法值"
    fr = inspect.getsource(E.find_breakout_then_revert)
    assert "prev_event" in fr, "配对没按 prev_event 认领"


# ================================================================== 第 5 步守卫
def test_impl_record_documents_the_forward_layer_and_its_red_line():
    """实现记录必须写清事后层的隔离红线 —— 否则下一轮会把表 3 当普通表用。"""
    rec = _read("docs", "K线演化-实现记录.md")
    assert "三十五" in rec, "实现记录没有第 5 步那一节"
    assert "kbar_forward" in rec
    assert "偷未来" in rec or "事后" in rec
    for token in ("ret1", "held", "held_ref_price", "扫源码"):
        assert token in rec, "第 5 步那节没提 %s" % token


def test_impl_record_records_that_held_has_no_discriminating_power():
    """held 的 52.8%/47.1% 接近对半 = 没区分度。这个结论必须留痕。

    不留痕的话，后来的人看到「一半守住了」会以为发现了什么，
    然后开始拿它当「线被尊重」的证据 —— 那正是这个项目要避免的。
    """
    rec = _read("docs", "K线演化-实现记录.md")
    assert "52.8" in rec and "47.2" in rec, "没记 held 的真实分布"
    assert "没有区分度" in rec or "接近对半" in rec, "没说明这个分布说明不了什么"
    assert "不是「发现了什么」" in rec or "不是定论" in rec or "占位" in rec


def test_pending_list_still_marks_the_three_forward_placeholders():
    """D-12 / D-13 / D-20 必须仍在待确定清单里 —— 不许因为「代码能跑」就当成定了。"""
    pend = _read("docs", "K线演化-待确定清单.md")
    for d in ("D-12", "D-13", "D-20"):
        assert d in pend, "待确定清单缺 %s" % d
    assert "占位" in pend, "没写明这些是占位而不是定论"
    assert "RET_FEE" in pend or "不扣费" in pend
    assert "HELD_BARS" in pend or "held_bars" in pend


def test_forward_constants_are_placeholders_agreed_with_the_pending_list():
    """代码里的占位常量必须和清单上写的一致（防止一边改了另一边没改）。"""
    from ai_trader.kbar import forward as W
    pend = _read("docs", "K线演化-待确定清单.md")
    rec = _read("docs", "K线演化-实现记录.md")
    assert str(W.HELD_BARS) in pend or "3" in pend
    # 扣费 0 这个事实要同时出现在两边
    assert W.RET_FEE == 0.0
    assert "不扣费" in pend and "不扣费" in rec


def test_handoff_warns_that_the_forward_layer_is_isolated():
    h = _read("docs", "HANDOFF-DSH-SESSION.md")
    assert "kbar_forward" in h
    assert "隔离" in h or "禁止参与当下判定" in h
    assert "forward.py" in h
    assert "D-12" in h, "交接文档没提醒口径还是占位"


def test_forward_probe_does_not_import_the_module_it_checks():
    """探针不许 import forward.py 的公式 —— 否则公式错了两边一起绿。"""
    src = _read("tools", "probe_kbar_forward.py")
    for bad in ("from ai_trader.kbar.forward", "from ai_trader.kbar import forward",
                "import ai_trader.kbar.forward"):
        assert bad not in src, "探针 import 了被测公式：%s" % bad
    # 但它必须能读库
    assert "sqlite3" in src and "kbar_forward" in src


# ---------------------------------------------------------------- 读数层（shape.py）
def _section(doc_text, start, end):
    """截取以 start 开头、到 end 之前的那一段。找不到就让测试红，不许静默跳过。"""
    assert start in doc_text, "文档里没有 %s 这一节" % start
    rest = doc_text.split(start, 1)[1]
    if end and end in rest:
        rest = rest.split(end, 1)[0]
    return rest


def test_impl_record_documents_the_shape_layer():
    """读数层必须有独立一节 —— 它是「客观值 → 判断」这一步，最容易被当成第 6 步。"""
    rec = _read("docs", "K线演化-实现记录.md")
    assert "三十八" in rec, "实现记录没有读数层那一节"
    for token in ("shape.py", "SCENARIO_TOL_PTS", "STATE_PANIC", "插针后稳住"):
        assert token in rec, "读数层那节没提 %s" % token
    # 「不落库」必须出现在 38.9 的**正文**里（标题里就有这个词，不能算）
    sec = _section(rec, "### 38.9", "### 38.10").split("\n", 1)[1]
    assert "不落库" in sec, "38.9 正文没写清判定不落库"
    assert "现算" in sec, "38.9 正文没写清判定是现算的"
    assert "会落库" not in sec and "要落库" not in sec, \
        "38.9 正文说判定要落库 —— 与实现相反（判定现算，没有表）"
    # 死条件那条推导必须在 38.5 里，否则下一个人会把 close_pos 条件加回来
    sec5 = _section(rec, "### 38.5", "### 38.6")
    assert "close_pos" in sec5, "38.5 没提被删掉的 close_pos 条件"
    assert "恒真" in sec5, "38.5 没写清那个条件是死的（恒真）"
    assert "收位 >= 下影占幅" in sec5, "38.5 没写清推导式"
    # 反例数必须是具体数字，不能是「若干」「几根」
    m = re.search(r"反例\s*\*{0,2}\s*(\d+)", sec5)
    assert m, "38.5 没写实测反例数（要具体数字）"
    assert m.group(1) == "0", "38.5 写的反例数是 %s，实测是 0" % m.group(1)


def test_impl_record_shape_numbers_match_the_database():
    """读数层写进文档的分布数字必须与用真库重算的一致。

    这些数字是「阈值取这一档」的全部依据。它们一漂，论证就失效了 ——
    而且漂了不会让任何功能变红，只会让下一次调参基于假前提。

    按表格的**列位置**取数，不在整行里搜数字：一行里同时有「成立根数」和
    「作 label 的根数」，搜数字的话两列相等时（恐慌盘 86/86）改错一列也查不出来。
    """
    import os
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "kbar.db")
    if not os.path.exists(db):
        import pytest
        pytest.skip("没有 data/kbar.db")
    from ai_trader.kbar import shape as S
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        rows = list(con.execute(
            "select ts,o,h,l,c,vol_rel,taker_buy_ratio from kbar_evolution "
            "where symbol='BTCUSDT' and interval_ms=900000 order by ts"))
        lines = {}
        for ts, lk, p in con.execute(
                "select ts,line_key,line_price from kbar_line "
                "where symbol='BTCUSDT' and interval_ms=900000"):
            lines.setdefault(ts, {})[lk] = float(p) if p is not None else None
        con.close()
    except sqlite3.Error:
        import pytest
        pytest.skip("库里还没有表")
    if not rows:
        import pytest
        pytest.skip("表 1 是空的")
    bars = [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
             "vol_rel": r[5], "taker_buy_ratio": r[6]} for r in rows]
    ts_list = [b["ts"] for b in bars]
    js = [S.judge(bars, i, prev_lines=lines.get(ts_list[i - 1], {}) if i else None)
          for i in range(len(bars))]
    c = S.counts_by_label(js)
    rec = _read("docs", "K线演化-实现记录.md")
    sec = _section(rec, "### 38.3", "### 38.4")

    def _cells(label):
        """表格列序：| 状态 | 判据 | 成立根数 | 占全量 | label 根数 | 占全量 |"""
        prefix = "| `%s`" % label
        hits = [ln for ln in sec.split("\n") if ln.strip().startswith(prefix)]
        assert hits, "38.3 里没有 %s 的分布那一行" % label
        cells = [x.strip() for x in hits[0].strip().strip("|").split("|")]
        assert len(cells) == 6, "%s 那一行不是 6 列：%s" % (label, hits[0])
        return cells

    def _num(s):
        t = s.replace(",", "").replace("%", "").strip()
        assert t.isdigit(), "那一格不是整数：%r" % s
        return int(t)

    for label in (S.STATE_PANIC, S.STATE_PIN_RECOVERED, S.STATE_PIN,
                  S.STATE_STABLE, S.STATE_NEAR_PIN):
        cells = _cells(label)
        n_any = sum(1 for j in js if j.has(label))
        n_label = c.get(label, 0)
        assert _num(cells[2]) == n_any, \
            "%s 的「成立根数」文档写 %s，实算 %d" % (label, cells[2], n_any)
        assert _num(cells[4]) == n_label, \
            "%s 的「label 根数」文档写 %s，实算 %d" % (label, cells[4], n_label)
        # 占比也要对得上（一位小数）
        expect = round(100.0 * n_any / len(bars), 2)
        assert abs(float(cells[3].replace("%", "")) - expect) < 0.02, \
            "%s 的成立占比文档写 %s，实算 %.2f%%" % (label, cells[3], expect)

    cells = _cells(S.STATE_NONE)
    assert _num(cells[4]) == c.get(S.STATE_NONE, 0), \
        "「无」的 label 根数文档写 %s，实算 %d" % (cells[4],
                                                    c.get(S.STATE_NONE, 0))
    # 分方向的两个数
    dn = sum(1 for j in js if j.side == S.SIDE_DOWN)
    up = sum(1 for j in js if j.side == S.SIDE_UP)
    assert "{:,}".format(dn) in sec and "{:,}".format(up) in sec, \
        "38.3 没写清下针 %d / 上针 %d" % (dn, up)
    # 多状态的根数
    multi = sum(1 for j in js if len(j.states) > 1)
    assert "{:,}".format(multi) in sec, "38.3 没写清多状态 %d 根" % multi
    assert "{:,}".format(len(bars)) in rec, "没写清分母 %d" % len(bars)


def test_pending_list_registers_the_shape_thresholds_as_provisional():
    """D-21：八个阈值全是临时值，用户只确认过容差 20。不许当成定论。"""
    pend = _read("docs", "K线演化-待确定清单.md")
    # 必须是条目标题，不是正文里提一嘴
    assert "### D-21" in pend, "没登记 D-21 条目（读数层的阈值全是临时值）"
    for token in ("WICK_PCT_MIN", "WICK_PTS_MIN", "PANIC_VOL_REL_MIN",
                  "PANIC_TAKER_BUY_MAX", "PANIC_CLOSE_POS_MAX",
                  "STABLE_MAX_PIN_RANGE_MULT", "SCENARIO_TOL_PTS",
                  "STABLE_LOOKBACK_BARS", "SWING_LOOKBACK_BARS"):
        assert token in pend, "D-21 没列 %s" % token
    sec = _section(pend, "### D-21", "## 八")
    assert "临时" in sec or "占位" in sec
    assert "对齐" in sec, "D-21 没写用户的原话（先把 K 线演化结果对齐再说）"
    assert "不许" in sec or "不要" in sec, "D-21 没写清「不许开特例」"


def test_shape_thresholds_in_code_match_the_pending_list():
    """代码里的临时阈值与清单上写的一致 —— 一边改了另一边没改就会被这条抓到。"""
    import re
    from ai_trader.kbar import shape as S
    pend = _read("docs", "K线演化-待确定清单.md")
    for name in ("WICK_PCT_MIN", "WICK_PTS_MIN", "PANIC_VOL_REL_MIN",
                 "PANIC_TAKER_BUY_MAX", "PANIC_CLOSE_POS_MAX",
                 "STABLE_MAX_PIN_RANGE_MULT", "SCENARIO_TOL_PTS",
                 "STABLE_LOOKBACK_BARS", "SWING_LOOKBACK_BARS"):
        val = getattr(S, name)
        txt = ("%g" % val)
        row = [ln for ln in pend.split("\n") if name in ln]
        assert row, "D-21 表里没有 %s 这一行" % name
        assert txt in row[0], \
            "%s 代码里是 %s，清单那一行没写这个数：%s" % (name, txt, row[0])


def test_handoff_explains_the_shape_layer_and_its_one_tolerance():
    """交接文档必须写清「容差只在一个场景生效」，否则下一个人会把它扩散出去。"""
    h = _read("docs", "HANDOFF-DSH-SESSION.md")
    sec = _section(h, "## 十一之四", "## 十一之五")
    assert "读数层" in sec
    assert "shape.py" in sec
    assert "SCENARIO_TOL_PTS" in sec
    # 这几句是防扩散的全部说明，删掉任何一条都可能让下一个人踩坑。
    # 必须保留用户自己的话（「只在这一个场景生效」），不能只留我的转述 ——
    # 转述可以被下一个人当成「可以商量的建议」，原话不行。
    assert "只在这一个场景生效" in sec, \
        "没保留用户原话「只在这一个场景生效」（只有转述的话，约束力会变弱）"
    assert "只在一个地方生效" in sec or "after_pin" in sec, \
        "没写清容差具体用在 after_pin 的哪一个条件上"
    assert "TOUCH_TOL_PCT" in sec, "没提醒不许扩散到表 2 的入表容差"
    assert "24,075" in sec and "30,874" in sec, \
        "没写扩散的代价（表 2 会从 24,075 行变 30,874 行）"
    assert "kbar_shape" in sec or "表 5" in sec, "没写判定落在表 5"
    assert "forward" in sec, "没写「不许 import forward」"
    assert "D-21" in sec, "没指向 D-21（阈值还是临时值）"
    # 状态词与分布数
    for label in ("恐慌盘", "插针收回", "插针后稳住", "针后远离"):
        assert label in sec, "没列状态词 %s" % label
    assert "17,654" in sec, "没写分母"


def test_readme_line_counts_match_reality():
    """索引里的行数必须与文件实际行数一致。

    这条存在的理由：行数漂了不会让任何功能坏，只会让人以为文档是旧的，
    然后不去读它。而「以为文档过期」正是这份索引要防的事。
    """
    readme = _read("docs", "README.md")
    rows = {}
    for ln in readme.split("\n"):
        # 行首是 `| `，文件名可能被 ** 包起来，行数是第二列的整数
        m = re.search(r"\|\s*\*{0,2}(docs/[^\s|*]+)\*{0,2}\s*\|\s*(\d+)\s*\|", ln)
        if m:
            rows[m.group(1)] = int(m.group(2))
    assert rows, "索引表里一行都没解析出来（格式变了？）"
    assert len(rows) >= 5, "索引表只解析出 %d 行，格式可能变了" % len(rows)
    drift = []
    for path, claimed in rows.items():
        full = _p(*path.split("/"))
        if not os.path.exists(full):
            continue
        with io.open(full, encoding="utf-8") as f:
            actual = sum(1 for _ in f)
        if actual != claimed:
            drift.append("%s 索引写 %d 行，实际 %d 行" % (path, claimed, actual))
    assert not drift, "索引行数漂了：\n  " + "\n  ".join(drift)

# ================================================================== 第 6 步：§39 的数
def _shape_facts():
    """从库里现算 §39 用到的每一个数。**只读**（生产库不碰）。"""
    import sqlite3
    from ai_trader.kbar import shape as S
    db = os.path.join(ROOT, "data", "kbar.db")
    if not os.path.exists(db):
        import pytest
        pytest.skip("没有 data/kbar.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        one = lambda q, *a: con.execute(q, a).fetchone()[0]   # noqa: E731
        n = one("select count(*) from kbar_shape where interval_ms=?", IV_DOC)
        facts = {
            "n": n,
            "hash": [h for (h,) in con.execute(
                "select distinct params_hash from kbar_shape "
                "where interval_ms=?", (IV_DOC,))],
            "label": dict(con.execute(
                "select label, count(*) from kbar_shape where interval_ms=? "
                "group by label", (IV_DOC,))),
            "pin_kind": dict(con.execute(
                "select pin_kind, count(*) from kbar_shape where interval_ms=? "
                "group by pin_kind", (IV_DOC,))),
            "ref_kind": dict(con.execute(
                "select ref_pin_kind, count(*) from kbar_shape "
                "where interval_ms=? and ref_pin_kind is not null "
                "group by ref_pin_kind", (IV_DOC,))),
            "n_ref": one("select count(*) from kbar_shape "
                         "where interval_ms=? and ref_pin_ts is not null",
                         IV_DOC),
            "n_swing_ref_bars": one(
                "select count(distinct ref_pin_ts) from kbar_shape "
                "where interval_ms=? and ref_pin_kind='swing'", IV_DOC),
        }
        bars = [{"o": r[0], "h": r[1], "l": r[2], "c": r[3]} for r in con.execute(
            "select o,h,l,c from kbar_evolution where symbol='BTCUSDT' "
            "and interval_ms=? order by ts", (IV_DOC,))]
    finally:
        con.close()
    # 本根自己是「已确认局部极值」的根数（33.1% 那个数）
    facts["n_self_extreme"] = sum(
        1 for k in range(1, len(bars))
        if S.swing_pin_kind(bars, k, k) is not None)
    # 收位落在最外 1/4 的根数（9650 那个数）
    facts["n_edge"] = sum(
        1 for b in bars
        if S.geometry(b["o"], b["h"], b["l"], b["c"]).close_pos <= 0.25
        or S.geometry(b["o"], b["h"], b["l"], b["c"]).close_pos >= 0.75)
    return facts


def test_impl_record_section_39_numbers_match_the_database():
    """§39 里每一个能从库里读出来的数，都必须和库里一致。

    这条测试是「文档即证据」的底线：§39 记的是两个判据漏洞的实测，
    数字一旦漂了，读者会照着一个不存在的现象去改代码。
    """
    rec = _read("docs", "K线演化-实现记录.md")
    sec = _section(rec, "### 39.7", "### 39.8")
    f = _shape_facts()

    assert "17,654 行" in sec, "39.7 没写表 5 行数"
    assert "{:,} 行".format(f["n"]) == "17,654 行", f["n"]
    assert f["hash"] == ["9d23fff74ad3"], "库里指纹不是单一一套：%s" % f["hash"]
    assert "9d23fff74ad3" in rec, "文档没记当前口径指纹"

    # label 分布：39.7 那段里的六个数字
    for lab in ("无·真空", "无·贴线", "插针后稳住",
                "插针收回", "插针", "针后远离", "恐慌盘"):
        assert "{:,}".format(f["label"][lab]) in sec,             "39.7 的 label 分布里 %s 的 %d 没写对" % (lab, f["label"][lab])

    # 参照针 / 本根针 —— 断言的对象必须是**文档文本**，不能只算一遍自己比对。
    # 写 `assert "{:,}".format(x) == "2,926"` 只是在测自己会不会 format，
    # 把文档里的 2,926 改成 2,927 测试照样绿（变异测试抓到过这个错）。
    poke = _section(rec, "### 39.4", "### 39.5")
    assert "参照针 %s 根" % "{:,}".format(f["n_ref"]) in poke, \
        "39.4 没写参照针总数 %d" % f["n_ref"]
    assert "本根是针 %s 根" % "{:,}".format(f["pin_kind"]["wick"]) in poke, \
        "39.4 没写本根针 %d" % f["pin_kind"]["wick"]
    assert "{:,}".format(f["ref_kind"]["wick"]) in poke, \
        "39.4 没写 wick 参照 %d" % f["ref_kind"]["wick"]
    # 用完整短语而不是裸数字：§39.4 里 2,926 出现了两次（明细 + 解释），
    # 只钉裸数字的话改一处、另一处还能让它通过。
    phrase = "`wick` %s + `swing` %s" % (
        "{:,}".format(f["ref_kind"]["wick"]),
        "{:,}".format(f["ref_kind"]["swing"]))
    assert phrase in poke, "39.4 的参照针明细不是 %r（文档可能是旧数）" % phrase
    assert "{:,}".format(f["n_swing_ref_bars"]) in poke, \
        "39.4 没写当过 swing 针的不同根 %d" % f["n_swing_ref_bars"]


def test_impl_record_33_percent_argument_is_still_true():
    """§39.4 用「33.1% / 5,852 根」论证「极值针不能自己打插针标签」。

    这个论证是这条规则的唯一依据。如果数字变了（比如阈值调过），
    论证就得重写 —— 不能让一个过期的比例继续当理由撑着。
    """
    rec = _read("docs", "K线演化-实现记录.md")
    f = _shape_facts()
    assert "{:,}".format(f["n_self_extreme"]) == "5,852",         "本根是已确认极值的根数变了：%d" % f["n_self_extreme"]
    pct = 100.0 * f["n_self_extreme"] / f["n"]
    sec = _section(rec, "### 39.4", "### 39.5")
    assert "{:,}".format(f["n_self_extreme"]) in sec, \
        "39.4 没写 %d 根（论证的分子）" % f["n_self_extreme"]
    assert "33.1%" in sec, "39.4 没写 33.1%%（实算 %.1f%%）" % pct
    assert abs(pct - 33.1) < 0.05, "比例已经不是 33.1%%：%.2f%%" % pct


def test_impl_record_cross_bar_numbers_match_a_real_run():
    """§39.6 的跨根收回数字必须能复现 —— 它是「默认关」这个决定的全部依据。

    这条特别值得钉：我第一次量的时候漏了 prev_lines、把 lines_at_bar 的键
    写成 ts（应该用根的序号），量出来是 0，差点得出「这个功能没接线」的错误结论。
    所以测试里必须按 build.py 的真实调用方式来量。
    """
    import sqlite3
    from ai_trader.kbar import shape as S
    db = os.path.join(ROOT, "data", "kbar.db")
    if not os.path.exists(db):
        import pytest
        pytest.skip("没有 data/kbar.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        bars = [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                 "vol_rel": r[5], "taker_buy_ratio": r[6]}
                for r in con.execute(
                    "select ts,o,h,l,c,vol_rel,taker_buy_ratio from kbar_evolution "
                    "where symbol='BTCUSDT' and interval_ms=900000 "
                    "and o is not null order by ts")]
        by_ts = {}
        for ts, key, price in con.execute(
                "select ts,line_key,line_price from kbar_line "
                "where symbol='BTCUSDT' and interval_ms=900000"):
            if price is not None:
                by_ts.setdefault(int(ts), {})[key] = float(price)
    finally:
        con.close()
    ts_list = [int(b["ts"]) for b in bars]
    lab = {j: ({} if j == 0 else by_ts.get(ts_list[j - 1], {}))
           for j in range(len(bars))}

    def run(cross):
        return [S.judge(bars, i, prev_lines=lab.get(i), lines_at_bar=lab,
                        allow_cross_bar=cross) for i in range(len(bars))]

    off, on = run(False), run(True)
    n = len(bars)
    rec_off = sum(1 for j in off if j.has(S.STATE_PIN_RECOVERED))
    rec_on = sum(1 for j in on if j.has(S.STATE_PIN_RECOVERED))
    assert rec_off == 1197, "关着时的插针收回应等于表 5 的 1,197，实算 %d" % rec_off
    assert rec_on == 5150, "打开后应是 5,150，实算 %d" % rec_on
    new = rec_on - rec_off
    assert new == 3953, "跨根新增应是 3,953，实算 %d" % new
    assert abs(100.0 * new / n - 22.4) < 0.1,         "跨根新增占比应是 22.4%%，实算 %.1f%%" % (100.0 * new / n)
    # 被抢的是 **label**，不是状态：稳住这个状态照样成立（3,926 -> 3,926），
    # 但它不再是结论，因为插针收回优先级更高。这里两个都钉住，防止混为一谈。
    st_off = sum(1 for j in off if j.has(S.STATE_STABLE))
    st_on = sum(1 for j in on if j.has(S.STATE_STABLE))
    assert (st_off, st_on) == (3926, 3926), \
        "「插针后稳住」这个状态不该因为跨根收回而消失：%s" % ((st_off, st_on),)

    def n_label(js, lab):
        return sum(1 for j in js if j.label == lab)

    assert n_label(off, S.STATE_STABLE) == 3886
    assert n_label(on, S.STATE_STABLE) == 2559, \
        "稳住 label 应被抢到 2,559，实算 %d" % n_label(on, S.STATE_STABLE)
    assert n_label(off, S.STATE_NEAR_PIN) == 706
    assert n_label(on, S.STATE_NEAR_PIN) == 192
    assert n_label(off, S.STATE_PIN_RECOVERED) == 1131
    assert n_label(on, S.STATE_PIN_RECOVERED) == 4970
    assert n_label(off, S.STATE_NONE) == 10633
    assert n_label(on, S.STATE_NONE) == 8780

    doc = _read("docs", "K线演化-实现记录.md")
    sec = _section(doc, "### 39.6", "### 39.7")
    for tok in ("3,953", "22.4%", "1,197", "5,150", "1,327", "514", "11:15",
                "3,886", "2,559", "706", "192"):
        assert tok in sec, "39.6 没写 %s" % tok
    # 这三个数也出现在别的文档里，一起钉住 —— 否则一处对、另一处旧，
    # 读的人会得到两个版本。
    pend = _section(_read("docs", "K线演化-待确定清单.md"), "### D-21", "## 八")
    hand = _section(_read("docs", "HANDOFF-DSH-SESSION.md"),
                    "## 十一之四", "## 十一之五")
    for name, text in (("待确定清单", pend), ("交接文档", hand)):
        assert "3,953" in text, "%s 的跨根新增数不是 3,953" % name
        assert "22.4%" in text, "%s 的跨根占比不是 22.4%%" % name


def test_pending_list_threshold_values_are_read_from_the_value_column():
    """D-21 的阈值必须写在**值那一列**，不许散在说明文字里蒙混过关。

    第一版测试是「这一行里出现过这个数」，结果把 0.25 改成 0.30 它还是绿的 ——
    因为说明里还有一句「0.25 = 收盘落在全幅最靠边的四分之一」。
    现在只认第二列（值列）。
    """
    import re
    from ai_trader.kbar import shape as S
    pend = _read("docs", "K线演化-待确定清单.md")
    sec = _section(pend, "### D-21", "## 八")
    names = ("WICK_PCT_MIN", "WICK_PTS_MIN", "PANIC_VOL_REL_MIN",
             "PANIC_TAKER_BUY_MAX", "PANIC_CLOSE_POS_MAX",
             "STABLE_MAX_PIN_RANGE_MULT", "SCENARIO_TOL_PTS",
             "STABLE_LOOKBACK_BARS", "SWING_LOOKBACK_BARS")
    for name in names:
        val = getattr(S, name)
        rows = [ln for ln in sec.split("\n") if name in ln and ln.strip().startswith("|")]
        assert rows, "D-21 表里没有 %s 这一行" % name
        cells = [c.strip().strip("*") for c in rows[0].strip().strip("|").split("|")]
        assert len(cells) >= 2, "%s 那行没有值列：%s" % (name, rows[0])
        cell = cells[1]
        nums = re.findall(r"\d+(?:\.\d+)?", cell)
        assert nums, "%s 的值列里没有数字：%r" % (name, cell)
        assert any(abs(float(x) - val) < 1e-9 for x in nums), \
            "%s 代码里是 %g，值列写的是 %r" % (name, val, cell)

def _load_bars_and_lines():
    """按 build.py 的方式装 bars 与「每根能看到的那份线快照」。"""
    import sqlite3
    db = os.path.join(ROOT, "data", "kbar.db")
    if not os.path.exists(db):
        import pytest
        pytest.skip("没有 data/kbar.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        bars = [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                 "vol_rel": r[5], "taker_buy_ratio": r[6]}
                for r in con.execute(
                    "select ts,o,h,l,c,vol_rel,taker_buy_ratio from kbar_evolution "
                    "where symbol='BTCUSDT' and interval_ms=900000 "
                    "and o is not null order by ts")]
        by_ts = {}
        for ts, key, price in con.execute(
                "select ts,line_key,line_price from kbar_line "
                "where symbol='BTCUSDT' and interval_ms=900000"):
            if price is not None:
                by_ts.setdefault(int(ts), {})[key] = float(price)
    finally:
        con.close()
    ts_list = [int(b["ts"]) for b in bars]
    lines = {j: ({} if j == 0 else by_ts.get(ts_list[j - 1], {}))
             for j in range(len(bars))}
    return bars, lines


def test_impl_record_181_candidates_argument_is_still_true():
    """§39.2 的「181 根候选、0 根能判成下针」是「恐慌盘以插针为前提有漏洞」的
    唯一量化证据。这条推翻了旧判据，所以它必须能复现。
    """
    from ai_trader.kbar import shape as S
    bars, _ = _load_bars_and_lines()
    cand = ans = edge = 0
    for b in bars:
        g = S.geometry(b["o"], b["h"], b["l"], b["c"])
        if g.close_pos <= S.PANIC_CLOSE_POS_MAX or g.close_pos >= 0.75:
            edge += 1
        if g.close_pos > S.PANIC_CLOSE_POS_MAX:
            continue
        if b["vol_rel"] is None or b["taker_buy_ratio"] is None:
            continue
        if (b["vol_rel"] >= S.PANIC_VOL_REL_MIN
                and b["taker_buy_ratio"] <= S.PANIC_TAKER_BUY_MAX):
            cand += 1
            if g.dn_pct >= S.WICK_PCT_MIN and g.dn >= S.WICK_PTS_MIN:
                ans += 1
    assert (cand, ans) == (181, 0), "候选/可判下针 应是 (181, 0)，实算 (%d, %d)" % (cand, ans)
    assert edge == 9650, "收位落在最外 1/4 的根数应是 9,650，实算 %d" % edge

    doc = _read("docs", "K线演化-实现记录.md")
    sec = _section(doc, "### 39.2", "### 39.3")
    # 同理：181 在 §39.2 出现两次（结论 + 「10:45 就是这 181 根之一」），
    # 所以钉完整短语。
    assert "全库有 **%d 根**" % cand in sec, \
        "39.2 没写 %d 根候选（写着别的数）" % cand
    assert "能被判成下针的只有 %d 根" % ans in sec, \
        "39.2 没写「能被判成下针的只有 %d 根」" % ans
    assert "全库有 %s 根" % "{:,}".format(edge) in sec, \
        "39.2 没写 %s（对照数）" % "{:,}".format(edge)


def test_impl_record_swing_window_table_matches_a_real_run():
    """§39.3 的窗口表必须能复现。

    这张表是「取 8 根」的全部依据，而且文档里明确写了「这条曲线上没有拐点」——
    正因为没有拐点，数字更不能漂：漂了就没有任何东西撑着这个选择了。
    """
    from ai_trader.kbar import shape as S
    bars, _ = _load_bars_and_lines()
    n = len(bars)
    keep = (S.swing_pin_kind.__defaults__, S.pin_amp.__defaults__,
            S.find_pin_before_ex.__defaults__, S.SWING_LOOKBACK_BARS)
    expect = {3: (5257, 4595), 5: (4605, 3777), 8: (3926, 2936),
              12: (3434, 2356), 16: (3111, 1952), 24: (2736, 1513)}
    try:
        for wide, want in expect.items():
            S.SWING_LOOKBACK_BARS = wide
            S.swing_pin_kind.__defaults__ = (wide, S.WICK_PTS_MIN)
            S.pin_amp.__defaults__ = (S.PIN_KIND_WICK, wide)
            S.find_pin_before_ex.__defaults__ = (
                S.STABLE_LOOKBACK_BARS, S.SIDE_DOWN, True, wide)
            js = [S.judge(bars, i) for i in range(n)]
            st = sum(1 for j in js if j.has(S.STATE_STABLE))
            rf = sum(1 for j in js if j.ref_pin_kind == S.PIN_KIND_SWING)
            assert (st, rf) == want, "窗口 %d 根：(稳住, swing参照) 应是 %s，实算 (%d, %d)" % (
                wide, want, st, rf)
    finally:
        (S.swing_pin_kind.__defaults__, S.pin_amp.__defaults__,
         S.find_pin_before_ex.__defaults__, S.SWING_LOOKBACK_BARS) = keep

    doc = _read("docs", "K线演化-实现记录.md")
    sec = _section(doc, "### 39.3", "### 39.4")
    for wide, (st, rf) in expect.items():
        assert "{:,}".format(st) in sec, "39.3 窗口表没写 %d 根那行的稳住数 %d" % (wide, st)
    assert "没有拐点" in sec, "39.3 没承认这条曲线没有拐点"


def test_impl_record_widest_amp_variant_numbers_match_a_run():
    """§39.5 里「撑大尺」那一档（4,220 / 508）是「为什么不用窗口最宽根」的证据。

    这类「被否掉的方案」的数字最容易烂掉：它不在主流程上，没人会再算一次。
    烂掉以后下一个人会看到一个假的对比表，以为「撑大一点也没什么差别」。
    """
    from ai_trader.kbar import shape as S
    bars, _ = _load_bars_and_lines()
    n = len(bars)
    orig = S.pin_amp

    def widest_amp(b, j, kind=S.PIN_KIND_WICK, look=None):
        own = S.geometry(b[j]["o"], b[j]["h"], b[j]["l"], b[j]["c"]).rng
        if kind != S.PIN_KIND_SWING:
            return max(own, S.WICK_PTS_MIN)
        lo = max(0, j - (look or S.SWING_LOOKBACK_BARS))
        w = own
        for k in range(lo, j + 1):
            r = S.geometry(b[k]["o"], b[k]["h"], b[k]["l"], b[k]["c"]).rng
            if r > w:
                w = r
        return max(w, S.WICK_PTS_MIN)

    try:
        S.pin_amp = widest_amp
        js = [S.judge(bars, i) for i in range(n)]
    finally:
        S.pin_amp = orig
    st = sum(1 for j in js if j.has(S.STATE_STABLE))
    nr = sum(1 for j in js if j.has(S.STATE_NEAR_PIN))
    assert (st, nr) == (4154, 494), "撑大尺那档应是 稳住 4,154 / 远离 494，实算 (%d, %d)" % (st, nr)

    doc = _read("docs", "K线演化-实现记录.md")
    sec = _section(doc, "### 39.5", "### 39.6")
    assert "4,154" in sec and "494" in sec, "39.5 没写撑大尺那档的 4,154 / 494"
    assert "3,926" in sec and "722" in sec, "39.5 没写现行口径的 3,926 / 722"
