# -*- coding: utf-8 -*-
"""三条挂单形态 → 变成 thesis 能吃的「预期」。

接在 `trading/thesis.py` 前面：thesis 管「预期成立之后现实有没有打脸」，
本模块管「预期从哪来、挂在哪、用什么类型的单」。

★★ 2026-11-17 用户纠正了这里的**根本机制**，看这一节就够 ★★
-------------------------------------------------------------
我第一版把三条都做成了「价格涨回拒绝位就挂空」—— 那是**反转挂法**：
等价格回来找我。用户纠正：三条里有两条是**突破挂法**，
要「让价格证明方向至少对了一次」才进场。

三条形态的最终口径（用户逐条口述，别自己重新推导）：

| 形态 | 方向 | 挂单价 | 类型 | 成交条件 | 语义 |
|------|------|--------|------|----------|------|
| 三引线假突破 | 做多 BUY | min(第1根低,第2根低) − 1 tick | **LIMIT** | low ≤ 价 | 回落接货 |
| EMA20 多次拒绝 | 做空 SELL | 结构低点 a − 1 tick | **STOP** | low ≤ 价 | 跌破 a 才追空 |
| 倒 V 反包 | 做多 BUY | max(倒V根高, 当前根高) + 1 tick | **STOP** | high ≥ 价 | 涨破高点才追多 |

上涨镜像三条全部实现（方向取反、价位规则对称、订单类型不变）。

★★ 名字 vs 方向：两者**不是一回事**，别把它们绑在一起 ★★
----------------------------------------------------------
形态名字说的是**语境**（这个形态出现在下跌里还是上涨里），
`direction` 说的是**交易方向** —— 所以它们经常相反：

    下跌三引线假突破限价单      direction = up    （下跌里做多）
    下跌逆向倒V反包突破限价单    direction = up    （下跌里做多）
    下跌中继EMA多次拒绝上引线单  direction = down  （下跌里做空）

用户原话「上涨时是镜像实现」：基准版一律叫「下跌…」，镜像版一律叫「上涨…」。
判据是 `direction != PATTERN_SPEC[kind]["direction"]`（跑的是不是镜像版），
**不是** `direction == UP`。后者只对 EMA 拒绝碰巧成立，会把前两条的名字对调
—— 不报错，只是统计/日志/订单备注全错。见 `_variant_name()`。

**同一个价位，LIMIT 和 STOP 的成交条件完全相反**，所以 `thesis.Thesis`
必须带 `order_type`。这是这一轮最关键的结构性改动 —— 只按方向判成交，
三条里有两条会判反。

三条形态的完整叙述（用户原话整理）
----------------------------------
* **三引线**：连续几根 K 线的影线反复试探同一侧，但每次都被打回。
  取**前两根**的极值（第 1、2 根，不含确认用的第 3 根），
  再往外让一个 tick 挂 LIMIT —— 意思是「再探一次我就接」。
* **EMA20 多次拒绝**（下跌）：价格失效地站上 ema20 → 后面几根出上引线
  → 跌破 ema20 往下砸 → 砸出低点 **a** → 反弹 → 这次可能只碰一下
  ema20 或压根没碰到就被拒 → **价格再次突破 a 才进场**。
  a 取「最后一根 ema20 拒绝上引线 K 之前的结构位低点」，
  通常不超过 10 根 K 线。所以扳机是 a，不是 ema20 ——
  ema20 只负责证明这是下跌中继的反弹。
* **倒 V 反包**（做多）：倒 V 发生在**单根 5m K 线内部**（冲高立马被打回，
  留下一根长上影）。看到它之后，预期下一根 5m 再冲一次、
  并且**收盘反包**这根倒 V。反包成立就在
  `max(倒V根的高, 当前根的高)` 上方一个 tick 挂 BUY STOP。

已定价位口径（§18.3，与形态无关）
--------------------------------
* 止盈 = 1h 振幅 × 1。
* 止损宽度 = max(1h 振幅 × stop_frac, 入场 5m 振幅)，`stop_frac` 留 1/4 与 1/2 两档。
* 止损参照 = 前两根已收盘 1h 里 `h−l` **较大**的那根。
* 区间价位只用 1h；破位判定用 1h 收盘。

★ 开关 `ALLOW_PENDING = False`：默认只算计划、只写日志，**一张单都不发**。

★ 我推的、可能不是用户本意的部分（都做成显式参数）
--------------------------------------------------
1. 各形态的**形状阈值**（几根、影线占比、回看根数）全是**未验证初值**，
   集中在 `PendingParams`，代码里没有任何阈值写死在别处。
2. `tick_size = 0.1`（BTCUSDT 实测价格只到 1 位小数）。
3. 倒 V 的「收盘反包」判定：当前 5m 收盘 ≥ 倒 V 根的**开盘价**
   （即吞掉它的实体），且当前根是阳线。
4. 倒 V 的形态用**5m 影线形状**近似「bar 内先冲高再被打回」。
   真正的 bar 内先后顺序 OHLC 判不了 —— 要判得用 1m/tick，
   而库里 1m 只有约 0.9 天、tick 从来没落库。这是**已知近似**，不是疏漏。

★ 真实数据上才会遇到的「已经晚了」
----------------------------------
计划在**一根 K 线收盘时**才算成立，但触发价往往来自更细的周期
（倒 V 的触发价是两根 5m 的高点）。等那根 15m 收盘时，价格可能早就
穿过触发价了 —— 这时候再挂就是**立刻成交的市价追单**。
所以 `_build()` 会发现「现价已经在触发价的另一侧」并跳过，
记账为 `already_triggered`（**计数**，不静默丢）。
这一条是真实数据冒烟抓出来的，单测的合成数据跨度太小，撞不到。
"""
from __future__ import annotations

import bisect
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import DEFAULT_SURPRISE_PTS
from . import thesis as T

# ---------------------------------------------------------------- 开关

#: ★ 总开关。False = 只算计划、只写日志，**不下单**。
ALLOW_PENDING: bool = False

#: 方向
DOWN = T.DOWN
UP = T.UP

#: 三条形态在**下跌行情**里的名字（用户定的，别改）
KIND_WICKS = "下跌三引线假突破限价单"
KIND_INVERSE_V = "下跌逆向倒V反包突破限价单"
KIND_EMA_REJECT = "下跌中继EMA多次拒绝上引线突破限价单"

KINDS = (KIND_WICKS, KIND_INVERSE_V, KIND_EMA_REJECT)

#: 每条形态的方向与订单类型（用户 2026-11-17 定，别自己改）。
#: 注意这三条**不是同一个方向、也不是同一个类型** ——
#: 第一版按「下跌就都做空、都挂上方」实现，三条全错。
PATTERN_SPEC: Dict[str, Dict[str, Any]] = {
    KIND_WICKS: {"direction": UP, "order_type": T.ORDER_LIMIT},
    KIND_INVERSE_V: {"direction": UP, "order_type": T.ORDER_STOP},
    KIND_EMA_REJECT: {"direction": DOWN, "order_type": T.ORDER_STOP},
}


def mirror_kind(kind: str) -> str:
    """下跌 → 上涨 的形态名。用户要求上涨镜像全部实现。"""
    if kind.startswith("下跌"):
        return "上涨" + kind[2:]
    return kind


def _variant_name(kind: str, direction: str) -> str:
    """按「跑的是基准版还是镜像版」给计划命名。

    ★ 判据是 `direction != PATTERN_SPEC[kind]["direction"]`，**不是方向本身**。

    用户原话：「上涨时是镜像实现」。所以基准版用「下跌…」的名字，
    镜像版用「上涨…」的名字。三条形态的**基准交易方向并不相同**：
        三引线   基准方向 UP（下跌里做多）→ 基准版叫「下跌三引线」
        倒 V     基准方向 UP（下跌里做多）→ 基准版叫「下跌逆向倒V」
        EMA 拒绝 基准方向 DOWN（下跌里做空）→ 基准版叫「下跌中继EMA」

    如果写成 `mirror if direction == UP else kind`，对 EMA 拒绝碰巧对，
    对三引线/倒 V 正好把基准版和镜像版的名字**对调**——
    不报错，只是统计/日志/订单备注全错。所以这里必须用基准方向比。
    """
    base = PATTERN_SPEC[kind]["direction"]
    return kind if direction == base else mirror_kind(kind)


#: ★ 每条形态属于哪个**家族** —— 失效判据的轴（用户 2026-11-17 口径）。
#:
#:   * ``reversal``（反转：大跌抄底做多 / 上涨摸顶做空）→ 只看止损价
#:   * ``trend``（趋势：下跌反弹做空 / 上涨回调做多）→ ema 打脸即失效
#:
#: ★ 这是一份**独立于方向**的映射：三条形态基准版都在下跌背景，
#:   但三引线与倒V 是「下跌里抄底做多」（反转），EMA 拒绝是
#:   「下跌里反弹做空」（趋势）。**别从 direction 推**，那正是 §23.23 的错。
PATTERN_FAMILY: Dict[str, str] = {
    KIND_WICKS: T.FAMILY_REVERSAL,
    KIND_INVERSE_V: T.FAMILY_REVERSAL,
    KIND_EMA_REJECT: T.FAMILY_TREND,
}


# ---------------------------------------------------------------- 参数

@dataclass(frozen=True)
class PendingParams:
    """所有形状阈值与价位口径。**集中在一处**，别散到判断里。

    ★ 标「未验证」的是我拍的初值，没有回测支撑。
    """

    # ---- 形态 1：三引线假突破
    #
    # ★★ 2026-11-20 用户口径重写（原实现「连续 3 根都有长影且都扎破水位」
    #    是错的，一笔都没出过）。用户原话：
    #      「09:55 和 10:00 两根是 09:50 的 inside bar，10:05 的下引线出现
    #        之后，我们就会在 09:50 和 10:05 的低点+1tick 做买单」
    #      「买点这里也应该优化，给与一定的容忍度，比如 +-10U」
    #      「先找第一根引线，找到了开始记录，预期下一根也出引线，但是发现
    #        第2/3都没出，且是记录的第一根的 inside bar，是不是作废，然后
    #        第4根出现了…条件满足开始挂单，第5根入场了！我的意思是容忍2根」
    #
    #    结构 = 参考引线 A（这一段的最低点，且有下影）
    #          + 中间 0~wick_tolerance_bars 根 A 的 inside bar
    #          + 确认引线 B（有下影，极值回到 A 附近）
    #   判据**全部是位置关系**，没有「影线占比」这种随振幅缩放的百分比：
    #   振幅越大比例越小，等于系统性惩罚大波动 K（这正是老实现 0 单的原因）。
    wick_tolerance_bars: int = 2    # A 与 B 之间最多夹几根 inside bar（用户：容忍 2 根）
    #: 确认引线的极值要落在参考引线极值的这个距离内（点）= 「同一个流动性口袋」。
    #:
    #: ★ **未验证初值**。标定依据（2026-11-20 实测，11-20 09:50~10:10）：
    #:   该出的那根 10:05 距 A 低点 16.6 点；不该出的 09:55 / 10:00 分别
    #:   147 / 405 点。20 落在这两个量级之间，留了 3.4 点余量。
    #:   不许为凑信号放大这个数 —— 放大会让 A 后面每一根擦边的 K 都变成确认根。
    wick_proximity_pts: float = 20.0
    #: 挂单价往**成交方向**让多少点（买点容差）。做多 = 低点 + 容差，
    #: 做空 = 高点 − 容差。用户 2026-11-20 定：10U。⚠ 未回测验证。
    entry_tolerance_pts: float = 10.0
    #: 老口径的「影线 / 整根振幅」下限。**2026-11-20 起三引线不再读它**
    #: （度量本身有问题），保留只为兼容旧测试与配置。
    wick_bars: int = 3              # 连续几根
    wick_min_pct: float = 0.35      # 影线 / 整根振幅 的最小占比

    # ---- 形态 2：倒 V 反包（未验证）
    v_min_wick_pct: float = 0.5     # 倒V根的上影线占比下限（冲高被打回的近似）
    v_lookback: int = 3             # 倒V根要在最近几根 5m 里是高点

    # ---- 形态 3：EMA 多次拒绝（未验证）
    reject_window: int = 10         # 最近几根里数拒绝
    reject_min_bars: int = 2        # 至少几根「触及 ema 但收在线下」
    #: 结构位 a 往前找几根。**0 = 就用最后一根拒绝 K 自己的极值**（默认）。
    #:
    #: ★ 2026-11-17 从 10 改成 0。实测 488 个候选：
    #:   lookback=10 → 成交率 41.7%（先死 280 条）
    #:   lookback=0  → 成交率 69.1%（先死 134 条）
    #:   即「把触发定在 10 根之外」本身扔掉了 27 个百分点的进场机会。
    #: ★ 用户原话「通常不超过 10 根」仍然有效 —— 想回老口径设成 10 即可。
    #: ★ 代价：同根歧义 8 → 54 条（既触及挂单价又收盘穿过 ema，
    #:   5m OHLC 判不了先后），由 `thesis` 打 `ambiguous_same_bar` 单列。
    struct_lookback: int = 0

    #: 「被多次拒绝的线」候选，**由近到远**。检测器取第一条成立的。
    #:
    #: ★★ 2026-11-20 用户口径（原话）：
    #:   「下跌反弹时 ema20 多次拒绝开空，同样适用于 ema50，不知道你实现了没有，
    #:     ema 没有被多次拒绝，被突破后行情来到 ema50，ema50 出现多次拒绝也是
    #:     同样的！上涨回调镜像」
    #:   追问后确认两件事：
    #:     · 范围 = ema20 + ema50 + ema144（**三条都算**）；
    #:     · **不要求**「先被 ema20 拒绝过」这个先后顺序。
    #:
    #: ★ 为什么「近的优先」而不是「谁拒绝得多用谁」：形态的叙事是「价格来
    #:   到哪条线就被哪条线压回」。价格同时被两条线拒绝时，近的那条才是
    #:   它当前真正在博弈的位置；而且近的线止损更近、目标更容易到。
    #:   这条是**判据唯一来源**：检测器与守卫都从这里取，不许各写一份。
    REJECT_LINE_KEYS: Tuple[str, ...] = (T.SLOW_LINE_KEY,      # ema20@15m
                                         T.TREND_SLOW_KEY,     # ema50@15m
                                         T.EMA144_LINE_KEY)    # ema144@15m

    # ---- 价位口径（已定，见 §18.3）
    stop_frac: float = 0.25         # 1h 振幅 × 这份 = 止损宽度（窄档）
    #: ★★ **最远止损距离（%%）—— 唯一的止损口径。** 2026-11-17 用户拍板。
    #:
    #: 用户原话：「完全按照公式来」。所以止损不再由形态决定，
    #: 而是由仓位公式算出的最远距离。
    #:
    #:     d_max = (1/杠杆 - 维持保证金率) × 100 / 安全倍数
    #:
    #: ★ d_max 由 **sizing.py** 算（`derive().max_stop_distance_pct`），
    #:   这里只接收。此处出现任何字面量（比如 1.618）都是错的 ——
    #:   将来公式输入变了（杠杆 22x→33x），d_max 必须自动跟着变。
    #:
    #: 0 = 用旧的振幅规则（仅供历史单测和对比实验）。**生产必须 > 0** ——
    #: `detect_all(require_window=True)` 那类生产入口会校验这一点。
    max_stop_pct: float = 0.0
    stop_frac_wide: float = 0.50    # 宽档
    target_mult: float = 1.0        # 止盈 = 1h 振幅 × 这份（「翻一翻」）
    #: ★ 止盈 = 最远止损 × 这份（4R）。仅在 `max_stop_pct > 0` 时生效。
    #:
    #: 为什么要跟止损同一个基准：以前 target = 1h振幅×1.0、
    #: 止损 = 1h振幅×0.25，于是“R:R = 4.00”是 `1.0/0.25` 的**算术恒等式**，
    #: 不是测出来的（HANDOFF §26 记录过）。现在两条线都从 d_max 来，
    #: 4R 才是真的「4 倍风险」。
    target_r_multiple: float = 4.0
    stop_floor_mult: float = 1.0    # 强制下限 = 入场 5m 振幅 × 这份

    # ---- 线与刻度
    #: EMA 拒绝族「首选」的被拒绝线 = 候选里最近的一条。
    #:
    #: ★ 2026-11-20 之前这里是**唯一**的被拒绝线，所以 ema50/ema144 上的
    #:   多次拒绝根本不在判据里。现在它只是 `REJECT_LINE_KEYS` 的第一条，
    #:   保留这个字段是为了兼容既有调用点（`evidence["ema_key"]`）。
    ema_key: str = T.SLOW_LINE_KEY          # ema20@15m
    resistance_key: str = "range_soft_up@1h"      # 做空：上方压力
    support_key: str = "range_soft_dn@1h"         # 做多：下方支撑
    tick_size: float = 0.1          # 一个 tick（BTCUSDT 价格 1 位小数）

    # ---- 交易成本（2026-11-17 新增，见 §24）
    #:
    #: ★ 为什么必须有：以前成本恒为 0，于是「R:R = 4」被当成好消息 ——
    #:   而实测往返手续费占止损距离**中位 44.4%、最差 71.7%**。
    #:   在 BTCUSDT 永续上，taker 0.05% ≈ 32 点，而 A 级止损只有 75~220 点。
    #:   没有成本项时，任何「这策略好不好」的结论都缺一半。
    fee_maker: float = 0.0002       # 永续 maker 0.02%
    fee_taker: float = 0.0005       # 永续 taker 0.05%
    #: 滑点（以 tick 计）。只在 **taker 腿**上算：
    #: STOP 进场的追价、止损出场的砸盘。LIMIT 挂单成交按不滑点处理。
    #: ★ 实盘应在成交后用真实 fill 价重算，这里只是计划时的估计。
    slippage_ticks: float = 10.0    # = 1.0 点（tick_size 0.1）

    # ---- 挂单存活
    #: 挂上之后最多等几根 **15m**（0 = 不设时间闸，只交给失效判据）。
    #:
    #: ★ 2026-11-17 从 0 改成 12（= 3 小时）。实测挂单到成交的间隔：
    #:   中位 1 根 / 75% 7 根 / 90% 79 根；而未成交的那些一直挂着 ——
    #:   最长一条挂到 18 根（4.5 小时）才被撤。那是完全没有信息的仓位
    #:   占着资金，正是时间闸该清掉的。
    #: ★ 这是**初值**，和 stop_frac 一样没有回测支撑。
    horizon_bars: int = 12
    #: ★ 成交之后最多持几根（0 = 不限）。2026-11-17 新增。
    #:
    #: 以前 `horizon_bars` **只管挂单**，成交之后没有任何时间闸 ——
    #: 实测 4/10 条持仓超过 12 根，最长一条持了 81 根（20.2 小时）
    #: 才等到止损。日内策略不能容忍这个。
    #: ★ 2026-11-17 用户裁决：**不设持仓时间闸**（原话见 `thesis.run` 里
    #: 那段注释）。这里曾经有过 `max_hold_bars = 24`，已删 —— 留着就是
    #: 一个「把目标不合理藏起来」的开关。
    #: ★ 反转家族挂单期的「预期衰减」距离 = 止损距离 × 这份（0 = 关）。
    #:
    #: 反转家族原来在挂单期**完全没有破功判据**：`invalidation_verdict`
    #: 对它恒返回 PENDING，所以挂着的三小时里价格怎么走都不会撤单。
    #: 用户 2026-11-17 要求补上（「在预期和现实之间根据 K 线演化」）。
    #: 语义：从挂单那根收盘起算，朝**远离触发价**的方向走满这么多 → 撤单。
    #: ★ 1.0R = 走掉一个完整的止损距离还没触发，形态已经不新鲜了。
    #: ★ 初值，没有回测支撑。
    stale_stop_mult: float = 1.0

    #: 是否在计划生产路径上执行第③道闸（`guard_preconditions`）。
    #:
    #: ★ 2026-11-17 接进 `_build` 之后，默认就是 True。
    #:   留这个开关是给**单元测试**用的：测某条形状规则时不需要背景线，
    #:   关掉它比伪造一条线干净。生产必须保持 True。
    apply_guard: bool = True

    #: ★ 惊喜K线阈值（点）：单根 5m **顺向**幅度到了就立即止盈。
    #:
    #: 2026-11-17 用户要求，原话：
    #:   「任何一根 5m 的持仓顺向涨跌幅达到 500U，都要立即止盈，因为很难得！
    #:     我们不是长线，遇到这种惊喜K线必须走」
    #:
    #: ★ 为什么是「点」不是「U」：仓位是 50U 本金 + 按风险反推的 qty，
    #:   同一个百分比在不同仓位上折出来的 U 完全不同。用户说的 500U
    #:   在 1 BTC 的名义下约等于 500 点；用点数做判据才不随仓位漂移。
    #:   仓位换算在 `PricePlan` 那层做（它知道 qty），状态机只认点数。
    #: ★ 初值，没有回测支撑。B 类裁决：这是用户给的系统要求，不是调参。
    #:
    #: ★★ 2026-11-17：**字面量不在这里了**。它移到 `config.DEFAULT_SURPRISE_PTS`，
    #:   这里只引用。原因：用户要求这个阈值能在 Web 上改（500/700/1200 都常见），
    #:   而一旦库里的值、`TradingCfg` 的默认值、这里的默认值各写一个数，
    #:   就会出现“页面显示 1200、状态机用 500” 这种不报错的分叉。
    surprise_pts: float = DEFAULT_SURPRISE_PTS

    def level_key(self, direction: str) -> str:
        """三引线用的「那条线」按方向取（做空看压力，做多看支撑）。"""
        return self.support_key if direction == UP else self.resistance_key

    def one_tick_beyond(self, price: float, direction: str,
                        order_type: str) -> float:
        """把价位再往外推一个 tick。

        「一个 tick」的方向由 (方向, 类型) 决定，不是由方向单独决定：
            往下成交的单（LIMIT 买 / STOP 卖）→ 减一个 tick
            往上成交的单（LIMIT 卖 / STOP 买）→ 加一个 tick
        写反了不会报错，只会让挂单价落在现价错误的一侧。

        ★ 判据复用 `T.triggers_when_price_falls`，不在这里另写一份。
        """
        delta = -float(self.tick_size) \
            if T.triggers_when_price_falls(direction, order_type) \
            else float(self.tick_size)
        return round(float(price) + delta, 10)


