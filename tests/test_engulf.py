# -*- coding: utf-8 -*-
"""实体反包三档策略的单元测试。

覆盖点（对应 `ai_trader/trading/engulf.py`）：
  1. 形态判定 —— 尤其是**今开 == 前一根收盘**这种连续市场的常态，
     用严格不等号会漏掉，且不会报错（用户人工核对时发现的真 bug）。
  2. 手续费倍数过滤器 —— 这是让策略从负期望翻正的**决定性**门槛。
  3. 波动率近期性过滤器。
  4. 三档定价的价位关系与目标倍数。
  5. 风险分摊系数 —— 三档各 1/3，否则敞口翻三倍（生产安全红线）。
  6. 接线：`PlanSource.plans()` 里既有反包、又不污染原有三条形态。
"""
from __future__ import annotations

import types

import pytest

from ai_trader.trading import engulf as E
from ai_trader.trading import pending_plan as P
from ai_trader.trading import thesis as T


def _bar(o, h, l, c, ts=0, v=0.0):
    return P._Bar(int(ts), float(o), float(h), float(l), float(c))


# ---------------------------------------------------------------- 1. 形态判定
class TestEngulfing:
    def test_真实反包_今开恰好等于前收(self):
        """11-25 22:45 的真实四价：今开 57357.3 **恰好等于**前一根收盘。

        ★ 这是严格不等号（`b.o < p.c`）会漏掉的标准实体反包。
          连续市场里没有跳空时，今开 == 前收是**常态**而非例外。
        """
        prev = _bar(58156.6, 58265.0, 57320.1, 57357.3)
        cur = _bar(57357.3, 58237.1, 57256.5, 58209.6)
        assert E.is_up_engulfing(prev, cur) is True

    def test_真实反包_09点那根(self):
        """11-26 09:00：今开 60496.0 恰好等于前收 60496.0。"""
        prev = _bar(60623.9, 60631.4, 60437.9, 60496.0)
        cur = _bar(60496.0, 60784.1, 60440.0, 60766.0)
        assert E.is_up_engulfing(prev, cur) is True

    def test_真实反包_有跳空(self):
        """今开严格低于前收（跳空低开）也成立。"""
        prev = _bar(100.0, 101.0, 95.0, 96.0)
        cur = _bar(94.0, 105.0, 93.0, 104.0)
        assert E.is_up_engulfing(prev, cur) is True

    def test_上沿恰好相等也算反包(self):
        """当根收盘**恰好等于**前一根开盘 → 上沿刚好够到，算反包。

        ★ 与「今开 == 前收」是**两个独立的等号**，必须各有一条测试。
          只测下沿那一个，把上沿的 `>=` 写成 `>` 时不会有任何测试报警
          （变异测试实测存活过 —— 这就是补它的原因）。
        """
        prev = _bar(100.0, 101.0, 95.0, 96.0)      # 阴，开 100
        cur = _bar(96.0, 100.0, 95.0, 100.0)       # 阳，收 100 == 前开
        assert E.is_up_engulfing(prev, cur) is True

    def test_下沿恰好相等也算反包(self):
        """当根开盘**恰好等于**前一根收盘（= 22:45 那根的情形）。

        ★ 上沿必须**严格**吃穿（c > 前开），这样这条测试只锁下沿那个等号，
          与 `test_上沿恰好相等也算反包` 互不重叠 —— 否则一条坏了另一条
          仍然绿，掩盖问题。
        """
        prev = _bar(100.0, 101.0, 95.0, 96.0)      # 阴，收 96
        cur = _bar(96.0, 101.5, 95.0, 101.0)       # 阳，开 96 == 前收
        assert E.is_up_engulfing(prev, cur) is True

    def test_前一根不是阴线则不算(self):
        prev = _bar(96.0, 101.0, 95.0, 100.0)      # 阳
        cur = _bar(96.0, 105.0, 93.0, 104.0)
        assert E.is_up_engulfing(prev, cur) is False

    def test_当根不是阳线则不算(self):
        prev = _bar(100.0, 101.0, 95.0, 96.0)
        cur = _bar(96.0, 97.0, 90.0, 92.0)         # 阴
        assert E.is_up_engulfing(prev, cur) is False

    def test_实体没包住前实体则不算(self):
        """当根收没有超过前一根开 → 不是实体反包。"""
        prev = _bar(100.0, 101.0, 95.0, 96.0)
        cur = _bar(96.0, 99.5, 93.0, 99.0)         # 收 99 < 前开 100
        assert E.is_up_engulfing(prev, cur) is False

    def test_下沿没吃穿则不算(self):
        """当根开高于前收 → 下沿没包住。"""
        prev = _bar(100.0, 101.0, 95.0, 96.0)
        cur = _bar(97.0, 105.0, 93.0, 104.0)       # 开 97 > 前收 96
        assert E.is_up_engulfing(prev, cur) is False

    def test_做空镜像(self):
        prev = _bar(96.0, 101.0, 95.0, 100.0)      # 阳
        cur = _bar(100.0, 105.0, 91.0, 92.0)       # 阴，实体吃穿
        assert E.is_down_engulfing(prev, cur) is True
        assert E.is_up_engulfing(prev, cur) is False


