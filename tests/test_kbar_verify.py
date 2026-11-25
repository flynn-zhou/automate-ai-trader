# -*- coding: utf-8 -*-
"""佐证层（verify.py / 表 6）的测试。

这层的特殊性：它是**唯一**允许看后面 K 线的地方，所以测试的重点不是
「算得对不对」，而是三件更要紧的事：

    1. 它**没有**偷偷被判定路径用上（shape.py 不许 import verify）
    2. 「无法判定」不许被算成「成立」—— 尾部缺数据不是说对了
    3. 「无预言」不许被算进命中率分母 —— 那等于拿没说的话充数

还有一条方法论上的：**命中率必须与同口径基准并排**，
本文件用一条测试钉着「只报命中率」这个坑（见 test_hit_rate_alone_is_not_a_conclusion）。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.kbar import shape as S      # noqa: E402
from ai_trader.kbar import verify as V     # noqa: E402


def bar(o, h, l, c, ts=0, **kw):
    d = {"o": o, "h": h, "l": l, "c": c, "ts": ts}
    d.update(kw)
    return d


def down_pin(ts=1, low=9900.0):
    """一根下长影线针：下影 >= 65% 全幅且 >= 40 点。"""
    return bar(10000.0, 10005.0, low, 10000.0, ts=ts)


def up_pin(ts=1, high=10100.0):
    """一根上长影线针：上影 >= 65% 全幅且 >= 40 点。"""
    return bar(10000.0, high, 9995.0, 10000.0, ts=ts)


# ---------------------------------------------------------------- 预言翻译
def test_pin_low_predicts_the_low_holds():
    bars = [down_pin()]
    jd = S.judge(bars, 0)
    assert S.STATE_PIN in jd.states
    p = V.predict(bars, 0, jd)
    assert p.kind == V.PRED_PIN_LOW_HELD
    assert p.side == S.SIDE_DOWN
    assert p.must_hold is True
    assert p.ref_price == 9900.0
    # 那句话必须带具体价位，否则一年后没人能复核
    assert "9900.0" in p.text


def test_pin_high_predicts_the_high_holds():
    bars = [up_pin()]
    jd = S.judge(bars, 0)
    assert S.STATE_PIN in jd.states
    p = V.predict(bars, 0, jd)
    assert p.kind == V.PRED_PIN_HIGH_HELD
    assert p.side == S.SIDE_UP
    assert p.ref_price == 10100.0


def test_panic_without_wick_predicts_continuation_not_reversal():
    """恐慌盘收在极值（无影线）= 卖压未释放 → 预言「会继续破低」。

    这是被数据纠正过的一条：原先以为恐慌盘是「见底」，
    实测基准 46.9% 下命中 77.2%（z=+8.2），是**延续**不是反转。
    """
    g = S.geometry(10050.0, 10055.0, 9950.0, 9950.0)
    assert S.panic_side(g) == S.SIDE_DOWN
    assert S.pin_side(g) is None, "这根没有长影线（影线占比不够）"
    jd = S.Judgement(states=(S.STATE_PANIC,), label=S.STATE_PANIC, side=None,
                     evidence=(), pin_ts=None)
    bars = [bar(10050.0, 10055.0, 9950.0, 9950.0),
            bar(9950.0, 9950.0, 9900.0, 9905.0, ts=1)]
    p = V.predict(bars, 0, jd)
    assert p.kind == V.PRED_PIN_LOW_BROKE
    assert p.must_hold is False, "恐慌盘无影线就该预言「会破」，不是「守住」"


def test_panic_with_lower_wick_predicts_the_low_holds():
    """恐慌盘但有下影（被买回）→ 与无影线的那半**相反**。

    实测：有下影 46 根，后面真破低的只有 15.2%（基准 46.9%，z=-4.3）。
    同一个结论词里装着两种相反的行为，这是佐证层发现的真问题。

    注意 `is_panic` 只看量能与主动性，**不看收位** —— 所以要造出一根
    「有长下影 + 放量 + 主动卖」的根是可能的，那正是这 46 根的来源。
    """
    g = S.geometry(10060.0, 10065.0, 9900.0, 10050.0)
    assert S.pin_side(g) == S.SIDE_DOWN
    assert S.panic_side(g) == S.SIDE_DOWN
    assert S.is_panic(g, 3.5, 0.30) is True
    jd = S.Judgement(states=(S.STATE_PANIC, S.STATE_PIN), label=S.STATE_PANIC,
                     side=S.SIDE_DOWN, evidence=(), pin_ts=None)
    bars = [bar(10060.0, 10065.0, 9900.0, 10050.0)]
    p = V.predict(bars, 0, jd)
    assert p.must_hold is True


def test_stable_predicts_the_reference_pin_extreme_holds():
    pin = down_pin(ts=1)
    # 本根自己**不能是针**，否则 judge 会走「本根自己是针」那条分支
    follow = bar(10000.0, 10020.0, 9990.0, 10015.0, ts=2)
    assert S.pin_side(S.geometry(10000.0, 10020.0, 9990.0, 10015.0)) is None
    bars = [pin, follow]
    jd = S.judge(bars, 1)
    assert S.STATE_STABLE in jd.states, jd.evidence
    p = V.predict(bars, 1, jd, ref_pin_side=jd.ref_pin_side)
    assert p.kind == V.PRED_REF_LOW_HELD
    assert p.ref_price == 9900.0


def test_none_promise_is_about_no_big_move_and_has_no_fixed_price():
    bars = [bar(10000.0, 10005.0, 9990.0, 10002.0)]
    jd = S.judge(bars, 0)
    assert jd.label == S.STATE_NONE
    p = V.predict(bars, 0, jd)
    assert p.kind == V.PRED_QUIET
    assert p.ref_price is None
    assert p.amp >= S.WICK_PTS_MIN


def test_recovered_note_says_only_the_extreme_half_is_checked():
    """「插针收回」全部同时是「插针」，被验的只有极值那半句。

    不在预言原文里写清楚，读者会以为「哪条线收回」也验过了。
    """
    g = S.geometry(10000.0, 10005.0, 9885.0, 10000.0)
    assert S.pin_side(g) == S.SIDE_DOWN
    jd = S.Judgement(states=(S.STATE_PIN_RECOVERED, S.STATE_PIN),
                     label=S.STATE_PIN_RECOVERED, side=S.SIDE_DOWN,
                     evidence=(), pin_ts=None)
    bars = [bar(10000.0, 10005.0, 9885.0, 10000.0)]
    p = V.predict(bars, 0, jd)
    assert "只验极值这一半" in p.text


# ---------------------------------------------------------------- 裁决
def test_hold_verdict_when_the_low_is_not_retested():
    bars = [down_pin(), bar(10000.0, 10020.0, 9960.0, 10015.0, ts=1)]
    p = V.Prediction(V.PRED_PIN_LOW_HELD, "t", 9900.0, S.SIDE_DOWN, True)
    r = V.verify(bars, 0, p, 1)
    assert r.verdict == V.VERDICT_HELD
    assert r.actual_price == 9960.0
    assert r.diff_pts == 60.0


def test_broke_verdict_when_the_low_is_retested():
    bars = [down_pin(), bar(9950.0, 9955.0, 9850.0, 9860.0, ts=1)]
    p = V.Prediction(V.PRED_PIN_LOW_HELD, "t", 9900.0, S.SIDE_DOWN, True)
    r = V.verify(bars, 0, p, 1)
    assert r.verdict == V.VERDICT_BROKE
    assert r.diff_pts == -50.0


def test_tolerance_zero_means_touching_the_extreme_counts_as_broken():
    """佐证层容差是 0 —— 用户给的 20 只属于 after_pin 一个场景，不许扩散。

    「容差 0 = 正好打到就算破」，与本项目 reaction 层 TOUCH_TOL_PCT=0
    「正好等于线价算触碰」同一口径。用严格 < 会把「打到没超过」算成守住，
    等于对被验的那个判断网开一面。
    """
    assert V.VERIFY_TOL_PTS == 0.0
    bars = [down_pin(), bar(9950.0, 9955.0, 9900.0, 9905.0, ts=1)]
    p = V.Prediction(V.PRED_PIN_LOW_HELD, "t", 9900.0, S.SIDE_DOWN, True)
    assert V.verify(bars, 0, p, 1).verdict == V.VERDICT_BROKE, "正好打到就算破"
    # 给一点容差就变成守住了 —— 证明这块容差确实在起作用，不是摆设
    assert V.verify(bars, 0, p, 1, tol_pts=1.0).verdict == V.VERDICT_HELD
    # 真的破穿，任何容差下都是破
    deep = [down_pin(), bar(9950.0, 9955.0, 9850.0, 9860.0, ts=1)]
    assert V.verify(deep, 0, p, 1).verdict == V.VERDICT_BROKE
    assert V.verify(deep, 0, p, 1, tol_pts=20.0).verdict == V.VERDICT_BROKE


def test_missing_future_bars_is_unknown_not_held():
    """表尾凑不满 N 根 → 无法判定。算成「成立」就是把没验说成验过。"""
    bars = [down_pin(), bar(10000.0, 10020.0, 9960.0, 10015.0, ts=1)]
    p = V.Prediction(V.PRED_PIN_LOW_HELD, "t", 9900.0, S.SIDE_DOWN, True)
    r = V.verify(bars, 0, p, 3)
    assert r.verdict == V.VERDICT_UNKNOWN
    assert r.n_bars == 1
    assert r.actual_price is None


def test_no_prediction_is_not_counted_as_held_or_broken():
    bars = [down_pin(), bar(10000.0, 10020.0, 9960.0, 10015.0, ts=1)]
    p = V.Prediction(V.PRED_NONE, "没有说法", None, None, True)
    r = V.verify(bars, 0, p, 1)
    assert r.verdict == V.VERDICT_NO_PRED


def test_continuation_prediction_is_held_only_when_it_actually_breaks():
    """预言「会破」——破了才叫成立，没破就是被推翻。方向反了会得出相反结论。"""
    p = V.Prediction(V.PRED_PIN_LOW_BROKE, "t", 9900.0, S.SIDE_DOWN, False)
    broke = [down_pin(), bar(9950.0, 9955.0, 9850.0, 9860.0, ts=1)]
    held = [down_pin(), bar(10000.0, 10020.0, 9960.0, 10015.0, ts=1)]
    assert V.verify(broke, 0, p, 1).verdict == V.VERDICT_HELD
    assert V.verify(held, 0, p, 1).verdict == V.VERDICT_BROKE


def test_quiet_prediction_measures_the_largest_excursion_in_either_direction():
    bars = [bar(10000.0, 10005.0, 9990.0, 10000.0, ts=0),
            bar(10000.0, 10050.0, 9950.0, 10010.0, ts=1)]
    p = V.predict(bars, 0, S.judge(bars, 0))
    assert p.kind == V.PRED_QUIET
    assert p.amp == S.WICK_PTS_MIN      # 全幅 15 点，但尺有 40 点的下限
    r = V.verify(bars, 0, p, 1)
    assert r.actual_price == 50.0       # 往上 50 点、往下 50 点，取最大偏离
    assert r.verdict == V.VERDICT_BROKE  # 50 >= 40，被推翻


def test_quiet_prediction_holds_when_the_move_stays_inside_the_ruler():
    bars = [bar(10000.0, 10005.0, 9990.0, 10000.0, ts=0),
            bar(10000.0, 10030.0, 9975.0, 10010.0, ts=1)]
    p = V.predict(bars, 0, S.judge(bars, 0))
    r = V.verify(bars, 0, p, 1)
    assert r.actual_price == 30.0
    assert r.verdict == V.VERDICT_HELD


def test_verify_rejects_a_non_positive_horizon():
    bars = [down_pin()]
    p = V.Prediction(V.PRED_PIN_LOW_HELD, "t", 9900.0, S.SIDE_DOWN, True)
    with pytest.raises(ValueError):
        V.verify(bars, 0, p, 0)


# ---------------------------------------------------------------- 口径指纹
def test_verify_hash_is_stable_and_short():
    h = V.verify_hash()
    assert len(h) == 12
    assert V.verify_hash() == h


def test_verify_hash_covers_the_tolerance():
    """改容差必须换指纹，否则表 6 里混着两套口径而看不出来。"""
    before = V.verify_hash()
    old = V.VERIFY_TOL_PTS
    try:
        V.VERIFY_TOL_PTS = 20.0
        after = V.verify_hash()
    finally:
        V.VERIFY_TOL_PTS = old
    assert after != before
    assert V.verify_hash() == before


def test_verify_hash_covers_the_horizons():
    before = V.verify_hash()
    old = V.VERIFY_HORIZONS
    try:
        V.VERIFY_HORIZONS = (1, 3, 5)
        after = V.verify_hash()
    finally:
        V.VERIFY_HORIZONS = old
    assert after != before


def test_verify_hash_covers_the_prediction_rules_version():
    """阈值指纹认不出「预言规则改了」，这个版本号才是那件事的开关。"""
    before = V.verify_hash()
    old = V.PRED_RULES_VERSION
    try:
        V.PRED_RULES_VERSION = "v2"
        after = V.verify_hash()
    finally:
        V.PRED_RULES_VERSION = old
    assert after != before


def test_verify_params_snapshot_lists_every_horizon():
    p = V.verify_params()
    assert p["horizons"] == list(V.VERIFY_HORIZONS)
    assert p["tol_pts"] == V.VERIFY_TOL_PTS
    assert p["pred_rules_version"] == V.PRED_RULES_VERSION


# ---------------------------------------------------------------- 聚合口径
def test_hit_rate_excludes_unknown_and_no_prediction_from_the_denominator():
    """把「没验」和「没说法」算进分母，等于用缺数据稀释准确率。"""
    slot = {V.VERDICT_HELD: 7, V.VERDICT_BROKE: 3,
            V.VERDICT_UNKNOWN: 90, V.VERDICT_NO_PRED: 100}
    assert V.hit_rate(slot) == 70.0


def test_hit_rate_is_none_when_nothing_was_actually_decided():
    assert V.hit_rate({V.VERDICT_UNKNOWN: 5}) is None


def test_lift_z_reports_the_gap_and_the_sample_significance():
    lift, z = V.lift_z(80, 100, 50.0)
    assert lift == pytest.approx(30.0)
    assert z == pytest.approx(6.0)     # 0.3 / sqrt(.25/100) = 0.3/0.05 = 6


def test_lift_z_is_zero_safe():
    assert V.lift_z(0, 0, 50.0) == (0.0, 0.0)


def test_hit_rate_alone_is_not_a_conclusion():
    """这条测试是方法论钉子，不是算数测试。

    N=1 时「随便一根，本根低点后面不被破」本来就有 ~53%：所以 73% 的
    命中率只相当于「比瞎猜好 20 个点」。把三个数写成同一个数就是骗人。
    """
    assert V.hit_rate({V.VERDICT_HELD: 73, V.VERDICT_BROKE: 27}) == 73.0
    # 73% 命中、基准 53% → 提升 20 点，这才是可以拿去判断的结论
    lift, z = V.lift_z(73, 100, 53.1)
    assert lift == pytest.approx(19.9, abs=0.05)
    assert z > 3


# ---------------------------------------------------------------- 红线
def test_shape_module_never_imports_verify():
    """**红线**：shape.py 一读表 6，判定就在偷看未来，1.7 万根历史结论全废。

    与「shape.py 不许 import forward」是同一条红线，只是方向不同：
    forward 是价格的事后收益，verify 是判断的事后对错。
    """
    with open(os.path.join(ROOT, "ai_trader", "kbar", "shape.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    for bad in ("import verify", "from .verify", "from ai_trader.kbar import verify",
                "kbar_verify"):
        assert bad not in src, "shape.py 里出现了 %r —— 判定层偷看佐证层了" % bad


def test_verify_may_import_shape_but_shape_must_not_import_verify():
    """单向依赖：佐证层读判定（shape），判定绝不读佐证（verify）。"""
    with open(os.path.join(ROOT, "ai_trader", "kbar", "verify.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "from . import shape as S" in src


def test_backfill_tool_exists_and_refuses_to_run_without_table_5(tmp_path):
    """工具在表 5 空的时候必须明确报错，不许静默产出空表。"""
    from ai_trader.kbar import build as B
    from ai_trader.kbar.store import KBarStore

    db = str(tmp_path / "t.db")
    with KBarStore(db) as st:
        st.upsert_evolution([{
            "symbol": "X", "interval_ms": 900000, "ts": 1, "idx": 0,
            "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5,
            "vol_rel": 1.0, "taker_buy_ratio": 0.5}])
    stats = B.build_and_persist_verify(db, "X", 900000)
    assert "error" in stats
    assert "表 5" in stats["error"]
