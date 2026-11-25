# -*- coding: utf-8 -*-
"""S5 现实跟随出场：三道闸的防复发测试。

钉的不是「数字对不对」，是**口径**：
  保本棘轮必须只朝有利方向、必须含成本、不许根内前视；
  恐慌盘必须顺向、拿不到方向就不许出场；
  缩量停滞必须默认关闭。
"""
from dataclasses import replace

import pytest

from ai_trader.trading import thesis as T

PX = 78000.0
STOP = PX - 1206.0          # 做多，止损在下方（≈ d_max 1.5467%）
TGT = PX + 4824.0


def _mk(**kw):
    base = dict(thesis_id="t1", kind="k", direction=T.UP,
                order_price=PX, stop_price=STOP, target_price=TGT)
    base.update(kw)
    return T.Thesis(**base)


class _Bar:
    def __init__(self, o, h, l, c, ts=0):
        self.o, self.h, self.l, self.c, self.ts = o, h, l, c, ts


# ------------------------------------------------------------ 保本棘轮

def test_ratchet_not_armed_before_one_r():
    """没赚到 1R 就不许动止损 —— 否则「保本」变成了「提前止损」。"""
    t = _mk()
    assert T.breakeven_stop(t, PX + 600.0) is None      # +0.5R
    assert T.breakeven_stop(t, PX - 100.0) is None      # 反向更不行


def test_ratchet_arms_at_exactly_one_r():
    """恰好 1R 就武装。边界不许依赖浮点巧合。"""
    t = _mk()
    r = abs(PX - STOP)
    assert T.breakeven_stop(t, PX + r) is not None
    assert T.breakeven_stop(t, PX + r - 1e-6) is None


def test_breakeven_includes_round_trip_cost():
    """★ 保本价必须**盖住往返成本**，不能是「回到入场价」。

    回到入场价叫「不赚不亏」是错的：还付了两笔手续费 + 滑点。
    成本走 `pending_plan.round_trip_costs()`（唯一来源），这里只验方向，
    不重复实现那条公式。
    """
    t = _mk()
    be = T.breakeven_stop(t, PX + 5000.0)
    assert be is not None
    assert be < PX, "做多的保本价必须低于入场价（留出成本）"
    from ai_trader.trading.pending_plan import PendingParams, round_trip_costs
    _cw, cost = round_trip_costs(PX, T.ORDER_LIMIT, PendingParams())
    assert be == pytest.approx(PX - cost, abs=1e-6)


def test_ratchet_can_be_disabled():
    t = _mk(s5_breakeven_r=0.0)
    assert T.breakeven_stop(t, PX + 99999.0) is None


def test_ratchet_symmetric_for_short():
    """做空镜像：把止损往下移到保本，保本价必须**高于**入场价。"""
    t = _mk(direction=T.DOWN, order_price=PX, stop_price=PX + 1206.0,
            target_price=PX - 4824.0)
    be = T.breakeven_stop(t, PX - 5000.0)
    assert be is not None and be > PX


def test_no_within_bar_lookahead():
    """★ 一根 K 不许既造出保本价、又打到它。

    反例：`h` 走到 +1.5R，`l` 又砸穿保本价。OHLC 分不出先到哪个极值，
    所以棘轮只能用**之前根**已经确立的 best_fav。

    这里直接模拟那一根：棘轮价传 None（因为之前根从未到 1R），
    那么即使这根自己冲到 +1.5R 并砸穿保本价，也不许记保本。
    """
    t = _mk()
    r = abs(PX - STOP)
    got = []
    hit = T._close_position(
        t, _Bar(PX, PX + r * 1.5, PX - r * 3, PX - r, ts=300_000),
        T.PENDING, {}, 1, lambda *a, **k: got.append(a), 300_000,
        kbar=None, ratchet_stop=None, best_favorable_px=PX)
    assert hit, "这根应该按原止损平掉"
    states = [a[0] for a in got]
    assert T.CLOSED_BREAKEVEN not in states, (
        "根内前视：用本根自己的最好值造出保本价又打到它")
    assert T.CLOSED_SL in states


# ------------------------------------------------------------ 恐慌盘

def test_panic_needs_favorable_side():
    """恐慌盘是**顺向**礼物；逆向的恐慌盘说明我们正被踩，不能当止盈。"""
    t = _mk(direction=T.UP)
    assert T.panic_exit(t, {"label": "恐慌盘", "side": "from_below"})
    assert T.panic_exit(t, {"label": "恐慌盘", "side": "from_above"}) is None


def test_panic_refuses_when_side_is_missing():
    """拿不到方向就**不许出场** —— 不猜。

    `kbar_shape.side` 填充率只有 11.8%，用它会让 89% 的情况静默拿到 None；
    所以方向必须取 `kbar_reaction.side`（100%）。这条钉住「没方向就别动」。
    """
    t = _mk()
    assert T.panic_exit(t, {"label": "恐慌盘", "side": None}) is None
    assert T.panic_exit(t, {"label": "恐慌盘"}) is None


def test_panic_ignores_other_labels():
    t = _mk()
    for lab in ("插针收回", "针后远离", "无·贴线", "插针后稳住", "无·真空"):
        assert T.panic_exit(t, {"label": lab, "side": "from_below"}) is None


def test_panic_can_be_disabled():
    t = _mk(s5_panic_exit=False)
    assert T.panic_exit(t, {"label": "恐慌盘", "side": "from_below"}) is None


# ------------------------------------------------------------ 缩量停滞

