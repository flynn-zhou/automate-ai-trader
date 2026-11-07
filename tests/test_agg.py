"""聚合器单元测试：3s 桶 -> 多周期，收盘边界与主动买卖归集。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_trader.market.agg import MultiAggregator, bucket_start  # noqa: E402


def test_bucket_alignment():
    assert bucket_start(1793462918887, 3000) % 3000 == 0
    assert bucket_start(1793462918887, 180000) == 1793462760000
    assert bucket_start(1793462760000, 180000) == 1793462760000   # 正好落在桶边界上


def test_feed_rolls_over_and_closes():
    agg = MultiAggregator([3000, 60000])
    t0 = 1793462820000            # 一个整分钟
    agg.feed(100.0, t0, 1.0, 100.0, True, 1)
    agg.feed(102.0, t0 + 1000, 2.0, 204.0, False, 1)
    closed, updated = agg.feed(101.0, t0 + 3000, 0.5, 50.5, True, 1)
    # 3s 桶跨桶收盘；1m 桶未收盘
    assert any(b.interval_ms == 3000 for b in closed)
    b3 = [b for b in closed if b.interval_ms == 3000][0]
    assert (b3.open, b3.high, b3.low, b3.close) == (100.0, 102.0, 100.0, 102.0)
    assert abs(b3.volume - 3.0) < 1e-9
    assert abs(b3.buy_base - 1.0) < 1e-9
    assert abs(b3.sell_base - 2.0) < 1e-9
    assert b3.trades == 2
    assert not any(b.interval_ms == 60000 for b in closed)


def test_minute_close_after_rollover():
    agg = MultiAggregator([3000, 60000])
    t0 = 1793462820000
    agg.feed(100.0, t0 + 59000)
    closed, _ = agg.feed(101.0, t0 + 60000)
    assert any(b.interval_ms == 60000 for b in closed)
    b = [b for b in closed if b.interval_ms == 60000][0]
    assert b.open_time == t0 and b.close == 100.0 and b.closed


def test_price_only_fallback_keeps_ohlc():
    agg = MultiAggregator([3000])
    agg.feed_price_only(500.0, 1793462820000)
    agg.feed_price_only(501.0, 1793462821000)
    cur = agg.current(3000)
    assert (cur.open, cur.high, cur.close) == (500.0, 501.0, 501.0)
    assert cur.volume == 0.0
