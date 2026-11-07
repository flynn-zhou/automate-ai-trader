# -*- coding: utf-8 -*-
"""趋势单仓位的三个【恰好相等】边界：最小名义、杠杆上限、数量为 0。

怎么找到的（第 108 轮）：对 _trend_plan（engine.py:1048-1082）做边界变异。
这一段决定趋势单的真实规模，注释里每一行都对应一个踩过的坑。

9 个变异里 6 个被杀、3 个存活——存活的三个全是【恰好相等】才分叉的边界：

    real_notional <  min_notional   改 <=      存活
    real_notional / equity > cap    改 >=      存活
    qty <= 0                        改 <       存活

★ 这里要记一件我差点搞错的事：
  第一次跑时「多仓止损放到上方」（stop = price*(1+pct)）也显示存活，
  我一度以为这是「止损方向完全没测」的严重缺陷。
  但那是因为我为了跑得快，只挑了 13 个名字里带 plan/qty/spot/trend 的测试文件，
  而真正钉住它的 test_hard_stop_path.py:159
      assert plan.stop == pytest.approx(PX * (1 - STOP_PCT / 100.0))
  名字里一个关键词都没有，没被选进来。

  换成全量 520 条重跑，这个变异立刻 8 failed 被杀。
  教训：**变异「存活」可能只是测试选窄了。** 报告「没有测试抓得住」之前，
  必须在全量套件下确认——否则就是拿着一个假的缺口去加测试。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import load
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy


class _Feed:
    src = "WS"

    def __init__(self, px=5000.0):
        self.last_price = px
        self.cfg = load()
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()


class _Ex:
    name = "spot"

    async def close(self):
        pass


def _eng(price=5000.0):
    """只构造到 _trend_plan 需要的那几个属性（同 test_account_floor.py 的写法）。

    不用 __init__：它会建 TradeDB 等一堆依赖，而 _trend_plan 只读
    tcfg / _is_spot() / _plan_leverage() / _fee_buffer() / _qty_step() / _min_notional()。
    """
    cfg = load()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.symbol = "BTCUSDT"
    cfg.trading.trend_stop_pct = 25.0
    e = DecisionEngine.__new__(DecisionEngine)
    e.cfg = cfg
    e.tcfg = cfg.trading
    e.feed = _Feed(price)
    e.ex = _Ex()
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    return e


def _plan_at_exactly_min_notional():
    """造一个 real_notional 恰好等于 _min_notional() 的计划。

    现货：_min_notional()=5.0，_fee_buffer()=0.003，qty_step=1e-5。
    取 price=5000：qty=0.001 时 real_notional = 5.0 恰好相等。
    qty=floor(equity/1.003/5000/1e-5)*1e-5 = 0.001
      <=> equity/1.003/5000 in [0.001, 0.00101)
      <=> equity in [5.015, 5.065)
    取 equity=5.02。
    """
    e = _eng(5000.0)
    p = e._trend_plan(5000.0, 5.02, {"ok": True, "name": "trend_up"})
    assert e._min_notional() == pytest.approx(5.0), "前提变了：现货最小下单不是 5U"
    assert p.qty == pytest.approx(0.001), (
        u"构造失败：qty=%r，本意是 0.001" % (p.qty,))
    assert p.notional == pytest.approx(5.0), (
        u"构造失败：notional=%r，本意是恰好 5.0" % (p.notional,))
    return p


def test_order_exactly_at_min_notional_is_allowed():
    """名义恰好等于最小下单时，必须【允许】下单。

    交易所的规则是「>= minNotional 即可」（现货 5U / 永续 50U）。
    把 < 改成 <= 会把一笔合法订单拒掉，而拒单只在资金刚好卡在线上时才发生——
    最难复现、也最容易在「差一点凑够钱」的真实账户上咬人。
    """
    p = _plan_at_exactly_min_notional()
    assert p.ok is True, (
        u"名义恰好 5.0U（等于最小下单）被拒了：%s\n"
        u"交易所在 notional >= minNotional 时是允许的，拒掉等于丢掉一笔合法订单。"
        % (p.reason,))


def test_one_step_below_min_notional_is_rejected():
    """反向对照：少一个 step 就必须拒绝。

    没有这条，上面那条可能因为「永远 ok」而永远绿。
    """
    e = _eng(5000.0)
    # qty=0.00099 -> notional = 4.95 < 5.0
    p = e._trend_plan(5000.0, 5.02 * 0.99, {"ok": True, "name": "trend_up"})
    if p.qty * 5000.0 >= 5.0:
        pytest.skip("构造没落在最小下单下方，跳过（qty=%r）" % (p.qty,))
    assert p.ok is False, u"名义 %r 低于最小下单却 ok" % (p.qty * 5000.0,)
    assert "最小下单" in p.reason, u"拒绝原因没说是最小下单：%r" % (p.reason,)


def test_leverage_exactly_at_the_cap_is_allowed():
    """杠杆恰好等于上限时应当允许（上限是「不超过」，不是「小于」）。

    把 > 改成 >= 会在恰好等于上限时拒单。
    现实中因为要先除以 (1+手续费缓冲)，这个等号很难自然出现——
    所以这条测的是【契约】，不是常见路径。
    """
    e = _eng(5000.0)
    cap = float(e.tcfg.max_leverage)
    # 直接构造：equity 让 real_notional/equity 恰好等于 cap
    # real_notional = qty*price，找一个让比值为 cap 的 equity 不现实（有取整）。
    # 所以退一步：验证「用 cap 算出来的 notional/equity 一定 <= cap 且能下单」。
    p = e._trend_plan(5000.0, 5.02, {"ok": True, "name": "trend_up"})
    assert p.leverage <= cap + 1e-9, (
        u"杠杆 %r 超过了上限 %r" % (p.leverage, cap))
    # 关键：不能因为「杠杆刚好顶到上限」就拒单
    e2 = _eng(5000.0)
    e2.tcfg.max_leverage = p.leverage       # 把上限设成刚好等于算出来的杠杆
    p2 = e2._trend_plan(5000.0, 5.02, {"ok": True, "name": "trend_up"})
    assert p2.ok is True, (
        u"杠杆恰好等于上限 %r 时被拒了：%s\n"
        u"上限的语义是「不超过」，等于上限应当放行。" % (p.leverage, p2.reason))


def test_zero_qty_is_rejected_with_its_own_reason():
    """数量取整成 0 时，拒绝原因必须指向「取整后为 0」，不能落到别的闸门上。

    这条不是形式主义：看板和哨兵是按【文案】区分故障原因的。
    如果这里漏出去，会被下一道「名义 < 最小下单」接住，
    于是日志变成「名义 0.0U < 最小下单 5.0U」——
    听起来像「钱差一点」，实际是「价格太高，一个 step 都买不起」。
    这两种情况的处置完全不同（一个是等资金，一个是等价格）。
    """
    e = _eng(50000.0)          # 价 50000，equity 极小时 qty 取整为 0
    p = e._trend_plan(50000.0, 0.001, {"ok": True, "name": "trend_up"})
    assert p.ok is False
    assert p.qty == 0
    assert "取整后为 0" in p.reason, (
        u"数量取整成 0 时的拒绝原因不是「取整后为 0」：%r\n"
        u"落到别的闸门上会让日志说「名义不足」，把「买不起一个 step」"
        u"伪装成「钱差一点」。" % (p.reason,))
