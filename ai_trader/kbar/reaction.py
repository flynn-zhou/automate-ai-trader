# -*- coding: utf-8 -*-
"""K 线演化 · 第 3 步：反应层（每根 K 线 × 每条**碰到的**观察线）。

设计稿 §12 第 3 步：「反应层：每根 × 每条碰到的线 → kbar_reaction（从表 0 派生）」。

这一步第一次产出「判断」的雏形 —— 但仍然不是判断。它只回答：
**这根 K 线有没有碰到这条线？从哪边来的？穿进去多深？收回来多少？收在线的哪一侧？**
它**不回答**「这条线撑住了没有」，那是「支撑」这个断言，本模块不碰。

不变量：反应只能用「本根开盘前就已经知道」的线值
------------------------------------------------
这是本模块最容易被做错、而且做错了完全看不出来的地方。

同周期线（`ema20@15m`、`donchian_hi@15m`、`prior_high@15m`…）在 t 根的值，
是由 t 根的**收盘价**算出来的，`line_ready_ms = t的收盘 = t+15m`。
若拿这个值去判断「t 根里价格有没有碰到它」，等于用收盘之后才知道的数字
去解释这根 K 线内部的走势 —— 就是偷未来。

实测这个错法的影响（2026-11-08 重建后，17,654 根 × 17 条线 = 300,118 个 (根, 线) 对）：

    朴素取法（用同一 ts 那行）        触碰 24,246 次
    正确取法（上一根那一行）          触碰 24,075 次
    判定翻转 3,301 处，其中
      朴素多报的「假触碰」 1,736 次   ← 占朴素触碰的 7.2%
      朴素漏报             1,565 次
      14,592 行（61%）会取到另一条线的价格

注意「假触碰」和「漏报」是两个方向，必须分开报：
  假触碰 = 朴素说碰到了、实际没碰到（= 用未来的线价解释已经走完的走势）
  漏报   = 朴素说没碰到、实际碰到了（同样因为拿了错的线价）
只报其中一个会让影响看起来像单向的。

（历史上还有一版中间态「ready <= 本根开盘」，触碰 24,577 次。它修掉了偷未来，
  但没有修掉往前带，所以最终值是 24,003（当时 17,584 根）——
  差的 574 行全是消失后仍被使用的 range 线。重建到 17,654 根后是 24,075。）

所以取法必须是：**取「上一根 K 线（t - interval）」那一行**，而不是对 ready 做二分。
1h 线在 15m 根上天然满足（那行的 1h 值在 t 的开盘时刻早已收盘）；
同周期线则自动退回到上一根收盘时算出的值。
第 1 步落 `line_ready_ms` 仍然是物证（下面的断言用它兜底），但**判据不是它**。

第二条不变量：「上一根没有，就是不存在」—— 不许往前带
------------------------------------------------------
第一版用的是「最近一个就绪值」。交叉核对时发现它会**无限期往前带**：
2026-08-05 01:00 起区间演化转 `PRE`（四线全空），可 05:15 的 K 线仍在拿
02:00 那条 `range_key` 判断「碰到」—— 用一条消失了 3 小时的线做反应。

正确规则：**取「上一根 K 线」那一行；那根没有这条线，就是那根上它不存在。**
依据是表 0 的性质：它对每条**存在的**线在**每根** K 线上都无条件落一行，
所以「缺行」与「不存在」是同一件事。

而「上一根那一行」正好等价于「本根开盘前已就绪」：15m 根 `T - interval` 的收盘
时刻**正好就是** `T` 的开盘时刻，那一行用到的信息与本根开盘时完全一样。
两条不变量因此自动同时成立，且不需要再对 ready 做二分。

口径（设计稿 §7，逐个对上；没定的显式留空，见 docs/K线演化-待确定清单.md）
--------------------------------------------------------------------------
    side           o > p → from_above，否则 from_below          （D-6 待定，临时用开盘价）
    pen_depth_pct  from_above: (p - l)/p     from_below: (h - p)/p   ← 是**比例**不是百分数
    recover_pct    from_above: (c - l)/p     from_below: (h - c)/p   ← 同上
    close_side     c > p → above，c < p → below，c == p → at     （D-7 待定）
    dist_pct       (c - p)/p * 100                              ← **这个才是百分数**
    touch          l <= p <= h（无阈值，含边界；D-5 待定）
    dwell_bars     留空（「贴着」需要距离阈值，D-8）
    nth_touch      **按段重置**：键是 (fam, seg, line_key)，换段从 1 重数；
                   段缺失的根留空并计 `nth_touch:no_segment`（D-9 的残余分歧见清单）

注意 `pen_depth_pct` / `recover_pct` 叫 `_pct` 但设计稿给的公式**不带 ×100**，
与 `body_pct` 一致（都是 0~1 的比例）；只有 `dist_pct` 是百分数。
这个不对称会静默坑下游，所以写在这里也写进文档。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

# ---- 口径常量（改这里必须同步 docs/K线演化-实现记录.md + 待确定清单）------------
TOUCH_TOL_PCT = 0.0        # 触碰容差（占线价的百分比）。0 = 无阈值，区间覆盖即算碰到（D-5）

SIDE_FROM_ABOVE = "from_above"
SIDE_FROM_BELOW = "from_below"
CLOSE_ABOVE = "above"
CLOSE_BELOW = "below"
CLOSE_AT = "at"            # 收盘价正好等于线价（实测 174 条），不塞进任何一边（D-7）


def as_of(series: Sequence[Tuple], ts: int) -> Optional[Tuple]:
    """在按 ready 升序的序列里，找最后一个 `ready <= ts` 的元素（二分）。

    series 元素: (line_ready_ms, src_ts, line_price)
    返回该元素，或 None（本根开盘前这条线还不存在）。

    > **注意**：这个「找最后一个就绪值」的写法本身没错，但它**会无限期往前带** ——
    > 一条线消失之后（例如区间演化转 PRE、四线全空），它仍会取到消失之前的那个值。
    > 所以 `build_reactions()` **不用这个函数**，改用「上一根那一行」的严格取法
    > （见 `pick_prev`）。保留它作为口径说明与独立核对工具。
    """
    lo, hi, ans = 0, len(series) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            ans = series[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def pick_prev(line_rows: Dict[int, Tuple], prev_ts: int) -> Optional[Tuple]:
    """取「上一根 K 线」那一行，前提是那根上这条线确实存在。

    为什么是「上一根」而不是「最近一个就绪值」：
        表 0 对**每条存在的线**在**每根** K 线上都无条件落一行。
        所以某根没有行 ⟺ 那根这条线不存在（例如区间演化转 PRE，四线全空）。
        用「最近就绪值」会把一条已经消失的线往前带 —— 实测 2026-08-05 01:00 起
        区间演化转 PRE，而 05:15 的 K 线仍在拿 02:00 那条线判断「碰到」，
        等于用一条消失了 3 小时的线做反应。

    为什么「上一根」正好等价于「本根开盘前已就绪」：
        15m 根 `T - interval` 的收盘时刻**正好就是** `T` 的开盘时刻，
        所以那一行用到的信息与本根开盘时可得的信息完全相同。
        1h 线也一样：`T - 15m` 收盘时该 1h 行若已收盘，那一行里就有它。
    """
    return line_rows.get(int(prev_ts))


def side_of(o: float, p: float) -> str:
    """从哪边来。用本根开盘价与线价比（只用本根自己的信息）。"""
    return SIDE_FROM_ABOVE if o > p else SIDE_FROM_BELOW


def close_side_of(c: float, p: float) -> str:
    if c > p:
        return CLOSE_ABOVE
    if c < p:
        return CLOSE_BELOW
    return CLOSE_AT


def is_touch(l: float, h: float, p: float, tol_pct: float = TOUCH_TOL_PCT) -> bool:
    """这根的价格区间有没有覆盖线价。tol_pct>0 时把「差一点」也算进来（默认 0）。"""
    if tol_pct:
        band = p * tol_pct / 100.0
        return (l - band) <= p <= (h + band)
    return l <= p <= h


def pen_depth_pct(p: float, l: float, h: float, side: str) -> float:
    """穿透深度（比例，0~1）。从上方来就看跌破多深，从下方来就看冲过多高。"""
    if side == SIDE_FROM_ABOVE:
        return (p - l) / p
    return (h - p) / p


def recover_pct(p: float, l: float, h: float, c: float, side: str) -> float:
    """收回幅度（比例，0~1）。从极值往回走到收盘的那一段，除以线价。

    可以大于 pen_depth_pct —— 收盘站到线的另一侧时就会这样，
    那正是「穿进去又收回来并且站过去了」这种最值得看的情形，不是错误。
    """
    if side == SIDE_FROM_ABOVE:
        return (c - l) / p
    return (h - c) / p


def dist_pct(c: float, p: float) -> float:
    """收盘价相对线价的距离（**百分数**）。正 = 收盘在线上方，负 = 在线下方。"""
    return (c - p) / p * 100.0


def build_reactions(symbol: str, interval_ms: int,
                    bars: Sequence[dict],
                    line_rows: Dict[str, Dict[int, Tuple]],
                    line_meta: Optional[Dict[str, Tuple[str, int]]] = None,
                    seg_by_ts: Optional[Dict[int, Tuple]] = None) -> Tuple[List[dict], Dict]:
    """算反应层。

    bars:       按 ts 升序的 [{ts, o, h, l, c, vol_rel, taker_buy_ratio}]
                （o/h/l/c 与 vol_rel/taker 来自表 1；表 2 要带着它们才好独立读懂）
    line_rows:  {line_key: {bar_ts: (line_price, line_ready_ms, src_ts)}}
                —— 表 0 的原始形状。**必须按 bar_ts 索引**，因为反应只能用
                「上一根那一行」（见 `pick_prev` 里为什么）。
    line_meta:  {line_key: (line_kind, line_period_ms)}
    seg_by_ts:  {ts: (fam, seg)}。给定时才算 `nth_touch`；不给就留空并说明原因。

    返回 (rows, stats)。stats 必须能说明「哪些 (根,线) 对没进表、为什么」。
    """
    stats = {
        "bars_in": len(bars), "rows": 0, "bars_without_touch": 0,
        "pairs_evaluated": 0, "touch": 0,
        "skipped": {}, "by_kind": {}, "lines": len(line_rows),
        "nth_touch": "not_computed" if seg_by_ts is None else "by_segment",
    }
    if not bars or not line_rows:
        return [], stats

    keys = sorted(line_rows)
    meta = line_meta or {}
    rows: List[dict] = []
    iv = int(interval_ms)
    prev_ts = None

    for b in bars:
        ts = int(b["ts"])
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        n_touch_here = 0
        for key in keys:
            stats["pairs_evaluated"] += 1
            if prev_ts is None:
                # 第一根之前没有「上一根」，这条线的值无从得知
                stats["skipped"]["line_not_ready"] = \
                    stats["skipped"].get("line_not_ready", 0) + 1
                continue
            val = pick_prev(line_rows[key], prev_ts)
            if val is None:
                # 上一根没有这条线 ⟺ 那根上它不存在（区间演化转 PRE 等），
                # **不许**拿更早的值往前带
                stats["skipped"]["line_absent_at_prev_bar"] = \
                    stats["skipped"].get("line_absent_at_prev_bar", 0) + 1
                continue
            p, ready, src_ts = float(val[0]), int(val[1]), int(val[2])
            if p is None or p <= 0:
                stats["skipped"]["price_bad"] = stats["skipped"].get("price_bad", 0) + 1
                continue
            if not is_touch(l, h, p):
                continue                      # 没碰到不进表 2；事实在表 0
            sd = side_of(o, p)
            kind, per = meta.get(key, ("", iv))
            rows.append({
                "symbol": symbol, "interval_ms": iv, "ts": ts,
                "line_key": key, "line_kind": kind, "line_period_ms": int(per),
                "line_price": p,
                "dist_pct": dist_pct(c, p),
                "side": sd,
                "pen_depth_pct": pen_depth_pct(p, l, h, sd),
                "recover_pct": recover_pct(p, l, h, c, sd),
                "close_side": close_side_of(c, p),
                "dwell_bars": None,           # D-8
                "vol_rel": b.get("vol_rel"),
                "taker_buy_ratio": b.get("taker_buy_ratio"),
                "nth_touch": None,            # 下面按段填
                # 内部用，落库时丢掉：证明这个反应用的是开盘前就有的值
                "_line_ready_ms": ready, "_src_ts": src_ts,
            })
            n_touch_here += 1
            stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
        stats["touch"] += n_touch_here
        if n_touch_here == 0:
            stats["bars_without_touch"] += 1
        prev_ts = ts

    stats["rows"] = len(rows)
    if seg_by_ts is not None:
        _assign_nth_touch(rows, seg_by_ts, stats)
    return rows, stats


def _assign_nth_touch(rows: List[dict], seg_by_ts: Dict[int, Tuple],
                      stats: Dict) -> None:
    """填 `nth_touch`：设计稿 §6 表 2「第几次触碰（按区间演化换段重置）」。

    读法（设计稿字面）：**每根 K 线自己就带着 `(fam, seg)`**（来自表 1），
    所以计数键是 `(段, line_key)` —— 同一段里同一条线第几次被碰到，换段从 1 重数。
    不是「只有区间四线才有段」：一根 K 线只会落在某一个段里，它的段对所有线都一样。

    段缺失的根（range_evolution 起点之前）留空并计数 `nth_touch:no_segment`，
    **不假装从 1 开始** —— 那不是第一次，是不知道第几次。
    """
    counters: Dict[Tuple, int] = {}
    for r in sorted(rows, key=lambda x: (int(x["ts"]), x["line_key"])):
        seg = seg_by_ts.get(int(r["ts"]))
        if seg is None:
            r["nth_touch"] = None
            stats["skipped"]["nth_touch:no_segment"] = \
                stats["skipped"].get("nth_touch:no_segment", 0) + 1
            continue
        k = (seg, r["line_key"])
        counters[k] = counters.get(k, 0) + 1
        r["nth_touch"] = counters[k]


def assert_no_lookahead(rows: Sequence[dict], interval_ms: int) -> None:
    """自检：每个反应的线值都必须是本根开盘前就绪的。

    写成断言而不是注释，因为「看起来没问题」的东西一旦错了完全看不出来。
    """
    for r in rows:
        ready = r.get("_line_ready_ms")
        if ready is None:
            continue
        if int(ready) > int(r["ts"]):
            raise RuntimeError(
                "偷未来：ts=%s 的 %s 用了 ready=%s 的线值"
                % (r["ts"], r["line_key"], ready))


def touch_counts_by_bar(rows: Sequence[dict]) -> Dict[int, int]:
    """每根碰到几条线 —— 表 1 的 n_touch 就是它，从反应层数出来而不是另外算。"""
    out: Dict[int, int] = {}
    for r in rows:
        out[int(r["ts"])] = out.get(int(r["ts"]), 0) + 1
    return out