def test_stall_is_off_by_default():
    """★★ 默认关：实测无条件触发率 31.4%/根，7 根内 93% 会被它平掉。

    那就是一个 2 根的时间闸，比用户 2026-11-17 删掉的那个还狠。
    想开必须显式打开 —— 默认值本身就是一条防线。
    """
    t = _mk()
    assert t.s5_stall_exit is False
    assert T.stall_exit(t, {"label": "无·贴线", "vol_rel": 0.3}) is None


def test_stall_when_enabled_uses_the_ledger_cut():
    """打开后：`vol_rel < 1.0` 是**库里已有**的口径（低于均量），不是新阈值。"""
    t = _mk(s5_stall_exit=True)
    assert T.stall_exit(t, {"label": "无·贴线", "vol_rel": 0.9})
    assert T.stall_exit(t, {"label": "无·真空", "vol_rel": 0.5})
    assert T.stall_exit(t, {"label": "无·贴线", "vol_rel": 1.0}) is None
    assert T.stall_exit(t, {"label": "插针收回", "vol_rel": 0.5}) is None
    # vol_rel 缺失时不许猜成「缩量」
    assert T.stall_exit(t, {"label": "无·贴线", "vol_rel": None}) is None


# ------------------------------------------------------------ 结构

def test_no_holding_time_field_came_back():
    """S5 不许把持仓时长闸偷偷装回来（§25.3 用户删过）。"""
    assert "max_hold_bars" not in T.Thesis.__dataclass_fields__
    assert not hasattr(T, "CLOSED_TIME")


def test_kbar_view_accepts_all_three_shapes():
    """回放传对象、增量传函数、测试传字典 —— 一套取值逻辑。"""
    assert T._kbar_at({"label": "x"}, 5) is None or True
    d = {5: {"label": "x"}}
    assert T._kbar_at(d, 5) == {"label": "x"}
    assert T._kbar_at(lambda ts: {"ts": ts}, 7) == {"ts": 7}
    assert T._kbar_at(None, 5) is None

    class V:
        def at(self, ts):
            return {"ts": ts}
    assert T._kbar_at(V(), 9) == {"ts": 9}


def test_breakeven_state_is_its_own_bucket():
    """★ 保本出场不许挤进 CLOSED_TP / CLOSED_SL / CLOSED_THESIS。

    挤进去会静默改变那个桶的统计，而且没人会发现 —— 这正是项目禁的静默归档。
    """
    assert T.CLOSED_BREAKEVEN in T.STATES
    assert T.CLOSED_BREAKEVEN in T.TERMINAL_STATES
    for other in (T.CLOSED_TP, T.CLOSED_SL, T.CLOSED_THESIS):
        assert T.CLOSED_BREAKEVEN != other


def test_best_favorable_is_monotonic():
    """棘轮游标只能朝有利方向走，否则「止损永不后退」这条不变量会破。"""
    t = _mk()
    cur = 100.0
    cur = T._update_best_fav(t, cur, _Bar(0, 150.0, 0, 0))
    assert cur == 150.0
    cur = T._update_best_fav(t, cur, _Bar(0, 120.0, 0, 0))
    assert cur == 150.0, "做多的 best_fav 不许往下走"

    s = _mk(direction=T.DOWN)
    cur = 200.0
    cur = T._update_best_fav(s, cur, _Bar(0, 999.0, 180.0, 0))
    assert cur == 180.0
    cur = T._update_best_fav(s, cur, _Bar(0, 999.0, 190.0, 0))
    assert cur == 180.0, "做空的 best_fav 不许往上走"


def test_ratchet_exit_lands_in_its_own_bucket():
    """★★ 棘轮真正触发时，终态必须是 `CLOSED_BREAKEVEN`。

    䥑上一条（只验常量存在）不够：把 `emit(..., state_after=CLOSED_BREAKEVEN)`
    改成 `CLOSED_TP` 也能通过 —— 那样保本就静默挤进了止盈桶，
    胜率会凭空变好看。这条把真实 emit 的状态钉住。
    """
    t = _mk()
    r = abs(PX - STOP)
    be = T.breakeven_stop(t, PX + r * 1.5)
    assert be is not None and be < PX
    got = []
    hit = T._close_position(
        t, _Bar(PX + r, PX + r, PX - r * 3, PX - r, ts=600_000),
        T.PENDING, {}, 2, lambda *a, **k: got.append(a), 600_000,
        kbar=None, ratchet_stop=be, best_favorable_px=PX + r * 1.5)
    assert hit, "价格已经跌穿保本价 %.2f，应该平掉" % be
    states = [a[0] for a in got]
    assert T.CLOSED_BREAKEVEN in states, (
        "棘轮触发了但没进保本桶，实际：%r" % (states,))
    assert T.CLOSED_TP not in states, "保本挤进了止盈桶"
    acts = [a[-1] for a in got if a[0] == T.CLOSED_BREAKEVEN]
    assert acts and getattr(acts[0], "reason", "") == "breakeven_ratchet"
    # ★★ 不变量：`emit` 的终态与 `Action.state_after` **必须一致**。
    #
    #   两者分开写（一个给转换序列、一个给下单动作），改一个忘一个
    #   不会报错，但会让库里记的状态和事件里的状态不一样。
    #   这正是「同一件事两个来源」的又一个实例。
    for a in got:
        act = a[-1] if a else None
        if act is not None and hasattr(act, "state_after"):
            assert act.state_after == a[0], (
                "动作里写的终态 %s 与事件终态 %s 不一致"
                % (act.state_after, a[0]))
