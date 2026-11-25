# -*- coding: utf-8 -*-
"""K 线演化 · 观察线计算层（第 1 步）。

定位（docs/K线演化-设计讨论稿.md §4）
------------------------------------
这些线**不是支撑线，是观察线**。本模块只回答一个问题：

    这根 K 线收盘时，这条线在哪个价格？

不回答「这条线守住没有」——那是反应层（第 3 步）的事。
「支撑」不是线的属性，是反应的统计结果；事前它只是观测位。

每条线的身份 = (种类, 自己的周期, 参数)
    EMA200 在 15m 上和 EMA200 在 1h 上是**两条不同的线**，所以 line_key 必须带周期：
        ema200@15m / ema200@1h / range_hard_up@1h
    区间演化四线的周期**固定 1h**，不随 K 线周期变（v3 §2 实测：状态机跑在 1h 上）。

口径（改这里必须同步 docs/K线演化-实现记录.md，禁止只改代码）
------------------------------------------------------------
    EMA(N)      α = 2/(N+1)，**种子 = 序列第一根收盘价**
                （与 ai_trader/web/live.py::ema_series 同一套算法。用 SMA 做种子会差几十点，
                 这不是风格差异，是两套不同的线。）
    预热        一根 K 要落 EMA 线，序列里它之前必须有 EMA_WARMUP_BARS 根
                （1500 = 币安单次上限 = 看板取数长度）。不足就不产出，并计数上报。
    唐奇安上轨  max(high[i-N : i])，N = 150，**不含当根**
    唐奇安下轨  min(low[i-M : i])，M = 75，**不含当根**
                （与 ai_trader/trading/donchian.py §口径 逐字一致）
    前高/前低   max(high[i-N:i]) / min(low[i-N:i])，N = 20，不含当根
    摆动高低点  严格局部极值，半窗 SWING_HALF = 3 根：
                    high[j] > high[j-1..j-3] 且 high[j] > high[j+1..j+3]
                **右侧确认**：枢轴 j 要到第 j+3 根收盘才成立。
    区间演化    S = soft_up/soft_dn，R = hard_up/hard_dn，K = v11_key，
                P = 顺向边界（DOWN 时是 soft_dn，UP 时是 soft_up）。
                本模块**只读不算**，值必须与 range_evolution 该小时行逐位相等。

防偷未来（设计稿 §12 第 1 步的硬要求）
--------------------------------------
一条线在「它来源的那根 K 收盘」之前不可用，所以每行都记两个时间：

    src_ts        这条线值的来源 K 线开盘时间
    line_ready_ms 这个值最早何时可用 = src_ts + 该线自己的周期

对本根 K 线（开盘 ts、周期 interval_ms，收盘 ts+interval_ms）只允许
    line_ready_ms <= ts + interval_ms
的线出现。边界取 `<=`：恰好同一时刻收盘的线，那一刻值已经确定，不是未来信息。

15m K 线 × 1h 观察线：15m 的 12:00 那根（11:45~12:00）只能用 11:00 那根 1h
（12:00 收盘）；12:00 那根 1h（13:00 才收盘）绝对不能用。
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 口径常量
# 全仓唯一口径。改这里 = 改所有历史数据的含义，必须同步文档。
EMA_PERIODS: Tuple[int, ...] = (20, 50, 144, 169, 200)
EMA_WARMUP_BARS: int = 1500      # = web/live.py::EMA_WARMUP_BARS（有测试钉死两者相等）
DONCHIAN_HI_BARS: int = 150      # 唐奇安上轨：150 根新高
DONCHIAN_LO_BARS: int = 75       # 唐奇安下轨：75 根新低
PRIOR_HL_BARS: int = 20          # 前高/前低回看根数
SWING_HALF: int = 3              # 摆动点确认半窗（左右各 3 根）
RANGE_PERIOD_MS: int = 3_600_000  # 区间演化观察线周期：固定 1h

# 区间演化四线（六条价格）在本模块里的名字，与 range/store.py 的列名对应：
#   soft_up / soft_dn  = S 软边界
#   hard_up / hard_dn  = R 反向边界
#   p                  = P 顺向边界（由 direction 决定取 soft_* 哪一侧）
#   key                = K 顺向关键价（列名 v11_key）
RANGE_LINE_KINDS: Tuple[str, ...] = (
    "range_soft_up", "range_soft_dn", "range_hard_up", "range_hard_dn",
    "range_p", "range_key",
)


class Bar(NamedTuple):
    """一根已收盘 K 线。ts 是**开盘时间**（UTC 毫秒，与全仓一致）。"""
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: Optional[float] = None


class Line(NamedTuple):
    """一条观察线在某一刻的读数。"""
    kind: str
    price: float
    period_ms: int
    src_ts: int          # 值来源那根 K 的开盘时间
    ready_ms: int        # 该值最早可用的时刻 = src_ts + period_ms


# ---------------------------------------------------------------- 命名
_LABEL_BY_MS = {180_000: "3m", 300_000: "5m", 900_000: "15m", 1_800_000: "30m",
                3_600_000: "1h", 7_200_000: "2h", 14_400_000: "4h"}


def period_label(ms: int) -> str:
    """周期 -> 线名里的后缀。1h/15m/4h 这种写法必须稳定，它是 line_key 的一部分。"""
    ms = int(ms)
    if ms in _LABEL_BY_MS:
        return _LABEL_BY_MS[ms]
    minutes = ms // 60_000
    if minutes and minutes % 60 == 0:
        return "%dh" % (minutes // 60)
    if minutes:
        return "%dm" % minutes
    if ms % 1000 == 0:
        return "%ds" % (ms // 1000)
    raise ValueError("无法命名的周期: %r ms" % (ms,))


def line_key(kind: str, period_ms: int) -> str:
    """"ema200@15m" / "range_hard_up@1h"。带周期是硬要求，否则两条不同的线会撞成一个。

    注意 line_key 是**下游（第 3 步反应层）唯一的引用键**，改格式等于改库。
    """
    return "%s@%s" % (kind, period_label(period_ms))


# ---------------------------------------------------------------- 均线
def ema_values(closes: Sequence[float], period: int) -> List[float]:
    """标准 EMA：α = 2/(N+1)，种子 = 序列第一根收盘。

    与 ai_trader/web/live.py::ema_series **逐位一致**（那里只是多做了 round(,1) 给前端）。
    看板显示 200 根、内部用 1500 根预热，所以比对时必须喂同样长度的收盘序列，
    否则差的是「种子位置」不是算法（1h 上量级 0.1%）。
    """
    if int(period) <= 0:
        raise ValueError("EMA 周期必须为正: %r" % (period,))
    a = 2.0 / (int(period) + 1)
    out: List[float] = []
    e: Optional[float] = None
    for v in closes:
        v = float(v)
        e = v if e is None else v * a + e * (1.0 - a)
        out.append(e)          # 不取整：存库要全精度，比对时按看板口径 round(,1)
    return out


# ---------------------------------------------------------------- 通道 / 结构位
def donchian_values(highs: Sequence[float], lows: Sequence[float],
                    hi_n: int = DONCHIAN_HI_BARS, lo_n: int = DONCHIAN_LO_BARS
                    ) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """唐奇安上下轨。**不含当根**：up[i] = max(high[i-N:i])，dn[i] = min(low[i-M:i])。

    与 trading/donchian.py 的 state() 同一口径（那里写的是 max(hs[-(n+1):-1])）。
    不含当根不是洁癖：含当根的话「收盘突破上轨」永远不可能成立。
    """
    n = len(highs)
    if n != len(lows):
        raise ValueError("highs/lows 长度不一致: %d vs %d" % (n, len(lows)))
    up: List[Optional[float]] = [None] * n
    dn: List[Optional[float]] = [None] * n
    for i in range(int(hi_n), n):
        up[i] = max(highs[i - int(hi_n):i])
    for i in range(int(lo_n), n):
        dn[i] = min(lows[i - int(lo_n):i])
    return up, dn


def prior_hl_values(highs: Sequence[float], lows: Sequence[float],
                    n: int = PRIOR_HL_BARS
                    ) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """前高/前低 = 前 n 根的最高 / 最低（不含当根）。

    为什么不含当根与前 N 根都定死：口径必须是「已经发生的价格」，
    含当根就等于把当根自己算进参照，参照系会跟着被测对象一起动。
    """
    n_bars = len(highs)
    if n_bars != len(lows):
        raise ValueError("highs/lows 长度不一致: %d vs %d" % (n_bars, len(lows)))
    hi: List[Optional[float]] = [None] * n_bars
    lo: List[Optional[float]] = [None] * n_bars
    for i in range(int(n), n_bars):
        hi[i] = max(highs[i - int(n):i])
        lo[i] = min(lows[i - int(n):i])
    return hi, lo


def swing_values(highs: Sequence[float], lows: Sequence[float], k: int = SWING_HALF
                 ) -> Tuple[List[Optional[float]], List[Optional[float]],
                            List[Optional[int]], List[Optional[int]]]:
    """摆动高低点（严格局部极值），**右侧确认**。

    返回 (swing_high 值, swing_low 值, swing_high 枢轴下标, swing_low 枢轴下标)。

    为什么必须右侧确认：`high[j] 是 ±k 内最高` 这件事要等 j+k 那根收盘才成立。
    如果第 j+1 根就把 j 当枢轴用，等于用了一根还没走完的 K 判断「这里是高点」——
    回测里这是最典型的一处偷未来，而且它在实盘里表现为「事后看很准、当时看不见」。
    所以第 i 根能用的枢轴只有 j <= i-k，且每行落库时记 src_ts = 枢轴时间。

    严格不等号（>）而不是 >=：平顶算不清谁是枢轴，不算比乱算好。
    """
    n = len(highs)
    if n != len(lows):
        raise ValueError("highs/lows 长度不一致: %d vs %d" % (n, len(lows)))
    k = int(k)
    sh: List[Optional[float]] = [None] * n
    sl: List[Optional[float]] = [None] * n
    sh_i: List[Optional[int]] = [None] * n
    sl_i: List[Optional[int]] = [None] * n
    cur_h: Optional[float] = None
    cur_l: Optional[float] = None
    cur_hi: Optional[int] = None
    cur_li: Optional[int] = None
    for i in range(n):
        j = i - k
        if j - k >= 0:                      # 枢轴 j 的右侧 k 根此刻都已收盘
            hj = highs[j]
            if all(hj > highs[j - t] for t in range(1, k + 1)) and \
               all(hj > highs[j + t] for t in range(1, k + 1)):
                cur_h, cur_hi = hj, j
            lj = lows[j]
            if all(lj < lows[j - t] for t in range(1, k + 1)) and \
               all(lj < lows[j + t] for t in range(1, k + 1)):
                cur_l, cur_li = lj, j
        sh[i], sl[i] = cur_h, cur_l
        sh_i[i], sl_i[i] = cur_hi, cur_li
    return sh, sl, sh_i, sl_i


# ---------------------------------------------------------------- 区间演化四线
def range_p(row: Dict) -> Optional[float]:
    """P 顺向边界。DOWN 时是 soft_dn(lo_b)，UP 时是 soft_up(hi_b)。

    与 range/store.py::rows_from_states 的映射**互逆**（那边写进去，这边读回来）：
        DOWN: soft_up=S,  soft_dn=lo_b=P, hard_up=hi_b=R, hard_dn=None
        UP  : soft_up=hi_b=P, soft_dn=S,  hard_up=None,   hard_dn=lo_b=R
    所以 P 不可能是「另外存一列」——它按方向折叠进 soft_* 了。这里还原出来，
    并单独命名成 range_p，避免下游把 P 当成 S（v11 纪律：P 与 K 必须分开命名）。
    """
    d = (row.get("direction") or "").strip().upper()
    if d == "DOWN":
        return row.get("soft_dn")
    if d == "UP":
        return row.get("soft_up")
    return None


def _norm_range_rows(rows: Optional[Sequence]) -> List[Dict]:
    """把 range_evolution 的查询结果统一成 dict。

    接受两种形态（都是 RangeStore 的真实返回/易得形态）：
        dict: ts/soft_up/soft_dn/hard_up/hard_dn/key|v11_key/direction[/family/segment]
        11 元组: (ts, ts_h, soft_up, soft_dn, hard_up, hard_dn, v11_key,
                  family, segment, direction, tag)   <- RangeStore.states() 的行序
    """
    out: List[Dict] = []
    for r in rows or ():
        if isinstance(r, dict):
            d = dict(r)
            if "key" not in d and "v11_key" in d:
                d["key"] = d.get("v11_key")
            out.append(d)
        else:
            t = tuple(r)
            d = {"ts": t[0], "ts_h": t[1] if len(t) > 1 else None,
                 "soft_up": t[2] if len(t) > 2 else None,
                 "soft_dn": t[3] if len(t) > 3 else None,
                 "hard_up": t[4] if len(t) > 4 else None,
                 "hard_dn": t[5] if len(t) > 5 else None,
                 "key": t[6] if len(t) > 6 else None,
                 "family": t[7] if len(t) > 7 else None,
                 "segment": t[8] if len(t) > 8 else None,
                 "direction": t[9] if len(t) > 9 else None,
                 "tag": t[10] if len(t) > 10 else None}
            out.append(d)
    out.sort(key=lambda x: int(x["ts"]))
    return out


def range_ts_list(rows: Sequence[Dict]) -> List[int]:
    """把 1h 行的 ts 提出来，供逐根 bisect 用（不要在每根 K 里重算一遍）。"""
    return [int(r["ts"]) for r in rows]


def range_lines_at(rows: Sequence[Dict], bar_close_ms: int,
                   ts_list: Optional[Sequence[int]] = None
                   ) -> Tuple[Optional[Dict], Optional[int]]:
    """取「截至本根 K 收盘已经收盘的最后一根 1h」的四线。

    返回 (那根 1h 行, 该行 ts)；没有已收盘的 1h 行时返回 (None, None)。

    这就是设计稿 §12 第 1 步那句「1h 线的值在 1h 收盘后才可用于 15m K 线」的落地：
    条件写成 ts + 1h <= bar_close，而不是 ts <= bar_close。
    差这一条，15m 的 12:00 那根就会用上 12:00 那根 1h —— 那根 13:00 才收盘。
    """
    if not rows:
        return None, None
    if ts_list is None:
        ts_list = range_ts_list(rows)
    # 最后一个满足 ts + RANGE_PERIOD_MS <= bar_close_ms 的位置
    idx = bisect_right(ts_list, int(bar_close_ms) - RANGE_PERIOD_MS) - 1
    if idx < 0:
        return None, None
    return rows[idx], int(rows[idx]["ts"])


# ---------------------------------------------------------------- 组装
def _row(symbol: str, interval_ms: int, bar_ts: int, bar_close: float,
         kind: str, price: float, period_ms: int, src_ts: int,
         ready_ms: int) -> Dict:
    dist = (float(bar_close) - float(price)) / float(price) * 100.0 if price else None
    return {"symbol": symbol, "interval_ms": int(interval_ms), "ts": int(bar_ts),
            "line_key": line_key(kind, period_ms), "line_kind": kind,
            "line_period_ms": int(period_ms), "line_price": float(price),
            "bar_close": float(bar_close), "dist_pct": dist,
            "src_ts": int(src_ts), "line_ready_ms": int(ready_ms)}


def build_bar_lines(symbol: str, interval_ms: int, bars: Sequence[Bar],
                    range_rows: Optional[Sequence] = None,
                    *, ema_periods: Sequence[int] = EMA_PERIODS,
                    warmup_bars: int = EMA_WARMUP_BARS,
                    donchian_hi_n: int = DONCHIAN_HI_BARS,
                    donchian_lo_n: int = DONCHIAN_LO_BARS,
                    prior_hl_n: int = PRIOR_HL_BARS,
                    swing_half: int = SWING_HALF,
                    now_ms: Optional[int] = None,
                    require_contiguous: bool = True
                    ) -> Tuple[List[Dict], Dict]:
    """逐根算观察线，返回 (可落库行, 统计)。

    统计里的 skipped 是**强制项**，不是调试信息：
    「禁止静默过滤」这条纪律要求每个没产出的线都说出原因（预热不足 / 窗口不够 /
    枢轴还没确认 / 区间行还没收盘 / 库里该字段就是 NULL）。回填时把它整份打到日志里，
    否则少了一万行也没人知道。

    now_ms: 给了就顺手把**还没收盘**的 K 剔掉（实盘路径用）。没给就认为传进来的
    都已经收盘——这是调用方的责任，与 range/live.py 的 forming 纪律一致。

    require_contiguous: 输入必须等间隔（默认开）。★ EMA 与唐奇安是**沿序列递推**
    的：缺一根，后面所有值都往前错位一格，而算出来的数字**看起来完全合法**。
    以前的防线只在 `forward`（表3）那一层有等间隔检查，最上游的 lines 反而没有 ——
    于是洞从最底层就开始污染，越往下越查不出来。带洞输入请走 `ingest_kbar.py`
    的连续性闸去补，不要在这里关掉。
    """
    bars = [b if isinstance(b, Bar) else Bar(*b) for b in (bars or ())]
    bars.sort(key=lambda b: int(b.ts))
    interval_ms = int(interval_ms)

    # ---- 等间隔断言 ----
    # 只在真的多于一根时查；查出来就抛，**不降级成警告**。
    # 理由：EMA/唐奇安的错值没有任何外部特征，喂进去一个洞，出来一串
    # 「合法但错位」的数，然后在图片和统计里长得跟真的一样。
    if require_contiguous and len(bars) > 1:
        for _i in range(1, len(bars)):
            _d = int(bars[_i].ts) - int(bars[_i - 1].ts)
            if _d != interval_ms:
                raise ValueError(
                    "K 线不等间隔：第 %d 根 ts=%d 与第 %d 根 ts=%d 相差 %d ms，"
                    "应为 %d ms（缺 %d 根）。EMA/唐奇安是沿序列递推的，"
                    "跨洞算出来的值看起来合法但是错位的 —— 先把洞补上，"
                    "确认要在带洞数据上硬算请显式传 require_contiguous=False。"
                    % (_i - 1, int(bars[_i - 1].ts), _i, int(bars[_i].ts),
                       _d, interval_ms, max(0, _d // interval_ms - 1)))

    rrows = _norm_range_rows(range_rows)
    stats: Dict[str, object] = {
        "symbol": symbol, "interval_ms": interval_ms,
        "bars_in": len(bars), "bars_used": 0, "bars_forming": 0,
        "range_rows": len(rrows), "lines": 0,
        "by_kind": {}, "skipped": {},
    }
    by_kind: Dict[str, int] = {}
    skipped: Dict[str, int] = {}

    def skip(kind: str, reason: str) -> None:
        k = "%s:%s" % (kind, reason)
        skipped[k] = skipped.get(k, 0) + 1

    # 预热不足：EMA 是递推量，序列里它之前不足 warmup 根就不落，且必须报数。
    if warmup_bars and warmup_bars > 0 and len(bars) < int(warmup_bars):
        stats["warmup_short"] = True

    # 收盘过滤：把还没收盘的最后一根（最多一根）剔掉
    used = bars
    if now_ms is not None:
        closed = [b for b in bars if int(b.ts) + interval_ms <= int(now_ms)]
        stats["bars_forming"] = len(bars) - len(closed)
        used = closed
    stats["bars_used"] = len(used)
    if not used:
        stats["by_kind"], stats["skipped"] = by_kind, skipped
        return [], stats

    closes = [float(b.c) for b in used]
    highs = [float(b.h) for b in used]
    lows = [float(b.l) for b in used]
    ts_list = [int(b.ts) for b in used]

    rts_all = range_ts_list(rrows)
    ema_by_period = {int(p): ema_values(closes, int(p)) for p in ema_periods}
    up, dn = donchian_values(highs, lows, donchian_hi_n, donchian_lo_n)
    phi, plo = prior_hl_values(highs, lows, prior_hl_n)
    sh, sl, sh_i, sl_i = swing_values(highs, lows, swing_half)

    out: List[Dict] = []

    def emit(bar_ts: int, bar_close: float, kind: str, price, period_ms: int,
             src_ts: int, ready_ms: int) -> None:
        out.append(_row(symbol, interval_ms, bar_ts, bar_close, kind,
                        float(price), period_ms, int(src_ts), int(ready_ms)))
        by_kind[kind] = by_kind.get(kind, 0) + 1

    for i, b in enumerate(used):
        bar_close_ms = int(b.ts) + interval_ms
        close = float(b.c)

        # ---- 均线：与 K 线同周期。预热不足不落（并报数），避免交出未收敛的线
        for p in ema_periods:
            p = int(p)
            kind = "ema%d" % p
            if (i + 1) < int(warmup_bars or 0):
                skip(kind, "warmup")
                continue
            emit(b.ts, close, kind, ema_by_period[p][i], interval_ms,
                 b.ts, bar_close_ms)

        # ---- 唐奇安上下轨：与 K 线同周期，不含当根
        if up[i] is None:
            skip("donchian_hi", "window")
        else:
            emit(b.ts, close, "donchian_hi", up[i], interval_ms, b.ts, bar_close_ms)
        if dn[i] is None:
            skip("donchian_lo", "window")
        else:
            emit(b.ts, close, "donchian_lo", dn[i], interval_ms, b.ts, bar_close_ms)

        # ---- 前高/前低：前 N 根极值
        if phi[i] is None:
            skip("prior_high", "window")
        else:
            emit(b.ts, close, "prior_high", phi[i], interval_ms, b.ts, bar_close_ms)
        if plo[i] is None:
            skip("prior_low", "window")
        else:
            emit(b.ts, close, "prior_low", plo[i], interval_ms, b.ts, bar_close_ms)

        # ---- 摆动高低点：src 是枢轴那根，ready = 枢轴 + (k+1) 根
        if sh[i] is None:
            skip("swing_high", "no_pivot")
        else:
            pivot_ts = ts_list[int(sh_i[i])]
            emit(b.ts, close, "swing_high", sh[i], interval_ms, pivot_ts,
                 pivot_ts + (int(swing_half) + 1) * interval_ms)
        if sl[i] is None:
            skip("swing_low", "no_pivot")
        else:
            pivot_ts = ts_list[int(sl_i[i])]
            emit(b.ts, close, "swing_low", sl[i], interval_ms, pivot_ts,
                 pivot_ts + (int(swing_half) + 1) * interval_ms)

        # ---- 区间演化四线：固定 1h，只读库里那根已收盘的 1h 行
        rrow, rts = range_lines_at(rrows, bar_close_ms, rts_all)
        if rrow is None:
            for kind in RANGE_LINE_KINDS:
                skip(kind, "range_not_closed")
        else:
            vals = {"range_soft_up": rrow.get("soft_up"),
                    "range_soft_dn": rrow.get("soft_dn"),
                    "range_hard_up": rrow.get("hard_up"),
                    "range_hard_dn": rrow.get("hard_dn"),
                    "range_p": range_p(rrow),
                    "range_key": rrow.get("key")}
            for kind in RANGE_LINE_KINDS:
                v = vals[kind]
                if v is None:
                    skip(kind, "null_in_db")   # 库该字段就是 NULL（未成形/该方向没有）
                    continue
                emit(b.ts, close, kind, v, RANGE_PERIOD_MS, rts,
                     int(rts) + RANGE_PERIOD_MS)

    # ---- 防偷未来：一处集中校验，任何一条线在它自己收盘前被用了就抛
    for row in out:
        bar_close_ms = int(row["ts"]) + interval_ms
        if int(row["line_ready_ms"]) > bar_close_ms:
            raise RuntimeError(
                "偷未来：%s 在 %d 就用了 %s（ready=%d）"
                % (row["line_key"], int(row["ts"]), row["line_key"],
                   int(row["line_ready_ms"])))

    stats["lines"] = len(out)
    stats["by_kind"] = by_kind
    stats["skipped"] = skipped
    return out, stats