# ---------------------------------------------------------------- 价位

@dataclass(frozen=True)
class PricePlan:
    order_price: float
    stop_price: float
    target_price: float
    stop_distance: float
    h1_amp: float
    amp5: float
    used_wide: bool
    #: ★ 成本（2026-11-17 新增）。单位都是**点**（价格差），不是 R。
    #:
    #: 分成「赢的那条路」和「亏的那条路」两笔，因为两者费种不同：
    #: 止盈是挂限价单（maker），止损是停损单（taker + 滑点）。
    cost_win_points: float = 0.0
    cost_loss_points: float = 0.0
    #: 折成 R（以止损距离为 1R）。`net_*` 已经扣掉成本。
    cost_win_r: float = 0.0
    cost_loss_r: float = 0.0
    net_win_r: float = 0.0          # 赢的净收益（R）
    net_loss_r: float = 0.0         # 亏的净损失（R，正数）
    #: 含成本的盈亏平衡胜率。★ 这个数才是「这个几何能不能活」的判据，
    #: 不是那个完全由 stop_frac / target_mult 决定的名义 R:R。
    breakeven_win_rate: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def gross_rr(self) -> float:
        """名义 R:R —— **不含成本**。别拿它当结论，它只是两个常数的比值。"""
        return (self.net_win_r + self.cost_win_r) / \
            (self.net_loss_r - self.cost_loss_r) \
            if (self.net_loss_r - self.cost_loss_r) else 0.0


def round_trip_costs(price: float, order_type: str,
                     params: Optional["PendingParams"] = None
                     ) -> Tuple[float, float]:
    """一笔往返的成本（**点数**）。返回 ``(cost_win, cost_loss)``。

    ★ 这是成本口径的**唯一来源**。`plan_prices()` 和
      `engine._pending_size()` 都必须调它 —— 不允许各自再算一份。

    为什么要单独拆出来：仓位必须把成本算进风险预算（否则“止损被打亏 R”
    这个承诺是假的），而定仓那一层拿不到 `PricePlan` 对象。
    把公式留在这里，两边都调，就不会漂移。

    口径（与历史一致）：
        赢：限价出场 → maker、不滑点；进场按 `order_type` 取费种
        亏：停损出场 → taker、有滑点
    """
    params = params or PendingParams()
    px = float(price)
    slip = float(params.slippage_ticks) * float(params.tick_size)
    is_stop = (order_type == T.ORDER_STOP)
    entry_rate = params.fee_taker if is_stop else params.fee_maker
    entry_slip = slip if is_stop else 0.0
    cost_win = px * (entry_rate + float(params.fee_maker)) + entry_slip
    cost_loss = (px * (entry_rate + float(params.fee_taker))
                 + entry_slip + slip)
    return cost_win, cost_loss


def amp_stop_distance(h1_amp: float, amp5: float,
                      params: "PendingParams", *, wide: bool = False) -> float:
    """形态自己算的止损距离 = max(1h振幅 × stop_frac, 5m振幅 × stop_floor_mult)。

    ★ 抽出来的唯一理由：`_build` 里要用 1R 去筛「够格当目标的关键线」，
      而 1R 就是这里的返回值（公式止损启用时再减成本，见 `_stop_distance`）。
      两处各写一份的话，改了系数只有一处生效。
    """
    frac = params.stop_frac_wide if wide else params.stop_frac
    return max(float(h1_amp) * float(frac),
               float(amp5) * float(params.stop_floor_mult))


def _stop_distance(order_price: float, amp_dist: float,
                   params: "PendingParams", *,
                   order_type: str = T.ORDER_LIMIT) -> float:
    """止损距离（= 1R）。**全仓库唯一一份**，被定价和「目标至少要 1R」共用。

    ★ 为什么要抽出来：`weak` 背景的止盈要收到关键线，而那条线离入场价
      可能只有 0.2R —— 那样的目标扣掉成本后是负期望。所以判定
      「哪些线够格当目标」必须先知道 1R 是多少。如果这个数在定价和
      过滤器里各写一份，改了一处另一处会静默不跟（本仓库的老毛病）。

    Args:
        amp_dist: 形态自己算的止损距离（振幅口径），公式止损不启用时用它。

    公式止损（用户口径，`max_stop_pct > 0` 时启用）：

        d_max 是「最坏亏 R」这件事的**总距离预算**，
        而总距离 = 止损距离 + 成本，所以止损必须往里让出成本：

            止损距离 = d_max − 成本占比

        否则「止损 + 成本」会超过 d_max，于是 `sizing.plan_position()`
        的爆仓安全校验会把**每一笔**都拒掉。这不是保守，
        是把风险预算算重了两次。
    """
    max_stop_pct = float(getattr(params, "max_stop_pct", 0.0) or 0.0)
    if max_stop_pct <= 0:
        return float(amp_dist)
    px = float(order_price)
    if px <= 0:
        return float(amp_dist)
    _cw, cost_loss_pts = round_trip_costs(px, order_type, params)
    cost_pct = cost_loss_pts / px * 100.0
    dist_pct = max_stop_pct - cost_pct
    if dist_pct <= 0:
        # d_max 连成本都盖不住 → 这笔根本不该做。
        # 不报错、不静默钳到 0，而是把 0 带回去让上层记账。
        return 0.0
    return px * dist_pct / 100.0


def plan_prices(order_price: float, direction: str,
                h1_amp: float, amp5: float,
                params: Optional[PendingParams] = None,
                *, wide: bool = False,
                order_type: str = T.ORDER_LIMIT,
                target_cap: Optional[float] = None) -> PricePlan:
    """算止损、止盈，**以及成本**。口径最确定的一块（用户逐条拍过）。

    止损宽度 = max(1h振幅 × stop_frac, 入场5m振幅 × stop_floor_mult)

    为什么要有那个下限：实测 1h 振幅中位约 320 点，×1/4 = 81 点；
    而入场那根 5m 的振幅中位 300+ 点。81 点的止损**落在入场 K 线体内**，
    而 5m 的 OHLC 没有 bar 内顺序 —— 这种止损「有没有被打」根本判不了，
    回测只能靠猜。这不是参数问题，是坐标系问题。

    止盈 = 1h 振幅 × target_mult（用户定的「翻一翻 = 一倍振幅」）。

    ★★ 2026-11-17 补成本（用户「第7条先只加成本模型看数」）：
      以前成本恒为 0，于是「R:R = 4」看上去很漂亮 —— 但那 4.00 是
      `target_mult / stop_frac = 1.0/0.25` 的**算术恒等式**，不是测出来的。
      真实约束是「手续费占止损距离多少」：实测中位 44.4%、最差 71.7%。

      分成赢/亏两条路分别算，因为费种不同：
        赢：止盈打出 → 挂限价单出场（maker，不滑点）
        亏：止损打出 → 停损单出场（taker + 滑点）
      进场费种看 `order_type`：STOP 追价 = taker（+滑点），LIMIT = maker。
    """
    params = params or PendingParams()
    if direction not in (DOWN, UP):
        raise ValueError("direction 只能是 %r 或 %r" % (DOWN, UP))
    if order_price <= 0 or h1_amp <= 0 or amp5 < 0:
        raise ValueError("价位/振幅不合法：order=%r h1_amp=%r amp5=%r"
                         % (order_price, h1_amp, amp5))

    frac = params.stop_frac_wide if wide else params.stop_frac
    from_h1 = float(h1_amp) * float(frac)
    floor = float(amp5) * float(params.stop_floor_mult)
    # 形态自己的止损距离 —— 现在**只进证据，不参与挂单**。
    amp_dist = amp_stop_distance(h1_amp, amp5, params, wide=wide)

    max_stop_pct = float(getattr(params, "max_stop_pct", 0.0) or 0.0)
    dist = _stop_distance(order_price, amp_dist, params,
                          order_type=order_type)

    # 止盈：4R。按**最远止损**的 4 倍 —— 与止损同一个基准，
    # 否则两条线各自算，R:R 又会变成算术恒等式。
    if max_stop_pct > 0:
        target_dist = float(order_price) * max_stop_pct / 100.0 * \
            float(params.target_r_multiple)
    else:
        target_dist = float(h1_amp) * float(params.target_mult)

    # ★★ 止盈上限（2026-11-20，用户口径）：
    #   「ema20 和 ema50 倾斜向下，说明预期不要太高……止盈目标不要太远」
    #   「三引线是 ema20 和 ema50 有同向趋势且相距越走越宽，这时反弹会相对
    #     强劲一些，像现在这种不是标准的三引线买法，反弹也会弱一些」
    #
    #   弱背景不拍一个新倍数，而是**把目标收到现实里存在的那条关键线**
    #   （`nearest_favorable_line` 的返回）。只收紧、不放宽 —— 传进来的
    #   cap 比 4R 还远时直接忽略，四舍五入的误差也不许把它变成放宽。
    target_cap_line = None
    if target_cap is not None:
        cap_dist = abs(float(target_cap) - float(order_price))
        if 0 < cap_dist < target_dist:
            target_dist = cap_dist
            target_cap_line = float(target_cap)

    if direction == DOWN:
        stop = float(order_price) + dist
        target = float(order_price) - target_dist
    else:
        stop = float(order_price) - dist
        target = float(order_price) + target_dist

    # ---- 成本 ----
    px = float(order_price)
    cost_win, cost_loss = round_trip_costs(px, order_type, params)

    denom = float(dist) if dist else 0.0
    cw_r = (cost_win / denom) if denom else 0.0
    cl_r = (cost_loss / denom) if denom else 0.0
    net_win = (target_dist / denom - cw_r) if denom else 0.0
    net_loss = 1.0 + cl_r
    be = (net_loss / (net_win + net_loss)) if (net_win + net_loss) else 0.0

    return PricePlan(
        order_price=float(order_price), stop_price=round(stop, 2),
        target_price=round(target, 2), stop_distance=round(dist, 2),
        h1_amp=float(h1_amp), amp5=float(amp5), used_wide=bool(wide),
        cost_win_points=round(cost_win, 2),
        cost_loss_points=round(cost_loss, 2),
        cost_win_r=round(cw_r, 4), cost_loss_r=round(cl_r, 4),
        net_win_r=round(net_win, 4), net_loss_r=round(net_loss, 4),
        breakeven_win_rate=round(be, 4),
        evidence={
            # ★ 这几个字符串由**实际参数**拼出来，不写死 —— 写死的话
            #   改了参数它就是假的，而它正是排查时第一个被读的东西。
            "rule": "stop = max(1h_amp × %.2f, amp5 × %.2f); "
                    "target = 1h_amp × %.2f"
                    % (params.stop_frac if not wide
                       else params.stop_frac_wide,
                       params.stop_floor_mult, params.target_mult),
            "stop_frac": frac, "target_mult": params.target_mult,
            "stop_floor_mult": params.stop_floor_mult,
            "from_h1": round(from_h1, 2), "from_floor": round(floor, 2),
            "floor_binds": bool(floor > from_h1),
            "cost_rule": "赢=maker出场; 亏=taker出场+滑点; "
                         "进场按 order_type 取费种",
            "order_type": order_type,
            # ★ 成本已拆到 `round_trip_costs()`（唯一来源），
            #   这里从它返回的数反推出来记账，不重新算。
            "entry_fee_rate": (params.fee_taker if order_type == T.ORDER_STOP
                               else params.fee_maker),
            "slip_points": round(float(params.slippage_ticks)
                                 * float(params.tick_size), 4),
            # ★ 形态自己算的止损（振幅规则）—— 现在只供参考，
            #   不参与挂单。保留它是为了将来 S5（现实跟随出场）
            #   需要知道“形态自己觉得失效在哪”。
            "amp_stop_distance": round(amp_dist, 2),
            "amp_stop_pct": round(amp_dist / float(order_price) * 100.0, 4)
            if order_price else 0.0,
            "max_stop_pct": max_stop_pct,
            "cost_loss_pct": round(float(cost_loss) / float(order_price) * 100.0, 4)
            if order_price else 0.0,
            "stop_rule": ("formula: d_max - cost" if max_stop_pct > 0
                          else "amplitude: max(1h_amp x frac, amp5 x floor)"),
            "gross_rr": round((target_dist / denom) if denom else 0.0, 4),
            "breakeven_win_rate": round(be, 4),
            "fee_share_of_stop": round(float(cost_loss) / denom, 4)
            if denom else 0.0,
            # ★ 止盈被关键线收紧时的留痕。`None` = 没收紧（照 4R）。
            #   「按什么价收的」必须能查，否则事后看不出这笔为什么只有 1.9R。
            "target_cap_line": (round(target_cap_line, 2)
                                if target_cap_line is not None else None),
            "target_source": ("nearest_favorable_line" if target_cap_line is not None
                              else "r_multiple"),
        })


# ---------------------------------------------------------------- 上下文

class _Bar:
    """内部统一 K 线形状：既吃 Bar(NamedTuple)，也吃 (ts,o,h,l,c) 元组。"""

    __slots__ = ("ts", "o", "h", "l", "c")

    def __init__(self, ts, o, h, l, c):
        self.ts = int(ts)
        self.o = float(o)
        self.h = float(h)
        self.l = float(l)
        self.c = float(c)

    def __repr__(self):      # pragma: no cover
        return "Bar(%d,%.2f,%.2f,%.2f,%.2f)" % (
            self.ts, self.o, self.h, self.l, self.c)


