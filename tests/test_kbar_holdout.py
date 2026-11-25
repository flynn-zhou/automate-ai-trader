# -*- coding: utf-8 -*-
"""留出集的测试。钉的是**防作弊的机制**，不是某次跑出来的数字。

这个模块的价值全在「不能被糊弄」上，所以测试也全冲着这一点：

  1. 切分不许重叠，embargo 必须正好盖住前视窗（不然是真泄漏）
  2. 切分冻结：同输入必得同输出，哈希稳定（不然可以试几个切点挑好看的）
  3. 朴素公式必须与 `verify.lift_z` **完全一致**（不然两套数打不了架）
  4. 聚类估计量必须嵌套朴素公式（每根自成一簇时）
  5. 聚类必须按**真实时间块**分，不许按筛选后顺序分（踩过的坑）
  6. 聚类太少必须拒绝报数，且不许进多重比较的分母
  7. 登记表只增不改
"""
from __future__ import annotations

import math
import os
import random
import shutil
import sqlite3
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import dims as D          # noqa: E402
from ai_trader.kbar import holdout as H       # noqa: E402
from ai_trader.kbar import verify as V        # noqa: E402

DB = os.path.join(ROOT, "data", "kbar.db")
SYM, IV = "BTCUSDT", 900000


def _ts_list(n, step_ms=900000, start=1_700_000_000_000):
    return [start + i * step_ms for i in range(n)]


# ---------------------------------------------------------------- 切分
def test_split_has_three_disjoint_ranges():
    """train / embargo / holdout 不许重叠，也不许有缝。"""
    ts = _ts_list(1000)
    sp = H.make_split(ts)
    assert not sp.get("error")
    ph = [H.phase_of(t, sp) for t in ts]
    assert ph[0] == "train" and ph[-1] == "holdout"
    assert set(ph) == {"train", "embargo", "holdout"}, "三段没都出现"
    # 顺序必须是 train → embargo → holdout，不许交错
    order = [{"train": 0, "embargo": 1, "holdout": 2}[p] for p in ph]
    assert order == sorted(order), "三段在时间上交错"
    assert sp["train_n"] + sp["embargo_n"] + sp["holdout_n"] == 1000


def test_embargo_exactly_covers_the_forward_window():
    """embargo 必须 >= 前视窗，否则训练段尾巴在偷看留出段。

    判据：训练段最后一根的「事后结论」要看 t+1..t+EMBARGO_BARS，
    这些根必须整段落在 holdout 之前。
    """
    ts = _ts_list(500)
    sp = H.make_split(ts)
    i_last_train = ts.index(sp["train_last_ts"])
    fwd = ts[i_last_train + 1:i_last_train + 1 + H.EMBARGO_BARS]
    assert len(fwd) == H.EMBARGO_BARS
    assert max(fwd) < sp["holdout_first_ts"], (
        "训练段最后一根的前视窗伸进了留出段 —— 这是真泄漏")
    assert sp["embargo_n"] == H.EMBARGO_BARS


def test_embargo_covers_every_known_horizon():
    """embargo 不许小于任何一层真实用到的前视窗。"""
    horizons = list(V.VERIFY_HORIZONS) + [int(V.SURVIVAL_MAX_N)]
    assert H.EMBARGO_BARS >= max(horizons), (
        "EMBARGO_BARS=%d 小于某层的前视窗 %d —— 那一层会泄漏"
        % (H.EMBARGO_BARS, max(horizons)))


def test_split_is_frozen_and_deterministic():
    """同输入必得同输出，哈希稳定。可配置的切点不是冻结的切点。"""
    ts = _ts_list(3000)
    a = H.make_split(ts)
    b = H.make_split(list(reversed(ts)))     # 输入顺序打乱也要同结果
    assert a == b, "切分对输入顺序敏感"
    assert a["split_hash"] == H.split_hash() == b["split_hash"]
    # 参数一变，哈希必须变（否则「改了切点」看不出来）
    old = H.TRAIN_FRAC
    try:
        H.TRAIN_FRAC = 0.5
        assert H.split_hash() != a["split_hash"], "改切点没改哈希"
    finally:
        H.TRAIN_FRAC = old


