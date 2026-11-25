# -*- coding: utf-8 -*-
"""增量推进（生产入口）与回放必须**逐字一致**。

★ 这一份文件防的不是「一个 bug」，是**一类** bug。

  生产环境里引擎每根 K 推进一次状态机，回放/诊断里是一次喂完整段。
  如果这两条路各写一套判定，它们迟早会分叉，而且分叉的方式总是同一个：
某一条路多判了一根、少判了一根、或者把「挂单时刻」这种由历史派生的值
  丢掉了 —— 全是**静默**的，跑完看不出来，只有对账时账不平。

  所以这里的第一条是不变式测试：
      `run(全段)`  ≡  `for 每根: step(bars[:k+1])`

  而且用**真实计划**（走 smoke 的同一套数据加载）跑，不用玩具数据 ——
  玩具数据只会告诉你构造函数没错。
"""
import importlib.util
import os
import sys
from dataclasses import replace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai_trader.trading import pending_plan as P      # noqa: E402
from ai_trader.trading import pending_thesis_store as S  # noqa: E402
from ai_trader.trading import thesis as T            # noqa: E402

M15 = T.BAR_INTERVAL_MS
M5 = 300_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % 1_800_000)


class Bar:
    def __init__(self, ts, o, h, l, c):
        self.ts, self.o, self.h, self.l, self.c = ts, o, h, l, c


def _book(n=12, price=1e6):
    bk = T.LineBook()
    for i in range(n):
        bk.add({"line_key": T.SLOW_LINE_KEY, "ts": T0 + i * M15,
                "line_price": price, "line_ready_ms": T0 + i * M15 + M15})
    return bk


def _long(**kw):
    base = dict(thesis_id="t", kind="测试", direction=T.UP, order_price=100.0,
                stop_price=90.0, target_price=200.0,
                order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL)
    base.update(kw)
    return T.Thesis(**base)


def _flat_bars(n, c=100.0):
    return [Bar(T0 + i * M15, c, c + 1.0, c - 1.0, c) for i in range(n)]


# ============================================================ 一、不变式

def _trans_key(tr):
    return (int(tr.bar_ts), tr.from_state, tr.to_state, tr.reason)


def _assert_same(a, b, what):
    assert a.state == b.state, "%s：终态分叉 run=%s fold=%s" % (what, a.state, b.state)
    ka = [_trans_key(t) for t in a.transitions]
    kb = [_trans_key(t) for t in b.transitions]
    assert ka == kb, ("%s：转换序列分叉\nrun =%r\nfold=%r" % (what, ka, kb))
    assert len(a.actions) == len(b.actions), (
        "%s：动作数量分叉 run=%d fold=%d" % (what, len(a.actions), len(b.actions)))
    assert a.unresolved_reason == b.unresolved_reason


def _fold(thesis, bars, book, claim=None, **kw):
    """逐根推进，把每段的转换拼起来 —— 这就是生产里引擎做的事。"""
    merged = T.RunResult(state=T.EXPECTED)
    prog = None
    for k in range(len(bars)):
        res, prog = T.step(thesis, bars[:k + 1], book, claim, progress=prog, **kw)
        merged.transitions.extend(res.transitions)
        merged.actions.extend(res.actions)
        merged.ambiguous.extend(res.ambiguous)
        merged.state = res.state
        merged.unresolved_reason = res.unresolved_reason
    return merged, prog


def test_fold_equals_replay_on_a_stopped_out_trade():
    th = _long()
    bars = _flat_bars(8)
    bars[3] = Bar(T0 + 3 * M15, 100.0, 101.0, 85.0, 86.0)
    book = _book(8)
    a = T.run(th, bars, book, start_at=0)
    b, prog = _fold(th, bars, book)
    _assert_same(a, b, "止损")
    assert prog["state"] == T.CLOSED_SL