def _norm(b: Any) -> Any:
    """把任意 K 线表示归一成「有 ts/o/h/l/c」的对象。"""
    if isinstance(b, _Bar):
        return b
    if hasattr(b, "ts") and hasattr(b, "o"):
        return b
    return _Bar(b[0], b[1], b[2], b[3], b[4])


class OpenWindow:
    """**开仓窗口** —— 用户 2026-11-17 定的背景条件，逐字是：

    > 「就是向下顺向带量突破区间后，下一个区间形成过程中出的开仓预期」

    ★ 为什么这不是我原来想的那个「位置闸门」：
      闸门是「额外加的一道过滤」，而这是**形态的定义域** ——
      三条形态只在这段时间里存在，就像「回调买」只存在于上涨趋势里。
      把这个当成过滤器，容易继续在窗口外产生计划再丢掉；
      必须让探测器**只在窗口内被调用**，窗口外连数都不用数。

    字段：
        start_ms: 突破那根 1h 的 ts（含）
        end_ms:   紧随其后的 forming 段结束（不含）
        direction: **突破的方向** DOWN / UP —— 必须和形态的背景方向配对。
            ★ 实测（§23.5）：只按 tag 不按 direction，倒V 距 ema20 的中位
              会从 +0.124% 变成 +0.278%（更差），因为 `突破` 里混着涨破，
              那是镜像的摸顶。方向不配对，等于把两种相反的打法混成一个形态。
    """

    __slots__ = ("start_ms", "end_ms", "direction")

    def __init__(self, start_ms: int, end_ms: int, direction: str):
        self.start_ms = int(start_ms)
        self.end_ms = int(end_ms)
        self.direction = str(direction)

    def contains(self, ts: int) -> bool:
        return self.start_ms <= int(ts) < self.end_ms

    def __repr__(self):      # pragma: no cover
        return "OpenWindow(%d→%d,%s)" % (self.start_ms, self.end_ms,
                                         self.direction)


#: 「区间未形成 / 下一个区间正在形成」的 tag —— 直接来自 `range/engine.py`
#: 写库时的取值，**不在 pending_plan 里重新推导**（重新推导必然和引擎漂）。
TAG_BREAKOUT = "突破"
TAG_REVERSAL = "反转"
TAG_FORMING = "forming"

#: 突破方向 → 该方向的**顺向边界**键。用户原话「向下顺向带量突破」。
#: 顺向边界就是 `range/__init__.py` 里的 P（DOWN 是 lo_b / soft_dn）。
FORWARD_BOUNDARY = {DOWN: "range_soft_dn@1h", UP: "range_soft_up@1h"}

#: `range_evolution.direction` 用的是**大写**（引擎写库口径），
#: 而本模块的 `DOWN`/`UP` 是 `thesis` 里的**小写**常量。
#: ★ 这两套词汇必须在这一个地方对应起来，否则比较永远不相等，
#:   表现得像「窗口一个都不命中」—— 实测踩过：窗口 7 段全部错过，
#:   计划数静默变成 0，没有任何报错。这是本项目最贵的一类 bug。
_RANGE_DIR = {DOWN: "DOWN", UP: "UP"}

#: 开仓窗口在 forming 段结束后**再追加几根**（用户 2026-11-17 定：追加 3 根）。
#:
#: 用户原话：「不换周期，但是 forming 是 3 根，我想再追加 3 根」。
#:
#: ★ 这是**窗口的尾巴**，不是引擎的 `FORM_BARS`。
#:   引擎的 `FORM_BARS=3`（突破根 + forming 2 根 = 成形窗口 3 根）属于
#:   规范 v11 §2 的区间定义，改它必须重跑 `range_evolution` 并重建
#:   `range_*@1h` 线表 —— 本轮**没有动它**，只把窗口往后延。
#:
#: 为什么要延：三引线要连续 3 根 15m「扎破水位又收回」，而**突破那一根**的
#: 水位还是旧下界（`engine.py:359` 在突破时把 hard/soft 置空，线表沿用旧值），
#: 3 根窗口里只剩 2 根可用。延 3 根后窗口 = 6 小时，
#: 1h 周期下 15m 可用根数从 8 根涨到 20 根。
WINDOW_EXTRA_BARS = 3

#: 量能门槛：该小时量 / 前 `WINDOW_VOL_WIN` 小时量**均值** >= 这个倍数。
#:
#: ★ 2026-11-26 用户重新定口径（原话）：
#:   「量能门槛要跟最近相比大于 3 就行，不是固定，只要最近 K 线出现了
#:     大于最近均量的 3 倍或者 4 倍或者 5 都算，具体多少回测一下，
#:     不一定是突破的这一根，我们看的是附近的放量行为，
#:     附近放量行为出现了，那就是满足了」
#:
#: 三条变化，都是用户明确要的：
#:   1. **参考基准从「中位量」改成「均值」** —— 用户说的是"均量"。
#:   2. **门槛从 1.5 抬到 3** —— 1.5 倍在真实数据上属于噪声级，
#:      量比 0.53/0.83/0.91 的事件都被它挡在门外，等于策略常年不开张。
#:   3. **不再要求「突破那一根」自己放量** —— 看「附近的放量行为」，
#:      突破根前后各 `WINDOW_VOL_NEAR` 小时内出现过合格放量即可。
#:      这一条最贴近用户说的"扫流动性"：放量是清算/扫单的结果，
#:      它可能落在突破根前一根（先扫后破）或后一根（破了才扫）。
#:
#: 取 3.0 而不是 4/5：具体值由 `tools/calibrate_vol_gate.py` 在真实数据上
#: 夹出来（见该脚本的 `--sweep`），常数在这里是**唯一来源**。
WINDOW_VOL_MULT = 3.0

#: 量比参考窗口：本根之前多少根 1h（用户口径"最近"）。
WINDOW_VOL_WIN = 24

#: 「附近放量」的半径：突破根前后各几根 1h 内出现过合格放量，即算满足。
#: 0 = 只看突破根自己（等价于老行为，但门槛用的是新值）。
#:
#: ★ 取 2 的依据（`tools/calibrate_vol_gate2.py`，2026-08-01~11-26 真实数据）：
#:    近邻=1、N=3.0 → 窗口 21、受闸形态 8 笔、均 R +1.561
#:    近邻=2、N=3.0 → 窗口 26、受闸形态 9 笔、均 R +1.862（最好）
#:   近邻=1、N=2.0 → 窗口 30、受闸形态 11 笔、均 R +1.518（窗口最多但均 R 最低）
#:   近邻=2、N=2.0 → 窗口 37、受闸形态 12 笔、均 R +1.747
#:  用户原话「不一定是突破的这一根，我们看的是附近的放量行为」，
#:  近邻=2（前后各 2 小时）既保留了"附近"语义，均 R 也最高。
WINDOW_VOL_NEAR = 2

#: 哪些 tag 可以当窗口起点。用户 2026-11-26 追加「反转」：
#: 22:45 那根 +852 点的 V 型反转就是典型起点，老口径只认「突破」，
#: 于是整夜一个窗口都长不出来。
WINDOW_START_TAGS = (TAG_BREAKOUT, TAG_REVERSAL)

#: ★★ 生产**启用**哪些形态。默认是 `KINDS` 的一个子集：
#:   三引线假突破**停用**（`KIND_WICKS` 不在里面）。
#:
#: 依据（`tools/calibrate_wicks_gate.py`，2026-08-01~11-26，真实生产库，
#: 口径 N=3.0 / 近邻=2，每个计划都用完整状态机跑结算）：
#:
#:   V0 三条全开（现状）     n=767  胜率 15%  总 R **-373.69**  均 R -0.487
#:   V1 三引线也进窗口闸     n= 11  胜率 18%  总 R   **-6.44**  均 R -0.586
#:   V2 停用三引线           n= 16  胜率 62%  总 R  **+17.72**  均 R +1.107
#:
#: 三引线在两个口径下都是负的（窗口外 758 笔 -0.515R/笔、窗口内 11 笔
#: -0.586R/笔），样本量比其它两条大两个数量级 —— 也就是说这个结论
#: **不是小样本噪声**，而是它本身的期望为负。
#:
#: ★ 为什么单独做成一个常量而不是删掉探测器：
#:   探测器/状态机/测试全部保留（它们是已验收的资产），只是生产路径
#:   不启用。要试回来，改这一个元组即可，不需要动任何逻辑。
#:   这与「同一件事两个来源」纪律一致：**启用集合只有一个来源**。
ENABLED_KINDS = (KIND_INVERSE_V, KIND_EMA_REJECT)


def build_open_windows(h1, tag_of, line_of, *,
                       vol_mult: float = WINDOW_VOL_MULT,
                       vol_win: int = WINDOW_VOL_WIN,
                       vol_near: int = WINDOW_VOL_NEAR,
                       start_tags=WINDOW_START_TAGS,
                       directions=(DOWN, UP),
                       extra_bars: int = 0,
                       period_ms: int = 3_600_000):
    """把「顺向带量突破 → 下一个区间形成」翻译成一段时间窗口。

    Args:
        h1: 1h 序列，每项要有 ``ts/o/h/l/c/v``（按 ts 升序）。
        tag_of: ``ts -> (tag, direction)``，取自 `range_evolution`；
            **direction 是大写**（`'DOWN'`/`'UP'`），本函数内部转好。
        line_of: ``(ts, line_key) -> price``，只在该 1h **收盘时已就绪**才返回，
            否则 ``None``。用 `LineBook.at(key, ts + HOUR)` 就行。
        vol_mult: 放量门槛（该小时量 / 前 `vol_win` 小时量**均值**）。
        vol_win: 量比参考窗口（小时数）。
        vol_near: 「附近放量」半径（小时）。突破根前后各这么多根内出现过
            合格放量即算满足；0 = 只看突破根自己。
        start_tags: 哪些 tag 可以当窗口起点（默认突破 + 反转）。
        directions: 要算哪些方向，用本模块的 `DOWN`/`UP`；默认两个都算
            （用户要求镜像一起做）。
        extra_bars: forming 段结束后**再追加几根**（默认 0 = 老行为）。
            生产用 `WINDOW_EXTRA_BARS`（= 3）。
        period_ms: 区间演化周期（默认 1h）；追加长度按它换算。

    Returns:
        ``List[OpenWindow]``，按 start_ms 升序。`OpenWindow.direction` 用
        本模块的 `DOWN`/`UP`（小写），和 `detect_all` 传进来的背景方向同源。

    ★ 三个刻意的取舍：
      1. **带量是硬条件**。用户原话有「带量」二字，不是修饰。
         不带量的顺向突破不开窗口 —— 那多半是噪声穿刺。
      2. **窗口要求后面真的跟着 forming 段**。突破了但没开始长新区间，
         说明还在原区间里晃，「下一个区间形成过程」尚未开始，不算。
      3. **追加的尾巴不许越过下一个「突破/反转」**。越过了相邻两段窗口就会
         重叠，而 `PlanContext.in_window` 只取第一个命中的窗口 —— 重叠时
         另一段会被**静默**吞掉，两个方向的窗口还可能同时盖住同一根 K。
         宁可截短，也不许重叠。
    """
    if int(extra_bars) < 0:
        raise ValueError("extra_bars 不能为负（拿到 %r）" % (extra_bars,))
    if int(period_ms) <= 0:
        raise ValueError("period_ms 必须 > 0（拿到 %r）" % (period_ms,))
    hours = [b for b in (h1 or ()) if b is not None]
    hours = sorted(hours, key=lambda b: int(b.ts))
    if not hours:
        return []
    out = []
    # ★ 量能序列**预先取全**：附近放量要看突破根**后面**的根，
    #   而下面 `vols.append` 是随循环长的，到 i 为止只有 i+1 个 ——
    #   拿它做 `i+1` 会 IndexError，做 `max(...)` 会静默少一根。
    all_vols = [float(getattr(b, "v", 0.0) or 0.0) for b in hours]
    for direction in directions:
        key = FORWARD_BOUNDARY[direction]
        rng_dir = _RANGE_DIR[direction]       # 大写，和库里一致
        down = direction == DOWN
        start_set = tuple(start_tags or ())
        for i, bar in enumerate(hours):
            ts = int(bar.ts)
            st = tag_of(ts)
            tag, tdir = (st if st else ("", ""))
            if tag not in start_set or tdir != rng_dir:
                continue
            if i == 0:
                continue
            px = line_of(ts, key)
            if px is None:
                continue
            prev_close = float(getattr(hours[i - 1], "c"))
            close = float(getattr(bar, "c"))
            crossed = (prev_close >= float(px) > close) if down else \
                      (prev_close <= float(px) < close)
            if not crossed:
                continue
            # ★ 量能：「附近的放量行为」—— 突破根前后各 vol_near 小时内的
            #   **最大**量，对比之前 vol_win 小时的**均值**。
            #   用户 2026-11-26 口径：不要求突破根自己放量（扫流动性可能
            #   落在突破前一根：先扫后破），附近出现过合格放量即算满足。
            ref_slice = all_vols[max(0, i - vol_win):i]
            ref = _mean(ref_slice)
            if ref <= 0:
                continue
            near_lo = max(0, i - int(vol_near))
            near_hi = min(len(all_vols), i + int(vol_near) + 1)
            peak = max(all_vols[near_lo:near_hi]) if near_hi > near_lo else 0.0
            if peak / ref < float(vol_mult):
                continue
            # 往后找紧跟的 forming 段（同方向），再按 extra_bars 追加尾巴
            end = None
            extra_left = int(extra_bars)
            for nxt in hours[i + 1:]:
                nts = int(nxt.ts)
                st2 = tag_of(nts)
                t2, d2 = (st2 if st2 else ("", ""))
                if t2 in (TAG_BREAKOUT, TAG_REVERSAL):
                    # ★ 下一个「突破/反转」开始 → 窗口必须停在这里。
                    #   不许越过：越过就会和下一段窗口重叠，而 in_window
                    #   只取第一个命中，另一段会被静默吞掉。
                    break
                if t2 == TAG_FORMING and d2 == rng_dir:
                    end = nts + int(period_ms)
                    extra_left = int(extra_bars)   # 还在 forming，额度重置
                elif end is not None:
                    if extra_left > 0:
                        end = nts + int(period_ms)
                        extra_left -= 1
                    else:
                        break
            if end is not None:
                out.append(OpenWindow(ts, end, direction))
    out.sort(key=lambda w: w.start_ms)
    return out


def _median(xs) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _mean(xs) -> float:
    """算术平均。用户 2026-11-26 把量能基准从「中位数」改成「均量」。"""
    vals = [float(x) for x in (xs or ()) if x is not None]
    if not vals:
        return 0.0
    return sum(vals) / float(len(vals))


