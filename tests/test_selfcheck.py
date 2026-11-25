# -*- coding: utf-8 -*-
"""链路自检单的测试。

重点不是「能不能下单」（那要真钱），而是这些容易静默错的地方：
  · 请求文件读走即删 → 不会重复下单
  · 坏文件必须丢掉 → 不会每根 5m 刷同一个错
  · 身份只认前缀，终态才算出局 → 不会重复开仓
  · 定价走公式参数 → 不是振幅，也没有数字字面量
  · 游标落在最后一根 15m → 不会从历史第一根重放成交
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from ai_trader.trading import pending_plan as P
from ai_trader.trading import selfcheck as SC
from ai_trader.trading import thesis as T

D_MAX = 1.6181818181818184


def _cfg(tmp_path, mode="live"):
    return SimpleNamespace(
        store=SimpleNamespace(path=str(tmp_path / "trader.db")),
        trading=SimpleNamespace(mode=mode),
    )


class FakeDB(object):
    def __init__(self):
        self.risks = []

    def log_risk(self, kind, detail=""):
        self.risks.append((kind, detail))


class FakePending(object):
    def __init__(self, rows=None):
        self.rows = list(rows or ())
        self.added = []

    def live(self, limit=200):
        return self.rows

    def add_if_new(self, row):
        self.added.append(row)
        self.rows.append(row)
        return True


class FakeExecutor(object):
    def __init__(self, price=100000.0):
        self.price = price
        self.placed = []

    async def mark_price(self):
        return self.price


class FakeEngine(object):
    def __init__(self, dry=False, pos=None, rows=None, price=100000.0):
        self.ex = FakeExecutor(price)
        self.pending = FakePending(rows)
        self.db = FakeDB()
        self._dry = dry
        self.pos = pos or {}
        self.placed = []

    def _pending_is_dry(self):
        return self._dry

    def _pending_params(self):
        return P.PendingParams(max_stop_pct=D_MAX)

    async def _pending_place(self, act, thesis_obj):
        # ★ 契约：返回 True = 这张单真的挂出去了。`fire_once` 现在检查这个返回值
        #   （挂单没成功却报 fired=True，等于自检自己失效 —— 2026-11-24 事故：
        #     -4015 让每一张挂单都被拒，自检却每轮都报「已下链路自检单」）。
        #   模拟挂单失败的用例把 self.place_ok 置 False。
        self.placed.append((act, thesis_obj))
        return bool(getattr(self, "place_ok", True))

    async def _pending_cancel(self, act):
        self.placed.append(("cancel", act))

    async def _pending_close(self, act, thesis_obj):
        self.placed.append(("close", act))


def _bars(n=8, start=1_800_000_000_000, step=900_000):
    return [P._Bar(start + i * step, 100.0, 101.0, 99.0, 100.0) for i in range(n)]


# ----------------------------------------------------------- 请求文件

def test_arm_then_take_returns_payload_once(tmp_path):
    cfg = _cfg(tmp_path)
    path = SC.arm(cfg, direction=T.DOWN)
    assert os.path.exists(path)
    assert SC.take_request(cfg)["direction"] == T.DOWN
    assert SC.take_request(cfg) is None          # 读走即删
    assert not os.path.exists(path)


def test_arm_rejects_bad_direction(tmp_path):
    with pytest.raises(ValueError):
        SC.arm(_cfg(tmp_path), direction="SIDEWAYS")


def test_broken_request_file_is_dropped_not_retried(tmp_path):
    """坏 JSON 必须删掉：否则服务每根 5m 重踩同一个坑，日志被刷满。"""
    cfg = _cfg(tmp_path)
    path = SC.request_path(cfg)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert SC.take_request(cfg) is None
    assert not os.path.exists(path)
    assert SC.take_request(cfg) is None          # 第二次就是「没有请求」


def test_disarm_reports_whether_it_deleted(tmp_path):
    cfg = _cfg(tmp_path)
    p = SC.arm(cfg)
    assert SC.disarm(p) is True
    assert SC.disarm(p) is False


def test_cancel_request_uses_its_own_file(tmp_path):
    cfg = _cfg(tmp_path)
    SC.arm(cfg)
    SC.arm_cancel(cfg)
    assert os.path.exists(SC.request_path(cfg))
    assert os.path.exists(SC.cancel_path(cfg))
    assert SC.take_cancel(cfg)["action"] == SC.ACTION_CANCEL
    assert os.path.exists(SC.request_path(cfg))   # 互不影响


# ------------------------------------------------------------- 身份

def test_selfcheck_id_prefix_roundtrip():
    tid = SC.selfcheck_id(1_800_000_000_000)
    assert SC.is_selfcheck_id(tid)
    assert not SC.is_selfcheck_id("abc-123")
    assert not SC.is_selfcheck_id(None)


def test_live_selfcheck_rows_only_keeps_selfcheck_and_unfinished():
    tid = SC.selfcheck_id(1)
    rows = [
        {"thesis_id": tid, "state": T.ARMED},
        {"thesis_id": SC.selfcheck_id(2), "state": T.EXPIRED},
        {"thesis_id": "normal-1", "state": T.ARMED},
    ]
    out = SC.live_selfcheck_rows(rows)
    assert [r["thesis_id"] for r in out] == [tid]


# ------------------------------------------------------------- 定价

def test_selfcheck_stop_is_formula_not_amplitude():
    """公式止损 ~1.5% 量级；振幅止损是 0.135% 量级。差一个数量级。"""
    pp = SC.build_prices(100000.0, T.UP, P.PendingParams(max_stop_pct=D_MAX), 200.0, 40.0)
    pct = (pp.order_price - pp.stop_price) / pp.order_price * 100.0
    assert pct > 1.0, "止损落回了振幅规则（%.4f%%）" % pct
    assert pct <= D_MAX + 1e-9
    assert pp.target_price > pp.order_price


def test_selfcheck_prices_follow_the_parameter_not_a_literal():
    """参数翻倍，止损距离跟着翻倍 —— 证明代码里没有硬编码距离。"""
    a = SC.build_prices(100000.0, T.UP, P.PendingParams(max_stop_pct=D_MAX), 200.0, 40.0)
    b = SC.build_prices(100000.0, T.UP, P.PendingParams(max_stop_pct=D_MAX * 2), 200.0, 40.0)
    ra = (a.order_price - a.stop_price) / a.order_price
    rb = (b.order_price - b.stop_price) / b.order_price
    assert rb > ra * 1.5


def test_selfcheck_refuses_params_without_formula_stop():
    """max_stop_pct=0 会让止损静默落回振幅 —— 必须直接拒，不许将就。"""
    with pytest.raises(ValueError):
        SC.build_prices(100000.0, T.UP, P.PendingParams(), 200.0, 40.0)


def test_selfcheck_direction_puts_stop_on_the_right_side():
    up = SC.build_prices(100000.0, T.UP, P.PendingParams(max_stop_pct=D_MAX), 200.0, 40.0)
    dn = SC.build_prices(100000.0, T.DOWN, P.PendingParams(max_stop_pct=D_MAX), 200.0, 40.0)
    assert up.stop_price < up.order_price < up.target_price
    assert dn.target_price < dn.order_price < dn.stop_price


# ------------------------------------------------------------- 拒绝

def test_refusal_blocks_non_live_mode(tmp_path):
    eng = FakeEngine()
    why = SC.refusal(eng, _cfg(tmp_path, mode="paper"))
    assert "live" in why


def test_refusal_blocks_dry_run(tmp_path):
    eng = FakeEngine(dry=True)
    assert "dry-run" in SC.refusal(eng, _cfg(tmp_path))


def test_refusal_blocks_when_position_exists(tmp_path):
    eng = FakeEngine(pos={"side": "long", "qty": 0.002})
    why = SC.refusal(eng, _cfg(tmp_path))
    assert "已有持仓" in why


def test_refusal_blocks_second_selfcheck(tmp_path):
    tid = SC.selfcheck_id(1)
    eng = FakeEngine(rows=[{"thesis_id": tid, "state": T.ARMED}])
    why = SC.refusal(eng, _cfg(tmp_path))
    assert "未结束的自检单" in why


def test_refusal_allows_clean_state(tmp_path):
    assert SC.refusal(FakeEngine(), _cfg(tmp_path)) == ""


# ------------------------------------------------------------- 执行

def test_fire_once_without_request_does_nothing(tmp_path):
    eng = FakeEngine()
    res = asyncio.run(SC.fire_once(eng, _bars(), _cfg(tmp_path)))
    assert res["fired"] is False
    assert eng.placed == []


def test_fire_once_places_then_writes_row_with_last_bar_cursor(tmp_path):
    cfg = _cfg(tmp_path)
    SC.arm(cfg, direction=T.UP)
    eng = FakeEngine()
    bars = _bars(8)
    res = asyncio.run(SC.fire_once(eng, bars, cfg))

    assert res["fired"] is True
    assert SC.is_selfcheck_id(res["thesis_id"])
    # 真的走了引擎的下单路径
    assert len(eng.placed) == 1
    act, th = eng.placed[0]
    assert act.kind == "place_order" and act.side == th.side
    # 游标必须落在最后一根 15m，否则状态机从历史第一根重放成交
    row = eng.pending.added[0]
    assert row["next_index"] == len(bars) - 1
    assert row["state"] == T.ARMED
    assert row["bar_ts"] == bars[-1].ts
    # 请求文件已消费
    assert SC.take_request(cfg) is None
    assert ("selfcheck_fired" in [k for k, _ in eng.db.risks])


def test_fire_once_refuses_and_logs_risk_when_blocked(tmp_path):
    cfg = _cfg(tmp_path)
    SC.arm(cfg)
    eng = FakeEngine(dry=True)
    res = asyncio.run(SC.fire_once(eng, _bars(), cfg))
    assert res["fired"] is False
    assert eng.placed == []
    assert "selfcheck_refused" in [k for k, _ in eng.db.risks]


def test_fire_once_reports_failure_when_order_is_not_placed(tmp_path):
    """★ 挂单没挂出去 → 必须报 fired=False，且**不能**记 ARMED。

    2026-11-24 事故：-4015 让每一张挂单都被交易所拒，自检单却每轮都
    报「已下链路自检单」，还把 thesis 记成 ARMED —— 自检验证的是
    「我调了下单函数」，不是「单挂出去了」，于是它替一个坏链路背书。
    """
    cfg = _cfg(tmp_path)
    SC.arm(cfg, direction=T.UP)
    eng = FakeEngine()
    eng.place_ok = False
    res = asyncio.run(SC.fire_once(eng, _bars(8), cfg))

    assert res["fired"] is False
    assert eng.pending.added == [], "没挂出去的单被记成了 ARMED"
    assert "selfcheck_place_failed" in [k for k, _ in eng.db.risks]
    # 注意：请求文件在 `fire_once` 入口就被 `take_request` 消费掉了
    # （既有行为，本次不改）—— 重试要重新 arm，这里不断言它会留下。


def test_fire_once_is_idempotent_across_two_calls(tmp_path):
    """请求只消费一次：同一份请求调两次只会下一单。"""
    cfg = _cfg(tmp_path)
    SC.arm(cfg)
    eng = FakeEngine()
    first = asyncio.run(SC.fire_once(eng, _bars(), cfg))
    second = asyncio.run(SC.fire_once(eng, _bars(), cfg))
    assert first["fired"] is True
    assert second["fired"] is False
    assert len(eng.placed) == 1
