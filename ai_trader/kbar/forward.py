# -*- coding: utf-8 -*-
"""K 线演化 · 第 5 步：事后确认层（表 3 `kbar_forward`）。

    ┌──────────────────────────────────────────────────────────────┐
    │ 这一层是**事后**的。「之后涨了没有」这句话本身就在偷未来 ——   │
    │ 所以它必须物理隔开：                                             │
    │   · 单独一张表（表 3），不摊进表 1 的宽表                        │
    │   · 单独一个进程（tools/backfill_kbar_forward.py）                │
    │   · **当下判定路径不许 import 本模块**（有测试钉着这条红线）      │
    └──────────────────────────────────────────────────────────────┘

设计稿 §六 表 3 给的结构是：

    symbol, interval_ms, ts, ret1 REAL, ret3 REAL, ret5 REAL, held INT

只有这四列。本模块按它实现，另外多写三列（`held_line`/`held_ref_price`/
`held_side`）—— 这不是改设计，是让 `held` 这个数**可审计**：

    设计稿的 `held` 没说「哪条线守住了」。而不写下来的话，
    一年后没人能回答「这个 held=1 是哪条线的 1」——
    这与表 1 的 `event` 列是有损的、所以要单开表 2.5 是同一个道理。

口径（都在文件顶部写成常量，一处可改）：

    ret_k    = c[t+k] / c[t] - 1                收盘对收盘，不扣费（D-12 占位）
    held     = 之后 HELD_BARS 根的收盘，是否一直接在事件判定那一侧（D-13 占位）
    held 参照价 = kbar_event.line_price（事件判定当时用的那个线价），**固定不变**

最后一条是刻意的：用「固定 p」答的是「价格有没有回到这条**水平**的另一侧」；
若改成每根都用线自己当根的价位，就把「线在移动」和「价格没守住」混成了一个数。
两种问法都合理，选哪种是口径问题 —— 记在待确定清单 D-20。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 口径常量
# 事后看多少根。顺序无所谓，必须与表 3 的列一一对应。
FORWARD_HORIZONS: Tuple[int, ...] = (1, 3, 5)

# 「守住」看多少根。
# ⚠ 占位：这是「阈值」，属判断标准，代码不许自己拍（D-13）。
HELD_BARS = 3

# 收盘价比较的容差。0 表示严格：收盘正好等于线价算「不算守住」。
# ⚠ 占位：等于线价时算不算守住，是口径问题（D-13）。
HELD_TOL_PCT = 0.0

# ret 的取价方式。名字进 kbar_meta，改口径时这里改一个字符串。
RET_BASIS = "close_to_close"
RET_FEE = 0.0

# 事件方向 → 「判定那一侧」。
# 向上突破判定的是「站上线上方」；向下突破判定的是「跌到线下方」。
# 反包不在这里：反包说的是「上一次突破没站住」，它自己不是一次穿越判定，
# 所以对反包只用「当根的收盘在哪一侧」，不预设方向 —— 见 held_of()。
_CROSS_SIDE = {"向上突破": "above", "向下突破": "below"}

BREAK_UP = "向上突破"
BREAK_DOWN = "向下突破"
FAILED_BREAK = "突破失败·反包"

SIDE_ABOVE = "above"
SIDE_BELOW = "below"
SIDE_AT = "at"


class ForwardLookaheadError(AssertionError):
    """事后层用到了不该用的数据（例如用当根或更早的 K 线算收益）。"""


def side_of_close(close: float, price: float, tol_pct: float = HELD_TOL_PCT) -> str:
    """收盘在参照价的哪一侧。tol_pct 是比例（0.001 = 0.1%）。"""
    if price is None or close is None:
        return SIDE_AT
    band = abs(float(price)) * float(tol_pct)
    if float(close) > float(price) + band:
        return SIDE_ABOVE
    if float(close) < float(price) - band:
        return SIDE_BELOW
    return SIDE_AT


def forward_returns(bars: Sequence[dict], horizons: Sequence[int] = FORWARD_HORIZONS,
                    fee: float = RET_FEE) -> Dict[int, Dict[int, Optional[float]]]:
    """算每根的未来收益：{ts: {k: ret_k}}。

    只用 **严格之后** 的 K 线：ret_k 用 bars[i+k]，k >= 1。
    后面不足 k 根就给 None，并且**不猜**（不拿最后一根顶替、不按已走过的算）。

    `bars` 必须按 ts 升序且等间隔；调用方负责（build_and_persist_forward 会校验）。
    """
    n = len(bars)
    out: Dict[int, Dict[int, Optional[float]]] = {}
    for i, b in enumerate(bars):
        ts = int(b["ts"])
        c0 = b.get("c")
        per: Dict[int, Optional[float]] = {}
        for k in horizons:
            if c0 is None or i + k >= n:
                per[int(k)] = None
                continue
            ck = bars[i + k].get("c")
            if ck is None or float(c0) == 0.0:
                per[int(k)] = None
                continue
            # 不扣费时就是纯价比；扣费口径留给 D-12，这里只在 fee != 0 时生效
            per[int(k)] = (float(ck) / float(c0) - 1.0) - float(fee)
        out[ts] = per
    return out


def held_of(bars: Sequence[dict], ts: int, ref_price: float, side: str,
            held_bars: int = HELD_BARS,
            tol_pct: float = HELD_TOL_PCT) -> Optional[int]:
    """之后 held_bars 根是否一直接在 `side` 这一侧。

    返回 1 / 0 / None：
      1    之后每根收盘都在 side 这一侧
      0    中间有根收盘跑到另一侧（或正好压在线上且容差为 0）
      None 后面不足 held_bars 根 —— **算不出来就留空**，别拿已走过的凑

    参照价 `ref_price` 是**固定**的（事件判定当时的线价），不随线移动。
    """
    idx = {int(b["ts"]): i for i, b in enumerate(bars)}
    i = idx.get(int(ts))
    if i is None or ref_price is None or side not in (SIDE_ABOVE, SIDE_BELOW):
        return None
    n = len(bars)
    if i + int(held_bars) >= n:
        return None
    for j in range(1, int(held_bars) + 1):
        c = bars[i + j].get("c")
        if c is None:
            return None
        if side_of_close(float(c), float(ref_price), tol_pct) != side:
            return 0
    return 1


def held_side_for(event: str, close_at_t: float, ref_price: float,
                  tol_pct: float = HELD_TOL_PCT) -> Optional[str]:
    """决定「守住」要拿哪一侧去比。

    突破：用事件本身的判定侧（向上突破 → above，向下突破 → below）。
    反包：没有自己的穿越方向，用**当根收盘所在的那一侧** ——
          它的意思是「当根收在这一侧，之后还守不守得住」，这是一个同一把尺子的问题，
          不是给反包临时发明一个新方向。
    """
    if event in _CROSS_SIDE:
        return _CROSS_SIDE[event]
    s = side_of_close(float(close_at_t), float(ref_price), tol_pct)
    return None if s == SIDE_AT else s


def build_forward(symbol: str, interval_ms: int, bars: Sequence[dict],
                  bar_events: Dict[int, dict],
                  event_rows: Optional[Sequence[dict]] = None,
                  horizons: Sequence[int] = FORWARD_HORIZONS,
                  held_bars: int = HELD_BARS,
                  tol_pct: float = HELD_TOL_PCT,
                  fee: float = RET_FEE) -> Tuple[List[dict], Dict]:
    """算整张表 3。每根 K 线一行。

    `bar_events` 是表 1 汇总出来的那几列（{ts: {event, on_line, ...}}，第 4 步的产物）。
    `event_rows` 是表 2.5 的明细行，用来取 `held` 的参照价（事件判定当时用的线价）——
    那是**判断依据**，不是「同一根线现在的价位」，两者不是一回事。
    """
    rows: List[dict] = []
    stats: Dict = {"bars_in": len(bars), "rows": 0}

    # 参照价：按 (ts, line_key) 索引表 2.5 的 line_price
    ref: Dict[Tuple[int, str], float] = {}
    for e in (event_rows or []):
        ref[(int(e["ts"]), str(e["line_key"]))] = (
            None if e.get("line_price") is None else float(e["line_price"]))

    rets = forward_returns(bars, horizons=horizons, fee=fee)

    c_by_ts = {int(b["ts"]): b.get("c") for b in bars}
    n_held1 = n_held0 = n_held_null = 0
    n_ref_missing = 0
    n_ret_null = {int(k): 0 for k in horizons}

    for b in bars:
        ts = int(b["ts"])
        per = rets.get(ts, {})
        r: Dict = {"symbol": symbol, "interval_ms": int(interval_ms), "ts": ts}
        for k in horizons:
            v = per.get(int(k))
            r["ret%d" % int(k)] = v
            if v is None:
                n_ret_null[int(k)] += 1

        ev = bar_events.get(ts)
        r["held"] = None
        r["held_line"] = None
        r["held_ref_price"] = None
        r["held_side"] = None
        if ev is not None:
            line_key = ev.get("on_line")
            event_name = ev.get("event")
            p = ref.get((ts, str(line_key))) if line_key is not None else None
            if p is None:
                n_ref_missing += 1
            else:
                side = held_side_for(event_name, c_by_ts.get(ts), p, tol_pct)
                h = (None if side is None
                     else held_of(bars, ts, p, side, held_bars, tol_pct))
                r["held"] = h
                r["held_line"] = line_key
                r["held_ref_price"] = p
                r["held_side"] = side
                if h is None:
                    n_held_null += 1
                elif h == 1:
                    n_held1 += 1
                else:
                    n_held0 += 1
        rows.append(r)

    stats["rows"] = len(rows)
    for k in horizons:
        stats["ret%d_null" % int(k)] = n_ret_null[int(k)]
    stats["held_bars"] = int(held_bars)
    stats["held_tol_pct"] = float(tol_pct)
    stats["bars_with_event"] = sum(1 for b in bars if int(b["ts"]) in bar_events)
    stats["held_1"] = n_held1
    stats["held_0"] = n_held0
    stats["held_null"] = n_held_null
    stats["held_ref_missing"] = n_ref_missing
    return rows, stats


def assert_no_lookahead(rows: Sequence[dict], bars: Sequence[dict]) -> None:
    """事后层也可以被检查：它**必须**只看之后的 K 线。

    查两件事：
      1. 每根都在表里（没漏根）
      2. ret_k 与自己独立重算的 c[t+k]/c[t]-1 一致 —— 这是「没用错根」的物证

    第 2 条不是重复计算：它保证「算 ret 的时候取的确实是 t+k 那根」，
    而不是「t 那根」或「t+k-1 那根」。这类错法不会报错，只会让收益整体偏小。
    """
    if not rows:
        raise ForwardLookaheadError("表 3 是空的")
    ts_list = [int(b["ts"]) for b in bars]
    if ts_list != sorted(ts_list):
        raise ForwardLookaheadError("bars 没有按 ts 升序")
    if len(set(ts_list)) != len(ts_list):
        raise ForwardLookaheadError("bars 里有重复 ts")
    got = [int(r["ts"]) for r in rows]
    if got != ts_list:
        raise ForwardLookaheadError("表 3 的根与输入不一致（%d vs %d）"
                                    % (len(got), len(ts_list)))
    idx = {ts: i for i, ts in enumerate(ts_list)}
    for r in rows:
        i = idx[int(r["ts"])]
        for k in FORWARD_HORIZONS:
            v = r.get("ret%d" % k)
            if i + k >= len(bars):
                if v is not None:
                    raise ForwardLookaheadError(
                        "ts=%s 后面不足 %d 根，ret%d 应为空" % (r["ts"], k, k))
                continue
            c0 = bars[i].get("c")
            ck = bars[i + k].get("c")
            if c0 is None or ck is None or float(c0) == 0.0:
                continue
            want = float(ck) / float(c0) - 1.0
            if v is None:
                raise ForwardLookaheadError(
                    "ts=%s 的 ret%d 该有值却空了" % (r["ts"], k))
            if abs(float(v) - want) > 1e-9:
                raise ForwardLookaheadError(
                    "ts=%s 的 ret%d = %r，按 c[t+%d]/c[t]-1 应为 %r"
                    % (r["ts"], k, v, k, want))


def stats_by_held(rows: Sequence[dict]) -> Dict[str, int]:
    """{held 的取值: 条数}，供探针打印。"""
    out: Dict[str, int] = {}
    for r in rows:
        k = "null" if r.get("held") is None else str(r["held"])
        out[k] = out.get(k, 0) + 1
    return out