class PlanContext:
    """给探测器用的只读上下文：15m/5m 序列 + 线的查表 + 1h/5m 振幅。"""

    H1_MS = 3_600_000
    M5_MS = 300_000

    #: 5m 数据允许的最大陈旧度。超过就认为「这根 5m 不存在」，
    #: 于是算不出止损下限，于是**不出计划** —— 而不是拿一根几天前的 5m 顶数。
    #: 真实数据上踩到过：kbar.db 的 5m 只到 11-01，15m 到 11-08，
    #: 于是 11-01 之后的每条计划都在用同一根 5m 的振幅 60.30 当止损下限。
    MAX_STALE_5M_MS: int = 3 * 300_000
    #: 1h 允许的最大陈旧度（1h 由 15m 聚合，正常情况下不会缺）
    MAX_STALE_1H_MS: int = 3 * 3_600_000

    def __init__(self, bars15: Sequence[Any], book: T.LineBook,
                 bars_1h: Sequence[Any] = (), bars_5m: Sequence[Any] = (),
                 windows: Optional[Sequence["OpenWindow"]] = None):
        self.bars15 = [_norm(b) for b in (bars15 or ())]
        self.book = book
        self.set_bars_5m(bars_5m)
        self.set_bars_1h(bars_1h)
        self._b15_ts = [b.ts for b in self.bars15]
        # ★ None 与 [] 在这里**语义不同**，不能合并：
        #   None = 调用方没提供窗口（只验形状的单元测试用，不做窗口过滤）；
        #   []   = 提供了但一段窗口都没有（真实的「这段时间没有开仓机会」），
        #          此时一条计划都不该产出。
        #   合并成同一个默认值，会让「窗口算成了空」被读成「不需要窗口」，
        #   于是又回到在任意位置出计划的状态 —— 那正是本轮要修的 bug。
        self.windows = None if windows is None else list(windows)

    # ---- 开仓窗口（用户口径：顺向带量突破 → 下一个区间形成过程）

    def window_at(self, ts: int) -> Optional["OpenWindow"]:
        """``ts`` 落在哪个开仓窗口里；不在任何窗口里返回 None。"""
        if self.windows is None:
            return None
        for w in self.windows:
            if int(w.start_ms) <= int(ts) < int(w.end_ms):
                return w
        return None

    def in_window(self, ts: int, want_direction: Optional[str] = None) -> bool:
        """``ts`` 是否在开仓窗口内，且窗口背景方向匹配。

        ``want_direction=None`` 表示不挑方向。
        ★ 没提供窗口（``windows is None``）时恒为 True —— 表示「不启用窗口
          过滤」，只用于单元测试；生产必须给窗口且 `require_window=True`。
        """
        if self.windows is None:
            return True
        w = self.window_at(ts)
        if w is None:
            return False
        if want_direction is None:
            return True
        return w.direction == want_direction

    def set_bars_1h(self, bars_1h: Sequence[Any]) -> None:
        """同时更新序列与时间戳索引 —— 这两个必须同步，分开改会静默失效。"""
        self._h1 = sorted((_norm(b) for b in (bars_1h or ())), key=lambda b: b.ts)
        self._h1_ts = [b.ts for b in self._h1]

    def set_bars_5m(self, bars_5m: Sequence[Any]) -> None:
        self.bars5 = [_norm(b) for b in (bars_5m or ())]
        self._m5_ts = [b.ts for b in self.bars5]

    # ---- 1h

    def _h1_index(self, close_ms: int, max_stale: int) -> int:
        """截至 close_ms 已收盘、且**不算太旧**的最后一根 1h 的下标，-1 = 没有。"""
        last = bisect.bisect_right(self._h1_ts, int(close_ms) - self.H1_MS) - 1
        if last < 0:
            return -1
        if int(close_ms) - (self._h1_ts[last] + self.H1_MS) > max_stale:
            return -1
        return last

    def h1_at(self, close_ms: int):
        i = self._h1_index(close_ms, self.MAX_STALE_1H_MS)
        return self._h1[i] if i >= 0 else None

    def h1_prev2_amp(self, close_ms: int) -> Optional[float]:
        """前两根已收盘 1h 里 h−l **较大**的那根。这是止损的参照振幅。"""
        i = self._h1_index(close_ms, self.MAX_STALE_1H_MS)
        if i < 0:
            return None
        amps = []
        for k in (i, i - 1):
            if k < 0:
                continue
            b = self._h1[k]
            amps.append(float(b.h) - float(b.l))
        return max(amps) if amps else None

    # ---- 序列选择（★ BAR_SPACE 是唯一来源）

    def space_for(self, kind: str) -> str:
        """该形态扫哪条序列的**名字**（``'5m'`` / ``'15m'``）。

        ★ 为什么要有这个方法：以前「扫哪条序列」有两套实现 ——
          `detect_all` 按 `BAR_SPACE` 选，探测器里却硬编码 `ctx.bars15`
          / `ctx.bars5`。把三引线改到 5m 时立刻 `IndexError`：
          `detect_all` 用 5m 的下标，探测器却去取 15m 的列表。
          实测踩到（见 HANDOFF §23.21）。现在两边都走这里，
          改周期只改 `BAR_SPACE` 一处。
        """
        space = BAR_SPACE.get(kind)
        if space not in ("5m", "15m"):
            raise ValueError(
                "BAR_SPACE[%r] = %r 不认识（只支持 '5m'/'15m'）。"
                "新增周期必须同时接好 PlanContext 的序列，"
                "否则「扫哪条序列」又会变成两套。" % (kind, space))
        return space

    def series_for(self, kind: str) -> Sequence[Any]:
        """该形态扫描的序列（与 `space_for` 同源）。"""
        return self.bars5 if self.space_for(kind) == "5m" else self.bars15

    def space_ms_for(self, kind: str) -> int:
        """该形态一根 K 的毫秒数 —— 判开仓窗口要用**收盘**时刻。"""
        return self.M5_MS if self.space_for(kind) == "5m" else T.BAR_INTERVAL_MS

    # ---- 5m

    def m5_at(self, close_ms: int):
        last = bisect.bisect_right(self._m5_ts, int(close_ms) - self.M5_MS) - 1
        if last < 0:
            return None
        if int(close_ms) - (self._m5_ts[last] + self.M5_MS) > self.MAX_STALE_5M_MS:
            return None         # ★ 陈旧数据当没有，绝不能拿几天前的 5m 顶数
        return self.bars5[last]

    def amp5_at(self, close_ms: int) -> Optional[float]:
        """★ 计划时刻最近一根**新鲜**的 5m 振幅；过期就返回 None（→ 不出计划）。

        实盘应在**成交那一刻**用真实入场那根的振幅重算 —— 计划时入场根
        还不存在，这是近似，不是精确值。
        """
        b = self.m5_at(close_ms)
        return (float(b.h) - float(b.l)) if b else None

    # ---- 15m

    def b15_index_of(self, ts: int) -> int:
        """15m 序列里 ts 精确等于给定值的下标，没有则 -1。"""
        i = bisect.bisect_left(self._b15_ts, int(ts))
        if 0 <= i < len(self._b15_ts) and self._b15_ts[i] == int(ts):
            return i
        return -1

    def plan_bar(self, plan: "Plan") -> Optional[Any]:
        """取计划**所在的那根 K**，按 ``plan.bar_space`` 选序列，绝不猜。

        ★ 为什么要有：消费方以前写 ``ctx.bars15[plan.bar_index]``。
          三引线切到 5m 之后，`bar_index` 是 **5m** 的下标，
          拿去索引 15m 列表 → `IndexError`（冒烟脚本当场炸）。
          这类错在「5m 下标恰好小于 15m 长度」时**不再报错**，
          而是静默取到完全不相干的一根 K —— 比报错危险得多。
          所以取 K 一律走这里，不许在消费方各写一遍。
        """
        space = str(getattr(plan, "bar_space", "15m") or "15m")
        series = self.bars5 if space == "5m" else self.bars15
        if not series:
            return None
        ts = [int(b.ts) for b in series]
        target = int(plan.bar_ts)
        i = bisect.bisect_left(ts, target)
        if 0 <= i < len(series) and int(series[i].ts) == target:
            return series[i]
        return None


# ---------------------------------------------------------------- 形态

@dataclass(frozen=True)
class Plan:
    """一次预挂单计划：形态成立的位置 + 三个价位 + 类型 + 证据。"""

    kind: str
    direction: str
    order_type: str
    bar_index: int          # 在 bar_space 那条序列里的下标
    bar_ts: int
    order_price: float
    stop_price: float
    target_price: float
    #: ``bar_index`` / ``bar_ts`` 属于哪条序列（``'15m'`` / ``'5m'``）。
    #:
    #: ★ 以前这个时钟是**隐含**的（一律当 15m）。把三引线改到 5m 时，
    #:   `bar_index` 变成 5m 的下标，而 `run_plan` 的 `start_at` 还按 15m
    #:   解释 → 从错误的位置开始扫；`claim_for` 也会拿 5m 的 ts 去比 15m 的 K
    #:   → **永远不挂单**，而且不报错。记成显式字段，两处都按它走。
    bar_space: str = "15m"
    evidence: Dict[str, Any] = field(default_factory=dict)
    # ---- 声明时刻（双节奏）----
    # claim_ts 是**决策那一根 K 的 ts**；claim_on_entry=True 表示这一根在
    # **5m 序列**上（倒V 就是这种），False 表示在 15m 序列上（三引线 / EMA 拒绝）。
    # claim_ts=0 表示「就用 bar_ts」，兼容只关心 15m 的老调用点。
    claim_ts: int = 0
    claim_on_entry: bool = False
    #: ★ 策略家族（``reversal`` / ``trend``）—— 失效判据的轴。
    #:
    #: 以前**计划上不记家族**，只能由消费方按 direction 猜，
    #: 结果就是把「反转」和「做多」当成一回事（§23.23，4/6 条反转空单白死）。
    #: 默认 ``trend``：没传就照判，保守方向，不许静默豁免。
    family: str = T.FAMILY_TREND

    @property
    def effective_claim_ts(self) -> int:
        return int(self.claim_ts or self.bar_ts)

    def validate(self, ref_price: Optional[float] = None) -> None:
        """计划自洽性检查。**价位放反了不会报错，只会静默变成另一种单。**

        ★ 判据按 (方向, 类型) 组合，不能只看方向：
            往下成交（LIMIT 买 / STOP 卖）→ 挂单价必须在现价**下方**
            往上成交（LIMIT 卖 / STOP 买）→ 挂单价必须在现价**上方**
          只按方向判会把 STOP 全判反。
        """
        if self.direction not in (DOWN, UP):
            raise ValueError("direction 只能是 %r 或 %r" % (DOWN, UP))
        if self.order_type not in T.ORDER_TYPES:
            raise ValueError("order_type 只能是 %r 或 %r，收到 %r"
                             % (T.ORDER_LIMIT, T.ORDER_STOP, self.order_type))
        if self.order_price <= 0:
            raise ValueError("挂单价必须为正")

        if self.direction == DOWN:
            if not (self.stop_price > self.order_price > self.target_price):
                raise ValueError(
                    "做空必须 止损 > 挂单 > 止盈，收到 %.2f / %.2f / %.2f"
                    % (self.stop_price, self.order_price, self.target_price))
        else:
            if not (self.stop_price < self.order_price < self.target_price):
                raise ValueError(
                    "做多必须 止损 < 挂单 < 止盈，收到 %.2f / %.2f / %.2f"
                    % (self.stop_price, self.order_price, self.target_price))

        if ref_price is not None:
            falls = self.triggers_when_price_falls
            if falls and not (self.order_price < ref_price):
                raise ValueError(
                    "%s %s 是「往下成交」，挂单价 %.2f 却不在现价 %.2f 下方 → "
                    "方向或价位放反了"
                    % (self.direction, self.order_type,
                       self.order_price, ref_price))
            if (not falls) and not (self.order_price > ref_price):
                raise ValueError(
                    "%s %s 是「往上成交」，挂单价 %.2f 却不在现价 %.2f 上方 → "
                    "方向或价位放反了"
                    % (self.direction, self.order_type,
                       self.order_price, ref_price))

    @property
    def triggers_when_price_falls(self) -> bool:
        """与 `Thesis.triggers_when_price_falls` 共用**同一份**实现。

        ★ 不要在这里再写一遍判据：两边各写一份时，改了一边忘了另一边
          不会有任何测试报警，只有真实成交序列会不对。
        """
        return T.triggers_when_price_falls(self.direction, self.order_type)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "direction": self.direction,
                "order_type": self.order_type,
                "bar_index": self.bar_index, "bar_ts": self.bar_ts,
                "order_price": self.order_price, "stop_price": self.stop_price,
                "target_price": self.target_price,
                "claim_ts": self.effective_claim_ts,
                "claim_on_entry": bool(self.claim_on_entry),
                "evidence": dict(self.evidence)}


def _wick_pct(bar: Any, side: str) -> Tuple[float, float]:
    """返回 (指定那一侧的影线占整根振幅的比例, 整根振幅)。

    ★ 影线在哪一侧是**形态的属性**，不是交易方向的属性。这一点很容易搞错：
        三引线（做多）看**下**影线 —— 下方假突破
        倒 V（做多）看**上**影线 —— 冲高被打回
      同样是做多，一个看下影、一个看上影。所以这里必须传 side，
      不能靠 direction 推。第一版用 direction 推，倒 V 的形态识别直接失效。
    """
    if side not in ("upper", "lower"):
        raise ValueError("side 只能是 'upper' 或 'lower'，收到 %r" % (side,))
    o, h, l, c = bar.o, bar.h, bar.l, bar.c
    rng = h - l
    if rng <= 0:
        return 0.0, 0.0
    wick = (h - max(o, c)) if side == "upper" else (min(o, c) - l)
    return wick / rng, rng


#: 每条形态在**基准方向**（PATTERN_SPEC 里的方向）下看哪一侧影线。
WICK_SIDE: Dict[str, str] = {
    KIND_WICKS: "lower",        # 下方假突破
    KIND_INVERSE_V: "upper",    # 冲高被打回
    KIND_EMA_REJECT: "upper",   # 弹到 ema 被打回
}


def _side_for(kind: str, direction: str) -> str:
    """基准方向 → 实际方向：方向取反时，影线侧也要跟着取反。"""
    base = WICK_SIDE[kind]
    flipped = direction != PATTERN_SPEC[kind]["direction"]
    if not flipped:
        return base
    return "upper" if base == "lower" else "lower"


def _pierced_and_closed(bar: Any, level: float, direction: str) -> bool:
    """刺到/穿过 level，但收盘回到 level 的另一侧（假突破的定义）。"""
    if direction == DOWN:
        return bar.h >= level and bar.c < level
    return bar.l <= level and bar.c > level


def background_of(kind: str, direction: str) -> str:
    """这条（形态, 方向）组合应该出现在**哪种背景行情**里。

    ★ 这是「背景方向」的**唯一来源**（2026-11-17 抽出来）。
      在这之前它在三个地方各写了一遍：
        · `detect_all` 里算窗口配对
        · `_build` 旁边（注释里）
        · `guard_preconditions` 里按 `_variant_name` 反查

      三份实现里任何一份漂了，表现都是「某类计划静默消失」而不是报错 ——
      和 §23.23 那次（`direction` 大小写两套词汇）同一类失败。
      现在三处都走这里。
    """
    base = PATTERN_SPEC[kind]["direction"]
    return DOWN if direction == base else UP


def _space_ms(space: str) -> int:
    """周期名 → 毫秒。不认识就抛错，不许静默当 15m。"""
    if space == "5m":
        return 300_000
    if space == "15m":
        return T.BAR_INTERVAL_MS
    raise ValueError("不认识的周期 %r（只支持 '5m'/'15m'）" % (space,))


# ---------------------------------------------------------------- 形态 1

def detect_wicks_false_break(i: int, ctx: PlanContext, params: PendingParams,
                             direction: str = UP
                             ) -> Tuple[Optional[Plan], str]:
    """形态 1：三引线假突破（**2026-11-20 用户口径重写**）。

    结构（以做多 / 下跌背景为例，做空镜像）：

        参考引线 A ── 这一段的最低点，且有下影
          │  └─ 中间 0~`wick_tolerance_bars` 根 **A 的 inside bar**（区间落在 A 之内）
        确认引线 B ── 有下影，极值回到 A 附近（同一个流动性区）

    在 **B 的收盘**出计划，挂 BUY LIMIT @ `min(A.low, B.low) + entry_tolerance_pts`。
    之后由 thesis 状态机推进：下一根起判成交，超过容忍窗口仍未成交则撤单。

    用户原话（2026-11-20，11-20 09:50~10:10 那组真实 K 线）：

        「09:55 和 10:00 两根 5mK 是 09:50 的 inside bar，10:05 的下引线
          出现之后，我们就会在 09:50 和 10:05 的低点 +1tick 做买单」
        「买点这里也应该优化，给与一定的容忍度，比如 +-10U」
        「先找第一根引线，找到了开始记录……发现第2/3都没出，且是记录的第
          一根的 inside bar……然后第4根出现了，第4根确实下引线，条件满足
          开始挂单，第5根入场了！我的意思是容忍 2 根」

    ★★ 为什么不再用「影线 / 整根振幅 ≥ 0.35」这种判据：
       那个比值**随振幅缩小**。11-20 那组实测：09:50 长下影占比 0.335、
       10:05 下影 0.199、中间 inside bar 只有 0.24~0.25 —— 三根全部
       `wick_too_short`，形态被自己的量具排除了（生产 107 天 0 单的直接原因之一）。
       而 inside bar 天生影线短，用「每根都要长影」筛选等于要求形态里
       不许出现 inside bar —— 与定义矛盾。现在换成纯位置关系，没有可调百分比。

    ★ 为什么定价不含 inside bar：中间几根的低点必然高于 A 的低点
      （inside bar 的定义），包含进来不改变 min，只会让证据变乱。
    """
    space = ctx.space_for(KIND_WICKS)            # 唯一来源，不许硬编码
    series = ctx.series_for(KIND_WICKS)
    space_ms = ctx.space_ms_for(KIND_WICKS)
    if i >= len(series):
        return None, "no_bar"
    bar = series[i]                              # 确认引线 B
    close_ms = bar.ts + space_ms

    side = _side_for(KIND_WICKS, direction)      # 做多看下影，做空看上影
    tol_bars = max(0, int(params.wick_tolerance_bars))
    if i - 1 - tol_bars < 0:
        return None, "not_enough_bars"

    # ---- B 自己必须有引线（做多：下影；做空：上影）
    wick_b, rng_b = _wick_pct(bar, side)
    if rng_b <= 0:
        return None, "zero_range_bar"
    if wick_b <= 0:
        return None, "no_wick"

    # ---- 往前找参考引线 A：先近后远，命中第一根就算
    hit_a = None                                 # (j, inside根数)
    why = "no_reference_wick"
    for k in range(0, tol_bars + 1):             # k = 中间夹几根 inside bar
        j = i - 1 - k
        if j < 0:
            why = "not_enough_bars"
            continue
        a = series[j]
        wick_a, rng_a = _wick_pct(a, side)
        if rng_a <= 0:
            why = "zero_range_bar"
            continue
        if wick_a <= 0:
            why = "ref_no_wick"
            continue
        mids = series[j + 1:i]
        # 中间每一根都必须是 A 的 inside bar（高低点都落在 A 之内）
        if any(not (a.l <= m.l and m.h <= a.h) for m in mids):
            why = "not_inside_bar"
            continue
        # ★ A 必须是**扫出来的**新低（做空：新高）—— 这是「假突破 / 扫流动性」
        #   的定义本身，不是额外过滤。
        #
        #   ★ 2026-11-20 补：没有这条时，两根连续的平淡 K（低点相同、都有
        #     一点点下影）会互相配成三引线 —— 实测 40 根横盘 K 出 36 条。
        #     这种「形态」在真行情里等于噪音，且它会自我复制成连续挂单。
        #   判据只用位置关系：A 的低点要严格低于它前面那 `tol_bars` 根。
        _prev = series[max(0, j - tol_bars):j]
        if not _prev:
            # 序列开头没有可比的前高/前低 —— 判不出「扫」，不许默认放行。
            # （★ 第一版写成 `if _prev:` 才检查，于是序列最前面的两根
            #   被无条件当成合法形态，夹具里就多出 4 条假计划。）
            why = "not_enough_bars"
            continue
        if direction == UP:
            if not (float(a.l) < min(float(x.l) for x in _prev)):
                why = "ref_not_a_sweep"
                continue
        else:
            if not (float(a.h) > max(float(x.h) for x in _prev)):
                why = "ref_not_a_sweep"
                continue
        # A 必须是这一段的最低点：中间不能有比 A 更低的低点
        if direction == UP:
            if any(m.l < a.l for m in mids):
                why = "ref_not_lowest"
                continue
        else:
            if any(m.h > a.h for m in mids):
                why = "ref_not_highest"
                continue
        # ★ B 必须**回到 A 极值附近的同一个流动性口袋** —— 这是用户
        #   「第 2/3 根**没出**引线，第 4 根才出」那句话的判据落点。
        #
        #   ★★ 2026-11-20：这一条我一共写错过两次，两次都记在这里。
        #
        #   ① 第一版用 `|B.low − A.low| ≤ wick_proximity_pts(10)`，
        #      11-20 那组（差 16.6）被判 ref_too_far。当时我以为是容差给小了，
        #      把判据换成「B 只要落进 A 的整个影线区（[A.low, 实体下沿]）」——
        #   ② 结果更糟：A 的影线区有 218 点宽，于是 **A 后面每一根擦到影线区
        #      的 K 都成了确认根**（09:55 低点 61402.5 也算），一条形态
        #      连出好几张单，而且第一张出在 09:55 —— 比用户说的 10:05 早两根。
        #
        #   正确的口径是两者都取：
        #     · 下影必须**守住** A 的低点（B.low > A.low）—— 一旦又破新低，
        #       A 就不是这段的极值了，参考根该换人；
        #     · 距离要落在同一个口袋里（A.low < B.low ≤ A.low + 区间宽度）。
        #       `wick_proximity_pts` 默认 20 = 两根影线根部相距 20 点以内，
        #       **是未验证初值**，标定依据是 11-20 实测 16.6（该出的那根）
        #       与 147 / 405（不该出的那两根）之间的空档；不许为凑信号再放大。
        zone = float(params.wick_proximity_pts)
        if direction == UP:
            if not (float(a.l) < float(bar.l) <= float(a.l) + zone):
                why = "ref_too_far"
                continue
        else:
            if not (float(a.h) - zone <= float(bar.h) < float(a.h)):
                why = "ref_too_far"
                continue
        hit_a = (j, k)
        break
    if hit_a is None:
        return None, why
    j, inside_n = hit_a
    a = series[j]

    # ---- 定价：两根引线的极值 + 容差（朝成交方向让）
    order_type = PATTERN_SPEC[KIND_WICKS]["order_type"]
    tol = float(params.entry_tolerance_pts)
    if direction == UP:
        base = min(float(a.l), float(bar.l))
        order_price = base + tol
    else:
        base = max(float(a.h), float(bar.h))
        order_price = base - tol
    order_price = round(order_price, 2)

    # ---- 位置背景：只记录，不做前置条件
    #
    #   用户从没说过三引线必须「收盘收回到某条线内侧」；老实现把它写成硬条件，
    #   于是 09:50 那根（收在线下方 61474.1 < 61512.5）直接被判 no_false_break。
    #   这里改成**证据**：记下声明时刻最近的那条关键线，出场侧（观察线拒绝）
    #   和事后归因都用得上。
    key = params.level_key(direction)
    line = ctx.book.at(key, close_ms)
    level = float(line.price) if line is not None else None

    on_entry = (space == "5m")
    plan, why = _build(kind=KIND_WICKS, direction=direction,
                       order_type=order_type, i=i, ctx=ctx, params=params,
                       order_price=order_price, bar=bar, bar_space=space,
                       claim_ts=int(bar.ts) if on_entry else 0,
                       claim_on_entry=on_entry,
                       evidence={"ref_index": j, "ref_ts": int(a.ts),
                                 "inside_bars": inside_n,
                                 "ref_wick": round(wick_b, 4),
                                 "confirm_wick": round(wick_b, 4),
                                 "gap_pts": round(
                                     abs(float(bar.l) - float(a.l)) if direction == UP
                                     else abs(float(bar.h) - float(a.h)), 2),
                                 "base": round(base, 2),
                                 "entry_tolerance_pts": tol,
                                 "level_key": key, "level": level,
                                 "ref_price": bar.c})
    return plan, why


