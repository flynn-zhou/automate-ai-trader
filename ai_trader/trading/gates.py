# -*- coding: utf-8 -*-
"""开仓闸门。**目前只有一道：仓位参数。**

为什么重写，而不是修
------------------
原来的 `trading/risk.py::build_plan()` 有六道 gate，看起来齐备。
但实际查下来，生产策略 donchian 走的 `engine.py::_trend_plan()`
**根本不经过 build_plan**，它返回的是 `gates=[]`。
那六道门（止损距离/成本门/可达性/波动门/最小下单/强平距离）
属于剥头皮路径，donchian 一次都不会走。

所以这不是"规则写错了"，是"规则没接在这条线上"。
按决定：全部弃用，从零重写，一道一道加回去。
当前只有第 1 道——仓位参数。其余等策略重新设计后再议。

设计约束
--------
1. **算和查必须是同一个函数。**
   老代码里 `_trend_plan` 自己算 qty，再自己内联判断，
   两段逻辑迟早会漂移；一旦漂移，闸门拦的是它以为的数，
   下单用的是另一个数，闸门就成了装饰。
   本模块**返回算好的 qty/notional**，调用方直接拿去下单，不许重算。

2. **钱以交易所真实权益为准，不以配置的本金为准。**
   实测过：配置写 100U，账户里只有 0.007U。
   用配置的 100U 算，会得出 0.004 个 BTC（名义 309U）的单子，
   然后在只有 7 分钱的账户上被交易所拒掉，或者更糟——在别的账户上真的成交。

3. **风险预算用真实权益算，配置的 4 个参数只做预算口径。**
   两者不一致时，闸门要说出差多少，而不是二选一悄悄用掉。

4. **向下取整，不四舍五入。**
   四舍五入会向上超配：实测拿 1x 名义变成 1.31x，
   现货被执行器按可用余额截断（记账与实际不符），永续直接被拒。

5. **缺数据一律判否。**
   拿不到步长/最小名义额/权益时，宁可不开仓。
   这一条写死在这里，因为"数据没到"和"条件满足"在布尔上长得一样。
"""
from __future__ import annotations

import math


class GateResult(dict):
    """一个闸门的结论。dict 是为了能直接塞进决策日志的 JSON 列。"""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))


def _fail(detail: str, **extra) -> GateResult:
    r = GateResult(gate="仓位参数", ok=False, detail=detail)
    r.update(extra)
    return r


