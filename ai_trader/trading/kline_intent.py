# -*- coding: utf-8 -*-
"""K 线意图识别（功能型方法）：这根 K 线想干什么、是不是空恐慌盘、带不带量。

定位
----
**辅助下单与离场，不是决策者。** 它回答三个问题：
    1. 意图与目的：突破 / 延续 / 衰竭 / 派发 / 无
    2. 是否空恐慌盘（插针洗盘）
    3. 带不带量

硬约束：**P0 保命分支禁止调用本模块。**
亏损到位就必须走，不能因为「K 线看着像洗盘」而留仓。
position_guard.py 里 P0 分支不读 kline，这是设计而非疏漏。

为什么每条判定都要带证据
------------------------
「放量突破」这种结论如果不写出「量是过去 20 根中位数的 2.1 倍」，
事后无法复核它到底看懂了还是在猜。所以每个结论都附带 evidence 字符串，
页面上能直接展示判定依据。

阈值全部写成显式常量，不散落在 if 里——调参必须看得见改了什么。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

# ---------------- 阈值（唯一出处，调参只改这里）----------------
VOL_LOOKBACK = 20          # 量能基准回看根数
VOL_HEAVY = 1.5            # >= 中位数 1.5 倍 = 放量
VOL_THIN = 0.6             # <= 中位数 0.6 倍 = 缩量
ATR_PERIOD = 14            # ATR 周期
BODY_STRONG = 0.55         # 实体占全幅 >= 55% = 强实体
BODY_WEAK = 0.35           # 实体占全幅 <= 35% = 小实体（派发/犹豫）
WICK_LONG = 2.0            # 影线 >= 2 倍实体 = 长影线
PANIC_WICK = 2.5           # 恐慌插针要求的下影线倍数
PANIC_CLOSE_POS = 0.6      # 收盘回到全幅 60% 以上才算收回
BREAK_BUFFER_ATR = 0.1     # 突破需要越过边界 0.1 ATR，避免压线误判
FAR_FROM_P = 1.5           # 离顺向边界 1.5 ATR 以上算「已走远」

INTENT_NONE = "none"
INTENT_BREAKOUT = "breakout"
INTENT_CONTINUATION = "continuation"
INTENT_EXHAUSTION = "exhaustion"
INTENT_DISTRIBUTION = "distribution"

VOL_HEAVY_S = "放量"
VOL_THIN_S = "缩量"
VOL_NORMAL_S = "常态"


@dataclass(frozen=True)
class KlineRead:
    """一根 K 线的读法。字段与 position_guard.decide(kline=) 的契约一致。"""
    intent: str = INTENT_NONE
    panic_flush: bool = False
    volume_regime: str = VOL_NORMAL_S
    low: float = 0.0
    atr: float = 0.0
    evidence: tuple = field(default=())

    def as_dict(self) -> dict:
        return {"intent": self.intent, "panic_flush": self.panic_flush,
                "volume_regime": self.volume_regime, "low": self.low,
                "atr": self.atr, "evidence": list(self.evidence)}


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _range(h: float, l: float) -> float:
    return max(h - l, 1e-12)


def atr(bars: Sequence[Sequence], i: int, period: int = ATR_PERIOD) -> float:
    """真实波幅均值。数据不足时用已有根数，全无则 0。"""
    lo = max(0, i - period + 1)
    spans = [bars[k][2] - bars[k][3] for k in range(lo, i + 1)]
    spans = [s for s in spans if s > 0]
    return sum(spans) / len(spans) if spans else 0.0


def volume_regime(bars: Sequence[Sequence], i: int,
                  lookback: int = VOL_LOOKBACK) -> tuple:
    """返回 (档位, 倍数或 None, 证据串)。量缺失时返回常态并说明。"""
    vols = [b[5] for b in bars[max(0, i - lookback + 1):i + 1]
            if len(b) > 5 and b[5] is not None and b[5] > 0]
    if len(vols) < 3:
        return VOL_NORMAL_S, None, "量能数据不足，按常态处理"
    cur = vols[-1]
    base = statistics.median(vols[:-1]) if len(vols) > 1 else vols[-1]
    if base <= 0:
        return VOL_NORMAL_S, None, "基准量为 0，按常态处理"
    mult = cur / base
    if mult >= VOL_HEAVY:
        return VOL_HEAVY_S, mult, "量 %.2f = 前%d根中位数的 %.2f 倍（放量线 %.1f）" % (
            cur, len(vols) - 1, mult, VOL_HEAVY)
    if mult <= VOL_THIN:
        return VOL_THIN_S, mult, "量 %.2f = 前%d根中位数的 %.2f 倍（缩量线 %.1f）" % (
            cur, len(vols) - 1, mult, VOL_THIN)
    return VOL_NORMAL_S, mult, "量 %.2f = 前%d根中位数的 %.2f 倍（常态）" % (
        cur, len(vols) - 1, mult)


def read_kline(bars: Sequence[Sequence], i: Optional[int] = None,
               range_state: Optional[dict] = None) -> KlineRead:
    """读第 i 根 K 线（默认最后一根）。

    bars 元素：至少 (ts, o, h, l, c)，可带第 6 位 volume。
    range_state：可选，api._row 的产物（含 direction / p / hard_up / hard_dn），
                 有它才能判「突破边界」。没有就只判形态，不臆造突破。

    判定顺序即优先级，不做并行条件：
        恐慌盘 > 突破 > 派发 > 衰竭 > 延续 > 无
    理由：恐慌盘是「反方向的插针」，最容易被误读成突破，必须先排掉。
    """
    if not bars:
        return KlineRead(evidence=("没有 K 线数据",))
    if i is None:
        i = len(bars) - 1
    if i < 0 or i >= len(bars):
        raise IndexError("K 线下标越界: %s（共 %d 根）" % (i, len(bars)))

    b = bars[i]
    o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
    rng = _range(h, l)
    body = _body(o, c)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_ratio = body / rng
    ev = []
    vol_s, vol_mult, vol_ev = volume_regime(bars, i)
    ev.append(vol_ev)
    a = atr(bars, i)
    if a > 0:
        ev.append("ATR(%d)=%.4f，本根全幅 %.4f = %.2f ATR" % (ATR_PERIOD, a, rng, rng / a))

    # ---- 1) 恐慌盘 / 空头陷阱：长下影 + 放量 + 收盘收回上半部 ----
    if (lower_wick >= PANIC_WICK * max(body, rng * 0.05)
            and vol_s == VOL_HEAVY_S
            and (c - l) / rng >= PANIC_CLOSE_POS):
        ev.append("下影线 %.4f >= %.1f×实体 %.4f，收盘位于全幅 %.0f%% 处" % (
            lower_wick, PANIC_WICK, body, (c - l) / rng * 100))
        return KlineRead(intent=INTENT_NONE, panic_flush=True, volume_regime=vol_s,
                         low=l, atr=a, evidence=tuple(ev))

    # ---- 2) 突破：收盘越过顺向边界 P，且越过 0.1 ATR ----
    if range_state:
        d = range_state.get("direction") or ""
        p = range_state.get("p")
        if p and d in ("UP", "DOWN"):
            buf = max(a * BREAK_BUFFER_ATR, p * 1e-6)
            if d == "UP" and c > p + buf:
                ev.append("收盘 %.4f 越过上沿 P=%.4f，超出 %.4f（缓冲 %.4f）" % (
                    c, p, c - p, buf))
                if vol_s == VOL_HEAVY_S:
                    return KlineRead(intent=INTENT_BREAKOUT, volume_regime=vol_s,
                                     low=l, atr=a, evidence=tuple(ev))
                ev.append("但%s，突破未获量能确认 → 不判突破" % vol_s)
            elif d == "DOWN" and c < p - buf:
                ev.append("收盘 %.4f 跌破下沿 P=%.4f，超出 %.4f（缓冲 %.4f）" % (
                    c, p, p - c, buf))
                if vol_s == VOL_HEAVY_S:
                    return KlineRead(intent=INTENT_BREAKOUT, volume_regime=vol_s,
                                     low=l, atr=a, evidence=tuple(ev))
                ev.append("但%s，突破未获量能确认 → 不判突破" % vol_s)

    # ---- 3) 派发：贴边界 + 放量 + 小实体（冲高/杀低后走不动）----
    if range_state and vol_s == VOL_HEAVY_S and body_ratio <= BODY_WEAK:
        near = _near_boundary(c, range_state, a)
        if near:
            ev.append("放量但实体只占全幅 %.0f%%，且贴近 %s → 派发" % (
                body_ratio * 100, near))
            return KlineRead(intent=INTENT_DISTRIBUTION, volume_regime=vol_s,
                             low=l, atr=a, evidence=tuple(ev))

    # ---- 4) 衰竭：反方向长影线，且价格已离 P 走远 ----
    if range_state:
        d = range_state.get("direction") or ""
        p = range_state.get("p")
        if p and a > 0 and abs(c - p) >= FAR_FROM_P * a:
            against = (upper_wick if d == "UP" else lower_wick)
            if against >= WICK_LONG * max(body, rng * 0.05):
                ev.append("%s 方向已离 P=%.4f 走 %.2f ATR，反向影线 %.4f >= %.1f×实体 → 衰竭" % (
                    d, p, abs(c - p) / a, against, WICK_LONG))
                return KlineRead(intent=INTENT_EXHAUSTION, volume_regime=vol_s,
                                 low=l, atr=a, evidence=tuple(ev))

    # ---- 5) 延续：强实体且方向与区间一致 ----
    if body_ratio >= BODY_STRONG:
        d = (range_state or {}).get("direction") or ""
        up = c > o
        if d == "UP" and up:
            ev.append("强实体 %.0f%% 且收阳，与区间方向 UP 一致 → 延续" % (body_ratio * 100))
            return KlineRead(intent=INTENT_CONTINUATION, volume_regime=vol_s,
                             low=l, atr=a, evidence=tuple(ev))
        if d == "DOWN" and not up:
            ev.append("强实体 %.0f%% 且收阴，与区间方向 DOWN 一致 → 延续" % (body_ratio * 100))
            return KlineRead(intent=INTENT_CONTINUATION, volume_regime=vol_s,
                             low=l, atr=a, evidence=tuple(ev))
        if not d:
            ev.append("强实体 %.0f%%，但无区间背景，不下结论" % (body_ratio * 100))

    ev.append("未满足任何形态判据")
    return KlineRead(intent=INTENT_NONE, volume_regime=vol_s, low=l, atr=a,
                     evidence=tuple(ev))


def _near_boundary(c: float, range_state: dict, a: float) -> str:
    """是否贴近某条边界，返回边界名或空串。贴边 = 距边界 <= 0.5 ATR。"""
    if a <= 0:
        return ""
    for name, key in (("反向边界 R↑", "hard_up"), ("反向边界 R↓", "hard_dn"),
                      ("顺向边界 P", "p"), ("关键价 K", "key")):
        v = range_state.get(key)
        if v and abs(c - v) <= 0.5 * a:
            return "%s(%.4f)" % (name, v)
    return ""