# ---------------------------------------------------------------- 形态 2

def detect_inverse_v(i5: int, ctx: PlanContext, params: PendingParams,
                     direction: str = UP) -> Tuple[Optional[Plan], str]:
    """形态 2：倒 V 反包（在 **5m** 序列上判）。

    倒 V 根 = 冲高立马被打回（长上影、收在低处）。下一根再冲并且
    **收盘反包**它 → 在 `max(倒V高, 当前高)` 上方一个 tick 挂 STOP。

    ★ 为什么在 5m 上判：用户说「倒V是发生在 5m k 线内部」。
    ★ 返回的 Plan.bar_index / bar_ts 却是**15m** 的 —— 状态机统一在 15m
      收盘上推进（失效判据定义在 15m 收盘），所以把 5m 信号映射到
      「信号确认之后的第一根 15m」。这样不会在 15m 内偷用未收盘信息。
      ★ 这个「15m 时钟」现在记在 `Plan.bar_space` 里（显式），
        以前是隐含的 —— 见 `Plan.bar_space` 的注释。
    """
    series = ctx.series_for(KIND_INVERSE_V)       # BAR_SPACE 是唯一来源
    space_ms = ctx.space_ms_for(KIND_INVERSE_V)
    if i5 < 1 or i5 >= len(series):
        return None, "not_enough_bars"
    v, cur = series[i5 - 1], series[i5]

    # 倒 V 根：长上影、收在实体下半区（冲高被压回的近似）
    pct, rng = _wick_pct(v, _side_for(KIND_INVERSE_V, direction))
    if rng <= 0:
        return None, "zero_range_bar"
    if pct < params.v_min_wick_pct:
        return None, "v_wick_too_short"

    lb = int(params.v_lookback)
    start = max(0, i5 - 1 - lb)
    if direction == UP:
        # 倒 V 根本身要是最近几根里的最高点（是「顶」而不是普通波动）
        if v.h < max(series[k].h for k in range(start, i5)):
            return None, "v_not_the_top"
        if not (cur.c > cur.o):
            return None, "bar_not_up"
        if not (cur.c >= v.o):
            return None, "not_engulfing"
        base = max(v.h, cur.h)
    else:
        if v.l > min(series[k].l for k in range(start, i5)):
            return None, "v_not_the_bottom"
        if not (cur.c < cur.o):
            return None, "bar_not_down"
        if not (cur.c <= v.o):
            return None, "not_engulfing"
        base = min(v.l, cur.l)

    order_type = PATTERN_SPEC[KIND_INVERSE_V]["order_type"]
    order_price = params.one_tick_beyond(base, direction, order_type)

    # ★ 5m 信号 → **信号那根 5m 自己**就是声明时刻（双节奏）。
    #
    #   旧实现把声明推到「信号确认之后的第一根 15m」，等于硬等 10~25 分钟。
    #   真实数据上 343/1053 条倒V 就这样被判成 already_triggered：
    #   等到那根 15m 收盘，价格早已涨过触发价，再挂 BUY STOP 就是市价追单。
    #
    #   为什么现在可以按 5m 声明：thesis.run(entry_bars=..., claim_on_entry=True)
    #   会在这一桶的每根 5m 上问一次 claim，命中就在**那根 5m 的收盘**挂单。
    #   并且 already_triggered 从此**结构上不可能**出现：
    #       触发价 = max(倒V高, 当前高) + 一个 tick > 当前高 >= 当前收盘
    #   也就是 order_price 必然 > 决策时刻的现价，不可能「已经涨破」。
    i15 = max(0, bisect.bisect_right(ctx._b15_ts, int(cur.ts)) - 1)
    if i15 >= len(ctx.bars15):
        return None, "no_15m_bar_for_signal"
    decision_ts = int(cur.ts) + space_ms

    plan, why = _build(kind=KIND_INVERSE_V, direction=direction,
                       order_type=order_type, i=i15, ctx=ctx, params=params,
                       order_price=order_price,
                       bar=ctx.bars15[i15], bar_space="15m",
                       decision_ts=decision_ts,
                       ref_price=float(cur.c),
                       claim_ts=int(cur.ts),
                       claim_on_entry=True,
                       evidence={"v_ts": v.ts, "cur_ts": cur.ts,
                                 "v_high": v.h, "v_low": v.l,
                                 "cur_high": cur.h, "cur_low": cur.l,
                                 "base": base, "v_wick_pct": round(pct, 4),
                                 "signal_5m_ts": cur.ts,
                                 "signal_close": float(cur.c),
                                 "ref_price": float(cur.c),
                                 "claim_space": "5m",
                                 "note": "5m 影线形状近似 bar 内路径，"
                                         "非真实 bar 内先后"})
    return plan, why


# ---------------------------------------------------------------- 形态 3

def _count_rejections(i: int, series: Any, ctx: PlanContext,
                      params: PendingParams, direction: str, side: str,
                      line_key: str, space_ms: int) -> List[Dict[str, Any]]:
    """在**某一条线**上数「触及但收回」的拒绝根（`reject_window` 根内）。

    抽出来是为了让「多候选线」只有一个遏环实现：候选线换个名字就能复用，
    不会出现「ema20 的判据和 ema50 的判据写得不一样」这种经典漂移。
    """
    w = int(params.reject_window)
    seen: List[Dict[str, Any]] = []
    for k in range(i - w + 1, i + 1):
        b = series[k]
        lvl = ctx.book.at(line_key, b.ts + space_ms)
        if lvl is None:
            continue
        pct, rng = _wick_pct(b, side)
        if rng <= 0:
            continue
        if pct >= params.wick_min_pct and _pierced_and_closed(
                b, float(lvl.price), direction):
            seen.append({"ts": b.ts, "line": float(lvl.price),
                         "wick_pct": round(pct, 4), "idx": k,
                         "line_key": line_key})
    return seen


def detect_ema_reject(i: int, ctx: PlanContext, params: PendingParams,
                      direction: str = DOWN) -> Tuple[Optional[Plan], str]:
    """形态 3：EMA 多次拒绝 → 跌破结构低点 a 才进场（挂 STOP）。

    完整叙事（用户口述）：
        价格失效地站上 ema20 → 几根上引线（反弹被压）→ 跌破 ema20 往下砸
        → 砸出低点 a → 反弹 → 这次可能只碰一下 ema20 或压根没碰到就被拒
        → **价格再次突破 a 才进场**。

    ★ 扳机是 a，不是 ema20。ema20 只负责证明这是下跌中继里的反弹。
      a 取「最后一根 ema20 拒绝上引线 K **之前**的结构位低点」，
      往前找 `struct_lookback` 根（用户说通常不超过 10 根）。

    ★★ 2026-11-20：被拒绝的线**不再只有 ema20**。

      用户原话：「下跌反弹时 ema20 多次拒绝开空，同样适用于 ema50，
      ema 没有被多次拒绝，被突破后行情来到 ema50，ema50 出现多次拒绝
      也是同样的！上涨回调镜像」；追问后确认 ema144 也算、且不要求
      「先被 ema20 拒绝过」的先后。

      也就是说：价格突破 ema20 之后继续反弹到 ema50（或 ema144），
      在那条线上被反复压回 —— **同一个形态**，只是拒绝发生在更远的一条线上。

      候选线按 `params.REJECT_LINE_KEYS`（**由近到远**）逐条试，取第一条
      满足「窗口内 ≥`reject_min_bars` 次拒绝 且 最后一根就是拒绝根」的。

      ★ 为什么必须重试而不是「合并所有线的拒绝次数」：把 ema20 的 1 次
        和 ema50 的 1 次加在一起算「2 次」，会把两条线各自只有 1 次的
        弱信号也当成成立 —— 而用户要的是「在**同一条**线上被多次拒绝」。
    """
    w = int(params.reject_window)
    if i - w + 1 < 0:
        return None, "not_enough_bars"
    space = ctx.space_for(KIND_EMA_REJECT)            # BAR_SPACE 是唯一来源
    series = ctx.series_for(KIND_EMA_REJECT)
    space_ms = ctx.space_ms_for(KIND_EMA_REJECT)
    if i >= len(series):
        return None, "no_bar"
    bar = series[i]
    side = _side_for(KIND_EMA_REJECT, direction)

    #: 每条候选线各自的理由，用于「都没成立」时给出**最有信息量**的那个。
    why_by_line: Dict[str, str] = {}
    used_key: Optional[str] = None
    seen: List[Dict[str, Any]] = []
    for line_key in params.REJECT_LINE_KEYS:
        got = _count_rejections(i, series, ctx, params, direction, side,
                                line_key, space_ms)
        if len(got) < int(params.reject_min_bars):
            why_by_line[line_key] = "too_few_rejections"
            continue
        # 最后一根拒绝根必须是当前这根（用户：「最后一根 ema20 拒绝上引线 K」）
        if got[-1]["idx"] != i:
            why_by_line[line_key] = "last_bar_not_a_rejection"
            continue
        used_key, seen = line_key, got
        break

    if used_key is None:
        # ★ 理由要能区分两类失败，否则统计里看不出「是形状不够还是时机不对」。
        #   优先级：只要有一条线凑够了次数、只是最后一根不匹配，就报后者
        #   （那说明形态真出现过，只是离决策根还差一根 —— 更有信息量）。
        reasons = set(why_by_line.values())
        if "last_bar_not_a_rejection" in reasons:
            return None, "last_bar_not_a_rejection"
        return None, "too_few_rejections"

    # ★ 结构低点 a。
    #
    #   ★★ 口径改过（2026-11-17，用户「1、2、3都做」）：
    #     原来是「往前 `struct_lookback=10` 根的最低点」，
    #     现在 `struct_lookback=0`（默认）= **就用最后一根拒绝 K 自己的低点**。
    #
    #     为什么改：实测全序列 488 个形态候选，往后走 200 根看先发生哪个 ——
    #
    #        lookback   成交   先死   同根   成交率
    #        10         200    280     8    41.7%   ← 改前
    #         1         282    153    53    64.8%
    #        self       300    134    54    69.1%   ← 改后默认
    #
    #     即「把触发定在 10 根之外」本身扔掉了 ~27 个百分点的进场机会。
    #     先死的那些一分钱没亏，但也没进市场 —— 账面看起来像「没机会」。
    #
    #     ★ 代价必须记账：同根歧义 8 → 54 条（那根 15m 里既触及挂单价
    #       又收盘穿过 ema，5m 的 OHLC 没有 bar 内顺序，判不了先后）。
    #       这些由 `thesis` 打 `ambiguous_same_bar` 标记，统计里单列，
    #       不静默按「先成交」算。
    #     ★ 用户原话「通常不超过 10 根」仍然有效 —— 想回到老口径把
    #       `struct_lookback` 设成 10 即可，参数没删。
    lb = int(params.struct_lookback)
    if lb <= 0:
        start = i
        window = [bar]
        base_from = "拒绝根本身（struct_lookback=0）"
    else:
        start = max(0, i - lb)
        window = series[start:i]
        base_from = "往前 %d 根的极值（不含拒绝根）" % lb
    if not window:
        return None, "no_structure_window"
    if direction == DOWN:
        a = min(b.l for b in window)
    else:
        a = max(b.h for b in window)

    order_type = PATTERN_SPEC[KIND_EMA_REJECT]["order_type"]
    order_price = params.one_tick_beyond(a, direction, order_type)

    plan, why = _build(kind=KIND_EMA_REJECT, direction=direction,
                       order_type=order_type, i=i, ctx=ctx, params=params,
                       order_price=order_price, bar=bar, bar_space=space,
                       evidence={"ema_key": used_key,
                                 "reject_line_key": used_key,
                                 "reject_candidates":
                                     list(params.REJECT_LINE_KEYS),
                                 "window": w,
                                 "rejections": seen, "structure_low": a,
                                 "struct_lookback": lb,
                                 "structure_from": base_from,
                                 "structure_range": [start, i],
                                 "ref_price": bar.c,
                                 "trigger_note": "扳机是拒绝 K 的极值，不是 ema；"
                                                 "也不是 10 根以前的老结构位"})
    return plan, why


DETECTORS: Dict[str, Callable[..., Tuple[Optional[Plan], str]]] = {
    KIND_WICKS: detect_wicks_false_break,
    KIND_INVERSE_V: detect_inverse_v,
    KIND_EMA_REJECT: detect_ema_reject,
}

#: 每条形态在哪个序列上扫。★ **这是「扫哪条序列」的唯一来源**。
#:
#: ★ 三引线用 5m（用户 2026-11-17 定）。依据：同一个 7 段开仓窗口，
#:   15m 出 0 条、5m 出 1 条（HANDOFF §23.21）。
#:   ★ 这不只是「更精确」—— 形态的**时间尺度**从 45 分钟变成 15 分钟，
#:     抓到的是更短促的抖动，是另一个现象。阈值仍是未验证初值。
#:   ★ 3m **不做**（用户 2026-11-17 决定：「3m 的不用试了，先这样吧」）。
#:     原因：本地 3m 只有 37.7 小时且不在任何开仓窗口里，评不了；
#:     这条留着是为了下次别再有人自动去试 3m。
#:     ★ 三条口径已定稿：三引线 5m · 倒V 5m · EMA20拒绝 15m。
#:   ★ 改这一行就够了 —— 探测器、窗口判定、plan 时钟都从这里取
#:     （见 `PlanContext.space_for`）。以前改这里会 `IndexError`。
BAR_SPACE: Dict[str, str] = {
    KIND_WICKS: "5m",
    KIND_INVERSE_V: "5m",
    KIND_EMA_REJECT: "15m",
}

#: ★★ 2026-11-20：哪些形态**不受「开仓窗口」约束**。
#:
#:   三引线是「逐根推进的结构形态」：参考引线 → 容忍 2 根 inside bar →
#:   确认引线 → 挂单 → 等成交。它自己的状态机就是它的前提。
#:
#:   而开仓窗口是**另一族**（顺向带量突破之后的一段时间）的前置条件：
#:   窗口要求「价格突破线之后在窗口里运行」，三引线要求「价格回到线下去
#:   扫流动性」。两者在**位置上互斥** —— 实测 107 天只有 5 个窗口，
#:   且 11-20 09:50 那组教科书级三引线不在任何窗口里，于是 0 单。
#:
#:   用户 2026-11-20 明确：三引线「先找第一根引线，找到了开始记录……」
#:   —— 形态本身就是条件，不需要窗口。
#:   镜像版（上涨里摸顶做空）同理，按 **kind** 豁免，不是按方向。
WINDOW_GATE: Dict[str, bool] = {KIND_WICKS: False}