def test_fold_equals_replay_when_still_armed():
    th = _long(order_price=10.0, stop_price=5.0)   # 挂单价远在下方 → 一直挂着
    bars = _flat_bars(10)
    book = _book(10)
    a = T.run(th, bars, book, start_at=0)
    b, prog = _fold(th, bars, book)
    _assert_same(a, b, "一直挂着")
    assert prog["state"] == T.ARMED
    assert a.unresolved_reason and b.unresolved_reason


def test_fold_equals_replay_with_stale_cancel():
    """★ 这一条是「丢派生值」的专门探针。

    预期衰减判据用的是 `armed_ref_close`（挂单那根的收盘）。
    续跑时如果没把它接回来，衰减永远算不出来 —— 而且**不报错**。
    """
    th = _long(order_price=95.0, stop_price=90.0, target_price=200.0,
               horizon_bars=0, stale_distance=4.0)
    closes = [100.0, 100.5, 102.0, 104.0, 106.0, 108.0]
    bars = [Bar(T0 + i * M15, c, c + 0.4, c - 0.4, c) for i, c in enumerate(closes)]
    book = _book(len(bars))
    a = T.run(th, bars, book, start_at=0)
    assert a.state == T.CANCELLED, a.state
    b, prog = _fold(th, bars, book)
    _assert_same(a, b, "预期衰减")
    assert prog["armed_ref_close"] is not None, "续跑丢了挂单参考价"


def test_fold_equals_replay_with_horizon_expiry():
    th = _long(order_price=10.0, stop_price=5.0, horizon_bars=3)
    bars = _flat_bars(10)
    book = _book(10)
    a = T.run(th, bars, book, start_at=0)
    assert a.state == T.EXPIRED, a.state
    b, prog = _fold(th, bars, book)
    _assert_same(a, b, "超期")
    assert prog["armed_at_bar"] is not None, "续跑丢了挂单根号（超期判据靠它）"


def test_fold_equals_replay_with_5m_entry_and_both_rhythms():
    """双节奏（15m 判失效 / 5m 判成交出场）下也必须一致。"""
    th = _long()
    bars = _flat_bars(12)
    b5 = [Bar(T0 + j * M5, 100.0, 100.2, 99.8, 100.0) for j in range(36)]
    b5[7] = Bar(T0 + 7 * M5, 100.0, 100.2, 89.0, 89.5)     # 第 3 根 15m 里砸穿止损
    book = _book(12)
    a = T.run(th, bars, book, entry_bars=b5, start_at=0)
    b, _ = _fold(th, bars, book, entry_bars=b5)
    assert a.state == T.CLOSED_SL, a.state
    _assert_same(a, b, "双节奏")


def test_stepping_a_terminal_thesis_is_idempotent():
    """★ 进了终态再推，一个字都不许再多出来。

    生产里每根 K 都会把所有 thesis 推一遍，包括已经结束的。
    只要它多冒一条转换，就会多平一次仓。
    """
    th = _long()
    bars = _flat_bars(4)
    bars[2] = Bar(T0 + 2 * M15, 100.0, 101.0, 85.0, 86.0)
    book = _book(4)
    res, prog = T.step(th, bars, book)
    assert prog["state"] == T.CLOSED_SL
    n_tr, n_act = len(res.transitions), len(res.actions)
    for k in range(1, 5):
        again, prog = T.step(th, bars + _flat_bars(k), book, progress=prog)
        assert again.transitions == [], "终态又多推了转换：%r" % (
            [_trans_key(t) for t in again.transitions],)
        assert again.actions == []
    assert n_tr and not n_act == 0


# ============================================================ 二、真实计划

def _real_plans():
    """走 smoke 同一套数据加载，拿真实计划 —— 玩具数据看不出这类分叉。"""
    spec = importlib.util.spec_from_file_location(
        "smoke", os.path.join(_ROOT, "tools", "_smoke_pending_plan.py"))
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    b15 = smoke.load_evolution(900_000)
    b5 = smoke.load_evolution(300_000)
    b1h = smoke.load_bars_1h()
    book = smoke.load_line_book()
    wins = smoke.build_windows(b1h, book, smoke.load_range_state())
    ctx = P.PlanContext(bars15=b15, book=book, bars_1h=b1h, bars_5m=b5,
                        windows=wins)
    plans, _ = P.detect_all(ctx, require_window=True, include_mirror=True)
    return plans, b15, b5, book


