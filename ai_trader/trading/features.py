"""特征计算：全部来自同一份 3s 桶事实，没有第二个数据源。

铁律（k-trader 用 5 次翻车换来的）：
- 前视偏差是头号杀手。这里所有特征只用**已经收盘**的 bar，feature_time 显式返回，
  调用方必须保证 feature_time <= decision_time。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def atr_pct(bars: list, n: int = 20, price: Optional[float] = None) -> float:
    """用收盘价近似 ATR%（3s 桶没有前收盘，用 high-low 均值更稳）。"""
    if not bars:
        return 0.0
    seg = bars[-n:]
    rng = [b.high - b.low for b in seg]
    ref = price or seg[-1].close
    return (_mean(rng) / ref * 100.0) if ref else 0.0


def volume_zscore(bars: list, n: int = 20) -> float:
    if len(bars) < 3:
        return 0.0
    seg = bars[-n:]
    vols = [b.volume for b in seg[:-1]]
    sd = _std(vols)
    if sd <= 0:
        return 0.0
    return (seg[-1].volume - _mean(vols)) / sd


def impulse(bars: list, price: float, n: int = 6) -> dict:
    """短窗口冲击：最近 n 根 3s 桶的涨跌幅与量能。

    为什么不只用 3m 突破：3s 时钟下 3m 桶一直在变，突破阈值又宽，
    在 10 分钟尺度的横盘里几乎永远等不到。用 3s 自身的冲击可以拿到
    "同一件事的更早、更便宜的那一档"。
    """
    if len(bars) < n + 1:
        return {"move_pct": 0.0, "vol_x": 0.0, "n": len(bars)}
    seg = bars[-(n + 1):-1]
    ref = seg[0].close
    move = (price - ref) / ref * 100.0 if ref else 0.0
    recent_vol = sum(b.volume for b in bars[-n:]) / n
    base_vol = sum(b.volume for b in seg) / max(1, len(seg))
    return {"move_pct": round(move, 4),
            "vol_x": round(recent_vol / base_vol, 2) if base_vol > 0 else 0.0,
            "n": len(bars)}


def buy_ratio(bars: list, n: int = 5) -> float:
    seg = bars[-n:]
    b = sum(x.buy_base for x in seg)
    s = sum(x.sell_base for x in seg)
    tot = b + s
    return (b / tot) if tot > 0 else 0.5


def swing(bars: list, n: int, exclude_last: bool = True) -> tuple:
    seg = bars[-(n + 1):-1] if exclude_last else bars[-n:]
    if not seg:
        return 0.0, 0.0
    return max(b.high for b in seg), min(b.low for b in seg)


def compute(bars_by_iv: Dict[int, list], price: float) -> dict:
    """一次性算出决策现场需要的全部特征。

    bars_by_iv: {3000: [Bar...], 60000: [...], 180000: [...]}，均只含**已收盘**的 bar。
    """
    b3 = bars_by_iv.get(3000) or []
    b1m = bars_by_iv.get(60000) or []
    b3m = bars_by_iv.get(180000) or []
    b15m = bars_by_iv.get(900000) or []

    atr3 = atr_pct(b3, 20, price)
    atr3m = atr_pct(b3m, 20, price)
    vz3 = volume_zscore(b3, 20)
    vz3m = volume_zscore(b3m, 20)
    br = buy_ratio(b3, 5)

    mom = 0.0
    if len(b3) >= 21:
        ref = b3[-21].close
        mom = (price - ref) / ref * 100.0 if ref else 0.0

    hi_3m, lo_3m = swing(b3m, 20)
    hi_15m, lo_15m = swing(b15m, 12)

    range15 = 0.0
    if hi_15m and lo_15m and price:
        range15 = (hi_15m - lo_15m) / price * 100.0

    last_feature_ts = 0
    for lst in (b3, b3m, b15m):
        if lst:
            last_feature_ts = max(last_feature_ts, lst[-1].open_time + lst[-1].interval_ms)

    imp = impulse(b3, price, 6)
    hi_3s, lo_3s = swing(b3, 20)
    # 用自身的实现波动做标尺：所有阈值都以 sigma 为单位，而不是绝对百分比
    rets = []
    for k in range(max(1, len(b3) - 60), len(b3)):
        prev = b3[k - 1].close if k > 0 else b3[k].open
        if prev:
            rets.append((b3[k].close - prev) / prev * 100.0)
    sigma3 = _std(rets)
    move_sigma = (imp["move_pct"] / sigma3) if sigma3 > 1e-9 else 0.0

    dist_hi = ((hi_3m - price) / price * 100.0) if (hi_3m and price) else 0.0
    dist_lo = ((price - lo_3m) / price * 100.0) if (lo_3m and price) else 0.0

    return {
        "price": price,
        "dist_to_break_hi_pct": round(dist_hi, 4),
        "dist_to_break_lo_pct": round(dist_lo, 4),
        "atr3s_pct": round(atr3, 4),
        "atr3m_pct": round(atr3m, 4),
        "vol_z_3s": round(vz3, 2),
        "vol_z_3m": round(vz3m, 2),
        "buy_ratio_3s": round(br, 3),
        "momentum_60s_pct": round(mom, 4),
        "swing_high_3m": hi_3m,
        "swing_low_3m": lo_3m,
        "swing_high_15m": hi_15m,
        "swing_low_15m": lo_15m,
        "range_15m_pct": round(range15, 4),
        "impulse_pct": imp["move_pct"],
        "impulse_vol_x": imp["vol_x"],
        "sigma3_pct": round(sigma3, 6),
        "move_sigma": round(move_sigma, 2),
        "swing_high_3s": hi_3s,
        "swing_low_3s": lo_3s,
        "bars_3s": len(b3),
        "bars_3m": len(b3m),
        "feature_time": last_feature_ts,
    }