# ---------------------------------------------------------------- 2. 手续费门槛
class TestRCostGate:
    def test_刚好六倍手续费通过(self):
        price = 60000.0
        need = 6.0 * price * E.ROUND_TRIP_FEE
        assert E.r_cost_ok(price, need) is True
        assert E.r_cost_ok(price, need * 0.999) is False

    def test_1R太小则拒绝(self):
        """15m 上 1R 中位数约 190 点、手续费约 42 点 —— 费/1R 0.22R，
        形态的毛优势（+0.19R）刚好被吃掉，所以必须拒。"""
        assert E.r_cost_ok(60000.0, 190.0) is False

    def test_边界输入不抛异常(self):
        assert E.r_cost_ok(0.0, 500.0) is False
        assert E.r_cost_ok(60000.0, 0.0) is False
        assert E.r_cost_ok(60000.0, -1.0) is False

    def test_往返费率是maker入加taker出(self):
        """★ 与 `tools/bt_engulf4.py` 的 FEE_RT 必须同值，否则回测与实盘脱节。"""
        assert E.ROUND_TRIP_FEE == pytest.approx(0.0007)


# ---------------------------------------------------------------- 3. 波动率近期性
class TestVolRecency:
    def _flat(self, n=20, rng=10.0, ts0=0):
        return [_bar(100.0, 100.0 + rng, 100.0, 100.0 + rng / 2.0,
                     ts=ts0 + i * 900_000) for i in range(n)]

    def test_波动率扩张在最近十根内则通过(self):
        bars = self._flat(20, rng=10.0)
        bars[-1] = _bar(100.0, 400.0, 100.0, 300.0)   # 最后一根振幅爆掉
        assert E.vol_recency_ok(bars, len(bars) - 1) is True

    def test_波动率扩张在十五根之前则不通过(self):
        """用户口径：「波动率太远了就都不用这个策略」。"""
        bars = self._flat(20, rng=10.0)
        bars[0] = _bar(100.0, 400.0, 100.0, 300.0)    # 爆在最老一根
        assert E.vol_recency_ok(bars, len(bars) - 1) is False

    def test_没有波动率则不通过(self):
        """全部振幅为 0 → 用户口径「没有波动率的时候都不用」。"""
        bars = [_bar(100.0, 100.0, 100.0, 100.0, ts=i * 900_000)
                for i in range(20)]
        assert E.vol_recency_ok(bars, 19) is False

    def test_样本不足则不通过(self):
        bars = self._flat(5)
        assert E.vol_recency_ok(bars, 4) is False


