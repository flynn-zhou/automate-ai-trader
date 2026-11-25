# -*- coding: utf-8 -*-
"""K 线演化 · 第 2 步：事实层（每根 K 线的结构 / 量能 / 内路径）。

设计稿 §12 第 2 步：「每根 K 线算结构 + 量能 + 内路径，落 kbar_evolution」。
这一步只产出**可测量的数**，不产出任何判断（不判断好坏、不判断是不是支撑）。

口径全部来自设计稿 §7，逐个对上；设计稿没定的东西，这里一律**不自己发明**：
留空 + 记一条跳过原因，由人决定。已经确认「当前数据算不出来」的也留空，见下。

字段与数据来源（这张对应关系是这一步最重要的东西）
----------------------------------------------------
    结构   o/h/l/c/dir/body_pct/up_wick_pct/dn_wick_pct/close_pos
           ← K 线本身，任何周期都算得出，不依赖第二个数据源
    量能   vol/vol_rel/vol_slot_rel/taker_buy_ratio
           ← vol 与主动买量（b）。注意 `market.db` 的 b/s 是**买量/卖量**
             （实测 b+s==v 精确成立），`backtest.db` 的 b 是「主动买量(base)」、
             s 其实是「主动买报价额(quote)」——两者字段同名但含义不同，
             所以只用 b/v 算 taker_buy_ratio，绝不用 backtest 的 s 当卖量。
    内路径 conc/t_low_min/t_high_min
           ← 3s 原子桶。生产只保留 168 小时（config.StoreCfg.tick_retention_hours），
             所以只有最近约 7 天的 K 线能算出内路径，更早的一律留空并计数。
    算不出 max_tick_share
           ← 需要**逐笔**成交。现有数据只有 3s 桶（桶内只留 n 笔数与买卖量），
             没有逐笔明细，所以这个字段当前不可算，留空并计数，不拿 conc 冒充。
    不知道 zone / path_shape 怎么定时**不许猜**
           ← 设计稿 §7 没有定义这两个口径。zone 这里用「收盘价落在四线切出的
             哪个价格区间」这个纯排序事实来填（不含阈值）；path_shape 是分类
             （需要词表 + 阈值 = 判断标准），按设计稿「第 5 步之前不碰判断标准」
             留空并计数。

不变量
------
  line_ready_ms / range_ts_h 的取法必须与第 1 步一致：只用**已经收盘**的 1h 区间行。
  本模块直接复用 `kbar.lines.range_lines_at()`，不另写一套二分。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .lines import RANGE_PERIOD_MS, range_lines_at, range_ts_list

# ---- 口径常量（改这里必须同步 docs/K线演化-实现记录.md）--------------------------
TICK_INTERVAL_MS = 3_000          # 3s 原子桶：内路径唯一的来源
VOL_REL_N = 20                    # §7 vol_rel = vol / median(vol, 前 20 根)
SLOTS_PER_DAY = 96                # 【15m 的值】一天的槽数。别直接用它，用 slots_per_day_for()
VOL_SLOT_MIN_SAMPLES = 20         # 「同槽历史量」至少要有几根才敢算中位数
TICKS_PER_BAR = 900_000 // TICK_INTERVAL_MS   # 【15m 的值】一根应有几根 3s。用 ticks_per_bar_for()

MS_PER_DAY = 86_400_000


def slots_per_day_for(interval_ms: int) -> int:
    """这个周期的「一天有几根」。**算术事实，不是判断。**

    ⚠ 曾经这里是写死的 96（= 24*60/15，只有 15m 对）。
    而 `build.py` 调 `bar_facts()` 时**没传** `slots_per_day`，
    于是永远落到 96。换到 5m 时 `(ts // 300000) % 96` 只覆盖 8 小时，
    三个不同时段被当成同一个「同时段」，`vol_slot_rel` 静默变垃圾 ——
    而它恰好是留出集里最强的维度之一（z −5.31 / +5.26）。
    不报错、不崩、数就是错的，这是最难发现的那种。

    现在一律按周期推：15m→96（与旧值相同，故 15m 结果不变），5m→288。
    """
    iv = int(interval_ms)
    if iv <= 0:
        raise ValueError("interval_ms 必须为正")
    slots = MS_PER_DAY // iv
    if slots * iv != MS_PER_DAY:
        raise ValueError(
            "周期 %d ms 不能整除一天（%d 槽，余 %d ms）—— "
            "这种周期没有稳定的「同时段」概念，先想清楚再支持"
            % (iv, slots, MS_PER_DAY - slots * iv))
    return int(slots)


def ticks_per_bar_for(interval_ms: int) -> int:
    """这个周期的一根 K 线里应有几根 3s 原子桶。同样是算术，不是判断。

    15m→300（与旧值相同），5m→100。
    """
    iv = int(interval_ms)
    if iv <= 0:
        raise ValueError("interval_ms 必须为正")
    return int(iv // TICK_INTERVAL_MS)

# 四线的**角色**映射。方向决定取哪一条 —— 这不是挑数据，是数据本身只有一条：
#   DOWN 段 hard_dn 恒为 NULL（实测 1303 行里 0 条有值），价格序 K < P < S < R
#   UP   段 hard_up 恒为 NULL（实测 902 行里 0 条有值），价格序 R < S < P < K
#   PRE  四线全空（实测 60 行全空）—— 未成形，无从谈区间
# 把 soft_up / soft_dn 不加区分地都叫 S 会退化成「at_S」，等于把两条线合并，白算。
ROLE_OF_LINE = {
    "DOWN": {"S": "soft_up", "R": "hard_up", "P": "soft_dn", "K": "key"},
    "UP": {"S": "soft_dn", "R": "hard_dn", "P": "soft_up", "K": "key"},
}


def levels_by_role(rng_row: Optional[dict]) -> Dict[str, Optional[float]]:
    """把 range_evolution 的一行换成 {角色: 价格}（S/R/P/K）。PRE 等方向返回空。"""
    if not rng_row:
        return {}
    d = (rng_row.get("direction") or "").upper()
    m = ROLE_OF_LINE.get(d)
    if not m:
        return {}
    return {role: _f(rng_row.get(col)) for role, col in m.items()}


def _f(x) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def median(xs: Sequence[float]) -> Optional[float]:
    """中位数（自己实现，避免依赖统计库对空序列的行为差异）。"""
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (float(s[mid - 1]) + float(s[mid])) / 2.0


# ---------------------------------------------------------------- 结构
def structure(o, h, l, c) -> Dict[str, Optional[float]]:
    """§7 的结构口径。h==l（一字板/无振幅）时全部返回 None —— 分母是 0。"""
    o, h, l, c = _f(o), _f(h), _f(l), _f(c)
    if None in (o, h, l, c):
        return {"bad": "null_ohlc"}
    rng = h - l
    if rng <= 0:
        return {"bad": "flat_bar"}
    return {
        "dir": 1 if c > o else (-1 if c < o else 0),
        "body_pct": abs(c - o) / rng,
        "up_wick_pct": (h - max(o, c)) / rng,
        "dn_wick_pct": (min(o, c) - l) / rng,
        "close_pos": (c - l) / rng,
    }


# ---------------------------------------------------------------- 量能
def vol_rel(vols: Sequence[Optional[float]], i: int,
            n: int = VOL_REL_N) -> Optional[float]:
    """§7：vol / median(vol, 前 n 根)。不含当根 —— 含了就自己跟自己比。

    「前 n 根里有任何一根没有量」就返回 None：用一个残缺窗口算出的中位量会
    系统性偏小，比值偏大，看起来像「突然放量」，正好制造假信号。
    """
    if i < n:
        return None
    cur = vols[i]
    if cur is None:
        return None
    window = vols[i - n:i]
    if any(x is None for x in window):
        return None
    base = median([float(x) for x in window])
    if not base or base <= 0:
        return None
    return float(cur) / base


def slot_of(ts: int, interval_ms: int, slots_per_day: int = SLOTS_PER_DAY) -> int:
    """把一根 K 映射到「一天里的第几个槽」。15m -> 0..95，1h -> 0..23。"""
    return int((int(ts) // int(interval_ms)) % slots_per_day)


def vol_slot_rel(vol: Optional[float], slot: int, history: Dict[int, List[float]],
                 min_samples: int = VOL_SLOT_MIN_SAMPLES) -> Optional[float]:
    """§7：vol / median(同槽的历史量)。只用**这根之前**的同槽量，不看未来。

    字段名叫 vol_slot_rel（时段相对），所以按「同一个日内槽」归一，而不是笼统的
    「同一个小时」。§7 正文写的是「同小时」，名字写的是 slot —— 15m 一小时后有
    4 个槽，用同槽才真的扣掉了日内效应；此处按字段名实现，并在文档里写明这个分歧。
    """
    if vol is None:
        return None
    past = history.get(slot) or []
    if len(past) < min_samples:
        return None
    base = median(past)
    if not base or base <= 0:
        return None
    return float(vol) / base


# ---------------------------------------------------------------- 内路径（3s）
def path_facts(ticks: Sequence[dict], bar_ts: int, bar_vol: Optional[float],
               interval_ms: int) -> Dict[str, Optional[float]]:
    """从一根 K 内的 3s 桶算内路径。

    参数 ticks: [{ts, v, o, h, l, c}]，按 ts 升序，必须**已经**落在 [bar_ts, bar_ts+interval)
    返回 conc / t_low_min / t_high_min / n_ticks / n_bad。

    conc 能不能算，判据是**量自洽**而不是「桶数够不够 300」：
      total = sum(3s 的量)。若 total 与这根 K 线的总量相等（相对差 <= 1e-6），
      说明这根 K 的量本来就是这些桶加出来的，分子分母出自同一组桶，
      `max/total` 就是精确的「最大桶占比」，**缺几个桶不影响比值**。
      反之若两边对不上，说明 3s 不是这根 K 的完整分解（量来自别的来源），
      此时比值会被系统性压低，看起来像「很均匀」，正好把要抓的冲击抹掉 -> 不算。
    实测依据：3s 覆盖窗内 641 根里，有 156 根存在 1~3 个缺桶，但它们的
    sum(3s) 与 K 线量**完全相等**；另有 77 根桶齐却对不上量。按桶数一刀切
    会把那 156 根误判为「算不出」，按量自洽判据才分得清这两种情况。
    """
    out: Dict[str, Optional[float]] = {
        "conc": None, "t_low_min": None, "t_high_min": None,
        "n_ticks": len(ticks), "n_bad": None,
    }
    if not ticks:
        out["n_bad"] = "no_ticks"
        return out
    expect = int(interval_ms) // TICK_INTERVAL_MS
    vols = [_f(t.get("v")) or 0.0 for t in ticks]
    total = sum(vols)
    if total <= 0:
        out["n_bad"] = "ticks_zero_vol"
        return out

    # 最低/最高价出现在第几分钟（用桶起点相对本根起点的偏移）
    lo_i = min(range(len(ticks)), key=lambda k: _f(ticks[k].get("l")) or float("inf"))
    hi_i = max(range(len(ticks)), key=lambda k: _f(ticks[k].get("h")) or float("-inf"))
    out["t_low_min"] = (int(ticks[lo_i]["ts"]) - int(bar_ts)) // 60_000
    out["t_high_min"] = (int(ticks[hi_i]["ts"]) - int(bar_ts)) // 60_000

    bv = _f(bar_vol)
    if bv is None or bv <= 0:
        out["n_bad"] = "bar_vol_missing"
        return out
    if abs(total - bv) / bv > 1e-6:
        # 3s 不是这根 K 的完整分解，比值不可信
        out["n_bad"] = "ticks_vol_mismatch"
        return out
    out["conc"] = max(vols) / bv
    if len(ticks) < expect:
        # 缺桶但量自洽：conc 仍然精确，只把「不完整」记下来供报表统计
        out["n_bad"] = "ticks_incomplete_but_consistent"
    return out


# ---------------------------------------------------------------- 区间演化位置
SAME_PRICE_REL = 1e-9      # 两条线的价格相对差小于这个就算「同价重合」


def zone_of(close: Optional[float], roles: Dict[str, Optional[float]]) -> Optional[str]:
    """收盘价落在四线切出的哪个价格区间。

    纯排序事实，不含任何阈值（同价判定只是浮点容差，不是业务阈值）：
    把有值的线按价格升序排，看 close 落在哪两条之间，用角色字母命名区间。

    两种真实存在、必须处理对的情况：
      1. 多条线同价重合（实测 DOWN 段有 186 行 hard_up == soft_up）。若不合并，
         重合点会被当成两个不同区间，切出零宽度区间，名字像 "P-R" 而其实 S 就在 R 上。
         这里把同价的角色合并成一个节点，标签写成 "RS"，于是区间名是 "P-RS"。
      2. 段首线还没画全（实测 104 行 soft_up/hard_up 同时为空）。此时只在**有值的**
         线之间命名 —— 不假装缺席的线在那儿。
    """
    c = _f(close)
    if c is None:
        return None
    pts = [(p, role) for role in ("S", "R", "P", "K")
           for p in [roles.get(role)] if p is not None]
    if not pts:
        return None
    pts.sort(key=lambda x: x[0])

    # 同价合并：价格相同的角色并到一个节点，标签按 S,R,P,K 顺序拼（不含阈值判断）
    order = {r: i for i, r in enumerate(("S", "R", "P", "K"))}
    merged: List[tuple] = []
    for price, role in pts:
        if merged and abs(price - merged[-1][0]) <= SAME_PRICE_REL * max(
                1.0, abs(price), abs(merged[-1][0])):
            roles_here = merged[-1][1] + [role]
            merged[-1] = (merged[-1][0], roles_here)
        else:
            merged.append((price, [role]))
    nodes = [(p, "".join(sorted(rs, key=lambda r: order[r]))) for p, rs in merged]

    if c < nodes[0][0]:
        return "below_%s" % nodes[0][1]
    if c > nodes[-1][0]:
        return "above_%s" % nodes[-1][1]
    for i in range(len(nodes) - 1):
        lo_p, lo_r = nodes[i]
        hi_p, hi_r = nodes[i + 1]
        if lo_p <= c <= hi_p:
            return "%s-%s" % (lo_r, hi_r)
    return None


# ---------------------------------------------------------------- 组装
def bar_facts(bars: Sequence[dict], interval_ms: int,
              range_rows: Optional[Sequence[dict]] = None,
              ticks_by_bar: Optional[Dict[int, List[dict]]] = None,
              line_counts: Optional[Dict[int, int]] = None,
              slots_per_day: Optional[int] = None,
              symbol: Optional[str] = None) -> tuple:
    """把一批 K 线算成 kbar_evolution 的行。

    bars: [{ts, o, h, l, c, v, buy_vol}]，按 ts 升序
    range_rows: range_evolution 的行（1h 维度），可为 None
    ticks_by_bar: {bar_ts: [3s 桶...]}，只在 3s 覆盖窗口内才有
    line_counts: {bar_ts: 该根上观察线条数}，来自第 1 步的 kbar_line

    返回 (rows, stats)。stats 里必须能说清「每一根为什么没有某个字段」。
    """
    stats = {"bars_in": len(bars), "rows": 0, "skipped": {}, "no_ticks": 0,
             "ticks_incomplete": 0, "ticks_vol_mismatch": 0, "bar_vol_missing": 0,
             "ticks_zero_vol": 0,
             "max_tick_share_undefined": 0, "path_shape_undefined": 0}
    if not bars:
        return [], stats

    # ⚠ 不传就按**这个周期自己的**一天几根推，绝不退回 96。
    #    退回 96 在 5m 下只覆盖 8 小时，三个时段混成一个槽，`vol_slot_rel`
    #    会静默变成垃圾（不报错、不崩）。
    slots = int(slots_per_day) if slots_per_day else slots_per_day_for(interval_ms)
    if slots != slots_per_day_for(interval_ms):
        raise ValueError(
            "传进来的 slots_per_day=%d 与周期 %d ms 推出来的 %d 不符 —— "
            "「同时段」的口径不能手工改，改了 vol_slot_rel 就不可比"
            % (slots, int(interval_ms), slots_per_day_for(interval_ms)))
    rts_all = range_ts_list(range_rows) if range_rows else []
    # seg_idx：该 1h 行在它自己的 (fam, seg) 段内的序号（由行序列算出，不靠库里的列）
    seg_idx_map: Dict[int, int] = {}
    if range_rows:
        seen: Dict[tuple, int] = {}
        for r in range_rows:
            k = (r.get("family"), r.get("segment"))
            n = seen.get(k, 0)
            seg_idx_map[int(r["ts"])] = n
            seen[k] = n + 1

    vols: List[Optional[float]] = [(_f(b.get("v")) if b.get("v") is not None else None)
                                   for b in bars]
    slot_hist: Dict[int, List[float]] = {}
    rows: List[dict] = []

    for i, b in enumerate(bars):
        ts = int(b["ts"])
        close = _f(b.get("c"))
        st = structure(b.get("o"), b.get("h"), b.get("l"), b.get("c"))
        if st.get("bad"):
            stats["skipped"]["%s" % st["bad"]] = stats["skipped"].get("%s" % st["bad"], 0) + 1
            continue

        slot = slot_of(ts, interval_ms, slots)
        v = vols[i]
        if v is None:
            # 没有量（例如只有结构的 CSV 段）：它既不能算比值，也不能进滚动历史 ——
            # 塞 0 会把中位量拉到 0，之后所有比值都失效，而且不报错。
            stats["skipped"]["vol:no_source"] = stats["skipped"].get("vol:no_source", 0) + 1
        buy = _f(b.get("buy_vol"))
        tbr = None
        if v is not None and v > 0 and buy is not None:
            tbr = buy / v
        elif v is not None and v > 0:
            stats["skipped"]["taker_buy_ratio:no_buy_vol"] = \
                stats["skipped"].get("taker_buy_ratio:no_buy_vol", 0) + 1

        vr = vol_rel(vols, i)
        if vr is None and v is not None:
            stats["skipped"]["vol_rel:warmup"] = stats["skipped"].get("vol_rel:warmup", 0) + 1
        vsr = vol_slot_rel(v, slot, slot_hist)
        if vsr is None and v is not None:
            stats["skipped"]["vol_slot_rel:warmup"] = \
                stats["skipped"].get("vol_slot_rel:warmup", 0) + 1
        if v is not None:
            slot_hist.setdefault(slot, []).append(v)

        # ---- 区间演化位置（只用已收盘的 1h 行，与第 1 步同一套二分）
        rng_row = None
        if range_rows:
            rng_row, _lines = range_lines_at(range_rows, ts + interval_ms, rts_all)
        if rng_row is None:
            stats["skipped"]["range:not_closed"] = stats["skipped"].get("range:not_closed", 0) + 1

        # ---- 内路径
        ticks = (ticks_by_bar or {}).get(ts) or []
        pf = path_facts(ticks, ts, v, interval_ms)
        nb = pf["n_bad"]
        if nb == "no_ticks":
            stats["no_ticks"] += 1
        elif nb == "ticks_incomplete_but_consistent":
            stats["ticks_incomplete"] += 1
        elif nb == "ticks_vol_mismatch":
            stats["ticks_vol_mismatch"] += 1
        elif nb == "bar_vol_missing":
            stats["bar_vol_missing"] += 1
        elif nb == "ticks_zero_vol":
            stats["ticks_zero_vol"] += 1
        # max_tick_share 需要逐笔，当前数据没有；path_shape 需要词表+阈值（判断标准）
        stats["max_tick_share_undefined"] += 1
        stats["path_shape_undefined"] += 1

        roles = levels_by_role(rng_row)
        zone = zone_of(close, roles) if roles else None
        if rng_row is not None:
            n_have = sum(1 for v in roles.values() if v is not None)
            if n_have == 0:
                # 有区间行但四线全空（PRE 未成形）：zone 无从谈起，必须计数，
                # 否则「没填 zone」这件事会从报表里消失。
                stats["skipped"]["zone:no_levels"] = \
                    stats["skipped"].get("zone:no_levels", 0) + 1
            elif n_have < len(roles):
                # 段首线还没画全：zone 只在有值的线之间命名，这个状态也要计数，
                # 否则「区间名只跨了部分线」看不出来。
                stats["skipped"]["zone:partial_lines"] = \
                    stats["skipped"].get("zone:partial_lines", 0) + 1

        rows.append({
            "symbol": symbol or b.get("symbol"), "interval_ms": int(interval_ms), "ts": ts,
            "idx": ts // int(interval_ms),
            "range_ts_h": int(rng_row["ts"]) if rng_row else None,
            "fam": rng_row.get("family") if rng_row else None,
            "seg": rng_row.get("segment") if rng_row else None,
            "seg_idx": seg_idx_map.get(int(rng_row["ts"])) if rng_row else None,
            "direction": rng_row.get("direction") if rng_row else None,
            "zone": zone,
            "o": _f(b.get("o")), "h": _f(b.get("h")), "l": _f(b.get("l")),
            "c": close, "dir": st["dir"],
            "body_pct": st["body_pct"], "up_wick_pct": st["up_wick_pct"],
            "dn_wick_pct": st["dn_wick_pct"], "close_pos": st["close_pos"],
            "vol": v, "vol_rel": vr, "vol_slot_rel": vsr, "taker_buy_ratio": tbr,            "conc": pf["conc"],
            "path_shape": None,          # 词表未定，见模块头
            "max_tick_share": None,      # 需逐笔，当前数据没有
            "t_low_min": pf["t_low_min"], "t_high_min": pf["t_high_min"],
            "n_lines": (line_counts or {}).get(ts),
            "n_touch": None, "n_break": None,        # 第 3 步反应层才填
            "event": None, "prev_event": None, "on_line": None,
            "since_event_idx": None, "state": None,  # 第 4 步事件层才填
        })

    stats["rows"] = len(rows)
    return rows, stats