def test_too_few_bars_refuses_instead_of_shrinking_embargo():
    """样本不够时必须报错，不许偷偷把 embargo 缩小。"""
    sp = H.make_split(_ts_list(5))
    assert sp.get("error"), "5 根也切了，embargo 被悄悄缩小了"


def test_train_fraction_is_roughly_70_30():
    ts = _ts_list(10000)
    sp = H.make_split(ts)
    frac = sp["holdout_n"] / float(sp["n"])
    assert abs(frac - 0.30) < 0.005, "留出段占比 %.4f 不是 30%%" % frac


# ---------------------------------------------------------------- 结果口径
def test_pending_rows_are_none_not_false():
    """「未成熟」不参与统计，必须是 None —— 不是 False。

    把它当 False，等于说「没验过」= 「没应验」，会把基准率压低。
    """
    assert H.outcome_ok({"status": H.STATUS_PENDING, "must_hold": 1}) is None
    assert H.outcome_ok({"status": H.STATUS_PENDING, "must_hold": 0}) is None


def test_must_hold_decides_which_way_is_correct():
    assert H.outcome_ok({"status": H.STATUS_LASTED, "must_hold": 1}) is True
    assert H.outcome_ok({"status": H.STATUS_BROKE, "must_hold": 1}) is False
    assert H.outcome_ok({"status": H.STATUS_BROKE, "must_hold": 0}) is True
    assert H.outcome_ok({"status": H.STATUS_LASTED, "must_hold": 0}) is False


# ---------------------------------------------------------------- 统计
def test_naive_z_matches_the_existing_verify_layer():
    """朴素口径必须与 `verify.lift_z` **完全一致**。

    不一致的话，留出集的数和以前报过的数就打不了架，也没法互相校验。
    """
    for hits, total, base in ((30, 100, 20.0), (5, 40, 50.0),
                              (0, 10, 10.0), (199, 200, 99.0)):
        a = H.lift_z_naive(hits, total, base)
        b = V.lift_z(hits, total, base)
        assert abs(a[0] - b[0]) < 1e-12 and abs(a[1] - b[1]) < 1e-12, (
            "lift_z_naive 与 verify.lift_z 不一致：%r vs %r" % (a, b))


def test_cluster_z_nests_the_naive_formula():
    """每根自成一簇、且 p̂ 正好等于 p0 时，聚类 z 必须等于朴素 z。

    ⚠ 只有在 `p̂ == p0` 时才完全相等。两个 z 用的方差不一样：
       - `lift_z_naive` → **零假设方差** `p0(1−p0)/n`（二项检验）
       - `lift_z_cluster` → **估计方差** `p̂(1−p̂)/n`（Wald 检验）
    这个测试最初按「任何时候都相等」写的，结果差 0.45%，查下来是这个原因。
    所以这里刻意构造 p̂ = p0 = 0.4，把差别排除掉，专测「分块公式本身」。

    ⚠ 不能用 z=0 的数据测 —— `p̂=p0` 时 lift=0、z 也=0，
       那样把 G/(G-1) 修掉也看不出来（变异测试抓到过这个漏）。
       这里造 p̂≠p0 但**每根一簇**的极端情况：修正因子必须显形。
    """
    n = 1000
    # 每根一簇 + p̂≠p0：这时聚类方差 = p̂(1−p̂)/n × G/(G−1)
    outs = [1] * 600 + [0] * 400             # p̂ = 0.60，p0 = 0.50
    blocks = list(range(n))
    _l, z_cl, g = H.lift_z_cluster(outs, 0.50, blocks=blocks)
    assert g == n
    p = 0.60
    var_no_corr = p * (1 - p) / n
    var_with_corr = var_no_corr * (n / float(n - 1))
    z_no_corr = (p - 0.50) / math.sqrt(var_no_corr)
    z_with_corr = (p - 0.50) / math.sqrt(var_with_corr)
    # 必须命中「带修正」的那个，不能命中「不带修正」的那个
    assert abs(z_cl - z_with_corr) < 1e-9, (
        "聚类 z %.10f 不等于带 G/(G−1) 修正的 %.10f" % (z_cl, z_with_corr))
    assert abs(z_cl - z_no_corr) > 1e-4, (
        "聚类 z 没做 G/(G−1) 修正（和不修正的结果 %.10f 无法区分）"
        % z_no_corr)