# ---------------------------------------------------------------- 4. 三档定价
class TestTranchePricing:
    BAR = _bar(60000.0, 60800.0, 59700.0, 60600.0)   # 阳，实体 +600，1R = 900

    def test_三档各一腿(self):
        out = E.tranche_prices(self.BAR)
        assert len(out) == 3

    def test_档位与目标一一对应(self):
        """第一份 1R、第二份 2R、第三份 3R —— 用户口径，不许共用目标。"""
        out = E.tranche_prices(self.BAR)
        assert [t["level"] for t in out] == [0.8, 0.5, 0.3]
        assert [t["r_mult"] for t in out] == [1.0, 2.0, 3.0]

    def test_入场价按实体百分比(self):
        out = E.tranche_prices(self.BAR)
        # 实体 = 60600 - 60000 = 600
        assert out[0]["entry"] == pytest.approx(60000.0 + 0.8 * 600.0)
        assert out[1]["entry"] == pytest.approx(60000.0 + 0.5 * 600.0)
        assert out[2]["entry"] == pytest.approx(60000.0 + 0.3 * 600.0)

    def test_止损在最低点下方一个tick且三档共用(self):
        out = E.tranche_prices(self.BAR)
        for t in out:
            assert t["stop"] == pytest.approx(59700.0 - 0.1)

    def test_一R定义是收盘减最低(self):
        out = E.tranche_prices(self.BAR)
        assert out[0]["r1"] == pytest.approx(60600.0 - 59700.0)

    def test_目标等于入场加k倍一R(self):
        out = E.tranche_prices(self.BAR)
        r1 = 60600.0 - 59700.0
        for t in out:
            assert t["target"] == pytest.approx(
                t["entry"] + t["r_mult"] * r1)

    def test_价位关系满足做多约束(self):
        """止损 < 挂单 < 止盈 —— `Plan.validate()` 的硬要求。"""
        for t in E.tranche_prices(self.BAR):
            assert t["stop"] < t["entry"] < t["target"]

    def test_阴线不产生档位(self):
        bar = _bar(60600.0, 60800.0, 59700.0, 60000.0)
        assert E.tranche_prices(bar) == []

    def test_三档挂单价互不相同(self):
        """★ 挂单价相同会被 `plan_key` 去重成一条 = 只剩三分之一仓位。"""
        prices = [t["entry"] for t in E.tranche_prices(self.BAR)]
        assert len(set(round(p, 4) for p in prices)) == 3

    def test_tick_size从参数取(self):
        params = P.PendingParams()
        out = E.tranche_prices(self.BAR, params)
        tick = float(getattr(params, "tick_size", 0.1) or 0.1)
        assert out[0]["stop"] == pytest.approx(59700.0 - tick)


# ---------------------------------------------------------------- 5. 风险分摊
class TestRiskScale:
    def test_反包是三分之一(self):
        """三档同时挂出，各 1/3 → 合计 1R。"""
        assert E.risk_scale_for(E.KIND_ENGULF) == pytest.approx(1.0 / 3.0)

    def test_其它形态是全额(self):
        for k in P.KINDS:
            assert E.risk_scale_for(k) == pytest.approx(1.0)

    def test_未知与None是全额(self):
        """★ 默认必须是 1.0 而不是 0 —— 0 会让仓位闸算出 0 手，
        所有既有形态静默不开仓。"""
        assert E.risk_scale_for(None) == pytest.approx(1.0)
        assert E.risk_scale_for("") == pytest.approx(1.0)
        assert E.risk_scale_for("随便什么") == pytest.approx(1.0)

    def test_三档合计恰好一R(self):
        total = E.RISK_SCALE * len(E.TRANCHE_LEVELS)
        assert total == pytest.approx(1.0)


