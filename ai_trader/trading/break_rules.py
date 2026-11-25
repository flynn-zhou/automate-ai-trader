# -*- coding: utf-8 -*-
"""收盘跌破 vs 有效跌破 —— 两个概念的唯一实现。

用户定的口径（2026-11-25，原文）：
    「收盘跌破就是在5m的买点，收盘5m跌破位置，本来预期收针的，
      但是现实是收盘跌破了，不是引针了；有效跌破是后面3根K线有过
      确认动作，也就是从收盘跌破那根K线之后，不包含收盘跌破那根，
      第三根还是跌破！」

翻译成交易语义（剥头皮视角）：

  * **收盘跌破** = 入场前提被否，但**还没被证伪**。
    这笔仓赌的是「支撑位被探一下就被买盘接住（收针）」。如果一根 5m
    **收盘价**落在买点下方，那根 K 线就不是针了，前提当场不成立。
    注意必须是**收盘**：盘中插下去又收回来，正是我们赌的东西，
    拿盘中价当跌破等于自己否决自己的前提。

    但「前提被否」不等于「市价砸掉」。认错的那一刻价格就在买点附近——
    那是全场最好的一个价。把它市价砸给做市商，是新手才会做的事。
    交易员的做法是：**认错，但挑价认** —— 盯住买点的回抽位，一回来就走。

  * **有效跌破** = 认错窗口到期。
    以跌破那根为锚、不含该根，第 3 根**仍**收在下方 → 确认这不是扫盘。
    到这一步就不能再挑了：市场已经用 3 根 K 线告诉你方向，继续等回抽
    是赌运气。无条件市价出。

两者不是两套规则，而是**同一条时间轴的两端**：
    收盘跌破 = 开钟（挂回抽位等一个好价）
    有效跌破 = 关钟（不再等，市价走）

为什么回抽位要挂在 `L − 0.1 ATR` 而不是正的 `L`：
    生产实测 2026-11-25，买点 58437，22:20 那根 5m 最高 **58435.00** ——
    差 2 个点没碰到。挂在正买点的单子不会成交，然后一路跌到 57320。
    往前站一步（0.1 ATR ≈ 40 点）成交概率高得多，代价只是少卖 40 点。
    这是从真实盘面学来的，不是拍的。

本模块是纯函数：只吃「已收盘的 K 线 + 一个价位」，不碰 IO、不碰状态。
因此它的状态可以随时从 K 线重算 —— 重启、重放、补算都得到同一个答案，
不会像挂单那样产生「进程挂了就没人管」的孤儿。
"""
from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

#: 触发缓冲（ATR 倍数）：收盘要**明显**越过关键位才算跌破。
#:
#: 0.05 ATR 这个量级是从两边夹出来的：
#:
#:   · 下限（不能太小）：实测 2026-11-25 之外，`test_run_plan_fills_the_
#:     inverse_v_on_a_later_5m_bar` 里成交那根收盘只比触发价高 **2 点**，
#:     对 BTC 是纯噪声。用 0 当缓冲，任何一次"带着零点几个点收在另一侧"
#:     都会被判成前提被否，策略被反复砍在噪声上 —— 那是管状止损之外
#:     第二种"死在噪音里"的方式。
#:   · 上限（不能太大）：事故那根收 58415.70，比买点低 21.3 点
#:     （≈0.09 ATR）必须能触发。
#:
#: ★ 为什么现在敢用 ATR 当缓冲（第一版不敢）：
#:   因为"武装状态"改成了**由收复解除**，不再由"没继续跌破"解除。
#:   缓冲变大只会推迟"哪一根第一次算跌破"，不会把已经开钟的窗口关掉 ——
#:   21:35 那根收 58436（只低 1 点）不触发新锚点，但它也没收复
#:   （收复要求收盘站回 58437 上方），所以窗口照样开着，
#:   「反弹回来就该离场」照样能在那一根成交。
NOISE_ATR = 0.05

#: 回抽位缓冲（ATR 倍数）。与 `kline_intent.BREAK_BUFFER_ATR` 同口径。
#:
#: 这个缓冲**只影响出场价**，不影响判读。为什么要往前站一步：
#: 生产实测 2026-11-25 买点 58437，22:20 那根 5m 最高 **58435.00** ——
#: 差 2 个点没碰到；挂在正买点的单不会成交，然后一路跌到 57320。
BOUNCE_ATR = 0.10

#: 有效跌破的确认根数：锚点之后第 3 根仍跌破。
#: 用户原话「不含收盘跌破那根，第三根还是跌破」。
CONFIRM_BARS = 3

