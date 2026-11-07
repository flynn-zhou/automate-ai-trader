# -*- coding: utf-8 -*-
"""持仓缺止损价时，重启必须重算一个保护位，绝不能留 0。

为什么单独写（第 103 轮）：
  第 100 轮覆盖率审计发现 engine.py:60-76（_fallback_stop）从未被执行，
  而它在 handle 的是一条【真实发生过的事故】。engine.py:87-92 的注释写着：

      "止损价绝不能是 0。0 的含义是\"价格跌到 0 才止损\"，
       等于这道保护静默消失，仓位会一直挂到跌破 SMA 才出场。
       脏数据（早期 bug 遗留、人工插入）真的会出现这一列是 NULL 的情况，
       事故 2 只修了 reconcile_live 的接管路径，attach() 这条被漏掉了。"

  也就是说：这条代码存在，正是因为「修了一处、漏了一处」被踩过。
  而修完之后它自己没有任何测试。

  现有几个测试会调 attach()（test_restart_entry_fee / test_reconcile_paper /
  test_feed_hook_wiring），但它们造的开仓记录都【带 stop_price】，
  所以那条兜底分支一次都没走过。

留意一个容易搞反的地方：
  多仓 stop = entry*(1-pct)，空仓 stop = entry*(1+pct)。
  方向写反的话，多仓的止损会落在【上方】——开仓瞬间就满足 price <= stop，
  或者反过来永远不触发。两种都很糟，所以方向单独测。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

ENTRY_PX = 78000.0
STOP_PCT = 25.0
QTY = 0.001


class _Feed:
    def __init__(self):
        self.last_price = ENTRY_PX
        self.bar_hooks = []
        self.src = "ws"

    class _Hub:
        def publish(self, m):
            pass

    hub = _Hub()


class _Ex:
    name = "spot"


class _DB:
    """只实现 attach() 用到的部分：open_trade + log_risk。"""

    def __init__(self, open_trade):
        self._ot = open_trade
        self.risks = []

    def open_trade(self):
        return self._ot

    def log_risk(self, k, m):
        self.risks.append((k, m))

    def close_trade(self, d):
        pass

    def log_trade(self, d):
        pass

    def log_decision(self, d):
        pass

    def log_equity(self, d):
        pass


def _row(side="long", stop=None, entry=ENTRY_PX):
    return {"id": 7, "symbol": "BTCUSDT", "side": side, "qty": QTY,
            "entry_ts": 1000, "entry_price": entry, "stop_price": stop,
            "target_price": 0.0, "open_reason": "test", "paper": 1,
            "max_hold_bars": 10 ** 9, "fee": 0.5}


def _engine(open_trade):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = STOP_PCT
    db = _DB(open_trade)
    e = DecisionEngine(cfg, _Feed(), _Ex(), db, guard=None)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    e.attach()
    return e, db


def test_null_stop_is_recomputed_not_left_at_zero():
    """stop_price 为 NULL 时，恢复的持仓必须有一个非 0 的保护位。"""
    e, db = _engine(_row(stop=None))
    assert e.pos is not None, "attach 应恢复出这笔持仓"
    got = e.pos["stop"]
    assert got > 0, (
        u"恢复的持仓止损价是 %r。0 的含义是「跌到 0 才止损」，"
        u"等于这道保护静默消失。" % (got,))
    assert got == pytest.approx(ENTRY_PX * (1 - STOP_PCT / 100.0)), (
        u"重算的止损位不对：%r，期望 %r"
        % (got, ENTRY_PX * (1 - STOP_PCT / 100.0)))
    assert any(k == "stop_missing" for k, _ in db.risks), (
        u"重算止损是一次异常修复，必须留下 stop_missing 风险事件，"
        u"否则没人知道脏数据出现过。实际记录：%r" % (db.risks,))


def test_zero_stop_is_also_treated_as_missing():
    """stop_price 写成 0（而不是 NULL）也要走兜底 —— 它同样等于没有保护。"""
    e, _ = _engine(_row(stop=0.0))
    assert e.pos["stop"] > 0, "stop_price=0 被当成有效止损，保护静默消失"


def test_short_side_fallback_stop_is_above_entry():
    """空仓的兜底止损必须落在开仓价【上方】。

    方向写反的话，空仓止损会落在下方，等于一开仓就已经「跌破止损」，
    或者反过来永远不触发。两种都很糟。
    """
    e, _ = _engine(_row(side="short", stop=None))
    got = e.pos["stop"]
    assert got == pytest.approx(ENTRY_PX * (1 + STOP_PCT / 100.0)), (
        u"空仓的兜底止损应在开仓价上方：%r，期望 %r"
        % (got, ENTRY_PX * (1 + STOP_PCT / 100.0)))
    assert got > ENTRY_PX, u"空仓止损 %r 落在了开仓价下方，方向反了" % (got,)


def test_existing_stop_is_kept_and_not_overwritten():
    """反向对照：本来就有止损价时必须【原样保留】，不能被兜底覆盖。

    没有这条，上面几条可能因为「无论如何都重算」而永远绿——
    那会把一个本来正确的止损位改掉，属于更严重的错误。
    """
    e, db = _engine(_row(stop=70000.0))
    assert e.pos["stop"] == pytest.approx(70000.0), (
        u"已有的止损价被覆盖成 %r，原本是 70000.0" % (e.pos["stop"],))
    assert not any(k == "stop_missing" for k, _ in db.risks), (
        u"止损价本来就有，不该记 stop_missing。实际：%r" % (db.risks,))


def test_no_stop_and_no_entry_gives_zero_and_shouts():
    """连开仓价都没有时：返回 0，不能凭空编一个价位。

    ★ 这条最初只用了 entry=0.0，结果一个变异（把 return 0.0 改成 return entry*0.75）
      没被抓住 —— 因为 0*0.75 还是 0，测试根本分不出来。
      改用【负数】开仓价才有区分力：原代码返回 0.0，那个变异会返回 -750.0。
    """
    for bad_entry in (0.0, -1000.0):
        e, db = _engine(_row(stop=None, entry=bad_entry))
        assert e.pos["stop"] == 0.0, (
            u"开仓价 %r 时不该凭空编一个止损位，实际 %r"
            % (bad_entry, e.pos["stop"]))