def test_cluster_variance_uses_estimated_not_null_variance():
    """聚类方差用 p̂，不是 p0 —— 这是它与朴素公式的**本质**差别。"""
    n = 500
    outs = [1] * 100 + [0] * 400             # p̂ = 0.20
    blocks = list(range(n))
    _l, z_cl, _g = H.lift_z_cluster(outs, 0.40, blocks=blocks)
    p = 0.20
    var_est = p * (1 - p) / n * (n / float(n - 1))
    expect = (p - 0.40) / math.sqrt(var_est)
    assert abs(z_cl - expect) < 1e-9, (
        "聚类 z 用的不是估计方差：%.10f vs 期望 %.10f" % (z_cl, expect))


def test_cluster_and_naive_use_different_variances():
    """两个 z 用的方差不同（零假设 vs 估计），所以 p̂≠p0 时数值不等。

    把这条**钉住**是防止后来的人把它当成 bug 去「修」—— 修了就变成
    二项检验，反而丢掉了按块去相关的能力。
    """
    n = 500
    outs = [1] * 100 + [0] * 400             # p̂ = 0.20
    blocks = list(range(n))
    _l, z_cl, _g = H.lift_z_cluster(outs, 0.40, blocks=blocks)
    _l2, z_nv = H.lift_z_naive(sum(outs), n, 40.0)
    assert abs(z_cl - z_nv) > 0.01, (
        "p̂≠p0 时两个 z 居然完全相同 —— 说明某一方被改成了另一种检验")
    # 但符号和量级必须一致（否则不是同一个统计量）
    assert (z_cl > 0) == (z_nv > 0)


