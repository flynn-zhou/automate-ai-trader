"""信号层：v1 只有两个，每个都必须能回答"它赚的是谁的钱"。

S1 breakout_3m —— 突破跟随
    赚的钱：突破后追单的动量交易者 + 止损被打的单子。3m 收盘突破前 20 根高点且放量。
    反证：k-trader 的 breakout 族是它唯一稳定为正的信号族（156 笔 +96U），所以保留。

S2 fakeout_fade —— 假突破回归
    赚的钱：插针打止损的人。3s 级别破前低后迅速收回（同一根内），反向做。
    反证：k-trader 的 range_fakeout_reversal 亏了 214U（占全部亏损 80%），
    但那是 15m/30m 级别的"区间边界假突破"，止损放 0.3%~0.95%、用 100x 去做，
    必然被噪音扫。这里改成 3s 级插针 + REST 逐笔验证收回，尺度完全对齐。
"""
from __future__ import annotations

from typing import Optional


def detect(features: dict, bars_by_iv: dict, cfg) -> Optional[dict]:
    """返回 {name, side, reason, ref_price, hint_stop} 或 None。"""
    b3 = bars_by_iv.get(3000) or []
    b3m = bars_by_iv.get(180000) or []
    if len(b3) < 3 or len(b3m) < 21:
        return None

    last3 = b3[-1]
    price = features["price"]

    # ---- S1 突破跟随：3m 收盘突破前 20 根极值 + 量能放大 ----
    prev_hi = features["swing_high_3m"]
    prev_lo = features["swing_low_3m"]
    if prev_hi and prev_lo:
        if price > prev_hi and features["vol_z_3m"] >= cfg.breakout_vol_z:
            return {
                "name": "breakout_3m",
                "side": "long",
                "reason": "3m 收盘 %.1f 突破前 20 根高点 %.1f（量能 z=%.2f）"
                          % (price, prev_hi, features["vol_z_3m"]),
                "ref_price": prev_hi,
                "hint_stop": max(features["swing_low_3m"],
                                 price * (1 - cfg.min_stop_pct / 100.0)),
            }
        if price < prev_lo and features["vol_z_3m"] >= cfg.breakout_vol_z:
            return {
                "name": "breakout_3m",
                "side": "short",
                "reason": "3m 收盘 %.1f 跌破前 20 根低点 %.1f（量能 z=%.2f）"
                          % (price, prev_lo, features["vol_z_3m"]),
                "ref_price": prev_lo,
                "hint_stop": min(features["swing_high_3m"],
                                 price * (1 + cfg.min_stop_pct / 100.0)),
            }

    # ---- S3 3s 微型动量：短窗口冲击 + 量能放大，止损放到 20 根 3s 的结构外 ----
    imp = features.get("impulse_pct", 0.0)
    vx = features.get("impulse_vol_x", 0.0)
    w_hi = features.get("swing_high_3s", 0.0)
    w_lo = features.get("swing_low_3s", 0.0)
    ms = abs(features.get("move_sigma", 0.0))
    sig3 = features.get("sigma3_pct", 0.0)
    # 噪声大的时候要求更大的位移，噪声小的时候自然更容易触发——尺度自适应的
    stop_mult = max(cfg.stop_sigma_mult, cfg.min_stop_pct / sig3) if sig3 > 0 else cfg.stop_sigma_mult
    if vx >= cfg.micro_vol_x and ms >= cfg.micro_sigma and w_hi and w_lo and sig3 > 0:
        if imp > 0 and price > w_hi:
            stop = min(w_lo * (1 - cfg.stop_buffer_pct / 100.0),
                       price * (1 - sig3 * stop_mult / 100.0))
            return {
                "name": "micro_momentum",
                "side": "long",
                "reason": "6 根 3s 内上涨 %.3f%%（=%.1fσ，σ=%.4f%%）量能 %.2fx，"
                          "价格 %.1f 站上 3s 结构高 %.1f"
                          % (imp, ms, sig3, vx, price, w_hi),
                "ref_price": w_hi,
                "hint_stop": stop,
            }
        if imp < 0 and price < w_lo:
            stop = max(w_hi * (1 + cfg.stop_buffer_pct / 100.0),
                       price * (1 + sig3 * stop_mult / 100.0))
            return {
                "name": "micro_momentum",
                "side": "short",
                "reason": "6 根 3s 内下跌 %.3f%%（=%.1fσ，σ=%.4f%%）量能 %.2fx，"
                          "价格 %.1f 跌破 3s 结构低 %.1f"
                          % (imp, ms, sig3, vx, price, w_lo),
                "ref_price": w_lo,
                "hint_stop": stop,
            }

    # ---- S2 插针回归：3s 破前低后收回，同一根内完成 ----
    if len(b3) >= 21:
        window = b3[-21:-1]
        win_lo = min(b.low for b in window)
        win_hi = max(b.high for b in window)
        wick = last3.low <= win_lo * (1 - cfg.fakeout_pierce_pct / 100.0)
        reclaim = last3.close >= win_lo
        if wick and reclaim and features["buy_ratio_3s"] >= 0.5:
            return {
                "name": "fakeout_fade",
                "side": "long",
                "reason": "3s 插针到 %.1f（破前低 %.1f 达 %.3f%%）后收回 %.1f，主买占比 %.2f"
                          % (last3.low, win_lo, cfg.fakeout_pierce_pct, last3.close,
                             features["buy_ratio_3s"]),
                "ref_price": win_lo,
                "hint_stop": last3.low * (1 - cfg.stop_buffer_pct / 100.0),
            }
        wick_up = last3.high >= win_hi * (1 + cfg.fakeout_pierce_pct / 100.0)
        reject = last3.close <= win_hi
        if wick_up and reject and features["buy_ratio_3s"] <= 0.5:
            return {
                "name": "fakeout_fade",
                "side": "short",
                "reason": "3s 插针到 %.1f（破前高 %.1f 达 %.3f%%）后回落 %.1f，主买占比 %.2f"
                          % (last3.high, win_hi, cfg.fakeout_pierce_pct, last3.close,
                             features["buy_ratio_3s"]),
                "ref_price": win_hi,
                "hint_stop": last3.high * (1 + cfg.stop_buffer_pct / 100.0),
            }
    return None