#: 状态
INTACT = "INTACT"            # 关键位没被收盘破掉 → 前提完好，正常持有
CLOSED_BELOW = "CLOSED_BELOW"  # 已收盘跌破，但未满 3 根 → 认错窗口打开
CONFIRMED = "CONFIRMED"      # 锚点后第 3 根仍跌破 → 有效跌破，市价走


class BreakRead(NamedTuple):
    """一次判读的完整证据。字段都在，才能回答「为什么走/为什么不走」。"""

    state: str
    level: float            # 关键位（买点）
    trigger: float          # 收盘价必须低于它才算跌破（含噪声缓冲）
    exit_level: float       # 回抽离场位（比 level 往前站一步，保证成交）
    anchor_ts: int          # 锚点那根（第一次收盘跌破）的 ts
    anchor_close: float     # 锚点那根的收盘价
    bars_since: int         # 锚点之后已收盘的根数（0 = 就是锚点本身）
    atr: float
    direction: str          # "long" / "short"

    @property
    def broken(self) -> bool:
        return self.state in (CLOSED_BELOW, CONFIRMED)

    @property
    def confirmed(self) -> bool:
        return self.state == CONFIRMED

    def describe(self) -> str:
        if self.state == INTACT:
            return "关键位 %.2f 未被收盘跌破" % self.level
        if self.state == CLOSED_BELOW:
            return ("收盘跌破 %.2f（锚点收 %.2f，已过 %d/%d 根）"
                    "→ 回抽 %.2f 即离场，等不到就第 %d 根后市价走"
                    % (self.level, self.anchor_close, self.bars_since,
                       CONFIRM_BARS, self.exit_level, CONFIRM_BARS))
        return ("有效跌破 %.2f（锚点收 %.2f，已过 %d 根仍跌破）→ 不等回抽，市价离场"
                % (self.level, self.anchor_close, self.bars_since))


def _row(b):
    """把一根 K 线读成 (ts, high, low, close)。

    兼容三种形状：tuple/list、带属性的对象、dict。行情池、交易所返回、
    状态机的 bar 三处形状都不一样，这里收口一次，别让调用方各自适配。
    """
    if isinstance(b, dict):
        return (int(b.get("ts") or b.get("t") or 0), float(b.get("h") or 0.0),
                float(b.get("l") or 0.0), float(b.get("c") or 0.0))
    if hasattr(b, "c"):
        return (int(getattr(b, "ts", 0) or 0), float(getattr(b, "h", 0.0) or 0.0),
                float(getattr(b, "l", 0.0) or 0.0), float(getattr(b, "c", 0.0) or 0.0))
    # 交易所 kline 数组：[openTime, o, h, l, c, volume, ...]
    return (int(b[0]), float(b[2]), float(b[3]), float(b[4]))