# ---------------------------------------------------------------- 6. 常量自检
class TestConfigGuards:
    def test_档位与目标等长(self):
        assert len(E.TRANCHE_LEVELS) == len(E.TRANCHE_TARGETS)

    def test_目标随深度单调递增(self):
        """越靠近最低点（档位越小）目标越远 —— 写反了不会报错，
        只会让第三份拿 1R、第一份拿 3R。"""
        pairs = sorted(zip(E.TRANCHE_LEVELS, E.TRANCHE_TARGETS), reverse=True)
        targets = [t for _, t in pairs]
        assert targets == sorted(targets)

    def test_重复档位会被拒(self, monkeypatch):
        monkeypatch.setattr(E, "TRANCHE_LEVELS", (0.5, 0.5, 0.3))
        monkeypatch.setattr(E, "TRANCHE_TARGETS", (1.0, 2.0, 3.0))
        with pytest.raises(ValueError):
            E._check_tranches()

    def test_长度不等会被拒(self, monkeypatch):
        monkeypatch.setattr(E, "TRANCHE_LEVELS", (0.8, 0.5))
        monkeypatch.setattr(E, "TRANCHE_TARGETS", (1.0, 2.0, 3.0))
        with pytest.raises(ValueError):
            E._check_tranches()

    def test_越界档位会被拒(self, monkeypatch):
        monkeypatch.setattr(E, "TRANCHE_LEVELS", (1.2, 0.5, 0.3))
        monkeypatch.setattr(E, "TRANCHE_TARGETS", (1.0, 2.0, 3.0))
        with pytest.raises(ValueError):
            E._check_tranches()


