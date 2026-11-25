# -*- coding: utf-8 -*-
"""缺口 2 的把关：计划源必须真的产出、而且**只产当前这一根**。

这个测试防的是什么
------------------

`detect_all()` 扫全历史，真实数据下 180 天返回 **11 条**计划。
不按时间过滤就全插进仓库的后果：

  · 引擎开盘那一刻按**几个月前的价位**挂出 11 张单
  · 每一张都通过 `validate()`：价位自洽、止损合规、方向正确
  · 不报错、不告警，表现只是「一开机就满仓」

所以这里钉两件事：
  1. 只收 `effective_claim_ts == 当前 15m` 的计划
  2. `add_if_new` 不许覆盖已存在的预期（否则平掉的仓会被重置重开）
"""
from types import SimpleNamespace

import pytest

from ai_trader.config import AppCfg
from ai_trader.settings import SettingsStore
from ai_trader.trading import pending_thesis_store as PTS
from ai_trader.trading import thesis as T
from ai_trader.trading.engine import DecisionEngine

from tests.test_pending_wiring import _DB, _Feed, _RecEx


def _engine(tmp_path, source="__unset__", kbar=None):
    cfg = AppCfg()
    cfg.trading.strategy = "pending"
    cfg.trading.mode = "paper"
    cfg.trading.exec_market = "perp"
    store = SettingsStore(str(tmp_path / "market_settings.db"))
    store.seed_from(cfg.trading)
    store.apply_to(cfg.trading)
    kw = {"pending_kbar": kbar}
    if source != "__unset__":
        kw["pending_source"] = source
    return DecisionEngine(
        cfg, _Feed(), _RecEx(), _DB(), guard=None, settings=store,
        pending_store=PTS.PendingThesisStore(str(tmp_path / "p.db")),
        pending_dry_run=True, **kw)


def _plan(claim_ts, order=78000.0, stop=79000.0, direction=T.DOWN, kind="k"):
    from ai_trader.trading.pending_plan import Plan
    return Plan(kind=kind, direction=direction, order_type=T.ORDER_LIMIT,
                bar_index=0, bar_ts=claim_ts, order_price=order,
                stop_price=stop, target_price=order - 4 * abs(order - stop),
                claim_ts=claim_ts, family=T.FAMILY_TREND)


def _b15(ts):
    return [SimpleNamespace(ts=ts, o=1, h=2, l=1, c=1)]


def test_only_the_current_bar_is_harvested(tmp_path):
    """★★ 历史计划一条都不许进来。"""
    cur = 1_790_000_000_000
    old = cur - 90 * 24 * 3_600_000          # 三个月前
    source = lambda params: ([_plan(old), _plan(cur)], {})
    eng = _engine(tmp_path, source=source)

    eng._pending_harvest(_b15(cur))

    rows = eng.pending.all(limit=50)
    assert len(rows) == 1, "采到了 %d 条（历史计划漏进来了）" % len(rows)
    assert int(rows[0]["bar_ts"]) == cur


def test_harvest_does_not_reset_an_existing_thesis(tmp_path):
    """★ 已存在的预期不许被新计划覆盖。

    `upsert_thesis()` 是 `ON CONFLICT DO UPDATE`。拿它插新计划，
    一旦 thesis_id 撞上（重启后重算同一根 K），就会把一条**已平掉的**
    预期重置回 EXPECTED，然后重新挂单、重新成交 —— 一次开仓变两次。
    """
    cur = 1_790_000_000_000
    eng = _engine(tmp_path, source=lambda p: ([_plan(cur)], {}))

    eng._pending_harvest(_b15(cur))
    rows = eng.pending.all(limit=10)
    tid = rows[0]["thesis_id"]

    # 人为把它推到终态
    th = PTS.thesis_of(rows[0])
    eng.pending.upsert_thesis(PTS.row_of(th, state=T.CLOSED_SL,
                                         next_index=5, bar_ts=cur))

    eng._pending_harvest_ts = 0              # 允许再采一次同一根
    eng._pending_harvest(_b15(cur))

    after = eng.pending.all(limit=10)
    assert len(after) == 1, "又插了一条重复计划"
    assert after[0]["state"] == T.CLOSED_SL, (
        "已平掉的预期被重置成 %s —— 会被重新挂单" % after[0]["state"])


def test_same_bar_is_harvested_once(tmp_path):
    """一根 15m 里重复调用只扫一次全库（`detect_all` 要扫 1.7 万根）。"""
    calls = []

    def source(params):
        calls.append(1)
        return ([], {})

    cur = 1_790_000_000_000
    eng = _engine(tmp_path, source=source)
    for _ in range(5):
        eng._pending_harvest(_b15(cur))
    assert len(calls) == 1, "同一根 15m 扫了 %d 次全库" % len(calls)


def test_missing_source_warns_once(tmp_path):
    """没接计划源必须留痕 —— 否则就是「完美管理空集合」。"""
    eng = _engine(tmp_path)
    cur = 1_790_000_000_000
    eng._pending_harvest(_b15(cur))
    eng._pending_harvest(_b15(cur + 900_000))
    kinds = [k for k, _ in eng.db.risks if k == "pending_source_not_wired"]
    assert len(kinds) == 1, "告警 %d 次" % len(kinds)
    assert eng.pending.all(limit=10) == []


def test_source_error_does_not_kill_existing_positions(tmp_path):
    """计划源抛错只影响「这根不采集」，不许影响已有持仓的推进。

    ★ 反过来的话，源里一个坏 SQL 就能让所有持仓停止被判止损。
    """
    def boom(params):
        raise RuntimeError("源坏了")

    cur = 1_790_000_000_000
    eng = _engine(tmp_path, source=boom)
    th = T.Thesis(thesis_id="t1", kind="k", direction=T.UP,
                  order_price=78000.0, stop_price=77000.0,
                  target_price=82000.0)
    eng.pending.upsert_thesis(PTS.row_of(
        th, state=T.FILLED, next_index=0, bar_ts=cur))

    eng._pending_harvest(_b15(cur))          # 不许抛出去

    assert any(k == "pending_source_error" for k, _ in eng.db.risks)
    assert len(eng.pending.live()) == 1, "已有持仓被计划源错误连带清掉了"


def test_plan_key_is_stable_and_content_based(tmp_path):
    """身份键必须由内容构造（`Plan` 上没有 plan_id 字段）。"""
    a = _plan(1_790_000_000_000)
    b = _plan(1_790_000_000_000)
    c = _plan(1_790_000_000_000, order=78100.0)
    ka, kb, kc = (DecisionEngine._pending_plan_key(x) for x in (a, b, c))
    assert ka == kb, "同一条计划两次算出不同的键"
    assert ka != kc, "不同价位的计划算出同一个键"