def position_gate(*, equity: float, price: float, leverage: float,
                  qty_step: float, min_qty: float, min_notional: float,
                  max_leverage: float, capital: float, risk_pct: float,
                  margin_cap_pct: float = 40.0, fee_buffer: float = 0.001,
                  plan_qty: float = 0.0, plan_notional: float = 0.0,
                  stop_distance_pct: float = 0.0,
                  max_stop_distance_pct: float = 0.0) -> GateResult:
    """第 1 道闸门：仓位参数。

    输入是「4 个仓位参数 + 交易所硬约束 + 真实权益」，
    输出是「算好的 qty/notional」或「一条明确的拒绝原因」。

    4 个仓位参数（Web 可改，实时生效）：
        capital        账户本金 U
        risk_pct       每笔风险占本金 %
        leverage       交易所杠杆
        margin_cap_pct 保证金占本金上限 %

    交易所硬约束（来自 exchangeInfo，不可配置）：
        qty_step / min_qty / min_notional
    """
    # ---- 0) 缺数据不许硬算 ----
    if price is None or price <= 0:
        return _fail("价格无效（%r），无法计算仓位" % (price,))
    if equity is None or equity <= 0:
        return _fail("权益为 0（交易所钱包没钱），开不出仓")
    if qty_step is None or qty_step <= 0:
        return _fail("数量步长无效（%r），无法取整下单量" % (qty_step,))
    if min_notional is None or min_notional <= 0:
        return _fail("最小名义额无效（%r），无法判断能否下单" % (min_notional,))
    lev = float(leverage or 0)
    if lev <= 0:
        return _fail("杠杆无效（%r）" % (leverage,))
    # 这里**不**直接拿配置杠杆去比上限。
    #
    # 上限约束的是"实际吃进去的杠杆"= 名义/权益，不是"目标杠杆"这个旋钮。
    # 老实现对 spot 永远 force 1x（_plan_leverage 里写死），
    # 如果这里按目标杠杆比，把上限设成 0.996 就会把 1x 现货单直接拒掉 ——
    # 而实际吃进去的恰恰只有 0.996x，根本没超。
    # 所以只在下面第 5 步按【实际杠杆】复核。

    # ---- 1) 目标名义：权益 x 杠杆，再留手续费/滑点缓冲 ----
    #
    # 按【真实权益】而不是配置本金。配置本金只用于下面的预算口径对比。
    raw_notional = equity * lev / (1.0 + float(fee_buffer or 0.0))

    # ---- 2) 保证金上限：不超过本金的 margin_cap_pct ----
    #
    # 这一条是 4 参数里唯一真正"截断仓位"的旋钮。
    # 没配（<=0 或 >=100）时按 100% 处理，等于不额外截断。
    cap_pct = float(margin_cap_pct or 0.0)
    if 0 < cap_pct < 100:
        cap_notional = equity * (cap_pct / 100.0) * lev
        if cap_notional < raw_notional:
            raw_notional = cap_notional

    # ---- 3) 向下取整到步长 ----
    raw_qty = raw_notional / price
    # round(...,9) 先消掉浮点尾巴，再 floor，避免 0.0049999999 被算成 0.004
    steps = math.floor(round(raw_qty / qty_step, 9))
    qty = round(steps * qty_step, 9)
    if qty <= 0:
        return _fail(
            "数量取整后为 0（权益 %.4fU x %.0fx = 名义 %.2fU / 价 %.1f / 步长 %s）"
            % (equity, lev, raw_notional, price, qty_step),
            qty=0.0, notional=0.0)

    notional = qty * price
    margin = notional / lev

    # ---- 4) 交易所最小量 ----
    if min_qty and qty < min_qty:
        return _fail("下单量 %.8f 小于交易所最小量 %.8f" % (qty, min_qty),
                     qty=qty, notional=notional)
    if notional < min_notional:
        # 文案里必须保留"最小下单"四个字：看板和哨兵按文案区分故障原因，
        # 改成"最小名义额"会让既有分流规则全部失配（已有一条测试锁住这个词）。
        return _fail("名义 %.2fU 低于最小下单 %.2fU（交易所约束）" % (notional, min_notional),
                     qty=qty, notional=notional)

    # ---- 5) 杠杆硬上限（取整后复核）----
    real_lev = notional / equity
    if max_leverage and real_lev > max_leverage + 1e-9:
        return _fail("实际杠杆 %.2fx 超过硬上限 %.2fx" % (real_lev, max_leverage),
                     qty=qty, notional=notional)

    # ---- 5.5) 以下是「最后一道闸」：只否决上游算好的计划，不自己定价 ----
    #
    # 为什么要有这一段：闸门曾经自己从零算仓位（名义 = 权益 x 杠杆），
    # 那是把闸门当成了定价器。正确分工是：开仓信号给出止损止盈方式，
    # 上游算出名义/倍数，闸门只负责在最后一步把这些值和硬约束核对一遍。
    #
    # 每条都遵循「有有效入参才校验」：上游没给这一项（0 或 None）就跳过，
    # 而不是拿一个编出来的默认值去拦。策略还没定，止损距离现在给不出来，
    # 所以这条会被跳过 —— 但代码已经在这里了，接上只需要传参。
    if stop_distance_pct and max_stop_distance_pct:
        if stop_distance_pct > max_stop_distance_pct + 1e-9:
            return _fail(
                "止损距离 %.3f%% 超过上限 %.3f%%（爆仓会先于止损触发）"
                % (stop_distance_pct, max_stop_distance_pct),
                qty=qty, notional=notional)

    if plan_qty and plan_qty > 0:
        # 上游给的量必须和闸门复核出来的量一致。不一致就是有一边在撒谎，
        # 与其挑一个信，不如直接拒绝并把两个数都打出来。
        tol = max(qty_step, plan_qty * 1e-6)
        if abs(plan_qty - qty) > tol:
            return _fail("上游计划数量 %.8f 与闸门复核 %.8f 不一致"
                         % (plan_qty, qty), qty=qty, notional=notional)

    if plan_notional and plan_notional > 0 and notional > 0:
        if abs(plan_notional - notional) > max(1.0, plan_notional * 0.01):
            return _fail("上游计划名义 %.2fU 与闸门复核 %.2fU 不一致"
                         % (plan_notional, notional), qty=qty, notional=notional)

    # 保证金上限：占本金 margin_cap_pct%。
    #
    # 基数用【真实权益】而不是配置本金。理由是配置本金是"打算投多少"，
    # 权益是"现在真有多少"。账户涨到 30000U 时按配置的 100U x 100% 卡，
    # 会把每一笔都拦掉（实测既有用例在这里全红）；反过来账户只剩 0.007U 时，
    # 按 100U 卡又会放过远超实际承受力的仓位。
    #
    # 权益拿不到（<=0）时才退回配置本金——那种情况下面第 0 步已经拒了。
    cap_base = float(equity if equity and equity > 0 else (capital or 0.0))
    cap_u = cap_base * float(margin_cap_pct or 100.0) / 100.0
    if cap_u > 0 and margin > cap_u + 1e-9:
        return _fail("占用保证金 %.2fU 超过本金上限 %.2fU（本金 %.0fU x %.0f%%）"
                     % (margin, cap_u, capital, margin_cap_pct),
                     qty=qty, notional=notional)
    # ---- 6) 4 参数预算口径的一致性说明（不拦，只报）----
    #
    # 配置本金和账户真实权益是两回事：前者是你打算投多少，
    # 后者是现在真有多少。不一致本身不是错（刚注资/刚亏损都会不一致），
    # 但如果差得远，说明页面上的 R 和生命数是按一个不存在的账户算的。
    notes = []
    cap_u = float(capital or 0.0)
    if cap_u > 0 and abs(equity - cap_u) / cap_u > 0.02:
        notes.append("配置本金 %.2fU 与真实权益 %.4fU 相差 %.1f%%"
                     % (cap_u, equity, (equity - cap_u) / cap_u * 100.0))
    r_u = math.floor(cap_u * float(risk_pct or 0.0) / 100.0) if cap_u > 0 else 0
    if r_u > 0 and margin > cap_u:
        notes.append("占用保证金 %.2fU 超过配置本金 %.2fU" % (margin, cap_u))

    detail = ("qty=%.6f 名义=%.2fU 保证金=%.2fU 实际杠杆=%.2fx"
              " (权益 %.4fU x 配置杠杆 %.0fx)"
              % (qty, notional, margin, real_lev, equity, lev))
    if r_u > 0:
        detail += " | 每笔风险预算 R=%.0fU" % r_u
    if notes:
        detail += " | " + "; ".join(notes)

    return GateResult(gate="仓位参数", ok=True, detail=detail,
                      qty=qty, notional=notional, margin=margin,
                      leverage=real_lev, notes=notes)