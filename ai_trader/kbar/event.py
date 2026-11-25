# -*- coding: utf-8 -*-
"""K 线演化 · 第 4 步：事件层。

设计稿 §8。三个要点，每个都不许自己加东西：

  8.1 **回溯是动态的，不是定死 N 根。**
      所以不存「前一根是什么」，存「上一件同类事件是什么」——
      `prev_event` = 同一条线上一次的事件名，`since_event_idx` = 隔了多少根。
      中间那些没意义的 K 不打断链条，隔 20 根也能接上。

  8.2 **允许「无意义」。**
      区间中间大量 K 是 neutral。强行给每根贴标签，就是在制造噪声。

  8.3 **词表复用区间演化，不另造。**
      `range/engine.py` / `range_evolution_events` 里有 14 个事件名。
      但事件必须带 `on_line`：突破对象不限于区间边界，EMA、唐奇安、结构位都能被突破。

## 这一步只做「价格与线的关系」，不做判断

事件全部由「收盘价相对线价的位置变化」决定，**没有阈值**：
上一根收盘在线的哪一侧、这一根收盘到了哪一侧。仅此而已。

所以本模块里没有「算不算有效突破」这种问题 —— 那是第 6 步（判断标准），
设计稿 §12 明令不许做。

## 为什么只有三个词

14 个事件名里，有 11 个是**区间自身的生命周期**，对一条 EMA 线没有意义：

    birth / 区间成形 / 下沿确认 / 上沿确认 / 下沿定格 / 上沿定格
    反向边界插针上移 / 反向边界插针下移 / R滚动
    向上反转 / 向下反转

（`向上反转`/`向下反转` 在区间引擎里指「收盘越过反向边界 R」，
 对一条普通线来说，越过另一侧就是突破本身，再单列一个名字会把同一件事记两遍。）

能落在**任意一条线**上的只有三个：

    向上突破      收盘从线的下方（或线上）走到上方
    向下突破      收盘从线的上方（或线上）走到下方
    突破失败·反包  突破之后 REVERT_WINDOW_BARS 根内收盘回到原来的那一侧

这不是「我挑的」，是「这 11 个名字的主语是区间、不是线」。

⚠ 设计稿 §8.4 举例写了 `event=收回`，但**词表 14 个名字里没有「收回」**。
  这一处按「不另造词」处理：那种情形本模块记为 `突破失败·反包`。
  已登记到 `docs/K线演化-待确定清单.md` D-15，等出钱的人定要不要另加这个词。

## 一根 K 线可能同时有几个事件

表 1 只有一组 `event / on_line` 列，装不下一根 K 线上的多个事件。
所以：

    kbar_event        每个 (根, 线, 事件) 一行 —— **事实，一行不丢**
    表 1 的 event 列  按「穿透最深的那个」取一个 —— 口径，见 BAR_EVENT_RULE

丢掉事件就是丢事实，所以先全量落 `kbar_event`，表 1 只是它的一个读法。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 词表
# 区间演化的 14 个事件名（来源：range/engine.py + 生产库 range_evolution_events）。
# 这里**全量登记**，即使本模块只用其中 3 个 —— 登记是为了让「为什么不用」有地方写。
EVENT_VOCAB_RANGE = (
    "birth", "区间成形",
    "下沿确认", "上沿确认", "下沿定格", "上沿定格",
    "向上突破", "向下突破",
    "突破失败·反包",
    "反向边界插针上移", "反向边界插针下移",
    "R滚动",
    "向上反转", "向下反转",
)

BREAK_UP = "向上突破"
BREAK_DOWN = "向下突破"
FAILED_BREAK = "突破失败·反包"

#: 落在**任意一条观察线**上时用得到的事件名。
EVENT_VOCAB_USED = (BREAK_UP, BREAK_DOWN, FAILED_BREAK)

#: 主语是「区间自身生命周期」的事件 —— 对一条 EMA/唐奇安/结构位没有意义。
EVENT_VOCAB_RANGE_ONLY = tuple(
    x for x in EVENT_VOCAB_RANGE if x not in EVENT_VOCAB_USED)

#: 反包窗口：突破之后多少根内收盘回到原侧算「突破失败·反包」。
#: 取值 3 不是拍的 —— `ai_trader/range/engine.py` 的 `FORM_BARS = 3`
#: 就是区间演化的成形窗口（规范 v11 §2「突破/反转后 3 根 1h 内取反抽极值」）。
#: 沿用同一个数，是为了两套事件对「多久算没站稳」用同一把尺。
#: 但 15m 上是否该沿用 1h 的 3 根，属于判断标准 → 待确定清单 D-16。
REVERT_WINDOW_BARS = 3

#: 没有任何事件、也没碰到任何线 —— §8.2 里「没意义」的那一类。
#: 注意：§8.2 的完整定义还有「量正常、在空间中间」，那两项都要阈值（判断标准），
#: 所以这里只用了「没碰到任何观察线」这个**无阈值**的部分。见 D-17。
NEUTRAL = "neutral"

#: 表 1 的 event 列怎么从多个事件里挑一个。口径不是判断标准（无阈值）。
BAR_EVENT_RULE = ("一根 K 线有多个事件时，表 1 取穿透最深的那个"
                  "（pen_depth_pct 最大，并列时按 line_key 字典序）；"
                  "全部事件仍完整保存在 kbar_event")


# ---------------------------------------------------------------- 纯计算
def classify_cross(prev_close: Optional[float], close: Optional[float],
                   line_price: Optional[float]) -> Optional[str]:
    """收盘相对线价的**穿越**。

    只在「换边」时给事件，站同一边不动就不给 —— 否则每根都是事件，等于没有事件。

    prev_close 为 None（第一根）时不给事件：没有「上一根收盘」就无法说穿越。
    碰巧收在线上（相等）归入「下方/上方」的处理见下：
      收盘 == 线价时**不算**穿越到任何一侧（不把 at 硬塞进一边，与第 3 步 close_side 一致）。
    """
    if prev_close is None or close is None or line_price is None:
        return None
    p = float(line_price)
    if post_zero(prev_close - p) <= 0 and post_zero(close - p) > 0:
        return BREAK_UP
    if post_zero(prev_close - p) >= 0 and post_zero(close - p) < 0:
        return BREAK_DOWN
    return None


def post_zero(v: float) -> float:
    """把 -0.0 归一成 0.0，免得比较时出现 -0.0 <= 0 的意外。"""
    v = float(v)
    return 0.0 if v == 0.0 else v


def side_of_close(close: Optional[float], line_price: Optional[float]) -> Optional[str]:
    """收盘在线的哪一侧：above / below / at / None。"""
    if close is None or line_price is None:
        return None
    d = post_zero(float(close) - float(line_price))
    if d > 0:
        return "above"
    if d < 0:
        return "below"
    return "at"


def build_events(symbol: str, interval_ms: int, bars: Sequence[dict],
                 line_rows: Dict[str, Dict[int, Tuple]],
                 line_meta: Optional[Dict[str, Tuple[str, int]]] = None,
                 touch_by_bar: Optional[Dict[int, int]] = None,
                 revert_window_bars: int = REVERT_WINDOW_BARS
                 ) -> Tuple[List[dict], Dict]:
    """算事件层。

    bars:        按 ts 升序的 [{ts, o, h, l, c, vol_rel, taker_buy_ratio}]
    line_rows:   {line_key: {bar_ts: (price, ready, src_ts)}} —— 与第 3 步同一个形状，
                 同一条「只取上一根那一行」的规则（不许往前带、不许偷未来）
    touch_by_bar: {ts: 表 2 里这根碰到几条线}，用来判定 neutral
    revert_window_bars: 反包窗口（见 REVERT_WINDOW_BARS 的取值来源）

    返回 (rows, stats)。rows 每个 (根, 线, 事件) 一行。
    """
    stats = {
        "bars_in": len(bars), "rows": 0, "events": 0, "bars_with_event": 0,
        "by_event": {}, "bars_neutral": 0, "bars_touch_no_event": 0,
        "skipped": {}, "lines": len(line_rows),
        "revert_window_bars": int(revert_window_bars),
        "vocab_used": list(EVENT_VOCAB_USED),
        "vocab_not_applicable": list(EVENT_VOCAB_RANGE_ONLY),
    }
    if not bars or not line_rows:
        return [], stats

    keys = sorted(line_rows)
    meta = line_meta or {}
    iv = int(interval_ms)
    rows: List[dict] = []
    prev_ts = None
    prev_close = None
    # 每条线的状态：上一次事件 (name, idx)；以及未平仓的突破（等反包）
    last_event: Dict[str, Tuple[str, int]] = {}
    open_break: Dict[str, dict] = {}
    bars_with_event = set()

    for b in bars:
        ts = int(b["ts"])
        c = b["c"]
        idx = ts // iv
        for key in keys:
            if prev_ts is None:
                stats["skipped"]["no_prev_bar"] = stats["skipped"].get("no_prev_bar", 0) + 1
                continue
            hit = line_rows[key].get(prev_ts)
            if hit is None:
                stats["skipped"]["line_absent_at_prev_bar"] = \
                    stats["skipped"].get("line_absent_at_prev_bar", 0) + 1
                continue
            p = float(hit[0])
            kind, per = meta.get(key, ("", iv))
            ev = classify_cross(prev_close, c, p)

            # 反包：之前有未平仓的突破，窗口内收盘又回到突破前的**原来那一侧**。
            #
            # 这里必须**独立于当根的穿越**判定，不能写在「当根没有穿越」的分支里。
            # 理由：「突破后价格穿回原侧」这根本身就是一次反向穿越（会被判成
            # `向下突破`）。若把反包挂在「当根无穿越」下，反包就只在「线自己移动到
            # 价格另一侧」时才触发 —— 那与「突破失败」这个名字不符。
            # 设计稿 §8.4 的例子 `event=收回, prev_event=向上突破` 也说明：
            # 收回与反向穿越可以是同一根上的两件事，且 prev_event 就是那个突破。
            ob = open_break.get(key)
            fwd = None
            if ob is not None:
                if idx - ob["idx"] > int(revert_window_bars):
                    open_break.pop(key, None)          # 窗口过了，突破算站住
                elif side_of_close(c, p) == ob["back_side"]:
                    fwd = FAILED_BREAK
                    open_break.pop(key, None)

            if ev:
                # 新的突破覆盖旧的未平仓突破（同一条线不会同时有两个待确认突破）
                prev_side = "below" if ev == BREAK_UP else "above"
                open_break[key] = {"idx": idx, "back_side": prev_side, "event": ev}

            # 穿透深度只在「这根真的碰到了这条线」时才有意义。
            # 反包可以在一根**没有碰到线**的 K 线上发生（跳空回到另一侧），
            # 那种情况留空 —— 硬算会得到一个解释不了的数。
            l, h, o = b["l"], b["h"], b["o"]
            touched = (l is not None and h is not None and l <= p <= h)
            pen = None
            if touched:
                from .reaction import pen_depth_pct, side_of
                pen = pen_depth_pct(p, l, h, side_of(o, p))

            # 顺序很重要：**先记反包、再记当根的穿越**。
            # 这样反包那条的 prev_event 正好是「被反掉的那个突破」，
            # 与设计稿 §8.4 的例子 `event=收回, prev_event=向上突破` 一致。
            for name in (fwd, ev):
                if not name:
                    continue
                last = last_event.get(key)
                rows.append({
                    "symbol": symbol, "interval_ms": iv, "ts": ts,
                    "idx": idx, "line_key": key, "line_kind": kind,
                    "line_period_ms": int(per), "line_price": p,
                    "event": name,
                    "prev_event": last[0] if last else None,
                    "since_event_idx": (idx - last[1]) if last else None,
                    "close_side": side_of_close(c, p),
                    "pen_depth_pct": pen,
                    "vol_rel": b.get("vol_rel"),
                    "taker_buy_ratio": b.get("taker_buy_ratio"),
                })
                last_event[key] = (name, idx)
                stats["by_event"][name] = stats["by_event"].get(name, 0) + 1
                bars_with_event.add(ts)

        stats["rows"] = len(rows)
        prev_ts = ts
        prev_close = c

    stats["events"] = len(rows)
    stats["bars_with_event"] = len(bars_with_event)

    # neutral：没事件、也没碰到任何线。§8.2 完整定义里的「量正常/空间中间」要阈值，
    # 那部分不在本模块做（D-17），所以这里只用无阈值的那一半。
    tbb = touch_by_bar or {}
    for b in bars:
        ts = int(b["ts"])
        if ts in bars_with_event:
            continue
        if tbb.get(ts, 0) > 0:
            stats["bars_touch_no_event"] += 1
        else:
            stats["bars_neutral"] += 1

    return rows, stats


def _pen_key(e: dict) -> Tuple[float, str]:
    """排序键：(穿透深度, line_key)。

    ⚠ 这里**必须**用 `is None` 判断缺失，不能写 `x or -1.0`。
    `pen_depth_pct` 可以**恰好等于 0.0**（本根最低价正好等于线价，实测库里有这种行），
    而 `0.0 or -1.0` 会得到 -1.0 —— 于是「已知穿透为 0」和「穿透未知」被压成同一档，
    排序退回按 line_key，把一个**已知**的最浅穿透当成**未知**处理。
    """
    pen = e.get("pen_depth_pct")
    return (-1.0 if pen is None else float(pen), str(e["line_key"]))


def bar_event_columns(events: Sequence[dict], bars: Sequence[dict],
                      touch_by_bar: Optional[Dict[int, int]] = None) -> Dict[int, dict]:
    """把 kbar_event 汇总成表 1 的那几列（每根一行）。

    一根有多个事件时按 BAR_EVENT_RULE 取「穿透最深」的那个。
    穿透深度用事件行里的 pen_depth_pct（来自表 2）；没有该值时按「最浅」参与比较。
    """
    best: Dict[int, dict] = {}
    for e in events:
        ts = int(e["ts"])
        cur = best.get(ts)
        if cur is None:
            best[ts] = e
            continue
        a = _pen_key(e)
        b = _pen_key(cur)
        # 穿透更深者胜；并列时 line_key 小的胜（保证确定性）
        if a[0] > b[0] or (a[0] == b[0] and a[1] < b[1]):
            best[ts] = e
    out: Dict[int, dict] = {}
    tbb = touch_by_bar or {}
    for b in bars:
        ts = int(b["ts"])
        e = best.get(ts)
        if e is not None:
            out[ts] = {"event": e["event"], "on_line": e["line_key"],
                       "prev_event": e.get("prev_event"),
                       "since_event_idx": e.get("since_event_idx"),
                       "state": None}
        elif tbb.get(ts, 0) > 0:
            # 碰到了线但没产生事件：既不是 neutral，也还没有事件 —— 留空，别硬贴
            out[ts] = {"event": None, "on_line": None, "prev_event": None,
                       "since_event_idx": None, "state": None}
        else:
            out[ts] = {"event": None, "on_line": None, "prev_event": None,
                       "since_event_idx": None, "state": NEUTRAL}
    return out


def event_counts_by_event(events: Sequence[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for e in events:
        out[e["event"]] = out.get(e["event"], 0) + 1
    return out


def find_breakout_then_revert(events: Sequence[dict], line_key: Optional[str] = None
                              ) -> List[Tuple[dict, dict]]:
    """找「突破之后又反包」的**因果**成对记录，供探针人工核对。

    按 `prev_event` 配对，不按「最近的先前突破」配。
    原因：反包那条自己的 `prev_event` 就写着它是被哪个突破反掉的，
    而「最近的先前突破」在来回震荡里会配到不相干的那一个 ——
    实测 2026-05-08 08:30 的突破会被配给 05-09 13:15 的反包，而后者真正的
    前序事件是同一条线上 12:45 的突破。

    返回 [(突破事件, 反包事件)]。同一轮里没配上对的反包不返回。
    """
    out: List[Tuple[dict, dict]] = []
    # 每条线上「还没有被配走的突破」，按发生顺序排队
    pool: Dict[str, List[dict]] = {}
    for e in sorted(events, key=lambda x: (int(x["ts"]), str(x["line_key"]))):
        k = e["line_key"]
        if line_key is not None and k != line_key:
            continue
        q = pool.setdefault(k, [])
        if e["event"] in (BREAK_UP, BREAK_DOWN):
            q.append(e)
        elif e["event"] == FAILED_BREAK:
            want = e.get("prev_event")
            # 只认「prev_event 正好是一个突破」的那种，并从该线上找到同名的前一个
            idx = None
            for i in range(len(q) - 1, -1, -1):
                if q[i]["event"] == want:
                    idx = i
                    break
            if idx is not None:
                out.append((q.pop(idx), e))
    return out
