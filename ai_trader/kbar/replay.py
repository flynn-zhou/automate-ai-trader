# -*- coding: utf-8 -*-
"""挂单式买法的规则回放（纯计算，不碰数据库、不碰实盘）。

## 为什么要单独一层

用户口述的规则里有一堆**必须由数据来定的数**：上引线多长算长、收在 ema20
上方多少算"一点点"、带量几倍算带量、盯多久算等到了。

这些数不能由我先拍一个写进执行器 —— 一旦写进执行器，验证方式就变成了
"拿真钱跑"，而不是"拿历史跑"。

所以这里是**纯函数**：给我一串 15m bar + 每根的 ema20，给我一组参数，
我告诉你命中多少次、进场多少次、止损被打掉多少次、平均盈亏多少。
改一个数就重跑，几十种组合几十秒出结果。

## 规则（用户口述，方向参数化）

**做空方向（下跌趋势）**：

1. 信号根 A：15m 的 high 穿过 ema20，但带长上引线，且
   - 收盘在 ema20 下方（A 型）；或
   - 收盘在 ema20 上方一点点（A' 型，容忍 `above_tol`）
2. 前置条件（可选）：A 之前 `lookback` 根内有"带量突破 1h 区间"
   （`event` 含「突破」且 `vol_rel >= vol_min`）
3. 触发根 B：A 之后 `max_wait` 根内，某根 again 碰上 ema20
   （high >= ema20*(1-touch_tol)）又收在下方的带上引线 K（拒绝）
4. 进场：在 B 的最低价下方 `tick` 处挂 STOP 空单；之后 `entry_wait` 根内
   跌破该价才成交（**不成交就不进，不留市价追单**）
5. 止损：进场价上方「前两根里 h−l 较大的那根」× `stop_frac`
6. 出场：`max_hold` 根后按收盘平（止盈规则还没定，所以先不假装有）

做多方向是镜像（上涨趋势里跌破 ema20 后又被下引线接住拒绝）。

## 这个模块故意不做的事

- 不计算止盈。用户的止盈是"投影单 + 演化判断"，规则还没定稿；
  硬编一个止盈，回放出来的胜率就会变成我自己规则的胜率。
  改为报告 MFE/MAE，让"止盈该放哪"由数据说。
- 不碰数据库。加载在 CLI 里做，这里只吃 list[dict]。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

# 15m bar 的字段名（与 kbar_evolution 对齐）
F_TS = "ts"
F_O, F_H, F_L, F_C = "o", "h", "l", "c"
F_UPW = "up_wick_pct"
F_DNW = "dn_wick_pct"
F_VOLREL = "vol_rel"
F_EVENT = "event"
F_EMA = "ema20"

#: 双边手续费（taker 开 + taker 平，%）。实盘 0.05%×2 = 0.1%，
#: 但带 maker 挂单开仓可能只付一边，取 0.09% 偏乐观地用。
DEFAULT_FEE_PCT = 0.09


class Params(object):
    """一组可回放参数。全都有默认值，但默认值不代表"对的"，只代表"起点"。"""

    __slots__ = ("wick_strong", "wick_any", "above_tol", "touch_tol",
                 "vol_min", "lookback", "max_wait", "entry_wait",
                 "stop_frac", "tick", "max_hold", "require_precondition",
                 "direction", "same_bar_stop", "stop_basis", "stop_bars",
                 "min_stop_pct")

    def __init__(self, wick_strong: float = 0.65, wick_any: float = 0.35,
                 above_tol: float = 0.0015, touch_tol: float = 0.0015,
                 vol_min: float = 3.0, lookback: int = 8, max_wait: int = 8,
                 entry_wait: int = 4, stop_frac: float = 0.25,
                 tick: float = 0.1, max_hold: int = 8,
                 require_precondition: bool = True,
                 direction: str = "short", same_bar_stop: bool = False,
                 stop_basis: str = "15m", stop_bars: int = 2,
                 min_stop_pct: float = 0.0):
        self.same_bar_stop = same_bar_stop
        self.stop_basis = stop_basis
        self.stop_bars = stop_bars
        self.min_stop_pct = min_stop_pct
        self.wick_strong = wick_strong
        self.wick_any = wick_any
        self.above_tol = above_tol
        self.touch_tol = touch_tol
        self.vol_min = vol_min
        self.lookback = lookback
        self.max_wait = max_wait
        self.entry_wait = entry_wait
        self.stop_frac = stop_frac
        self.tick = tick
        self.max_hold = max_hold
        self.require_precondition = require_precondition
        self.direction = direction

    def replace(self, **kw) -> "Params":
        p = Params(direction=self.direction)
        for k in self.__slots__:
            setattr(p, k, getattr(self, k))
        for k, v in kw.items():
            if k not in self.__slots__:
                raise KeyError("未知参数 %s" % k)
            setattr(p, k, v)
        return p

    def as_dict(self) -> Dict[str, object]:
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self) -> str:
        return "Params(%s)" % ", ".join(
            "%s=%r" % (k, getattr(self, k)) for k in self.__slots__)


def _f(bar: Dict, key: str, default: float = 0.0) -> float:
    v = bar.get(key)
    return default if v is None else float(v)


def _bar_range(bar: Dict) -> float:
    return _f(bar, F_H) - _f(bar, F_L)


HOUR_MS = 3600000


def hour_ranges(bars: Sequence[Dict]) -> Dict[int, List[float]]:
    """把 15m bar 聚合成 1h 桶的 [最低, 最高]，用于 1h 止损基准。

    用聚合而不是另读一个库：1h 的高/低本来就等于 4 根 15m 的最大/最小，
    聚合出来是精确的，还能避免两份数据不同步。

    **没有偷看未来**：找"已完成的 1h"用的是 `桶起点 + 1h <= 当前 ts`，
    进场那一刻当前这根 1h 还没走完，不能拿它的振幅当已知信息。
    """
    out: Dict[int, List[float]] = {}
    for b in bars:
        ts = b.get(F_TS)
        if ts is None:
            continue
        k = (int(ts) // HOUR_MS) * HOUR_MS
        r = out.get(k)
        lo, hi = _f(b, F_L), _f(b, F_H)
        if r is None:
            out[k] = [lo, hi]
        else:
            if lo < r[0]:
                r[0] = lo
            if hi > r[1]:
                r[1] = hi
    return out


def find_signals(bars: Sequence[Dict], p: Params) -> List[Dict]:
    """按规则找出所有信号，并算进出场结果。

    返回的每一条都带 `status`：
        no_trigger   —— 有 A 没等到 B
        no_fill      —— 有 B，但挂单没被打到（价格没再破 1 个 tick）
        settled      —— 真正进场了，字段齐全
    **统计胜率时只算 settled**，no_fill 单独报，因为它决定了"这规则实际
    能开出去多少单"。
    """
    short = p.direction == "short"
    out: List[Dict] = []
    n = len(bars)
    hbuckets = hour_ranges(bars) if p.stop_basis == "1h" else {}

    for i in range(n):
        a = bars[i]
        e = a.get(F_EMA)
        if e is None:
            continue
        e = float(e)
        hi, lo, cl = _f(a, F_H), _f(a, F_L), _f(a, F_C)
        wick = _f(a, F_UPW) if short else _f(a, F_DNW)

        # ---- A：刺穿 ema20 且带长引线，收盘在 ema20 内侧 ----
        if short:
            if hi <= e or wick < p.wick_strong:
                continue
            if not (cl < e or cl <= e * (1 + p.above_tol)):
                continue
        else:
            if lo >= e or wick < p.wick_strong:
                continue
            if not (cl > e or cl >= e * (1 - p.above_tol)):
                continue

        # ---- 前置条件：之前有带量突破 1h 区间 ----
        pre = None
        for j in range(max(0, i - p.lookback), i):
            ev = bars[j].get(F_EVENT) or ""
            want = "向下突破" if short else "向上突破"
            if want in ev and _f(bars[j], F_VOLREL) >= p.vol_min:
                pre = j
                break
        if p.require_precondition and pre is None:
            continue

        rec = {"a_ts": a.get(F_TS), "a_close": cl, "a_ema": e,
               "a_wick": wick, "pre_ts": (bars[pre].get(F_TS)
                                          if pre is not None else None)}

        # ---- B：再次碰上 ema20 又被拒绝 ----
        b_i: Optional[int] = None
        for j in range(i + 1, min(n, i + 1 + p.max_wait)):
            b = bars[j]
            eb = b.get(F_EMA)
            if eb is None:
                continue
            eb = float(eb)
            if short:
                ok = (_f(b, F_H) >= eb * (1 - p.touch_tol)
                      and _f(b, F_UPW) >= p.wick_any and _f(b, F_C) < eb)
            else:
                ok = (_f(b, F_L) <= eb * (1 + p.touch_tol)
                      and _f(b, F_DNW) >= p.wick_any and _f(b, F_C) > eb)
            if ok:
                b_i = j
                break
        if b_i is None:
            rec["status"] = "no_trigger"
            out.append(rec)
            continue

        b = bars[b_i]
        rec.update({"b_ts": b.get(F_TS), "b_close": _f(b, F_C),
                    "b_high": _f(b, F_H), "b_low": _f(b, F_L),
                    "wait": b_i - i})

        # ---- 进场：B 的外侧 1 个 tick 挂 STOP，entry_wait 根内没破就不进 ----
        if short:
            trigger = _f(b, F_L) - p.tick
        else:
            trigger = _f(b, F_H) + p.tick
        rec["trigger"] = trigger

        fill_i = None
        for k in range(b_i + 1, min(n, b_i + 1 + p.entry_wait)):
            if short and _f(bars[k], F_L) <= trigger:
                fill_i = k
                break
            if (not short) and _f(bars[k], F_H) >= trigger:
                fill_i = k
                break
        if fill_i is None:
            rec["status"] = "no_fill"
            out.append(rec)
            continue

        # ---- 止损基准：15m（前 N 根）或 1h（前 N 根已完成的 1h 桶）----
        if p.stop_basis == "1h":
            t_b = int(b.get(F_TS))
            cur = (t_b // HOUR_MS) * HOUR_MS
            refs = []
            for k in range(1, p.stop_bars + 1):
                r = hbuckets.get(cur - k * HOUR_MS)
                if r:
                    refs.append(r[1] - r[0])
            ref = max(refs) if refs else 0.0
        else:
            prevs = [bars[x] for x in range(b_i - p.stop_bars, b_i)
                     if 0 <= x < n]
            ref = max([_bar_range(x) for x in prevs] or [0.0])
        if ref <= 0:
            rec["status"] = "no_fill"
            rec["note"] = "止损基准区间为 0，无法定止损"
            out.append(rec)
            continue
        stop_dist = ref * p.stop_frac
        entry = trigger
        stop = entry + stop_dist if short else entry - stop_dist
        rec["stop_pct"] = stop_dist / entry * 100.0

        # ---- 最小止损距离闸 ----
        # 用户的形态是"好几百点的 5m 突破"，对应的止损不可能只有几十点。
        # 止损距离太小的信号说明抓到的是噪声而不是那个形态 —— 直接筛掉，
        # 而不是硬着头皮算它的胜率。
        if p.min_stop_pct > 0 and rec["stop_pct"] < p.min_stop_pct:
            rec["status"] = "filtered"
            out.append(rec)
            continue

        # ---- 走完 max_hold 根：先判止损，再按收盘平 ----
        #
        # ★ 成交那一根**默认不判止损**（same_bar_stop=False）。
        #
        # 这是个必须说清的取舍：15m OHLC 只给了这根的最高/最低，没给先后顺序。
        # 挂单是在这根**盘中**被跌破才成交的，而这根的 high 很可能出现在成交
        # **之前**。若照 high 判止损，就相当于假设"成交瞬间价格已经在最高点"，
        # 结果必然是构造性 100% 止损 —— 第一版就是这么错的（止损率 100%、
        # MFE 恒 0.00%），那种数字拿去讨论策略毫无意义。
        #
        # 所以默认从**成交的下一根**开始判。代价是偏向乐观（真实可能在同一根
        # 内被扫），把 same_bar_stop=True 可以看到悲观端，两者之差就是这个
        # 建模不确定性的大小。
        start = fill_i if p.same_bar_stop else fill_i + 1
        exit_px, exit_i, why = None, None, "time"
        mfe = mae = 0.0
        for k in range(start, min(n, fill_i + 1 + p.max_hold)):
            kk = bars[k]
            if short:
                fav = (entry - _f(kk, F_L)) / entry * 100.0
                adv = (entry - _f(kk, F_H)) / entry * 100.0
                if _f(kk, F_H) >= stop:
                    exit_px, exit_i, why = stop, k, "stop"
                    break
            else:
                fav = (_f(kk, F_H) - entry) / entry * 100.0
                adv = (_f(kk, F_L) - entry) / entry * 100.0
                if _f(kk, F_L) <= stop:
                    exit_px, exit_i, why = stop, k, "stop"
                    break
            mfe = max(mfe, fav)
            mae = min(mae, adv)
        if exit_px is None:
            k = min(n - 1, fill_i + p.max_hold)
            exit_px, exit_i = _f(bars[k], F_C), k

        gross = ((entry - exit_px) if short else (exit_px - entry)) / entry * 100.0
        rec.update({"status": "settled", "fill_ts": bars[fill_i].get(F_TS),
                    "entry": entry, "stop": stop, "exit": exit_px,
                    "exit_ts": bars[exit_i].get(F_TS), "why": why,
                    "hold": exit_i - fill_i, "gross_pct": gross,
                    "mfe_pct": mfe, "mae_pct": mae,
                    "r_multiple": (gross / (stop_dist / entry * 100.0)
                                   if stop_dist else 0.0)})
        out.append(rec)
    return out


def summarize(signals: Sequence[Dict], fee_pct: float = DEFAULT_FEE_PCT) -> Dict:
    """把信号列表压成一组能横比的数字。"""
    settled = [s for s in signals if s.get("status") == "settled"]
    n_all = len(signals)
    n_trig = sum(1 for s in signals if s.get("status") != "no_trigger")
    n_filtered = sum(1 for s in signals if s.get("status") == "filtered")
    n_settled = len(settled)
    base = {"signals": n_all, "triggered": n_trig,
            "filtered": n_filtered, "settled": n_settled}
    if not settled:
        base.update({"fill_rate": 0.0, "stop_rate": 0.0, "win_rate": 0.0,
                     "avg_net_pct": 0.0, "sum_net_pct": 0.0, "avg_r": 0.0,
                     "avg_mfe_pct": 0.0, "avg_mae_pct": 0.0,
                     "avg_stop_pct": 0.0, "median_stop_pct": 0.0})
        return base
    wins = [s for s in settled if s["gross_pct"] - fee_pct > 0]
    stops = [s for s in settled if s.get("why") == "stop"]
    nets = [s["gross_pct"] - fee_pct for s in settled]
    stop_pcts = sorted(s.get("stop_pct", 0.0) for s in settled)
    base.update({
        "fill_rate": round(n_settled / float(n_trig), 3) if n_trig else 0.0,
        "stop_rate": round(len(stops) / float(n_settled), 3),
        "win_rate": round(len(wins) / float(n_settled), 3),
        "avg_net_pct": round(sum(nets) / len(nets), 4),
        "sum_net_pct": round(sum(nets), 4),
        "avg_r": round(sum(s["r_multiple"] for s in settled) / n_settled, 3),
        "avg_mfe_pct": round(sum(s["mfe_pct"] for s in settled) / n_settled, 3),
        "avg_mae_pct": round(sum(s["mae_pct"] for s in settled) / n_settled, 3),
        # 止损距离的典型值 —— 这是判断"抓到的是不是那个形态"的第一手指标。
        # 用户说好几百点，若中位数只有几十点，说明信号找错了，不是参数不对。
        "avg_stop_pct": round(sum(stop_pcts) / len(stop_pcts), 3),
        "median_stop_pct": round(stop_pcts[len(stop_pcts) // 2], 3),
    })
    return base


def sweep(bars: Sequence[Dict], base: Params,
          grid: Dict[str, Sequence[object]]) -> List[Dict]:
    """在参数网格上跑一遍，返回按平均净值降序的结果。

    单变量网格（一次只动一个参数）是刻意的：多参数联合网格会立刻过拟合，
    14 天数据上找到的"最优组合"基本是噪声。
    """
    rows = []
    for name, values in grid.items():
        for v in values:
            p = base.replace(**{name: v})
            s = summarize(find_signals(bars, p))
            s["param"] = name
            s["value"] = v
            rows.append(s)
    base_s = summarize(find_signals(bars, base))
    base_s["param"] = "(基准)"
    base_s["value"] = "-"
    rows.append(base_s)
    rows.sort(key=lambda r: r["avg_net_pct"], reverse=True)
    return rows
