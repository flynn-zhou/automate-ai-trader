# -*- coding: utf-8 -*-
"""买法三（真版）的信号识别：1h 三次同向带量破区间 → 等 → 两种情况之一。

## 和我之前实现错的东西的区别

我之前实现的（`replay.py` 里的 short/long）是「15m 上引线刺穿 ema20 后被拒」。
那个形态**在任何市场里每天都有好几次**，本身不带方向信息 —— 所以回放出来
止损中位数只有 0.059%（约 38 点），跟用户说的「好几百点」差了整整一个量级。
错在**量纲**：拿 15m 的振幅去给 5m 级别的入场定止损。

这一版按用户口述重写，分三段：

### 第 1 段：闸门（gate）—— 1h 收盘，连续同向、每次带量，破区间

用户原话：「三次同方向区间突破，每次突破带量」「1h 找突破」
判定：1h 收盘价从上方跌破某条 1h 区间线（`range_soft_dn` / `range_p` /
`range_key`），且该 1h 的成交量 ≥ 前 `vol_win` 个小时中位数的 `gate_vol` 倍。
在 `gate_lookback_h` 小时内累计到 `gate_min_breaks` 次 → 闸门打开。

### 第 2 段：等待两种「拒绝」形态（五米级）

用户原话：「2 的情况出现了就开始等待，第一种：出现连续 2 根插针 5m，
或者第二种情况…是反弹空的形式，前面也是放量后，反弹一会开始出现 15m ema20
的拒绝（多根反弹上引线）」

- `case_pin`：连续 `pin_count` 根 5m 都是插针（影线占全幅 ≥ `pin_pct` 且
  绝对点数 ≥ `pin_pts`）
- `case_ema`：连续 `ema_count` 根 15m 都碰上 ema20 又被拒绝
  （上引线 ≥ `ema_wick` 且收盘在 ema20 下方）

### 第 3 段：入场 + 止损

- 入场：拒绝形态之后的第一根「量能 5m」—— 振幅 ≥ `entry_pts` 点
  （用户选的口径：取最近 N 根量能根振幅中位数，默认 350）且
  `vol_rel >= entry_vol`
- 止损：**1h 的振幅 × stop_frac**（用户明确纠正：15m 显然不合适）

## 为什么是纯函数

门槛参数全是估计值。做成纯函数才能在历史数据上把每个参数扫一遍，
而不是先写进执行器再拿真钱验证。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

HOUR_MS = 3600000
F_TS = "ts"

#: 判定向下突破时要看的 1h 区间线（由内到外）。
#: `range_hard_dn` 不在里面：实测它在回填窗口里 0/622 —— 结构上大部分时间不存在，
#: 拿它当必要条件会让闸门永远不开。
BREAK_LINES_DOWN = ("range_soft_dn@1h", "range_p@1h", "range_key@1h")
#: 向上突破的镜像。
BREAK_LINES_UP = ("range_soft_up@1h", "range_hard_up@1h", "range_key@1h")


class GateParams(object):
    """第 1 段：区间破位闸门。"""

    __slots__ = ("min_breaks", "lookback_h", "vol_win", "gate_vol",
                 "direction", "break_lines")

    def __init__(self, min_breaks: int = 3, lookback_h: int = 12,
                 vol_win: int = 24, gate_vol: float = 1.5,
                 direction: str = "down",
                 break_lines: Optional[Sequence[str]] = None):
        self.min_breaks = min_breaks
        self.lookback_h = lookback_h
        self.vol_win = vol_win
        self.gate_vol = gate_vol
        self.direction = direction
        self.break_lines = tuple(
            break_lines if break_lines is not None
            else (BREAK_LINES_DOWN if direction == "down" else BREAK_LINES_UP))

    def replace(self, **kw) -> "GateParams":
        p = GateParams(direction=self.direction)
        for k in self.__slots__:
            setattr(p, k, getattr(self, k))
        for k, v in kw.items():
            if k not in self.__slots__:
                raise KeyError("未知参数 %s" % k)
            if k == "break_lines":
                v = tuple(v)
            setattr(p, k, v)
        return p

    def as_dict(self) -> Dict[str, object]:
        return {k: getattr(self, k) for k in self.__slots__}


def aggregate_1h(bars15: Sequence[Dict]) -> Dict[int, Dict]:
    """把 15m bar 聚成 1h 桶。高/低取最大最小、量求和、收盘取最后一根的收盘。

    为什么用聚合而不是另读一个 1h 库：1h 的 O/H/L/C 本来就等于 4 根 15m，
    聚合是精确的，还避免两份数据不同步。
    """
    out: Dict[int, Dict] = {}
    for b in bars15:
        ts = b.get(F_TS)
        if ts is None or b.get("o") is None:
            continue
        k = (int(ts) // HOUR_MS) * HOUR_MS
        r = out.get(k)
        if r is None:
            out[k] = {"ts": k, "o": float(b["o"]), "h": float(b["h"]),
                      "l": float(b["l"]), "c": float(b["c"]),
                      "v": float(b.get("vol") or 0.0), "last_ts": int(ts)}
        else:
            r["h"] = max(r["h"], float(b["h"]))
            r["l"] = min(r["l"], float(b["l"]))
            r["c"] = float(b["c"])
            r["v"] += float(b.get("vol") or 0.0)
            r["last_ts"] = int(ts)
    return out


def lines_by_hour(line_px: Dict[Tuple[int, str], float],
                  hours: Dict[int, Dict]) -> Dict[int, Dict[str, float]]:
    """把每个 15m 时刻的线价投影到它所属的 1h 桶上（取该小时最后一根的值）。

    `line_px` 的键是 (15m ts, line_key)。
    """
    out: Dict[int, Dict[str, float]] = {}
    for (ts, key), px in line_px.items():
        if px is None:
            continue
        k = (int(ts) // HOUR_MS) * HOUR_MS
        hb = hours.get(k)
        if hb is None or hb["last_ts"] != int(ts):
            continue                     # 只用该小时**最后一根** 15m 的线价
        out.setdefault(k, {})[key] = float(px)
    return out


def _median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def detect_breaks(hours: Dict[int, Dict],
                  lines: Dict[int, Dict[str, float]],
                  p: GateParams) -> List[Dict]:
    """找出「1h 收盘越过区间线」的时刻。

    越线 = 上一小时收盘在线的一侧、这一小时收盘到了另一侧。
    不用「本小时最高/最低穿过」——那是影线，不是收盘突破。
    """
    down = p.direction == "down"
    keys = sorted(hours)
    out: List[Dict] = []
    prev: Dict[str, float] = {}
    vols: List[float] = []

    for k in keys:
        hb = hours[k]
        c = hb["c"]
        ls = lines.get(k, {})
        vols.append(hb["v"])
        # 量比：该小时量 / 前 vol_win 小时中位数
        ref = _median(vols[:-1][-p.vol_win:]) if len(vols) > 1 else 0.0
        vol_rel = (hb["v"] / ref) if ref > 0 else None

        for key in p.break_lines:
            px = ls.get(key)
            if px is None:
                continue
            pc = prev.get(key)
            prev[key] = c
            if pc is None:
                continue
            crossed = (pc >= px and c < px) if down else (pc <= px and c > px)
            if not crossed:
                continue
            out.append({"h_ts": k, "line": key, "line_px": px,
                        "prev_close": pc, "close": c,
                        "beyond_pts": (px - c) if down else (c - px),
                        "vol": hb["v"], "vol_rel": vol_rel,
                        "heavy": (vol_rel is not None and vol_rel >= p.gate_vol)})
            break                      # 一根 1h 只记一次越线
    return out


def gate_state(breaks: Sequence[Dict], p: GateParams,
               hours: Dict[int, Dict]) -> List[Dict]:
    """闸门是**状态**，不是一次事件。

    第一版把闸门做成「破位后 24 根 15m 之内」，实测 11-15 07:00 开的闸门
    到 11-16 00:30 的拒绝已经过去 17.5 小时（70 根 15m），全部漏掉 ——
    而那段正是用户描述的行情。

    用户原话是「三次同方向区间突破，每次突破带量」之后「就开始等待」。
    这是**趋势状态**：闸门打开后一直开着，直到被证伪。
    证伪条件（做空方向）：1h 收盘重新站回最后一次跌破的那条线之上。
    """
    down = p.direction == "down"
    use = [b for b in breaks
           if b.get("vol_rel") is not None and b["vol_rel"] >= p.gate_vol]
    if not use:
        return []
    hk = sorted(hours)

    out: List[Dict] = []
    i = 0
    while i < len(use):
        # 从 use[i] 起数一个够 min_breaks 的段
        seg = [use[i]]
        j = i + 1
        while j < len(use):
            gap_h = (use[j]["h_ts"] - seg[-1]["h_ts"]) / float(HOUR_MS)
            if gap_h > p.lookback_h:
                break
            seg.append(use[j])
            j += 1
        if len(seg) < p.min_breaks:
            i = j
            continue

        open_ts = seg[p.min_breaks - 1]["h_ts"]
        # 证伪：之后第一个「收盘站回最后一条线」的 1h
        last_px = seg[-1]["line_px"]
        close_ts = None
        for k in hk:
            if k <= seg[-1]["h_ts"]:
                continue
            c = hours[k]["c"]
            recovered = (c > last_px) if down else (c < last_px)
            if recovered:
                close_ts = k
                break
        out.append({"open_ts": open_ts,
                    "close_ts": close_ts if close_ts is not None
                                else hk[-1],
                    "breaks": list(seg), "n_breaks": len(seg),
                    "first_close": seg[0]["close"],
                    "last_close": seg[-1]["close"],
                    "last_line_px": last_px,
                    "invalidated": close_ts is not None})
        i = j
    return out


def find_gates(breaks: Sequence[Dict], p: GateParams,
               heavy_only: bool = True) -> List[Dict]:
    """把破位聚成「段」，够 `min_breaks` 次的段就是打开的闸门（**不带到证伪**）。

    这是 `gate_state` 的简化版：只回答「哪些段够格」，不管它什么时候结束。
    保留它是因为回放里有时只想数「开了几次闸」，不需要区间。
    """
    if heavy_only:
        use = [b for b in breaks
               if b.get("vol_rel") is not None and b["vol_rel"] >= p.gate_vol]
    else:
        use = list(breaks)
    if not use:
        return []
    segs: List[List[Dict]] = []
    for b in use:
        if segs:
            gap_h = (b["h_ts"] - segs[-1][-1]["h_ts"]) / float(HOUR_MS)
            if gap_h <= p.lookback_h:
                segs[-1].append(b)
                continue
        segs.append([b])

    out: List[Dict] = []
    for seg in segs:
        if len(seg) < p.min_breaks:
            continue
        out.append({"open_ts": seg[p.min_breaks - 1]["h_ts"],
                    "close_ts": seg[-1]["h_ts"],
                    "breaks": list(seg),
                    "n_breaks": len(seg),
                    "first_close": seg[0]["close"],
                    "last_close": seg[-1]["close"]})
    return out


# ---------------------------------------------------------------- 第 2 段
class RejectParams(object):
    """第 2 段：两种「拒绝」形态。"""

    __slots__ = ("pin_count", "pin_pct", "pin_pts",
                 "ema_count", "ema_wick", "ema_tol", "direction",
                 "wait_max_bars")

    def __init__(self, pin_count: int = 2, pin_pct: float = 0.65,
                 pin_pts: float = 350.0, ema_count: int = 2,
                 ema_wick: float = 0.35, ema_tol: float = 0.0015,
                 direction: str = "down", wait_max_bars: int = 24):
        self.pin_count = pin_count
        self.pin_pct = pin_pct
        self.pin_pts = pin_pts
        self.ema_count = ema_count
        self.ema_wick = ema_wick
        self.ema_tol = ema_tol
        self.direction = direction
        self.wait_max_bars = wait_max_bars

    def replace(self, **kw) -> "RejectParams":
        q = RejectParams(direction=self.direction)
        for k in self.__slots__:
            setattr(q, k, getattr(self, k))
        for k, v in kw.items():
            if k not in self.__slots__:
                raise KeyError("未知参数 %s" % k)
            setattr(q, k, v)
        return q

    def as_dict(self) -> Dict[str, object]:
        return {k: getattr(self, k) for k in self.__slots__}


def _is_pin(bar: Dict, p: RejectParams) -> Optional[str]:
    """这根 5m 是不是插针，返回方向或 None。

    和 `ai_trader/kbar/shape.py::pin_side` 同一个思路：影线**比例**和
    影线**绝对点数**必须同时满足。只看比例，一根 5 点的小 K 也能占 70%；
    只看点数，一根 2000 点的大阳的 400 点上引线不算针。
    """
    h, l = float(bar["h"]), float(bar["l"])
    o, c = float(bar["o"]), float(bar["c"])
    rng = h - l
    if rng <= 0:
        return None
    up_pct = (h - max(o, c)) / rng
    dn_pct = (min(o, c) - l) / rng
    if dn_pct >= p.pin_pct and (min(o, c) - l) >= p.pin_pts:
        return "down"
    if up_pct >= p.pin_pct and (h - max(o, c)) >= p.pin_pts:
        return "up"
    return None


def _case(kind: str, at: Dict, frm: Dict, bars: Sequence[Dict]) -> Dict:
    """拒绝形态的统一结构。

    `ref_low` / `ref_high` 一起带上，是给第 3 段判「结构坏了没有」用的。
    第一版是让 `find_entries` 自己去 `bars5` 里按 ts 找那根 bar —— 找不到就
    静默跳过破位确认（`trigger = None`），等于把一道闸悄悄拆了。
    把极值直接写进 case 里，这条路就走不通了。
    """
    return {"kind": kind, "at_ts": at[F_TS], "from_ts": frm[F_TS],
            "bars": [b[F_TS] for b in bars],
            "ref_low": float(at["l"]), "ref_high": float(at["h"])}


def find_pin_case(bars5: Sequence[Dict], p: RejectParams) -> List[Dict]:
    """情况 1：连续 `pin_count` 根 5m 都是往同一侧的插针。

    做空方向要的是「向上被打出去又拉回」= `up` 针（拒绝上涨）。
    """
    want = "up" if p.direction == "down" else "down"
    out: List[Dict] = []
    n = len(bars5)
    run = 0
    start = 0
    for i in range(n):
        side = _is_pin(bars5[i], p)
        if side == want:
            if run == 0:
                start = i
            run += 1
            if run == p.pin_count:
                out.append(_case("pin", bars5[i], bars5[start],
                                 bars5[start:i + 1]))
                run = 0
        else:
            run = 0
    return out


def find_ema_case(bars15: Sequence[Dict], p: RejectParams) -> List[Dict]:
    """情况 2：连续 `ema_count` 根 15m 都碰上 ema20 被拒绝。

    做空方向：上引线够长、且收盘在 ema20 下方（价格冲上去被打回来）。
    """
    out: List[Dict] = []
    n = len(bars15)
    run = 0
    start = 0
    for i in range(n):
        b = bars15[i]
        e = b.get("ema20")
        ok = False
        if e is not None:
            e = float(e)
            h, l, c = float(b["h"]), float(b["l"]), float(b["c"])
            if p.direction == "down":
                ok = (h >= e * (1 - p.ema_tol)
                      and float(b.get("up_wick_pct") or 0) >= p.ema_wick
                      and c < e)
            else:
                ok = (l <= e * (1 + p.ema_tol)
                      and float(b.get("dn_wick_pct") or 0) >= p.ema_wick
                      and c > e)
        if ok:
            if run == 0:
                start = i
            run += 1
            if run == p.ema_count:
                out.append(_case("ema", b, bars15[start],
                                 bars15[start:i + 1]))
                run = 0
        else:
            run = 0
    return out


def cases_after(gate: Dict, cases: Sequence[Dict]) -> List[Dict]:
    """闸门**期间**出现的拒绝形态。

    闸门的边界就是 `open_ts`（第 min_breaks 次破位那一小时）到 `close_ts`
    （收盘站回最后一条线、也就是被证伪的那一小时，或数据末尾）。

    第一版用的是「破位后固定 24 根 15m 之内」，实测会漏掉 11-15 07:00
    开闸、11-16 00:30 才出现的那次拒绝 —— 中间隔了 70 根 15m，而那段正是
    用户描述「跌到 61900 然后反弹」的行情。闸门是趋势状态，不是一次性事件。
    """
    lo = gate["open_ts"]
    hi = gate.get("close_ts")
    if hi is None:
        return [c for c in cases if c["at_ts"] > lo]
    return [c for c in cases if lo < c["at_ts"] <= hi]


def dedup_cases(picked: Sequence[Tuple[Dict, Dict]]) -> List[Tuple[Dict, Dict]]:
    """把 (闸门, 拒绝形态) 去重到「每个拒绝形态只算一次」。

    为什么需要：闸门区间会重叠（一段行情里可能同时开着 2–3 个闸门），
    同一个拒绝形态会被每个闸门各挑中一次；`pin` 和 `ema` 两种形态也可能
    落在同一个 ts 上。不去重的话同一笔交易会被数 2–4 次。

    实测 60 天：去重前「90 笔」，去重后 **33 笔**，累计收益被放大了 2.7 倍。
    均值不变，但样本量、胜率、p 值全都被吹大了 —— 这是最容易骗过自己的
    一类统计错误，因为它不改变任何单笔的数字。
    """
    seen = set()
    out: List[Tuple[Dict, Dict]] = []
    for g, c in picked:
        key = c["at_ts"]
        if key in seen:
            continue
        seen.add(key)
        out.append((g, c))
    return out


def dedup_entries(entries: Sequence[Dict]) -> List[Dict]:
    """把入场去重到「每个入场时刻只算一次」。

    两个不同的拒绝形态可能等到同一根量能 5m（它们在同一段行情里）。
    去重之后结果里保留第一次出现的那个。
    """
    seen = set()
    out: List[Dict] = []
    for e in entries:
        k = e.get("entry_ts")
        if k is not None and k in seen:
            continue
        if k is not None:
            seen.add(k)
        out.append(e)
    return out


# ---------------------------------------------------------------- 第 3 段
class EntryParams(object):
    """第 3 段：入场（5m 量能根）与止损（1h 振幅）。"""

    __slots__ = ("entry_pts", "entry_vol", "stop_frac", "stop_bars",
                 "tick", "max_hold_15m", "direction", "min_stop_rng_mult",
                 "wait_bars5")

    def __init__(self, entry_pts: float = 350.0, entry_vol: float = 5.0,
                 stop_frac: float = 0.25, stop_bars: int = 1,
                 tick: float = 0.1, max_hold_15m: int = 16,
                 direction: str = "down", min_stop_rng_mult: float = 0.0,
                 wait_bars5: int = 12):
        self.entry_pts = entry_pts
        self.entry_vol = entry_vol
        self.stop_frac = stop_frac
        self.stop_bars = stop_bars
        self.tick = tick
        self.max_hold_15m = max_hold_15m
        self.direction = direction
        self.min_stop_rng_mult = min_stop_rng_mult
        self.wait_bars5 = wait_bars5

    def replace(self, **kw) -> "EntryParams":
        q = EntryParams(direction=self.direction)
        for k in self.__slots__:
            setattr(q, k, getattr(self, k))
        for k, v in kw.items():
            if k not in self.__slots__:
                raise KeyError("未知参数 %s" % k)
            setattr(q, k, v)
        return q

    def as_dict(self) -> Dict[str, object]:
        return {k: getattr(self, k) for k in self.__slots__}


def find_entries(bars5: Sequence[Dict], cases: Sequence[Dict],
                 hours: Dict[int, Dict], p: EntryParams) -> List[Dict]:
    """拒绝形态之后的第一根「顺势量能 5m」就是入场。

    三个条件都要满足：
    1. **方向对**：做空要收阴（c < o），做多要收阳。这一条是补上的 ——
       第一版只看振幅和量比，结果把「反弹还在继续」的那根大阳线也当成入场，
       实测 100% 止损就是这么来的：那根大 K 本身就是往止损方向跑的。
    2. **振幅够**：h−l ≥ `entry_pts`（用户说的「好几百点」）
    3. **带量**：`vol_rel ≥ entry_vol`

    另外要求价格**先破掉拒绝那根的低点**（做空）才算确认 —— 只是「又出现一根
    大阴线」还不够，得是反弹结构真的坏了。
    """
    down = p.direction == "down"
    out: List[Dict] = []
    ts_index = {b[F_TS]: i for i, b in enumerate(bars5)}
    for c in cases:
        idx = None
        for b in bars5:
            if b[F_TS] > c["at_ts"]:
                idx = ts_index[b[F_TS]]
                break
        if idx is None:
            continue

        # 拒绝那根的极值：做空要跌破它的低点，做多要突破它的高点。
        # 直接读 case 里烘好的值，不再去 bars5 里按 ts 找 —— 找不到会静默跳过，
        # 等于悄悄拆掉一道确认闸。
        trigger = c.get("ref_low") if down else c.get("ref_high")
        if trigger is None:
            out.append({"case": c, "status": "no_ref"})
            continue

        entry_bar = None
        for b in bars5[idx:idx + p.wait_bars5]:
            rng = float(b["h"]) - float(b["l"])
            vr = b.get("vol_rel")
            right_way = (float(b["c"]) < float(b["o"])) if down \
                else (float(b["c"]) > float(b["o"]))
            if not right_way:
                continue
            if rng < p.entry_pts or vr is None or vr < p.entry_vol:
                continue
            broke = (float(b["l"]) < trigger) if down \
                else (float(b["h"]) > trigger)
            if not broke:
                continue
            entry_bar = b
            break
        if entry_bar is None:
            out.append({"case": c, "status": "no_entry_bar"})
            continue

        # 止损基准：入场时刻之前**已完成**的 1h 桶
        hk = (int(entry_bar[F_TS]) // HOUR_MS) * HOUR_MS
        refs = []
        for j in range(1, p.stop_bars + 1):
            hb = hours.get(hk - j * HOUR_MS)
            if hb:
                refs.append(hb["h"] - hb["l"])
        ref = max(refs) if refs else 0.0
        if ref <= 0:
            out.append({"case": c, "status": "no_stop_ref"})
            continue

        px = float(entry_bar["c"])
        entry_rng = float(entry_bar["h"]) - float(entry_bar["l"])
        stop_dist = ref * p.stop_frac
        # 止损必须落在入场那根 5m 的振幅之外。
        # 实测：1h×1/4 的中位止损 81–95 点，而入场那根 5m 的振幅中位 300+ 点
        # —— 9/9 笔的止损都在那根 K 的体内。5m 的 OHLC 没有 bar 内顺序，
        # 这种止损「有没有被打」在日线数据上根本判不了，回放只能瞎猜。
        # 这不是参数问题，是止损的坐标系选错了：入场在 5m，止损却用 1h/4。
        if p.min_stop_rng_mult > 0:
            stop_dist = max(stop_dist, entry_rng * p.min_stop_rng_mult)
        out.append({"case": c, "status": "ok",
                    "entry_ts": entry_bar[F_TS], "entry": px,
                    "entry_rng": entry_rng,
                    "entry_vol_rel": entry_bar.get("vol_rel"),
                    "stop": px + stop_dist if down else px - stop_dist,
                    "stop_pts": stop_dist,
                    "stop_pct": stop_dist / px * 100.0})
    return out