#: 前置条件闸查「价格在 ema 哪一侧」时，按顺序取第一条**存在**的线。
#:
#: ★ 2026-11-20：`thesis.FAST_LINE_KEY` 是 `ema20@30m`，生产库从来没写过
#:   这条线（见 `guard_preconditions` 里的长注释）。回退到 `ema20@15m`
#:   —— 那正是用户原话「价格远离 **15m 的 ema20**」。
GUARD_LINE_FALLBACKS = (T.SLOW_LINE_KEY,)


# ---------------------------------------------------------------- 趋势背景

#: 趋势背景看的两条线（快线 / 慢线），**只在 15m 上**取。
#:
#: ★ 用户 2026-11-20 的原话：
#:   「15m 的 ema20 和 ema50 是倾斜向下，说明预期不要太高，震荡偏下的行情，
#:     止盈目标不要太远。严格来说我们的三引线是 ema20 和 ema50 有同向趋势
#:     且相距越走越宽，这时反弹会相对强劲一些，像现在这种不是标准的三引线
#:     买法，反弹也会弱一些。」
#:   「下跌时 ema20 是快线，在 ema50 下面才是正常的，假如在 ema50 上面，
#:     说明在尝试反转，那就是别的策略开单，三引线和倒V不开单，上涨镜像。」
TREND_FAST_KEY = T.TREND_FAST_KEY         # ema20@15m
TREND_SLOW_KEY = T.TREND_SLOW_KEY         # ema50@15m

#: 判「斜率/开口」回看几根 15m。3 根 = 45 分钟，能看出倾斜但不至于滞后。
#: ★ 未验证初值。
TREND_LOOKBACK_BARS = 3

#: 背景分级。**位置**决定开不开单，**斜率/开口**只决定预期多强、止盈放多远。
TREND_STANDARD = "standard"    # 同向 + 开口变宽 → 趋势在加强，4R 照放
TREND_WEAK = "weak"            # 位置对，但斜率不同向或开口收窄 → 弱，目标收近
TREND_WRONG_SIDE = "wrong_side"  # 快线在慢线的**错误一侧** → 这笔不归本族做
TREND_FLAT = "flat"            # 两线重合，判不出背景 → 判不了就不做

#: 每族要求的背景方向（用户 2026-11-20 逐条点名）。
#:
#: ★★ **极性按「族」定，不按「方向」定** —— 这是上一版判错的地方。
#:
#:   用户原话（把三族放在同一句里）：
#:     「在下跌趋势，ema20 在 ema50 下面，**三下引线和倒V开单**，
#:       **反弹时 ema20 拒绝开空单**；上涨趋势，ema20 在 ema50 上面，
#:       三上引线和V开单，回调时 ema20 拒绝开多单。」
#:
#:   摊开就是：
#:
#:     | 形态 | 族 | 基准方向 | 要求的背景 |
#:     |---|---|---|---|
#:     | 三引线（下引线/上引线） | reversal | long / short | **逆**着做：做多要跌、做空要涨 |
#:     | 倒V / V | reversal | long / short | **逆**着做：同上 |
#:     | EMA 拒绝 | **trend** | short / long | **顺**着做：做空要跌、做多要涨 |
#:
#:   为什么反转族要「逆」：三引线是**抄底**（在下引线扫出新低后买），
#:   前提本来就是下跌背景；倒V 同理。用户说的「在 ema50 上面说明在尝试反转，
#:   那就是别的策略开单」正是这个意思。
#:
#:   为什么趋势族要「顺」：EMA 拒绝是**反弹做空**（下跌中继里反弹到 ema20
#:   被拒 → 继续跌）。上一版把「做空 ⇒ 要 ema20 > ema50」当成通用规则，
#:   对趋势族正好**反了** —— 那会把所有真正的反弹做空全拒掉。
_TREND_REQUIRED_REGIME: Dict[str, Dict[str, str]] = {
    # 反转族：做多要下跌背景，做空要上涨背景
    T.FAMILY_REVERSAL: {UP: "down", DOWN: "up"},
    # 趋势族：做空要下跌背景，做多要上涨背景
    T.FAMILY_TREND: {DOWN: "down", UP: "up"},
}


def trend_context(book: Any, close_ms: int, direction: str, *,
                  family: str,
                  lookback: int = TREND_LOOKBACK_BARS
                  ) -> Optional[Dict[str, Any]]:
    """量出 ema20/ema50 的**位置 + 斜率 + 开口**，并给出背景分级。

    ★ 为什么必须是一个函数、一个结论：这套判据要同时被
      ① 开单前的闸（位置对不对）、② 定价（止盈放多远）、③ 证据（事后排查）
       三个消费方使用。三处各写一遍，就是本仓库反复吃亏的
      「同一事实两个来源」—— 改了一处另两处静默不跟。

    ★ `family` **必填**（不给默认值）：极性按族定（见
      `_TREND_REQUIRED_REGIME`），默认值一旦猜错，就会把整整一族的方向判反，
      而且**不会报错**。宁可让调用方显式写出来。

    返回 ``None`` 表示**这两条线取不到**（缺线/这根还没就绪）。
    调用方必须把 None 当成「判不了」而不是「没问题」：

        · 闸：判不了 → 按**保守方向**拒（`no_trend_line`）；
        · 定价：判不了 → 不放宽（照原 4R）。

    ★ 判据分两块，**互不混用**：

        ① 位置（决定开不开单，二值）
             反转族：做多要 ema20 < ema50；做空要 ema20 > ema50
             趋势族：做空要 ema20 < ema50；做多要 ema20 > ema50

        ② 强弱（决定止盈放多远，分级）
             只看背景自己，与形态无关：
               两根同向 **且** |ema20−ema50| 变大 → `standard`（4R 照放）
               否则                                  → `weak`  （目标收到最近的顺向关键线）

        ★ 为什么强弱「只看背景自己」：用户说的是「ema20 和 ema50 有同向趋势
          且相距越走越宽，这时**反弹**会相对强劲一些」。开口变宽 = 价格被拉
          离均线（超买/超卖更极端）→ 回归的动能更大。这个道理对做多（抄底）
          和做空（摸顶）是同一件事，所以不需要按形态分。
    """
    if book is None:
        return None
    now_f = book.at(TREND_FAST_KEY, int(close_ms))
    now_s = book.at(TREND_SLOW_KEY, int(close_ms))
    prev_ms = int(close_ms) - max(1, int(lookback)) * _space_ms("15m")
    prev_f = book.at(TREND_FAST_KEY, prev_ms)
    prev_s = book.at(TREND_SLOW_KEY, prev_ms)
    if now_f is None or now_s is None or prev_f is None or prev_s is None:
        return None
    f, s = float(now_f.price), float(now_s.price)
    pf, ps = float(prev_f.price), float(prev_s.price)
    if min(f, s, pf, ps) <= 0:
        return None

    # ---- 背景方向（由**线自己的位置**决定，与形态无关）----
    if f == s:
        regime = "flat"
    elif f < s:
        regime = "down"
    else:
        regime = "up"

    # ---- 这一族这个方向要求的背景 ----
    want = _TREND_REQUIRED_REGIME.get(family, {}).get(direction)
    if want is None:
        # 族/方向没登记 → 判不了。不许默认放行（那正好是「缺字段回退默认值」）。
        return None

    # ---- 强弱（只在方向明确时才有意义）----
    same_dir = ((f - pf) < 0 and (s - ps) < 0) if regime == "down" else (
        (f - pf) > 0 and (s - ps) > 0) if regime == "up" else False
    gap_now, gap_prev = abs(f - s), abs(pf - ps)
    widening = gap_now > gap_prev

    aligned = (regime == want)
    if regime == "flat":
        grade = TREND_FLAT
    elif not aligned:
        grade = TREND_WRONG_SIDE
    elif same_dir and widening:
        grade = TREND_STANDARD
    else:
        grade = TREND_WEAK

    return {"grade": grade, "family": family, "direction": direction,
            "fast_key": TREND_FAST_KEY, "slow_key": TREND_SLOW_KEY,
            "fast": round(f, 2), "slow": round(s, 2),
            "slope_fast": round(f - pf, 2), "slope_slow": round(s - ps, 2),
            "gap_now": round(gap_now, 2), "gap_prev": round(gap_prev, 2),
            "regime": regime, "required_regime": want,
            "aligned": bool(aligned), "same_dir": bool(same_dir),
            "widening": bool(widening), "lookback": int(lookback),
            "decision_ts": int(close_ms),
            "rule": ("位置：%s 族做%s要求 ema20 %s ema50（当前 regime=%s，要求 %s）；"
                     "强弱：同向+开口变宽=standard(4R)，否则 weak(目标收到最近"
                     "顺向关键线)"
                     % (family, "多" if direction == UP else "空",
                        "<" if want == "down" else ">", regime, want))}


def nearest_favorable_line(book: Any, close_ms: int, entry: float,
                           direction: str,
                           keys: Sequence[str] = (),
                           min_dist: float = 0.0) -> Optional[Dict[str, Any]]:
    """取**最近的顺向关键线**（做多：入场价上方最近的一条）。

    ★ 用途：`weak` 背景的止盈目标。用户口径「反弹也会弱一些 → 止盈目标
      不要太远」，而这个系统里**「目标位」本来就有关键价这一层**（ema20/50/144、
      前高前低都是观察线）。所以不是拍一个新倍数，而是把目标收到现实里
      真的存在的那条线。

    ★ 为什么要求 ``line > entry``（做空 ``line < entry``）：不利方向的线
      不是目标位；把它当目标等于把止盈放到止损那一侧。

    ★ ``min_dist``：要求这条线离入场价**至少**这么远（调用方传 1R）。
      为什么不是「无条件取最近的一条」：一条贴脸的线（比如 0.2R 外的 ema20）
      当目标时，实测手续费中位吃掉止损的 44.4%，那笔的净期望是负的。
      所以从「≥1R 的顺向线」里取最近的 —— 既不放松（没有 1R 以上就拒绝），
      也不会因为一条贴脸的线把整个机会丢掉。
    """
    if book is None or close_ms is None:
        return None
    keys = tuple(keys) or OBSERVATION_LINE_KEYS
    entry = float(entry)
    up = (direction == UP)
    floor = abs(float(min_dist))
    best = None
    for key in keys:
        pt = book.at(key, int(close_ms))
        if pt is None:
            continue
        line = float(pt.price)
        if line <= 0:
            continue
        if up and not (line > entry):
            continue
        if (not up) and not (line < entry):
            continue
        dist = abs(line - entry)
        if dist < floor:
            continue
        if best is None or dist < best["dist"]:
            best = {"key": key, "line": round(line, 2), "dist": dist,
                    "src_ts": int(getattr(pt, "src_ts", 0) or 0)}
    if best is None:
        return None
    best.pop("dist", None)
    return best


#: 「最近的顺向关键线」查哪些线（**与出场观察线同一张表**，不另立一份）。
OBSERVATION_LINE_KEYS = T.OBSERVATION_KEYS

#: 哪些形态要过「趋势背景」这道闸（位置必须与背景相配）。
#:
#: ★ 2026-11-20 用户点名的**三族都在里面**（原话把三族放在同一句里）：
#:   下跌趋势（ema20 < ema50）→ 三下引线 / 倒V 开单，**反弹时 ema20 拒绝开空单**；
#:   上涨趋势（ema20 > ema50）→ 三上引线 / V 开单，**回调时 ema20 拒绝开多单**。
#:
#: ★ 上一版只登记了两族、把 EMA 拒绝排除在外，理由是「用户只点名了三引线和
#:   倒V」。那句话读窄了 —— 用户说的正是「反弹时 ema20 拒绝开空单」，也就是
#:   EMA 拒绝**同样有背景前提**，只是极性相反（它顺背景做，见
#:   `_TREND_REQUIRED_REGIME`）。漏掉它的后果很具体：EMA 拒绝是趋势族，
#:   本来就不该在逆势里开单，而它的基准方向是**做空** —— 若不登记，
#:   上一版那条「做空就要 ema20 > ema50」的通用规则一旦被套上去，
#:   会把所有真正的反弹做空全拒掉。
TREND_GATE: Dict[str, bool] = {
    KIND_WICKS: True,
    KIND_INVERSE_V: True,
    KIND_EMA_REJECT: True,
}


def _build(kind: str, direction: str, order_type: str, i: int,
           ctx: PlanContext, params: PendingParams, order_price: float,
           evidence: Dict[str, Any], *,
           bar: Optional[Any] = None,
           bar_space: Optional[str] = None,
           decision_ts: Optional[int] = None,
           ref_price: Optional[float] = None,
           claim_ts: int = 0,
           claim_on_entry: bool = False) -> Tuple[Optional[Plan], str]:
    """组装计划并做出口校验。返回 ``(plan, reason)``。

    Args:
        i: **所在序列里的下标**（哪条序列由 `bar_space` 决定）。
        bar: 决策那一根 K。给了就用它，不再按下标去取 —— 这是唯一
            不会取错序列的方式。
        bar_space: ``'5m'`` / ``'15m'``，这条计划的 ``bar_index`` / ``bar_ts``
            属于哪条序列。默认取 `BAR_SPACE[kind]`。
            ★ 为什么要显式记：以前这个时钟是**隐含**的（一律当 15m），
              把三引线改到 5m 时，`bar_index` 变成 5m 的下标，
              而 `run_plan` 的 `start_at` 还按 15m 解释 → 静默从错误的位置
              开始；`claim_for` 也会拿 5m 的 ts 去比 15m 的 K → 永远不挂单。
        decision_ts: **决策时刻**（毫秒）。价位里的 1h 振幅、5m 振幅都按这一刻
            取；默认是这根 K 的收盘。
        ref_price: 判「机会是不是已经过去了」用的参照价。默认这根 K 的收盘。
            倒V 必须传信号那根 5m 的收盘 —— 用了 15m 收盘就会系统性偏高
            （15m 收盘比 5m 收盘晚 10 分钟），把机会判成早就没了。
        claim_ts / claim_on_entry: 声明落在哪根 K 上。见 `Plan`。

    ★ 为什么 `_build` 返回 reason 而不是「失败就 None」：真实数据上踩到过 ——
      倒 V 的触发价是用**5m** 的两根高点算的，但声明却落在**下一根 15m**
      收盘；等到那根 15m 收盘时，价格可能早就涨过触发价了。
      这时候再挂 BUY STOP 就是**立刻成交的市价追单**。
      正确行为是**跳过并记账**（`already_triggered`），而不是抛异常、
      也不是静默丢掉 —— 本项目纪律：禁止静默过滤。
    """
    space = bar_space or ctx.space_for(kind)
    if bar is None:
        series = ctx.bars5 if space == "5m" else ctx.bars15
        if i < 0 or i >= len(series):
            return None, "no_bar"
        bar = series[i]
    # ★ 时钟必须跟 `bar` 自己那条序列走，也就是 `space`（= bar_space），
    #   **不能**用 `ctx.space_ms_for(kind)`。
    #
    #   2026-11-17 修：倒V 的 `bar_space` 是 "15m"（信号那根 15m），
    #   而 `space_for(KIND_INVERSE_V)` 是 "5m" —— 两者差 10 分钟。
    #   以前这里用 kind 的周期算 close_ms，倒V 只是**碰巧**每次都显式传了
    #   `decision_ts` 才没出错。哪天有人省掉那个参数，amp5 和 h1_amp 就会
    #   按早 10 分钟的时刻取，价位全偏，而且不报错。
    close_ms = int(decision_ts) if decision_ts else bar.ts + _space_ms(space)
    h1 = ctx.h1_prev2_amp(close_ms)
    amp5 = ctx.amp5_at(close_ms)
    if h1 is None or amp5 is None:
        return None, "no_amp"
    # ---- 趋势背景（ema20/ema50 的位置 + 斜率 + 开口）----
    #
    # ★ 2026-11-20 接进生产路径。用户口径：
    #   · **位置**决定开不开单：下跌时快线 ema20 在慢线 ema50 下方才是顺势，
    #     在**上方**说明在尝试反转 —— 那是别的策略的活，三引线/倒V 不开单。
    #   · **斜率/开口**决定预期多强：同向且越走越宽 = 标准形态（反弹强，4R 照放）；
    #     否则反弹弱，止盈收到最近的顺向关键线。
    #
    # ★ 为什么放在定价**之前**：目标价要用这条结论（`target_cap`）。
    #   放后面就得重算一遍成本/净 R，那等于两份定价实现。
    trend = None
    target_cap = None
    if TREND_GATE.get(kind, False):
        # ★ `family` 必须显式传：极性按族定（反转族逆背景、趋势族顺背景），
        #   默认值一旦猜错就整族判反，而且不会报错。
        trend = trend_context(ctx.book, int(close_ms), direction,
                              family=PATTERN_FAMILY[kind])
        if trend is not None and trend["grade"] == TREND_WRONG_SIDE:
            # 位置错 = 用户明确说「不开单的那一类」。不是强弱问题，直接拒。
            return None, "trend_wrong_side"
        if trend is not None and trend["grade"] == TREND_FLAT:
            # 两条线重合 = 判不出背景。判不了就不做（不许默认放行）。
            return None, "trend_flat"
        if trend is None:
            # 取不到线 = **判不了**，不许当「没问题」放行。
            # 这条以前会静默空转（`book.at` 恒 None），所以缺线必须在别处喊
            # （`main.py` 的装线名单会报警），这里按保守方向拒。
            return None, "no_trend_line"
        if trend["grade"] == TREND_WEAK:
            stop_r = _stop_distance(
                order_price, amp_stop_distance(h1, amp5, params),
                params, order_type=order_type)
            cap = nearest_favorable_line(ctx.book, int(close_ms), order_price,
                                         direction, min_dist=stop_r)
            if cap is not None:
                target_cap = cap["line"]

    pp = plan_prices(order_price, direction, h1, amp5, params,
                     order_type=order_type, target_cap=target_cap)
    ev = dict(evidence)
    if trend is not None:
        ev["trend_ctx"] = trend
        ev["target_cap"] = target_cap
    ev.update({"price_plan": pp.evidence, "stop_distance": pp.stop_distance,
               "h1_amp": pp.h1_amp, "amp5": pp.amp5,
               "order_type": order_type, "tick_size": params.tick_size,
               "decision_ts": int(close_ms),
               "cost_win_points": pp.cost_win_points,
               "cost_loss_points": pp.cost_loss_points,
               "cost_win_r": pp.cost_win_r, "cost_loss_r": pp.cost_loss_r,
               "net_win_r": pp.net_win_r, "net_loss_r": pp.net_loss_r,
               "breakeven_win_rate": pp.breakeven_win_rate,
               "amp5_note": "计划时刻近似；实盘应在成交那根用真实振幅重算"})
    # ★ 镜像必须换名字，但**判据是「跑的是不是镜像那一版」，不是方向**。
    #
    #   用户原话：「上涨时是镜像实现」。所以：
    #     基准版 = 「下跌…」名字，跑的是 PATTERN_SPEC 里记的方向；
    #     镜像版 = 「上涨…」名字，方向与基准相反。
    #
    #   第一版写成 `mirror if direction == UP else kind` —— 对 EMA 拒绝
    #   （基准方向 DOWN）碰巧是对的，对三引线/倒 V（基准方向 UP）正好**反了**：
    #   基准版被叫成「上涨…」，镜像版被叫成「下跌…」。
    #   而且不会报错，只是统计/日志/订单备注全错。
    name = _variant_name(kind, direction)
    plan = Plan(kind=name, direction=direction, order_type=order_type,
                bar_index=i, bar_ts=bar.ts, bar_space=space,
                order_price=pp.order_price,
                stop_price=pp.stop_price, target_price=pp.target_price,
                evidence=ev, claim_ts=int(claim_ts or 0),
                claim_on_entry=bool(claim_on_entry),
                family=PATTERN_FAMILY[kind])

    # 1) 价位自洽（止损/挂单/止盈三者的先后）—— 这里必须是硬错误。
    plan.validate()

    # 2) ★ 前置条件（第③道闸）—— 2026-11-17 接进生产路径。
    #
    #   在这之前 `guard_preconditions` 只被 `tools/_smoke_pending_plan.py`
    #   单独调用，`detect_all` 出来的计划**从没经过它**。
    #   于是「四道闸」实际上只有三道在流水线上，第三道靠调用方自觉 ——
    #   谁忘了调谁就漏，而且不会有任何报错。
    #   代价是每次候选多查一次线（可忽略），换来的是闸门位置唯一。
    if params.apply_guard:
        # ★ 只有**趋势族**（EMA 拒绝）传 `line_key` = 它实际被拒绝的那条线。
        #
        #   理由：EMA 拒绝现在可以发生在 ema20/ema50/ema144 上，而
        #   ema50/ema144 的场景本来就是「价格已突破 ema20、反弹到更远的线」，
        #   按 ema20 判位置会把这些单全拒掉（实测 155+75 个候选放行 0 个）。
        #
        #   ★ 反转族（三引线 / 倒V）**绝不传**：它们的口径一直是
        #     「价格远离 15m 的 ema20」，而且它们的证据里根本没有
        #     `reject_line_key` 这个字段 —— 顺手传 None 等于悄悄改了判据。
        _guard_line = None
        if PATTERN_FAMILY.get(kind) == T.FAMILY_TREND:
            _guard_line = (evidence or {}).get("reject_line_key")
        ok, why = guard_preconditions(plan, bar, ctx.book,
                                      direction=direction, kind=plan.kind,
                                      close_ms=int(close_ms),
                                      line_key=_guard_line)
        if not ok:
            return None, "precondition:%s" % why

    # 3) 挂单价相对现价的方向 —— 已经跑到另一侧 = 机会已经过去了。
    close = float(ref_price if ref_price is not None else bar.c)
    falls = plan.triggers_when_price_falls
    if falls and not (plan.order_price < close):
        # 该往下成交，但现价已经跌破/跌到挂单价下方 → 晚了
        return None, "already_triggered"
    if (not falls) and not (plan.order_price > close):
        # 该往上成交，但现价已经涨破挂单价 → 晚了
        return None, "already_triggered"
    return plan, "ok"


