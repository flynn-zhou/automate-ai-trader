# -*- coding: utf-8 -*-
"""S5 必须真的接在实盘推进路径上（缺口 1 的把关）。

为什么这条测试值得单独写
------------------------

`thesis.run/step` 收 `kbar=None` 是**可选参数**。可选参数意味着：
引擎忘了传它，代码照样跑、测试照样绿、状态机照样成交止损 ——
只是**三道出场闸永远不会触发**。

而这个失效极难发现：
  · 日志里没有任何异常
  · 持仓照样按止损/止盈/失效平掉
  · 统计上只表现为「保本棘轮 0 次触发」，而 0 次触发还能被解释成
    「样本里没有先赚 1R 再回吐的单子」—— 我上一轮就是这么解释的。

所以这里不测「闸判得对不对」（那是 test_s5_reality_exit.py 的事），
只测**线接上了没有**。
"""
import asyncio
from types import SimpleNamespace

import pytest

from ai_trader.config import AppCfg
from ai_trader.settings import SettingsStore
from ai_trader.trading import pending_thesis_store as PTS
from ai_trader.trading import thesis as T
from ai_trader.trading.engine import DecisionEngine

from tests.test_pending_wiring import _DB, _Feed, _RecEx, _bars, _seed_thesis


def _engine(tmp_path, kbar="__unset__"):
    cfg = AppCfg()
    cfg.trading.strategy = "pending"
    cfg.trading.mode = "paper"
    cfg.trading.exec_market = "perp"
    store = SettingsStore(str(tmp_path / "market_settings.db"))
    store.seed_from(cfg.trading)
    store.apply_to(cfg.trading)
    kw = {}
    if kbar != "__unset__":
        kw["pending_kbar"] = kbar
    eng = DecisionEngine(
        cfg, _Feed(), _RecEx(), _DB(), guard=None, settings=store,
        pending_store=PTS.PendingThesisStore(str(tmp_path / "p.db")),
        pending_dry_run=True, **kw)
    return eng, store


class _Spy:
    """替换 thesis.step，记录它被怎么调用。"""

    def __init__(self):
        self.calls = []

    def __call__(self, thesis, bars, book, **kw):
        self.calls.append(kw)
        p = dict(kw.get("progress") or {})
        newp = {"state": p.get("state") or T.FILLED,
                "next_index": int(p.get("next_index") or 0) + 1,
                "armed_at_bar": p.get("armed_at_bar"),
                "best_favorable_px": p.get("best_favorable_px")}
        return T.RunResult(state=newp["state"]), newp


def _run(eng, ts):
    """推进一根。★ ts 必须递增 —— 否则会被 `_pending_5m_ts` 正确去重，
    于是「跨根也要带 kbar」这条根本测不到（第一次就写错过）。"""
    # ★ 必须带 `open_time`：引擎用
    #   `ts = int(getattr(raw5[-1], "open_time", 0) or 0)` 取时间戳，
    #   而 `pending_plan._Bar` 的字段叫 `ts` —— 拿不到就是 0，
    #   于是 `ts <= self._pending_5m_ts` 正确地去重掉，整条测试静默空转。
    def _b(t):
        return SimpleNamespace(interval_ms=300000, open_time=t,
                               open=78000.0, high=78100.0, low=77900.0,
                               close=78050.0, volume=1.0)
    bars = {300000: [_b(ts)],
            900000: [_b(ts - ts % 900000)]}
    asyncio.get_event_loop().run_until_complete(
        eng._step_pending(bars, 78000.0, 50.0, ts))


def test_step_receives_the_kbar_view(tmp_path, monkeypatch):
    """★ 核心断言：`_step_pending` 调 `step()` 时**必须**带 kbar。

    这条红了 = 实盘里 S5 的三道闸静默失效。
    """
    sentinel = {"label": "恐慌盘", "side": "from_below", "vol_rel": 6.0}
    eng, _s = _engine(tmp_path, kbar=sentinel)
    _seed_thesis(eng.pending)
    spy = _Spy()
    monkeypatch.setattr(T, "step", spy)

    _run(eng, 1_000_000)

    assert spy.calls, "step 根本没被调用（预期没推进）"
    kw = spy.calls[-1]
    assert "kbar" in kw, "step() 没收到 kbar —— S5 在实盘里不存在"
    assert kw["kbar"] is sentinel, "传进去的不是注入的那个视图"


