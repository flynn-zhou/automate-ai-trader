# -*- coding: utf-8 -*-
"""统计必须能按策略分开，否则会拿别的策略的战绩冒充当前的。

2026-11-01 的实际问题：
大屏头条显示【4 笔 / 胜率 25% / 净利 -20.46U】，
而这 4 笔全部来自【已经被否掉的剥头皮策略】，
真正在跑的趋势策略是 0 笔。

后果有两个方向：
  - 用户看大屏以为系统在亏钱
  - 我拿"历史统计"印证趋势策略的边际时，其实在看别的策略

所以这条测试不是在测一个格式，是在测【能不能分清自己在说哪个策略】。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.trade_db import TradeDB


def _mk():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.db")
    # 先用 TradeDB 建表，再直接插数据（本测试只验 stats() 的 SQL 口径，
    # 写入路径由 test_engine_ledger / test_trend_lifecycle 覆盖）
    t = TradeDB(p)
    t.start()
    t.stop()
    return p


def _seed(p, rows):
    c = sqlite3.connect(p)
    for strat, pnl, paper in rows:
        c.execute(
            "INSERT INTO trades(symbol,side,qty,entry_ts,entry_price,exit_ts,"
            "exit_price,pnl_gross,fee,pnl_net,paper,strategy)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("BTCUSDT", "long", 0.001, 1, 80000.0, 2, 81000.0,
             pnl, 0.0, pnl, paper, strat))
    c.commit()
    c.close()


def _stats(p, strategy=None):
    t = TradeDB(p)
    t.start()
    try:
        return t.stats(strategy)
    finally:
        t.stop()


def test_stats_filter_by_strategy():
    """按策略过滤：趋势策略不能把剥头皮的战绩算进来。"""
    p = _mk()
    _seed(p, [("scalp", -9.0, 1), ("scalp", -11.0, 1)])

    st = _stats(p, "trend")
    assert st["trades"] == 0, (
        u"趋势策略 0 笔，但 stats('trend') 报 %d 笔 —— "
        u"把已废弃剥头皮的交易算进来了" % st["trades"])
    assert st["pnl_net"] == 0.0
    assert st["win_rate"] is None, u"0 笔时胜率必须是 None（未知），不能是 0.0（很差）"

    sc = _stats(p, "scalp")
    assert sc["trades"] == 2
    assert sc["pnl_net"] == pytest.approx(-20.0)


def test_stats_includes_by_strategy_breakdown():
    """汇总数必须能拆开检查，否则没人看得出它混了什么。"""
    p = _mk()
    _seed(p, [("scalp", -20.0, 1), ("trend", 5.0, 1)])

    st = _stats(p)
    by = st["by_strategy"]
    assert "scalp" in by and "trend" in by, u"by_strategy 没把策略拆开：%s" % by
    assert by["scalp"]["trades"] == 1
    assert by["trend"]["trades"] == 1
    assert by["scalp"]["pnl_net"] == pytest.approx(-20.0)
    assert by["trend"]["pnl_net"] == pytest.approx(5.0)
    assert st["pnl_net"] == pytest.approx(sum(v["pnl_net"] for v in by.values()))


def test_stats_all_when_no_strategy_given():
    """不传策略时保持旧行为（全部），避免悄悄改变已有调用方的语义。"""
    p = _mk()
    _seed(p, [("scalp", -20.0, 1), ("trend", 5.0, 1)])

    st = _stats(p)
    assert st["trades"] == 2
    assert st["pnl_net"] == pytest.approx(-15.0)
    assert "strategy" not in st, u"不传策略时不该声称自己属于某个策略"


def test_open_trade_only_counts_unclosed():
    """已平仓的交易不算未平仓——0 笔未平时大屏必须能这么说。"""
    p = _mk()
    _seed(p, [("trend", 5.0, 1)])
    t = TradeDB(p)
    t.start()
    try:
        assert t.open_trade() is None, u"已平仓的交易被当成未平仓了"
    finally:
        t.stop()