def dedupe_plans(plans: Sequence["Plan"],
                 window_keys: Sequence[Optional[Tuple[int, int]]], *,
                 one_per_window: bool = True) -> List["Plan"]:
    """同一窗口内、同形态同方向只保留**第一条**。

    ★ 为什么必须去重（实测，§23.19）：
      2026-08-04 那个窗口里，「上涨EMA拒绝」在 21:15 / 21:30 / 22:00 /
      22:15 / 22:45 连续出了 5 条，**挂单价全部是 64397.7**。
      形态在连续几根上都成立，探测器就会连续出单 —— 实盘等于同一套设置
      下挂了 5 倍的仓。统计上也是同一个 Setup 被数了 5 次。

    ★ 为什么用「同窗口只留第一条」而不是价格阈值：
      价格邻近阈值（比如 0.05%）是**未验证的初值**，本项目纪律不许为凑数
      发明阈值。而「一个窗口 = 一次机会」不需要任何数字：窗口本身就是
      「顺向带量突破之后的那一段」，同一段里同一形态反复触发说的是同一件事。

    Args:
        plans: 按时间顺序收集的计划（`detect_all` 就是这个顺序）。
        window_keys: 与 ``plans`` **一一对应**的窗口键 ``(start_ms, end_ms)``，
            ``None`` 表示该条不在任何窗口里。由 `detect_all` 在判窗口的
            同时记录 —— 不允许事后用 ``plan.bar_ts`` 反推：
            实测 ``plan.bar_ts`` 与判窗口用的收盘时刻差一个周期，
            反推会让边缘那条落到别的窗口，去重就漏了。
        one_per_window: 关掉就原样返回（仅供对比测量）。

    Returns:
        去重后的计划，保持原顺序（第一条胜出）。

    ★ 窗口键为 ``None`` 的计划**不去重** —— 那种情况只出现在
      ``windows is None``（只验形状）的调用里，此时没有「同一窗口」可比。
      这里不许静默丢计划：丢了会表现成「少了几条」，比报错难查得多。
    """
    if not one_per_window or not plans:
        return list(plans)
    if len(window_keys) != len(plans):
        raise ValueError(
            "window_keys 必须和 plans 等长（%d vs %d）"
            % (len(window_keys), len(plans)))
    out: List[Plan] = []
    seen = set()
    for p, wkey in zip(plans, window_keys):
        if wkey is None:
            out.append(p)          # 没有窗口可比 → 不去重，不许静默丢
            continue
        key = (p.kind, p.direction, int(wkey[0]), int(wkey[1]))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def detect_all(ctx: PlanContext,
               params: Optional[PendingParams] = None,
               kinds: Sequence[str] = KINDS,
               direction: Optional[str] = None,
               require_window: bool = False,
               include_mirror: bool = False,
               dedupe: bool = True,
               one_per_window: bool = True
               ) -> Tuple[List[Plan], Dict[str, int]]:
    """扫全序列，返回 (计划列表, 各类未成立原因的计数)。

    Args:
        direction: ``None``（默认）表示**按 `PATTERN_SPEC` 各自的方向**跑，
            因为三条形态的方向本来就不一样；传 ``DOWN``/``UP`` 则强制统一。
        require_window: **生产必须传 True**。为 True 时若 `ctx.windows` 是
            ``None``（没给开仓窗口）就直接抛错。
            ★ 为什么要有这个参数：单元测试可以只验形态形状、不管背景；
              但生产路径上「忘了传窗口」的后果是**计划数从几十条暴涨到一千多条
              而且不报错** —— 这正是本轮之前的状态。让它在生产路径上硬失败。
        include_mirror: 是否同时跑**镜像版**（用户口径「我们上涨时是镜像的」）。
            ★ 必须显式打开：``direction=None`` 只跑基准版，而三条基准版都在
              下跌背景下，所以上涨时的摸顶做空 / 回调做多需要把方向翻过来
              再跑一遍。不打开不会报错，只会表现为「上涨行情里永远不出计划」。
        dedupe: 是否**去重**（默认 True，见 `dedupe_plans`）。
            ★ 关掉它的后果是同一窗口里同一形态连续出单（实测 5 条同价），
              实盘等于 5 倍仓位，统计上等于同一 Setup 被数 5 次。
        one_per_window: 去重的粒度；关掉等于不去重（仅供对比测量）。

    ★ 第二个返回值是**强制项**：项目纪律「禁止静默过滤」。
      少了一千条没人知道，比报错难查得多。
      注意：`stats` 记的是**去重前**的计数，所以「各原因计数」之和
      不等于返回的计划数 —— 差额就是被去重掉的重复单。
    """
    params = params or PendingParams()
    if require_window and ctx.windows is None:
        raise ValueError(
            "detect_all(require_window=True) 但没给开仓窗口。"
            "三条形态只在「顺向带量突破 → 下一个区间形成过程」里存在"
            "（用户 2026-11-17 口径，见 HANDOFF §23）；"
            "不给窗口就会在任意位置产生计划，而且不会报错。"
            "请先 build_open_windows(...) 再建 PlanContext，"
            "或显式传 require_window=False 表示「只验形状、不管背景」。")
    plans: List[Plan] = []
    plan_wkeys: List[Optional[Tuple[int, int]]] = []
    stats: Dict[str, int] = {}
    # 要把哪些 (kind, direction) 组合各跑一遍
    combos = []
    for kind in kinds:
        base_dir = PATTERN_SPEC[kind]["direction"]
        if direction is not None:
            combos.append((kind, direction))
        else:
            combos.append((kind, base_dir))
            if include_mirror:
                combos.append((kind, DOWN if base_dir == UP else UP))
    for kind, d in combos:
        fn = DETECTORS[kind]
        seq = ctx.series_for(kind)                # BAR_SPACE 是唯一来源
        # 统计的键也要跟着「基准版/镜像版」换名字，否则两个方向混在一张表里
        # 分不清。判据必须与 _build 用同一份（_variant_name），
        # 不能在这里另写一个 `d == UP` —— 那正是第一版把名字对调的原因。
        label = _variant_name(kind, d)
        # ★ 背景方向：**基准版一律在下跌背景里，镜像版在上涨背景里**。
        #   三引线/倒V 是「大跌抄底」（做多）、EMA 拒绝是「反弹做空」（做空），
        #   三条的基准版都在下跌背景下，这是用户 2026-11-17 定的定位。
        #   镜像（上涨）时方向翻转 → 抄底变摸顶做空、反弹做空变回调做多。
        #   ★ 必须带方向配对：只按 tag 不按 direction，倒V 距 ema20 的中位
        #     会从 +0.124% 变成 +0.278%（实测更差），因为 `突破` 里混着涨破。
        bg = background_of(kind, d)       # ★ 唯一来源
        space_ms = ctx.space_ms_for(kind)
        for i in range(len(seq)):
            wkey = None
            # ★ 三引线这类「自带状态机的结构形态」不进窗口闸（见 WINDOW_GATE）。
            if ctx.windows is not None and WINDOW_GATE.get(kind, True):
                ts = int(seq[i].ts) + space_ms      # 用**收盘**时刻判窗口
                w = ctx.window_at(ts)
                if w is None or (bg is not None and w.direction != bg):
                    key = "%s:outside_window" % label
                    stats[key] = stats.get(key, 0) + 1
                    continue
                # ★ 在**判窗口的同一个地方**把窗口键记下来。
                #   不许事后用 plan.bar_ts 再推一遍 —— 实测踩过：
                #   plan.bar_ts 和这里的 ts 不是同一个值（差一个周期），
                #   于是边缘那条被算到别的窗口/算成 None，去重就漏了，
                #   表现为「UP 窗口 5 段却出了 6 条」。
                wkey = (int(w.start_ms), int(w.end_ms))
            plan, why = fn(i, ctx, params, d)
            # ★ 现实跟随：R（区间反向边界）落在 1R..止盈之间就把止盈收紧到 R。
            #   必须在这里做 —— 只有这里同时握着 `ctx.book` 和计划的声明时刻。
            if plan is not None:
                plan = apply_range_target(plan, ctx.book)
            key = "%s:%s" % (label, why)
            stats[key] = stats.get(key, 0) + 1
            if plan is not None:
                plans.append(plan)
                plan_wkeys.append(wkey)
    # ★ 去重放在最后：`stats` 保留**去重前**的真实触发次数（禁止静默过滤），
    #   返回的计划列表才去重。两者的差额就是被合并掉的重复单。
    if dedupe:
        plans = dedupe_plans(plans, plan_wkeys,
                             one_per_window=one_per_window)
    return plans, stats


# ---------------------------------------------------------------- 接 thesis

#: R（反向边界）的线键。方向不同、存在的列也不同 —— 唯一来源是
#: `kbar/lines.py::range_p` 的注释：
#:     DOWN: hard_up=hi_b=R, hard_dn=None
#:     UP  : hard_dn=lo_b=R, hard_up=None
RANGE_R_KEY = {T.UP: "range_hard_dn@1h", T.DOWN: "range_hard_up@1h"}

#: 声明那根 K 的周期（毫秒）。区间线固定 1h，但**声明**发生在 5m 或 15m 上。
CLAIM_MS = {"5m": 300_000, "15m": 900_000}


def claim_close_ms(plan: "Plan") -> int:
    """这条计划「声明那一刻」的时间戳：声明那根 K 的**收盘**时刻。

    ★ 为什么必须由 `bar_space` 决定，不能一律加 1h：
      `claim_ts` 是 5m / 15m 那根 K 的**起始**时刻，不是 1h 桶的起点。
      一律按 `ts + 1h` 取区间线，等价于承认「声明时已经能看到那根 1h 的收盘价」
      —— 而那根 1h 当时还在形成中。
    """
    space = str(getattr(plan, "bar_space", "15m") or "15m")
    iv = CLAIM_MS.get(space, 900_000)
    return int(plan.effective_claim_ts) + iv


def range_r_price(book, close_ms: int, direction: str) -> Optional[float]:
    """取该方向的「反向边界 R」在 ``close_ms``（声明那根 K 收盘）**已就绪**的值。

    ★★ 2026-11-19 修：这里原来是 `book.at(key, ts + 1h)`，注释写「这才是这根
      1h 已收盘」—— 只有当 `ts` 恰好是 1h 桶起点时才成立。实测

        392 / 1250 条计划（31.4%）取到的线在声明 K 收盘之后才就绪，
        最多提前 55 分钟。

      这是**偷未来**：回测会拿到实盘此刻不可能有的区间边界。`kbar/lines.py`
      有专门的「防偷未来」硬校验（`line_ready_ms > bar_close_ms` 就抛），
      但本函数自己算 `at()`，绕过了那道闸。
      价格上暂时看不出差异（区间边界常连续几小时不变），所以它不会报错，
      只会在边界真的移动的那一小时给出一个假的好价位——
      正是本项目反复吃亏的「静默失效」。

    ★ 为什么 R 要按方向取两条不同的线：区间把 R 折叠进了 hard_up / hard_dn，
      见 `kbar/lines.py::range_p` 的注释。取错方向不是拿到 None 就是拿到
      另一侧的线 —— 两种都会让「收紧止盈」彻底失效，而且不报错。
    """
    if book is None:
        return None
    key = RANGE_R_KEY.get(direction)
    if not key:
        return None
    try:
        pt = book.at(key, int(close_ms))     # at() 只返回 ready_ms <= close_ms 的
    except Exception:
        return None
    if pt is None:
        return None
    try:
        v = float(pt.price)
    except (TypeError, ValueError, AttributeError):
        return None
    return v if v > 0 else None


def tighten_target_to_range(order_price: float, stop_price: float,
                            target_price: float, r_px: Optional[float],
                            direction: str) -> Tuple[float, Optional[float]]:
    """现实跟随：R 落在「1R ↔ 止盈」之间时，把止盈收紧到 R。

    返回 ``(生效止盈, r 的 R 倍数)``。r 倍数为 None 表示这条腿取不到。

    ★★ 这是一条**只收紧、不放宽**的规则，方向是刻意选过的：

        1R <= r_R < r_target  →  止盈 = R 的价位      （提前落袋）
        其它（含 r_R >= 止盈、r_R < 1R、取不到）  →  维持原止盈

    为什么必须只收紧：放宽意味着「本来该走的单子因为区间边界更远而继续扛」，
    出事的方向是**晚走**。收紧出事的方向是**早走**，最坏结果是少赚。
    在没验证过的初值上，永远选「少赚」这一侧。

    为什么下限是 1R 而不是 0：实测 1271 条真实计划，R 有 2~4% 落在
    「入场价 ↔ 止损价」之间。那些计划一旦接上这条规则，挂单成交的同一根
    K 就会立刻触发止盈（甚至还没离开止损距离），等于把「提前止损」伪装成
    「止盈」——归因、统计、看板全都会被污染。1R 是「已经走过一个止损距离」
    的最小确认，用它当门槛比任何魔法数字都好解释。

    为什么上限用 ``r_target`` 而不是硬编码 4R：止盈本来就是由公式算出来的
    （`4 × d_max`），实测目标/R 中位 4.000 但最小 0.926。硬编码 4 会让
    「公式变了之后」这条规则跟着静默失效，而它不会报错。
    """
    if r_px is None:
        return float(target_price), None
    # ★ 未知方向必须**原地不动**，不能落进 else 被当成空单。
    #   实测踩过（2026-11-19 部署验收）：壳里手写了 "UP"，而 T.UP == "up"，
    #   于是多单走了空单分支，算出一个看起来完全合理的「-2.0R」，
    #   并且悄无声息地把止盈改错。fail-safe 的方向永远是「不改」。
    if direction not in (T.UP, T.DOWN):
        return float(target_price), None
    r = abs(float(order_price) - float(stop_price))
    if r <= 0:
        return float(target_price), None
    o = float(order_price)
    if direction == T.UP:
        r_r = (float(r_px) - o) / r
        r_t = (float(target_price) - o) / r
    else:
        r_r = (o - float(r_px)) / r
        r_t = (o - float(target_price)) / r
    if r_r < 1.0 or r_r >= r_t:
        return float(target_price), r_r
    return float(r_px), r_r


