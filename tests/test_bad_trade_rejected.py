# -*- coding: utf-8 -*-
"""畸形逐笔成交（价格 0 / 数量 0 / 成交类型 NA）必须被挡在摄取边界外。

起因（2026-11-02）：从服务器抓 60 秒原始 @trade 报文，732 条里有 2 条长这样：

    {"e":"trade","t":8224963627,"p":"0","q":"0","X":"NA","m":false,"st":1}

我方代码直接 float(d["p"]) 得到 0.0，于是：
  1. 若它恰好是新 3s 桶的第一笔，bar 会以 open=0/low=0 创建，
     之后真实价格只能更新 high/close，**low 永远是 0**——
     任何基于最低价的逻辑（止损、波动率、结构低点）都会被污染。
     线上实测 32338 根 3s bar 里有 8 根 low=0、1 根 open=0。
  2. 更严重：_ingest 里 self.last_price = tr["price"]，
     一条畸形报文就能把决策看到的现价变成 0。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.market.agg import MultiAggregator


BAD = [
    {"price": 0.0, "qty": 0.0, "ts": 1000},      # 价格 0
    {"price": -1.0, "qty": 1.0, "ts": 1000},     # 负价
    {"price": 100.0, "qty": -0.5, "ts": 1000},   # 负数量
]


def test_aggregator_rejects_bad_prices():
    agg = MultiAggregator([3000, 15000])
    for bad in BAD:
        agg.feed(bad["price"], bad["ts"], bad["qty"], bad["price"] * bad["qty"], True, 1)
    b = agg.current(3000)
    assert b is None or (b.open > 0 and b.low > 0), (
        u"畸形成交造出了 open/low 为 0 的 bar：open=%r low=%r"
        % (None if b is None else b.open, None if b is None else b.low))


def test_good_bar_unaffected_by_bad_tick():
    """真实成交建立的 bar，不能被随后混进来的 0 价把 low 拉下去。"""
    agg = MultiAggregator([3000])
    agg.feed(77700.0, 1000, 1.0, 77700.0, True, 1)
    agg.feed(77750.0, 1500, 1.0, 77750.0, True, 1)
    b_before = agg.current(3000)
    low_before = b_before.low
    agg.feed(0.0, 1600, 0.0, 0.0, True, 1)      # 畸形
    b = agg.current(3000)
    assert b.low == pytest.approx(low_before), (
        u"0 价把 low 从 %r 拉到 %r" % (low_before, b.low))
    assert b.close == pytest.approx(77750.0), u"0 价把 close 改成了 %r" % b.close


def test_bad_tick_does_not_count_as_trade():
    """畸形报文不该计入成交笔数——它是环境噪声，不是一次成交。"""
    agg = MultiAggregator([3000])
    agg.feed(77700.0, 1000, 1.0, 77700.0, True, 1)
    n_before = agg.current(3000).trades
    agg.feed(0.0, 1100, 0.0, 0.0, True, 1)
    assert agg.current(3000).trades == n_before, (
        u"畸形报文被算成了成交：%d -> %d"
        % (n_before, agg.current(3000).trades))


def test_ingest_never_sets_zero_last_price():
    """_ingest 是 last_price 的唯一写入点之一，必须挡住 0 价。

    比坏 bar 更严重：引擎看到 last_price=0 时，
    涨跌幅、止损距离、清仓判定全部基于一个不存在的价格。
    """
    from ai_trader.feed import Feed

    class _Store:
        def put_event(self, *a, **k): pass
        def put_bar(self, *a, **k): pass

    f = Feed.__new__(Feed)          # 不走 __init__（那里会连网络）
    f.last_price = 77700.0
    f.last_price_ts = 0
    f.ticks_total = 0
    f._recent_trades = []
    f.agg = MultiAggregator([3000])
    f._after_feed = lambda *a, **k: None

    f._ingest({"price": 0.0, "qty": 0.0, "ts": 1000, "is_buy": True})
    assert f.last_price == pytest.approx(77700.0), (
        u"0 价成交把 last_price 改成了 %r" % f.last_price)
    assert f.ticks_total == 0, u"无效成交被计入 ticks_total"

    # 正常成交仍然要生效
    f._ingest({"price": 77800.0, "qty": 1.0, "ts": 2000, "is_buy": True})
    assert f.last_price == pytest.approx(77800.0)
    assert f.ticks_total == 1


def test_dropped_counter_is_observable():
    """丢弃必须可计数、可上报。

    否则「流本来就很干净」和「脏数据被静默丢掉」在监控上完全一样，
    我们又回到"指标在撒谎"。这个计数器要出现在 /api/health 里。
    """
    from ai_trader.market.ws import AggTradeWS

    async def noop(d): pass
    w = AggTradeWS("wss://example/ws", "BTCUSDT", noop)
    assert w.dropped == 0, "初始丢弃计数应为 0"

    import inspect
    from ai_trader import feed as feed_mod
    src = inspect.getsource(feed_mod)
    assert "\"ws_dropped\":" in src, (
        u"快照里没有 ws_dropped —— 丢弃数不出现在 /api/health，"
        u"过滤就是静默的")


def test_health_endpoint_exposes_degradation_fields():
    """/api/health 必须把"降级"和"过滤"两类事实透出来。

    只透 src/ws_connected 是不够的：
      - 连上但不推 -> ws_silent
      - 脏数据被丢 -> ws_dropped
    少了这两个字段，看板上"一切正常"和"被静默处理"长得一样。
    这里用行为断言（真的调接口），不是匹配源码文本。
    """
    from ai_trader.web.app import create_app

    class _Hub:
        subscribers = 0

    class _FakeFeed:
        hub = _Hub()

        def snapshot(self):
            # 完整的快照形状（feed 真的会返回这些键）
            return {"type": "state", "symbol": "BTCUSDT", "src": "WS",
                    "price": 77700.0, "price_ts": 0,
                    "ws_connected": True, "ws_stream": "trade",
                    "ws_silent": True, "ws_dropped": 7,
                    "ws_idle_ms": 123456, "ws_reconnects": 0, "ws_ticks": 1,
                    "rest_latency_ms": 0, "rest_fails": 0,
                    "ticks_total": 10, "rest_ticks": 0, "trades_per_sec": 1.0,
                    "uptime_s": 60, "bars": {}, "server_ts": 0}

    from ai_trader.config import AppCfg
    app = create_app(AppCfg(), _FakeFeed())
    resp = app.test_client().get("/api/health")
    assert resp.status_code == 200
    d = resp.get_json()

    # 行为断言：字段真的出现在响应里，而不是源码里有这几个字
    assert d.get("ws_stream") == "trade", (
        u"/api/health 没透出 ws_stream（实际流名），当前键：%r" % sorted(d))
    assert d.get("ws_silent") is True, (
        u"/api/health 没透出 ws_silent —— 连上但不推的状态看不出来")
    assert d.get("ws_dropped") == 7, (
        u"/api/health 没透出 ws_dropped —— 被丢弃的脏数据不可见")


def test_price_only_heartbeat_still_works():
    """自检：过滤不能误伤正常的价格心跳（有价无量，是有意设计）。"""
    agg = MultiAggregator([3000])
    agg.feed_price_only(77700.0, 1000)
    b = agg.current(3000)
    assert b is not None and b.open == pytest.approx(77700.0)
    assert b.trades == 0, u"价格心跳不该增加成交笔数"
