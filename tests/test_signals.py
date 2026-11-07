"""信号测试：突破只认收盘价越界 + 放量；插针必须收回。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_trader.config import TradingCfg  # noqa: E402
from ai_trader.market.agg import Bar  # noqa: E402
from ai_trader.trading import signals as sig  # noqa: E402


def mk_bar(iv, t, o, h, l, c, v=1.0, buy=0.5, sell=0.5):
    b = Bar(interval_ms=iv, open_time=t, open=o, high=h, low=l, close=c, volume=v)
    b.buy_base = buy
    b.sell_base = sell
    b.trades = 1
    b.closed = True
    return b


def _flat_3m(n=25, base=100.0):
    return [mk_bar(180000, i * 180000, base, base + 1, base - 1, base) for i in range(n)]


def test_breakout_long():
    cfg = TradingCfg()
    b3m = _flat_3m()
    b3 = [mk_bar(3000, 10_000_000 + i * 3000, 100, 100.5, 99.5, 100) for i in range(5)]
    feats = {"price": 102.0, "swing_high_3m": 101.0, "swing_low_3m": 99.0,
             "vol_z_3m": 2.0, "buy_ratio_3s": 0.7, "feature_time": 0}
    got = sig.detect(feats, {3000: b3, 180000: b3m}, cfg)
    assert got and got["side"] == "long" and got["name"] == "breakout_3m"


def test_breakout_needs_volume():
    cfg = TradingCfg()
    b3 = [mk_bar(3000, 10_000_000 + i * 3000, 100, 100.5, 99.5, 100) for i in range(5)]
    feats = {"price": 102.0, "swing_high_3m": 101.0, "swing_low_3m": 99.0,
             "vol_z_3m": 0.1, "buy_ratio_3s": 0.7, "feature_time": 0}
    assert sig.detect(feats, {3000: b3, 180000: _flat_3m()}, cfg) is None


def test_fakeout_fade_long():
    cfg = TradingCfg()
    window = [mk_bar(3000, 10_000_000 + i * 3000, 100, 100.4, 99.9, 100) for i in range(20)]
    # 最后一根插针破前低 99.9 -> 99.5，收盘收回 100.05
    window.append(mk_bar(3000, 10_000_000 + 20 * 3000, 100, 100.2, 99.5, 100.05))
    feats = {"price": 100.05, "swing_high_3m": 0.0, "swing_low_3m": 0.0,
             "vol_z_3m": 0.0, "buy_ratio_3s": 0.8, "feature_time": 0}
    got = sig.detect(feats, {3000: window, 180000: _flat_3m()}, cfg)
    assert got and got["name"] == "fakeout_fade" and got["side"] == "long"
    assert got["hint_stop"] < 99.5