def test_cluster_z_shrinks_under_perfect_correlation():
    """完全正相关（每 12 根全同）时，se 应放大约 √12，z 相应缩小。"""
    random.seed(5)
    outs = []
    for _ in range(100):
        v = 1 if random.random() < 0.4 else 0
        outs.extend([v] * 12)
    p = sum(outs) / len(outs)
    blocks = [i // 12 for i in range(len(outs))]
    _l, z_cl, g = H.lift_z_cluster(outs, 0.4, blocks=blocks)
    _l2, z_nv = H.lift_z_naive(sum(outs), len(outs), 40.0)
    assert g == 100
    ratio = abs(z_nv / z_cl) if z_cl else 0
    assert abs(ratio - math.sqrt(12)) < 0.5, (
        "完全相关时 z 只缩小了 %.2f 倍，应约 √12=3.46" % ratio)


def test_cluster_blocks_come_from_real_time_not_filtered_order():
    """聚类块必须按真实时间分，不许按「筛选后成员顺序」分。

    踩过的坑：`无·贴线` 占留出段 40%，按成员顺序切 12 个一块，
    一块实际横跨约 30 根真实 K 线，块内相关性被稀释，
    结果 cluster z 反而比 naive 大 1.4 倍（−5.53 → −8.75）。
    """
    # 造一份「只有偶数位命中」的稀疏序列：按成员顺序切块会把相邻成员
    # 分到同一块（它们其实隔了很远），按真实时间切才不会。
    random.seed(3)
    outs = [1 if random.random() < 0.5 else 0 for _ in range(600)]
    # 两个口径：真实时间块（稀疏，每个成员来自不同时间段）
    blocks_time = [i for i in range(len(outs))]          # 每根不同块
    # 错误口径：把成员重新从 0 编号，每 12 个一簇
    blocks_member = [i // 12 for i in range(len(outs))]
    _l, z_time, g_time = H.lift_z_cluster(outs, 0.5, blocks=blocks_time)
    _l, z_member, g_member = H.lift_z_cluster(outs, 0.5, blocks=blocks_member)
    assert g_time == 600 and g_member == 50
    assert z_time != z_member, "两种分块给出同一个 z，说明块号没被用上"


def test_few_clusters_refuses_to_report_a_z():
    """聚类数不足时必须给 None，不许硬报一个 z。"""
    rows = []
    for i in range(200):
        rows.append({"ts": 1000 + i * 900000, "label": "L",
                     "values": {"zone": "A" if i % 2 else "B"},
                     "buckets": {},
                     "status": H.STATUS_LASTED if i % 3 else H.STATUS_BROKE,
                     "must_hold": 1})
    # 强行把 MIN_CLUSTERS 提到高得离谱，让所有格子都不可信
    old = H.MIN_CLUSTERS
    try:
        H.MIN_CLUSTERS = 10 ** 6
        res = H.evaluate(rows, "L")
        assert res["cells"], "没生成格子"
        assert all(c["z_cluster"] is None for c in res["cells"]), (
            "聚类不足却报了 z")
        assert all(not c["pass_cluster"] for c in res["cells"])
    finally:
        H.MIN_CLUSTERS = old


def test_multiple_comparison_denominator_excludes_untrusted_cells():
    """多重比较的分母只能是可信格数。

    把「z 没报」的格子算进分母，会稀释掉真正验过的次数
    —— 等于偷偷放宽门槛。

    ⚠ 必须造出**可信/不可信混合**的局面来测。全可信时 usable==flat，
       把分母改成 flat 也看不出来（变异测试抓到过这个漏）。
       这里用「一小撮格子在时间上挤成一团」造出 G<MIN_CLUSTERS 的格子。
    """
    rows = []
    # 主体：2000 根，均匀铺开 → zone 两格各自跨很多块，可信
    for i in range(2000):
        rows.append({"ts": 1000 + i * 900000, "label": "L",
                     "values": {"zone": "A" if i % 2 else "B"},
                     "buckets": {},
                     "status": H.STATUS_LASTED if i % 3 else H.STATUS_BROKE,
                     "must_hold": 1})
    # 稀有待：40 根挤在连续 40 个时间点里 → 只跨约 4 个块，不可信
    for i in range(40):
        rows.append({"ts": 1000 + (3000 + i) * 900000, "label": "L",
                     "values": {"zone": "RARE"},
                     "buckets": {},
                     "status": H.STATUS_LASTED if i % 2 else H.STATUS_BROKE,
                     "must_hold": 1})
    res = H.evaluate_all(rows, ["L"])
    assert res["n_dropped_few_clusters"] > 0, (
        "没造出不可信的格子，这条测试测不到东西")
    assert res["n_tested"] == res["n_cells_all"] - res["n_dropped_few_clusters"]
    assert res["n_tested"] < res["n_cells_all"]
    # 不可信的格子必须不在 per_label 的可信集合里
    rare = [c for p in res["per_label"] for c in p["cells"]
            if c["val"] == "RARE"]
    assert rare, "稀有格被 MIN_N 干掉了，造不出来"
    assert all(not c["cluster_ok"] for c in rare), "稀有格居然被判可信"


def test_bonferroni_threshold_actually_tightens_in_evaluate_all():
    """`evaluate_all` 报的 Bonferroni 门槛必须随格数上升。

    只测 `_z_for_two_sided` 本身是不够的 —— 把 evaluate_all 里那行
    换成固定 Z_CRIT，单测那个函数照样绿（变异测试抓到过）。
    """
    def build(ndims):
        rows = []
        for i in range(3000):
            vals = {}
            for k in range(ndims):
                vals["d%d" % k] = "A" if (i // (k + 1)) % 2 else "B"
            rows.append({"ts": 1000 + i * 900000, "label": "L",
                         "values": vals, "buckets": {},
                         "status": H.STATUS_LASTED if i % 3 else H.STATUS_BROKE,
                         "must_hold": 1})
        return rows
    few = H.evaluate_all(build(1), ["L"])
    many = H.evaluate_all(build(6), ["L"])
    assert many["n_tested"] > few["n_tested"], "格数没变多，测不到门槛变化"
    assert many["z_bonferroni"] > few["z_bonferroni"] + 1e-6, (
        "格数从 %d 涨到 %d，Bonferroni 门槛却没收紧（%.3f vs %.3f）"
        % (few["n_tested"], many["n_tested"],
           few["z_bonferroni"], many["z_bonferroni"]))


def test_bonferroni_threshold_rises_with_more_tests():
    assert H._z_for_two_sided(0.05) > 1.9
    assert H._z_for_two_sided(0.05) < 2.1
    assert H._z_for_two_sided(0.05 / 100) > H._z_for_two_sided(0.05)
    assert H._z_for_two_sided(0.05 / 1000) > H._z_for_two_sided(0.05 / 100)


def test_binom_sf_is_a_probability():
    assert abs(H.binom_sf(0, 10, 0.05) - 1.0) < 1e-12
    assert H.binom_sf(11, 10, 0.05) == 0.0
    # 全噪声时，至少过 k 格的概率应随 k 上升而下降
    vals = [H.binom_sf(k, 100, 0.05) for k in (0, 5, 10, 20)]
    assert vals == sorted(vals, reverse=True), "概率没随门槛上升而下降"


# ---------------------------------------------------------------- 候选格
def test_candidate_cells_only_use_predefined_buckets_and_small_categoricals():
    """候选格只许来自「预先定义的分档」和「取值很少的类别变量」。

    不许对连续量（价格、时间戳）临时切点 —— 那就是在造阈值。

    ⚠ 连续量必须造出**多于 max_rare_values 个不同取值**，否则
       「取值太散就跳过」那条规则会顺手把它挡掉，
       去掉 isinstance 检查也看不出来（变异测试抓到过这个漏）。
    """
    rows = [{"ts": i, "label": "L", "status": H.STATUS_LASTED, "must_hold": 1,
             "buckets": {"close_pos": "小", "vol_rel": "显著放量"},
             "values": {"zone": "P-S" if i % 2 else "P-R",
                        "line_price": 70000.0 + i,      # 每根都不同
                        "direction": "UP" if i % 3 else "DOWN"}}
            for i in range(60)]
    cells = H.candidate_cells(rows, max_rare_values=8)
    dims = {c[0] for c in cells}
    assert "close_pos" in dims and "vol_rel" in dims
    assert "zone" in dims and "direction" in dims
    assert "line_price" not in dims, "连续量被枚举成格了（在造阈值）"
    # 直接测那条防线：即便放开取值上限，float 也不许进格
    wide = H.candidate_cells(rows, max_rare_values=10_000)
    assert "line_price" not in {c[0] for c in wide}, (
        "取值上限放大后连续量混进了候选格 —— isinstance 防线没起作用")


def test_cells_with_too_few_rows_are_skipped():
    rows = [{"ts": i, "label": "L", "status": H.STATUS_LASTED, "must_hold": 1,
             "buckets": {"close_pos": "小" if i < 5 else "大"},
             "values": {}} for i in range(200)]
    res = H.evaluate(rows, "L")
    assert all(c["n"] >= H.MIN_N for c in res["cells"]), (
        "有格子样本数低于 MIN_N 却报了")


# ---------------------------------------------------------------- 登记表
@pytest.fixture()
def tmp_kbar_db():
    if not os.path.exists(DB):
        pytest.skip("没有 data/kbar.db")
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "k.db")
    shutil.copy2(DB, dst)
    con = sqlite3.connect(dst)
    try:
        for t in ("kbar_evolution", "kbar_shape", "kbar_verify",
                  "kbar_verify_survival", "kbar_line", "kbar_reaction",
                  "kbar_event", "kbar_forward", "kbar_dims",
                  "kbar_holdout_log"):
            con.execute("DELETE FROM %s" % t)
        con.commit()
    finally:
        con.close()
    yield dst
    shutil.rmtree(tmp, ignore_errors=True)


def test_holdout_log_persists_every_field_including_the_primary_count(
        tmp_kbar_db):
    """登记必须把**每一个**字段真写进去，尤其是主口径的计数。

    踩过的坑：表 8 的 DDL 里根本没有 `n_pass_cluster` 这一列，
    而 `add_holdout_log` 用的是白名单 tuple，于是主口径的数
    （32 格）被**静默丢掉**，只落下了对照口径（8 格）。
    结果是「登记了，但登记的不是主口径的数」—— 比没登记更糟。
    这条测试逐个字段读回来对，防它再犯。
    """
    from ai_trader.kbar.store import KBarStore
    rec = {"symbol": SYM, "interval_ms": IV, "split_version": "v1",
           "split_hash": "h1", "dims_hash": D.dims_hash(),
           "train_n": 12346, "holdout_n": 5296, "embargo_n": 12,
           "n_tested": 192, "n_pass_cluster": 32, "n_pass_block": 8,
           "n_pass_naive": 31, "n_expected": 9.6, "z_bonferroni": 3.65,
           "n_pass_bonf": 6, "note": "对字段"}
    with KBarStore(tmp_kbar_db) as st:
        st.add_holdout_log(rec)
        rows = st.holdout_log(SYM, IV)
    assert len(rows) == 1
    got = rows[0]
    for k, v in rec.items():
        assert got[k] == v, "字段 %s 没落库：库里 %r，写入 %r" % (k, got[k], v)
    # 单独再钉一次主口径那一列，别让它再被白名单漏掉
    assert got["n_pass_cluster"] == 32
    assert got["n_pass_block"] == 8


def test_holdout_log_schema_has_the_primary_count_column():
    """表 8 的物理列里必须有 `n_pass_cluster`。

    DDL 漏列 + 白名单插入 = 静默丢数据，而且不报任何错。
    所以除了「插得进」，还要直接查物理列。

    ⚠ **必须用全新的空库查，不能复用 `tmp_kbar_db`。**
       `tmp_kbar_db` 是拷贝生产库，那份库早就被迁移补上了这一列 ——
       DDL 被改坏它也照样绿（变异测试抓到过这个漏）。
       类型变更/缺列这类回归，只有在「从 SCHEMA 从零建库」时才看得见。
    """
    tmpdir = tempfile.mkdtemp()
    fresh = os.path.join(tmpdir, "fresh.db")
    try:
        from ai_trader.kbar.store import KBarStore
        with KBarStore(fresh):
            pass
        con = sqlite3.connect(fresh)
        try:
            cols = [r[1] for r in
                    con.execute("PRAGMA table_info(kbar_holdout_log)")]
        finally:
            con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    assert cols, "表 8 没建出来"
    assert "n_pass_cluster" in cols, (
        "全新库的表 8 没有 n_pass_cluster 列，主口径的数会丢（列：%r）" % cols)
    for need in ("n_tested", "n_expected", "z_bonferroni", "n_pass_bonf",
                 "split_hash", "dims_hash"):
        assert need in cols, "表 8 缺列 %s（列：%r）" % (need, cols)


def test_holdout_log_only_appends_and_counts_runs(tmp_kbar_db):
    """登记只增不改：跑两次就是两行，计数跟着涨。"""
    from ai_trader.kbar.store import KBarStore
    rec = {"symbol": SYM, "interval_ms": IV, "split_version": "v1",
           "split_hash": "abc", "dims_hash": D.dims_hash(),
           "train_n": 100, "holdout_n": 50, "embargo_n": 12,
           "n_tested": 10, "n_pass_block": 1, "n_pass_naive": 2,
           "n_expected": 0.5, "z_bonferroni": 3.0, "n_pass_bonf": 0,
           "note": "第一次"}
    with KBarStore(tmp_kbar_db) as st:
        assert st.holdout_run_count(SYM, IV, "abc") == 0
        i1 = st.add_holdout_log(rec)
        assert st.holdout_run_count(SYM, IV, "abc") == 1
        i2 = st.add_holdout_log(dict(rec, note="第二次"))
        assert st.holdout_run_count(SYM, IV, "abc") == 2
        rows = st.holdout_log(SYM, IV)
    assert i2 > i1
    assert len(rows) == 2, "旧行被覆盖了"
    assert [r["note"] for r in rows] == ["第一次", "第二次"]


def test_holdout_log_is_per_split_hash(tmp_kbar_db):
    """换切分（新哈希）就是另一本账，旧记录不能被新切分「借用」。"""
    from ai_trader.kbar.store import KBarStore
    base = {"symbol": SYM, "interval_ms": IV, "split_version": "v1",
            "dims_hash": D.dims_hash(), "train_n": 1, "holdout_n": 1,
            "embargo_n": 12, "n_tested": 1, "n_pass_block": 0,
            "n_pass_naive": 0, "n_expected": 0.05, "z_bonferroni": 1.9,
            "n_pass_bonf": 0, "note": None}
    with KBarStore(tmp_kbar_db) as st:
        st.add_holdout_log(dict(base, split_hash="AAA"))
        assert st.holdout_run_count(SYM, IV, "AAA") == 1
        assert st.holdout_run_count(SYM, IV, "BBB") == 0, (
            "换了哈希还算旧账")
