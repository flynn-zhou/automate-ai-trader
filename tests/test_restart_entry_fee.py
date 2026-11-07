# -*- coding: utf-8 -*-
"""重启后恢复的持仓，平仓时必须把【开仓手续费】一起算进盈亏。

2026-11-02 实测发现的真缺陷：
  线上纸面账对不上——4 笔已平仓 pnl_net 合计 -20.4628 U，
  而余额实际从 100 变成 71.77029（-28.2297），缺口 7.7669 U。

  逐笔核对余额真实变化 vs 记录值，缺口正好等于【开仓手续费】：
      第 1 笔 实际 -12.0175，记录 -9.0330，差 2.9845（开仓费）
      第 2 笔 实际  -2.3592，记录 +0.3585，差 2.7177（开仓费）
      第 3 笔 实际  -7.1147，记录 -4.4737，差 2.6410（开仓费）

  根因：_close 里写 fee_total = p.get("fee", 0.0) + fill.fee，
  它期望持仓字典里有 "fee"（开仓时 _open 确实写了）。
  但 attach() 从 trades 表恢复持仓时【没有带 fee】，
  reconcile_live() 接管时也没有——于是 p.get("fee", 0.0) 取到 0，
  记录的盈亏只减了平仓费，少算一笔开仓费。

  为什么这条特别危险：漏记成本的方向永远是【让亏损看起来更小】，
  也就是让策略看起来更赚钱。而这正是目标里「用纸面样本找到真实边际」
  最怕的偏差——它会把亏损策略伪装成盈利策略。
"""
import pytest

from ai_trader.config import AppCfg
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

ENTRY_FEE = 2.5           # 开仓时真实扣掉的手续费
QTY = 0.001
ENTRY_PX = 78000.0
EXIT_PX = 80000.0


class _Hub:
    def publish(self, msg):
        pass


class _Feed:
    src = "ws"

    def __init__(self, px=ENTRY_PX):
        self.px = px
        self.last_price = px
        self.agg = self
        self.bar_hooks = []
        self.hub = _Hub()

    def current(self, iv):
        px = self.px
        return type("B", (), {"high": px, "low": px, "close": px,
                              "open": px, "closed": True})()


class _Ex:
    name = "paper"

    def __init__(self, px=ENTRY_PX):
        self.px = px

    async def position(self):
        return {"qty": QTY, "entry": ENTRY_PX}

    async def equity(self):
        return {"equity": 100.0}

    async def market_order(self, side, qty, reason="", reduce_only=False):
        filled = qty           # 类体里不能写 qty = qty，先落到局部变量

        class R:
            ok = True
            price = EXIT_PX
            qty = filled
            fee = 4.0          # 平仓费，故意和开仓费不同，便于区分
            order_id = "T1"
            status = "filled"
            reason = ""
        return R()


class _DB:
    def __init__(self, open_trade):
        self.closed = []
        self._open = open_trade

    def open_trade(self):
        return self._open

    def close_trade(self, d):
        self.closed.append(d)

    def log_order(self, d): pass
    def log_fill(self, d): pass
    def log_risk(self, k, m): pass
    def log_decision(self, d): pass
    def log_trade(self, d): pass


def _open_trade_row(with_fee=True):
    """trades 表里那笔未平仓记录。开仓时 fee 列就写入了开仓手续费。"""
    ot = {"id": 9, "symbol": "BTCUSDT", "side": "long", "qty": QTY,
          "entry_ts": 1000, "entry_price": ENTRY_PX, "stop_price": 70000.0,
          "target_price": 0.0, "open_reason": "test", "paper": 1,
          "max_hold_bars": 10 ** 9}
    if with_fee:
        ot["fee"] = ENTRY_FEE
    return ot


def _engine(open_trade):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    db = _DB(open_trade)
    ex = _Ex()
    e = DecisionEngine(cfg, _Feed(), ex, db, guard=None)
    e.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    e.trend.seeded = True
    e.trend.need_reconcile = False
    e.attach()                 # <- 重启恢复持仓的路径
    return e, db


def test_attach_restores_entry_fee():
    """attach() 恢复持仓时必须把开仓手续费带回来。"""
    e, _ = _engine(_open_trade_row())
    assert e.pos is not None, "attach 应恢复出这笔持仓"
    assert e.pos.get("fee") == pytest.approx(ENTRY_FEE), (
        u"恢复的持仓没有带上开仓手续费（fee=%r）。"
        u"平仓时 fee_total = p.get('fee', 0.0) + fill.fee 会把开仓费算成 0，"
        u"记录的盈亏少减一笔手续费——方向是让亏损看起来更小。"
        % (e.pos.get("fee"),))


@pytest.mark.asyncio
async def test_close_after_restart_counts_both_fees():
    """重启后平仓：记录的 pnl_net 必须同时减掉开仓费和平仓费。"""
    e, db = _engine(_open_trade_row())
    ok = await e._close("测试平仓", EXIT_PX, EXIT_PX, {})
    assert ok, "平仓应成功"
    assert len(db.closed) == 1
    rec = db.closed[0]
    gross = (EXIT_PX - ENTRY_PX) * QTY
    exit_fee = 4.0
    expect_net = gross - (ENTRY_FEE + exit_fee)
    assert rec["pnl_net"] == pytest.approx(expect_net, abs=1e-9), (
        u"记录净盈亏 %.9f，应为 毛利 %.4f - 开仓费 %.4f - 平仓费 %.4f = %.9f。"
        u"少算的那部分就是被吞掉的开仓手续费。"
        % (rec["pnl_net"], gross, ENTRY_FEE, exit_fee, expect_net))


def test_open_trade_returns_stored_entry_fee(tmp_path):
    """open_trade() 必须把库里存的开仓手续费读出来。

    这里刻意做【行为】断言，不去 grep 源码：
    上一版写成 "fee" in getsource(...)，结果被我自己的注释（注释里就有 fee 三个字）
    匹配成功——变异验证时把 SELECT 里的 fee 列删掉，测试照样绿。
    一条从注释里通过/失败的测试，等于什么都没验。
    """
    import time as _t
    from ai_trader.trade_db import TradeDB

    dbp = str(tmp_path / "t.db")
    tdb = TradeDB(dbp)
    tdb.start()
    try:
        tdb.log_trade({"symbol": "BTCUSDT", "side": "long", "qty": QTY,
                       "entry_ts": 1234, "entry_price": ENTRY_PX,
                       "stop_price": 70000.0, "target_price": 0.0,
                       "max_hold_bars": 10 ** 9, "open_reason": "test",
                       "fee": ENTRY_FEE, "paper": 1, "strategy": "trend"})
        row = None
        deadline = _t.time() + 5.0
        while _t.time() < deadline:          # 写走队列，必须轮询不能死等
            row = tdb.open_trade()
            if row:
                break
            _t.sleep(0.05)
        assert row is not None, "刚落库的未平仓记录没读出来"
        assert "fee" in row, (
            u"open_trade() 返回的字典里没有 fee。attach() 靠它恢复开仓手续费，"
            u"缺了这一项，平仓时开仓费会被算成 0。")
        assert row["fee"] == pytest.approx(ENTRY_FEE), (
            u"open_trade() 读出的 fee=%r，库里存的是 %r。"
            % (row.get("fee"), ENTRY_FEE))
    finally:
        tdb.stop()