# ---------------------------------------------------------------- 7. 产出计划
class TestDetect:
    def _ctx(self, bars):
        return P.PlanContext(bars15=bars, book=T.LineBook(),
                             bars_1h=[], bars_5m=[], windows=[])

    def _bars_with_signal(self):
        """造一根必过的反包：实体大、1R 远大于 6 倍手续费。"""
        bars = []
        for i in range(19):
            bars.append(_bar(60000.0, 60100.0, 59900.0, 60050.0,
                             ts=i * 900_000))
        # 倒数第二根：阴
        bars.append(_bar(60100.0, 60150.0, 59400.0, 59500.0,
                         ts=19 * 900_000))
        # 最后一根：阳，实体包住前阴，1R = 60700-59400 = 1300 点
        bars.append(_bar(59500.0, 60750.0, 59400.0, 60700.0,
                         ts=20 * 900_000))
        return bars

    def test_一条信号产出三条腿(self):
        bars = self._bars_with_signal()
        plans, why = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        assert why == ""
        assert len(plans) == 3

    def test_三条腿的plan_key互不相同(self):
        from ai_trader.trading.plan_source import PlanSource
        bars = self._bars_with_signal()
        plans, _ = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        keys = {PlanSource.plan_key(p) for p in plans}
        assert len(keys) == 3

    def test_每条腿都带risk_scale(self):
        """★ 丢了它 → 三档各吃满额风险 → 敞口 3 倍。"""
        bars = self._bars_with_signal()
        plans, _ = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        for p in plans:
            assert p.evidence["risk_scale"] == pytest.approx(1.0 / 3.0)

    def test_每条腿都带stop_distance(self):
        """★ 丢了它 → `to_thesis()` 算出的 `stale_distance` = 0
        → 「挂单期跑偏永不撤销」。这是 2026-11-17 修过的洞，不能再漏。"""
        bars = self._bars_with_signal()
        plans, _ = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        for p in plans:
            sd = p.evidence.get("stop_distance")
            assert sd is not None and float(sd) > 0
            # 口径是价格点数，不是百分比
            assert float(sd) == pytest.approx(
                abs(p.order_price - p.stop_price))

    def test_跑偏撤销距离不为零(self):
        """端到端：`to_thesis()` 拿到的 `stale_distance` 必须 > 0，
        否则挂单期跑偏永不撤销 —— 价格跑了还照价成交。"""
        bars = self._bars_with_signal()
        plans, _ = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        for p in plans:
            th = P.to_thesis(p, params=P.PendingParams())
            assert th.stale_distance > 0

    def test_三档的thesis_id互不相同(self):
        """★ 默认 id 只拼 direction/kind/bar_ts 时，三档会撞成一个 id，
        落库后互相覆盖（表现为「下了三档只剩一条」）。"""
        bars = self._bars_with_signal()
        plans, _ = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        ids = {P.to_thesis(p, params=P.PendingParams()).thesis_id
               for p in plans}
        assert len(ids) == 3

    def test_腿全部通过Plan自校验(self):
        bars = self._bars_with_signal()
        plans, _ = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        for p in plans:
            p.validate(ref_price=60700.0)

    def test_是反转家族(self):
        """反包是「大跌抄底做多」→ 失效判据只看止损价。"""
        bars = self._bars_with_signal()
        plans, _ = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        assert all(p.family == T.FAMILY_REVERSAL for p in plans)

    def test_是15m序列(self):
        bars = self._bars_with_signal()
        plans, _ = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        assert all(p.bar_space == "15m" for p in plans)

    def test_一R太小则不出计划(self):
        """1R 太小（远不到 6 倍手续费）→ 直接不出计划。

        ★ 构造要点：反包必须**成立**（否会先被 `no_engulf` 拦掉，
          测不到这道闸）。所以前一根阴必须很窄且贴低点：
          前阴开 59600 收 59550 → 当根要 c>=59600 且 o<=59550，
          于是 1R = 59650-59550 = 100 点 << 6 x 59650 x 0.0007 ≈ 250.5。
        """
        bars = [_bar(60000.0, 60100.0, 59900.0, 60050.0, ts=i * 900_000)
                for i in range(19)]
        bars.append(_bar(59600.0, 59700.0, 59500.0, 59550.0,
                         ts=19 * 900_000))          # 窄阴，贴低点
        bars.append(_bar(59550.0, 59660.0, 59550.0, 59650.0,
                         ts=20 * 900_000))          # 阳，实体包住，1R=100
        plans, why = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        assert plans == []
        assert why == "r_too_small_vs_fee"

    def test_没有反包就不出计划(self):
        bars = self._bars_with_signal()
        bars[-2] = _bar(59500.0, 59600.0, 59400.0, 59550.0)   # 改成阳
        plans, why = E.detect_engulf(20, self._ctx(bars), P.PendingParams())
        assert plans == []
        assert why == "no_engulf"

    def test_做空方向未启用(self):
        bars = self._bars_with_signal()
        plans, why = E.detect_engulf(20, self._ctx(bars), P.PendingParams(),
                                     direction=T.DOWN)
        assert plans == []
        assert why == "direction_not_supported"

    def test_detect给出去重前的原因计数(self):
        bars = self._bars_with_signal()
        plans, stats = E.detect(self._ctx(bars), P.PendingParams())
        assert len(plans) == 3
        assert any(k.endswith(":ok") for k in stats)
        assert any(k.endswith(":no_engulf") for k in stats)


# ---------------------------------------------------------------- 8. 接线
class TestWiring:
    def test_计划源里同时有反包与原有形态(self, monkeypatch):
        """★ 反包必须是**追加**，不能挤掉原有三条形态。"""
        from ai_trader.trading import plan_source as PS
        calls = {}

        def fake_detect_all(ctx, params=None, **kw):
            calls["kw"] = kw
            return [], {"orig:ok": 1}

        monkeypatch.setattr(P, "detect_all", fake_detect_all)
        monkeypatch.setattr(PS.PlanSource, "context",
                            lambda self: P.PlanContext(
                                bars15=[], book=T.LineBook(), bars_1h=[],
                                bars_5m=[], windows=[]))
        monkeypatch.setattr(E, "detect",
                            lambda ctx, params=None, **kw: (
                                ["ENGULF_PLAN"], {"engulf:ok": 1}))
        plans, stats = PS.PlanSource(symbol="BTCUSDT").plans()
        assert "ENGULF_PLAN" in plans
        assert stats["orig:ok"] == 1
        assert stats["engulf:ok"] == 1
        # 原有调用参数一个都不能变
        assert calls["kw"]["kinds"] == P.ENABLED_KINDS
        assert calls["kw"]["require_window"] is True
        assert calls["kw"]["include_mirror"] is True

    def test_反包不在ENABLED_KINDS里(self):
        """★ 反包走独立通道；若混进 ENABLED_KINDS，
        `detect_all` 会去 `DETECTORS` 找它 → KeyError。"""
        assert E.KIND_ENGULF not in P.ENABLED_KINDS
        assert E.KIND_ENGULF not in P.KINDS
        assert E.KIND_ENGULF not in P.DETECTORS


