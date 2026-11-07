# -*- coding: utf-8 -*-
"""滞回带：让进场门槛长得像成本。

一次往返手续费 0.2%（现货 taker 实测 0.100%/边）。
收盘只比均线高 0.03% 的那种交叉，赚的钱大概率不够付手续费，
却照样触发一次完整往返。滞回带把进场上移到 SMA*(1+h)、
出场下移到 SMA*(1-h)，于是带子里的来回不算信号。

实测（服务器 6.84 年，h=0.2%）：笔数 213->147，净盈亏 945.05U->1131.84U，
手续费 233.65U->182.86U，三个时代全部"收益与回撤同时改善"。

这组用例要钉住四件事：
  1. h=0 时与旧行为【逐字等价】——不能因为加参数改了默认逻辑
  2. h>0 时擦边交叉不再进场
  3. h>0 时均线附近的回踩不再出场（这才是省手续费的来源）
  4. 看板/滚动推算报的是【真触发线】，不是裸均线
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import AppCfg
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000


def _mk(prices, lookback=10, hyst=0.0):
    cfg = AppCfg()
    cfg.trading.sma_lookback = lookback
    cfg.trading.trend_hysteresis_pct = hyst
    st = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    st.bars = [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
               for i, p in enumerate(prices)]
    return st


# 均线口径：sma = mean(cl[-n-1:-1])，不含最后一根。
# 10 个 100 打底 => 判定时窗口里是 9 个 100 + 倒数第二根。
BASE = [100.0] * 11


def test_hyst_zero_is_identical_to_old_rule():
    """h=0 必须与旧行为逐字等价：三个谓词完全相同。"""
    for tail in ([100.0], [101.0], [99.0], [100.5]):
        st = _mk(BASE + tail, hyst=0.0)
        s = st.state(0.0)
        assert s["enter_above"] == s["above"], "h=0 时进场的'在上方'必须等于裸判定"
        assert s["exit_above"] == s["above"], "h=0 时出场的'在上方'必须等于裸判定"
        assert s["enter_prev_above"] == s["prev_above"]
        assert s["entry_line"] == pytest.approx(s["sma"])
        assert s["exit_line"] == pytest.approx(s["sma"])


def test_bare_cross_enters_without_hysteresis():
    """反证：没有滞回时，刚擦过均线就算上穿，会进场。"""
    # 窗口均线 = (9*100 + 100)/10 = 100；最后一根 100.1 严格高于 -> 上穿
    st = _mk(BASE + [100.1], hyst=0.0)
    sig = st.want_entry(0.0)
    assert sig is not None, "裸规则下 100.1 > 100 应当触发进场"


def test_bare_cross_blocked_by_hysteresis():
    """有滞回时，同样的擦边交叉不得进场——这正是省下手续费的地方。"""
    st = _mk(BASE + [100.1], hyst=0.2)     # 门槛 = 100 * 1.002 = 100.2
    assert st.want_entry(0.0) is None, (
        "100.1 只比均线高 0.1%，低于 0.2% 的滞回带，不该触发往返")
    s = st.state(0.0)
    assert s["above"] is True, "真实关系仍是'在上方'，不能被滞回改写"
    assert s["enter_above"] is False, "进场谓词在带子里应为 False"


def test_clear_cross_still_enters_with_hysteresis():
    """反证：明确突破（超过带宽）必须照常进场，不能把真信号一起挡掉。"""
    st = _mk(BASE + [100.5], hyst=0.2)     # 100.5 > 100.2
    sig = st.want_entry(0.0)
    assert sig is not None, "100.5 已越过 0.2% 的带子，应当进场"


def test_small_dip_does_not_exit_with_hysteresis():
    """有滞回时，跌破均线但没跌穿下沿，不该平仓。"""
    # 均线 = (9*100 + 100)/10 = 100；最后 99.9 低于均线但在带内
    st = _mk(BASE + [99.9], hyst=0.2)      # 下沿 = 100 * 0.998 = 99.8
    st.pos = {"side": "long", "qty": 1.0}
    assert st.want_exit(0.0) is None, (
        "99.9 只比均线低 0.1%，没到 -0.2% 的下沿，不该平仓")


def test_deep_break_still_exits_with_hysteresis():
    """反证：真正跌穿下沿必须平仓，滞回不能变成'不止损'。"""
    st = _mk(BASE + [99.0], hyst=0.2)      # 99.0 < 99.8
    st.pos = {"side": "long", "qty": 1.0}
    sig = st.want_exit(0.0)
    assert sig is not None, "99.0 已跌穿下沿，必须出场"


def test_startup_reconcile_ignores_position_inside_band():
    """启动对账不能用裸 above，否则会把'本来就不该持有'补成持仓。

    场景：进程重启时现价 100.1，均线 100，滞回带要求 100.2。
    按规则此刻不该有仓，所以对账应当认为'不欠仓'并清掉标志，
    而不是拿裸 above 认为'在均线上方、欠一笔'去补开。
    """
    st = _mk(BASE + [100.1], hyst=0.2)
    st.need_reconcile = True
    sig = st.startup_entry(0.0)
    assert sig is None, "带子里不该补仓"
    assert st.need_reconcile is False, "对账标志应当清掉，否则它会一直挂着"


def test_startup_reconcile_still_adopts_clear_uptrend():
    """反证：明确在带子上方时，对账仍必须补进（否则重启会错过整段趋势）。"""
    st = _mk(BASE + [101.0], hyst=0.2)
    st.need_reconcile = True
    sig = st.startup_entry(0.0)
    assert sig is not None, "明确在上方时必须补仓"
    assert sig["name"] == "trend_reconcile"


def test_roll_forward_uses_hysteresis_threshold():
    """滚动推算必须按【真触发线】算，否则报的天数对应一个不发生的进场。

    ★ 第一版这条用例写错了：用的序列是 200 和 50 交替，均线每滚一根跳 5 个点，
      而 0.2% 的带宽在 100 的价位上只有 0.2。步长远大于带宽，
      两者必然在同一步跨过，测出来都是 5 根——这条用例于是永远绿，什么都没钉住。

    要真正分开两种口径，必须让均线落在【带宽之内】：
      窗口 = 9 个 100 + 1 个 99  -> SMA = 99.9
      现价冻结在 100 -> 差 0.1，相当于 0.1%，正好小于 0.2% 的带子。
      裸口径 100 > 99.9 成立（滚 1 根就上穿）；
      滞回口径要 100 > 99.9*1.002 = 100.0998，不成立。
      而 99 一旦被挤出去，窗口全变 100，永远不可能满足
      （100 > 100*1.002 恒假），所以滞回口径应当报"等不到"。
    """
    prices = [100.0] * 9 + [99.0] + [50.0]     # 最后一根 50：当前在均线下方
    px = 100.0
    st0 = _mk(prices, lookback=10, hyst=0.0)
    st2 = _mk(prices, lookback=10, hyst=0.2)
    assert st0.state(px)["sma"] == pytest.approx(99.9), "构造前提：SMA 必须是 99.9"

    r0 = st0.roll_forward_to_cross(px)
    r2 = st2.roll_forward_to_cross(px)
    assert r0.get("roll_bars") == 1, (
        "裸口径下现价 100 已高于均线 99.9，滚 1 根就该上穿；实际 %s" % (r0,))
    assert r2.get("roll_bars") is None, (
        "滞回口径下 100 够不到 100.0998，且窗口最终全为 100 永远够不到，"
        "应当报'等不到'；实际 %s" % (r2,))


def test_roll_forward_never_earlier_with_hysteresis():
    """兜底性质：加了带宽，到达时间只能更晚，不可能更早。

    粗步长形态只能做弱断言，精确分离由上面那条负责。
    """
    prices = [150.0] * 3 + [200.0] * 8 + [50.0] * 5
    px = 100.0
    r0 = _mk(prices, lookback=10, hyst=0.0).roll_forward_to_cross(px)
    r2 = _mk(prices, lookback=10, hyst=0.2).roll_forward_to_cross(px)
    a, b = r0.get("roll_bars"), r2.get("roll_bars")
    assert b is None or (a is not None and b >= a), (
        "滞回口径不得早于裸口径：%s vs %s" % (b, a))


def test_state_reports_entry_line_for_display():
    """看板要能拿到真触发线，而不是自己拿 sma 去乘。"""
    st = _mk(BASE + [99.0], hyst=0.2)
    s = st.state(0.0)
    assert s["entry_line"] == pytest.approx(s["sma"] * 1.002, rel=1e-12)
    assert s["exit_line"] == pytest.approx(s["sma"] * 0.998, rel=1e-12)
    assert s["hyst_pct"] == pytest.approx(0.2, rel=1e-12)