def _atr_rows(rs: Sequence) -> float:
    """平均真实波幅（已经归一成 4 元组的行）。

    只用来定缓冲大小，不需要精确到小数点。
    """
    if len(rs) < 2:
        return 0.0
    trs = []
    for i in range(1, len(rs)):
        _, h, l, _ = rs[i]
        pc = rs[i - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def atr_of(bars: Sequence, n: int = 14) -> float:
    """平均真实波幅。接受任意 K 线形状（内部归一）。"""
    rs = [_row(b) for b in bars][-max(2, int(n)):]
    return _atr_rows(rs)


def assess(level: float, bars: Sequence, tick: float = 0.0,
           atr: Optional[float] = None, since_ts: int = 0,
           direction: str = "long") -> BreakRead:
    """判读关键位 `level` 当前处于哪种跌破状态。

    参数
    ----
    level : 关键位（多头是买点，空头是卖点）
    bars  : **已收盘**的 K 线序列（最后一根是最新收盘的那根）
    tick  : 最小变动价位；缓冲至少 2 个跳，避免用浮点噪声做交易决定
    atr   : 不传就按 bars 现算
    since_ts : 只看这个时间**起**的 K 线。★ 必须是"成交所在那根的
               开盘时刻"（bar open time），不是成交的精确毫秒 ——
               2026-11-25 那笔 21:31:05 成交，它所在那根的 ts 是 21:30。
               传精确毫秒会把这一根整个过滤掉，于是判读从下一根才开始，
               锚点永久晚一根，事故里那根放量清算 K 就看不见了。
    direction : "long" 破下方算跌破；"short" 破上方算跌破（镜像）

    返回 `BreakRead`。纯函数，同样的输入永远得到同样的输出。
    """
    level = float(level or 0.0)
    d = (1 if str(direction).lower() == "long" else -1)
    rs_all = [_row(b) for b in bars]
    # ★ ATR 用**整段**（最近 14 根）算，绝不能用"入场之后那几根"算。
    #   ATR 是市场属性，不随我们什么时候进场而变。用 since_ts 之后的
    #   两三根估 ATR，值会随窗口剧烈跳（实测 234 → 461），
    #   于是缓冲跟着跳、同一根 K 线的判读结果取决于我们哪天进场 ——
    #   这种"看起来稳定其实随机"的判据比没有判据更危险。
    a_est = _atr_rows(rs_all[-14:]) if len(rs_all) >= 2 else 0.0
    rs = [r for r in rs_all if r[0] >= int(since_ts)] if since_ts else rs_all
    if level <= 0 or not rs:
        # state, level, trigger, exit_level, anchor_ts, anchor_close,
        # bars_since, atr, direction —— 九个字段一个都不能少。
        return BreakRead(INTACT, level, level, level, 0, 0.0, 0, a_est,
                         str(direction))

    a = float(atr) if atr else a_est
    d1 = max(2.0 * float(tick or 0.0), NOISE_ATR * a)
    d2 = max(2.0 * float(tick or 0.0), BOUNCE_ATR * a)
    # 多头：收在 level-d1 之下才算跌破；空头镜像
    trigger = level - d1 * d if d > 0 else level + d1
    exit_level = level - d2 * d if d > 0 else level + d2
    # 收复线：收在 level+d1 的另一侧才算"收复"。
    # ★ 触发线和收复线必须留一条缝（±d1），不能共用一个阈值 ——
    #   共用会让"刚好等于买点"的收盘价在两种状态之间抖动，
    #   而 2026-11-25 那根恰好就收在买点下方 1 点。
    reclaim_level = level + d1 * d if d > 0 else level - d1

    def _broke(close: float) -> bool:
        return (close < trigger) if d > 0 else (close > trigger)

    def _reclaimed(close: float) -> bool:
        return (close > reclaim_level) if d > 0 else (close < reclaim_level)

    # ★★ 锚点一旦成立，就**保持武装直到收盘收复**为止。
    #
    #   这里以前写成"找最近的连续跌破段"，即中间一根收回到触发线上方
    #   就把整段作废。那是错的：2026-11-25 的 21:35 收 58436.00，
    #   只比买点低 1 点，但它在结构上仍然是"没有收复"（买盘没能把价格
    #   顶回 58437 上方）。按"连续段"逻辑那一刻会翻回 INTACT，
    #   回抽离场意图被解除 —— 而这恰恰是用户说的「反弹回来就该离场」
    #   的那一根。武装状态必须由**收复**解除，不能由"没继续跌"解除。
    anchor = None
    for i in range(len(rs)):
        if anchor is None:
            if _broke(rs[i][3]):
                anchor = i
        elif _reclaimed(rs[i][3]):
            # 收盘重新站回关键位上方 → 那个"跌破"是假警报，前提恢复
            anchor = None
    if anchor is None:
        return BreakRead(INTACT, level, trigger, exit_level, 0, 0.0, 0, a,
                         str(direction))

    since = len(rs) - 1 - anchor
    state = CONFIRMED if since >= CONFIRM_BARS else CLOSED_BELOW
    return BreakRead(state, level, trigger, exit_level,
                     int(rs[anchor][0]), float(rs[anchor][3]), int(since), a,
                     str(direction))


def exit_now(read: BreakRead, price: float, tick: float = 0.0) -> tuple:
    """该不该现在离场？返回 (bool, 理由)。

    这是把上面的"状态"变成"动作"的**唯一**地方：

      * 有效跌破 → 立刻市价走（不再等回抽）
      * 收盘跌破 → 只在价格回到/好于回抽位时走（挑一个好价认错）
      * 未跌破   → 不动

    为什么"收盘跌破"不立刻走：那一刻价格就在关键位附近，已经是当下
    最好的价；但如果是**深跌破**（价格已经跌到很低），立刻市价卖出
    等于把最差的价成交在自己手上。等回抽是给市场一次修正的机会，
    而"有效跌破"就是这次机会的截止时间。
    """
    px = float(price or 0.0)
    if px <= 0:
        return False, ""
    if read.state == CONFIRMED:
        return True, ("有效跌破：%s" % read.describe())
    if read.state != CLOSED_BELOW:
        return False, ""
    d = 1 if read.direction == "long" else -1
    reach = (px >= read.exit_level) if d > 0 else (px <= read.exit_level)
    if reach:
        return True, ("收盘跌破后回抽到位：%s" % read.describe())
    return False, ""


__all__ = ["INTACT", "CLOSED_BELOW", "CONFIRMED", "CONFIRM_BARS", "BreakRead",
           "assess", "exit_now", "atr_of"]