def test_real_plans_fold_equals_replay():
    """★ 真实计划上逐条对比「一次跑完」与「逐根推进」。

    ★ 每条计划只切一段窗口（计划那根起 240 根 15m = 2.5 天）：
      全量序列是几万根，逐根推进是 O(n²)，跑一次十几分钟 ——
      那样的测试没人会在改代码时真去跑，等于没有。
      切窗口不削弱这条不变式：分叉只会发生在「推进的接缝」上，
      240 个接缝足够暴露游标/派生值/同根重复判的问题。
    """
    plans, b15, b5, book = _real_plans()
    assert len(plans) >= 10, "样本变少了（%d 条），这条不变式就白测了" % len(plans)
    # ★ 2026-11-20：三引线修好之后，真实计划的量级从个位数涨到数千条
    #   （窗口闸不再拦它 + 形状判据换成纯位置关系）。逐条 × 240 步是 O(n²)，
    #   全跑要十几分钟 —— 又回到这条 docstring 说的「等于没有」。
    #   这里**等距抽样** 40 条：覆盖整段时间轴，秒级跑完。
    #   抽样不削弱不变式：它验的是「推进接缝上 fold 与 replay 是否分叉」,
    #   任取 40 个接缝都能暴露游标/派生值/同根重复判的问题。
    _stride = max(1, len(plans) // 40)
    sample = plans[::_stride][:40]
    params = replace(P.PendingParams(), stale_stop_mult=0.0)
    bad = []
    for p in sample:
        th = P.to_thesis(p, params=params)
        start = P._b15_index_for_plan(p, b15)
        lo = max(0, start - 1)
        seg15 = list(b15[lo:lo + 240])
        if len(seg15) < 20:
            continue
        t_lo, t_hi = int(seg15[0].ts), int(seg15[-1].ts) + M15
        seg5 = [b for b in b5 if t_lo <= int(b.ts) < t_hi]
        claim = P.claim_for(p)
        kw = dict(claim=claim, start_at=0)
        if p.claim_on_entry:
            kw.update(entry_bars=seg5, claim_on_entry=True)
        elif seg5:
            kw.update(entry_bars=seg5)
        a = T.run(th, seg15, book, **kw)

        prog = None
        merged = []
        for k in range(len(seg15)):
            cur = {k2: v for k2, v in kw.items() if k2 != "start_at"}
            res, prog = T.step(th, seg15[:k + 1], book, progress=prog, **cur)
            merged.extend(res.transitions)
        if a.state != prog["state"]:
            bad.append((p.kind, p.direction, a.state, prog["state"]))
        elif [_trans_key(t) for t in a.transitions] != \
                [_trans_key(t) for t in merged]:
            bad.append((p.kind, p.direction, "转换序列分叉",
                        [_trans_key(t) for t in a.transitions],
                        [_trans_key(t) for t in merged]))
    assert not bad, "回放与增量分叉：%r" % (bad,)


# ============================================================ 三、落库

def test_store_roundtrip_resumes_to_the_same_terminal_state(tmp_path):
    """存一半 → 关掉 → 重开 → 接着跑，终态必须和一口气跑完一致。"""
    th = _long()
    bars = _flat_bars(9)
    bars[6] = Bar(T0 + 6 * M15, 100.0, 101.0, 85.0, 86.0)
    book = _book(9)
    want = T.run(th, bars, book, start_at=0)

    path = str(tmp_path / "t.db")
    st = S.PendingThesisStore(path)
    st.upsert_thesis(S.row_of(th, state=T.EXPECTED, next_index=0, bar_ts=T0))
    prog = S.progress_of(st.get(th.thesis_id))
    for k in range(5):                                  # 只推到第 5 根
        res, prog = T.step(th, bars[:k + 1], book, progress=prog)
        row = st.get(th.thesis_id)
        row.update(prog)
        st.upsert_thesis(row)
    st.close()

    st2 = S.PendingThesisStore(path)                     # 模拟重启
    row = st2.get(th.thesis_id)
    assert row["next_index"] == 5 and row["state"] == T.FILLED, row
    th2 = S.thesis_of(row)
    prog2 = S.progress_of(row)
    for k in range(5, len(bars)):
        res, prog2 = T.step(th2, bars[:k + 1], book, progress=prog2)
        row.update(prog2)
        st2.upsert_thesis(row)
    assert prog2["state"] == want.state == T.CLOSED_SL, (prog2, want.state)
    st2.close()


def test_thesis_of_rebuilds_every_field_that_changes_behaviour():
    """★ 重建漏一个字段 = 重放结果变掉，而且不报错。

    `surprise_pts` / `stale_distance` / `family` 任何一个丢了，
    出来的终态就可能完全不同（一个该止盈的变成拿着、一个该撤的继续挂）。
    """
    th = _long(stale_distance=7.0, surprise_pts=500.0, horizon_bars=12,
               family=T.FAMILY_TREND, order_type=T.ORDER_STOP)
    row = S.row_of(th, state=T.ARMED, next_index=3, bar_ts=T0)
    back = S.thesis_of(row)
    for f in ("thesis_id", "kind", "direction", "family", "order_price",
              "stop_price", "target_price", "order_type", "horizon_bars",
              "stale_distance", "surprise_pts"):
        assert getattr(back, f) == getattr(th, f), "重建丢了 %s" % f
    # `bar_ts` 不在 `Thesis` 上（它属于计划），所以单独核对它进了行里
    assert int(row["bar_ts"]) == T0


def test_action_ids_are_idempotent_across_restarts(tmp_path):
    """同一条动作在重启后被重新算出来，必须认出「已经做过」。"""
    path = str(tmp_path / "a.db")
    st = S.PendingThesisStore(path)
    first = st.put_action(thesis_id="t1", kind="close", state_after=T.CLOSED_SL,
                          bar_ts=T0 + 3 * M15, price=90.0)
    assert first is True
    st.close()

    st2 = S.PendingThesisStore(path)
    again = st2.put_action(thesis_id="t1", kind="close",
                           state_after=T.CLOSED_SL, bar_ts=T0 + 3 * M15,
                           price=90.0)
    assert again is False, "重启后同一条动作被判成新的 → 止损会被平第二次"
    assert len(st2.pending_actions("t1")) == 1
    st2.mark_applied("t1", "close", T0 + 3 * M15)
    assert st2.pending_actions("t1") == []
    st2.close()


def test_live_excludes_terminal_rows(tmp_path):
    st = S.PendingThesisStore(str(tmp_path / "b.db"))
    st.upsert_thesis(S.row_of(_long(), state=T.ARMED, next_index=2, bar_ts=T0))
    st.upsert_thesis(S.row_of(_long(thesis_id="t2"), state=T.CLOSED_SL,
                             next_index=9, bar_ts=T0 + M15))
    assert [r["thesis_id"] for r in st.live()] == ["t"]
    assert len(st.all()) == 2
    st.close()


def test_surprise_pts_is_persisted_through_plan():
    """`PendingParams.surprise_pts` → `to_thesis` → 落库行，一路上不许掉。"""
    params = P.PendingParams()
    assert hasattr(params, "surprise_pts"), "PendingParams 没有惊喜K线阈值"
    th = _long(surprise_pts=params.surprise_pts)
    row = S.row_of(th, state=T.ARMED, next_index=0, bar_ts=T0)
    assert float(row["surprise_pts"]) == float(params.surprise_pts)