def test_missing_kbar_warns_once_and_still_steps(tmp_path, monkeypatch):
    """没接线不许静默：告警一次，但状态机**照常推进**（不能因此拒单）。

    止损/止盈不依赖 kbar，所以缺 kbar 绝不能阻断推进 ——
    否则「缺一个可选输入」会变成「持仓失管」，那比少三道闸严重得多。
    """
    eng, _s = _engine(tmp_path)          # 不传 pending_kbar
    _seed_thesis(eng.pending)
    spy = _Spy()
    monkeypatch.setattr(T, "step", spy)

    _run(eng, 1_000_000)
    assert spy.calls, "没 kbar 也必须推进（止损不能因此失效）"
    assert spy.calls[-1].get("kbar") is None

    risks = [r for r in eng.db.risks if r[0] == "pending_kbar_not_wired"]
    assert risks, "没接线却没留痕 —— 事后分不清「闸没数据」和「闸没触发」"
    n1 = len(risks)

    # 第二次不许再报（否则每根 5m 刷一次，把真告警淹掉）
    _run(eng, 2_000_000)
    n2 = len([r for r in eng.db.risks if r[0] == "pending_kbar_not_wired"])
    assert n2 == n1, "告警重复了 %d 次" % (n2 - n1)


def test_view_is_passed_through_every_bar(tmp_path, monkeypatch):
    """跨根也要一直带：断一根就等于那根上的恶性波动没被看到。"""
    sentinel = {"label": "无·贴线", "side": None, "vol_rel": 0.8}
    eng, _s = _engine(tmp_path, kbar=sentinel)
    _seed_thesis(eng.pending)
    spy = _Spy()
    monkeypatch.setattr(T, "step", spy)

    for i in range(1, 4):
        _run(eng, i * 300_000)

    assert len(spy.calls) >= 3, "只推进了 %d 次" % len(spy.calls)
    assert all(c.get("kbar") is sentinel for c in spy.calls)


def test_kbar_view_module_refuses_a_missing_db(tmp_path):
    """★ 库不存在必须**抛错**，不许静默返回空视图。

    空视图 = 三道闸永不触发，而看板上一笔一笔正常持仓，没人会怀疑。
    """
    from ai_trader.trading.kbar_view import load_kbar_view
    with pytest.raises(FileNotFoundError):
        load_kbar_view(str(tmp_path / "nope.db"))


def test_every_terminal_state_is_not_live(tmp_path):
    """★★ 终态必须**一个不漏**地被 `live()` 排除。

    真实实例：`live()` 里曾经硬编码了六个终态。
    S5 加了 `CLOSED_BREAKEVEN` 之后，被保本棘轮平掉的仓位
    **不在那个名单里** → 它们仍然是“live”，
    于是每根 5m 都会去推进一个已经平掉的仓位。

    ★ 为什么要逐个状态**单独插入并单独断言**：
      一次全插进去再看整体结果，一旦某个状态没插成功
      （主键冲突 / 库里已有同 id），整条断言会静默地变得宽松。
      逐个验就能定位到具体是哪个状态漏了。
    """
    eng, _s = _engine(tmp_path)
    for i, st in enumerate(sorted(T.TERMINAL_STATES)):
        th = T.Thesis(thesis_id="t-%s" % st, kind="k", direction=T.UP,
                      order_price=78000.0, stop_price=77000.0,
                      target_price=82000.0)
        eng.pending.upsert_thesis(PTS.row_of(
            th, state=st, next_index=1, bar_ts=0, note="t"))

    live_states = [r["state"] for r in eng.pending.live(limit=100)]
    assert live_states == [], (
        "这些终态仍被当成 live：%r" % (live_states,))

    # 反向对照：非终态必须能查出来（否则上面那条可能是因为
    # 压根一行都没插进去 —— 那就是空转。这个坑我踩过）
    th = T.Thesis(thesis_id="t-live", kind="k", direction=T.UP,
                  order_price=78000.0, stop_price=77000.0,
                  target_price=82000.0)
    eng.pending.upsert_thesis(PTS.row_of(
        th, state=T.FILLED, next_index=1, bar_ts=0, note="t"))
    assert [r["state"] for r in eng.pending.live(limit=100)] == [T.FILLED]