def apply_range_target(plan: "Plan", book) -> "Plan":
    """按区间演化把计划的止盈收紧（记录证据，不改变 4R 的原始值）。

    ★ 为什么在**探测时**做而不是在 `to_thesis` 里做：`to_thesis` 手里没有
      `LineBook`，而 R 必须按**声明那一刻**已就绪的区间线取。放到 thesis 里
      现算，就会用「后来才收盘」的线去定一条早就该定的止盈 —— 那是偷未来。

    ★ 取线时刻必须是**声明那根 K 的收盘**（`claim_close_ms`），不是 `ts + 1h`。
      见 `range_r_price` 的注释：后者让 31.4% 的计划读到尚未收盘的那根 1h。
    """
    if plan is None or book is None:
        return plan
    try:
        r_px = range_r_price(book, claim_close_ms(plan), plan.direction)
    except Exception:
        return plan
    new_target, r_mult = tighten_target_to_range(
        plan.order_price, plan.stop_price, plan.target_price, r_px,
        plan.direction)
    if new_target == plan.target_price:
        return plan
    ev = dict(plan.evidence or {})
    ev["range_r_price"] = float(r_px)
    ev["range_r_mult"] = round(float(r_mult), 4) if r_mult is not None else None
    ev["target_4r"] = float(plan.target_price)
    return dataclasses.replace(plan, target_price=new_target, evidence=ev)


def to_thesis(plan: Plan, thesis_id: str = "",
              params: Optional[PendingParams] = None) -> T.Thesis:
    """把计划变成一条可交给 `thesis.run()` 的预期。

    ★ 2026-11-17 补 `stale_distance` —— 反转家族挂单期的预期衰减距离。
      只给**反转**家族：趋势家族已经由 ema 负责撤单，再加一条就成了
      两个来源争着撤，先撤的那个会让归因失真。

    ★ 同日**撤掉**持仓时间闸（`max_hold_bars`）：用户裁决「持仓时长用
      预期与现实管理」，时间闸会把「目标不合理」藏起来（见 `thesis.run`）。
    """
    params = params or PendingParams()
    tid = thesis_id or "%s-%s-%s" % (plan.direction, plan.kind, plan.bar_ts)
    stale = 0.0
    if plan.family == T.FAMILY_REVERSAL:
        sd = float(plan.evidence.get("stop_distance") or 0.0)
        stale = round(sd * float(params.stale_stop_mult), 4)
    th = T.Thesis(thesis_id=tid, kind=plan.kind, direction=plan.direction,
                  order_price=plan.order_price, stop_price=plan.stop_price,
                  target_price=plan.target_price,
                  order_type=plan.order_type,
                  horizon_bars=params.horizon_bars,
                  stale_distance=stale,
                  surprise_pts=params.surprise_pts,
                  family=plan.family,
                  note="bar_ts=%d" % plan.bar_ts)
    th.validate()
    return th


def claim_for(plan: Plan) -> T.ClaimFn:
    """生成 claim：只在**计划成立的那一根**返回 True。

    这一根的时钟由 `plan.claim_on_entry` 决定：

    * False（三引线 / EMA 拒绝）：`bars` 是 ``plan.bar_space`` 那条序列
      （当前是 15m），比对 `plan.bar_ts`。形态定义在那条序列的收盘上，
      不能提前到 5m。
    * True（倒V）：`bars` 是 **5m** 序列，比对 `plan.claim_ts`。
      必须配 `thesis.run(entry_bars=..., claim_on_entry=True)` 一起用 ——
      传错时钟时比对的是完全不相等的两个 ts，表现为「永远不挂单」，
      所以这里把期望的时钟写进 evidence，出问题能一眼看出来。

    ★ ``claim_space`` 必须报**真正**的时钟：以前写死 ``"5m" if on_entry
      else "15m"``，而三引线一旦改到 5m，`claim_on_entry=False` 却仍然
      报 "15m" —— evidence 会说谎，排查时被它带偏。现在按 `bar_space` 报。
    """
    target_ts = int(plan.effective_claim_ts)
    on_entry = bool(plan.claim_on_entry)
    space = str(getattr(plan, "bar_space", "15m") or "15m")
    claim_space = "5m" if on_entry else space

    def claim(i: int, bars: Sequence[Any], book: T.LineBook):
        if i < 0 or i >= len(bars):
            return False, {}
        hit = int(bars[i].ts) == target_ts
        return hit, {"plan_kind": plan.kind, "plan_bar_ts": plan.bar_ts,
                     "plan_claim_ts": target_ts,
                     "claim_space": claim_space}
    return claim


def entry_bars_for(plan: Plan, bars5: Sequence[Any],
                   interval_ms: int = T.BAR_INTERVAL_MS) -> List[Any]:
    """给 `thesis.run` 用的 5m 序列：取覆盖这条计划生命周期的那一段。

    取 ``[plan.bar_ts - interval_ms, plan.bar_ts + N*interval_ms)`` 的窗口——
    前面多留一个桶，是为了让第一条 15m 的 claim 有落点（倒V 的信号根可能
    落在 ``bar_ts`` 之前，因为它是「包含信号的那一桶」）。
    上限直接放到序列末尾即可：状态机到终态就退出，多给的不会被执行。
    """
    lo = int(plan.bar_ts) - int(interval_ms)
    out = [b for b in bars5 or () if int(b.ts) >= lo]
    return out


def _b15_index_for_plan(plan: Plan, bars15: Sequence[Any]) -> int:
    """把 ``plan.bar_index`` 换算成 **15m 序列**的下标。

    ``T.run`` 永远按 15m 推进，而 ``plan.bar_index`` 属于 ``plan.bar_space``
    那条序列。两者相同就直接用；不同就必须按**时间**换算 ——
    直接拿 5m 的下标当 15m 用，起点会跑到很后面（5m 根数是 15m 的 3 倍），
    表现为「这条计划几乎没被推进过」。
    """
    idx = max(0, int(plan.bar_index))
    space = str(getattr(plan, "bar_space", "15m") or "15m")
    if space == "15m" or not bars15:
        return idx
    # 不同序列 → 按 bar_ts 找最后一根「收盘不晚于它」的 15m
    ts = [int(b.ts) for b in bars15]
    i = bisect.bisect_right(ts, int(plan.bar_ts)) - 1
    return max(0, i)


def run_plan(plan: Plan, bars15: Sequence[Any], book: T.LineBook,
             bars5: Optional[Sequence[Any]] = None,
             params: Optional[PendingParams] = None,
             entry_interval_ms: int = 300_000,
             trace: bool = False) -> T.RunResult:
    """把一条计划跑完 —— **双节奏**：形态/失效用 15m，入场执行用 5m。

    这是把 `pending_plan` 和 `thesis.run` 接起来的唯一入口，
    不要在别处自己拼 `run(...)` 的参数：`claim_on_entry` 与
    `entry_bars` 必须和 `plan.claim_on_entry` 一致，配错不会报错，
    只会表现为「一直不挂单」或「挂早了」。
    """
    th = to_thesis(plan, params=params)
    claim = claim_for(plan)
    if plan.claim_on_entry and not bars5:
        # ★ 不许静默空转：倒V 的 claim 比对的是 5m 的 ts，拿到 15m 序列时
        #   那个 ts 永远匹配不上 —— 跑出来的结果是「一条转换都没有」，
        #   看起来像成功，实际上什么都没做。这正是本项目最吃过亏的失败方式。
        raise ValueError(
            "这条计划声明在 5m 上（claim_on_entry=True），但没给 5m 序列。"
            "退化成 15m 时钟会让它永远不挂单（静默空转）—— "
            "请传 bars5，或自己用 claim_for(plan) + T.run 显式指定时钟。")
    # ★ 从计划所在的那一根开始算「这条 thesis 存在」。
    #   不设的话状态机会从第 0 根就开始判失效，于是每条计划都在声明之前
    #   被判成 INVALIDATED（真实数据冒烟里 12/12 全中）。
    #
    #   ★ `bar_index` 属于 `plan.bar_space` 那条序列。`T.run` 永远按 15m
    #     （`bars15`）推进，所以 bar_space 不是 15m 时必须**换算**成 15m 下标，
    #     否则 start_at 会偏（5m 下标远大于 15m 下标 → 从很后面才开始）。
    start_at = _b15_index_for_plan(plan, bars15)
    if bars5 and plan.claim_on_entry:
        return T.run(th, bars15, book, claim=claim,
                     entry_bars=entry_bars_for(plan, bars5),
                     entry_interval_ms=entry_interval_ms,
                     claim_on_entry=True, start_at=start_at, trace=trace)
    if bars5:
        # 形态在 15m 上确认，但确认之后的成交/止损/止盈仍在 5m 上判
        return T.run(th, bars15, book, claim=claim,
                     entry_bars=entry_bars_for(plan, bars5),
                     entry_interval_ms=entry_interval_ms,
                     claim_on_entry=False, start_at=start_at, trace=trace)
    return T.run(th, bars15, book, claim=claim, start_at=start_at, trace=trace)


def guard_preconditions(plan: Optional[Plan], bar: Any, book: T.LineBook,
                        direction: str = DOWN,
                        kind: Optional[str] = None,
                        *,
                        close_ms: Optional[int] = None,
                        bar_space: Optional[str] = None,
                        line_key: Optional[str] = None) -> Tuple[bool, str]:
    """★ 声明预期之前的前置条件检查。

    ★★ 这里修的是一个**极性缺陷**（2026-11-17，用户点出来的）。

    用户原话：「三引线和倒V出现的位置一定是价格远离15m的ema20的位置，
    不存在「一出生就死在 ema 上方」」。

    原来的实现是**只看方向**：做多要求 ``close > ema20@30m``。
    但三条形态的定位是（§23.2）：

        三引线   = 大跌**抄底** → 做多，价格在 ema20 **下方**
        倒V      = 大跌**抄底** → 做多，价格在 ema20 **下方**
        EMA 拒绝 = 反弹**做空** → 做空，价格在 ema20 **下方**

    三条**全都在 ema20 下方**！所以对做多要求「在上方」不是严，是**反了**：
    它把位置对的（下方的 350 条倒V）全拦死，把位置错的（上方的 703 条）放行。
    而且不报错，只体现为「做多形态一条都活不下来」。

    正确判据：**三条形态都要求价格在 ema20 下方**，因为它们的背景都是下跌
    （抄底发生在跌透之后，反弹做空发生在反弹到线被压回之时）。
    上涨时镜像 → 三条都要求价格在线**上方**。

    参数：
        kind: 形态名（``Plan.kind``）。给了就按「基准版/镜像版」推背景方向
            （和 `detect_all`、`_variant_name` 用同一份判据）；
            不给则退回只看 `direction` 的老行为，仅为兼容既有调用点。
        line_key: 用**哪条线**判「价格在哪一侧」。不给 = 老行为
            （`FAST_LINE_KEY` → 回退链 `ema20@15m`）。

            ★★ 2026-11-20 新增，修的是「ema50/ema144 拒绝永远出不来单」：
              EMA 拒绝族现在可以发生在 ema20/ema50/ema144 上
              （用户：「ema50 出现多次拒绝也是同样的」）。而它们的场景恰恰是
              **价格已经突破 ema20、反弹到 ema50** —— 这时 `close > ema20`，
              老守卫按 ``close < ema20`` 判，会把这些单**全部**拒掉。
              实测（121 天）：ema50 有 155 个形态候选、ema144 有 75 个，
              在守卫面前放行 **0** 个。不修这道，改检测器等于白改。

              ★ 修法是把判据锚到**被拒绝的那条线**，而不是放宽判据：
                「价格在被它拒绝的那条线的正确一侧」。三引线 / 倒V
                **不传这个参数** —— 它们的口径一直是「价格远离 15m 的
                ema20」，与这条无关，不能顺手改。

              ★ 老实说清它现在的强度：改完之后这道守卫对 EMA 拒绝族只剩
                **一致性检查**（拒绝根本来就收盘在该线正确一侧，只是线值
                取自计划时刻、不是拒绝根时刻，所以不是恒真）。这一族
                「位置对不对」的真正把关在 `trend_context` 的 regime 判据
                （ema20 vs ema50）。守卫不再承担那件事，也不假装承担。

    Returns:
        ``(放行?, 原因)``。
    """
    if bar is None:
        return False, "no_bar"
    # ★ 查线用的时刻必须是**这条计划的决策时刻**。
    #
    #   2026-11-17 修：以前写死 `bar.ts + BAR_INTERVAL_MS`（15m）。
    #   三引线的计划在 **5m** 上（`bar_space="5m"`），于是守卫会去查
    #   10 分钟之后才就绪的 ema20@30m —— 那是**前视**，用到了决策时
    #   还不存在的信息。实测 5/11 条拿到不同的线值，但裁决翻转 0/11，
    #   所以目前没有造成错误结果；这类东西不会自己变好，只会等一次巧合。
    #
    #   取值优先级：显式 close_ms > plan 自己的 bar_space > 调用方给的
    #   bar_space > kind 的扫描周期。
    if close_ms is not None:
        _close_ms = int(close_ms)
    else:
        _space = str(getattr(plan, "bar_space", "") or "") or bar_space or ""
        if _space not in ("5m", "15m"):
            _space = BAR_SPACE.get(kind, "15m") if kind else "15m"
        _close_ms = int(bar.ts) + _space_ms(_space)
    if line_key:
        # ★ 显式指定的线（EMA 拒绝族的「被拒绝的那条」）。
        #   取不到就拒 —— 不许静默退回 ema20，那正好是这轮修掉的缺陷。
        fast = book.at(line_key, _close_ms)
        if fast is None:
            return False, "no_guard_line:%s" % line_key
        _side_note = line_key
    else:
        fast = book.at(T.FAST_LINE_KEY, _close_ms)
        _side_note = T.FAST_LINE_KEY
        if fast is None:
            # ★★ 2026-11-20 修：`FAST_LINE_KEY` 是 `ema20@30m`，而生产的
            #    `kbar_line` 里**根本没有 @30m 这条线**（28 个 key，只有
            #    ema20/50/144/169/200 的 @5m 与 @15m、donchian、prior_high/low、
            #    range_*、swing_high）。于是 `book.at()` 恒为 None →
            #    这道闸对**每一条候选**都返回 `no_fast_line` → `_build` 全部拒绝。
            #
            #    也就是说：即便窗口、形状、价位全对，生产也**永远出不了计划**
            #    （107 天 0 单的直接原因）。这是一条只能靠「查线拿到 None 就
            #    直接拒」这种写法长期隐藏的死路 —— 拒得越干脆，越没人怀疑。
            #
            #    判据本身用户说的是「价格远离 **15m 的 ema20**」，`ema20@15m`
            #    是存在的（也是 `SLOW_LINE_KEY`）。所以按**存在性**逐级回退：
            #    有 @30m 就用 @30m（保持原设计），没有就用 @15m（用户原话）。
            for _alt in GUARD_LINE_FALLBACKS:
                fast = book.at(_alt, _close_ms)
                if fast is not None:
                    _side_note = _alt
                    break
    if fast is None:
        return False, "no_fast_line"
    close = float(bar.c)
    # ★ 2026-11-17：**去掉「只看方向」的兼容分支**。
    #
    #   那个分支判的是「做多要求价格在线上方、做空要求在线下方」，
    #   而三条形态的定位是「基准版都在 ema 下方」——它把位置对的拦死、
    #   位置错的放行（§23.2 记的极性缺陷）。修法是按 `kind` 推背景方向。
    #
    #   但兼容分支留着就是活隐患：任何调用方忘了传 `kind`，
    #   就会静默退回那个**已知是错**的判据，而且不报错。
    #   所以这里直接要求 kind，不许再退回。
    if kind is None:
        raise ValueError(
            "guard_preconditions 必须传 kind（形态名）。"
            "只看 direction 的旧判据是错的：三条形态的基准版都在 ema **下方**，"
            "按方向判会把位置对的拦死、位置错的放行（§23.2）。")
    # 背景方向：基准版在下跌背景，镜像版在上涨背景。
    # ★ 与 detect_all / _build 共用 `background_of`（唯一来源）。
    bg = DOWN
    for base_kind, spec in PATTERN_SPEC.items():
        if _variant_name(base_kind, direction) == kind:
            bg = background_of(base_kind, direction)
            break
    # ★ 原因串带上**是哪条线**判的。不带的话，ema20 拒绝和 ema50 拒绝的
    #   拒绝原因长得一模一样，统计里分不出「哪条线上的位置不对」——
    #   这正是「同一事实两个来源」那类排查困境。
    _tag = "" if not line_key else "@%s" % line_key
    if bg == DOWN and not (close < float(fast.price)):
        return False, "not_below_ema30m_for_down_background%s" % _tag
    if bg == UP and not (close > float(fast.price)):
        return False, "not_above_ema30m_for_up_background%s" % _tag
    return True, "ok"