# ---------------------------------------------------------------- 9. 引擎接线
class TestEngineWiring:
    def test_仓位闸把risk_scale透传给_pending_size(self):
        """`_pending_size` 必须收到 1/3，否则三档各吃满额风险。"""
        import asyncio
        from ai_trader.trading import engine as ENG

        seen = {}

        def fake_size(entry, stop, order_type="", risk_scale=1.0):
            seen["risk_scale"] = risk_scale
            return 0.01, ""

        st = types.SimpleNamespace(_pending_size=fake_size)
        intent = types.SimpleNamespace(
            entry=60000.0, stop=59700.0, order_type="LIMIT",
            meta={"risk_scale": 1.0 / 3.0})
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            ENG.DecisionEngine._gate_pending_size(st, intent, {}))
        assert seen["risk_scale"] == pytest.approx(1.0 / 3.0)

    def test_没有meta时缺省全额(self):
        import asyncio
        from ai_trader.trading import engine as ENG

        seen = {}

        def fake_size(entry, stop, order_type="", risk_scale=1.0):
            seen["risk_scale"] = risk_scale
            return 0.01, ""

        st = types.SimpleNamespace(_pending_size=fake_size)
        intent = types.SimpleNamespace(
            entry=60000.0, stop=59700.0, order_type="LIMIT", meta={})
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            ENG.DecisionEngine._gate_pending_size(st, intent, {}))
        assert seen["risk_scale"] == pytest.approx(1.0)

    def test_风险系数非法时拒绝开仓(self):
        """0 或负数会让手数算成 inf/负数，必须当场拒绝。"""
        from ai_trader.trading import engine as ENG
        st = types.SimpleNamespace()
        qty, why = ENG.DecisionEngine._pending_size(
            st, 60000.0, 59700.0, "LIMIT", risk_scale=0.0)
        assert qty == 0.0
        assert "风险系数非法" in why

    def test_风险系数真的作用在仓位上(self):
        """★ 端到端：系数必须真的把**手数**压下去。

        只测「参数透传进了 `_pending_size`」是不够的 —— 传进来但不参与
        计算时，三档照样各吃满额风险。变异测试实测存活过这一处，
        所以这里用真实 `plan_position` 比一比 1.0 与 1/3 的手数。
        """
        from ai_trader.trading import engine as ENG

        def _stub():
            return types.SimpleNamespace(
                _sizing_params=lambda: {
                    "sizing.capital": 1000.0, "sizing.risk_pct": 10.0,
                    "sizing.leverage": 20.0,
                    "sizing.margin_cap_pct": 100.0},
                _qty_step=lambda: 0.001,
                _min_notional=lambda: 5.0)

        full, why = ENG.DecisionEngine._pending_size(
            _stub(), 60000.0, 59400.0, "LIMIT", risk_scale=1.0)
        assert full > 0, why
        third, why = ENG.DecisionEngine._pending_size(
            _stub(), 60000.0, 59400.0, "LIMIT", risk_scale=1.0 / 3.0)
        assert third > 0, why
        assert third < full
        # 步长取整会带来微小偏差，1/3 落在 0.3~0.36 之间即算正确
        assert 0.30 <= third / full <= 0.36
