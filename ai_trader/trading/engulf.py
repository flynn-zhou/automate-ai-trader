# -*- coding: utf-8 -*-
"""实体反包三档限价策略（15m）—— 独立于原有「窗口闸」三条形态。

★ 策略名（用户 2026-11-26 定）：**15m反包K**（`KIND_ENGULF`）。
  下面正文里的「实体反包」是形态描述，名字以 `KIND_ENGULF` 为准。

这是什么
--------

用户 2026-11-25/26 口径：15m 上出现**实体反包**（前一根阴、当根阳，且当根
实体**包住**前一根实体）时，把仓位分成三份挂限价单吃回调：

    第一份  实体 80% 位置   目标 1R
    第二份  实体 50% 位置   目标 2R
    第三份  实体 30% 位置   目标 3R
    止损    反包K 最低点 - 1tick（三档共用）
    1R      = 反包K 收盘 - 最低点

每份**各自独立出场**：第一份 1R 平、第二份 2R 平、第三份 3R 平。
（早期版本让三档共用「1R 平一半、2R 全平」，第三份在 2R 就被砍掉，
 用户当面指出这是实现错位 —— 见下方「回测证据」第二行。）

为什么要两个过滤器
------------------

反包形态本身**毛期望为正、净期望为负**，死因是手续费：

    15m  均1R 188.6 点  费/1R 0.263R  →  总R净 -112.60
    1h   均1R 382.8 点  费/1R 0.129R  →  总R净  -10.12
    4h   均1R 735.8 点  费/1R 0.068R  →  总R净   +4.35

毛 R 基本持平（+40 / +3 / +7），费/1R 单调下降，净值随之翻正。
**形态的原始优势一直在，是手续费把它磨掉的。**

用户明确「不想换 1h，先测 15m」，所以在 15m 上把「1R 太小、费用占比太高」
的样本直接过滤掉：

① **手续费倍数**：``1R >= MIN_R_COST_MULT x 价格 x ROUND_TRIP_FEE``。
   这是决定性杠杆 —— 30 天窗口下「最小风险门槛」从 0x 扫到 10x 费，
   净值单调从 -87.46R 爬到 +2.79R（12 个独立配置的一致梯度）。

② **波动率近期性**：近 20 根振幅的头部 10% 必须落在**最近 10 根**内。
   含义：波动率刚刚扩张过，不是在 15 根之前（那时反包已经过期了）。
   用户原话：「没有波动率的时候或者波动率太远了就都不用这个策略」。

回测证据（202 天真实 15m K 库，`tools/bt_engulf4.py`）
------------------------------------------------------

    只做多 · 止损不放宽 · 风险>=6x费 · 波动率过滤
    202 天  130 笔  胜率 54%  毛 +0.190R/笔  费 -0.104R/笔  净 +0.086R/笔

★ 必须知道的三个限制（写在这里，防止后来者把它当成"已证明赚钱"）：

  1. 优势**集中在最近 30 天**（+0.239R/笔）；前 172 天只有 +0.030R，几乎打平。
     这是**特定行情策略**（下跌反弹），不是全天候策略。
  2. 130 笔样本的均 R 标准误差约 ±0.09R，**+0.086 的置信区间跨过 0**。
     准确表述是「没有被证伪」，不是「已证明有正期望」。
  3. 「止损放宽 30%」那个变体在 202 天样本外是 **-0.011**（前 172 天 -0.103），
     属于追最近行情，**已否决**。不要因为"放宽止损能降费/R"就重新引入。

为什么单独一个模块、而不是加进 `pending_plan.detect_all`
-------------------------------------------------------

`detect_all` 里三条形态都强制过**开仓窗口闸**（`WINDOW_GATE`），而反包是
**无背景**的价格形态，不需要窗口。硬塞进去要么放开窗口闸（那会让原有三条
形态的计划数暴涨，正是历史上踩过的坑），要么写一堆 `if kind == ...` 分支。

所以这里独立实现，在 `plan_source.plans()` 里**追加**在 `detect_all` 之后。
对已上线的 VOL-1 三条形态**零改动、零回归**。

一条信号产出**三条** Plan（三档），它们靠 `plan_key` 里的 `order_price` 区分。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import pending_plan as P
from . import thesis as T

#: 形态名。**不要**加「下跌/上涨」前缀 —— 它是纯价格形态，与背景方向无关；
#: 加上前缀会被 `_variant_name` 当成镜像版改名，统计表会分不清。
#:
#: ★ 2026-11-26 用户定名：**15m反包K**（原名「实体反包三档限价单」）。
#:   「15m」写进名字里是有信息量的：这条形态跑在 15m 上（`BAR_SPACE`），
#:   而 1h/4h 的费/1R 完全不同 —— 见模块 docstring 的三行回测证据。
#:   名字里不带周期的话，其实盘成交和回测样本很容易被当成同一口径。
#:
#: ★ 改名是**纯字符串**，所有引用都走这个常量（全仓无硬编码），
#:   生产库改名时 0 条反包历史行，所以不存在统计分裂、也不需要迁移。
KIND_ENGULF = "15m反包K"

#: 三档入场位置（反包K 实体的百分比）与各自的目标 R 倍数。
#: ★ 两个元组必须等长，`_check_tranches` 会在 import 时校验。
TRANCHE_LEVELS: Tuple[float, ...] = (0.8, 0.5, 0.3)
TRANCHE_TARGETS: Tuple[float, ...] = (1.0, 2.0, 3.0)

#: 每档承担的风险占比。三档同时挂出 → 合计 1R。
#:
#: ★★ 这是**生产安全红线**：引擎的 `_pending_size` 默认让每一张单承担
#:    全额风险（`risk_pct`=10.10%）。三档不做分摊就是 30.30%，
#:    一次反包打掉三分之一本金。见 `RISK_SCALE` 在计划证据里的传递。
RISK_SCALE = 1.0 / float(len(TRANCHE_LEVELS))

#: 1R 至少要等于几倍往返手续费。这是决定性过滤器，见模块 docstring。
MIN_R_COST_MULT = 6.0

#: 往返手续费率：入场限价 maker 0.02% + 出场 taker 0.05%。
#: ★ 与 `tools/bt_engulf4.py` 的 FEE_RT 必须**同值**，否则回测与实盘脱节。
ROUND_TRIP_FEE = 0.0002 + 0.0005

#: 波动率近期性：回看窗口根数 / 最大振幅必须落在最近几根内。
#: ★ 判据是「最大振幅的位置」，不是分位数阈值 —— 见 `vol_recency_ok`。
VOL_WIN = 20
VOL_RECENT = 10

#: 止损距最低点几个 tick。
STOP_TICKS = 1.0

#: 声明那根 K 属于哪条序列。
BAR_SPACE = "15m"


def _check_tranches() -> None:
    """import 时校验档位配置自洽 —— 配置错位会让风险敞口静默翻倍。"""
    if len(TRANCHE_LEVELS) != len(TRANCHE_TARGETS):
        raise ValueError("TRANCHE_LEVELS 与 TRANCHE_TARGETS 长度必须相同")
    if not all(0.0 < lv < 1.0 for lv in TRANCHE_LEVELS):
        raise ValueError("档位必须在 (0,1) 开区间内：%r" % (TRANCHE_LEVELS,))
    if len(set(TRANCHE_LEVELS)) != len(TRANCHE_LEVELS):
        raise ValueError("档位不能重复：%r" % (TRANCHE_LEVELS,))
    # ★ 目标必须**随入场深度单调递减**：越深（越靠近最低点）目标越远。
    #   反过来写不会报错，只会让第三份拿 1R、第一份拿 3R —— 逻辑全反。
    ordered = sorted(zip(TRANCHE_LEVELS, TRANCHE_TARGETS), reverse=True)
    targets = [t for _, t in ordered]
    if targets != sorted(targets):
        raise ValueError("目标 R 必须随入场深度单调递增，收到 %r"
                         % (TRANCHE_TARGETS,))


_check_tranches()


def risk_scale_for(kind: Any) -> float:
    """这条计划的手数要乘的风险系数。**唯一来源**。

    ★ 为什么做成函数而不是让引擎写死 `1/3`：

      引擎的 `_pending_size()` 默认让每一张单承担**全额**风险
      （`sizing.risk_pct`，生产是 10.10%）。反包一次挂三档，
      不摊就是 **30.30% 本金敞口** —— 一次打掉三分之一。

      而 `kind` 是**已经落库**的字段，所以系数可以从它推出来，
      不需要给 `Thesis` 加列、不需要数据库迁移。代价是这里多一个分支，
      所以把判据收在**这一个函数**里，引擎只负责调用。

    Args:
        kind: `Plan.kind` 或 `Thesis.kind`。给 ``None`` / 其它形态 → 1.0。
    """
    return float(RISK_SCALE) if kind == KIND_ENGULF else 1.0


# ---------------------------------------------------------------- 形态判定
def is_up_engulfing(prev: Any, cur: Any) -> bool:
    """实体反包（做多）：前一根阴、当根阳，且当根实体**包住**前一根实体。

    ★★ 用 `<=` / `>=` 而不是 `<` / `>`。

      连续市场里「今开 == 前一根收盘」是**常态**，不是例外。用严格不等号
      会把下面这两根标准反包全部漏掉（实测由用户人工核对发现）：

          11-25 22:45  前收 57357.3  今开 57357.3  ← 相等
          11-26 09:00  前收 60496.0  今开 60496.0  ← 相等

      判定条件（四条缺一不可）：
        1. 前一根收 < 开（阴）
        2. 当根  收 > 开（阳）
        3. 当根  收 >= 前一根开（向上吃穿上沿）
        4. 当根  开 <= 前一根收（向下吃穿下沿）
    """
    return (float(prev.c) < float(prev.o)
            and float(cur.c) > float(cur.o)
            and float(cur.c) >= float(prev.o)
            and float(cur.o) <= float(prev.c))


def is_down_engulfing(prev: Any, cur: Any) -> bool:
    """实体反包（做空）：镜像。当前**未启用**，保留给后续镜像实验。"""
    return (float(prev.c) > float(prev.o)
            and float(cur.c) < float(cur.o)
            and float(cur.c) <= float(prev.o)
            and float(cur.o) >= float(prev.c))


# ---------------------------------------------------------------- 过滤器
def vol_recency_ok(bars: Sequence[Any], i: int,
                   win: int = VOL_WIN,
                   recent: int = VOL_RECENT) -> bool:
    """② 波动率近期性：近 `win` 根里**最大的那根振幅**必须出现在最近 `recent` 根内。

    用户口径：「进 10 根 K 拥有近 20 根中头部 10% 最大的波动率出现的条件」，
    「没有波动率的时候或者波动率太远了就都不用这个策略」。

    ★★ 判据是**最大振幅所在的位置**，不是「分位数阈值比较」。

      第一版写成「排序取 (1-top) 分位当阈值，最近 N 根里有没有振幅 >= 阈值」，
      结果退化成一个几乎恒真的过滤器：一根尖峰 + 十九根平盘时，
      第 18 个分位恰好等于平盘值，于是最近十根里随便一根都"过"
      （实测通过率 76.3%，等于没过滤）。而且它不是用户说的那个意思 ——
      用户要的是「**那次波动率扩张**发生在最近」，即尖峰本身要新。

    ★ 没有波动率（振幅全为 0）→ False，对应「没有波动率的时候不用」。
    """
    lo = max(0, int(i) - int(win) + 1)
    if int(i) - lo + 1 < win:
        return False
    amps = [float(bars[k].h) - float(bars[k].l) for k in range(lo, int(i) + 1)]
    if not amps or max(amps) <= 0:
        return False
    idx_max = lo + amps.index(max(amps))
    r0 = int(i) - int(recent) + 1
    return idx_max >= r0


def r_cost_ok(price: float, r_points: float,
              mult: float = MIN_R_COST_MULT,
              fee: float = ROUND_TRIP_FEE) -> bool:
    """① 1R 是否至少是 `mult` 倍往返手续费。

    ★ 这是决定性过滤器：费用是固定点数，1R 越小，费/1R 越大。
      实测 15m 上 1R 中位数只有 ~190 点，而手续费 ~42 点 —— 费/1R 0.22R，
      形态的毛优势（+0.19R）刚好被吃掉。
    """
    if price <= 0 or r_points <= 0:
        return False
    return float(r_points) >= float(mult) * float(price) * float(fee)


# ---------------------------------------------------------------- 定价
def _tick(params: Optional[P.PendingParams]) -> float:
    t = float(getattr(params, "tick_size", 0.0) or 0.0)
    return t if t > 0 else 0.1


def tranche_prices(bar: Any, params: Optional[P.PendingParams] = None
                   ) -> List[Dict[str, float]]:
    """把一根反包K 拆成三档 ``(档位, 挂单价, 止损, 止盈, 1R)``。

    做多口径（`bar` 必须是阳线，即 `c > o`）：

        1R     = 收盘 - 最低点
        止损   = 最低点 - 1 tick          （三档共用）
        挂单_k = 开盘 + 档位_k x 实体      （实体 = 收 - 开）
        止盈_k = 挂单_k + 目标_k x 1R

    ★ 三档的**挂单价各不相同**，所以 `plan_source.plan_key` 能把它们区分开，
      不会互相去重。用同一个挂单价就会退化成一条（= 三分之一仓位）。
    """
    o, h, l, c = (float(bar.o), float(bar.h),
                   float(bar.l), float(bar.c))
    body = c - o
    r1 = c - l
    if body <= 0 or r1 <= 0:
        return []
    tick = _tick(params)
    stop = l - STOP_TICKS * tick
    out = []
    for level, mult in zip(TRANCHE_LEVELS, TRANCHE_TARGETS):
        entry = o + float(level) * body
        target = entry + float(mult) * r1
        out.append({"level": float(level), "r_mult": float(mult),
                    "entry": entry, "stop": stop, "target": target,
                    "r1": r1, "tick": tick})
    return out


# ---------------------------------------------------------------- 产出计划
def detect_engulf(i: int, ctx: P.PlanContext,
                  params: Optional[P.PendingParams] = None,
                  direction: str = T.UP) -> Tuple[List[P.Plan], str]:
    """在第 `i` 根 15m 上产出反包三档计划。返回 ``(plans, why)``。

    只做多（`direction=T.UP`）。做空镜像**未启用** —— 回测显示只做多
    均 R +0.086、上下都做 +0.042，多头明显更好，且符合用户「下跌反弹」定位。

    ``why`` 是**未成立原因**（项目纪律：禁止静默过滤）。成立时为空串。
    """
    bars = ctx.bars15
    n = len(bars)
    if i < 1 or i >= n:
        return [], "no_bar"
    prev, cur = bars[i - 1], bars[i]
    if direction != T.UP:
        return [], "direction_not_supported"
    if not is_up_engulfing(prev, cur):
        return [], "no_engulf"
    # ① 手续费倍数
    r1 = float(cur.c) - float(cur.l)
    if not r_cost_ok(float(cur.c), r1):
        return [], "r_too_small_vs_fee"
    # ② 波动率近期性
    if not vol_recency_ok(bars, i):
        return [], "vol_not_recent"
    prices = tranche_prices(cur, params)
    if not prices:
        return [], "bad_bar"
    plans: List[P.Plan] = []
    for t in prices:
        plan = P.Plan(
            kind=KIND_ENGULF,
            direction=T.UP,
            order_type=T.ORDER_LIMIT,
            bar_index=int(i),
            bar_ts=int(cur.ts),
            order_price=float(t["entry"]),
            stop_price=float(t["stop"]),
            target_price=float(t["target"]),
            bar_space=BAR_SPACE,
            # 反包是**反转族**（大跌抄底做多），失效判据只看止损价。
            family=T.FAMILY_REVERSAL,
            claim_ts=0,          # 0 = 用 bar_ts
            claim_on_entry=False,  # 15m 收盘声明
            evidence={
                "pattern": "entity_engulfing_15m",
                "tranche_level": t["level"],
                "target_r": t["r_mult"],
                "r1_points": t["r1"],
                # ★★ 必填：`to_thesis()` 用 `stop_distance` 算挂单期的
                #    **跑偏撤销距离**（`stale_distance = stop_distance x
                #    stale_stop_mult`）。漏了它 → stale_distance = 0 →
                #    「挂单期跑偏永不撤销」，计划一直挂着直到 horizon 过期，
                #    价格早就跑掉了还照价成交。这是 2026-11-17 修过的同一个洞
                #    （生产库 22/22 行全为 0），别再漏第二次。
                #
                #    口径：**价格点数**（abs(挂单价 - 止损价)），不是百分比。
                #    三档挂单价不同 → 各自的跑偏距离也不同，这是对的。
                "stop_distance": abs(float(t["entry"]) - float(t["stop"])),
                "r_cost_mult": MIN_R_COST_MULT,
                "round_trip_fee": ROUND_TRIP_FEE,
                # ★★ 生产安全红线：引擎按这个系数摊风险。
                #    丢了它 → 三档各吃满额风险 → 敞口 3 倍。
                "risk_scale": RISK_SCALE,
                "prev_bar": {"o": float(prev.o), "h": float(prev.h),
                             "l": float(prev.l), "c": float(prev.c)},
                "vol_recency": {"win": VOL_WIN, "recent": VOL_RECENT},
            })
        plan.validate(ref_price=float(cur.c))
        plans.append(plan)
    return plans, ""


def detect(ctx: P.PlanContext,
           params: Optional[P.PendingParams] = None,
           direction: str = T.UP
           ) -> Tuple[List[P.Plan], Dict[str, int]]:
    """扫全 15m 序列，返回 ``(计划列表, 未成立原因计数)``。

    ★ 不做去重：三档靠 `order_price` 天然不同，同一根 K 上不会重复。
      跨根去重由引擎的 `_pending_harvest`（按 `claim_ts == 当前15m`）负责。
    """
    plans: List[P.Plan] = []
    stats: Dict[str, int] = {}
    bars = ctx.bars15 or []
    for i in range(len(bars)):
        sub, why = detect_engulf(i, ctx, params, direction)
        key = "%s:%s" % (KIND_ENGULF, why or "ok")
        stats[key] = stats.get(key, 0) + 1
        plans.extend(sub)
    return plans, stats
