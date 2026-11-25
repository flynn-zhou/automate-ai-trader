# -*- coding: utf-8 -*-
"""预期-现实管理（thesis）：把「我们以为 K 线会怎么走」做成可判定、可回溯的对象。

为什么要有这一层
---------------
用户原话（2026-11-17）：

    我们都是在之前就预期它会这么走，符合预期我们就挂单了，不符合预期就不挂，
    接到单之后，现实不符合预期就走。

所以真实机制不是「等信号出现再市价追」，而是：

    声明预期 ──> 现实符合 ──> 提前挂限价单 ──> 价格走到 ──> 成交
                    │                              │
                    └─ 现实不符合 ─> 不挂            └─ 成交后现实不符合 ─> 走

不把「预期」做成显式对象，三种形态必然把预期隐式摊在 if-else 里：
以后任何一个口径变化都要改三处，而且**改漏了不会报错**。

术语：用 thesis（论点），不用 expectation。原因是交易语境里
`expectancy` 指的是**期望收益统计**（E = 胜率×均盈 − 败率×均亏），
和这里的「预期」不是一个东西；混淆了以后读代码的人会算收益去。

设计约束
--------
1. **纯函数。** `run()` 只吃 (thesis, bars, line_book)，不碰 DB、不碰网络、
   不读时钟。这样既能在几万根历史上跑，也能一条一条钉测试。

2. **判定是三值，不是布尔。**
   `pending`（现在下不了结论）/ `satisfied`（走成了）/ `violated`（打脸了）。
   二值逻辑会逼你在证据不足时下结论，于是要么过早挂单（够不到），
   要么过早撤单（刚撤就成立）—— 这就是「判定抖动」的来源。
   借的是运行时验证里的 LTL₃ 三值语义（Bauer/Leucker/Schallhart）。

3. **判定必须带证据。** 每次转换都记 `evidence`：判在哪根 K 线、
   用了哪条线的哪个值、阈值是多少。没有证据的判定等于「事后随便解释」，
   回测里看不出来，实盘里也复盘不了。

4. **INVALIDATED 与 CANCELLED 必须分开。**
   前者是「预期压根没成立，所以**根本没挂过单**」，
   后者是「挂上了，但预期破功，所以撤单」。
   两者搞混的表现是「该撤的没撤」或「该挂的没挂」，都不会报错。

5. **同一根 K 线内的先后顺序一律取保守侧。**
   OHLC 没有 bar 内顺序，这是数据本身的缺陷，不是可以绕过去的实现细节。
   所以：同根既成交又失效 → 按**先成交再失效平掉**（多亏一点）；
   同根既打止损又打止盈 → 按**先打止损**（多亏一点）。
   宁可回测难看，也不要一个好看但靠运气加出来的曲线。

失效判据（用户 2026-11-17 定，见交接文档 §18.5）
------------------------------------------------
**「有效」= 收盘价。影线完全不参与。**
用户原话：「收盘价穿过了，收盘了，跟插针有什么关系？再 15m 的 K 线收盘前，
向上插针 10 次都过了 15m 的 ema20，都无所谓，只要收盘那一刻价格在
ema20 下面，就算无效！」

所以这里**一个容差都没有** —— 没有点数、没有百分比、没有量比。
判定退化成比较：`close(15m) vs ema20`。少一组容差就少一处能调歪的地方。

两条路径，速度不同、**独立结算**：

    慢（本周期）: 某根 15m 收盘站上 ema20@15m，之后**只认第 3 根** ——
                  第 3 根收盘仍在 ema20 上方 → 失效
    快（大一级）: 任意一根 15m 收盘站上 ema20@30m → 立即失效，不等第 3 根

★ 慢路径是「跳读」，不是滚动窗口。用户：「第三根跌破就认第三根，只认第 3 根！」
中间第 1、2 根无论怎么走都不看、不重置计数。所以实现上它就是一个
跨 3 根的跳读比较，**不需要维护任何计数器** —— 有计数器就会有人给它加重置规则。

★ 快路径**自己就是 15m 站稳的确认**，不需要再拿更大周期去确认它。
用户：「30m 的有效站稳不用确认，这里是对 15m 的站稳的确认，
不是需要大一级周期的有效确认哦！」

EMA 参照价用**当根那根 K 自己的 ema20**（移动参照）。拿第一根的值当固定参照，
下跌途中 ema20 一路下行，价格没涨也会「站上去」—— 那是假的。
（这条同时回答了待确定清单的 D-20：用移动参照。）

关于「为什么不走 pick_prev」
--------------------------
`kbar/reaction.py::pick_prev` 规定反应层只能取**上一根**那一行。那是对的：
反应层判断的是「本根有没有碰到某条**事先存在**的线」，所以线值必须是
本根开盘前就已知的那个。

本模块判的是 `close(t) vs ema20(t)` —— 两个值都在**同一时刻**（本根收盘）
才定下来，属于同时刻比较，不是偷未来。`kbar_line.line_ready_ms` 对
ema20@15m 恰好就等于本根收盘时刻。所以这里用「截至收盘时刻已就绪的最新值」
（as-of，带 `line_ready_ms <= close_ms` 硬过滤），而不是 pick_prev。
两个口径不同，各有各的理由，别互相套用。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import break_rules as _br

# ---------------------------------------------------------------- 口径常量

#: 慢路径确认用第几根（用户：「只认第 3 根」）。1-based 语义：站上之后数到第 3 根。
SLOW_CONFIRM_BARS: int = 3

#: 本周期（判断「站不站上」的那条线）
SLOW_LINE_KEY: str = "ema20@15m"
#: 大一个级别（快路径）
FAST_LINE_KEY: str = "ema20@30m"

#: 趋势背景看的两条线：快线 ema20 / 慢线 ema50，**都在 15m 上**。
#:
#: ★ 用户 2026-11-20：
#:   「下跌时 ema20 是快线，在 ema50 下面才是正常的，假如在 ema50 上面，
#:     说明在尝试反转，那就是别的策略开单，三引线和倒V不开单，上涨镜像。」
#:   「三引线是 ema20 和 ema50 有同向趋势且相距越走越宽，这时反弹会相对
#:     强劲一些……不是标准的三引线买法，反弹也会弱一些。」
#:
#: ★ 放在这里（而不是 pending_plan）的理由：`main.py` 的装线名单要从
#:   **消费方自己的常量**推导。常量散落在两个模块，装线名单就会漏 ——
#:   ema50@15m 漏装正是这么发生的。
TREND_FAST_KEY: str = SLOW_LINE_KEY          # ema20@15m
TREND_SLOW_KEY: str = "ema50@15m"

#: 15m 的 ema144 —— 第三条可以「被多次拒绝」的线。
#:
#: ★ 用户 2026-11-20：
#:   「下跌反弹时 ema20 多次拒绝开空，同样适用于 ema50，不知道你实现了没有，
#:     ema 没有被多次拒绝，被突破后行情来到 ema50，ema50 出现多次拒绝也是
#:     同样的！上涨回调镜像」
#:   追问后明确：**ema144 也算**（「ema20 + ema50 + ema144 都算」），
#:   且**不要求**「先被 ema20 拒绝过」这个先后顺序。
#:
#: ★ 为什么这个常量也放在这里：它同时被
#:   ① `OBSERVATION_KEYS`（观察线出场）、
#:   ② `pending_plan.REJECT_LINE_KEYS`（EMA 拒绝的候选线）
#:   两处消费，而两处都要求「装线名单从消费方常量推导」。
#:   以前 `"ema144@15m"` 是字面量散落的，改一处另一处静默不跟。
EMA144_LINE_KEY: str = "ema144@15m"

#: 15m 的周期（毫秒）。判据里的「收盘时刻」= bar.ts + 15m。
BAR_INTERVAL_MS: int = 900_000

#: 方向
DOWN = "down"   # 预期下跌（做空、挂 SELL 限价在上方）
UP = "up"       # 预期上涨（做多、挂 BUY 限价在下方）

#: ★ 策略家族 —— **失效判据的轴是家族，不是方向**。
#:
#: 用户 2026-11-17 的口径：
#:
#:   * ``reversal``（反转：大跌抄底做多 / 上涨摸顶做空）
#:     → **只看止损价，不看 ema**。理由：这类形态一出生就在 ema 的另一侧
#:       （抄底在 ema20 下方），「收盘跌破 ema」一出生就成立，
#:       拿它判失效等于每条都秒死 —— 而且不报错。
#:   * ``trend``（趋势：下跌反弹做空 / 上涨回调做多）
#:     → **ema 打脸就是失效**。价格站回 ema 说明趋势没在继续，
#:       这条预期本来赌的就是「趋势继续」。
#:
#: ★ 为什么不能用 `direction == UP` 代替：镜像上线后，反转有**做空**的
#:   （上涨里的摸顶），趋势有**做多**的（上涨里的回调）。
#:   按方向判会让反转空单被 ema 白杀、趋势多单被白豁免 —— 两头都错，
#:   而且两边看起来都很正常（HANDOFF §23.23）。
FAMILY_REVERSAL = "reversal"
FAMILY_TREND = "trend"

#: 状态
EXPECTED = "EXPECTED"
ARMED = "ARMED"
FILLED = "FILLED"
INVALIDATED = "INVALIDATED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
CLOSED_TP = "CLOSED_TP"
CLOSED_SL = "CLOSED_SL"
CLOSED_THESIS = "CLOSED_THESIS"
#: ★ 保本棘轮出场（S5）。**专门一个状态，不往别人身上挤**。
#:
#: 它既不是止损（没亏钱），也不是止盈（没到 4R），也不是「预期被现实否决」
#: （预期一度是对的，已经赚到 1R）。归到任何一个已有桶都会静默改变那个桶的统计，
#: 而没有人会发现 —— 这正是项目禁的「静默归档」。
CLOSED_BREAKEVEN = "CLOSED_BREAKEVEN"

STATES = (EXPECTED, ARMED, FILLED, INVALIDATED, CANCELLED, EXPIRED,
          CLOSED_TP, CLOSED_SL, CLOSED_THESIS, CLOSED_BREAKEVEN)

TERMINAL_STATES = frozenset(
    {INVALIDATED, CANCELLED, EXPIRED, CLOSED_TP, CLOSED_SL, CLOSED_THESIS,
     CLOSED_BREAKEVEN})

#: 三值判定
PENDING = "pending"
SATISFIED = "satisfied"
VIOLATED = "violated"

#: 订单类型
#:
#: 为什么必须显式建模：同一个价位、不同订单类型，成交条件**完全相反**。
#:   LIMIT 挂在正确一侧，等价格「走到」它；
#:   STOP  是「穿越触发」，价格必须突破它才启动。
#: 用户 2026-11-17 定的：三引线 = LIMIT（等回落接），
#: EMA 拒绝 / 倒 V = STOP（突破才进，让价格先证明方向）。
ORDER_LIMIT = "LIMIT"
ORDER_STOP = "STOP"
ORDER_TYPES = (ORDER_LIMIT, ORDER_STOP)


def triggers_when_price_falls(direction: str, order_type: str) -> bool:
    """这笔单是不是「价格往下走到/穿过挂单价」才成交。

    ★ 这是 **(方向, 类型) → 成交侧** 的**唯一**一份实现。
      `Thesis.triggers_when_price_falls` 与
      `pending_plan.Plan.triggers_when_price_falls` 都委托给它。

      为什么必须只有一份：方向/类型判反是这一层最经典的 bug，
      而两边各写一遍时「改了这边忘了那边」不会有任何测试报警 ——
      只有真实成交序列会不对，而那已经是策略结果层面的事了。

    | 方向 | 类型 | 成交侧 |
    |---|---|---|
    | DOWN | LIMIT | 往上（反弹到那儿卖） |
    | DOWN | STOP  | 往下（跌破才追空） |
    | UP   | LIMIT | 往下（回落到那儿买） |
    | UP   | STOP  | 往上（涨破才追多） |
    """
    if order_type == ORDER_STOP:
        return direction == DOWN
    return direction == UP

#: 同根 K 线内既打到止损又打到止盈时，按止损先（保守）。
STOP_BEFORE_TARGET: bool = True

#: 同根 K 线内既成交又触发失效时，按先成交再平掉（保守：多亏一点）。
#: 反过来（先失效→不成交）等于「事后挑掉坏单」，那是回测作弊。
#:
#: ★★ 但「按先成交算」**不等于这件事可判定**（2026-11-17 补）。
#:   5m / 15m 的 OHLC 都没有 bar 内顺序：同一根里既触及挂单价、又收盘穿过
#:   ema 时，**到底哪个先发生是不可知的**。以前这里默默按「先成交」算，
#:   于是那批单子看起来是「成交后被 ema 平掉」，其实是脏数据。
#:   现在一律打 `ambiguous_same_bar` 标记进 `RunResult.ambiguous`，
#:   统计时必须单列 —— 不许把它们混进正常成交里。
FILL_BEFORE_INVALIDATION: bool = True


# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class Thesis:
    """一条预期。字段全部显式 —— 不允许把口径藏在注释里。

    Attributes:
        thesis_id: 唯一标识（会写进 working_orders.client_order_id 的前缀）。
        kind: 形态名（例如「下跌中继EMA多次拒绝」）。P7 负责填。
        direction: ``DOWN`` 或 ``UP``。
        order_type: ``ORDER_LIMIT`` 或 ``ORDER_STOP``。**决定成交条件**，
            见下面的 `fill_when_below_market`。
        order_price: 挂单价 / 触发价。
        stop_price: 止损价。
        target_price: 止盈投影价。
        horizon_bars: 挂上之后最多等几根。0 = 不限（但通常应该给个值）。
        note: 自由备注，进证据链。

    ★ 挂单价在哪一侧，**不是**由方向单独决定的，而是由 (方向, 订单类型) 一起决定：

        | 方向 | 类型   | 挂单价位置 | 成交条件      | 语义           |
        |------|--------|-----------|---------------|----------------|
        | DOWN | LIMIT  | 现价上方   | high ≥ 价     | 反弹到那儿卖    |
        | DOWN | STOP   | 现价下方   | low  ≤ 价     | 跌破才追空      |
        | UP   | LIMIT  | 现价下方   | low  ≤ 价     | 回落到那儿买    |
        | UP   | STOP   | 现价上方   | high ≥ 价     | 涨破才追多      |

    只记「做空挂上方」会把 STOP 单判反；这正是用户纠正过的地方
    （三引线是回落接的 LIMIT，EMA 拒绝是跌破追的 STOP）。
    """

    thesis_id: str
    kind: str
    direction: str
    order_price: float
    stop_price: float
    target_price: float
    order_type: str = ORDER_LIMIT
    horizon_bars: int = 0
    note: str = ""
    #: 策略家族（``reversal`` / ``trend``）—— 决定失效判据的轴。
    #: ★ 默认 ``trend``：没传就照判，不许静默豁免。
    family: str = FAMILY_TREND
    #: ★ 挂单期的「预期衰减」距离（0 = 不看）。2026-11-17 新增。
    #:
    #: **只对反转家族生效**（趋势家族已经由 ema 负责撤单）。
    #:
    #: 反转家族原来在挂单期**完全没有失效判据** —— `invalidation_verdict`
    #: 对它恒返回 PENDING，于是挂着的三小时里价格怎么走都不会撤单，
    #: 只有成交之后才有止损。用户口径是「在预期和现实之间根据 K 线演化
    #: 止盈或者止损」，对反转只兑现了后半句。
    #:
    #: 语义：从**挂单那一刻的收盘价**起算，价格朝**远离触发价**的方向
    #: 走了 `stale_distance` 点以上 → 这个形态已经过期，撤单。
    #: 方向由 `triggers_when_price_falls` 决定，不另写一份。
    stale_distance: float = 0.0
    #: ★ 惊喜K线阈值（点）：单根 5m **顺向**幅度到了就立即止盈（0 = 不看）。
    #:
    #: 2026-11-17 用户要求，原话：
    #:   「任何一根 5m 的持仓顺向涨跌幅达到 500U，都要立即止盈，因为很难得！
    #:     我们不是长线，遇到这种惊喜K线必须走」
    #:
    #: ★ 为什么它是独立于「止盈价」的一条规则：止盈价是**固定的**（1h 振幅），
    #:   而行情偶尔会给出一根远超日常幅度的单边根 —— 那种根不属于「预期」，
    #:   属于「意外之喜」，拿到就走，不跟它讲道理。
    #: ★ 口径：**单根 5m 自己的顺向幅度**（不是从进场累计）。
    #:   判断依据是用户说的「任何一根 5m 的……」，而且如果是从进场累计，
    #:   它就退化成第二个固定止盈，与「止盈不是固定的」矛盾。
    surprise_pts: float = 0.0

    # ------------------------------------------------------------ S5 现实跟随出场
    #: ★ **保本棘轮**：有利浮盈达到几个 R 时，把止损移到保本价。
    #:
    #: 0 = 关闭。默认 1.0。
    #:
    #: 为什么用 R 而不是拍脑袋的百分比：**仓位本来就是按 R 定的**，
    #: `1R = |entry - stop|` 就是 `d_max`，不引入新参数。
    #: 它消灭的是最伤的一类单：先赚 1R 再回吐到亏一个 R。
    s5_breakeven_r: float = 1.0

    #: ★ **恐慌盘顺向出场**：`label == 恐慌盘` 且方向顺我 → 立即出。
    #:
    #: 机制：恐慌盘是**一次性流动性释放**，5m 上它的平均 `vol_rel = 6.17`
    #: （六倍量），日内不续。与用户「遇到惊喜K线必须走」同源，
    #: 是它的**慢速版**：惊喜K线看点数，这里看行为形态。
    #:
    #: 实测无条件触发率 4.1% / 根，且 side 两边基本对半
    #: （from_above 1,897 / from_below 1,859）—— 所以**必须**用 side 过滤方向。
    s5_panic_exit: bool = True

    #: ★ **缩量停滞出场**：当前这根「缩量 + 贴线/真空」就出。
    #:
    #: ⚠⚠ **默认 False，因为实测无条件触发率 31.4% / 根**。
    #:   持仓中位 7 根的话，`1-(1-0.314)^7 = 93%` 的仓位会在 7 根内被它平掉，
    #:   中位存活约 **2 根（10 分钟）**。
    #:
    #:   它字面上「不数根数」，但效果就是一个 2 根的时间闸 —— 比用户
    #:   2026-11-17 删掉的那个（20 小时）还狠。所以**写完不开**，
    #:   等条件化（已有持仓、已有不利进展）之后的 lift 量出来再议。
    s5_stall_exit: bool = False

    #: ★ 一个 R 的价格宽度（= 入场到止损的距离）。
    #: 由 `run()` 从 `|order_price - stop_price|` 派生，**不在这里存一份** ——
    #: 又是「同一件事两个来源」的坑。

    # ------------------------------------------------------------ 派生属性

    @property
    def triggers_when_price_falls(self) -> bool:
        """这笔单是不是「价格往下走到/穿过挂单价」才成交。

        LIMIT 买、STOP 卖 → 往下成交；
        LIMIT 卖、STOP 买 → 往上成交。

        ★ 委托给模块级 `triggers_when_price_falls()`，与
          `pending_plan.Plan.triggers_when_price_falls` 共用**同一份**实现。
          两边各写一遍是「方向/类型判反」最经典的复发方式：
          改了这里忘了那里，两边一致时才不出错，而没有任何测试会告诉你
          「两份实现开始不一致了」。
        """
        return triggers_when_price_falls(self.direction, self.order_type)

    @property
    def side(self) -> str:
        return "sell" if self.direction == DOWN else "buy"

    def fill_condition(self, bar: Any) -> bool:
        """本根有没有成交。看影线的哪一头由 `triggers_when_price_falls` 决定。

        ★ 这里必须看影线（h/l），不能看收盘价 —— 挂单被「走到」是 bar 内
          随时可能发生的事，跟收盘在哪儿无关。这一点和**失效判据**正好相反：
          失效只看收盘（用户：「跟插针有什么关系」），成交必须看影线
          （否则明明触及了却算没成交）。两个口径方向相反，别互相套用。
        """
        if self.triggers_when_price_falls:
            return float(bar.l) <= float(self.order_price)
        return float(bar.h) >= float(self.order_price)

    def validate(self) -> None:
        """把「价位放反了」这种错挡在门口。

        为什么值得单独校验：方向写反不会崩，只会让「止损」比入场价还低、
        「止盈」比入场价还高 —— 于是止损永远打不到、止盈立刻触发，
        回测结果看起来还挺好（因为根本没亏过）。这类错最难发现。
        """
        if self.direction not in (DOWN, UP):
            raise ValueError("direction 只能是 %r 或 %r，收到 %r"
                             % (DOWN, UP, self.direction))
        if self.order_type not in ORDER_TYPES:
            raise ValueError("order_type 只能是 %r 或 %r，收到 %r"
                             % (ORDER_LIMIT, ORDER_STOP, self.order_type))
        if self.order_price <= 0 or self.stop_price <= 0 or self.target_price <= 0:
            raise ValueError("价位必须为正数")
        if self.direction == DOWN:
            if not (self.stop_price > self.order_price):
                raise ValueError(
                    "做空：止损必须高于挂单价（%.2f vs %.2f）"
                    % (self.stop_price, self.order_price))
            if not (self.target_price < self.order_price):
                raise ValueError(
                    "做空：止盈必须低于挂单价（%.2f vs %.2f）"
                    % (self.target_price, self.order_price))
        else:
            if not (self.stop_price < self.order_price):
                raise ValueError(
                    "做多：止损必须低于挂单价（%.2f vs %.2f）"
                    % (self.stop_price, self.order_price))
            if not (self.target_price > self.order_price):
                raise ValueError(
                    "做多：止盈必须高于挂单价（%.2f vs %.2f）"
                    % (self.target_price, self.order_price))
        if self.horizon_bars < 0:
            raise ValueError("horizon_bars 不能为负")
        if self.surprise_pts < 0:
            raise ValueError("surprise_pts 不能为负")
        if self.stale_distance < 0:
            raise ValueError("stale_distance 不能为负")


@dataclass(frozen=True)
class LinePoint:
    """一条线在某一根上的值，带「什么时候可以用」。"""

    price: float
    src_ts: int          # 这个值是哪根 K 线算出来的
    ready_ms: int        # 最早可用时刻（= src_ts + 线自己的周期）


class LineBook:
    """按 line_key 取「截至某收盘时刻已就绪」的线值。

    形状对应 `kbar_line` 表：每行有 line_key / ts / line_price / line_ready_ms。
    这里只做内存查表，不碰 DB —— 数据从哪里来是调用方的事。

    为什么要 `ready_ms <= close_ms` 这个硬过滤，而不是「取最新一行」：
    ema20@30m 的 30m 桶和 15m 的桶边界不重合。15m 某一根收盘时，
    包住它的那个 30m 桶**可能还没收盘**；直接取最新一行 = 用到了未来的
    30m 收盘价。这个错误会让回测里的「快速失效」提前触发，
    看起来是策略更保守了，实际是偷了未来。
    """

    def __init__(self, rows: Iterable[Any] = ()):
        #: key -> [LinePoint]，按 ready_ms 升序
        self._by_key: Dict[str, List[LinePoint]] = {}
        #: key -> [ready_ms]，与 _by_key[key] 同下标、同步维护。
        #: ★ 必须缓存这个数组，不能每次 at() 现算：
        #: 早期版本在 at() 里写 `[p.ready_ms for p in bucket]`，
        #: 于是每次取线都是 O(n)。探测器在 17654 根上逐根调 at()，
        #: 整体退化成 O(n²)，真实数据冒烟直接跑不完（10 分钟无输出）。
        #: 缓存后 at() 是 O(log n)。
        self._ready: Dict[str, List[int]] = {}
        for r in rows or ():
            self.add(r)

    def add(self, row: Any) -> None:
        """加一行库行（dict）或已构造好的 (key, LinePoint)。

        dict 形状对应 `kbar_line`：至少要 line_key / line_price / ts，
        有 src_ts / line_ready_ms 就用，没有就按「ts + 该线自己的周期」推。
        """
        if isinstance(row, LinePoint):
            raise TypeError(
                "LinePoint 没有 line_key，无法归属。请用 add_point(key, pt)")
        if isinstance(row, (tuple, list)) and len(row) == 2 \
                and isinstance(row[1], LinePoint):
            self.add_point(str(row[0]), row[1])
            return
        d = row if isinstance(row, dict) else dict(row)
        key = str(d["line_key"])
        if d.get("line_price") is None:
            return          # 这条线在这根上不存在（是「没有」，不是 0）
        ts = int(d.get("ts", d.get("src_ts", 0)))
        self.add_point(key, LinePoint(
            price=float(d["line_price"]),
            src_ts=int(d.get("src_ts", ts)),
            ready_ms=int(d.get("line_ready_ms", ts + _key_period(key)))))

    def add_point(self, key: str, pt: LinePoint) -> None:
        key = str(key)
        bucket = self._by_key.setdefault(key, [])
        ready = self._ready.setdefault(key, [])
        i = bisect.bisect_right(ready, pt.ready_ms)
        bucket.insert(i, pt)
        ready.insert(i, pt.ready_ms)

    def at(self, key: str, close_ms: int,
           max_staleness_ms: Optional[int] = None) -> Optional[LinePoint]:
        """取 key 这条线在 ``close_ms`` 时刻「已经就绪」的最新一个值。

        Args:
            max_staleness_ms: 允许的最大陈旧度。给了就要求
                ``close_ms - ready_ms <= max_staleness_ms``，否则返回 None。
                为什么要有：ema 线不会「消失」，所以 as-of 本身没有
                reaction 层那个「把消失的线往前带」的问题；但**数据停更**时
                它会安静地把三小时前那条线的值当现在用。给个上限能让它
                露出「这条线没有新值」而不是假装有值。
        """
        bucket = self._by_key.get(key) or []
        if not bucket:
            return None
        ready = self._ready.get(key) or []
        i = bisect.bisect_right(ready, int(close_ms)) - 1
        if i < 0:
            return None
        pt = bucket[i]
        if max_staleness_ms is not None:
            if int(close_ms) - pt.ready_ms > int(max_staleness_ms):
                return None
        return pt

    def keys(self) -> List[str]:
        return sorted(self._by_key)


def _key_period(line_key: str) -> int:
    """从 ``ema20@30m`` 这样的 key 里抠出周期毫秒；抠不到返回 0。"""
    tag = str(line_key).rsplit("@", 1)[-1].strip().lower()
    table = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
             "1h": 3_600_000, "60m": 3_600_000, "4h": 14_400_000}
    return table.get(tag, 0)


@dataclass(frozen=True)
class Action:
    """状态转换要对**外面**做的事。本模块只产出，不执行。"""

    kind: str               # place_order / cancel / close
    thesis_id: str
    reason: str
    price: float = 0.0
    side: str = ""          # sell / buy
    order_type: str = ""    # LIMIT / STOP（place_order 必填）
    state_after: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "thesis_id": self.thesis_id,
                "reason": self.reason, "price": self.price,
                "side": self.side, "order_type": self.order_type,
                "state_after": self.state_after}


@dataclass(frozen=True)
class Transition:
    """一次状态转换，带证据。这就是「可回溯」的全部。"""

    thesis_id: str
    bar_ts: int
    from_state: str
    to_state: str
    verdict: str
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"thesis_id": self.thesis_id, "bar_ts": self.bar_ts,
                "from": self.from_state, "to": self.to_state,
                "verdict": self.verdict, "reason": self.reason,
                "evidence": dict(self.evidence)}


@dataclass
class RunResult:
    state: str
    transitions: List[Transition] = field(default_factory=list)
    actions: List[Action] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    #: ★ 同一根 K 里「既成交 / 又触发失效」的次数。
    #:
    #: 这些**不是**正常成交：OHLC 无 bar 内顺序，哪个先发生判不了。
    #: 以前默默按「先成交」算，于是脏数据混进了成交统计。
    #: 现在单列出来，任何统计都必须把它们排除或单独报告。
    ambiguous: List[Dict[str, Any]] = field(default_factory=list)
    #: ★ 跑到数据末尾时还没终结的原因（空 = 已进终态）。2026-11-17 新增。
    #:
    #: 以前「非终态收尾」是**完全静默**的：`res.state` 停在 EXPECTED /
    #: ARMED / FILLED，`res.terminal` 是 False，但没有任何地方会因此报错。
    #: 最坏的情况是 claim 的时间戳压根不在序列里 —— 状态机一根都推进不了，
    #: 跑完的结果看起来和「跑成功了、只是没成交」一模一样。
    #: 现在把原因写在这里，调用方（冒烟/回测/引擎）必须显式处理它。
    unresolved_reason: str = ""
    #: ★ 2026-11-17：跑完之后「下一根从哪儿接、接到什么状态」。
    #:
    #: 这两个值以前是 `run()` 的**局部变量**，也就是「一条 thesis 的生命周期
    #: 只存在于一次函数调用里」—— 生产环境里每根 K 都要推进一次，重启还要
    #: 能接上，局部变量做不到。现在它们跟着结果一起返回，
    #: `thesis.step()` / `pending_thesis_store` 靠它把状态机切成一段一段推进。
    armed_at_bar: Optional[int] = None
    armed_ref_close: Optional[float] = None
    #: ★ 2026-11-20：挂单发生在细周期（5m）上的时刻，超期按细周期根数计。
    #:   必须跟着结果返回并落库 —— 它是增量推进的**游标**，丢了就等于
    #:   「每根都重新开始数容忍根数」，容忍窗口永远不到期。
    armed_at_sub_ts: Optional[int] = None
    #: 已经推进到第几根（下一次 `step` 从 `next_index` 接着跑）。
    next_index: int = 0
    #: ★ S5 棘轮游标：自成交以来**最有利**的价格已走到哪（做多=最高价，做空=最低价）。
    #:
    #: ★★ 它**必须跟着结果一起返回并落库**，不能只当局部变量。
    #:   增量推进每根调一次 `step()`，局部变量每根都会重置 →
    #:   棘轮永远凑不到 1R，而回放里凑得到 —— 两边结果不一致而且不报错。
    best_favorable_px: Optional[float] = None

    fill_bar_ts: int = 0

    @property
    def ambiguous_same_bar(self) -> int:
        """同根歧义的条数（供统计用，不要混进正常成交）。"""
        return len(self.ambiguous)

    @property
    def unresolved(self) -> bool:
        """★ 没进终态就结束了 —— 调用方**必须**处理，不许当成功。"""
        return not self.terminal

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def states(self) -> List[str]:
        """走过的状态序列，方便断言（也方便看「抖了几下」）。"""
        if not self.transitions:
            return []
        out = [self.transitions[0].from_state]
        out.extend(t.to_state for t in self.transitions)
        return out

    def reasons(self) -> List[str]:
        return [t.reason for t in self.transitions]


# ---------------------------------------------------------------- 判定

def close_ms_of(bar: Any, interval_ms: int = BAR_INTERVAL_MS) -> int:
    return int(bar.ts) + int(interval_ms)


def _above(a: float, b: float) -> bool:
    return float(a) > float(b)


def _below(a: float, b: float) -> bool:
    return float(a) < float(b)


def invalidation_verdict(bars: Sequence[Any], i: int, book: LineBook,
                         direction: str,
                         *, interval_ms: int = BAR_INTERVAL_MS,
                         slow_bars: int = SLOW_CONFIRM_BARS,
                         slow_key: str = SLOW_LINE_KEY,
                         fast_key: str = FAST_LINE_KEY,
                         family: str = FAMILY_TREND
                         ) -> Tuple[str, Dict[str, Any]]:
    """判定第 ``i`` 根收盘时，这条 thesis 有没有被现实打脸。

    Args:
        family: 策略家族（``reversal`` / ``trend``），**决定要不要用 ema 判失效**。
            ★ 默认 ``trend``（照判）—— 保守方向：没传家族时宁可多判一次，
              也不许静默豁免。旧参数 `skip_ema_for_long` 已删除，因为它按
              **方向**判，镜像上线后两头都错（见常量区的注释）。

    Returns:
        ``(verdict, evidence)``，verdict 是 ``PENDING`` 或 ``VIOLATED``。
        （这条判据没有 ``SATISFIED`` —— 「预期没被推翻」本身就是 pending。）
        证据里一定带：判在哪根、慢路径比较的两个 close 与两个线值、
        快路径的比较值、以及实际触发了哪条路径。

    判据一律用**收盘价**，影线不参与。见模块 docstring。
    """
    bar = bars[i]
    close = float(bar.c)
    close_ms = close_ms_of(bar, interval_ms)
    ev: Dict[str, Any] = {"bar_ts": int(bar.ts), "close": close,
                          "direction": direction, "family": family,
                          "paths": []}

    # ★★ **反转**家族不用 ema 判失效 —— 用户 2026-11-17 拍板。
    #
    #   原因是个极性缺陷：反转的失效原本判「收盘跌破 ema20」，
    #   而抄底做多**一出生就在 ema20 下方**（§23.2），所以这条判据
    #   一出生就成立 → 每条反转预期都会在第一根 INVALIDATED。
    #   它不会报错，只会让所有反转形态永远不起作用。
    #
    #   用户的选择：「只用止损价，不看 ema」。
    #   于是反转的「预期被推翻」= 价格走到止损价（由 `_stop_hit` 负责，
    #   在 FILLED / ARMED 两个状态里判），这里直接返回 PENDING。
    #
    # ★★★ 判的是**家族**，不是方向（HANDOFF §23.23）。
    #   以前这里写 `direction == UP`，在「做空都是趋势」的年代碰巧成立；
    #   镜像上线后：反转**也有做空**（上涨摸顶），趋势**也有做多**（上涨回调），
    #   于是反转空单被 ema 白杀、趋势多单被白豁免 —— 两头都错，都不报错。
    #   实测：4/6 条反转空单在挂单前被 ema 判死（`tools/_diag_cancel_reason.py`）。
    if family == FAMILY_REVERSAL:
        ev["skipped"] = "反转家族不看 ema 失效（用户口径：只认止损价）"
        return PENDING, ev

    # 趋势家族：ema 打脸就是失效。
    #   做空 → 站上 ema = 趋势没在继续下跌；
    #   做多 → 跌破 ema = 趋势没在继续上涨。（镜像对称，不是特例。）
    want_above = (direction == DOWN)     # 做空：价格「站上」= 打脸

    # ---- 快路径：站上 ema20@30m，立即失效（独立结算，不等第 3 根）
    fast = book.at(fast_key, close_ms)
    if fast is not None:
        hit = _above(close, fast.price) if want_above else _below(close, fast.price)
        ev["fast"] = {"key": fast_key, "line": fast.price,
                      "src_ts": fast.src_ts, "ready_ms": fast.ready_ms,
                      "close": close, "hit": bool(hit)}
        if hit:
            ev["paths"].append("fast")
    else:
        ev["fast"] = {"key": fast_key, "line": None, "hit": False,
                      "why": "线在此时刻没有就绪值"}

    # ---- 慢路径：跳读第 3 根（不是滚动窗口，不重置计数）
    j = i - int(slow_bars)
    if j >= 0:
        prev = bars[j]
        prev_close = float(prev.c)
        prev_ms = close_ms_of(prev, interval_ms)
        prev_line = book.at(slow_key, prev_ms)
        now_line = book.at(slow_key, close_ms)
        if prev_line is not None and now_line is not None:
            prev_hit = (_above(prev_close, prev_line.price) if want_above
                        else _below(prev_close, prev_line.price))
            now_hit = (_above(close, now_line.price) if want_above
                       else _below(close, now_line.price))
            ev["slow"] = {"key": slow_key, "bars": int(slow_bars),
                          "prev_ts": int(prev.ts), "prev_close": prev_close,
                          "prev_line": prev_line.price, "prev_hit": bool(prev_hit),
                          "now_ts": int(bar.ts), "now_close": close,
                          "now_line": now_line.price, "now_hit": bool(now_hit)}
            if prev_hit and now_hit:
                ev["paths"].append("slow")
        else:
            ev["slow"] = {"key": slow_key, "why": "任一端线值缺失", "hit": False}
    else:
        ev["slow"] = {"key": slow_key, "why": "历史不足 %d 根" % slow_bars,
                      "hit": False}

    if ev["paths"]:
        return VIOLATED, ev
    return PENDING, ev


def _fill_condition(bar: Any, thesis: Thesis) -> bool:
    """这笔单在**本根**有没有成交。

    ★ 判定逻辑整个挪到了 `Thesis.fill_condition()` 里，因为成交条件取决于
      (方向, 订单类型) 两个字段的组合，单看方向会判反：
        DOWN+LIMIT（反弹到上方卖）→ high ≥ 价
        DOWN+STOP （跌破下方追空）→ low  ≤ 价
        UP  +LIMIT（回落到下方买）→ low  ≤ 价
        UP  +STOP （涨破上方追多）→ high ≥ 价
      这是用户 2026-11-17 纠正过的地方（三引线是回落接的 LIMIT，
      EMA 拒绝是跌破追的 STOP），留个薄壳保持调用点不变。
    """
    return thesis.fill_condition(bar)


def _stop_hit(bar: Any, thesis: Thesis) -> bool:
    if thesis.direction == DOWN:
        return float(bar.h) >= float(thesis.stop_price)
    return float(bar.l) <= float(thesis.stop_price)


def _target_hit(bar: Any, thesis: Thesis) -> bool:
    if thesis.direction == DOWN:
        return float(bar.l) <= float(thesis.target_price)
    return float(bar.h) >= float(thesis.target_price)


def _entry_ref_price(thesis: Thesis, bar: Any) -> float:
    """这一根**实际**是在什么价位成交的 —— 惊喜K线判据要用它当基准。

    ★ 为什么不能一律用 `bar.o`：成交可能发生在**盘中**。
      实测反例（我自己写出来的）：挂 STOP buy @100500，那根 5m 是
      `o=100000 h=100505 l=99999 c=100502`。按 `o` 算「顺向 502 点」
      就直接止盈了 —— 可我们是在 100500 成交的，真正只赚了 1.9 点。
      那不是惊喜，是把「一根本来就很宽的大K」误当成了利润。

      所以成交那根的基准必须是**成交价**：跳空时按开盘价（更优/更差都算），
      盘中触及按挂单价。
    """
    o = float(getattr(bar, "o", 0.0) or 0.0)
    px = float(thesis.order_price)
    if o <= 0:
        return px
    if thesis.side == "buy":
        return min(px, o) if thesis.triggers_when_price_falls else max(px, o)
    return max(px, o) if thesis.triggers_when_price_falls else min(px, o)


def _surprise_hit(thesis: Thesis, bar: Any,
                  entry_ref: Optional[float] = None) -> Optional[float]:
    """这根 K 的**顺向幅度**（点）够不够「惊喜」，够就返回幅度，否则 None。

    · 做多：`high - low` 里朝上的部分 —— 用 `high - open` 会漏掉跳空，
      所以取「这根自己能走出的最大顺向距离」= 多头看 high、空头看 low
      相对**开盘价**的距离，同时不低于「完整振幅」的一半（巨震根两边都大）。
    · 先按最保守的算法：**只用收盘相对开盘**，那是最小值；
      只有一根把整个振幅都走成顺向（`h-l`）时才算数。
      ★ 为什么选保守：止盈是按**收盘价**结算的，如果按影线极值判定、
        再按收盘价成交，等于「用最好的一刻判、用较差的一刻记」，
        会让样本看起来更好 —— 这个项目里所有偏差都必须朝不利方向。
        所以判据用「`close - open` 顺向幅度」，成交也用 close，两者同一口径。
    """
    pts = float(getattr(thesis, "surprise_pts", 0.0) or 0.0)
    if pts <= 0:
        return None
    # ★ 基准：平时用这根的开盘价；**成交那一根**必须用成交价（见
    #   `_entry_ref_price` 里的反例）。
    base = float(entry_ref) if entry_ref is not None \
        else float(getattr(bar, "o", 0.0) or 0.0)
    c = float(bar.c)
    if base <= 0:
        return None
    move = (c - base) if thesis.direction == UP else (base - c)
    if move >= pts:
        return move
    return None


# ---------------------------------------------------------------- 主循环

ClaimFn = Callable[[int, Sequence[Any], LineBook], Tuple[bool, Dict[str, Any]]]


# ======================================================================
# S5 · 现实跟随出场
#
# 用户口径：「止盈不是固定的，可以先定一个很远的止盈，但是我想要的是
# 现实跟随，根据我们的区间演化 + K线演化，判断风向！」
#
# 这一段是第二组名义线之外的**第三层**：
#   止损 = d_max（公式，硬兜底）
#   4R 止盈 = 4 × d_max（名义天花板，多数到不了）
#   实际出场 = 本段
#
# 所有判据只读**t 时刻可观测**的量，不读任何事后列（`kbar_forward` /
# `kbar_verify` / `kbar_verify_survival` 目录下的东西一律不碰）。
# ======================================================================

#: 恐慌盘的结论词（来自 `shape.judge`，未重新定义）。
KBAR_LABEL_PANIC = "恐慌盘"
#: 「什么也没发生」的两个词：贴线 / 真空。
KBAR_LABELS_QUIET = ("无·贴线", "无·真空")
#: 缩量阈值。★ 直接用库里已有的口径（`dims._mult_word`：< 1.0 = 低于均量），
#: **不新造阈值** —— 它正是维度总账 §十一 留出集里 z = −8.61 的那一档。
KBAR_SHRINK_CUT = 1.0


def kbar_fields(kbar: Any) -> Optional[Dict[str, Any]]:
    """把 `kbar` 入参正规化成字典。None / 缺失 → None。

    ★ 为什么只认字典和属性两种：回放路径传 `dict`（从库里轻量装载），
      增量路径传对象。两种都能读，但**只有一套取值逻辑**。
    """
    if kbar is None:
        return None
    if isinstance(kbar, dict):
        return kbar or None
    out = {}
    for k in ("label", "side", "vol_rel", "ts"):
        out[k] = getattr(kbar, k, None)
    return out if any(v is not None for v in out.values()) else None


def breakeven_stop(thesis: "Thesis", best_favorable_px: float,
                   *, entry: Optional[float] = None) -> Optional[float]:
    """保本棘轮：有利浮盈达到 `s5_breakeven_r` 个 R 后，返回**保本止损价**；
    还没到就返回 None。

    Args:
        best_favorable_px: 自成交以来**最有利**的价格已走到哪（做多是最高价，
            做空是最低价）。它是单调的，所以棘轮天然只能朝有利方向移 ——
            硬不变量 #5「止损永不后退」由构造保证，不靠纪律。

    ★ 保本不是「回到入场价」，是「回到不亏的价」：还要盖住往返成本。
      否则叫做保本、实际亏了两项手续费。成本来自
      `pending_plan.round_trip_costs()`（唯一来源），不在这里重算。
    """
    k = float(getattr(thesis, "s5_breakeven_r", 0.0) or 0.0)
    if k <= 0:
        return None
    e = float(entry if entry is not None else thesis.order_price)
    r = abs(e - float(thesis.stop_price))
    if r <= 0:
        return None
    got = (float(best_favorable_px) - e) if thesis.direction == UP \
        else (e - float(best_favorable_px))
    if got < k * r:
        return None
    # 保本价 = 入场价 + 成本（不利侧方向）
    cost = _cost_points_for(thesis, e)
    if thesis.direction == UP:
        return e - cost
    return e + cost


def _cost_points_for(thesis: "Thesis", entry: float) -> float:
    """往返成本（点数）。**只调 `pending_plan.round_trip_costs`**，不自己算。

    ★ 这里的 import 必须是**函数内部延迟 import**：
      `pending_plan` 顶层 `from . import thesis as T`，顶层互相 import 会环。
    """
    try:
        from .pending_plan import PendingParams, round_trip_costs
        _cw, cl = round_trip_costs(
            float(entry),
            (thesis.order_type or ORDER_LIMIT).upper(), PendingParams())
        return float(cl)
    except Exception:
        # ★ 不静默归 0：那会把「保本」变成「不亏不赚·未含成本」。
        #   但也不能报错弄挂起来 —— 它不在开仓路径上。返回 0 并依赖
        #   `_cost_points_for` 的调用方记账。
        return 0.0


def panic_exit(thesis: "Thesis", kbar: Any) -> Optional[Dict[str, Any]]:
    """恐慌盘**顺我方向**——出场。否则 None。

    三个条件同时满足：
      1. `label == 恐慌盘`（shape.judge 已判，未重新定义）
      2. 方向顺我：做多要 `from_below`（从下方向上打过去），做空要 `from_above`
      3. `side` 必须有值 —— 拿不到方向就**不能出场**（不猜）

    ★ 为什么方向取 `kbar_reaction.side` 而不是 `kbar_shape.side`：
      `kbar_shape.side` 填充率只有 **11.8%**，`kbar_reaction.side` 是 **100%**。
      拿 11.8% 的列当判据，89% 的情况会拿到 None 而静默不出场。
    """
    if not bool(getattr(thesis, "s5_panic_exit", False)):
        return None
    f = kbar_fields(kbar)
    if not f:
        return None
    if f.get("label") != KBAR_LABEL_PANIC:
        return None
    side = f.get("side")
    want = "from_below" if thesis.direction == UP else "from_above"
    if side != want:
        return None
    return {"label": f.get("label"), "side": side, "want_side": want,
            "vol_rel": f.get("vol_rel"), "kbar_ts": f.get("ts")}


def stall_exit(thesis: "Thesis", kbar: Any) -> Optional[Dict[str, Any]]:
    """缩量停滞——出场。否则 None。

    ★★ **默认关闭**（`s5_stall_exit = False`），理由写在字段注释里：
      实测无条件触发率 **31.4% / 根**，持仓 7 根内 93% 会被它平掉，
      中位存活约 2 根 —— 那就是一个 2 根的时间闸。

    判据：当前这根 `label ∈ (无·贴线, 无·真空)` 且 `vol_rel < 1.0`（低于均量）。
    机制：我们持仓需要一段行情发生，而**缩量说明它不会来**。
    这是维度总账 §十一 里唯一过了留出集的维度（z = −8.61 / +5.89）。
    """
    if not bool(getattr(thesis, "s5_stall_exit", False)):
        return None
    f = kbar_fields(kbar)
    if not f:
        return None
    if f.get("label") not in KBAR_LABELS_QUIET:
        return None
    v = f.get("vol_rel")
    if v is None or float(v) >= KBAR_SHRINK_CUT:
        return None
    return {"label": f.get("label"), "vol_rel": float(v),
            "cut": KBAR_SHRINK_CUT, "kbar_ts": f.get("ts")}


def _update_best_fav(thesis: "Thesis", cur: Optional[float],
                     bar: Any) -> Optional[float]:
    """用这根 K 的影线更新「最有利价格已到哪」。

    做多取 `high`，做空取 `low`；只能更有利，不能更差。
    ★ 用影线而不用收盘价：棘轮问的是「最好曾到过哪」，
      不是「现在在哪」。用收盘会把已经拿到过的 1R 忘掉。
    """
    if cur is None:
        return None
    c = float(cur)
    if thesis.direction == UP:
        return max(c, float(bar.h))
    return min(c, float(bar.l))


def _kbar_at(kbar: Any, ts_ms: int) -> Any:
    """从 `kbar` 视图里取这一刻的 K线演化事实。None → None。

    支持三种形式（一套取值逻辑，不逐个实现）：
      · 对象带 `.at(ts)`  —— 回放从库里装载的只读视图（推荐）
      · 可调用 `f(ts)`    —— 增量路径现取
      · 字典 `d[ts]`        —— 小范围测试
    """
    if kbar is None:
        return None
    try:
        at = getattr(kbar, "at", None)
        if callable(at):
            return at(int(ts_ms))
        if callable(kbar):
            return kbar(int(ts_ms))
        if isinstance(kbar, dict):
            return kbar.get(int(ts_ms)) or kbar.get(ts_ms)
    except Exception:
        return None
    return None


def _bucket_of(ts: int, interval_ms: int) -> int:
    """某根细周期 K 落在哪个粗周期桶里（按**开盘时刻**对齐）。

    ★ 口径必须和 `line_ready_ms = src_ts + period_ms` 一致：库里的 `ts` 是
      **开盘时刻**。所以 15m 的 ts=T 覆盖 [T, T+15m) 并在 T+15m 收盘；
      一根 ts=S 的 5m 属于桶 ``S - S % 15m``。
      以前 pending_plan 里用的是「往后找第一根 ts >= S+5m 的 15m」，
      那等于「等下一根 15m 才动手」，把 5m 的信号硬拖成 15m ——
      这就是三分之一倒V 信号被吃掉的机制。
    """
    iv = int(interval_ms)
    return int(ts) - (int(ts) % iv)


def _sub_index(entry_bars: Sequence[Any], interval_ms: int) -> Dict[int, List[Any]]:
    """把细周期序列按粗周期桶分组。"""
    out: Dict[int, List[Any]] = {}
    for b in entry_bars or ():
        out.setdefault(_bucket_of(int(b.ts), interval_ms), []).append(b)
    for v in out.values():
        v.sort(key=lambda x: int(x.ts))
    return out


def run(thesis: Thesis,
        bars: Sequence[Any],
        book: LineBook,
        claim: Optional[ClaimFn] = None,
        *,
        interval_ms: int = BAR_INTERVAL_MS,
        entry_bars: Optional[Sequence[Any]] = None,
        entry_interval_ms: int = 300_000,
        claim_on_entry: bool = False,
        start_at: int = 0,
        trace: bool = False,
        initial_state: str = EXPECTED,
        resume: Optional[Dict[str, Any]] = None,
        kbar: Any = None,
        fill_bar_ts: int = 0) -> RunResult:
    """把一条 thesis 在一段 K 线上跑完，返回终态 + 转换 + 动作。

    Args:
        claim: ``(i, bars, book) -> (成立?, evidence)``。
            ``None`` 表示「一开始就成立」（预期已确认，直接挂单）——
            这样状态机能脱离三种形态单独测。
        entry_bars: **入场执行用的细周期 K 线**（5m）。给了就按双节奏跑：
            成交 / 止损 / 止盈 全在 5m 上判，而**失效仍只按粗周期（15m）收盘**判。
            这是用户定的口径：「入场全部在 5m K，K 线演化和区间演化用原周期」。
        entry_interval_ms: entry_bars 的周期。只用来把 5m 归到 15m 桶。
        claim_on_entry: **声明（claim）在哪个时钟上评估**。
            * False（默认）：在粗周期桶收盘时评估，``claim(i, bars, book)``
              里的 i 是 15m 下标 —— 三引线 / EMA 拒绝用这个（形态在 15m 上确认）。
            * True：每一根细周期都评估，``claim(j, entry_bars, book)``
              里的 j 是 5m 下标 —— 倒V 用这个，因为它的信号本身就在 5m 上，
              再等一根 15m 才挂单，价格往往已经涨破触发价（already_triggered）。
        start_at: 从第几根开始**当这条 thesis 存在**。之前那几根只占位、
            不做任何判定。
            ★ 为什么必须有：把一条「某天某根才成立」的计划扔进几万根历史里跑，
              状态机从第 0 根就开始判失效 —— 而那时这条 thesis 根本还不存在，
              于是每条计划都在「声明之前」被判掉 INVALIDATED（真实数据冒烟里
              12/12 全中）。这不是失效判据的错，是起点错了。
              注意它只跳过**判定**，不裁剪序列：`invalidation_verdict` 仍然拿
              全局下标去看「第 3 根之前」的收盘，判据口径不变。
        trace: 是否记录**每根**的三值判定（抖动分析要用）。
            注意它按根增长，几万根时别默认打开。
        fill_bar_ts: ★ EXIT-1 用：成交所在那根的**开盘时刻**（5m 桶的 ts）。
            增量推进（``start_at > 0``）或重启恢复时，本次调用看不到成交那一根，
            锚点就找不到了 —— 所以必须由调用方（引擎知道持仓的 entry_ts）传进来。
            传 0 表示"本次调用会亲眼看到成交"，由状态机自己在成交时记下。

    转换规则（逐根，按状态分派）：
        EXPECTED: 失效 → INVALIDATED（**根本没挂过单**）；claim 成立 → ARMED
        ARMED:    成交 → FILLED；否则失效 → CANCELLED；否则超期 → EXPIRED
        FILLED:   止损 / 止盈 / 失效平仓

    ## 双节奏的时序（entry_bars 给了时）

    一个 15m 桶里依次有 3 根 5m。逐根判成交/止损/止盈；**只有最后那根 5m
    的收盘等于 15m 的收盘**，所以失效判定（三值）只在那时做一次。
    这样「同桶里先成交、再打到止损」和「先被推翻、又刚好触及挂单价」
    这两种顺序都不会错 —— 而这正是用粗周期 h/l 一起判时必然判错的地方
    （粗 h/l 把 15 分钟内的先后顺序抹平了）。
    """
    thesis.validate()
    bars = list(bars or ())
    res = RunResult(state=initial_state)
    state = initial_state
    armed_at_bar: Optional[int] = None
    #: 挂单那一刻的收盘价 —— 反转家族的「预期衰减」从这里起算。
    armed_ref_close: Optional[float] = None
    #: ★ 2026-11-20：挂单发生在**细周期（5m）收盘**上的时刻。
    #:
    #:   用户口径：「标记4满足下引线，挂单，容忍5/6/7……直到不满足撤单或者成交」
    #:   —— 容忍是按**5m 根数**数的，不是 15m。原来 `horizon_bars` 一律按
    #:   粗周期数（5m 形态要乘 3 才等价），于是 12 根的默认值对三引线
    #:   实际是 36 根 5m = 3 小时，远超「几根」。
    #:   有值 = 这笔单的超期按细周期计（同时关掉粗周期那份计数，避免双记）。
    armed_at_sub_ts: Optional[int] = None
    _walked = max(0, int(start_at))
    # ★ 续跑：把上一次调用留下的两个「由历史派生」的值的接回来。
    #
    #   为什么必须显式接：`start_at` 之前的 K 根本不会被遍历，所以
    #   ARMED 那两根里的赋值语句不会执行。少了它，续跑的第一根就会
    #   丢掉挂单时刻 → `horizon_bars` 永远不触发、`stale_distance` 永远
    #   算不出来，而且**不报错**（和本项目历史上那些静默缺陷同型）。
    # ★ S5 棘轮游标：从 resume 恢复（增量推进跨 step 必须接上）
    best_fav = None
    #: ★ EXIT-1：已收盘的 5m 序列（按时间顺序累积）+ 成交那根的开盘时刻。
    #:  ``break_rules.assess`` 需要"从成交那根起的收盘序列"才能找到锚点，
    #:  只给它当前这根是算不出来的。没有 5m 数据（只喂了 15m）时保持空，
    #:  此时**不判**收盘跌破 —— 拿 15m 冒充 5m 等于换了用户的规则。
    _b5_hist: List[Any] = []
    _fill_bar_ts: int = int(fill_bar_ts or 0)
    #: 预排序的 5m 全序列（ts, bar），用于 O(log n) 取"从成交那根到当前根"的切片。
    #: 不预建的话每根都要遍历全部 subs —— 400 桶 × 12 根 × 400 根 = 两百万次，
    #: 回测直接慢一个数量级。
    _b5_all: List[Any] = []
    _b5_ts: List[int] = []
    if entry_bars:
        _b5_all = sorted(entry_bars, key=lambda b: int(b.ts))
        _b5_ts = [int(b.ts) for b in _b5_all]

    def _b5_since(end_ts: int):
        """取「成交那根 → end_ts」这一段已收盘 5m，供 `break_rules.assess` 用。

        返回 None 表示"现在还判不了"（还没成交，或没有 5m 数据）。
        —— 拿 15m 的数据去判 5m 的规则等于换了用户的定义，宁可不判。
        """
        if not _fill_bar_ts:
            return None
        if _b5_all:
            lo = bisect.bisect_left(_b5_ts, _fill_bar_ts)
            hi = bisect.bisect_right(_b5_ts, int(end_ts))
            return _b5_all[lo:hi] or None
        seg = [b for b in _b5_hist if int(b.ts) <= int(end_ts)]
        return seg or None
    if resume:
        _bf = resume.get("best_favorable_px")
        if _bf is not None:
            best_fav = float(_bf)
    if resume:
        armed_at_bar = resume.get("armed_at_bar")
        _arc = resume.get("armed_ref_close")
        armed_ref_close = float(_arc) if _arc is not None else None
        _ast = resume.get("armed_at_sub_ts")
        armed_at_sub_ts = int(_ast) if _ast is not None else None

    def emit(to_state: str, verdict: str, reason: str,
             evidence: Dict[str, Any], bar_ts: int,
             action: Optional[Action] = None) -> None:
        nonlocal state
        res.transitions.append(Transition(
            thesis_id=thesis.thesis_id, bar_ts=int(bar_ts),
            from_state=state, to_state=to_state, verdict=verdict,
            reason=reason, evidence=dict(evidence)))
        state = to_state
        if action is not None:
            res.actions.append(action)

    # ---- 双节奏的准备 ----
    # 没有 entry_bars 时 subs_by_bucket 为空、pos_in_entry 为空，
    # 循环里 seq 恒等于 [bar]，等价于「每一步都是 15m」= 改造前行为。
    subs_by_bucket = _sub_index(entry_bars, interval_ms) if entry_bars else {}
    pos_in_entry = ({int(b.ts): j for j, b in enumerate(entry_bars)}
                    if entry_bars else {})

    for i, bar in enumerate(bars):
        if i < int(start_at):
            # 声明之前：这条 thesis 还不存在，不判失效、不看成交。
            continue
        if state in TERMINAL_STATES:
            break
        _walked = i + 1

        verdict, ev = invalidation_verdict(
            bars, i, book, thesis.direction, interval_ms=interval_ms,
            family=thesis.family)

        if trace:
            res.trace.append({"bar_ts": int(bar.ts), "state": state,
                              "verdict": verdict, "evidence": ev})
        ts = int(bar.ts)

        # 这一桶里的细周期 K（没有就是空）。入场执行看 seq：
        # 有 5m 就逐根 5m 判，没有就退化成用这根 15m 判 —— 宁可粗，不可瞎。
        lo = ts
        hi = ts + int(interval_ms)
        subs = [s for s in subs_by_bucket.get(_bucket_of(lo, interval_ms), ())
                if lo <= int(s.ts) < hi]
        seq = subs if subs else [bar]
        # 声明（claim）能不能在细周期上成立：倒V 走这条，
        # 三引线 / EMA 拒绝的形态定义在 15m 收盘上，不能提前。
        ask_on_subs = bool(claim_on_entry and subs)

        for sb in seq:
            if state in TERMINAL_STATES:
                break
            sb_ts = int(sb.ts)
            # ★ EXIT-1：只在**真有 5m 细周期**时累积（`seq` 退化成 15m 时不记，
            #   否则收盘跌破会被拿 15m 的收盘价判出来）。
            if subs:
                _b5_hist.append(sb)
            #: 判读用的"从成交那根到当前根"的真实 5m 切片。
            #: ★ 必须在这里算（而不是在外面算一次）：成交那根的 `_fill_bar_ts`
            #: 是在 fill 分支里才赋的值，在外面先算会拿到 None，
            #: 于是成交当根就漏判了「收盘跌破」—— 而那正是事故里最关键的一根。
            _b5_for_read = _b5_since(sb_ts)

            if state == EXPECTED:
                # 声明在哪儿成立：细周期（claim_on_entry）或粗周期收盘
                ask = ask_on_subs or (sb is bar)
                if not ask:
                    continue
                ok = True
                c_ev: Dict[str, Any] = {}
                if claim is not None:
                    if ask_on_subs:
                        ok, c_ev = claim(pos_in_entry[sb_ts], entry_bars, book)
                    else:
                        ok, c_ev = claim(i, bars, book)
                if not ok:
                    continue
                emit(ARMED, SATISFIED,
                     "预期成立 → 挂 %s %s @ %.2f"
                     % (thesis.order_type, thesis.side, thesis.order_price),
                     {**ev, "claim": c_ev, "order_price": thesis.order_price,
                      "order_type": thesis.order_type, "side": thesis.side},
                     sb_ts,
                     Action(kind="place_order", thesis_id=thesis.thesis_id,
                            reason="thesis_armed", price=thesis.order_price,
                            side=thesis.side, order_type=thesis.order_type,
                            state_after=ARMED))
                # ★ 挂单发生在**这根收盘之后**，所以本根的 h/l 属于过去，
                #   不拿它判成交；成交从下一根开始看。
                #   （同一桶里下一根 5m 就是下一轮循环，不会漏。）
                armed_at_bar = i
                armed_ref_close = float(sb.c)
                armed_at_sub_ts = sb_ts      # 超期按 5m 根数计（用户 2026-11-20）
                continue

            if state == ARMED:
                # ★★ 成交只能发生在**挂单之后**（2026-11-25 真钱事故）。
                #
                #   挂单那一轮的 `continue` 只保护**同一次调用**。但游标
                #   `next_index` 会被解冻重算（SIG-PARAMS：卡住的 400 被清回
                #   真实起点 386），于是**下一次**调用从 386 开始遍历，而
                #   `armed_at_bar` 是 399 —— ARMED 状态把挂单**之前**的
                #   386..399 又扫了一遍，并在其中一根上判出"成交"。
                #
                #   实测后果（14:50→14:55）：交易所那两张 `AI-pend-*` 单
                #   始终 NEW、executed_qty=0，本地却推出 FILLED → close →
                #   CLOSED_TP，两张真实挂单变成孤儿（无人再管；一旦成交
                #   会被判成"我方成交"而硬停）。
                #
                #   ★ 判据用**时间戳**而不是 15m 索引：双节奏（`ask_on_subs`）
                #     下挂单发生在某个 5m 子根，**同一根 15m 之内之后的子根**
                #     是合法的（5m 入场粒度本来就是这样），用 `i <= armed_at_bar`
                #     会把这批合法成交一起挡掉（test_run_plan_fills_the_inverse_v_
                #     on_a_later_5m_bar 当场变红）。
                #   单在 `armed_at_sub_ts` 收盘之后才存在 → 那之前（含）的
                #   子根里不可能成交。这是二值事实，不是阈值。
                if armed_at_sub_ts is not None:
                    if int(sb_ts) <= int(armed_at_sub_ts):
                        continue
                elif armed_at_bar is not None and i <= int(armed_at_bar):
                    continue
                # 先看成交（保守：同根既成交又失效时，按先成交再平掉）
                filled = FILL_BEFORE_INVALIDATION and _fill_condition(sb, thesis)
                # ★ 挂单后按**细周期根数**容忍（用户 2026-11-20：
                #   「挂单，容忍5/6/7……直到不满足撤单或者成交」）。
                #   放在成交判定**之后**：同一根既到期又成交，按成交算
                #   （成交是价格事实，到期只是时间到了）。
                if (not filled and armed_at_sub_ts is not None
                        and thesis.horizon_bars
                        and int(entry_interval_ms) > 0):
                    _elapsed = (sb_ts - armed_at_sub_ts) // int(entry_interval_ms)
                    if _elapsed >= int(thesis.horizon_bars):
                        emit(EXPIRED, PENDING,
                             "挂单超过 %d 根 5m 仍未成交 → 撤单"
                             % int(thesis.horizon_bars),
                             {"armed_sub_ts": armed_at_sub_ts, "now_sub_ts": sb_ts,
                              "elapsed_bars": int(_elapsed),
                              "cancel_reason": "thesis_expired"},
                             sb_ts,
                             Action(kind="cancel", thesis_id=thesis.thesis_id,
                                    reason="thesis_expired",
                                    state_after=EXPIRED))
                        continue
                if filled:
                    # ★ 同根歧义：这根既触及挂单价、又（收盘）穿过 ema。
                    #   OHLC 没有 bar 内顺序，**哪个先发生判不了**。
                    #   仍然按「先成交」继续跑（保守），但必须记账 ——
                    #   不记账的话这批脏数据会混进正常成交统计里。
                    # 两类同根歧义，先各算一次（下面记账和打标记都复用）
                    amb_inval = bool(verdict == VIOLATED)
                    amb_exit = bool(_stop_hit(sb, thesis)
                                    or _target_hit(sb, thesis))
                    if amb_inval:
                        res.ambiguous.append({
                            "bar_ts": sb_ts, "order_price": thesis.order_price,
                            "order_type": thesis.order_type,
                            "bar_h": float(sb.h), "bar_l": float(sb.l),
                            "kind": "fill+invalidation",
                            "why": "同一根里既成交又触发失效，先后不可判定",
                            "handling": "按先成交处理（FILL_BEFORE_INVALIDATION）",
                        })
                    # ★ 同一类问题的另一半：成交那根**同根**就打到止损/止盈。
                    #   入场价和止损价都在这根的高低点区间内，谁先碰到
                    #   同样不可判定。以前默默按「成交后立刻止损」算。
                    if amb_exit:
                        res.ambiguous.append({
                            "bar_ts": sb_ts, "order_price": thesis.order_price,
                            "stop_price": thesis.stop_price,
                            "target_price": thesis.target_price,
                            "bar_h": float(sb.h), "bar_l": float(sb.l),
                            "kind": "fill+exit",
                            "why": "成交那根同根就打到止损/止盈，先后不可判定",
                            "handling": "按「成交后立刻平掉」处理（保守：多亏一点）",
                        })
                    emit(FILLED, SATISFIED,
                         "%s %.2f 被走到 → 成交" % (thesis.order_type,
                                                     thesis.order_price),
                         {"bar_ts": sb_ts, "order_price": thesis.order_price,
                          "order_type": thesis.order_type,
                          "bar_h": float(sb.h), "bar_l": float(sb.l),
                          "ambiguous_same_bar": amb_inval or amb_exit,
                          "entry_interval_ms": (int(entry_interval_ms)
                                                if subs else int(interval_ms))},
                         sb_ts)
                    # 成交后同根立刻判退场（保守：不假设「成交那根就一定安全」）
                    _entry_px = _entry_ref_price(thesis, sb)
                    # ★ EXIT-1：记下成交所在那根的**开盘时刻**。
                    #   锚点只能从这一根往后找 —— 入场前的走势不是这笔仓的事。
                    #   这里必须用 bar 的开盘时刻（sb_ts），不能用"现在几点"：
                    #   assess 的过滤条件是 `ts >= since_ts`，用成交的精确毫秒
                    #   会把成交那根自己排除掉，锚点永久晚一根。
                    _fill_bar_ts = sb_ts
                    # ★ 棘轮游标起点 = 实际成交价
                    if best_fav is None:
                        best_fav = float(_entry_px)
                    _closed_fill = _close_position(
                        thesis, sb, PENDING, {}, i, emit, sb_ts,
                        entry_ref=_entry_px,
                        kbar=_kbar_at(kbar, sb_ts),
                        ratchet_stop=breakeven_stop(
                            thesis, best_fav, entry=_entry_px),
                        best_favorable_px=best_fav,
                        book=book,
                        close_ms=sb_ts + int(entry_interval_ms),
                        bars5=_b5_for_read, fill_bar_ts=_fill_bar_ts)
                    if not _closed_fill:
                        best_fav = _update_best_fav(thesis, best_fav, sb)
                continue

            if state == FILLED:
                # 止损 / 止盈按细周期 h/l 判（用户定：跟入场一致用 5m 的 h/l）
                # ★ S5：每根更新「最有利已到哪」。它单调递增，所以棘轮天然只能朝有利方向移。
                if best_fav is None:
                    best_fav = float(thesis.order_price)
                _closed = _close_position(
                    thesis, sb, PENDING, {}, i, emit, sb_ts,
                    kbar=_kbar_at(kbar, sb_ts),
                    ratchet_stop=breakeven_stop(thesis, best_fav),
                    best_favorable_px=best_fav,
                    book=book,
                    close_ms=sb_ts + int(entry_interval_ms),
                    bars5=_b5_for_read, fill_bar_ts=_fill_bar_ts)
                # ★ 更新在判定**之后** —— 不许用本根自己的最好值给自己开路（根内前视）。
                if not _closed:
                    best_fav = _update_best_fav(thesis, best_fav, sb)
                if _closed:
                    break
                continue

        if state in TERMINAL_STATES:
            continue

        # ---- 到粗周期收盘了 ----
        if state == EXPECTED:
            if verdict == VIOLATED:
                ok = True
                c_ev = {}
                if claim is not None and not ask_on_subs:
                    ok, c_ev = claim(i, bars, book)
                emit(INVALIDATED, VIOLATED,
                     "预期没成立就先被推翻 → 压根不挂单",
                     {**ev, "claim": c_ev}, ts)
                continue
            if not ask_on_subs:
                # 粗周期确认（三引线 / EMA 拒绝走这条）：收盘成立才挂单
                ok = True
                c_ev = {}
                if claim is not None:
                    ok, c_ev = claim(i, bars, book)
                if ok:
                    emit(ARMED, SATISFIED,
                         "预期成立 → 挂 %s %s @ %.2f"
                         % (thesis.order_type, thesis.side, thesis.order_price),
                         {**ev, "claim": c_ev,
                          "order_price": thesis.order_price,
                          "order_type": thesis.order_type, "side": thesis.side},
                         ts,
                         Action(kind="place_order", thesis_id=thesis.thesis_id,
                                reason="thesis_armed", price=thesis.order_price,
                                side=thesis.side, order_type=thesis.order_type,
                                state_after=ARMED))
                    # 同 split 的保守约定：本根 h/l 属于过去，不判成交
                    armed_at_bar = i
                    armed_ref_close = float(bar.c)
            continue

        if state == ARMED:
            # 这一桶有 5m 时，成交判据已在子循环里逐根判过了；
            # 这里只剩失效 / 预期衰减 / 超期（撤单不看 h/l，看收盘）。
            # ★ 超期计数只有一个来源：细周期挂的单按 5m 数（子循环里），
            #   粗周期挂的单按 15m 数（这里）。两边都数 = 容忍窗口减半且不报错。
            _armed_bar_for_coarse = (None if armed_at_sub_ts is not None
                                     else armed_at_bar)
            _armed_coarse_checks(thesis, verdict, ev, i, ts,
                                 _armed_bar_for_coarse, emit,
                                 close=float(bar.c),
                                 armed_ref_close=armed_ref_close)
            continue

        if state == FILLED:
            # ★ 有 5m 时，止损/止盈已经在子循环里逐根判过了。
            #   这里**只能**处理「收盘被推翻 → 按收盘价平」，绝不能再拿
            #   15m 的 h/l 判一次止损：那等于把子循环刚判对的先后顺序
            #   又抹平回粗粒度（实测就是「入场前那根的下探」被误当成
            #   入场后的止损）。没有 5m 时才退回粗周期全判。
            #
            # ★ 顺序很重要：**价格事实优先于时间**。
            #   同一根里既到止损又到持仓上限，应该是止损（真实成交价），
            #   不是「按收盘价平」。第一版把时间闸写在前面，等于让
            #   「拿太久」覆盖「打到止损」，归因和价格都错。
            # ★ 这一块**只处理「收盘被推翻」**。
            #
            #   以前还有一条 `else: _close_position(bar, ...)` 分支，想给
            #   「没有 5m」的情况兜底。那条分支是**冗余的**：`seq = subs or
            #   [bar]`，所以同一根 bar 刚刚已经在子循环里被判过一次
            #   `_close_position(…, PENDING, …)`，同一个函数、同一根 K、
            #   同样的入参 —— 再判一次必然得到同样的结果（不是 True 就是 False）。
            #   留着它等于埋着一个「同一根判两次止损」的陷阱：哪天 `seq`
            #   的退化方式变了，它就会真的重复判，而那正是上面注释警告的事。
            #
            #   所以这里只剩失效平仓；价格事实（止损/止盈）由子循环独占。
            closed = False
            if verdict == VIOLATED:
                closed = _close_on_invalidation(thesis, bar, ev, emit, ts)
            if closed:
                continue
            # ★ 2026-11-17 用户裁决：**不设持仓时间闸**。
            #
            #   原话：「持仓时长我还是坚持用预期与现实管理，我们做日内短线
            #   基本不可能持仓超过 20h，10h 都难，如果你会测发现持仓那么久
            #   肯定有问题，说明对现实的认知不够！」
            #
            #   所以「拿太久」不能靠时钟治，要靠**预期与现实对不上就退出**。
            #   实测那笔 20.2 小时的：行情先朝有利方向走了 2.53R，
            #   没到 4R 止盈就反转回止损 —— 根因是**止盈比现实能给的距离远**
            #   （`tools/_diag_target_reach.py`），不是时钟。
            #   用时间闸兜住它等于把「目标不合理」藏起来。
            continue

    res.state = state
    # ★ 续跑所需的三件事一起返回（见 `RunResult.armed_at_bar`）。
    #   `next_index` 取「实际走到的下一根」，而不是 `len(bars)`：
    #   进终态之后循环 break，剩下的 K 没走，游标不能跳过去。
    res.armed_at_bar = armed_at_bar
    res.best_favorable_px = best_fav
    res.armed_ref_close = armed_ref_close
    res.armed_at_sub_ts = armed_at_sub_ts
    # ★ EXIT-1：把成交那根的锚点带出去（增量推进/重启要用它）
    res.fill_bar_ts = int(_fill_bar_ts or 0)
    res.next_index = max(0, min(len(bars), _walked))
    # ★ 非终态收尾必须说清原因（以前完全静默）。
    if state not in TERMINAL_STATES:
        res.unresolved_reason = {
            EXPECTED: "数据结束时还没等到 claim 成立（形态那根的 ts 可能不在序列里）",
            ARMED: "数据结束时还挂着，既没成交也没失效/超期",
            FILLED: "数据结束时还持仓，既没到止损/止盈、也没被预期推翻",
        }.get(state, "数据结束时停在非终态 %s" % state)
    return res


def _close_on_invalidation(thesis: Thesis, bar: Any, ev: Dict[str, Any],
                           emit, ts: int) -> bool:
    """只按「预期被推翻」平仓（按收盘价），**不看 h/l**。

    和 `_close_position` 的区别：那个还要判止损/止盈（用影线），这个只认
    15m 收盘。双节奏下止损止盈已经在 5m 上判完了，收盘时再用 15m 的
    h/l 补一刀就是重复且更粗的判决。
    """
    emit(CLOSED_THESIS, VIOLATED,
         "成交后现实不符合预期 → 按收盘价平掉",
         {**ev, "exit_price": float(bar.c)}, ts,
         Action(kind="close", thesis_id=thesis.thesis_id,
                reason="thesis_invalidated", price=float(bar.c),
                state_after=CLOSED_THESIS))
    return True


def step(thesis: Thesis,
         bars: Sequence[Any],
         book: LineBook,
         claim: Optional[ClaimFn] = None,
         progress: Optional[Dict[str, Any]] = None,
         **kw) -> Tuple[RunResult, Dict[str, Any]]:
    """★ 生产入口：把状态机**往前推一段**，返回 `(这一段的结果, 新游标)`。

    为什么生产不能直接用 `run()`

      `run()` 是回放：喂进一段完整 K，吐一个终态，整条 thesis 的生命周期
      只存在于一次函数调用里。生产环境里引擎每根 K 推进一次，两次推进之间
      进程可能重启 —— 局部变量活不过重启，而**交易所那头挂着的单还活着**。
      忘了状态就会重复挂单 / 永远不平仓。

    为什么它和 `run()` 不会分叉

      因为 `step()` **就是** `run()`：它没有第二套判定逻辑，只是把
      「从哪根开始、接什么状态、两个派生值是什么」这四件事显式传进去。
      回放 = 一次 step 走完；增量 = 每根 step 一次。两者是同一个函数，
      这条不变式由 `tests/test_thesis_incremental.py` 在 11 条真实计划上钉住。

    Args:
        progress: 上一次 `step()` 返回的游标（也可以来自
            `pending_thesis_store.progress_of(row)`）。None = 从头开始。

    Returns:
        `(RunResult, progress)`。新 progress 要**在动作真正落地之后**才落库，
        否则「游标前移了但单没挂」会漏掉一次挂单。
    """
    p = dict(progress or {})
    start = int(p.get("next_index", kw.pop("start_at", 0)))
    state = p.get("state") or kw.pop("initial_state", EXPECTED)
    resume = {"armed_at_bar": p.get("armed_at_bar"),
              "best_favorable_px": p.get("best_favorable_px"),
              "armed_ref_close": p.get("armed_ref_close"),
              "armed_at_sub_ts": p.get("armed_at_sub_ts")}
    # ★ EXIT-1：成交那根的开盘时刻必须跨调用/跨重启传下去。
    #   它在一次 step 内部是"亲眼看到的成交那根"，但下一次 step 只从
    #   next_index 往后走，看不到那一根了 —— 不传就会把锚点丢掉，
    #   收盘跌破判据静默失效（而失效的表现是"什么都不发生"，最难发现）。
    if not kw.get("fill_bar_ts"):
        kw["fill_bar_ts"] = int(p.get("fill_bar_ts") or 0)
    res = run(thesis, bars, book, claim, start_at=start, initial_state=state,
              resume=resume, **kw)
    newp = {"state": res.state, "next_index": int(res.next_index),
            "armed_at_bar": res.armed_at_bar,
            "best_favorable_px": res.best_favorable_px,
            "armed_ref_close": res.armed_ref_close,
            "armed_at_sub_ts": res.armed_at_sub_ts,
            # 首次成交时由 `run()` 返回；之后一路沿用
            "fill_bar_ts": int(getattr(res, "fill_bar_ts", 0)
                               or kw.get("fill_bar_ts") or 0)}
    return res, newp


def _armed_coarse_checks(thesis: Thesis, verdict: str, ev: Dict[str, Any],
                         i: int, ts: int, armed_at_bar: Optional[int],
                         emit, *,
                         close: Optional[float] = None,
                         armed_ref_close: Optional[float] = None) -> None:
    """已挂单、粗周期收盘时的三个「不看 h/l」的判决：
    失效撤单 / **预期衰减撤单** / 超期撤单。

    ★ 只在**子循环之后**那一处调用（粗周期收盘）。子循环里不能调：
      5m 中途收盘不代表 15m 收盘，那时既不能撤单，也不能算超期。
      （第一版在子循环里写了一个 `sb is bar` 的调用点，有 5m 时永远不成立、
        没 5m 时又和外面那次重复 —— 两处判决点迟早会漂，删掉。）
    """
    if verdict == VIOLATED:
        emit(CANCELLED, VIOLATED,
             "已挂单，但预期破功 → 撤单",
             {**ev, "cancel_reason": "thesis_invalidated"}, ts,
             Action(kind="cancel", thesis_id=thesis.thesis_id,
                    reason="thesis_invalidated",
                    state_after=CANCELLED))
        return
    # ★ 预期衰减：反转家族在挂单期**唯一**的破功判据（2026-11-17）。
    #
    #   趋势家族有 ema 负责撤单；反转家族原来一条都没有 —— 挂着的几小时里
    #   价格怎么走都不会撤。用户要的是「在预期和现实之间根据 K 线演化
    #   止盈或者止损」，反转只兑现了成交后的止损。
    #
    #   判据：从**挂单那一根收盘**起算，价格朝**远离触发价**的方向
    #   走满 `stale_distance` → 这个形态已经不是「马上要突破」了，撤。
    #   方向复用 `triggers_when_price_falls`，不另写一份。
    if (thesis.stale_distance > 0 and close is not None
            and armed_ref_close is not None):
        # 要往下成交（LIMIT 买 / STOP 卖）→ 涨价是远离；反之跌价是远离
        adverse = (float(close) - float(armed_ref_close)) \
            if thesis.triggers_when_price_falls \
            else (float(armed_ref_close) - float(close))
        if adverse >= float(thesis.stale_distance):
            emit(CANCELLED, PENDING,
                 "价格朝反向走了 %.2f 点（≥ %.2f）→ 形态过期，撤单"
                 % (adverse, thesis.stale_distance),
                 {**ev, "armed_ref_close": float(armed_ref_close),
                  "close": float(close), "adverse": round(adverse, 2),
                  "stale_distance": float(thesis.stale_distance),
                  # ★ 机器可读的撤单原因。`Transition.reason` 是给人看的中文，
                  #   归因要靠这个 —— 否则「为什么撤的」只能去正则匹配散文。
                  "cancel_reason": "thesis_stale",
                  "rule": "从挂单那根收盘起算，远离触发价 ≥ stale_distance"},
                 ts,
                 Action(kind="cancel", thesis_id=thesis.thesis_id,
                        reason="thesis_stale",
                        state_after=CANCELLED))
            return
    if thesis.horizon_bars and armed_at_bar is not None:
        if i - armed_at_bar >= int(thesis.horizon_bars):
            emit(EXPIRED, PENDING,
                 "挂单超过 %d 根仍未成交 → 撤单"
                 % thesis.horizon_bars,
                 {"armed_at_bar": armed_at_bar, "now_bar": i,
                  "cancel_reason": "thesis_expired"},
                 ts,
                 Action(kind="cancel", thesis_id=thesis.thesis_id,
                        reason="thesis_expired",
                        state_after=EXPIRED))


# ---------------------------------------------------------------- 观察线拒绝出场

#: 出场侧「现实跟随」用的**观察线**（关键价）。
#:
#: 用户 2026-11-20 原话：
#:   「所有的关键价 ema20/50/144，前高等都是关键价，关键价在我们系统内
#:     都是观察线，观察反应，最终在 ema50 价格显示拒绝，我们出场」
#:
#: ★ 为什么是这几条：都是**慢线/结构位**。5m 的 ema 是噪声，放进来会让
#:   每一根插针都变成出场信号（那正是「ema20 被反复突破所以没用」的同一种病）。
#: ★ 顺序不影响结果：命中一条就出，`keys` 只决定证据里写哪条。
OBSERVATION_KEYS = (TREND_SLOW_KEY, EMA144_LINE_KEY, SLOW_LINE_KEY,
                    "prior_high@15m", "prior_low@15m")

#: **必须**装进 LineBook 的线 —— 缺一条就是真缺陷。
#:
#: ★ 为什么要集中成一个常量：`main.py`（生产）和
#:   `tools/_smoke_pending_plan.py`（探针/测试）各自硬编码过一份装线名单，
#:   两份都只装了 4 条（SLOW/FAST + 两条 range_soft），于是
#:   `ema50@15m` / `ema144@15m` / `prior_high@15m` / `prior_low@15m`
#:   **一条都没装**。后果全是静默的：
#:     · `observation_reject` 的 5 条观察线里 4 条恒 None，
#:       「关键价拒绝出场」只可能在 ema20 上触发 ——
#:       而 11-20 那笔真实出场靠的正是 ema50；
#:     · `pending_plan.trend_context` 缺 ema50 → 判不了趋势背景 → 保守拒单。
#:
#:   ★ 2026-11-20 上生产时验证过：生产 `kbar_line` 有 28 条线，
#:     这 5 条**全都在**（各 1.7~1.9 万行）—— 也就是说数据一直有，
#:     只是从来没被装进 LineBook。所以这是纯粹的装载缺陷。
#:
#: ★ 1h 区间线（`range_*@1h`）不在这里：它们的名字由
#:   `kbar.lines::RANGE_LINE_KINDS` 决定，装线方各自拼。
REQUIRED_LINE_KEYS = tuple(dict.fromkeys(
    (SLOW_LINE_KEY, TREND_SLOW_KEY) + OBSERVATION_KEYS))

#: **可选**的线：缺了是**已知状态**，不是缺陷，不许当错误喊。
#:
#: ★ `ema20@30m`（`FAST_LINE_KEY`）是早期设计里的「快路径」，
#:   生产 `kbar_line` 的 28 条线里**从来没有它**，而 `guard_preconditions`
#:   有明确的回退链 FAST → SLOW（`ema20@15m`）。
#:
#: ★ 为什么要单独分一类：装线自检如果把它算成「缺线 ERROR」，
#:   每次启动都会喊一条**永远不会被修的**警 —— 这正是本仓库反复
#:   写下的那条教训（`main.py` 里 kbar.db 的告警注释：
#:   「喊狼来了的次数多了，真的那条也就没人看了」）。
#:   第一次上生产就这么喊了一次，所以这里立刻分开。
OPTIONAL_LINE_KEYS = (FAST_LINE_KEY,)


def observation_reject(thesis: "Thesis", bar: Any, book: Any,
                       close_ms: int) -> Optional[Dict[str, Any]]:
    """持仓中：价格**穿透某条观察线又收回来** → 那条线拒绝了这段行情，出场。

    判据（做多，做空镜像）：

        bar.h > line    穿到线上方（真的去试了那条线）
        bar.c < line    收盘回到线下方（没站住 = 拒绝）
        line > 入场价   这条线在**有利方向**，才算「到过目标区」

    ★ 为什么是收盘而不是影线：拒绝的定义就是「收不上去」。用影线判，
      任何一次插针都会触发 —— 用户说的「ema20 在前面被反复突破过了」
      正是「每次都被穿过去」，而 ema50 那次是「穿上去又收回来」。
      两者唯一的区别就是**收盘在哪一侧**。

    ★ 为什么需要 `line > 入场价`：做多时只有上方的线才可能是「到了目标区
      被拒」。否则一根下影线扫到下方某条线就平仓，等于把止损提前。

    ★ 已知短板（用户 2026-11-20 也点到了）：没有做「这条线是不是已经
      被反复突破过」的有效性过滤 —— 一条贴得很近、刚被反复穿越的线
      也可能触发。先按「穿透+收回」这个物理事实判，等实测样本再谈有效性。
    """
    if book is None or close_ms is None:
        return None
    entry = float(thesis.order_price)
    up = (thesis.direction == UP)
    best = None
    for key in OBSERVATION_KEYS:
        pt = book.at(key, int(close_ms))
        if pt is None:
            continue
        line = float(pt.price)
        if line <= 0:
            continue
        if up:
            if line <= entry:                 # 只在有利方向（上方）
                continue
            pierced = float(bar.h) > line
            back = float(bar.c) < line
        else:
            if line >= entry:                 # 做空：只在下方
                continue
            pierced = float(bar.l) < line
            back = float(bar.c) > line
        if not (pierced and back):
            continue
        # 同根命中多条线时，取**离收盘最近**的那条（它才是真正压住价格的那条）
        dist = abs(float(bar.c) - line)
        if best is None or dist < best["dist"]:
            best = {"key": key, "line": line, "dist": dist,
                    "bar_h": float(bar.h), "bar_l": float(bar.l),
                    "bar_c": float(bar.c), "src_ts": pt.src_ts}
    if best is None:
        return None
    best.pop("dist", None)
    best["exit_price"] = float(bar.c)
    best["rule"] = ("穿透观察线后收盘收回 → 那条线拒绝这段行情，按收盘价出场"
                    "（现实跟随，不等固定止盈）")
    return best


def _close_position(thesis: Thesis, bar: Any, verdict: str,
                    ev: Dict[str, Any], i: int, emit, ts: int,
                    entry_ref: Optional[float] = None,
                    kbar: Any = None,
                    ratchet_stop: Optional[float] = None,
                    best_favorable_px: Optional[float] = None,
                    book: Any = None,
                    close_ms: Optional[int] = None,
                    bars5: Optional[Sequence] = None,
                    fill_bar_ts: int = 0) -> bool:
    """持仓中：判止损 / 止盈 / 惊喜K线 / 预期失效平仓。返回是否已经平掉。

    `entry_ref`：**成交那一根**的实际成交价（其它根传 None）。惊喜K线判据
    要用它当基准，否则盘中成交的那根会把「本来就很宽的大K」误算成利润。

    `bars5` / `fill_bar_ts`：EXIT-1 用。已收盘的 5m 序列 + 成交所在那根的
    开盘时刻。没有 5m 数据时传 None —— 那时**不判**收盘跌破，
    而不是拿 15m 冒充 5m（用户的定义是「5m 收盘」，用粗周期判等于换了个规则）。
    """
    stop = _stop_hit(bar, thesis)
    tgt = _target_hit(bar, thesis)
    surprise = _surprise_hit(thesis, bar, entry_ref)
    # ---- S5 现实跟随出场 ----
    panic = panic_exit(thesis, kbar)
    stall = stall_exit(thesis, kbar)
    r_stop = None
    if ratchet_stop is not None:
        hit = (_below(bar.l, ratchet_stop) if thesis.direction == UP
               else _above(bar.h, ratchet_stop))
        if hit:
            r_stop = float(ratchet_stop)

    # ★ 惊喜K线（2026-11-17 用户要求）：
    #   「任何一根 5m 的持仓顺向涨跌幅达到 500U，都要立即止盈，因为很难得！
    #     我们不是长线，遇到这种惊喜K线必须走」
    #
    #   放在止损/止盈**之后**、失效**之前**，理由：
    #     · 止损优先 —— 同一根既砸穿止损又顺向 500 点，只可能是巨震，
    #       那是同根歧义，保守侧按止损（不会让样本变好看）；
    #     · 止盈优先于惊喜 —— 正常止盈价更远，谁先到谁结算；
    #     · 但必须优先于**失效平仓**：失效平仓是按收盘价走，
    #       而顺向 500 点那一根是最难得的一次机会，按收盘价比按目标差得多。
    # ★★ S5 优先级：
    #   1. 棘轮止损 —— 它离价格更近，价格向不利方向走必然先碰到它；
    #      先判原止损会把一笔本可保本的单记成亏一个 R。
    #   2. 恐慌盘顺向 —— 单根行为给的礼物；一根既恐慌又砸穿止损的巨震根，
    #      OHLC 分不清先后，而“这根发生了什么”比“价到了哪”更能说清楚。
    if r_stop is not None:
        emit(CLOSED_BREAKEVEN, SATISFIED,
             "保本棘轮：曾赚到 ≥%.1fR，止损已移到 %.2f → 平掉"
             % (float(thesis.s5_breakeven_r), r_stop),
             {"bar_ts": ts, "ratchet_stop": r_stop,
              "best_favorable_px": best_favorable_px,
              "breakeven_r": float(thesis.s5_breakeven_r),
              "exit_price": r_stop,
              "rule": "浮盈 ≥ s5_breakeven_r → 止损移到保本价（只向有利方向）"},
             ts,
             Action(kind="close", thesis_id=thesis.thesis_id,
                    reason="breakeven_ratchet", price=r_stop,
                    state_after=CLOSED_BREAKEVEN))
        return True
    if panic is not None:
        emit(CLOSED_TP, SATISFIED,
             "恐慌盘顺向（%s，vol_rel %.1f）→ 立即出"
             % (panic.get("side"), float(panic.get("vol_rel") or 0.0)),
             {"bar_ts": ts, "exit_price": float(bar.c), **panic,
              "rule": "单根恐慌盘且方向顺我 → 出场"},
             ts,
             Action(kind="close", thesis_id=thesis.thesis_id,
                    reason="panic_bar", price=float(bar.c),
                    state_after=CLOSED_TP))
        return True
    if STOP_BEFORE_TARGET and stop:
        emit(CLOSED_SL, VIOLATED, "打到止损 %.2f" % thesis.stop_price,
             {"bar_ts": ts, "stop_price": thesis.stop_price,
              "bar_h": float(bar.h), "bar_l": float(bar.l)}, ts,
             Action(kind="close", thesis_id=thesis.thesis_id,
                    reason="stop", price=thesis.stop_price,
                    state_after=CLOSED_SL))
        return True
    if tgt:
        emit(CLOSED_TP, SATISFIED, "打到止盈 %.2f" % thesis.target_price,
             {"bar_ts": ts, "target_price": thesis.target_price,
              "bar_h": float(bar.h), "bar_l": float(bar.l)}, ts,
             Action(kind="close", thesis_id=thesis.thesis_id,
                    reason="target", price=thesis.target_price,
                    state_after=CLOSED_TP))
        return True
    if surprise is not None:
        # 出场价按**这一根的收盘价**：判到它的时候这根已经走完了，
        # 收盘价才是真能拿到的价。生产环境有 3s 行情，可以更早一点成交，
        # 那是实盘更优，纸面**不许**按更优的价格记账。
        emit(CLOSED_TP, SATISFIED,
             "惊喜K线：这一根顺向走了 %.0f 点（≥ %.0f）→ 立即止盈"
             % (surprise, thesis.surprise_pts),
             {"bar_ts": ts, "surprise_points": round(surprise, 2),
              "surprise_pts": float(thesis.surprise_pts),
              "exit_price": float(bar.c),
              "bar_h": float(bar.h), "bar_l": float(bar.l),
              "rule": "单根 5m 顺向幅度 ≥ surprise_pts 立即止盈"},
             ts,
             Action(kind="close", thesis_id=thesis.thesis_id,
                    reason="surprise_bar", price=float(bar.c),
                    state_after=CLOSED_TP))
        return True
    if stop:
        emit(CLOSED_SL, VIOLATED, "打到止损 %.2f" % thesis.stop_price,
             {"bar_ts": ts, "stop_price": thesis.stop_price}, ts,
             Action(kind="close", thesis_id=thesis.thesis_id,
                    reason="stop", price=thesis.stop_price,
                    state_after=CLOSED_SL))
        return True
    # ★★ EXIT-1（2026-11-25 用户口径）：「收盘跌破」与「有效跌破」。
    #
    #   用户原话：
    #     「收盘跌破就是在5m的买点，收盘5m跌破位置，本来预期收针的，
    #       但是现实是收盘跌破了，不是引针了；有效跌破是后面3根K线有过
    #       确认动作，也就是从收盘跌破那根K线之后，不包含收盘跌破那根，
    #       第三根还是跌破！」
    #
    #   规则的实现全部在 `break_rules.py`（纯函数，不碰 IO），这里只负责
    #   把判读变成一条 `close` 动作、并决定用的是哪个价。
    #
    #   ★ 为什么排在止盈/止损/惊喜**之后**：
    #     收盘跌破只有等那根收盘才知道，而同一根里价格可能早就把止损打穿了 ——
    #     把它排到止损前面，等于用收盘才有的信息去抢一个盘中先发生的成交，
    #     那是偷价，回测会虚高。
    #   ★ 为什么排在观察线拒绝/失效**之前**：
    #     前两者是"行情给出了别的答案"，而收盘跌破是"入场前提被否" ——
    #     前提没了就不该再用前提推出来的目标价去等。
    if bars5 and fill_bar_ts and float(thesis.order_price or 0.0) > 0:
        _long = (thesis.direction == UP)
        _read = _br.assess(
            float(thesis.order_price), bars5,
            since_ts=int(fill_bar_ts),
            direction=("long" if _long else "short"))
        if _read.state == _br.CONFIRMED:
            # 有效跌破：确认之后不许再等回抽 —— 市场已经用 3 根 K 线表态了。
            emit(CLOSED_THESIS, VIOLATED,
                 "有效跌破：%s" % _read.describe(),
                 {"bar_ts": ts, "exit_price": float(bar.c),
                  "break_state": _read.state, "break_level": _read.level,
                  "anchor_ts": _read.anchor_ts,
                  "anchor_close": _read.anchor_close,
                  "bars_since": _read.bars_since, "atr": _read.atr,
                  "rule": ("入场前提是「关键位被探一下就被接住（收针）」。"
                           "锚点那根收盘跌破它，锚点后第 %d 根仍跌破 → "
                           "确认不是扫盘，不等回抽，市价离场" % _br.CONFIRM_BARS)},
                 ts,
                 Action(kind="close", thesis_id=thesis.thesis_id,
                        reason="effective_break", price=float(bar.c),
                        state_after=CLOSED_THESIS))
            return True
        if _read.state == _br.CLOSED_BELOW:
            _px = None
            if _read.bars_since == 0:
                # 锚点根本身：跌破只在**这根收盘**成立，所以只能用收盘价判。
                # （不许用本根的高点/低点回推"本来能在回抽位成交"——
                #   那时候我们还不知道自己错了。）
                _go, _why = _br.exit_now(_read, float(bar.c))
                if _go:
                    _px = float(bar.c)
            else:
                # 窗口已开：整根 K 线期间回抽单都挂着，碰到就是成交。
                _hit = (float(bar.h) >= _read.exit_level) if _long \
                    else (float(bar.l) <= _read.exit_level)
                if _hit:
                    _px = float(_read.exit_level)
            if _px is not None:
                emit(CLOSED_THESIS, VIOLATED,
                     "收盘跌破后离场：%s" % _read.describe(),
                     {"bar_ts": ts, "exit_price": _px,
                      "break_state": _read.state, "break_level": _read.level,
                      "exit_level": _read.exit_level,
                      "anchor_ts": _read.anchor_ts,
                      "anchor_close": _read.anchor_close,
                      "bars_since": _read.bars_since, "atr": _read.atr,
                      "rule": ("「本来预期收针、现实收盘跌破」→ 前提被否。"
                               "认错但不追低：盯回抽位（关键位前方一步，"
                               "保证成交）走人；等不到就由有效跌破兜底")},
                     ts,
                     Action(kind="close", thesis_id=thesis.thesis_id,
                            reason="closed_below_entry", price=_px,
                            state_after=CLOSED_THESIS))
                return True
    # ★★ 观察线拒绝出场（2026-11-20 用户口径：「关键价都是观察线，观察反应，
    #    最终在 ema50 价格显示拒绝，我们出场」）。
    #
    #    位置在**固定止盈之后**：止盈价更远、给的价格更好，谁先到谁结算；
    #    在**止损之后**：同一根既砸穿止损又碰到某条线，那是同根歧义，
    #    保守侧按止损（不让样本变好看）。
    #    在**预期失效之前**：失效是按收盘价平（证据驱动的次优解），
    #    而观察线拒绝是「行情自己给了答案」，价格同样取收盘，但归因不同。
    if book is not None and close_ms is not None:
        rej = observation_reject(thesis, bar, book, close_ms)
        if rej is not None:
            emit(CLOSED_TP, SATISFIED,
                 "观察线拒绝：%s @ %.2f（穿透后收回到 %.2f）→ 出场"
                 % (rej["key"], float(rej["line"]), float(rej["bar_c"])),
                 {"bar_ts": ts, **rej},
                 ts,
                 Action(kind="close", thesis_id=thesis.thesis_id,
                        reason="observation_reject", price=float(bar.c),
                        state_after=CLOSED_TP))
            return True
    if stall is not None:
        emit(CLOSED_THESIS, SATISFIED,
             "缩量停滞（%s，vol_rel %.2f < %.1f）→ 现实不给行情，平掉"
             % (stall.get("label"), float(stall.get("vol_rel") or 0.0),
                float(stall.get("cut") or 0.0)),
             {"bar_ts": ts, "exit_price": float(bar.c), **stall,
              "rule": "缩量 + 无动作 → 出场（默认关闭）"},
             ts,
             Action(kind="close", thesis_id=thesis.thesis_id,
                    reason="stall_exit", price=float(bar.c),
                    state_after=CLOSED_THESIS))
        return True
    if verdict == VIOLATED:
        emit(CLOSED_THESIS, VIOLATED,
             "成交后现实不符合预期 → 按收盘价平掉",
             {**ev, "exit_price": float(bar.c)}, ts,
             Action(kind="close", thesis_id=thesis.thesis_id,
                    reason="thesis_invalidated", price=float(bar.c),
                    state_after=CLOSED_THESIS))
        return True
    return False


__all__ = [
    "Thesis", "LineBook", "LinePoint", "Action", "Transition", "RunResult",
    "invalidation_verdict", "run", "step", "close_ms_of",
    "EXPECTED", "ARMED", "FILLED", "INVALIDATED", "CANCELLED", "EXPIRED",
    "CLOSED_TP", "CLOSED_SL", "CLOSED_THESIS", "CLOSED_BREAKEVEN",
    "breakeven_stop", "panic_exit", "stall_exit", "kbar_fields",
    "STATES", "TERMINAL_STATES",
    "PENDING", "SATISFIED", "VIOLATED", "DOWN", "UP",
    "ORDER_LIMIT", "ORDER_STOP", "ORDER_TYPES",
    "SLOW_CONFIRM_BARS", "SLOW_LINE_KEY", "FAST_LINE_KEY", "BAR_INTERVAL_MS",
]
