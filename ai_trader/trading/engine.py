"""决策引擎：每根 3s 桶收盘跑一次，把「看行情 -> 判断 -> 开/管/平」全过程留痕。

三条铁律：
1. 决策只用已收盘 bar（feature_time 显式写入，且断言 <= 决策时间），杜绝前视。
2. 每根 bar 都留一行决策记录，包括"什么都没做"及其原因。
3. 仓位只由风险预算决定；杠杆、强平距离是校验项，不是目标。
"""
from __future__ import annotations

import asyncio
import bisect
import hashlib
import logging
import os
import time
from typing import Optional

from ..trade_db import TradeDB, ADOPT_REASON, reconcile_orders
from . import features as feat_mod
from . import signals as sig_mod
from .events import (GATE_MARGIN, GATE_SIZING, GATE_UNATTENDED, EntryIntent,
                     GateResult)
from .order_router import OrderRouter
from .risk import RiskState, build_plan, check_risk_state, roll_day

logger = logging.getLogger(__name__)

# ADOPT_REASON 的【定义】在 trade_db.py —— 它同时被 stats() 用来把这类仓
# 排除出策略战绩，两个消费方必须共用同一个字面量。
#
# 它标记的是「这笔仓不是策略开的」：
#   reconcile_live() 每次发现「交易所上有仓、数据库里没有」，就把它【接管】
#   进 self.pos。来历可能是：
#     1) 用户在手机 App 上手工开的
#     2) 下单超时但其实成交了、回报丢了
#     3) 上次进程崩在 log_trade 之前
#
# 第 1 类没有策略的出场依据——用户为什么在那个价位开仓、打算拿到哪里、
# 能容忍多少回撤，系统一概不知道。拿唐奇安的「跌破 75 根新低」去平掉一笔
# 手工仓，是把两套完全不同的决策混成一套。
#
# 生产事故（11-12）：用户 21:09 手工买入 0.075 BTC，21:12 被接管，
# 22:00 引擎按通道出场平掉（本金 292U 的账户，那一笔真实净额 -3.48U）。
# 用户的原话是「我们还没有策略呢，是什么在干预我的单子」。
#
# 所以：接管来的仓【不判策略出场】，但【保留熔断止损】。
# 判定入口是 _is_adopted()，判据只能读 open_reason（内存布尔活不过重启）。

#: 我们自己的单 `clientOrderId` 的固定前缀（见 live.py 的 _open/_close）。
#: 判定「这笔仓是不是我们开的」只用这一个字符串，不要在别处另写一份。
AI_CLIENT_PREFIX = "AI-"

#: 挂单（pending）策略 `clientOrderId` 的前缀。
#: 与 `AI_CLIENT_PREFIX` 分开：看板和成交归因要能一眼区分「趋势/剥头皮的单」
#: 和「挂单策略的单」，而且挂单还多一条撤单路径，前缀必须可识别。
PENDING_CLIENT_PREFIX = "AI-pend-"

#: 挂单策略**自己开的**仓的 `open_reason`。
#:
#: 它与 `trade_db.ADOPT_REASON`（`实盘对账接管`，用户手工仓）互斥，
#: 是回答「这笔仓归谁管」的唯一判据：
#:   · 写：`_pending_book_fill` 成交落账时写这个值；
#:   · 读：`_pending_close` 只肯平这个值的仓。
#: 故意做成常量而不是两处字面量 —— 2026-11-24 的误平就是因为
#: 「该不该平」没有任何归属判据，只有「引擎手里有没有仓」。
PENDING_FILL_REASON = "挂单成交"

#: 交易所里被认作「已成交」的状态（含部分成交）。
#: 与 `PendingOrder.status` 的原生值对齐，不做归一化 —— 归一化会把
#: 「部分成交」压成「成交」，`_pending_book_fill` 就会按全额落账。
_FILLED_STATES = ("FILLED", "PARTIALLY_FILLED")

#: 人工仓在交易所侧那张保护性止损单的 clientOrderId 前缀。
#:
#: ★ A1（2026-11-25 交易域审查）：这张单是**进程外的第二道保护**。
#:
#:   在这之前，一笔 22 倍杠杆的持仓，全部保护只有进程内每 3 秒一次的
#:   轮询：进程挂了 / 服务器重启 / 网络断 / 行情源卡住 —— 仓位在交易所
#:   侧就是裸的。1.6% 是止损位、4% 就直接爆仓。
#:   而 `place_order` 本来就支持 `stop_price` + `reduce_only` +
#:   `working_type`，且有单测覆盖，缺的只是"有人去挂"。
#:
#:   用 `<前缀><entry_ts>` 而不是随机 id，是为了让**重启后认得出同一张单**：
#:   引擎重启时内存里没有 order id，但 clientOrderId 是稳定的，
#:   查一次就能把已有的那张单接管过来，而不是再挂一张。
ADOPTED_STOP_CID_PREFIX = "AI-SL-"


def pending_client_id(thesis_id) -> str:
    """挂单/撤单**共用**的 clientOrderId。两处必须调同一个函数。

    ★ 为什么不能再用 thesis_id 截断（2026-11-24 生产事故，已实测）：

      币安要求 `clientOrderId` **< 36 字符**（是「小于」，不是「小于等于」）。
      thesis_id 的形状是 ``形态|方向|ts|价``，例如

          下跌三引线假突破限价单|up|1795481700000|61178.1000

      47 个字符 / 69 字节，且含中文。原来的写法
      ``("AI-pend-%s" % tid)[:36]`` 截出来**正好 36 字符 = 58 字节**，
      两边都不满足，于是**每一张挂单都被交易所拒**：

          HTTP 400 {"code":-4015,
                    "msg":"Client order id length should be less than 36 chars"}

      实测后果：orders 表里 `AI-pend-` 开头的记录**一条都没有** ——
      挂单策略上线至今**从来没成功挂出过任何一张单**。而由于失败不阻断
      状态推进（见 `_pending_place`），状态机照样把 thesis 记成 ARMED，
      然后在历史 K 线上推出「成交 + 出场」，最后拿市价单砸在真实持仓上。

    ★ 为什么用定长哈希而不是「截更短」：

      · 全 ASCII、长度固定（8 + 16 = 24 字符），不随形态名长度/中文/价格位数变化；
      · 对同一个 thesis_id 稳定 —— 幂等重放和撤单都靠这一点（撤单必须
        能算出与挂单**逐字相同**的 id）；
      · 日志里仍带完整 thesis_id，认人不受影响。

    ★ 不做「保留可读前缀」的折中：中文形态名一进来长度就不可控，
      而这类「看起来能读、实际会超长」的写法才是这次踩坑的原因。
    """
    key = hashlib.sha1(str(thesis_id).encode("utf-8")).hexdigest()[:16]
    cid = "%s%s" % (PENDING_CLIENT_PREFIX, key)
    # 把交易所的硬约束变成代码里的断言：以后谁改这个函数，长度错了当场红。
    if len(cid) > 35 or not cid.isascii():
        raise ValueError("clientOrderId 不合法（%r，%d 字符）"
                         % (cid, len(cid)))
    return cid


def _close_client_id(kind: str, key: str) -> str:
    """平仓/止损市价单的 clientOrderId：`AI-<kind>-<12 位哈希>`。

    ★ 2026-11-25 为什么必须给市价单也带上（真钱自检单查出来）：
      币安 `/fapi/v1/userTrades` 不返回 clientOrderId，成交归属只能靠
      `orderId → clientOrderId` 反查；而下单时若不下发
      `newClientOrderId`，交易所会自己生成一个随机串（实测
      `8sgedyZgQSvDXiO9Sn1luN`），反查回来也不带 `AI-` 前缀
      → 我们自己砸出去的市价单成交被判成**用户手工仓**。

      方向比"认不出"更糟：市价单正是平仓和止损。一笔止损成交被当人工仓
      接管之后，§13.2 规定接管仓不判策略出场 —— 等于止损失效。

    ★ 长度：`AI-` + kind + `-` + 12 = 至多 20 字符，远低于币安 36 上限。
      key 用哈希而不是原文：thesis_id 里有中文和竖线，原文必然超长
      （见 `pending_client_id` 那段 11-24 事故说明）。
    """
    key_h = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:12]
    cid = "AI-%s-%s" % (kind, key_h)
    if len(cid) > 35 or not cid.isascii():
        raise ValueError("clientOrderId 不合法（%r，%d 字符）"
                         % (cid, len(cid)))
    return cid

#: 归类结论：用户在手机 App 上手工开的仓（§13.2：接管、不许硬停）。
PROV_MANUAL = "manual"
#: 归类结论：我们自己的单，成交回报丢了（真故障，必须硬停）。
PROV_OURS = "ours"
#: 归类结论：查不到 / 凑不出差额 / 执行器不支持（保守方向：照旧硬停）。
PROV_UNKNOWN = "unknown"

#: 判「差额能不能被这些成交解释」的容差（BTC 那种 8 位小数够用）。
_DELTA_EPS = 1e-9


def _fmt_ms(ms) -> str:
    """毫秒时间戳 → ``MM-DD HH:MM``（UTC）。只是日志/通知里的人读格式。"""
    try:
        return time.strftime("%m-%d %H:%M", time.gmtime(int(ms) / 1000))
    except (TypeError, ValueError):
        return "?"


def fills_explaining_delta(fills, delta: float):
    """从**最近**成交往回累加，找出解释「错配差额」的那组成交。

    为什么解释的是**差额**（``交易所 - 数据库``）而不是整笔持仓：
    系统自己正常开的仓也在成交里躺着。拿整笔持仓去解释，必然把
    我们自己的 ``AI-`` 成交一起卷进来，于是「里面有我们的单」永远成立，
    人工仓一次也放不出去 —— 这个 bug 会表现得像「改了没生效」。

    返回 ``(used, ok)``：``used`` 是被用到的成交（最近优先），
    ``ok=False`` 表示凑不出正好等于 delta 的后缀（成交不够 7 天、
    或者差额被一笔成交切开），调用方必须当作未知，不能猜。
    """
    need = float(delta)
    if abs(need) < _DELTA_EPS:
        return [], False
    used = []
    acc = 0.0
    for f in fills:                      # 已经按时间倒序
        side = str(f.get("side") or "").upper()
        qty = float(f.get("qty") or 0.0)
        if qty <= 0:
            continue
        acc += qty if side == "BUY" else -qty
        used.append(f)
        if abs(acc - need) < _DELTA_EPS:
            return used, True
    return used, False


def norm_side(side) -> str:
    """把 side 规范化成 ``"long"`` / ``"short"``（认不出时返回小写原值）。

    ★ 为什么必须有（2026-11-24）：
      这个项目里 `side` 存在三种写法 —— 内存/数据库用 ``long``/``short``
      （`plan.side`），下单接口用 ``buy``/``sell``，交易所回报用大写
      ``BUY``/``SELL``。而对账里有一句
      ``_own * (1 if side == "long" else -1)``：任何不等于 ``"long"``
      的写法都会被当成**空头** —— 一笔 ``"buy"`` 的多仓会被算成负数，
      差额符号直接反掉，后面的接管/硬停判断全部跟着错。
      对账是全系统唯一敢说「我到底有多少仓」的地方，符号错一次就全错。

    ★ 放在模块级而不是做成方法：它是纯函数，挂单成交落账那条链上的
      测试替身（SimpleNamespace）也能直接用，不必逐个桩去 `_bind`。
    """
    s = str(side or "").strip().lower()
    if s in ("long", "buy", "b"):
        return "long"
    if s in ("short", "sell", "s"):
        return "short"
    return s


def close_booking(pos: dict, exit_price: float, close_fee: float,
                  qty: float, side: str):
    """平仓记账的**唯一口径**：返回 ``(毛盈亏, 手续费合计, 净盈亏)``。

    ★ 为什么必须只有一个出处（2026-11-24 用户要求「统一口径」）：

      改之前三条平仓路径各写一套，同一笔仓会算出三个 pnl_net：

        `_close`（策略仓）       fee = 开仓费 + 平仓费   net = 毛 - fee   ← 正确
        `_adopted_close`（人工仓） fee = 平仓费          net = 毛 - fee   ← 漏开仓费
        `_pending_close`（挂单仓） fee = 0.0            net = 毛         ← 两边都漏

      而漏记成本的方向是**单向**的：永远让亏损看起来更小、盈利看起来更大，
      也就是让策略显得更赚钱。这会直接毁掉纸面样本的证据价值。
      2026-11-02 已经为这个方向吃过一次亏（线上 4 笔剥头皮仓、缺口 7.77U，
      逐笔核对每笔少的钱恰好等于它的开仓手续费）。

      所以把公式收进一个纯函数，三条路都调它 —— 以后不可能再分家。

    ★ `pos["fee"]` 是**开仓**手续费，必须在开仓落账时真实写入；
      写 0 的话这个函数算出来仍然偏乐观（`_pending_book_fill` 以前正是写 0）。

    ★ 手续费一律取绝对值后相加：开/平两侧的 `commission` 符号约定不同
      （有的返回负数表示扣款），比较净盈亏时只关心总额，方向由 gross 表达。
    """
    entry = float(pos.get("entry") or 0.0)
    direction = 1 if norm_side(side or pos.get("side")) == "long" else -1
    gross = (float(exit_price) - entry) * float(qty) * direction
    fee_total = (abs(float(pos.get("fee") or 0.0))
                 + abs(float(close_fee or 0.0)))
    return gross, fee_total, gross - fee_total


def classify_delta(fills, delta: float):
    """把错配差额归成 manual / ours / unknown，返回 ``(结论, 用到的成交)``。

    ★ 判据只有一条：解释差额的成交里有没有 ``AI-`` 前缀。

      * 全是系统外的（用户手单）→ manual：§13.2 约定，接管但**不硬停**
      * 只要有一笔是我们自己的 → ours：我们发过单却不知道成交了，必须硬停
      * 凑不出差额 → unknown：宁可停，也不猜

    ★ `client_id` 三态（2026-11-25 真钱自检单查出来的）：

      ``"AI-…"``  我们的单        → ours
      ``""``      交易所明说是手单 → manual
      ``None``    **我们不知道**   → unknown（硬停）

      原先只有两态，而 `/fapi/v1/userTrades` 根本不返回 `clientOrderId`，
      于是「我们的单」长期落在 ``""`` 这一支上，被当成人手单接管 ——
      更要命的是「成交回报丢了」这种必须硬停的真故障因此永不硬停。

      所以三态必须分开：**不知道 ≠ 不是我们的**。方向只能选保守那一边，
      因为误判成 ours 的代价是多停一次（人来解除就行），
      误判成 manual 的代价是我们不知道自己的敞口还在不在。
    """
    used, ok = fills_explaining_delta(fills, delta)
    if not ok:
        return PROV_UNKNOWN, used
    saw_unknown = False
    for f in used:
        cid = f.get("client_id")
        if cid is None:
            saw_unknown = True
        elif str(cid).startswith(AI_CLIENT_PREFIX):
            return PROV_OURS, used
    if saw_unknown:
        return PROV_UNKNOWN, used
    return PROV_MANUAL, used


async def pending_own_filled_qty(ex, thesis_id) -> Optional[float]:
    """问交易所：**我这张单**成交了多少？返回已成交数量，未成交/查不到返回 None。

    ★ 为什么不能用「账户里有仓」当成交证据：账户级净持仓分不清来源。
      2026-11-24：用户手工仓 0.032 BTC 被当成了挂单策略的成交，
      一路推成「观察线拒绝」再用市价平掉。

    ★ 为什么不能用价格推断：限价买单的成交价可以低于挂单价，
      手工仓 entry=60880 低于挂单价 61178.10，价格完全"合法"，
      所以「买价不高于限价」这条规则会放行它。价格对不证明归属对。

    ★ 查不到（None）按**不是我的**处理：宁可漏记一笔（留一条
      `pending_fill_not_ours` 风险事件，人能看到），也不能把别人的仓
      记成自己的 —— 后者会让 `_pending_close` 得到一个错误的"归属权"，
      进而把用户的仓卖掉。方向必须选保守的那一边。
    """
    q = getattr(ex, "query_order", None)
    if q is None:
        # 执行器不支持查单 → 无法证明归属。挂单策略在 live 才需要它，
        # paper/测试桩可能没有；此时按「无法证明即不接管」处理。
        logger.warning("[挂单] %s 执行器不支持 query_order，无法确认成交归属",
                       thesis_id)
        return None
    try:
        od = await q(client_id=pending_client_id(thesis_id))
    except Exception as e:
        logger.error("[挂单] %s 查单失败：%s", thesis_id, e)
        return None
    if od is None:
        return None
    if str(getattr(od, "status", "") or "").upper() not in _FILLED_STATES:
        logger.warning("[挂单] %s 状态机判定成交，但交易所这张单状态是 %r",
                       thesis_id, getattr(od, "status", ""))
        return None
    got = abs(float(getattr(od, "executed_qty", 0.0) or 0.0))
    return got if got > 0 else None


async def pending_own_fill_fee(ex, thesis_id) -> float:
    """问交易所：**我们这张单**开仓时实际付了多少手续费。拿不到返回 0。

    ★ 为什么必须单独查一次（2026-11-24 统一口径）：
      `PendingOrder`（`/fapi/v1/order`）**不带手续费字段**，只有
      `executedQty` / `avgPrice`。真实 commission 只出现在
      `/fapi/v1/userTrades`，而那个接口需要 `orderId`。
      所以挂单成交落账这条链长期写死 `fee: 0.0` —— 整个开仓成本被漏掉，
      净盈亏系统性偏乐观（方向和「让策略显得更赚钱」一致，最危险的那种）。

    ★ maker / taker 不能用一个费率代替：用户手工挂的限价单成交是 maker
      （永续 0.02%），我们自己的市价平仓是 taker（0.05%），差 2.5 倍。
      同一笔持仓开、平两侧费率不同，估算必然错，所以必须查真实值。

    ★ 查不到返回 0，由调用方退回「按 taker 估算」——
      保守方向是多算成本，不是少算。
    """
    q = getattr(ex, "query_order", None)
    if q is None:
        return 0.0
    try:
        od = await q(client_id=pending_client_id(thesis_id))
    except Exception as e:
        logger.warning("[挂单] %s 查开仓手续费失败（查单）：%s", thesis_id, e)
        return 0.0
    if od is None:
        return 0.0
    oid = str(getattr(od, "order_id", "") or "")
    fee_fn = getattr(ex, "_commission_of", None)
    if not oid or fee_fn is None:
        return 0.0
    try:
        fee = abs(float(await fee_fn(oid) or 0.0))
    except Exception as e:
        logger.warning("[挂单] %s 查开仓手续费失败：%s", thesis_id, e)
        return 0.0
    return fee


class DecisionEngine:
    def __init__(self, cfg, feed, executor, tdb: TradeDB, guard=None,
                 settings=None, pending_store=None, pending_lines=None,
                 pending_dry_run=None, pending_kbar=None,
                 pending_source=None):
        self.cfg = cfg
        # 4 个仓位参数（本金/风险%/杠杆/保证金上限）的唯一来源是 Web 配置库。
        # 不传就当没有：宁可按"参数缺失"拒绝开仓，也不按硬编码默认值下单。
        self.settings = settings
        # ---- 挂单策略（strategy="pending"）的三个依赖 ----
        #: 预期（thesis）的持久化仓库。没有它就**不能**跑 pending：
        #: 状态机每根 5m 往前推一次，两次推之间进程可能重启，
        #: 局部变量活不过重启，而交易所那头挂着的单还活着。
        self.pending = pending_store
        #: 返回当前 `LineBook` 的可调用对象（ema20 等线值来自 kbar 库）。
        #: 不注入时用空 LineBook：reversal 家族不需要线，
        #: trend 家族的失效判定会因此恒不成立 —— 见 `_step_pending` 里的告警。
        self.pending_lines = pending_lines
        #: ★ S5 现实跟随出场的 K 线演化视图（`ts -> {label, side, vol_rel}`）。
        #:
        #: 它必须被**注入**，不能在引擎里直接读 `kbar.db`：
        #: 回放和实盘必须走同一条取值路径，否则回测里闸会响、实盘里不会，
        #: 而两边都不报错。
        self.pending_kbar = pending_kbar
        #: kbar 没接线的告警只报一次（道理同 `_pending_warn_once`）。
        self._pending_kbar_warned = False
        #: ★ 计划源：`(params) -> (plans, stats)`。没它就**永远不会有新计划**。
        #:
        #: 引擎本身只推进**已存在**的预期。没有计划源，
        #: 它会完美地管理一个空集合 —— 日志正常、看板正常、
        #: 永远不下单，而“没信号”和“没源头”在事后一样。
        self.pending_source = pending_source
        self._pending_source_warned = False
        #: 上次采集是哪一根 15m（避免每根 5m 都扫一遍全库）。
        self._pending_harvest_ts = 0
        #: 「只出计划不挂单」。None = 按配置取；显式 True/False 优先（测试用）。
        self._pending_dry = pending_dry_run
        #: 上次推进到哪一根 5m（毫秒开盘时刻）。0 = 还没推过。
        self._pending_5m_ts = 0
        #: pending 未接线的告警只报一次，避免每根 3s K 线刷屏。
        self._pending_warned = False
        self.tcfg = cfg.trading
        self.feed = feed
        self.ex = executor
        self.db = tdb
        self.guard = guard
        self.risk = RiskState()
        self.pos: Optional[dict] = None
        # ★ 接管来的人工仓，与系统仓**分开记账**（2026-11-24，用户要求）。
        #
        #   币安永续一个品种只有一个**净持仓**，交易所分不清这手是谁开的。
        #   所以「谁的仓」这件事只能由引擎自己拆开记：
        #       self.pos         = 系统仓（挂单成交 / 趋势 / 剥头皮开的）
        #       self.adopted_pos = 接管来的人工仓（open_reason=ADOPT_REASON）
        #
        #   为什么必须拆（而不是合成一个仓位）：
        #     用户 2026-11-24 明确要求「已存在的单子是系统单子，属于加仓行为，
        #     当然要执行」。系统单成交 = 加仓，但**不能把人工那部分吞掉**。
        #     合并成一个 dict 时，成交落账会拿账户净量覆盖整笔仓
        #     （`_pending_book_fill` 旧写法正是这样），人工仓的来历、
        #     入场价、它自己的止损同时消失 —— 那是 11-24 事故的同一族缺陷。
        #   拆开之后每条规则只看得见自己的那份，谁也不能替对方做决定。
        self.adopted_pos: Optional[dict] = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self.decisions = 0
        self.block_stats = {}
        self._last_bar_time = 0
        self.last_decision: Optional[dict] = None
        # ★ BUS-B：最近一次出站路由的结果（`RouteResult`）。
        #
        # 为什么要把它留在引擎上：路由层知道**具体是哪道闸**拦下了一笔开仓、
        # 理由是什么，但 `_pending_place()` 只把 `bool` 交回上层 ——
        # 于是"被安全闸拦住"这个事实只活在 `risk_events` 里，
        # `decisions` 上看到的是「等触发价」。用户盯着决策流会以为系统在等价格，
        # 实际上它被闸门挡着，而且可能一整晚都不会动。
        #
        # 这正是用户反复要求的那种诚实：「系统在做什么、为什么没做」。
        # 拒绝必须成为一条**决策**，不是一条只有日志能看见的 side effect。
        self._last_route = None
        # ★ BUS-C 观察期状态：上次对照的 15m 桶、上次 guard 的判断。
        #   只用于节流与"仓位消失后补判分歧"，不参与任何交易决策。
        self._guard_obs_ts = 0
        self._guard_obs_last = None
        self.last_error = ""
        # 错误发生的时刻。只存文本不存时间是个陷阱：一旦写入就永久留着，
        # 于是"三天前网络抖了一下"和"刚刚引擎挂了"在看板上长得一模一样。
        # 看板必须能按新旧分级——现在的错误标红，旧错误淡显，否则用户分不清
        # 系统到底是不是正在坏着。
        self.last_error_ts = 0
        self.trend = None            # 趋势策略实例（首次进入趋势分支时才初始化）
        self._last_equity = 0.0      # 最近一次权益，供同步接口兜底（WS 断开时看板仍能显示）
        self._seed_retry_ts = 0      # 补数重试节流
        self._order_fail_count = 0   # 连续下单失败次数（指数退避用）
        self._order_retry_ts = 0     # 早于此刻不再尝试下单
        # 平仓也要退避，理由和开仓一样但后果更重：
        # _manage 每根 3s K 线都会再试一次，持续失败时实测【一天 28,800 次】。
        # 币安按限频会直接封 IP，而封了 IP 就【再也平不掉仓】——
        # 止损失效比开不了仓严重得多。
        # 所以这里不是"失败就放弃"（那等于放弃保护），而是"继续试，但别打满"。
        self._close_fail_count = 0
        self._close_retry_ts = 0
        self._last_reconcile_ms = 0  # 上次实盘对账时间（周期对账用）
        self._trend_block_bar = 0    # 本根 4h 内禁止再入场（止损后防死循环）
        # 挂单对账的重入锁。和 _ledger_busy 同一个用意：首次对账可能要
        # 逐张查询 vanished 的单，耗时不短，两个周期叠在一起会把同一批单
        # 重复写库（虽然 upsert 幂等，但日志和 risk 事件会刷两遍）。
        self._orders_busy = False

    def _fallback_stop(self, side: str, entry: float,
                       adopted: bool = False) -> float:
        """持仓缺少止损价时重算一个，并大声告警。

        宁可给一个保守的保护，也不能留 0（=永不止损）。
        entry 也是 0 时返回 0，由调用方/对账逻辑处理——
        但那种情况下连成本基准都没有，属于必须人工介入的异常。

        ★ `adopted` 区分两种仓（2026-11-24）：
          人工仓（adopted=True）没有任何策略依据，唯一站得住的兜底是
          「爆仓距离 / 安全倍数」—— 那个值在数学上保证先于爆仓触发。

          策略仓仍用 `trend_stop_pct`：那是**已回测的策略参数**（1 倍杠杆
          时代定的），我无权在这一轮把它悄悄改成 1.618% —— 那会把一个
          从不触发的止损变成频繁触发的止损，等于换了策略。
          但会检查它是否落在爆仓之外并报警（那种"保护"是假的）。
        """
        if entry <= 0:
            logger.critical("持仓缺少止损价且无开仓价，无法推算保护位，需人工介入")
            return 0.0
        # 唐奇安策略【刻意不设硬停损】（实测：加 2ATR 停损使每笔 +2.42%->+1.39%，
        # 100U 账户终值 689U->559U）。所以它的 stop 合法地就是 0。
        # 硬塞一个 25% 进去等于给这个策略套上另一个没验证过的规则，
        # 而且会在大屏上把一个不存在的保护位显示成"已设好"。
        if self._is_donchian() and not adopted:
            return 0.0
        if adopted:
            pct = self._stop_distance_pct(entry)
            if pct <= 0:
                pct = float(self.tcfg.trend_stop_pct)
                logger.error("人工仓缺少止损价且算不出安全距离，退回 %.2f%%", pct)
            why = "人工仓无止损价，按爆仓距离推导的 %.3f%%" % pct
        else:
            pct = float(self.tcfg.trend_stop_pct)
            why = "持仓无止损价，按熔断 %.0f%%" % pct
        stop = (entry * (1 - pct / 100.0) if side == "long"
                else entry * (1 + pct / 100.0))
        logger.critical("%s重算为 %.1f（入场 %.1f）", why, stop, entry)
        try:
            self.db.log_risk("stop_missing",
                             "%s 重算为 %.1f" % (why, stop))
        except Exception:
            pass
        # ★ 补一道告警：算出来的止损如果还在爆仓价之外，它就是个假保护。
        #   策略仓这里**只报不改**（参数是回测出来的，改动需另行裁决）。
        self._warn_if_stop_unreachable(side, entry, stop)
        return stop

    def _liq_distance_pct(self) -> float:
        """爆仓距离（价格 %）。算不出返回 0。

        公式与配置页同源：`sizing.derive().liq_distance_pct`。
        22x + 维持保证金 0.5% -> (1/22 - 0.005)*100 = 4.045%
        """
        params = self._sizing_params()
        if params is None:
            return 0.0
        try:
            from .sizing import SizingConfig, derive
            c = SizingConfig(
                capital=float(params.get("sizing.capital", 0.0) or 0.0),
                risk_pct=float(params.get("sizing.risk_pct", 0.0) or 1.0),
                leverage=float(params.get("sizing.leverage", 0.0) or 0.0))
            return float(derive(c).liq_distance_pct)
        except Exception:
            logger.warning("算爆仓距离失败", exc_info=True)
            return 0.0

    def _warn_if_stop_unreachable(self, side: str, entry: float,
                                  stop: float) -> bool:
        """止损是否落在爆仓价之外（= 永远不会触发）。是则告警并返回 True。

        这就是 `position_guard.py:440` 那条不变量
        「止损必须落在爆仓价的安全一侧，否则止损永远不会先触发」——
        它早就写好了，但引擎从来没接（`check_invariants` 零调用）。
        这里至少先把它变成**可见的告警**，不让假保护静默存在。
        """
        liq_pct = self._liq_distance_pct()
        if liq_pct <= 0 or entry <= 0 or stop <= 0:
            return False
        long_side = norm_side(side) == "long"
        # 止损距离与爆仓距离都取绝对值比较，多空共用一套
        stop_pct = abs(entry - stop) / entry * 100.0
        if stop_pct <= liq_pct + 1e-9:
            return False
        msg = ("%s止损 %.4f 距入场 %.3f%%，而爆仓距离只有 %.3f%% —— "
               "这个止损永远不会触发（价格先爆仓）。止损位是假的保护"
               % ("多单" if long_side else "空单", stop, stop_pct, liq_pct))
        logger.critical(msg)
        try:
            self.db.log_risk("stop_beyond_liquidation", msg[:300])
        except Exception:
            pass
        return True

    # ---------------- 生命周期 ----------------
    def _restore_close_fail_count(self) -> None:
        """把平仓连续失败次数从 Guard 读回引擎（第 128 轮）。

        这个计数器有两份副本：

            engine._close_fail_count    引擎自己的，决定退避 3/6/12…/300 秒
            Guard.st.close_fail_streak  第 127 轮落库的，决定能不能开新仓

        而 _sync_close_fail_streak() 是【单向】的 engine -> Guard。
        _close() 清计数走的是「本地置 0 再同步」，
        所以没有任何一条路能把库里的值读回引擎。

        重启之后：Guard 恢复了 6（闸锁着），引擎是 0。两份副本当场不一致。
        后果一：退避从「已经退到 300 秒」掉回「3, 6, 12…」，
                每次重启都把重试频率打回最激进档。
        后果二更隐蔽：引擎以为自己清白，成功平一次仓就 sync(0)，
                把 Guard 那条闸解除了——而库里记着的 6 次失败是真发生过的。

        改法就一句：启动时让引擎【采纳】Guard 的值。
        这样两份副本只可能通过这个方向汇合，不会再各说各话。

        为什么【不】恢复 _close_retry_ts：
        退避是为了「别在交易所抽风时把自己打到封禁」。
        新进程起来时那次抽风可能已经过去了，立刻试一次是对的——
        平仓方向本来就该更积极。这里只恢复计数，不恢复退避时点。
        """
        if self.guard is None:
            return
        try:
            streak = int(getattr(self.guard.st, "close_fail_streak", 0) or 0)
        except Exception:
            logger.warning("读回平仓失败计数失败，按 0 处理", exc_info=True)
            return
        if streak > 0:
            self._close_fail_count = streak
            logger.warning("已从 Guard 恢复平仓连续失败计数 %d 次（退避档位随之接上）",
                           streak)

    def _set_block_bar(self, bar_t: int) -> None:
        """把「本根 4h 已熔断出局」记到库里（第 126 轮）。

        为什么要落库：这是唯一挡住"止损 -> 重启 -> 立刻补回 -> 再止损"
        死循环的东西。放在内存里等于它的有效期只到下一次重启为止，
        而重启最可能发生在行情剧烈波动时——正是这个闸刚被设上的时刻。

        ★ 写和清走同一个方法：新的一根 4h 到了就把旧值清成 0。
          两处各写各的，就会出现只锁不放（第 116/117/123 轮的教训）。
        """
        self._trend_block_bar = int(bar_t or 0)
        db = getattr(self, "db", None)
        if db is None:
            return
        try:
            db.save_block_bar(self._trend_block_bar)
        except Exception:
            logger.error("熔断出局标记落库失败", exc_info=True)

    def _restore_block_bar(self) -> None:
        """启动时把「本根 4h 已熔断出局」读回来（第 126 轮）。

        读不到就保持 0（= 没被拦住），并【明确记日志】——
        静默地退回旧行为，正是本轮这个缺陷最初的成因。
        """
        db = getattr(self, "db", None)
        if db is None:
            return
        try:
            v = int(db.load_block_bar() or 0)
        except Exception:
            logger.warning("读回熔断出局标记失败，按未拦住处理", exc_info=True)
            return
        if v > 0:
            self._trend_block_bar = v
            logger.warning("已从库恢复「本根 4h 已熔断出局」标记 t=%s", v)

    def attach(self) -> None:
        self.feed.bar_hooks.append(self._on_bar_sync)
        # 熔断出局标记必须【在恢复持仓之前】读回来：
        # 恢复持仓后会走 reconcile_live 接管/平账，那些路径不看这个标记，
        # 但紧接着的第一根 3s 就会判入场——中间没有别的机会再补读。
        self._restore_block_bar()
        # 平仓失败计数有两份副本（engine 的和 Guard 的），第 127 轮只落了
        # Guard 那份。这里让引擎启动时【采纳】Guard 的值，两份才不会再各说各话。
        self._restore_close_fail_count()
        # ★ 重启恢复：两个来源都要恢复（2026-11-24 拆仓）。
        #
        #   以前只恢复 `open_trade()`（`ORDER BY id DESC LIMIT 1`）那一行。
        #   现在账上可能同时开着「系统仓」和「接管的人工仓」，只取最新一行
        #   必然丢掉其中一个来源 —— 而丢哪个取决于 id 顺序，等于让重启随机
        #   改变归属。所以这里取**全部**未平仓记录，按 open_reason 分别归位。
        try:
            _open_rows = self.db.open_trades(limit=5)
        except AttributeError:            # 测试替身/旧库没有复数接口
            _r0 = self.db.open_trade()
            _open_rows = [_r0] if _r0 else []
        _restored_sys = None
        for _row in _open_rows:
            _built = self._pos_from_row(_row)
            if str(_row.get("open_reason") or "").strip() == ADOPT_REASON:
                if self.adopted_pos is None:
                    self.adopted_pos = _built
                    logger.warning("恢复【接管的人工仓】: %s", self.adopted_pos)
                else:
                    logger.error("恢复时发现第二笔接管仓，只保留最新一笔：id=%s",
                                 _row.get("id"))
            else:
                if _restored_sys is None:
                    _restored_sys = _built
                else:
                    logger.error("恢复时发现第二笔系统仓，只保留最新一笔：id=%s",
                                 _row.get("id"))
        self.pos = _restored_sys
        if self.pos:
            logger.info("恢复未平仓持仓: %s", self.pos)

    def _pos_from_row(self, open_trade: dict) -> dict:
        """把 `trades` 的一行还原成引擎内存里的持仓 dict。

        ★ 提成独立函数是必须的：`attach()`（重启恢复）和
          `_reconcile_positions()`（接管/平账）都要按同一套字段构造仓位。
          以前这段构造逻辑只散落在 attach() 里，谁再写一份就又是一次
          「同一件事两个来源」。
        """
        entry = float(open_trade.get("entry_price") or 0.0)
        stop = float(open_trade.get("stop_price") or 0.0)
        # 这一行是「对账接管的人工仓」吗？决定缺止损时用哪个口径重算：
        # 人工仓没有策略依据 -> 用爆仓距离推导的安全止损；
        # 策略仓 -> 仍用已回测的 trend_stop_pct。
        _is_adopted_row = (str(open_trade.get("open_reason") or "").strip()
                           == ADOPT_REASON)
        if stop <= 0:
            # 止损价绝不能是 0。0 的含义是"价格跌到 0 才止损"，
            # 等于这道保护静默消失，仓位会一直挂到跌破 SMA 才出场。
            # 脏数据（早期 bug 遗留、人工插入）真的会出现这一列是 NULL 的情况，
            # 事故 2 只修了 reconcile_live 的接管路径，attach() 这条被漏掉了。
            stop = self._fallback_stop(open_trade.get("side"), entry,
                                       adopted=_is_adopted_row)
        if stop > 0:
            # 恢复出来的止损也要过一遍「是否还在爆仓之内」的检查：
            # 库里可能躺着一条 22 倍杠杆下永远触发不了的止损。
            self._warn_if_stop_unreachable(open_trade.get("side"), entry, stop)
        return {
            "side": open_trade["side"],
            "qty": open_trade["qty"],
            "entry": open_trade.get("entry_price"),
            "stop": stop,
            "target": open_trade.get("target_price") or 0.0,
            "entry_ts": open_trade.get("entry_ts"),
            # 按时间推算已走过的 3s 根数，避免重启把超时计时清零
            "bars": max(0, int((int(time.time() * 1000)
                                - int(open_trade.get("entry_ts") or 0)) / 3000)),
            "best": entry,
            "worst": entry,
            "trade_id": open_trade.get("id"),
            # 必须用开仓时记录的值，不能用默认值：
            # 2026-11-01 事故 —— 一笔按 18 小时设计的单在重启后被当成 600 根(30分钟)强平，
            # 其间它本来还在正常轨道上。
            "max_hold_bars": int(open_trade.get("max_hold_bars") or 10 ** 9),
            "open_reason": open_trade.get("open_reason") or "",
            # 必须带出来源的 paper 标记。
            # reconcile_live() 关闭遗留持仓时要按它记账，以前那里写死 paper: 0，
            # 于是纸面仓被记成真实交易，纸面/实盘两本账当场串掉。
            "paper": int(open_trade.get("paper") or 0),
            # 开仓手续费必须带回来。
            # 2026-11-02 实测缺陷：这里以前没有 fee，于是
            #   _close 里 fee_total = p.get("fee", 0.0) + fill.fee
            # 把开仓费算成 0，记录的 pnl_net 只减了平仓费。
            # 线上 4 笔剥头皮仓的缺口 7.77U 正是这么来的：
            # 逐笔核对，每笔少的钱恰好等于它的开仓手续费。
            # 危险之处在于方向——漏记成本永远是让亏损看起来更小，
            # 也就是让策略看起来更赚钱，这会把纸面样本的证据价值直接毁掉。
            "fee": float(open_trade.get("fee") or 0.0),
        }

    async def _cost_basis_from_trades(self, ex_qty: float) -> float:
        """用真实成交记录推算持仓成本。拿不到或不自洽时返回 0。

        为什么要求"自洽"：成交历史可能被 limit 截断，也可能是别人手工买的、
        或者币是转进来的。只有回放出来的净持仓和交易所余额对得上，
        这个成本才可信；对不上就宁可退回"按现价估算 + 大声告警"。
        """
        fn = getattr(self.ex, "cost_basis", None)
        if fn is None:
            return 0.0
        try:
            cb = await fn()
        except Exception as e:
            logger.warning("成本基准计算失败：%s", e)
            return 0.0
        if not cb or not cb.get("ok"):
            return 0.0
        avg = float(cb.get("avg") or 0.0)
        qty = float(cb.get("qty") or 0.0)
        if avg <= 0 or qty <= 0:
            return 0.0
        # 容忍 0.5% 的偏差（手续费从币里扣、极小残留等）
        if abs(qty - abs(ex_qty)) > max(abs(ex_qty) * 0.005, 1e-8):
            logger.warning("成交历史净持仓 %.8f 与交易所余额 %.8f 对不上，"
                           "成本基准不可信，弃用", qty, abs(ex_qty))
            return 0.0
        return avg

    async def reconcile_live(self) -> None:
        """实盘对账 + 顺手抓一次真账（第 130 轮）。

        为什么要包一层而不是在原方法末尾加一句：原方法在"对账通过"时
        **提前 return**（那是绝大多数情况），末尾那句永远执行不到——
        结果就是只在账实不符时才抓真账，正好反了。

        真账同步放在 finally 里：对账本身抛异常时也要抓一次。
        _sync_live_ledger() 内部兜住了所有异常，所以它不会改变
        对账的成败，也不会吞掉对账自己的异常。
        """
        try:
            await self._reconcile_positions()
        finally:
            await self._sync_live_ledger()
            # 挂单对账放在最后：它依赖真实持仓已经对齐（"这个止损单
            # 属于哪一笔仓"要先知道仓是多少），而且它自己会再查一次
            # 交易所状态。
            await self._sync_working_orders()

    async def _sync_working_orders(self) -> None:
        """把本地挂单账和交易所对齐（第 131 轮）。

        没有这一步的话，P0–P2 建的东西全是死的：`working_orders` 表不会被
        填，「交易所有、本地没有」的单永远不会被补进来。而这张表唯一的价值
        恰恰就是重启之后还能知道自己挂过什么。

        三个方向都要走通：

        1. **交易所有、本地没有** → 补进来。
           漏掉它的后果不是"少一条记录"，而是引擎不知道自己还挂着一张止损单，
           于是再挂一张 → 同一笔仓两个止损 → 先触发的平掉大部分仓，
           后触发的那张把剩仓**反向开出去**。
        2. **本地有、交易所没有** → 先标 LOST，再逐张 `query_order()` 查真实终态。
           LOST 只是"我这儿认为它还挂着，交易所那儿查不到"这个中间态，
           **不是结论**。它可能是已经成交了（那就是一笔真实成交，持仓变了、
           账要记），也可能被手工撤了。直接当"没有"会把已成交的单当成不存在，
           账面上少一笔真实的平仓。
        3. **两边都有但状态不同** → 用交易所的覆盖。

        失败只记日志和 risk，绝不冒泡：拿不到挂单列表时，沿用本地账
        比"对账失败导致整个 reconcile_live 抛出去"安全得多 ——
        后者的后果是持仓对账也一并失败（在同一个 try 里）。
        """
        tdb = getattr(self, "db", None)
        ex = getattr(self, "ex", None)
        if tdb is None or ex is None:
            return
        # 现货执行器没有挂单接口；纸面执行器更没有。
        # 用 hasattr 而不是 try/except AttributeError：后者会把 open_orders
        # 内部的真故障也当成"这个执行器不支持"吞掉。
        # ★ 2026-11-17：判据从 `hasattr` 换成**能力标记**。
        #
        #   基类现在声明了 `open_orders`（否则「纸面没有挂单」只能等生产发现），
        #   于是 `hasattr` 恒为真 —— 现货执行器被当成支持挂单，
        #   启动对账抛 `SpotExecutor 不支持查挂单列表`，写出一条假的
        #   `orders_fetch_failed`，而哨兵正是靠「risk_events 为空」判健康的。
        #   `supports_pending=False` + `hasattr` 兜底：鸭子类型的测试替身
        #   （只实现方法、不设标记）仍然照旧能跑。
        if not (hasattr(ex, "open_orders")
                and getattr(ex, "supports_pending", True)):
            return
        if getattr(self, "_orders_busy", False):
            return
        self._orders_busy = True
        try:
            try:
                remote = await ex.open_orders()
            except Exception as e:
                logger.error("拉挂单列表失败，本轮沿用本地账：%s", e)
                tdb.log_risk("orders_fetch_failed", str(e)[:200])
                return

            local = tdb.working_orders()
            res = reconcile_orders(local, remote)
            out = tdb.apply_order_reconciliation(res)

            # `vanished` 必须逐张查清真实终态 —— 见上面第 2 点。
            settled = await self._resolve_vanished(res.get("vanished") or [])

            logger.info("[挂单] 对账：交易所 %d 张 / 本地 %d 张 → "
                        "补 %d、变 %d、一致 %d、查不到 %d（其中查清 %d）",
                        len(remote), len(local), out["adopted"], out["changed"],
                        out["same"], out["lost"], settled)
            if out["adopted"]:
                # 补进来的单必须是显眼的：它们意味着上一次退出时本地账
                # 和交易所就已经不一致了，那是一次没被发现的异常。
                logger.warning("[挂单] 从交易所补回 %d 张本地不知道的挂单", out["adopted"])
                tdb.log_risk("orders_adopted", "从交易所补回 %d 张挂单" % out["adopted"])
        except Exception as e:
            logger.error("挂单对账失败（不影响交易）", exc_info=True)
            # P3 的验收写的是「失败只记 risk 和日志」—— 只写日志不够：
            # 这条 except 会把**任何**实现缺陷吞成一行 ERROR（第 131 轮就是
            # PendingOrder/dict 接口断裂在这里静默消失了十几分钟，
            # 表面上"对账跑了"，实际一张单都没对上）。落一条 risk 事件，
            # 看板和 risk_events 里才查得出来。
            try:
                tdb.log_risk("orders_sync_failed", str(e)[:200])
            except Exception:
                pass
        finally:
            self._orders_busy = False

    async def _resolve_vanished(self, vanished) -> int:
        """对「本地认为挂着、交易所查不到」的单逐张查真实终态。返回查清的张数。

        查清之后落到本地账上的状态才有意义：
          * FILLED          → 这张单真的成交过，持仓已经变了
          * CANCELED        → 被撤了（可能是手工撤的）
          * 查不到 / 报错   → 保持 LOST。**不要**改写成 CANCELED：
            把"不知道"写成"已撤销"是这里最容易犯的错，它会让一张
            可能已经成交的单从此消失。
        """
        tdb = getattr(self, "db", None)
        ex = getattr(self, "ex", None)
        if tdb is None or ex is None or not vanished:
            return 0
        if not hasattr(ex, "query_order"):
            return 0
        done = 0
        for o in vanished:
            cid = o.get("client_order_id") or ""
            oid = o.get("exchange_order_id") or ""
            try:
                cur = await ex.query_order(order_id=oid, client_id=cid)
            except Exception as e:
                logger.warning("[挂单] 查 %s 真实状态失败：%s", cid, e)
                continue
            if cur is None:
                # 查不到就保持 LOST —— 不改写、不删除。
                continue
            st = str(getattr(cur, "status", "") or "")
            if not st:
                continue
            row = dict(o)
            row["status"] = st
            row["executed_qty"] = float(getattr(cur, "executed_qty", 0) or 0)
            row["avg_price"] = float(getattr(cur, "avg_price", 0) or 0)
            row["exchange_order_id"] = str(getattr(cur, "order_id", "") or oid)
            row["updated_ts"] = int(time.time() * 1000)
            row["synced_ts"] = row["updated_ts"]
            if st == "FILLED":
                row["note"] = (o.get("note") or "") + " | 对账查清：已成交"
                logger.warning("[挂单] %s 本地以为挂着，实际已成交 %.6f @ %.2f",
                               cid, row["executed_qty"], row["avg_price"])
            else:
                row["note"] = (o.get("note") or "") + " | 对账查清：%s" % st
            tdb.upsert_working_orders([row])
            done += 1
        return done

    async def _classify_position_delta(self, delta: float):
        """查交易所成交，判这次错配是「人工仓」还是「我们丢了成交回报」。

        返回 ``(结论, 用到的成交)``。三个**保守**决定：

          * 执行器不支持查成交历史（纸面/现货）→ unknown → 照旧硬停
          * 查失败（网络/签名/限频）→ unknown → 照旧硬停
          * 7 天成交凑不出差额 → unknown → 照旧硬停

        绝不因为「查不到」就放宽：放宽等于赌没人动过仓位。
        """
        ex = getattr(self, "ex", None)
        if ex is None or not getattr(ex, "supports_fill_history", False):
            return PROV_UNKNOWN, []
        try:
            fills = await ex.recent_fills()
        except Exception as e:
            logger.warning("[对账] 查成交历史失败，按未知处理（仍会硬停）：%s", e)
            return PROV_UNKNOWN, []
        return classify_delta(fills, delta)

    async def _reconcile_positions(self) -> None:
        """实盘启动对账：数据库记的和交易所实际持仓必须一致，不一致以交易所为准。

        为什么必须以交易所为准：数据库只是一份记账，钱和仓在交易所手里。
        如果这里图省事沿用数据库，就会出现"引擎以为有仓、实际没有"，
        于是止损永远打在一个不存在的仓上——这是能直接亏损的分歧。
        """
        if self.ex.name not in ("live", "spot"):
            return
        try:
            actual = await self.ex.position()
        except Exception as e:
            logger.critical("实盘对账失败，无法确认持仓：%s", e)
            self.last_error = "对账失败：" + str(e)[:120]
            self.last_error_ts = int(time.time() * 1000)
            return
        # ★ 2026-11-24 拆仓后：数据库这边的"应有多少仓"是**两个来源之和**。
        #   交易所只有净持仓，所以对账必须比「系统仓 + 人工仓」的和，
        #   否则一旦两个来源同时存在，每一轮对账都会判成不一致。
        db_qty = 0.0
        db_side = ""
        for _slot in (self.pos, self.adopted_pos):
            if not _slot:
                continue
            # 必须把开仓时接管的零头算进来。
            #
            # 现货账上的余额 = 持仓 + 零头（零头在 _open 里被认领进 pos["dust"]，
            # 平仓时一起卖掉）。如果这里只算 pos["qty"]，账上就永远比数据库多一个
            # 零头的量，差额大于 1e-9 容差 —— 持仓期间每 5 分钟的对账都会判定
            # "不一致"，然后走接管分支把仓位重写一遍：pos["qty"] 被改成账上余额，
            # dust 键丢掉（下一笔平仓就不再回收零头，累积立刻恢复），
            # 入场价也可能被 _cost_basis_from_trades 的加权成本顶替。
            #
            # 实测：开仓后账上 0.00126746、pos["qty"] 0.00125874、dust 0.00000872；
            # 对账一次之后 dust 键消失、pos["qty"] 变成 0.00126746。
            _own = float(_slot["qty"]) + float(_slot.get("dust") or 0.0)
            db_qty += _own * (1 if self._norm_side(_slot["side"]) == "long" else -1)
            db_side = _slot["side"] if not db_side else (
                db_side if db_side == _slot["side"] else db_side + "/" + _slot["side"])
        ex_qty = float(actual["qty"])
        # 分流结论的跨分支载体：else-分支里算一次，硬停块复用（见那里的注释）。
        prov_pre = None
        used_pre = ()

        if abs(ex_qty - db_qty) < 1e-9:
            logger.info("实盘对账通过：持仓 %+.6f 与数据库一致", ex_qty)
            return

        # 零头不是持仓。
        #
        # 现货买入的手续费扣在【基础币】上，到账净额不是 step 的整数倍
        # （买 0.00127 BTC 实际到账 0.00126873）。平仓时按 step 向下取整卖出，
        # 必然留下零头。下一笔买回来又叠在零头上面，于是零头单调累积。
        #
        # 实测（价格 78000，step 0.00001，100U 起）：
        #   第 1 笔后余 0.00000872 BTC -> SpotExecutor.position() 折叠成 0
        #   第 2 笔后余 0.00001746 BTC -> position() 开始把它当真持仓返回
        # 于是这里会判定"交易所不一致"，走进下面的接管分支，
        # 把一个 1.36U 的零头当成真仓位接管下来写进 self.pos。
        #
        # 后果是致命的：引擎用 if self.pos 拦着不重复开仓，
        # 一旦 self.pos 被零头占住，【从此再也不会开新仓】——策略静默失效。
        # 而且日志还会说"拿不到真实成交记录，只能以当前价当成本"，
        # 那笔假盈亏会被记进策略边际统计。
        #
        # 判据：连一笔最小下单都凑不出来的余额，不可能是策略仓位。
        if ex_qty != 0 and not db_qty:
            px_now = float(self.feed.last_price or 0.0)
            dust_notional = abs(ex_qty) * px_now
            min_n = self._min_notional()
            if px_now > 0 and dust_notional < min_n:
                logger.warning("实盘对账：交易所余 %.8f BTC（约 %.2fU）低于最小下单 %.1fU，"
                               "判定为 step 取整零头，不当持仓", ex_qty, dust_notional, min_n)
                return

        logger.critical("实盘对账不一致：数据库 %+.6f(%s)，交易所 %+.6f —— 以交易所为准",
                        db_qty, db_side or "空仓", ex_qty)
        if abs(ex_qty) < 1e-9:
            # 交易所空仓但数据库有仓：把**两个来源**都标记为已平。
            #
            # ★ 两个都要关（2026-11-24 拆仓）：漏掉一个的后果不是"少一条记录"，
            #   而是重启之后那一笔会被当成"交易所有仓、我们没有" —— 于是
            #   系统仓被重新接管成人工仓，归因再次串掉。
            for _which in ("pos", "adopted"):
                _slot = getattr(self, "adopted_pos" if _which == "adopted" else "pos")
                if not _slot:
                    continue
                self.db.close_trade({
                    "symbol": self.tcfg.symbol, "side": _slot["side"],
                    "qty": _slot["qty"], "entry_ts": _slot["entry_ts"],
                    "entry_price": _slot["entry"], "exit_ts": int(time.time() * 1000),
                    "exit_price": float(actual.get("entry") or _slot["entry"]),
                    "stop_price": _slot["stop"], "target_price": _slot["target"],
                    "pnl_gross": 0.0, "fee": 0.0, "pnl_net": 0.0, "mfe": 0.0, "mae": 0.0,
                    "open_reason": _slot.get("open_reason", ""),
                    "close_reason": "实盘对账：交易所已无此仓",
                    # 按这笔仓【原本】的来历记账，不能一律记成真实交易：
                    # 从纸面切到实盘后，库里遗留的纸面仓会走到这条路径。
                    "paper": int(_slot.get("paper") or 0)})
                if _which == "adopted":
                    self.adopted_pos = None
                else:
                    self.pos = None
        else:
            # 交易所有仓、而我们记的不一致。**先判这笔差额是谁造成的，
            # 再决定怎么记账** —— 顺序反了就会把系统自己的漏账成交
            # 记成人工仓（11-20 自检单成交没落账，就是这么被接管的）。
            delta = ex_qty - db_qty
            prov, used = await self._classify_position_delta(delta)
            # ★ 无论判不判得出来源，**账都必须先修对**。
            #
            #   这里有过一个想当然的写法：只有 prov==MANUAL 才接管，
            #   判不出来就不动账。那样 db_qty 会停在 0，引擎以为自己空仓 ——
            #   交易所有真仓、本地没仓，等于仓位既没有止损、还可能再开一笔
            #   叠加在上面。比"记错来源"危险得多。
            #   所以：接管照做（本地认知必须等于交易所），
            #   而"要不要停下来等人查"是下面硬停块单独的判断。
            prov_pre, used_pre = prov, used
            # ★★ 先把**与交易所方向相反的本地仓**关掉（账先修对）。
            #
            #   漏掉这一步的后果（2026-11-24 我重构时就漏过一次，
            #   被 tests/test_equity_outage.py 逮住）：
            #   库里记空头 -0.001、交易所是多头 +0.001，
            #   于是同时留下「空头 0.001」和「接管多头 0.002」两笔账 ——
            #   合计恰好等于交易所净量，对账反而显示"一致"，
            #   但引擎实际持有一多一空两笔互相抵消的仓：
            #   出场规则各自算各自的，止损/止盈全部错位。
            #   交易所只有一个净持仓，本地账也**只能有一个方向**。
            _ex_side = "long" if ex_qty > 0 else "short"
            for _which in ("pos", "adopted"):
                _slot = (self.adopted_pos if _which == "adopted" else self.pos)
                if not _slot:
                    continue
                if self._norm_side(_slot.get("side")) == _ex_side:
                    continue
                logger.critical(
                    "[对账] 本地 %s 仓与交易所方向相反（本地 %s %.6f / 交易所 %s），"
                    "按交易所为准关账", _which, _slot.get("side"),
                    float(_slot.get("qty") or 0.0), _ex_side)
                try:
                    self.db.close_trade({
                        "symbol": self.tcfg.symbol, "side": _slot["side"],
                        "qty": _slot["qty"], "entry_ts": _slot.get("entry_ts"),
                        "entry_price": _slot.get("entry"),
                        "exit_ts": int(time.time() * 1000),
                        "exit_price": float(actual.get("entry") or _slot.get("entry") or 0.0),
                        "stop_price": _slot.get("stop"),
                        "target_price": _slot.get("target"),
                        "pnl_gross": 0.0, "fee": 0.0, "pnl_net": 0.0,
                        "mfe": 0.0, "mae": 0.0,
                        "open_reason": _slot.get("open_reason", ""),
                        "close_reason": "实盘对账：方向与交易所相反，按交易所为准",
                        "paper": int(_slot.get("paper") or 0)})
                except Exception:
                    logger.error("[对账] 关账失败（方向冲突，继续）", exc_info=True)
                if _which == "adopted":
                    self.adopted_pos = None
                else:
                    self.pos = None
            # 反向的账关完之后，本地剩下的都是同向仓，重算差额。
            db_qty = 0.0
            for _slot in (self.pos, self.adopted_pos):
                if not _slot:
                    continue
                db_qty += (float(_slot["qty"]) + float(_slot.get("dust") or 0.0)) * (
                    1 if _slot["side"] == "long" else -1)
            delta = ex_qty - db_qty
            # ★ "交易所比本地账少"必须用**符号关系**判，不能用 `delta < 0`：
            #   空头仓 delta 天然是负数（ex=-0.00127、db=0 → delta=-0.00127），
            #   用 sign(delta) 会把这笔正常的空头接管误判成"疑似部分被平"，
            #   直接 return 掉 —— 引擎于是以为自己空仓，而交易所真有空头。
            #   正确判据：本地账非空、且差额方向与交易所净持仓方向相反，
            #   才说明「账上记的比交易所多」（被部分平/被强平）。
            if db_qty != 0 and delta * ex_qty < 0:
                # 交易所比本地账**少**：有人（或强平）平掉了一部分，而我们的账
                # 还记着全额。不知道平了多少、什么价平的 → 不许猜。
                _msg = ("实盘对账：交易所比本地账少 %.6f（本地 %+.6f / 交易所 %+.6f），"
                        "疑似部分被平，需人工核查" % (abs(delta), db_qty, ex_qty))
                logger.critical(_msg)
                try:
                    self.db.log_risk("position_shrunk_externally", _msg[:300])
                except Exception:
                    pass
                if self.guard is not None:
                    try:
                        self.guard.position_mismatch(_msg)
                    except Exception:
                        logger.error("部分被平的告警通道失败（继续）", exc_info=True)
                return
            if abs(delta) < 1e-9:
                return
            if True:
                # ---- §13.2：接管为【人工仓】----
                side = "long" if delta > 0 else "short"
                qty_delta = abs(delta)
                # ★ 增量的入场价必须用**成交记录**加权算，
                #   不能用 actual["entry"] —— 那是整笔净持仓的均价，
                #   里面混着系统仓的成本。用它会把两个来源的成本再次搅在一起。
                entry = 0.0
                _wsum = 0.0
                _wqty = 0.0
                for f in (used or ()):
                    _q = abs(float(f.get("qty") or 0.0))
                    _p = float(f.get("price") or 0.0)
                    if _q > 0 and _p > 0:
                        _wsum += _p * _q
                        _wqty += _q
                if _wqty > 0:
                    entry = _wsum / _wqty
                    logger.info("[对账] 接管增量 %+.6f 的入场价按 %d 笔成交加权 = %.2f",
                                delta, len(used or ()), entry)
                if entry <= 0:
                    # 成交里没有价（理论上不会）→ 退回交易所均价再退回现价
                    entry = float(actual.get("entry") or 0.0)
                if entry <= 0:
                    # ★ 现货查不到开仓价（交易所只记余额），先用真实成交记录
                    #   算加权成本。这一步**不能省**（2026-11-24 我重构时漏过一次，
                    #   被 tests/test_takeover_no_entry.py 逮住）：少了它，
                    #   接管成本会退回当前价，止损跟着挪到一个假成本上，
                    #   而且平仓时算出的盈亏全按估算值结算，边际统计全废。
                    entry = await self._cost_basis_from_trades(ex_qty)
                    if entry > 0:
                        logger.warning("交易所持仓 %+.6f 无开仓价（现货特性），"
                                       "已按真实成交记录算出成本基准 %.1f",
                                       ex_qty, entry)
                if entry <= 0:
                    entry = float(self.feed.last_price or 0.0)
                    if entry > 0:
                        logger.critical(
                            "接管持仓 %+.6f 时【拿不到真实成交记录】，只能以当前价 %.1f 当成本。"
                            "此后再平仓，盈亏会按这个估算值结算，不是真实成本——"
                            "该笔盈亏不应计入策略边际统计", ex_qty, entry)
                        try:
                            self.db.log_risk(
                                "adopt_estimated_entry",
                                "成交历史不可用，以现价 %.1f 接管 %.6f" % (entry, ex_qty))
                        except Exception:
                            pass
                if entry <= 0:
                    logger.critical("接管持仓失败：既无开仓价也无行情价，放弃接管")
                    return
                # ★ 接管的仓位【一律给一个真实的熔断止损】（第 129 轮）。
                #
                # 原来这里对唐奇安给 0.0，理由是"不能凭空造一条回测里不存在的规则"。
                # 那个理由对【策略自己开的仓】成立，对【接管来的仓】不成立：
                # 它不是回测里的任何一笔，本来就没有策略依据，也就谈不上"偏离回测"。
                #
                # 而 stop=0.0 的实际效果是 `price <= 0` 恒为假 —— 这笔仓完全没有兜底，
                # 跌掉 90% 引擎也不会动一下，看板上还把 stop 显示成 0.0。
                #
                # 用户明确要求：人工仓不判策略出场，但要保留熔断止损。
                #
                # ★★ 2026-11-24 修正：**不能直接用 `trend_stop_pct` 当价格跌幅。**
                #
                #   生产实测（22 倍杠杆，0.032 BTC @ 60880，权益 114U）：
                #       逐仓爆仓价 58356（距入场 4.15%）
                #       旧止损     45660（距入场 25.00%）← trend_stop_pct 直接当价格 %
                #   止损比爆仓价还低 12696 点 = 0.17 倍爆仓距离。
                #   **价格永远先到 58356 爆仓，45660 一次都不可能触发。**
                #   这不是"止损太远"，是一句永远不会兑现的话 ——
                #   比没有止损更糟，因为看板上它显示成"保护已设好"。
                #
                #   歧义来自 `trend_leverage = 1.0` 的历史设计：1 倍杠杆下
                #   「价格跌 25%」和「保证金亏 25%」是同一个数，两种理解
                #   恰好等价，问题从未暴露。上了 22 倍杠杆才分家。
                #
                #   现在改成**唯一正确口径**：止损距离 = 爆仓距离 / 安全倍数
                #   （`sizing.derive().max_stop_distance_pct`）。这个公式本来
                #   就写在仓库里、给挂单策略用（1.618%），且天然满足
                #   「止损永远先于爆仓」，不需要另立一套规则。
                _stop_pct = self._stop_distance_pct(entry)
                if _stop_pct <= 0:
                    # 算不出来的唯一可能是仓位参数不可用。此时宁可沿用旧口径
                    # 也不能给 0（0 = 永不止损），但必须喊出来。
                    _stop_pct = float(self.tcfg.trend_stop_pct)
                    logger.error(
                        "接管仓算不出安全止损距离（仓位参数不可用），退回 "
                        "trend_stop_pct=%.2f%%——这一笔的保护位可能晚于爆仓", _stop_pct)
                    try:
                        self.db.log_risk("adopted_stop_fallback",
                                         "无法计算安全止损距离，退回 %.2f%%" % _stop_pct)
                    except Exception:
                        pass
                stop = (entry * (1 - _stop_pct / 100.0) if side == "long"
                        else entry * (1 + _stop_pct / 100.0))
                logger.info(
                    "[对账] 接管仓止损按爆仓距离推导：%.3f%%（入场 %.2f -> 止损 %.2f）",
                    _stop_pct, entry, stop)
                # ★ 合并规则：只允许同向累加（用户分几次加仓是正常的）；
                #   反向意味着用户先平再反手，那是「关掉旧仓 + 开新仓」两件事，
                #   不能用一次"合并"糊过去 —— 拒绝并交给人工。
                if self.adopted_pos and self._norm_side(
                        self.adopted_pos.get("side")) != side:
                    _msg = ("实盘对账：人工仓方向变了（内存 %s %s / 交易所 %s %+.6f），"
                            "拒绝自动合并，需人工核查"
                            % (self.adopted_pos.get("side"),
                               self.adopted_pos.get("qty"), side, delta))
                    logger.critical(_msg)
                    try:
                        self.db.log_risk("adopted_side_flip", _msg[:300])
                    except Exception:
                        pass
                    if self.guard is not None:
                        try:
                            self.guard.position_mismatch(_msg)
                        except Exception:
                            logger.error("方向冲突的告警通道失败（继续）", exc_info=True)
                    return
                # ★ 每笔接管都写一条独立的 trade 记录（不合并成一条），
                #   溯源时才能看到"用户 19:39 买了多少、21:10 又加了多少"。
                self.db.log_trade({
                    "symbol": self.tcfg.symbol, "side": side, "qty": qty_delta,
                    "entry_ts": int(time.time() * 1000), "entry_price": entry,
                    "stop_price": stop, "target_price": 0.0,
                    "open_reason": ADOPT_REASON, "paper": 0,
                    "strategy": self.tcfg.strategy,
                    "max_hold_bars": 10 ** 9 if self._is_chan()
                    else self.tcfg.max_hold_bars})
                _now = int(time.time() * 1000)
                if self.adopted_pos:
                    _oq = float(self.adopted_pos.get("qty") or 0.0)
                    _oe = float(self.adopted_pos.get("entry") or 0.0)
                    _nq = _oq + qty_delta
                    self.adopted_pos["entry"] = (
                        (_oe * _oq + entry * qty_delta) / _nq if _nq > 0 else entry)
                    self.adopted_pos["qty"] = _nq
                    logger.warning("[对账] 人工仓加仓：%.6f @ %.2f → 合计 %.6f，均价 %.2f",
                                   qty_delta, entry, _nq, self.adopted_pos["entry"])
                else:
                    self.adopted_pos = {
                        "side": side, "qty": qty_delta, "entry": entry,
                        "stop": stop, "target": 0.0, "entry_ts": _now,
                        "bars": 0, "best": entry, "worst": entry,
                        "trade_id": None,
                        "max_hold_bars": 10 ** 9 if self._is_chan()
                        else self.tcfg.max_hold_bars,
                        "open_reason": ADOPT_REASON,
                        # 接管的仓位是交易所上本来就有的，它的开仓手续费
                        # 我们客观上不知道（可能是人工买的、或早于本系统上线）。
                        # 记 0 是诚实的下界，不是漏写——但代价是这笔的盈亏
                        # 会系统性偏乐观（少了开仓成本）。显式写出来，
                        # 免得以后有人看到缺项以为是 bug 又去改。
                        #
                        # 注意：这个 0 只影响【这一笔的入场成本】。
                        # 平仓时 fill.fee 那一侧的手续费曾经因为 ACK 回报
                        # 整段漏成 0（见 exchange/live.py 的 _fill_truth），
                        # 那是另一个 bug，已单独修。
                        "fee": 0.0}
                    logger.critical("[对账] 接管人工仓：%s %.6f @ %.2f（熔断 %.2f）",
                                    side, qty_delta, entry, stop)

                # ★ 2026-11-25（用户裁决）：接管的那一瞬就要挂上交易所侧保护单。
                #   放在对账里而不是等 `_step`，是因为对账可能在启动时跑，
                #   而 `_step` 的第一轮要等行情；抄底仓 10 分钟内就可能遇到
                #   超级大波动，那段时间里裸奔的敞口没有第二次机会。
                #   幂等：已在 `_sync_adopted_stop_order` 里做「已对齐不发请求」，
                #   重复调用只多一次本地比较。
                if self.adopted_pos:
                    _px = float(getattr(self.feed, "last_price", 0.0) or 0.0)
                    try:
                        await self._sync_adopted_stop_order(self.adopted_pos, _px)
                    except Exception as e:
                        logger.error("[对账] 接管瞬间挂保护单失败（下一 tick 重试）：%s",
                                     e)

        # guard.notify 可能本身是 None：没配 AI_FEISHU_WEBHOOK 时
        # feishu_notifier() 返回 None，Guard 把它原样存进 self.notify。
        # Guard.alert() 里检查了这一点，但这里直接调用就会 TypeError，
        # 而且崩在 reconcile_live() 中间——等于启动对账整体失败，
        # 孤儿仓位永远接不过来。生产配了 webhook 所以没暴露。
        # ★ 第 6 条闸「持仓与账户不一致 -> 硬停」必须真的踩下去（第 115 轮）。
        #
        # 以前这里只 notify 一句就继续跑了。而文档写的是
        # 「真金白银时对账不一致 = 必须先查」——不一致意味着我们不知道
        # 真实敞口是多少，这时候继续交易等于在不懂的仓位上叠加下注。
        #
        # 三类真实成因，每一种都要人看：
        #   1. 下单超时但交易所已成交，回报丢了（12s 超时就会走到这里）
        #   2. 有人在手机 App 上手工买卖
        #   3. 交易所侧强平/风控
        #
        # 位置放在【接管/平账完成之后】：先把本地认知修到和交易所一致，
        # 再停下来。反过来的话，硬停会让 reconcile 提前返回，
        # 本地账永远修不正，而持仓还在交易所上跑。
        if self.guard is not None:
            why = ("实盘对账不一致：数据库 %+.6f(%s)，交易所 %+.6f —— "
                   "已按交易所修正，停止开新仓等待人工核查"
                   % (db_qty, db_side or "空仓", ex_qty))
            # ★ 这里曾经是 guard.notify("一句话") —— 少传一个参数（第 129 轮）。
            #
            # 生产上的 notifier 是 feishu_notifier() 返回的 _send(title, body)，
            # 两个位置参数都必填，于是每一次"对账不一致"都在这里抛
            #   feishu_notifier.<locals>._send() missing 1 required positional argument: 'body'
            #
            # 后果不是"少发一条消息"：异常直接冒泡出 reconcile_live()，
            # 下面是 mark_reconciled()，再下面（周期对账）只会记一条
            # reconcile_failed 就继续——等于每 5 分钟报一次错、什么都没修好。
            # 生产实证：risk_events 里 16 条 reconcile_failed，全是这个字符串
            # （11-11 14 次 + 11-12 2 次）。
            #
            # 为什么 position_mismatch(why) 自己够了、这条是多余的：
            # 它内部走 _halt_hard -> _event(alert=True)，_event 里已经
            # 用【正确的两个参数】发过飞书，而且已经 try/except 包好。
            # 重复发一遍只会让同一件事在群里出现两次。
            #
            # 但"删掉多余的 notify"还不够：通知这条路以后换个实现
            # （webhook 超时、DNS 失败、模板 KeyError）仍然可能抛，
            # 而它【绝不能反过来把对账搞崩】——对账修的是本地账本，
            # 是全系统唯一敢说"我到底有多少仓"的地方。
            # 所以 position_mismatch 这一整段也兜住：账必须先修对。
            #
            # ★ 2026-11-19：硬停之前必须**先分清这笔错配是谁造成的**。
            #
            #   用户 2026-11-13 在手机上手工买了 0.15 BTC（harvest 到 four
            #   次接管：0.06 → 0.09 → 0.12 → 0.15），系统每轮接管完就
            #   position_mismatch() 硬停自己一次，而 kind="mismatch" **永不
            #   自动解除** —— 于是那一次硬停一直挂到 11-19，期间策略再也
            #   没有开过仓。用户的原话：「不带 AI- 的七笔单是我人工做的，
            #   而且我还会持续做」。
            #
            #   这与 §13.2 的约定直接冲突：手工仓只接管、不判策略出场、
            #   保留熔断止损，**不是故障**。所以这里按错配差额的**成交来源**
            #   分流：差额全由系统外的成交解释 → 人工仓 → 只记事件+通知；
            #   只要有一笔是我们自己的（`AI-` 前缀）→ 真漏账 → 照旧硬停；
            #   查不到/凑不出差额 → unknown → 照旧硬停（保守，不猜）。
            # ★ 分流结果在 else-分支里已经算过一次了（拆仓后接管前必须先判来源）。
            #   这里复用，不重复查交易所成交：查两次不只浪费配额，
            #   两次之间成交可能变化，会出现「接管用的是 A 结论、硬停用的是
            #   B 结论」这种自相矛盾。
            if prov_pre is None:
                delta = ex_qty - db_qty
                prov, used = await self._classify_position_delta(delta)
            else:
                prov, used = prov_pre, used_pre
                delta = ex_qty - db_qty
            if prov == PROV_MANUAL:
                detail = ("已接管人工持仓：数据库 %+.6f(%s)，交易所 %+.6f，"
                          "差额 %+.6f 全部来自 %d 笔系统外成交（%s~%s）"
                          % (db_qty, db_side or "空仓", ex_qty, delta,
                             len(used),
                             _fmt_ms(used[-1]["ts"]) if used else "?",
                             _fmt_ms(used[0]["ts"]) if used else "?"))
                logger.warning("[对账] %s —— 按 §13.2 约定接管，"
                               "不判策略出场、保留熔断止损，**不硬停**", detail)
                try:
                    self.db.log_risk("manual_position_adopted", detail)
                except Exception:
                    logger.error("人工仓事件落库失败（账已修正，继续）",
                                 exc_info=True)
                if self.guard is not None:
                    try:
                        self.guard.manual_position_adopted(detail)
                    except Exception:
                        logger.error("人工仓接管的通知通道失败（账已修正，继续）",
                                     exc_info=True)
            else:
                if prov == PROV_UNKNOWN:
                    logger.error("[对账] 无法判定差额 %+.6f 的来源"
                                 "（成交历史不足或查询失败），按未知处理并硬停",
                                 delta)
                try:
                    self.guard.position_mismatch(why)
                except Exception:
                    logger.error("对账不一致的告警通道失败（账已按交易所修正，继续）",
                                 exc_info=True)

        # 对账已经完成，而且我们【已经站在趋势里】——没有"补上车"这回事了。
        # 不清这个标志的话它会一直挂着（接管路径不经过 _open，而 _open 才是
        # 正常清标志的地方），于是这笔仓一旦被 25% 熔断平掉、价格却还在
        # SMA 上方，startup_entry 会立刻把仓补回去，绕过
        # "必须等下一根 4h"的规则。止损后立刻重新进场是最坏的行为。
        # ★ 判据必须包含人工仓（2026-11-24 拆仓）：
        #   接管来的仓现在在 `self.adopted_pos` 里，只看 `self.pos` 会让
        #   `need_reconcile` 永远挂着 —— 而那笔仓一旦被熔断平掉、
        #   价格还在 SMA 上方，`startup_entry` 会立刻补回来，
        #   绕过「必须等下一根 4h」。止损后立刻重新进场是最坏的行为。
        if (self.pos or getattr(self, "adopted_pos", None)) \
                and getattr(self.trend, "need_reconcile", False):
            self.trend.mark_reconciled()

    async def _sync_live_ledger(self) -> None:
        """抓真账（成交 → 回合 → 打分 → 落库）。失败只记日志。

        单独抽成方法是为了让 reconcile_live() 里能一句调用，
        也让 _close() 之后能复用同一段逻辑（平仓完立刻刷新，
        否则用户要等最多 5 分钟才看见这一单）。
        """
        if getattr(self, "_ledger_busy", False):
            return          # 上一轮还没跑完（首次同步可能要拉几次 K 线）
        tdb = getattr(self, "db", None)
        ex = getattr(self, "ex", None)
        if tdb is None or ex is None:
            return
        if not hasattr(ex, "user_trades"):
            return          # 现货执行器没有这个接口，跳过而不是报错
        self._ledger_busy = True
        try:
            from ..live_ledger import sync as ledger_sync
            r = await ledger_sync(ex, tdb)
            logger.info("[真账] 成交 %d 笔 → 回合 %d 个（新算 %d 个）",
                        r.get("fills", 0), r.get("rounds", 0),
                        r.get("analyzed", 0))
        except Exception:
            logger.error("真账同步失败（不影响交易）", exc_info=True)
        finally:
            self._ledger_busy = False

    def _on_bar_sync(self, bars_by_iv, last_bar) -> None:
        """feed 的回调在事件循环里同步调用；这里只做调度，不阻塞采集。"""
        try:
            asyncio.get_event_loop().create_task(self._on_bar(bars_by_iv))
        except Exception:
            logger.error("调度决策失败", exc_info=True)

    # ---------------- 主流程 ----------------
    async def _on_bar(self, bars_by_iv) -> None:
        if self._lock.locked():
            return                      # 上一轮还没跑完，丢弃本轮（避免排队雪崩）
        async with self._lock:
            try:
                await self._step(bars_by_iv)
            except Exception as e:
                self.last_error = str(e)[:200]
                self.last_error_ts = int(time.time() * 1000)
                logger.error("决策步骤异常", exc_info=True)
                self.db.log_risk("engine_error", str(e)[:300])

    def _watch_executor_halt(self) -> None:
        """执行器熔断的状态跳变 -> 走 Guard 的告警通道。

        为什么需要（2026-11-02）：
        halted 只是执行器（spot.py / live.py）的内存属性；sentinel 只读数据库，
        健康自检只看进程和 K 线。于是熔断发生时【没有任何人会收到通知】，
        只能靠人去打开看板——而 7x24 运行不会有人一直盯着看板。

        三条行为要求：
          1. 第一次熔断立刻告警（含飞书）；
          2. 同一原因不重复告警——告警刷屏会把自己淹掉，等于没有告警；
             但原因【变了】说明是另一回事，隔 5 分钟再报一次；
          3. 解除也要告警，否则人会去查一个已经好了的系统。
        """
        cur = str(getattr(self.ex, "halted", "") or "")
        prev = getattr(self, "_halt_seen", "")
        if cur == prev:
            return
        now_ms = int(time.time() * 1000)
        self._halt_seen = cur
        if cur:
            # 仍在熔断、只是原因变了：限流。原因里通常带动态数字
            # （期望/实际持仓），不限流就会每根 3s K 线报一次。
            if prev and now_ms - getattr(self, "_halt_alert_ts", 0) < 300000:
                return
            self._halt_alert_ts = now_ms
            detail = ("执行器熔断，已停止开新仓（减仓仍会放行）：%s" % cur[:280])
            logger.critical("[熔断] %s", detail)
            if self.guard is not None:
                self.guard.executor_halt(detail)
            else:
                self.db.log_risk("executor_halted", detail[:300])
        else:
            detail = "执行器熔断已解除，恢复开仓。原原因：%s" % str(prev)[:280]
            logger.warning("[熔断] %s", detail)
            if self.guard is not None:
                self.guard.executor_halt(detail, recovered=True)
            else:
                self.db.log_risk("executor_halt_recovered", detail[:300])

    async def _step(self, bars_by_iv) -> None:
        # 熔断监视放在最前面：_step 在拿不到权益时会提前 return，
        # 而「执行器熔断 + 权益查询也失败」恰恰是最需要告警的组合。
        self._watch_executor_halt()
        # 查余额失败不能中断整个决策步骤。
        #
        # 以前 equity() 是第一行且没有保护：它一抛异常，后面的出场判断、
        # 周期对账、风控更新全部跳过。实盘现货里这是 HTTP 请求（12s 超时），
        # 而交易所 API 最容易超时的时刻正是行情剧烈波动的时候——
        # 也就是【最需要按时出场】的时候。持仓会因此被套住。
        #
        # 处置原则（有意的不对称）：
        #   权益过期 -> 照常出场（卖多少是已知的，不需要权益）
        #   权益过期 -> 拒绝入场（不知道有多少钱，就不能下注）
        eq = None
        try:
            eq = await self.ex.equity()
            equity = float(eq["equity"])
            self._last_equity = equity
            self._equity_stale = False
        except Exception as e:
            equity = getattr(self, "_last_equity", None)
            was_stale = getattr(self, "_equity_stale", False)
            self._equity_stale = True
            if not was_stale:
                logger.error("查询权益失败，改用上一次已知值（%.2f）。"
                             "出场照常执行，入场暂停。原因：%s",
                             equity if equity is not None else -1.0, e)
                self.db.log_risk("equity_stale", str(e)[:300])
            if equity is None:
                # 从启动到现在从没成功拿到过权益：没有任何可信的仓位基础
                logger.error("从未成功获取权益，本轮跳过（%s）", e)
                return

        price = float(self.feed.agg.current(3000).close) if self.feed.agg.current(3000) else 0.0
        if not price:
            return
        now_ms = int(time.time() * 1000)
        roll_day(self.risk, now_ms)

        # ★★ A3（2026-11-25 交易域审查）：用**这一次** equity() 里
        #   `position()` 已经拿到的真实爆仓价去刷新缓存，短 TTL。
        #
        #   改之前：`_live_liq_price` 自己缓存 5 分钟，而 `equity()` 每 tick
        #   都已经调了一次 `position()`（为拿 position_qty 按账截断）——
        #   同一份事实取两次、还把新鲜的那次丢掉，只留一个 5 分钟前的。
        #   最该新鲜的数（P0 保命用的爆仓距离）反而是链上最旧的。
        #
        #   刷新逻辑单独抽在 `_seed_liq_cache_from_equity`：写在 `_step`
        #   内联时没有任何测试能证明它真的跑到了（变异验证实测：把这一支
        #   改成 `if False:` 测试依然全绿）。抽出来之后可以被直接断言。
        self._seed_liq_cache_from_equity(eq, now_ms)

        if self.guard is not None:
            # 心跳由 update() 自己维护。
            # 原来这里先手工盖一次心跳、再 update(..., now_ms)，
            # 判定用的是同一个 now_ms，第 3 条闸（引擎卡住）恒不成立——
            # 一条结构上永远不会触发的刹车。tests/test_guard_stall_is_real.py 钉住。
            self.guard.update(equity, self.feed.src, now_ms)

        if eq is not None:
            self.db.log_equity({"ts": int(now_ms // 1000) * 1000,
                            "balance": eq["balance"],
                            "unrealized": eq["unrealized"], "equity": equity,
                            "position_qty": eq.get("position_qty", 0.0),
                            "leverage": (abs(eq.get("position_qty", 0.0)) * price / equity)
                                        if equity else 0.0,
                            "paper": 1 if self.ex.name == "paper" else 0})

        # 周期对账：实盘运行时每 5 分钟核对一次真实持仓。
        #
        # 为什么必须有：以前 reconcile_live() 只在启动时跑一次，之后再也不对账。
        # 而持仓和数据库对不上有四条常见路径：
        #   1. 下单请求超时但交易所其实已成交（回报丢了）——12s 超时会走到这里
        #   2. 用户自己在手机 App 上买了/卖了
        #   3. 部分成交
        #   4. 交易所强平/风控
        # 任何一条发生后，引擎会一直按错误持仓跑，直到下次重启。
        #
        # 实测过后果：交易所已有 0.002 BTC，引擎以为空仓，于是又买 0.00128，
        # 实际持仓 0.00328 而引擎只记 0.00128——止损只保护 39%，其余裸奔。
        #
        # 成本：每 5 分钟一次 /api/v3/account，一天 288 次，远低于限频。
        if self.ex.name in ("live", "spot"):
            last = getattr(self, "_last_reconcile_ms", 0)
            if now_ms - last >= 300000:
                # 先写时间戳再对账：对账本身抛异常时，也避免每根 K 线都重试
                self._last_reconcile_ms = now_ms
                try:
                    await self.reconcile_live()
                except Exception as e:
                    # 对账失败不能中断交易：拿不到真实持仓时，
                    # 保持现有认知比停下来更安全（停下来等于放弃止损）。
                    logger.error("周期对账失败，本轮沿用现有持仓认知：%s", e)
                    self.db.log_risk("reconcile_failed", str(e)[:200])

        # ★★ BUS-C 观察期：让 `position_guard.decide()` 第一次在生产环境跑起来，
        #    **只算不做**（分歧写 decision，绝不产生任何动作）。
        #
        #    为什么必须放在所有策略分流**之前**：趋势/挂单/剥头皮三条链各自
        #    直接 return，钩子放在任何一个分支里都只覆盖一个策略。
        #    而观察期要回答的问题是「守卫的 P0→P5 若接管出场，会和现有逻辑
        #    分歧多少次」—— 只覆盖一条链，答案就是错的。
        #
        #    `position_guard.py` 是完整的持仓管理系统（P0 保命 → P1 止损 →
        #    P2 结构 → P3 分批止盈 → P4 加仓 → P5 持有）+ `check_invariants`，
        #    测试齐全，但**生产零调用**（grep check_invariants/GuardPosition
        #    在引擎里 0 命中）。BUS-D 要把它接线成唯一的出场出口，
        #    前提是先知道它和现状差多少。
        #
        # ★ F7（2026-11-24 审查）：观察者与人工仓必须用**同一个爆仓价来源**。
        #   以前观察期用配置推算、人工仓用交易所真实 —— 同一个账户净仓
        #   只有一个真实爆仓价，两套数字算出来的"缓冲几倍"不可比，
        #   观察期的分歧统计就失去意义。这里先暖一次缓存（内部 5 分钟节流），
        #   两个调用方随后都读 `_liq_cache`。
        if self.pos and self.guard is not None:
            try:
                await self._live_liq_price()
            except Exception as e:
                logger.warning("[守卫] 预热真实爆仓价失败（本轮用推算值）：%s", e)
        try:
            self._guard_observe(bars_by_iv, price, now_ms)
        except Exception as e:
            # 观察者坏了绝不能影响交易。它是纯读的，最坏结果只是少一条对照记录。
            logger.warning("[守卫观察] 本轮跳过（不影响交易）：%s", e)

        # 趋势策略走独立分支：它的开平仓条件、仓位算法、止损逻辑都和剥头皮不同，
        # 混在一条链上会让两边互相污染（而且剥头皮那套可达性门会直接拒掉趋势单）。
        #
        # 【有意为之，不要"修复"】趋势分支不调用 check_risk_state，因此：
        #   - 连亏冷却对趋势单不生效
        #   - 日亏损上限对趋势单不生效
        # 理由（实测数据，见 HANDOFF 六点三十二）：
        #   趋势策略胜率 15.5%（33 胜 / 180 负），平均 5.5 笔才赢一次，
        #   历史上最长连亏 13 笔。而 max_consecutive_losses=3 / cooldown_min=30
        #   是剥头皮时代留下的参数——真让它生效，会在最需要信号的时候把单掐掉。
        #   实测：漏掉最好 1 笔进场 = 总收益少 46%，漏 5 笔 = 6.84 年白干。
        #   日亏损上限同样够不着：单笔最大亏损约 11%，两笔之间隔好几天。
        # 已有测试锁定此行为：tests/test_trend_risk_gate.py
        # ★★ 人工仓（接管来的）的出场管理：**任何策略下、每个 tick 都必须跑**。
        #
        #   2026-11-24 拆仓后人工仓放在 `self.adopted_pos`，而
        #   `_step_trend` / `_manage` / 剥头皮链全都只读 `self.pos`。
        #   如果只在挂单链里管人工仓，那么把 strategy 换成趋势/剥头皮时，
        #   人工仓就变成**没有任何人管出场**的裸仓 —— 比重构前更危险：
        #   重构前它至少会被策略规则管（虽然用错了规则），
        #   而现在连错的规则都轮不到它，熔断止损也不会执行。
        #   tests/test_equity_outage.py 逮住的就是这个（该测试期望
        #   「价格跌破后必须卖」而一笔卖单都没有）。
        #
        #   ★★ 2026-11-24 全链路审查（F2）：这里原来写着
        #      `and not self._is_pending()`，理由是"挂单链自己会调用它，
        #      避免同一根 K 判两次"。实际后果是 —— 生产跑的正是
        #      `strategy=pending`，于是唯一的调用点在 `_step_pending` 里，
        #      而那里**先过了 5m 节流才轮到它**（`ts <= self._pending_5m_ts` 就 return）。
        #
        #      也就是说：22 倍杠杆、止损 1.618% 的仓位，
        #      **止损与爆仓保护每 5 分钟才判一次**，不是 3 秒。
        #      BUS-D 之前这里只判"结构是否失效"，慢 5 分钟是判断迟滞；
        #      BUS-D 之后这里扛着 P0 保命，慢 5 分钟就是真实的敞口。
        #
        #      修法不是"两个地方都跑"（那才是同一根 K 判两次），
        #      而是**只留一个调用点，放在这里**：3 秒粒度、与策略无关、
        #      `_step_pending` 不再调用它。
        if self._is_adopted():
            # ★★ 2026-11-25（用户裁决）保护单必须在**接管那一刻**就存在，
            #    不能等「接管之后第一根已收盘 15m」。
            #
            #    根因：`_sync_adopted_stop_order` 原来只挂在
            #    `_step_adopted_exit` 的两处（`plan.is_noop` 分支与出场之后），
            #    而这两处都在「没有接管之后的已收盘 15m 就 return」之后。
            #    于是从接管到挂单之间，最长 = 一根 15m K 线 + 一个 tick
            #    ≈ 15 分钟，交易所侧**完全没有保护**，进程内 3 秒轮询是唯一
            #    防线 —— 进程一挂就是裸奔。
            #
            #    用户的理由（原话）：「我们做的是抄底行为，可能在 10 分钟内
            #    发生超级大波动」。15 分钟的空窗比一次死机还长，而且是
            #    22 倍杠杆：1.6% 的逆向插针就是全部保证金。
            #
            #    放这里而不是放进 `_step_adopted_exit` 的开头：那函数里
            #    剩下的判断（P0 保命 / P1 止损）确实需要结构数据才算，而
            #    「挂一张保护单」只依赖 stop 和 qty —— 接管时两者就有。
            #    把**无条件该做的事**摆在有条件判断前面，顺序才不会反过来。
            _ap, _ = self._adopted_slot()
            if _ap:
                try:
                    await self._sync_adopted_stop_order(_ap, price)
                except Exception as e:
                    logger.error("[接管] 保护单维护失败（下一 tick 重试）：%s", e)
            try:
                _b15 = self._to_pending_bars((bars_by_iv or {}).get(900000))
                await self._step_adopted_exit(bars_by_iv, _b15, price, now_ms,
                                             log_quiet=True)
            except Exception as e:
                logger.error("[接管] 人工仓出场判断失败：%s", e, exc_info=True)
                try:
                    self.db.log_risk("adopted_exit_error", str(e)[:200])
                except Exception:
                    pass
        elif self._adopted_stop_state().get("oid"):
            # ★ A1 看门狗：人工仓已经不在了，但交易所侧还挂着我们那张
            #   保护性止损单 —— 必须撤掉。
            #
            #   什么时候会走到这里：仓位在**别处**消失（交易所强平、
            #   用户在手机上平掉、对账修正），没经过 `_adopted_close`。
            #   那种情况下没人撤单，单子会一直挂着：reduceOnly 触发也开不出
            #   反向仓（不会造成新的敞口），但会白占保证金和挂单数，
            #   而且下一笔仓万一 entry_ts 撞上就会复用同一个 clientOrderId。
            #   放在 `_step` 而不是 `_step_adopted_exit`：后者在无仓时
            #   根本不会被调用。
            try:
                await self._cancel_adopted_stop_order("人工仓已不在")
            except Exception as e:
                logger.error("[接管] 撤遗留止损单失败：%s", e)

        if self._is_chan():
            await self._step_trend(bars_by_iv, price, equity, now_ms)
            return

        # ★★ 挂单策略（strategy="pending"）必须**在**剥头皮链之前分流。
        #
        # 为什么这是一个安全问题的修复而不是加功能：
        #   在 2026-11-17 之前，`strategy` 的白名单里已经有 "pending"，
        #   但 `_step` 里没有任何 `_is_pending()` 分支 —— 于是配了 pending
        #   的环境会**直接落进下面那段剥头皮逻辑**：3s 动量 + 可达性门，
        #   那是一个已被回测证伪的负期望策略。
        #   不报错、不告警，日志里能看到策略是 pending，实际跑的是 scalp。
        #   这正是这个项目反复吃亏的「同一件事两个来源」的变体：
        #   配置说 A，执行链不知道 A 的存在。
        if self._is_pending():
            await self._step_pending(bars_by_iv, price, equity, now_ms)
            return

        # ★ 只有人工仓、没有系统仓：**不许入场**（2026-11-24 拆仓）。
        #   剥头皮/旧链同理：`self.pos` 为空不代表真的空仓，
        #   人工仓的出路由 `_step_adopted_exit` 管，这里只挡开仓，
        #   免得在用户的未验证仓位上再叠一笔系统仓。
        if getattr(self, "adopted_pos", None) and not self.pos:
            self._log_decision(
                bars_by_iv, price, equity, now_ms, None, "hold",
                "接管的人工仓在管（%s %.6f @ %.1f），不新开系统仓"
                % (self.adopted_pos.get("side"),
                   float(self.adopted_pos.get("qty") or 0.0),
                   float(self.adopted_pos.get("entry") or 0.0)))
            return

        # 1) 有仓位 -> 只管仓位
        if self.pos:
            closed = await self._manage(price, bars_by_iv)
            if closed:
                return
            self._log_decision(bars_by_iv, price, equity, now_ms, None, "hold",
                               "持仓中：%s %.3f @ %.1f，止损 %.1f 目标 %.1f"
                               % (self.pos["side"], self.pos["qty"], self.pos["entry"],
                                  self.pos["stop"], self.pos["target"]),
                               gates=[], risk_info=self._pos_risk(price, equity))
            return

        # 2) 空仓 -> 评估开仓
        feats = feat_mod.compute(bars_by_iv, price)
        feature_time = feats.get("feature_time", 0)
        if feature_time > now_ms:
            self.db.log_risk("lookahead", "feature_time %s > now %s" % (feature_time, now_ms))
            return

        if self.guard is not None:
            g_ok, g_why = self.guard.can_open()
            if not g_ok:
                self._log_decision(bars_by_iv, price, equity, now_ms, None, "skip",
                                   "无人值守闸拦截：" + g_why,
                                   gates=[{"gate": "无人值守闸", "ok": False, "detail": g_why}],
                                   features=feats)
                return
        ok_risk, why = check_risk_state(self.risk, self.tcfg, now_ms, equity)
        if not ok_risk:
            self._log_decision(bars_by_iv, price, equity, now_ms, None, "skip", "风控拦截：" + why,
                               gates=[{"gate": "风控状态", "ok": False, "detail": why}],
                               features=feats)
            return

        sig = sig_mod.detect(feats, bars_by_iv, self.tcfg)
        if not sig:
            _px = feats.get("price", 0) or 1.0
            _hi = feats.get("swing_high_3s", 0) or _px
            _lo = feats.get("swing_low_3s", 0) or _px
            d_hi = (_px - _hi) / _px * 100.0
            d_lo = (_px - _lo) / _px * 100.0
            watch = ("盯盘中｜3m突破：距上 %.3f%%/距下 %.3f%%，3m量z %.2f(需≥%.1f)｜"
                     "3s动量：%.3f%%(需±%.2f%%) 量%.2fx(需≥%.2f)｜距20根3s高 %.3f%%/低 %.3f%%"
                     % (feats.get("dist_to_break_hi_pct", 0),
                        feats.get("dist_to_break_lo_pct", 0),
                        feats.get("vol_z_3m", 0), self.tcfg.breakout_vol_z,
                        feats.get("impulse_pct", 0), self.tcfg.micro_move_pct,
                        feats.get("impulse_vol_x", 0), self.tcfg.micro_vol_x,
                        d_hi, d_lo))
            self._log_decision(bars_by_iv, price, equity, now_ms, None, "watch",
                               watch, gates=[], features=feats)
            return

        plan = build_plan(sig["side"], price, sig["hint_stop"], equity, self.tcfg, feats,
                          account_mmr=self.tcfg.account_mmr)
        risk_info = {"qty": plan.qty, "notional": plan.notional, "leverage": plan.leverage,
                     "stop": plan.stop, "target": plan.target, "stop_pct": plan.stop_pct,
                     "tp_pct": plan.tp_pct, "risk_usdt": plan.risk_usdt,
                     "liq_distance_pct": plan.liq_distance_pct, "cost_pct": plan.cost_pct}
        if not plan.ok:
            self.block_stats[sig["name"]] = self.block_stats.get(sig["name"], 0) + 1
            self._log_decision(bars_by_iv, price, equity, now_ms, sig["name"], "skip",
                               "计划被拒：" + plan.reason, gates=plan.gates,
                               features=feats, risk_info=risk_info, signal_reason=sig["reason"])
            return

        await self._open(sig, plan, price, equity, now_ms, feats, bars_by_iv)

    # ---------------- 开仓 ----------------
    # ------------------------------------------------------------------
    # 趋势策略分支
    # ------------------------------------------------------------------
    async def _step_trend(self, bars_by_iv, price, equity, now_ms) -> None:
        """4h 只做多趋势跟随的执行分支。

        每根 4h 收盘才有判断点；3s 心跳只用来：喂 K 线、盯熔断止损、记录状态。
        """
        # 两个策略共用这一个执行分支，因为它们的执行语义相同：
        # 空仓判入场、持仓判出场、不做 T+0 止损、不套连亏冷却。
        # 主周期（1h 或 4h）由策略自己声明，下面的喂数逻辑一律照它走——
        # 写死 14400000 的话，唐奇安会永远拿不到自己要的 1h K，而且不会报错。
        if self.trend is None:
            if self._is_donchian():
                from . import donchian as don_mod
                self.trend = don_mod.DonchianStrategy(
                    self.tcfg, symbol=self.tcfg.symbol,
                    fapi_base=self.feed.cfg.market.fapi_base,
                    proxy=self.feed.cfg.market.http_proxy,
                    exec_market=self.tcfg.exec_market)
            else:
                from . import trend as trend_mod
                self.trend = trend_mod.TrendStrategy(
                    self.tcfg, symbol=self.tcfg.symbol,
                    fapi_base=self.feed.cfg.market.fapi_base,
                    proxy=self.feed.cfg.market.http_proxy,
                    exec_market=self.tcfg.exec_market)
            n = self.trend.seed_from_rest()
            if n < self._chan_warmup():
                logger.warning("策略历史不足（%d < %d），暂不交易", n, self._chan_warmup())
            self._seed_retry_ts = 0

        # 补数失败要能自愈：只在启动时补一次的话，一旦那次网络抖动，
        # 策略会一直"历史不足"——而 4h K 线每 4 小时才长一根，攒到 200 根要 33 天。
        # 这就是典型的"静默停机一个月"，必须定期重试。
        if len(self.trend.bars) < self._chan_warmup():
            now_s = int(time.time())
            if now_s - getattr(self, "_seed_retry_ts", 0) > 1800:
                self._seed_retry_ts = now_s
                n = self.trend.seed_from_rest()
                logger.warning("趋势历史仍不足，重试补数：%d 根（每 30 分钟重试）", n)

        # 把已收盘的 4h K 喂给策略。
        #
        # 优先用【交易所官方 4h K】而不是本地聚合出来的 bar：
        # 回测就是用官方 K 线跑的，线上若用本地聚合，等于让线上和回测
        # 吃不同构造的输入。2026-11-02 实测本地 bar 的成交量只有官方的
        # 0~7%（聚合器每次重启清零，一个 4h 桶只攒到本次运行期间的成交），
        # 收盘价则取决于"收盘那一刻我们是否在线"——第 81 轮 WS 全程静默，
        # 那种情况下本地 close 只是"我们最后收到的成交价"。
        #
        # 取不到（网络/限频）就退回本地 bar，绝不因为补数据失败而不判断。
        # 主周期从策略实例上取，不写死。config.INTERVALS_MS 里没有 1h，
        # 所以唐奇安在本地聚合器里拿不到 1h K（b1 恒为空），只能靠
        # fetch_official_bar 拉官方 K——那条路必须通，通了就够。
        _civ = int(getattr(self.trend, "iv", 14400000) or 14400000)
        _ckey = _civ if _civ in bars_by_iv else (14400000 if _civ == 14400000 else 0)
        b4 = (bars_by_iv.get(_ckey) or []) if _ckey else []
        if b4:
            local = b4[-1]
            t_new = int(getattr(local, "open_time", 0) or 0)
            prev_t = int(getattr(self, "_trend_fed_b4_t", 0) or 0)
            bar_to_feed = local
            # 只在"出现了一根没喂过的 4h K"时才多花一次 REST，不是每 3 秒。
            # 另外只对【刚收盘不久】的 K 去取官方数据：
            # 补历史时（seed_from_rest）本来就用的官方 K 线，
            # 这里再查一遍没意义，而且会让测试/回放对着假时间戳发真请求。
            fresh = (now_ms - t_new) < 2 * _civ
            if (fresh and t_new > prev_t
                    and t_new > int(getattr(self.trend, "last_eval_bar", 0) or 0)):
                try:
                    off = await asyncio.to_thread(self.trend.fetch_official_bar, t_new)
                except Exception as e:
                    logger.warning("取官方 4h K 异常，退回本地 bar：%s", e)
                    off = None
                if off is not None:
                    # 每根 4h K 都要能说清"喂进去的是哪一份数据"。
                    # 否则这个修复在线上不可观测：收盘价两种口径本来就一致，
                    # SMA 完全区分不出来（第 83 轮就用 SMA 比对做过一次
                    # 看似成功、实则区分不了的验证）。真正的差别在成交量：
                    # 官方 8558.161 vs 本地 117.730。所以必须把量记下来。
                    self._trend_bar_src = "official"
                    self._trend_bar_vol = (float(off.volume),
                                           float(getattr(local, "volume", 0.0) or 0.0))
                    # 官方与本地收盘价差异过大时记一笔，便于事后核对口径
                    d_bp = (float(local.close) - float(off.close)) / float(off.close) * 10000.0
                    logger.info(
                        "喂入 4h K(官方) t=%s 收盘=%.1f 量=%.3f（本地量=%.3f，差 %.1f bp）",
                        time.strftime("%m-%d %H:%M", time.localtime(t_new / 1000)),
                        float(off.close), float(off.volume),
                        float(getattr(local, "volume", 0.0) or 0.0), d_bp)
                    if abs(d_bp) > 20.0:
                        logger.warning(
                            "本地 4h 收盘与官方差 %.1f bp（本地 %.1f / 官方 %.1f），以官方为准",
                            d_bp, float(local.close), float(off.close))
                    bar_to_feed = off
                else:
                    # 取不到官方 K 也要留痕：否则"跑的是本地残缺 bar"
                    # 在线上完全看不出来，和 PD 正常时长得一模一样。
                    self._trend_bar_src = "local(回退)"
                    self._trend_bar_vol = (float(getattr(local, "volume", 0.0) or 0.0), 0.0)
                    logger.warning(
                        "取不到官方 4h K（t=%s），本根改用本地聚合 bar（量 %.3f）",
                        time.strftime("%m-%d %H:%M", time.localtime(t_new / 1000)),
                        float(getattr(local, "volume", 0.0) or 0.0))
                self._trend_fed_b4_t = t_new
            self.trend.on_bar(bar_to_feed)

        # 看板快照必须【无条件】先建，位置在闸门检查和持仓/空仓分支之前。
        #
        # 以前它写在空仓分支的闸门检查【之后】，于是两种最常见的情况看板是瞎的：
        #
        #   1) 闸门拦截（数据源 DOWN / 引擎卡住 / 硬熔断）
        #      闸门一拦就 return，快照不更新，trend_state() 返回 ready=False，
        #      大屏渲染成「趋势策略预热中：4h K线 0/200」。
        #      而真实原因是"数据源 DOWN 已 60s，禁止开仓"——
        #      用户看到"还在加载"，会一直等一个不会来的完成。
        #
        #   2) 持仓中
        #      空仓分支根本不走，快照永远不更新，t.pos 一直是空的。
        #      于是大屏的持仓区（qty/成本/浮盈/止损/持有时长）永不出现，
        #      而这些恰恰是用户最想知道的。
        #
        # 快照是纯展示数据，不参与任何交易判断，
        # 放在最前面没有任何副作用，但能让看板在所有分支都说真话。
        self._trend_snapshot = self._build_trend_snapshot(price, equity)

        # --- 有仓位：只判出场 ---
        if self.pos:
            p = self.pos
            # 这里的 best/worst 不是装饰：trades 表的 mfe/mae 靠它们算。
            # 趋势模式不调用 _manage（它在 342 行之前就 return 了），
            # 而涨跌幅跟踪只写在 _manage 里——不在这里补上，
            # mfe 会恒为 0、mae 会变成"出场移动"，两列都是假数据。
            # 防御式：这些只是统计字段，拿不到就退回 price，
            # 绝不能让"记 MFE/MAE"这种附加功能把出场判断搞崩。
            try:
                _b = self.feed.agg.current(3000)
                _hi = float(getattr(_b, "high", price))
                _lo = float(getattr(_b, "low", price))
            except Exception:
                _hi = _lo = float(price)
            _dir = 1 if p["side"] == "long" else -1
            if _dir > 0:
                p["best"] = max(p.get("best", p["entry"]), _hi)
                p["worst"] = min(p.get("worst", p["entry"]), _lo)
            else:
                p["best"] = min(p.get("best", p["entry"]), _lo)
                p["worst"] = max(p.get("worst", p["entry"]), _hi)
            # 熔断止损是"一执行就不可逆"的动作，而本地价格在行情断线时是陈旧的。
            # 陈旧价可能假触发，把健康仓位按市价卖掉——之后要等下一次金叉才能回场，
            # 可能几周。所以触发时先向交易所要一次权威价格复核。
            if price <= p["stop"]:
                truth = await self._confirm_stop_price(price)
                if truth and truth > p["stop"]:
                    logger.warning("本地价 %.1f 触发熔断，但交易所价 %.1f 未触发，"
                                   "判定为行情陈旧，不执行", price, truth)
                    self.db.log_risk("stale_price",
                                     "本地 %.1f / 交易所 %.1f，已忽略熔断" % (price, truth))
                    price = truth
            stop_hit = price <= p["stop"]
            # ★ 接管来的仓【不判策略出场】（第 129 轮）。
            #
            # 判据见模块顶部 ADOPT_REASON 的注释：唐奇安的出场是
            # 「1h 收盘跌破 M 根新低」，那是【策略自己的进场依据】的镜像。
            # 一笔用户手工开的仓没有这个依据，用镜像规则去平它是无中生有。
            #
            # 实测（11-12）：用户 22:54 手工买入 0.05 BTC，22:57:51 被接管，
            # 紧接着在同一秒被平掉——因为 21:00 那根 1h 早就收在 75 根新低下方，
            # want_exit() 一看到 "below_lower 且 ready" 就成立。
            # 也就是说：接管之后【第一个 tick】就出场，用户根本没有时间反应。
            #
            # 保留的是熔断止损（上面那一段）：那是「本金快要没了」的兜底，
            # 不是策略判断。用户明确要求留它。
            adopted = self._is_adopted()
            ex = None if adopted else self.trend.want_exit(price)
            if stop_hit:
                # 熔断出局后，同一根 4h 内不再进场：否则交叉信号仍成立，
                # 会变成"止损->立即再进->再止损"的死循环。
                #
                # ★ 必须【落库】（第 126 轮）。
                #   这个字段原本只活在内存里（__init__ 里 = 0）。
                #   进程一重启：
                #       _trend_block_bar = 0                     <- 内存，归零
                #       trend.last_eval_bar = 最后一根已收盘 4h  <- seed_from_rest 会补
                #       trend.need_reconcile = True              <- __init__ 里设的
                #   于是闸门 last_eval_bar <= block_bar 变成
                #       999 <= 0 = False  -> 直接放行，
                #   而 startup_entry 看到"空仓 + 站在 SMA*(1+h) 上方"就立刻补开。
                #
                #   死循环：止损 -> 重启 -> 启动对账立刻补回 -> 再止损 -> ...
                #   每次 25%，两轮就把 100U 打成 56U。
                #
                #   而"重启"在这里不是理论风险：部署、OOM、哨兵重启都会发生，
                #   而且【行情剧烈波动时最可能重启】——正是熔断止损刚触发的时刻。
                # 只走 _set_block_bar：它同时管内存和落库。
                # 这里再直接赋一次值是多余的，而且会让「写内存」有两处——
                # 变异「落库但不设内存」第一轮真的活了，就是被这行掩盖掉的。
                self._set_block_bar(self.trend.last_eval_bar)
                return await self._close("熔断止损 %.1f（跌破 %.1f%%）"
                                         % (p["stop"], self.tcfg.trend_stop_pct),
                                         p["stop"], price, bars_by_iv)
            if ex:
                return await self._close(ex["reason"], price, price, bars_by_iv)
            st = self._chan_view(self.trend.state(price))
            # 唐奇安没有熔断止损（stop=0），这里若照抄"距熔断 X%"会算出
            # 一个 100% 的假数字。两种处境分开写，日志才说得清系统在做什么。
            if adopted:
                # 接管仓：绝不能照抄"出场线（下轨）距 X%"——那条线我们不判。
                # 看板/日志说"距下轨 3%"会让人以为引擎随时会平，
                # 而实际它不会动，除非跌到熔断止损。
                tail = (u"人工仓·不判策略出场｜距熔断 %.1f%%"
                        % ((price - p["stop"]) / price * 100) if p["stop"] > 0
                        else u"人工仓·不判策略出场｜无熔断止损")
            elif self._is_donchian():
                tail = "出场线（下轨）%.1f，距 %.2f%%" % (
                    st.get("lower") or 0.0,
                    (price - (st.get("lower") or price)) / (st.get("lower") or price) * 100)
            else:
                tail = "距熔断 %.1f%%" % ((price - p["stop"]) / price * 100)
            self._log_decision(
                bars_by_iv, price, equity, now_ms, "trend_hold", "hold",
                "持多 %.3f @ %.1f｜现价 %.1f｜%s %.1f（%s）｜%s"
                % (p["qty"], p["entry"], price,
                   ("%d根新高" % st["lookback"]) if self._is_donchian()
                   else ("SMA%d" % st["lookback"]),
                   st["sma"] or 0, self._hold_phrase(st), tail),
                gates=[], risk_info=self._pos_risk(price, equity))
            return

        # ★★ 只有人工仓、没有系统仓：**不许入场**（2026-11-24 拆仓）。
        #
        #   拆分之后 `self.pos` 只放系统仓，于是「人工仓在账上」这件事
        #   在入场判断眼里等于空仓 —— 引擎会照着趋势信号再开一笔，
        #   叠在用户的手工仓上。用户说的是「有人工单时不挂新单」，
        #   而且从风险角度这也是错的：那笔人工仓没有策略依据，
        #   在它上面加系统仓等于用一个未验证的仓位当基础放大敞口。
        #   出场管理已经由 `_step` 里的 `_step_adopted_exit` 负责，这里只挡开仓。
        if getattr(self, "adopted_pos", None):
            _ap = self.adopted_pos
            self._log_decision(
                bars_by_iv, price, equity, now_ms, "trend_hold", "hold",
                "接管的人工仓在管（%s %.3f @ %.1f），不新开系统仓"
                % (_ap.get("side"), float(_ap.get("qty") or 0.0),
                   float(_ap.get("entry") or 0.0)),
                gates=[], risk_info=None)
            return

        # 数据源 DOWN 时禁止开仓（REQUIREMENTS 的铁律：看不见行情就不许开枪）。
        # 之前趋势分支完全没有这道闸门——scalp 路径检查了 can_open，趋势路径没检查，
        # 等于"数据源监控"在最需要它的策略上是失效的。
        if self.guard is not None:
            g_ok, g_why = self.guard.can_open()
            if not g_ok:
                self._log_decision(bars_by_iv, price, equity, now_ms, "trend_entry", "skip",
                                   "开仓闸门拦截：" + g_why, gates=[], risk_info=None)
                return

        # --- 空仓：先做启动对账，再判常规入场 ---
        #
        # ★ 启动对账也必须受"本根 4h 内已熔断出局"的约束。
        #
        # 这个闸原来只加在下面的 want_entry 分支（第 626 行），而 startup_entry
        # 在它【之前】——于是漏掉了一条只在实盘重启时才走到的路径：
        #   进程带着持仓重启 -> trend.need_reconcile=True（__init__ 里设的）
        #   -> reconcile_live 接管仓位，【不经过 _open】，标志一直挂着
        #   -> 价格跌破 25% 熔断，_close 平仓并设上 _trend_block_bar
        #   -> 下一根 3s：空仓 + 价还在 SMA 上方 -> startup_entry 触发 -> 立刻补回
        # 也就是"止损 -> 立即再进 -> 再止损"的死循环，正是 block_bar 要防的。
        #
        # 这不是我的猜测：engine.py:949-952 的注释早就点名过这个危险，
        # 但那个防护写在 _open 里，接管路径不经过 _open，所以拦不住。
        if (self.trend.last_eval_bar
                and self.trend.last_eval_bar <= self._trend_block_bar):
            self._log_decision(bars_by_iv, price, equity, now_ms, "trend_wait", "hold",
                               "本根 4h 内已熔断出局，等下一根再评估（启动对账也受限）",
                               gates=[], risk_info=None)
            return
        rec = self.trend.startup_entry(price)
        if rec:
            plan = self._trend_plan(price, equity, rec)
            if plan.ok:
                await self._open(rec, plan, price, equity, now_ms, {}, bars_by_iv)
                return
            # 对账信号有了但计划不通过：必须明确记下来。
            # 不记的话会继续往下走到 want_entry（上穿事件已过去 -> None），
            # 最后记成"空仓等待：现价 >SMA"，把"买不成"伪装成"在等待"。
            # 这两种状态处置完全不同，而且哨兵靠这段文字识别故障。
            key = "recfail|%s|%d" % (plan.reason[:40], int(equity))
            if (key != getattr(self, "_trend_log_key", "")
                    or now_ms - getattr(self, "_trend_log_ts", 0) > 600000):
                self._trend_log_key = key
                self._trend_log_ts = now_ms
                self._log_decision(bars_by_iv, price, equity, now_ms,
                                   "trend_entry", "skip",
                                   "启动对账未能开仓：" + plan.reason,
                                   gates=[], risk_info=None)
            return
        st = self._chan_view(self.trend.state(price))
        if not st["ready"]:
            self._log_decision(
                bars_by_iv, price, equity, now_ms, "trend_wait", "hold",
                "等 %s K 线攒够：%d/%d" % (
                    self.trend.interval_name() if self._is_donchian() else "4h",
                    st["bars"], self._chan_warmup()),
                gates=[], risk_info=None)
            return
        if self.trend.last_eval_bar and self.trend.last_eval_bar <= self._trend_block_bar:
            self._log_decision(bars_by_iv, price, equity, now_ms, "trend_wait", "hold",
                               "本根 4h 内已熔断出局，等下一根再评估", gates=[], risk_info=None)
            return
        # 资金够不够建一手仓，必须和"信号到没到"分开说。
        # 之前无论权益多少都只写"空仓等待"，于是账户只有 0.53U 时，
        # 日志看起来像"在等信号"，实际是"就算信号来了也买不起"。
        # 这两种状态的处置完全不同，混在一起会让人等一个永远不会来的成交。
        short = self._affordability_note(price, equity)
        if short:
            key = "poor|%s|%d" % (int(equity), int(price / 100))
            if (key != getattr(self, "_trend_log_key", "")
                    or now_ms - getattr(self, "_trend_log_ts", 0) > 600000):
                self._trend_log_key = key
                self._trend_log_ts = now_ms
                self._log_decision(bars_by_iv, price, equity, now_ms,
                                   "trend_wait", "hold", short, gates=[], risk_info=None)
            return

        # 下单失败退避期内不重试。放在 want_entry 之前，
        # 是为了在那段时间里连"要不要开仓"都不判——省掉一次无用的下单试探。
        # 日志要说明白是退避，不是没信号：否则运维会以为策略在等条件，
        # 实际上它在等一个自己刚刚触发的失败计时器。
        if now_ms < getattr(self, "_order_retry_ts", 0):
            key = "backoff|%d" % (self._order_retry_ts // 1000)
            if (key != getattr(self, "_trend_log_key", "")
                    or now_ms - getattr(self, "_trend_log_ts", 0) > 600000):
                self._trend_log_key = key
                self._trend_log_ts = now_ms
                left = int((self._order_retry_ts - now_ms) / 1000)
                self._log_decision(bars_by_iv, price, equity, now_ms,
                                   "trend_wait", "hold",
                                   "下单失败退避中：%d 秒后再试（已连续失败 %d 次）"
                                   % (left, getattr(self, "_order_fail_count", 0)),
                                   gates=[], risk_info=None)
            return

        sig = self.trend.want_entry(price)
        if not sig:
            # 趋势策略一天只需要几次判断，3s 一次全写库会淹掉真正的信号
            sig_key = "watch|%s|%d" % (st["above"], int(st["sma"] or 0))  # 已归一化
            if (sig_key != getattr(self, "_trend_log_key", "")
                    or now_ms - getattr(self, "_trend_log_ts", 0) > 600000):
                self._trend_log_key = sig_key
                self._trend_log_ts = now_ms
                if self._is_donchian():
                    watch = ("空仓等待：现价 %.1f %s %d根新高上轨 %.1f（差 %.2f%%）"
                             "｜下一根 %s 收盘 %.1f 小时后"
                             % (price, ">=" if st["above"] else "< ",
                                st["lookback"], st["sma"],
                                (price - (st["sma"] or price)) / (st["sma"] or price) * 100,
                                self.trend.interval_name(),
                                self.trend.next_bar_eta_h()))
                else:
                    watch = ("空仓等待：现价 %.1f %s SMA%d %.1f（差 %.2f%%）"
                             "｜下一根 4h 收盘 %.1f 小时后"
                             % (price, "> " if st["above"] else "< ", st["lookback"],
                                st["sma"],
                                (price - (st["sma"] or price)) / (st["sma"] or price) * 100,
                                self.trend.next_bar_eta_h()))
                self._log_decision(
                    bars_by_iv, price, equity, now_ms, "trend_watch", "watch", watch,
                    gates=[], risk_info=None)
            return

        plan = self._trend_plan(price, equity, sig)
        if not plan.ok:
            self._log_decision(bars_by_iv, price, equity, now_ms, "trend_entry", "skip",
                               "计划被拒：" + plan.reason, gates=[], risk_info=None)
            return
        await self._open(sig, plan, price, equity, now_ms, {}, bars_by_iv)

    def _hold_phrase(self, st: dict) -> str:
        """持仓时的一句话状态——必须和【真的出场规则】一致。

        第九轮引入滞回带之后，这里原本写成两态：
            above ? "价在上方，继续持有" : "已跌破SMA，等下一根确认"
        但出场条件已经不是"跌破均线"，而是"跌破 SMA*(1-h)"。
        价格落在下沿与均线之间时，系统【仍然持有】，而那句话却说
        "已跌破SMA，等下一根确认"——把"还在持有"说成"准备出场"。

        用户要的是"我要知道你在做什么"。日志说反了，比不写更坏：
        事后复盘会以为出场信号早就触发过。
        """
        # 唐奇安的出场线是"下轨"，不是"SMA*(1-h)"。
        # 沿用同一句话会把一个不存在的规则说给用户听，
        # 而他正是靠这句话判断"系统现在是什么状态"。
        if self._is_donchian():
            if st.get("below_lower"):
                return "已跌破 %d 根新低下轨，本根收盘确认出场" % (
                    st.get("exit_m") or 0)
            return "价在下轨 %.1f 之上，继续持有" % (st.get("lower") or 0.0)
        if st.get("exit_above"):
            return "价在最下方出场线之上，继续持有"
        return "已跌破出场线 SMA*(1-h)，等下一根确认"

    def _chan_view(self, st: dict) -> dict:
        """把两个策略的 state() 归一化成决策分支认识的字段。

        ★ 这不是"整洁"，这是 2026-11-02 线上真实断过的故障：
        唐奇安刚上线，_step_trend 里还在读 st["above"] / st["sma"]，
        而唐奇安的 state() 根本没有这两个键 -> 每 3 秒一次 KeyError
        -> 被上层 except 吞成"决策步骤异常"。
        表现和历史上那两次一模一样：**系统看起来在跑（服务 active、看板有数据），
        实际上从未真正评估过一次**，而且日志里只有一行 KeyError。

        修法不是在每个读点加 .get(默认值)——那样会把"没这个字段"
        悄悄变成 0/False，策略于是拿一个假的上轨做判断，更坏。
        修法是让两个 state() 在一处被翻译成同一套语义字段，
        任何未来的读点都只认这一套。键名沿用趋势版（sma/above/lookback），
        因为它们已经是看板契约的一部分。
        """
        if not self._is_donchian():
            return st
        up = st.get("upper")
        dn = st.get("lower")
        out = dict(st)
        out["lookback"] = st.get("entry_n", 0)
        out["sma"] = up                      # 上轨就是唐奇安的"入场线"
        out["above"] = bool(st.get("above_upper"))
        out["entry_line"] = up
        out["exit_line"] = dn
        # 唐奇安没有滞回带，也没有"出场线在均线下方"这回事：
        # 出场判据是"收盘跌破下轨"，直接由 below_lower 表达。
        out["exit_above"] = not bool(st.get("below_lower"))
        out["hyst_pct"] = 0.0
        return out

    def _build_chan_snapshot(self, st: dict, price: float, equity: float) -> dict:
        """唐奇安版因果快照。

        字段语义刻意和 4h 版对齐（sma->upper/lower、entry_line、dist_pct…），
        这样大屏不必分成两套渲染逻辑，也保证了「回测验证的世界」与
        「线上展示的世界」是同一套数字。

        为什么必须单独写而不是硬套趋势版：趋势版读的是 st["sma"]/st["above"]，
        唐奇安根本没有这两个键，硬套会在第一次渲染时 KeyError，
        而 KeyError 会被上层吞成「快照构建失败」——大屏一片空白但系统在跑。
        """
        up, dn = st.get("upper"), st.get("lower")
        snap = {"mode": "donchian", "ready": st.get("ready", False),
                "bars": st.get("bars", 0), "lookback": st.get("entry_n", 0),
                "entry_n": st.get("entry_n", 0), "exit_m": st.get("exit_m", 0),
                "upper": up, "lower": dn,
                "sma": up,                      # 兼容旧看板字段名
                "price": price,
                "above": bool(st.get("above_upper")),
                "next_bar_h": self.trend.next_bar_eta_h(),
                "hold": bool(self.pos),
                "interval": self.trend.interval_name()}
        if getattr(self, "_trend_bar_src", ""):
            snap["bar_src"] = self._trend_bar_src
            ov, lv = getattr(self, "_trend_bar_vol", (0.0, 0.0))
            snap["bar_vol"] = round(float(ov), 3)
            snap["bar_vol_local"] = round(float(lv), 3)
        if st.get("ready") and up and dn:
            snap["entry_line"] = up
            snap["exit_line"] = dn
            snap["dist_pct"] = (price - up) / up * 100.0
            snap["dist_to_exit_pct"] = (price - dn) / dn * 100.0
            snap["need_pct"] = ((up - price) / price * 100.0) if price < up else 0.0
            snap["hyst_pct"] = 0.0
            if price < up:
                try:
                    _roll = self.trend.roll_forward_to_break(price)
                    snap.update(_roll)
                    # 上轨只用 high 计算、且滚动时把最老那根挤出去、
                    # 尾部补【现价】。价格不动时上轨最终收敛到现价本身，
                    # 而进场要求 收盘 > 上轨，所以"等通道自己下来"走不通。
                    if not _roll.get("roll_bars"):
                        snap["roll_never"] = True
                except Exception:
                    logger.warning("通道滚动推算失败", exc_info=True)
            lev = self._plan_leverage()
            snap["target_leverage"] = lev
            snap["exec_market"] = getattr(self.ex, "name", "paper")
            try:
                _sig = {"name": "donchian_preview", "reason": "看板预览", "side": "long"}
                _plan = self._trend_plan(price, equity, _sig)
                if _plan.ok:
                    snap["plan_notional"] = round(float(_plan.notional), 2)
                    snap["plan_qty"] = float(_plan.qty)
                else:
                    snap["plan_notional"] = 0.0
                    snap["plan_qty"] = 0.0
                    snap["plan_blocked"] = _plan.reason
            except Exception as e:
                snap["plan_notional"] = 0.0
                snap["plan_qty"] = 0.0
                snap["plan_blocked"] = "计划计算失败：%s" % e
            # 唐奇安没有硬停损，这两个字段必须显式为 0——
            # 否则大屏会沿用趋势版的"距熔断 X%"，那个数字在这里不存在。
            snap["stop_pct"] = 0.0
            snap["hard_stop"] = False
        if self.pos:
            p = self.pos
            # adopted=True 是给看板看的：这笔仓不是策略开的，引擎【不会】按
            # 通道出场平掉它，只保留熔断止损。不标出来，用户看到的
            # "持仓 0.075 @ 72475 / 出场线 72691" 会让人以为马上要被平。
            snap["pos"] = {"side": p["side"], "qty": p["qty"], "entry": p["entry"],
                           "stop": p["stop"], "entry_ts": p["entry_ts"],
                           "adopted": self._is_adopted()}
            snap["pos_pnl_pct"] = (price - p["entry"]) / p["entry"] * 100.0
            snap["hold_h"] = (int(time.time() * 1000) - p["entry_ts"]) / 3600000.0
            # 接管仓有真实熔断止损，这里就必须显示 True —— 上面那两行
            # 是"策略计划没有硬停损"的口径，不能覆盖到持仓身上。
            if self._is_adopted() and p["stop"] > 0:
                snap["hard_stop"] = True
                snap["stop_pct"] = self.tcfg.trend_stop_pct
        return snap

    def _build_trend_snapshot(self, price: float, equity: float) -> dict:
        st = self.trend.state(price)
        if self._is_donchian():
            return self._build_chan_snapshot(st, price, equity)
        sma = st["sma"]
        snap = {"mode": "trend", "ready": st["ready"], "bars": st["bars"],
                "lookback": st["lookback"], "sma": sma, "price": price,
                "above": st["above"], "next_bar_h": self.trend.next_bar_eta_h(),
                # ★ 人工仓也算"有仓"：否则看板在用户手上有仓时显示"空仓"。
                "hold": bool(self.pos or getattr(self, "adopted_pos", None)),
                "adopted": getattr(self, "adopted_pos", None)}
        # 最近一根 4h K 是从哪来的、量是多少。
        # 收盘价两种口径一致，所以 SMA 区分不出用的是官方还是本地；
        # 只有成交量能区分（官方 8558 vs 本地 117）。不报出来，
        # "回退到本地 bar"在线上就完全不可见。
        if getattr(self, "_trend_bar_src", ""):
            snap["bar_src"] = self._trend_bar_src
            ov, lv = getattr(self, "_trend_bar_vol", (0.0, 0.0))
            snap["bar_vol"] = round(float(ov), 3)
            snap["bar_vol_local"] = round(float(lv), 3)
        if st["ready"] and sma:
            snap["dist_pct"] = (price - sma) / sma * 100.0
            # ★ 触发线不是裸均线。
            #
            # 有滞回带时，进场条件是 收盘 > SMA*(1+h)，比裸 SMA 更晚。
            # 如果这里还按裸 SMA 报「还需 +5.5%」，用户会盯着一个
            # 到了也不会开仓的价格——和第六十六轮把 SMA200 说成静止门槛
            # 是同一类错：报的门槛不是真门槛。
            trig = float(st.get("entry_line") or sma)
            snap["trigger"] = trig
            snap["hyst_pct"] = st.get("hyst_pct", 0.0)
            snap["need_pct"] = (trig - price) / price * 100.0 if price < trig else 0.0
            # need_pct 把门槛当成静止的，但均线每根 4h 都在滚动。
            # 价格完全不动，只要被挤出去的老 K 价高于现价，均线就自己下来。
            # 实测（2026-11-01，价 77719 / SMA 82337）：31 天后自然上穿。
            # 只说「还需涨 5.5%」会把门槛说高，用户会以为在等一个可能不来的上涨。
            # 注意判据用 price < trig（不是 not above）：
            # 价格已经在均线上方、但还在滞回带里时，同样还没到触发线。
            if price < trig:
                try:
                    _roll = self.trend.roll_forward_to_cross(price)
                    snap.update(_roll)
                    # ★ 有滞回带时，「等均线自己下来」这条路【走不通】。
                    #
                    # 价格不动的话，SMA 最终收敛到现价本身；而进场要求
                    # 现价 > SMA*(1+h)。SMA 最多等于现价，永远够不到现价*(1+h)。
                    # 所以开多必须【价格自己涨】，不是等出来的。
                    #
                    # 这不是细节：第六十六轮大屏为了纠正"把门槛说成静止的"，
                    # 加了一句「或价格不动、均线自己下来约 31 天后上穿」。
                    # 有滞回之后那句话变成【假的】——必须改成说清楚"等不到"，
                    # 否则用户会等一件数学上不可能发生的事。
                    if not _roll.get("roll_bars") and                             float(st.get("hyst_pct") or 0.0) > 0:
                        snap["roll_never"] = True
                except Exception:
                    logger.warning("均线滚动推算失败", exc_info=True)
            # 空仓等待时，除了"差多少个点"，还要能回答"历史上的等待一般多久"。
            # 否则用户无法区分「正常等待」和「系统卡住了」——这两种处置完全不同，
            # 而大屏上都只是一句"空仓等待"。
            # 数据是历史频率、不是预测，source 一起带上，看板要标明。
            # 判据用 price < trig（不是 not above）：
            # 价格已在裸均线上方、但还在滞回带里时，同样还没进场、同样该给预期。
            # 而 expected_wait 要带上滞回半宽，否则它回答的是
            # "涨回裸均线要多久"，比真正进场的门槛乐观（实测差 0~0.8 天）。
            if price < trig and not self.pos:
                try:
                    from .wait_stats import expected_wait
                    ew = expected_wait(snap["dist_pct"], snap.get("hyst_pct") or 0.0)
                    if ew:
                        snap["wait"] = ew
                except Exception:
                    logger.warning("等待预期查询失败", exc_info=True)
            lev = self._plan_leverage()
            snap["target_leverage"] = lev
            snap["exec_market"] = getattr(self.ex, "name", "paper")
            # 必须直接问引擎要计划，不能在这里重算一遍。
            #
            # 以前这里是自己算的：notional = equity * lev，qty 用 round()。
            # 而 _trend_plan 真正的算法是：
            #   notional = equity * lev / (1 + fee_buffer)   <- 漏了 0.3% 手续费缓冲
            #   qty      = int(...) * step                   <- 向下取整，不是四舍五入
            # 于是看板显示的"届时仓位"比引擎实际下的单更大。
            #
            # 数字差得不多（100U 账户差 0.3%），但性质很坏：
            # 用户在大屏上看到的不是系统要做的事，而是另一个系统要做的事。
            # 而"我要知道你在做什么"这条需求，前提就是两者必须同一份计算。
            #
            # 顺带说明为什么必须是向下取整：四舍五入会向上超配，
            # 实测 180 次开仓里有几次资金使用率达到 131%。
            try:
                _sig = {"name": "trend_preview", "reason": "看板预览",
                        "side": "long"}
                _plan = self._trend_plan(price, equity, _sig)
                if _plan.ok:
                    snap["plan_notional"] = round(float(_plan.notional), 2)
                    snap["plan_qty"] = float(_plan.qty)
                else:
                    # 计划不可行就把原因直接摆到看板上，
                    # 不能显示一个"看起来会有"的仓位
                    snap["plan_notional"] = 0.0
                    snap["plan_qty"] = 0.0
                    snap["plan_blocked"] = _plan.reason
            except Exception as e:
                snap["plan_notional"] = 0.0
                snap["plan_qty"] = 0.0
                snap["plan_blocked"] = "计划计算失败：%s" % e
            snap["stop_pct"] = self.tcfg.trend_stop_pct
        if self.pos:
            p = self.pos
            snap["pos"] = {"side": p["side"], "qty": p["qty"], "entry": p["entry"],
                           "stop": p["stop"], "entry_ts": p["entry_ts"],
                           "adopted": self._is_adopted()}
            snap["pos_pnl_pct"] = (price - p["entry"]) / p["entry"] * 100.0
            snap["hold_h"] = (int(time.time() * 1000) - p["entry_ts"]) / 3600000.0
        return snap

    def _seed_liq_cache_from_equity(self, eq, now_ms: int) -> None:
        """用 `equity()` 顺手带回来的真实爆仓价刷新缓存（A3）。

        为什么单独抽成一个方法：这段逻辑原来写在 `_step` 里，而 `_step`
        依赖十几个协作者 —— 于是没有任何测试能证明它**真的执行了**。
        抽出来之后，"缓存到底有没有被刷新"变成一个可以直接断言的函数，
        而不是一句只在源码里看起来存在的赋值。

        短 TTL（5 秒）而不是当成永久值：保证它只活过一个 tick，
        下一个 tick 的 `equity()` 会再刷一次。`equity()` 失败时缓存
        也不会因此无限期变陈旧 —— `_live_liq_price` 还有自己的兜底路径。
        """
        liq = 0.0
        if isinstance(eq, dict):
            try:
                liq = float(eq.get("liq_price") or 0.0)
            except (TypeError, ValueError):
                liq = 0.0
        if liq > 0:
            self._liq_cache = (now_ms, liq, 5000)

    async def _confirm_stop_price(self, local_px: float) -> float:
        """向交易所复核一次价格。拿不到就返回 0（表示无法确认，按本地价执行）。

        ★ A2（2026-11-25 交易域审查）：复核必须与**触发价同源**。

          触发价是 `self.feed.agg.current(3000).close` —— 由成交推动的
          3 秒聚合收盘价。以前复核调 `mark_price()`（标记价 = 指数价 +
          基差平滑），两者不是一个东西：

              快跌时 last 先动、mark 慢半拍 →
              「本地已触发止损、交易所未触发」→ 判「行情陈旧」→ 拒绝平仓。
              而那正是最该平的那一秒。

          而且币安 STOP/STOP_MARKET 的默认触发口径是
          `workingType=CONTRACT_PRICE`（最新成交价），不是标记价 ——
          拿标记价复核一张按最新成交价触发的止损，本身就对不上。

          改成取最新成交价之后，"两个价格不同"才真正等于
          "其中一个是陈旧的"，这道闸才判对了东西。
          `mark_price()` 仍保留给记账兜底（那里要权威价，不要可成交价）。
        """
        # 优先最新成交价；执行器没实现就退回标记价（纸面/测试替身）。
        fn = (getattr(self.ex, "last_trade_price", None)
              or getattr(self.ex, "mark_price", None))
        if fn is None:
            return 0.0
        try:
            return float(await fn() or 0.0)
        except Exception as e:
            logger.warning("复核价格失败，按本地价执行：%s", e)
            return 0.0

    def _is_spot(self) -> bool:
        """★ 永远返回 False。无论配置、无论执行器名字。

        2026-11-17 用户裁决：「我们是合约账户做交易，我不想看到现货相关的
        东西影响到我们了！」

        为什么不是删掉这个方法、而是让它恒 False：
        它的 7 个调用点（杠杆 / 步长 / 手续费缓冲 / 最小名义 / 钱包提示 / dust 清理）
        每一处都带着一串现货专用常量。直接删方法要同时改 7 处，
        任何一处漏改就是静默按现货记账（这个项目最吃亏的失败方式）。
        恒 False 让所有分支同时失效，且每个分支变量都被注销。

        ★ 它不是“看不见就不存在”：`tests/test_perp_only.py` 钉住
          这个方法恒 False，且配置层面拒绝 `exec_market=spot`。
        """
        return False

    def _chan_warmup(self) -> int:
        """策略变热需要多少根 K。

        唐奇安要 entry_n + 2 根（判定那根的窗口 n 根 + 上一根做"新鲜突破"判断）；
        4h SMA 要 sma_lookback + 1 根。写错这个数只有一个后果：
        策略在数据还不够时就报"ready"，用半截窗口算出错误的通道。
        """
        if self._is_donchian():
            return int(getattr(self.tcfg, "donchian_entry", 150) or 150) + 2
        return int(self.tcfg.sma_lookback) + 1

    @staticmethod
    def _norm_side(side) -> str:
        """兼容旧调用点的别名，实现见模块级 `norm_side()`。"""
        return norm_side(side)

    def _is_adopted(self) -> bool:
        """当前是否存在「对账接管」来的人工仓（不是策略自己开的）。

        为什么不能只看内存里的一个布尔：进程重启后持仓是从
        trades 表恢复的，那个布尔不会跟着走。
        唯一能活过重启的载体就是 open_reason —— 所以判断也必须读它。

        ★ 2026-11-24 拆仓后：人工仓存放在 `self.adopted_pos`，
          系统仓在 `self.pos`。所以主判据变成「adopted_pos 非空」。

        ★ 但**保留**对 `self.pos` 的兜底判断（两者取或）：
          任何还没改到的新代码路径、测试替身、或者从旧版本升级时遗留的
          状态，都可能把一笔接管仓放进 `self.pos`。这道闸必须**宁可多触发**——
          它拦的是「拿策略规则去平用户手工仓」，漏一次就是真钱。
          读不到 open_reason 时返回 False，维持「按策略仓处理」的老行为，
          那一条方向相反（宁可多管一笔策略仓，也不要让策略仓无人看管）。
        """
        # ★ 用 getattr 而不是 `self.adopted_pos`：测试替身（SimpleNamespace）
        #   和"跨版本恢复的旧状态对象"都可能没有这个属性，而这道闸是**安全闸**，
        #   它自己抛 AttributeError 等于把归属判断整段跳过 —— 那就是 11-24 事故。
        #   getattr 默认 None = "没有人工仓"，随后仍会走 self.pos 的兜底判断。
        if getattr(self, "adopted_pos", None):
            return True
        p = self.pos
        if not p:
            return False
        return str(p.get("open_reason") or "").strip() == ADOPT_REASON

    def _adopted_slot(self) -> Optional[dict]:
        """取「人工仓」那一份，并告诉调用方它放在哪个槽里。

        返回 ``(pos_dict, "adopted"|"pos"|"")``。`"pos"` 只出现在旧状态兜底
        （接管仓被放进了 `self.pos`）时，清空时要清对槽，不能清错。
        """
        _a = getattr(self, "adopted_pos", None)
        if _a:
            return _a, "adopted"
        p = self.pos
        if p and str(p.get("open_reason") or "").strip() == ADOPT_REASON:
            return p, "pos"
        return None, ""

    def _is_donchian(self) -> bool:
        """当前是否跑唐奇安策略。

        单一口径：只认配置值。策略实例是懒加载的（第一次进分支才建），
        所以不能靠 self.strat is not None 判断。
        """
        return str(getattr(self.tcfg, "strategy", "") or "").lower() == "donchian"

    def _is_chan(self) -> bool:
        """是否走通道/趋势这一族（4h SMA 与 1h 唐奇安共用同一条执行链）。

        为什么合成一个谓词：两者的执行语义完全一样——空仓判入场、持仓判出场、
        不做 T+0 止损、不做可达性门、不套连亏冷却。分成两处判断迟早会漏掉一处，
        而漏掉的那一处会静默跑一套没验证过的风控。
        """
        return str(getattr(self.tcfg, "strategy", "") or "").lower() in ("trend", "donchian")

    def _is_pending(self) -> bool:
        """是否走「提前挂单」策略。单一口径：只认配置值。"""
        return str(getattr(self.tcfg, "strategy", "") or "").lower() == "pending"

    def _pending_is_dry(self) -> bool:
        """本轮是否「只出计划不挂单」。

        默认值来自配置（`trading.pending_dry_run`），构造参数可以覆盖它
        —— 测试和临时演练都用得上，但**线上唯一权威是配置**。
        """
        if self._pending_dry is not None:
            return bool(self._pending_dry)
        return bool(getattr(self.tcfg, "pending_dry_run", True))

    def _pending_harvest(self, b15, b5=None) -> None:
        """从计划源采集**当前这一根 K** 上新生的计划。

        ★★ 为什么必须按时间过滤

          `detect_all()` 扫的是**全历史**，返回的也是全部历史计划
          （真实数据：180 天 **11 条**）。如果不过滤就全插进去，
          引擎会在开盘那一刻按**几个月前的价位**挂出 11 张单。
          而且每一张看起来都合法：价位自洽、止损合规、方向正确。
          这种错误不会报错，只会表现为“一开机就满仓”。

        ★★ 2026-11-20 修：**时钟必须跟着形态的周期**

          三引线声明在 **5m** 收盘上（`claim_on_entry=True`，
          `effective_claim_ts` 是那根 5m 的起始时刻，例如 10:05），
          而采集原来只拿**最后一根 15m** 的 ts 去比。5m 的时刻和 15m
          的时刻（10:00 / 10:15）基本不可能相等 → 三引线的计划
          **一条都采不到**，永远是 0 条预期。

          这不是「偶尔漏一条」：它让 5m 周期的形态**整体失效**，
          而表现只是「没有计划」，看起来像行情没配合。

          现在的判据按形态自己的时钟走：
            · `claim_on_entry=True`  → 和「最后一根 5m」比
            · 否则（15m 口径）        → 和「最后一根 15m」比
          `thesis_id` 是 `方向-形态-bar_ts` 的确定值、store 按主键覆盖，
          所以重复采集是幂等的（不会插出两条）。

        不报错的三种情形（都要留痕，不许静默）

          1. 没接计划源 → 告警一次，然后不产生计划（而不是崩）。
          2. 计划源抛错 → 记风控，本根 K 不采集（不能因为源坏了就
             让已有持仓的推进也停下来）。
          3. 一根 K 里重复调用 → 直接返回（全库扫一遍不便宜）。
        """
        if self.pending_source is None:
            if not self._pending_source_warned:
                self._pending_source_warned = True
                detail = (
                    "挂单策略没接计划源（pending_source=None）：永远不会有新计划，"
                    "也就永远不会下单。实盘必须注入 PlanSource.plans。")
                logger.error("[挂单] %s", detail)
                try:
                    self.db.log_risk("pending_source_not_wired", detail[:300])
                except Exception:
                    pass
            return

        cur15 = 0
        for b in reversed(list(b15 or ())):
            cur15 = int(getattr(b, "ts", 0) or 0)
            if cur15:
                break
        cur5 = 0
        for b in reversed(list(b5 or ())):
            cur5 = int(getattr(b, "ts", 0) or 0)
            if cur5:
                break
        # 节流：按 5m 时钟（采集节奏 = 状态机推进节奏）
        cur = cur5 or cur15
        if not cur or cur == self._pending_harvest_ts:
            return
        self._pending_harvest_ts = cur

        # ★ 参数**只生成一次**，计划发现和建预期共用同一份。
        #   分两次调 `_pending_params()` 就等于两个来源：一旦其中一次拿到
        #   None（`_sizing_params()` 失败时的兜底），计划按 A 组参数算、
        #   预期按 B 组参数落库，止损/超时/衰减会各说各话。
        params = self._pending_params()
        try:
            plans, stats = self.pending_source(params)
        except Exception as e:
            logger.error("[挂单] 计划源出错，本根不采集：%s", e, exc_info=True)
            try:
                self.db.log_risk("pending_source_error", str(e)[:200])
            except Exception:
                pass
            return

        from . import pending_plan as _P
        from . import pending_thesis_store as _PTS
        from . import thesis as _T
        added = 0
        for pl in (plans or ()):
            # ★ 只收「决策时刻 = 当前这根」的计划，时钟跟着形态自己的周期
            _claim_ts = int(getattr(pl, "effective_claim_ts", 0) or 0)
            _want = cur5 if bool(getattr(pl, "claim_on_entry", False)) else cur15
            if not _want or _claim_ts != _want:
                continue
            try:
                pl.validate()
            except Exception as e:
                logger.warning("[挂单] 计划自洽性不过，丢弃 %s：%s", getattr(pl, "kind", ""), e)
                continue
            tid = self._pending_plan_key(pl)
            # ★★ P1（2026-11-25 交易域审查）：**必须**走 `to_thesis()`。
            #
            #   这里原来是手工拼 `_T.Thesis(...)`，只传了形态/方向/价格/止损/
            #   止盈/order_type/family，漏掉了 `horizon_bars`、`stale_distance`、
            #   `surprise_pts` 三个参数 —— 于是它们取了 `Thesis` 的默认值 0。
            #
            #   后果是三条规则**在生产上静默关闭**（实测库里 22/22 行全为 0）：
            #     · horizon_bars=0  → 挂单超时永不过期
            #     · stale_distance=0 → 挂单期跑偏永不撤销（僵尸计划越积越多）
            #     · surprise_pts=0  → `_surprise_hit` 遇 pts<=0 直接返回 None，
            #                          「单根 5m 顺向 ≥500 点立即止盈」彻底失效
            #
            #   而 `PendingParams` 里这三个值本来是 12 / 止损距离×1.0 / 500。
            #   唯一会把它们带下去的函数就是 `to_thesis()`，测试也都在测它 ——
            #   但生产走的是另一条手工路径，所以测试全绿、线上全 0。
            #   这是本项目第十次栽在「同一事实两个来源」上，所以这次不是
            #   "补三个参数"，而是**把生产路径换成那个唯一实现**。
            th = _P.to_thesis(pl, thesis_id=tid, params=params)
            try:
                if self.pending.add_if_new(_PTS.row_of(
                        th, state=_T.EXPECTED, next_index=0,
                        bar_ts=int(pl.bar_ts))):
                    added += 1
            except Exception as e:
                logger.error("[挂单] 新计划落库失败 %s：%s",
                             tid, e, exc_info=True)
        if added:
            logger.warning("[挂单] 采集到 %d 条新计划（bar_ts=%s）", added, cur15)

    async def _step_selfcheck(self, b15, b5=None) -> None:
        """消费一次链路自检请求（请求文件读走即删，所以只会触发一次）。

        语义与边界全部在 `trading/selfcheck.py` 的模块 docstring 里；
        这里只做「有没有请求」这一件事，**不判断该不该下单** ——
        判定在 `selfcheck.refusal()`，仓位在 `_pending_size()`，
        安全闸在 `_pending_place()`，一处都不在这里重复。
        """
        from . import selfcheck as _SC

        if os.path.exists(_SC.request_path(self.cfg)):
            res = await _SC.fire_once(self, b15, self.cfg, b5=b5)
        elif os.path.exists(_SC.cancel_path(self.cfg)):
            res = await _SC.cancel_once(self, self.cfg)
        else:
            return
        if res.get("fired"):
            self._log_pending_note("[自检] %s" % (res.get("detail")
                                                  or ",".join(res.get("cancelled") or ())))
        elif res.get("reason"):
            logger.info("[自检] 未执行：%s", res.get("reason"))

    def _log_pending_note(self, text: str) -> None:
        """自检留痕。风控表由 selfcheck 自己写，这里只保证日志里看得见。"""
        logger.warning("%s", text)

    @staticmethod
    def _pending_plan_key(plan) -> str:
        """计划的稳定身份。与 `PlanSource.plan_key()` 同一份口径"""
        from .plan_source import PlanSource
        return PlanSource.plan_key(plan)

    def _pending_kbar_view(self):
        """取 S5 等着的 K 线演化视图；没接线就告警。

        返回 None 不是“错误”，是“这三道闸本轮不工作”。
        但它必须在日志和风控表里留下一条 —— 否则“闸没数据”和“闸没触发”
        在事后看起来完全一样。
        """
        kv = self.pending_kbar
        if kv is None:
            if not self._pending_kbar_warned:
                self._pending_kbar_warned = True
                detail = (
                    "S5 出场闸没接 K 线演化视图（pending_kbar=None）：保本棘轮、恐慌盘顺向、缩量停滞"
                    "本轮全部不工作。实盘必须注入 load_kbar_view()。")
                logger.error("[挂单] %s", detail)
                try:
                    self.db.log_risk("pending_kbar_not_wired", detail[:300])
                except Exception:
                    pass
        return kv

    def _pending_warn_once(self, detail: str) -> None:
        """pending 没接线/idle 的告警只报一次。

        每根 3s K 线报一次的后果不是"更安全"，而是把真正的告警淹掉。
        """
        if self._pending_warned:
            return
        self._pending_warned = True
        logger.error("[挂单] %s", detail)
        try:
            self.db.log_risk("pending_not_wired", detail[:300])
        except Exception:
            pass

    async def _step_adopted_exit(self, bars_by_iv, b15, price, now_ms,
                                 log_quiet=False) -> None:
        """接管仓（人工仓）的**出场管理**：只管出场，不挂新单。

        ★ 为什么要这个函数（2026-11-24 事故 + 用户要求「系统帮我管理手工单
          的出场我是满意的，只是要正确」）：

          事故前，`strategy=pending` 的下接管仓**没有任何正当管理路径**：
          分派在 `_step` 里 `if self._is_pending(): _step_pending(); return`，
          所以 `_step_trend` 那段熔断判断（`price <= p["stop"]`，还带交易所价
          复核）**永远不会跑**；而挂单链又只推进 thesis。
          接管时写进 `self.pos["stop"]` 的那个 25% 熔断止损（`entry*0.75`）
          于是只存在于数据库里，没有一行代码去执行它。

        ★★ BUS-D（2026-11-24）：判据从「三条自写规则」改为**守卫的 P0+P1**。

        用户的裁决（原话）：
          「这样的话，你人工仓还一定要基于结构失效的判断吗？」

        **不需要，而那两条判据现在都删了**：

          · 原来的「结构失效」用 `thesis.invalidation_verdict(family=FAMILY_TREND)`。
            `FAMILY_TREND` 是**我编造的前提** —— 人工仓没有入场时的结构，
            凭什么用一个趋势家族的结构去判它失效。
          · 原来的「观察线穿透后收回」同属 P2 结构类，一并删除。
            （它需要"入场时盯的是哪条线"，人工仓同样没有。）

        留下的是两条**与策略无关的数学事实**：

          P0 保命 —— 离爆仓还有几个止损宽度。用**交易所返回的真实爆仓价**
                   （BUS-D 补的 `liquidationPrice`），不再用配置推算。
          P1 止损 —— 止损价 = 爆仓距离 / 安全倍数（上一轮已修好）。
                   价格越过它才走；「越过」这件事仍用交易所权威价复核一次，
                   防本地行情陈旧导致假触发（这条防御是有效的，保留）。

        用户说的「每一根 K 线都有话说」在这里也成立，但**只用一处**：
        `KlineRead` 每根都算，只喂给 P1 的一次性恐慌插针延迟
        （`panic_flush`），永远不作为「所以该出场了」的理由。

        ★ 不设止盈：人工仓没有 R 依据，硬编 4R 等于凭空造规则；而且用户
          明确说「我是准备拿久一点」，固定目标位会把它自动卖掉。

        ★ 2026-11-24 全链路审查改动（F1/F3/F4/F5/F6/F10）：
          · 参数表收缩为 `(bars_by_iv, b15, price, now_ms, log_quiet)`。
            原来的 `b5` / `book` / `ts` 在删掉 P2 之后**已无人使用**，
            两处调用方却仍在每 tick 计算 `pending_lines()` —— 白算。
          · 部分平仓不再由 `_adopted_close` 一律清槽（见该函数）。
          · 出场/减仓都会写一条 `action="close"/"reduce"` 的决策行，
            并因此触发看板推送 —— 用户最初的困惑就是"为什么在这个价位出场"，
            而这条路径以前**一条决策都没留**。
          · 维护 `best/worst` 供 MFE/MAE 记账（以前恒写 0）。
          · `stop <= 0` 不再当成"止损在 0 元"（见下面 F4 处）。
        """
        # ★ 只处理**人工仓**那一份（2026-11-24 拆仓）。
        #   用 `_adopted_slot()` 而不是直接读 self.pos：旧状态可能把接管仓
        #   放在 self.pos 里，这个辅助函数两种都能找到，并且告诉我们
        #   清槽时该清哪一个。
        pos, _slot = self._adopted_slot()
        if not pos:
            return
        qty = abs(float(pos.get("qty") or 0.0))
        if qty <= 0:
            return
        side = str(pos.get("side") or "long").lower()
        up = side == "long"
        entry = float(pos.get("entry") or 0.0)
        stop = float(pos.get("stop") or 0.0)
        anchor = self._adopted_anchor_ts(pos)

        # ---- F5：维护持仓期间的最高/最低价（MFE/MAE 的唯一来源）----
        #   `_manage` 里对系统仓做过同一件事（`p["best"]/["worst"]`），
        #   但人工仓从不经过 `_manage`，所以这两列在 trades 表里恒为 0。
        #   而"这笔出场是不是太早"恰恰只能靠 MFE 判断 —— 正是用户最初的问题。
        #   统计字段一律防御式：拿不到就用现价，绝不让它把出场判断搞崩。
        try:
            _b3 = self.feed.agg.current(3000)
            _hi = float(getattr(_b3, "high", price))
            _lo = float(getattr(_b3, "low", price))
        except Exception:
            _hi = _lo = float(price)
        _dir = 1 if up else -1
        try:
            if _dir > 0:
                pos["best"] = max(float(pos.get("best") or entry), _hi)
                pos["worst"] = min(float(pos.get("worst") or entry), _lo)
            else:
                pos["best"] = min(float(pos.get("best") or entry), _lo)
                pos["worst"] = max(float(pos.get("worst") or entry), _hi)
        except Exception:
            pass

        # ---- F4：`stop <= 0` 不是"止损在 0 元" ----
        #   唐奇安策略**合法地不设硬止损**（`_fallback_stop` 第 445 行），
        #   它的 stop 就是 0。而守卫把 stop=0 读成"入场价下方一整个入场价"
        #   的距离：d_stop = entry，结构性缓冲 = 爆仓距离/入场价 ≈ 0.04 倍，
        #   于是判「减仓 98%」—— 那不是守卫激进，是我们在用错的语言问它。
        #   人工仓不该出现这种状态（接管时就算好了止损），所以这里**补一个**
        #   而不是跳过：补的是"爆仓距离/安全倍数"，数学上保证先于爆仓触发。
        if stop <= 0 and entry > 0:
            _pct = self._stop_distance_pct(entry)
            if _pct > 0:
                stop = (entry * (1 - _pct / 100.0) if up
                        else entry * (1 + _pct / 100.0))
                pos["stop"] = stop
                logger.error("[接管] 人工仓没有止损价 → 按爆仓距离推导 %.3f%% "
                             "补一个 %.2f（入场 %.2f）", _pct, stop, entry)
                self.db.log_risk("adopted_stop_missing_repaired",
                                 "按 %.3f%% 补止损 %.2f（入场 %.2f）"
                                 % (_pct, stop, entry))
            else:
                self._log_decision(
                    bars_by_iv, price, 0.0, now_ms, None, "watch",
                    "接管仓没有止损价且算不出安全距离 —— 需人工介入"
                    "（不调用守卫，避免把 stop=0 读成「止损在 0 元」）")
                return

        from . import kline_intent as _ki
        from .exit_executor import HOLD as _EX_HOLD
        from .exit_executor import describe as _ex_describe
        from .exit_executor import plan_exit as _plan_exit
        from .position_guard import decide as _pg_decide

        # ---- 只取「接管之后」的已收盘 15m K 线 ----
        #   「正确」的可实现定义：早于接管时刻的 K 线一律不参与判断。
        #   事故那次用的就是 11-20 的 K 线去判 11-24 的仓。
        bars = self._adopted_bars(b15, anchor)
        if not bars:
            self._log_decision(
                bars_by_iv, price, 0.0, now_ms, None, "watch",
                "接管仓：还没有接管之后的已收盘 15m，本轮不判出场")
            return

        # ---- 组装守卫的持仓视图 ----
        gp = await self._guard_position_for_adopted(pos, price)
        if gp is None:
            # 连爆仓价都拿不到：只能用熔断止损兜底，绝不用猜的缓冲去判 P0。
            if stop > 0 and ((price <= stop) if up else (price >= stop)):
                truth = await self._confirm_stop_price(price)
                if truth and ((up and truth > stop) or ((not up) and truth < stop)):
                    logger.warning("[接管] 本地价触发止损但交易所价未触发，忽略")
                    self.db.log_risk("stale_price",
                                     "接管仓：本地 %.1f / 交易所 %.1f，已忽略"
                                     % (price, truth))
                else:
                    await self._adopted_close(
                        qty, side, entry, "熔断止损（stop=%.2f 无爆仓价）" % stop)
                    return
            self._log_decision(
                bars_by_iv, price, 0.0, now_ms, None, "watch",
                "接管仓在管：拿不到爆仓价，本轮只做止损兜底（stop=%.2f）" % stop)
            return

        # ---- K 线读数：只用于 P1 的一次性恐慌插针延迟 ----
        #   ★ 用 `_bar_ts` / `_bar_ohlcv` 取值，不要写死 `b.open` 或 `b.o`：
        #     真实 `feed.Bar` 是 open/high/low/close，
        #     `pending_plan._Bar` 是 o/h/l/c，写死任一种都会在另一种上炸。
        seq = [((self._bar_ts(b),) + self._bar_ohlcv(b)) for b in bars[-60:]]
        try:
            kline = _ki.read_kline(seq) if seq else None
        except Exception as e:
            logger.error("[接管] K 线读数失败（不影响止损）：%s", e)
            kline = None

        cfg = self._guard_config_for_observe()
        decision = _pg_decide(gp, float(price), cfg, kline=kline)

        # ---- 出场执行层：这里守住手数门槛（用户裁决 ②）----
        plan = _plan_exit(decision, qty, self._qty_step(), price)

        if plan.is_noop:
            # ★ A1：持有期间维持交易所侧那张保护性止损单。
            #   放在这一支而不是函数入口：本 tick 若已经要平仓，
            #   再挂一张单等于白挂一次又要撤一次。
            await self._sync_adopted_stop_order(pos, price)
            if not log_quiet:
                # ★ F6：把爆仓价**来源**写进决策。以前 `_liq_src` 只赋值、
                #   从不输出，docstring 却写着"记进日志" —— 一行装成
                #   可观测的假字段。"离爆仓还有 2.5 倍"这句话到底基于
                #   交易所真实值还是配置推算，必须能从库里读出来。
                self._log_decision(
                    bars_by_iv, price, 0.0, now_ms, None, "watch",
                    "接管仓在管：未出场（数量 %.6f / 入场 %.2f / 止损 %.2f "
                    "距离 %.2f%%；守卫 %s：%s；判据用 %d 根接管后 15m；"
                    "爆仓价来源 %s）"
                    % (qty, entry, gp.stop_price,
                       (abs(price - gp.stop_price) / price * 100.0)
                       if price > 0 else 0.0,
                       decision.action, _ex_describe(plan), len(bars),
                       getattr(self, "_liq_src", "未知")))
            return

        # ---- 止损/减仓都要先过「本地价是否陈旧」这一关 ----
        if plan.action in ("EXIT_STOP", "REDUCE"):
            truth = await self._confirm_stop_price(price)
            if truth:
                _adverse = (truth <= gp.stop_price) if up else (truth >= gp.stop_price)
                _triggered_by_stop = gp.remaining_to_stop(float(price)) <= 0
                if _triggered_by_stop and not _adverse:
                    logger.warning(
                        "[接管] 本地价 %.1f 触发止损，但交易所价 %.1f 未触发，"
                        "判定为行情陈旧，不执行", price, truth)
                    self.db.log_risk(
                        "stale_price",
                        "接管仓：本地 %.1f / 交易所 %.1f，已忽略出场"
                        % (price, truth))
                    return

        if plan.is_full_exit:
            logger.info("[接管] 守卫判 %s → 全部离场：%s",
                        decision.action, decision.reason)
            await self._adopted_close(qty, side, entry,
                                      "守卫 %s：%s" % (decision.action,
                                                      decision.reason))
            # ★ F3：出场必须自己留一条决策行（`_adopted_close` 目前不写，
            #   它只写 trades + risk_events）。用户最初的困惑就是
            #   「我没明白系统为什么要在这个价位出场」—— 而这条路径
            #   以前在决策流里**一条记录都没有**，看板也没有推送。
            #   写在这里而不是 `_adopted_close` 里：这个函数才有
            #   `bars_by_iv` 与 equity，且只有"真的平掉了"才会走到这儿
            #   （`_adopted_close` 失败会抛，不会静默记账）。
            self._log_decision(
                bars_by_iv, price, float(getattr(self, "_last_equity", 0.0) or 0.0),
                now_ms, "adopted_exit", "close",
                "接管仓全部离场（守卫 %s）：%s｜qty %.6f 入场 %.2f 止损 %.2f"
                "｜爆仓价来源 %s"
                % (decision.action, decision.reason, qty, entry, gp.stop_price,
                   getattr(self, "_liq_src", "未知")),
                gates=[{"gate": "守卫P判断", "ok": True,
                        "detail": "%s：%s" % (decision.action, decision.reason)}],
                features={"guard": decision.as_dict(), "liq_src": getattr(
                    self, "_liq_src", "未知"), "qty": qty,
                    "stop": gp.stop_price, "liq": gp.liq_price},
                risk_info={"qty": qty, "entry": entry,
                           "stop": gp.stop_price, "liq": gp.liq_price,
                           "buffer": gp.structural_buffer()})
            return

        if plan.action == "REDUCE":
            logger.info("[接管] 守卫判 REDUCE → 减仓 %.6f：%s",
                        plan.qty, decision.reason)
            await self._adopted_close(plan.qty, side, entry,
                                      "守卫 REDUCE：%s" % decision.reason)
            # ★ 实际剩余从**槽里读回来**，不拿 plan.qty 推算。
            #   `_adopted_close` 按 fill.qty（实际成交量）减仓，可能与计划量
            #   不同；粉尘还会被升格成"全部平完"（槽清空）。
            #   拿计划量算剩余，就等于在决策行里写了一个没发生的数字 ——
            #   而这条记录正是用来事后追溯的。
            _left = self._adopted_slot()[0]
            _remain = abs(float(_left.get("qty") or 0.0)) if _left else 0.0
            _sold = qty - _remain
            self._log_decision(
                bars_by_iv, price, float(getattr(self, "_last_equity", 0.0) or 0.0),
                now_ms, "adopted_reduce", "reduce",
                "接管仓减仓 %.6f / 共 %.6f（守卫 REDUCE）：%s｜剩余 %.6f%s"
                "｜爆仓价来源 %s"
                % (_sold, qty, decision.reason, _remain,
                   "（剩余不足一个步长，已一次平完）" if _left is None else "",
                   getattr(self, "_liq_src", "未知")),
                gates=[{"gate": "守卫P判断", "ok": True,
                        "detail": "%s：%s" % (decision.action, decision.reason)}],
                features={"guard": decision.as_dict(), "liq_src": getattr(
                    self, "_liq_src", "未知"), "planned": plan.qty,
                    "sold": _sold, "remain": _remain},
                risk_info={"planned": plan.qty, "sold": _sold,
                           "remain": _remain})
            return

        if plan.action == "MOVE_STOP":
            old = float(pos.get("stop") or 0.0)
            if abs(old - plan.new_stop) > 1e-9:
                pos["stop"] = plan.new_stop
                logger.info("[接管] 守卫移动止损 %.2f -> %.2f：%s",
                            old, plan.new_stop, decision.reason)
                self.db.log_risk(
                    "adopted_stop_moved",
                    "%.2f -> %.2f（%s）" % (old, plan.new_stop,
                                           decision.reason))
                self._persist_adopted_stop(pos)
                # ★ A1：止损被抬高之后，交易所侧那张单必须跟着改 ——
                #   否则进程内说"止损 60500"、交易所侧还挂着 59895，
                #   进程一挂，保护退回旧的、更松的位置。
                await self._sync_adopted_stop_order(pos, price)
                self._log_decision(
                    bars_by_iv, price, 0.0, now_ms, None, "watch",
                    "接管仓止损移动：%.2f -> %.2f（守卫 %s）"
                    % (old, plan.new_stop, decision.reason))
                return

        # 理论到不了这里；到了说明执行层放行了一个它不认识的动作用途
        logger.error("[接管] 执行层返回了未知动作 %r，本轮不动作", plan.action)
        return

    @staticmethod
    def _adopted_anchor_ts(pos) -> int:
        """接管仓的**判断锚点**（毫秒）。

        ★ 这是「正确」的核心：早于这个时刻的 K 线一律不许参与出场判断。
          事故就是用 11-20 的 K 线判 11-24 的仓。

        取 `entry_ts`（这笔仓存在的最早时刻）。取不到就退到「现在」，
        **绝不退到 0**——退到 0 等于允许重放整个缓冲，也就是事故本身。
        """
        try:
            v = int(pos.get("entry_ts") or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
        return int(time.time() * 1000)

    @staticmethod
    def _bar_ts(b) -> int:
        """取一根 bar 的时间戳，**兼容两种真实存在过的形状**。

        ★★ 2026-11-24 BUS-D 发现并修掉的**既存缺陷**（不是本次引入的）：

          `feed.Bar`（`market/agg.py` 的 dataclass）字段是
          `(interval_ms, open_time, open, high, low, close, volume)` ——
          **没有 `ts`**。而 `_adopted_bars` 原来用 `getattr(b, "ts", 0)` 过滤，
          于是真实行情上每根都取到 0，`0 >= anchor` 恒为 False，
          **过滤结果永远是空列表**。

          后果：`_step_adopted_exit` 每次都走到
          「还没有接管之后的已收盘 15m，本轮不判出场」就 return。
          也就是说接管仓的两条结构判据（观察线、结构失效）
          **在生产上从未执行过一次** —— 它们不是"有争议的规则"，
          是看着活的死代码。

          实测证据（用真实 `Bar` 跑 `_adopted_bars(400根, anchor=第300根)`）：
              带 .ts 的假对象 -> 保留 100 根（期望值）
              真实 feed.Bar   -> 保留   0 根   ← 恒空

          这正是这个项目栽过 8 次的同一个错：同一事实两个来源。
          假对象有 `.ts`、真对象只有 `.open_time`，
          而测试夹具造的是假对象，所以测试一路绿。

        ★ 为什么不干脆只认 `open_time`：`pending_plan._Bar` 是
          `(ts, o, h, l, c)` 形状，同时代码里还有元组形态的序列。
          三种形态都是合法输入，所以这里按**字段名**逐个试，
          不靠位置对齐（位置对齐全错位且不报错，历史上更致命）。
        """
        for attr in ("open_time", "ts"):
            v = getattr(b, attr, None)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        # 元组/列表形态：`_to_pending_bars` 约定第 0 位是时间
        try:
            return int(b[0])
        except (TypeError, IndexError, ValueError, KeyError):
            return 0

    @staticmethod
    def _bar_ohlcv(b):
        """取一根 bar 的 `(open, high, low, close, volume)`，兼容两种命名。

        ★ 和 `_bar_ts` 是同一个理由：这个仓库里同时生活着两种 bar
          · `feed.Bar`（真实行情）—— `open/high/low/close/volume`
          · `pending_plan._Bar`      —— `o/h/l/c`（没有 volume）
          写死任一种都会在另一种上抛 AttributeError 或被静默取到 0。
        """
        def _pick(*names):
            for n in names:
                v = getattr(b, n, None)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        return (_pick("open", "o"), _pick("high", "h"), _pick("low", "l"),
                _pick("close", "c"), _pick("volume", "v"))

    @staticmethod
    def _adopted_bars(b15, anchor) -> list:
        """只保留 `时间 >= anchor` 的 15m bar（按时间升序）。"""
        out = []
        try:
            a = int(anchor or 0)
        except (TypeError, ValueError):
            a = 0
        for b in (b15 or ()):
            try:
                if DecisionEngine._bar_ts(b) >= a:
                    out.append(b)
            except (TypeError, ValueError):
                continue
        return out

    async def _adopted_close(self, qty, side, entry, reason: str) -> None:
        """平掉接管来的人工仓。**与 `_pending_close` 分开是故意的。**

        ★ 为什么不能复用 `_pending_close`：
          那个函数的归属判据是 `open_reason == "挂单成交"`，接管仓是
          `ADOPT_REASON` —— 它必须拒绝。这里走的是**另一条授权路径**：
          接管仓的出场由「接管仓在管」这条规则授权，不是由某条 thesis 授权。
          把两者混成一个函数，就等于把 11-24 那个洞重新打开。

        ★ 平仓同样走 `market_order(reduce_only=True)`，不用 place_order
          （执行器不支持 MARKET，见 `_pending_close` 的注释）。

        ★★ 2026-11-24 全链路审查（F1）：**部分平仓与全部平仓必须分开收尾。**

          改之前这个函数无条件执行 `self.adopted_pos = None`。BUS-D 之前
          它只会被"全量平仓"调用，所以看不出问题；BUS-D 之后守卫的
          `REDUCE` 会带一个**部分数量**进来，于是：

              卖 93.4%        （交易所真的只卖这么多）
              记一笔已平      （trades 里这笔 exit_ts 非空）
              清空整槽        （剩余 6.6% 从内存消失）
              `_step` 随即认为"没有人工仓" → 交给策略链

          三重后果，全是真金白银：
            1. 剩余仓**无人管出场**（没有止损、没有爆仓保护）；
            2. `trades` 表里这笔已经"平完了"，**剩余仓在库里不存在** ——
               重启后引擎按空仓跑，等 5 分钟对账再重新接管，
               `entry_ts` 变了，"只用接管之后的 K 线"这个锚点历史一起丢；
            3. `close_booking` 把**整笔开仓费**摊在这一条部分腿上，
               pnl 偏负，而剩余仓没有成本。

          现在：卖出量 < 持仓量时走部分平仓 —— 内存减仓、费用按比例分摊、
          `close_trade` 记已卖部分、再补一条剩余的 open 行让重启接得上。
          卖完不足一个步长则升格为全平（不留下卖不掉的粉尘）。

        ★★ 记账一律用 `fill.qty`（交易所回来的**实际成交量**），不用计划量。

          这是 `_close`（策略仓）早就写对、而这里一度写错的地方：
          `market_order` 返回的 `FillResult.qty` 是真实成交量，计划量只是意图。
          拿计划量减内存持仓，部分成交时会把"没卖掉的那部分"从内存里抹掉 ——
          同一个后果又回来了一次（剩余仓无人管、库里不存在）。
          两条平仓路径必须用同一个口径，否则又是一处"同一事实两个来源"。
        """
        pos, _slot = self._adopted_slot()
        if not pos or abs(float(pos.get("qty") or 0.0)) <= 0:
            return
        pos_qty = abs(float(pos.get("qty") or 0.0))
        want = abs(float(qty or 0.0))
        step = float(self._qty_step() or 0.0)
        remain = pos_qty - want
        # 剩余不足一个步长 = 卖不掉（交易所会拒单），按全平处理
        if remain <= 1e-12 or (step > 0 and remain < step - 1e-12):
            want = pos_qty
        close_side = "SELL" if str(side).lower() == "long" else "BUY"
        try:
            fill = await self.ex.market_order(
                close_side, want, reason=("接管仓%s %s"
                                          % ("减仓" if remain > 0 else "出场",
                                             reason)),
                reduce_only=True,
                client_id=_close_client_id(
                    "adopt", "%s|%s" % (reason, pos.get("entry_ts"))))
        except Exception as e:
            logger.error("[接管] 平仓失败（人工仓，%s）：%s", reason, e)
            self.db.log_risk("adopted_close_failed", "%s: %s" % (reason, str(e)[:200]))
            raise
        if not getattr(fill, "ok", False):
            err = str(getattr(fill, "reason", "") or "未知原因")
            logger.error("[接管] 平仓失败（人工仓，%s）：%s", reason, err)
            self.db.log_risk("adopted_close_failed", "%s: %s" % (reason, err[:200]))
            raise RuntimeError(err)

        # ★ 实际成交量优先；执行器没给（或给 0）才退回计划量。
        sold = float(getattr(fill, "qty", 0.0) or 0.0)
        if sold <= 0:
            sold = want
        if sold > pos_qty:                     # 防御：不许卖出超过持仓
            sold = pos_qty
        if abs(sold - want) > 1e-9:
            logger.warning(
                "[接管] 实际成交 %.8f 与计划 %.8f 不一致（差 %.8f），"
                "按实际成交量记账", sold, want, want - sold)
        remain = pos_qty - sold
        # 卖完之后剩的不足一个步长 = 卖不掉的粉尘，按全平收尾
        if remain > 0 and step > 0 and remain < step - 1e-12:
            logger.info("[接管] 剩 %.8f 不足一个步长（%.6f），按全平收尾",
                        remain, step)
            remain = 0.0
        partial = remain > 1e-12

        exit_price = float(getattr(fill, "price", 0.0) or 0.0)
        if exit_price <= 0:
            try:
                exit_price = float(await self.ex.mark_price())
            except Exception:
                exit_price = float(pos.get("entry") or 0.0)

        # ★★ 统一口径（2026-11-24）：走 `close_booking()`，三条平仓路径
        #   同一个公式。这里以前只减平仓费、漏了开仓费（`pos["fee"]`），
        #   净盈亏仍然偏乐观；`_pending_close` 更是两边都不记。
        #
        # ★ F1：部分平仓时开仓费必须**按比例**摊 —— 整笔开仓费属于整笔仓，
        #   把它全压在这一条部分腿上会让这笔看起来更亏，而剩下的仓
        #   一分成本都不剩。所以先算出本腿该承担的那一份。
        _open_fee = abs(float(pos.get("fee") or 0.0))
        _fee_share = (_open_fee if not partial or pos_qty <= 0
                      else _open_fee * (sold / pos_qty))
        pnl, fee_total, pnl_net = close_booking(
            dict(pos, fee=_fee_share), exit_price,
            getattr(fill, "fee", 0.0), sold, side)

        # ★ F5：MFE/MAE 用持仓期间真实的最高/最低价。
        #   以前这里恒写 0.0 —— 而"这笔出场是不是太早了"只能靠 MFE 回答，
        #   正是用户最初那个问题（61550 没卖、61250 被平）。
        _dir = 1 if str(side).lower() == "long" else -1
        _best = float(pos.get("best") or entry)
        _worst = float(pos.get("worst") or entry)
        _mfe = round((_best - entry) * _dir, 6)
        _mae = round((_worst - entry) * _dir, 6)

        try:
            self.db.close_trade({
                "symbol": self.tcfg.symbol, "side": side,
                "qty": sold, "entry_ts": pos.get("entry_ts"),
                "entry_price": entry, "exit_ts": int(time.time() * 1000),
                "exit_price": exit_price,
                "stop_price": pos.get("stop"), "target_price": pos.get("target"),
                "pnl_gross": pnl, "fee": fee_total, "pnl_net": pnl_net,
                "mfe": _mfe, "mae": _mae,
                "open_reason": pos.get("open_reason", ""),
                "close_reason": "接管仓%s（%s）"
                                % ("减仓" if partial else "出场", reason),
                "paper": int(pos.get("paper") or 0)})
        except Exception as e:
            logger.error("[接管] 平仓已成交但关账失败：%s", e, exc_info=True)
            self.db.log_risk("adopted_close_book_failed", str(e)[:200])

        if partial:
            # ---- 部分平仓：只减仓，不清槽 ----
            #   内存里的数量与成本同步改写，`best/worst` 保留（它们描述的
            #   是同一段持仓期间），`entry_ts` 保留（判断锚点不变）。
            pos["qty"] = abs(float(pos.get("qty") or 0.0)) - sold
            pos["fee"] = max(0.0, _open_fee - _fee_share)
            # 剩下那部分仓在库里必须有一条 open 行，否则重启后引擎认为
            # 这笔已经平完 —— 那就是"账实不符"的最短路径。
            try:
                self.db.log_trade({
                    "symbol": self.tcfg.symbol, "side": side,
                    "qty": pos["qty"], "entry_ts": pos.get("entry_ts"),
                    "entry_price": entry,
                    "stop_price": pos.get("stop"),
                    "target_price": pos.get("target"),
                    "fee": pos["fee"],
                    "open_reason": pos.get("open_reason", ""),
                    "paper": int(pos.get("paper") or 0),
                    "strategy": getattr(self.tcfg, "strategy", "")})
            except Exception as e:
                logger.error("[接管] 减仓后补记剩余仓失败（内存仍是正确数量）：%s", e)
                self.db.log_risk("adopted_remain_unbooked", str(e)[:200])
            # 交易所的爆仓价会随持仓量变化，让下一跳重取真实值
            self._liq_cache = None
            # ★ A1：手数变了，交易所侧那张单的 quantity 必须跟着改。
            #   不改的话，减仓之后那张 reduceOnly 单比持仓还大 ——
            #   reduceOnly 会截到持仓量，不会开出反向仓，但"单上的量
            #   和真实仓不符"会让对账和人工核对都无法解释。
            await self._cancel_adopted_stop_order("减仓后手数变化")
            await self._sync_adopted_stop_order(pos, exit_price)
            try:
                self.db.log_risk(
                    "adopted_reduced",
                    "%s %.6f @ %.2f（剩余 %.6f，%s）"
                    % (close_side, sold, exit_price, pos["qty"], reason[:120]))
            except Exception:
                pass
            logger.warning(
                "[接管] 人工仓 %s 减 %.6f @ %.2f，剩余 %.6f（%s，本腿毛 %+.2f / 净 %+.2f）",
                close_side, sold, exit_price, pos["qty"], reason, pnl, pnl_net)
            return

        # ★ 只清**人工仓**那一个槽。以前这里是 `self.pos = None`，
        #   拆仓之后那样写会把系统仓一起清掉（反过来更糟：不清人工仓，
        #   下一根 bar 又会判一次出场 → 反复市价平仓）。
        if _slot == "adopted":
            self.adopted_pos = None
        else:
            self.pos = None
        self._liq_cache = None
        # ★ A1：仓没了，交易所侧那张保护性止损单必须撤掉。
        #   不撤的后果：仓已平、单还挂着（reduceOnly 触发也开不出反向仓，
        #   但会永久占着保证金和"挂单数"），而且下一笔仓如果 entry_ts 撞上
        #   就会复用同一个 clientOrderId。
        await self._cancel_adopted_stop_order("人工仓已全部平掉")
        try:
            self.db.log_risk("adopted_closed",
                             "%s %.6f @ %.2f（%s）" % (close_side, sold,
                                                      exit_price, reason[:160]))
        except Exception:
            pass
        logger.warning("[接管] 人工仓 %s 平 %.6f @ %.2f（%s，毛 %+.2f / 净 %+.2f）",
                       close_side, sold, exit_price, reason, pnl, pnl_net)

    @staticmethod
    def _pending_start_index(b15, bar_ts) -> int:
        """新 thesis 的 15m **起点下标**：它自己那根计划 K。

        ★★ 这是 2026-11-24 生产事故的直接成因，必须在这里说清楚。

          新 thesis 落库时 `next_index=0`（`_pending_harvest`），而
          `thesis.step()` 拿 `progress["next_index"]` 当 `start_at`。
          于是「从第 0 根开始」= **从行情缓冲最老那一根开始** ——
          `feed.py` 的 `history` 是 `deque(maxlen=400)`，400 根 15m ≈ 4 天。

          而生产又没传 `claim`，`thesis.run` 对 claim=None 的语义是
          「一开始就成立，直接挂单」。两件事一叠加：

              bars[0]（4 天前）立刻 ARMED → 顺着历史往后走 →
              某根历史 K 上判成 FILLED → 再某根上判成「观察线拒绝」
              → 产出 close 动作 → 拿**市价**平掉**当下**的真实持仓。

          实测那一天：起点 = 11-20 05:00（`armed_ref_close=61962.2`
          正是那根收盘），出场 = 11-20 20:00（决策价 61389.5 正是那根收盘），
          `next_index=61` 恰好是 0..60 共 61 根；而真实成交是
          11-24 09:00:14 的市价 61289.40 —— 拿 4 天前的结论平今天的仓。

        ★ 为什么复用 `pending_plan._b15_index_for_plan` 的算法而不是另写一套：
          回测路径（`run_plan`）一直是对的，生产的错在于**自己拼 step 参数、
          绕过了那个唯一入口**。同一个事实（计划在哪一根）只能有一个来源。

        ★ 用 `bisect_left`（第一根 ts >= bar_ts）而不是 `right-1`：
          计划就是在 `bar_ts` 那根**收盘后**才成立的，起点必须是它自己，
          从它开始判失效/成交才不会把「计划之前」的行情算进来。

        ★ 两种「算不出正确起点」的边界，都必须**拒绝重放**而不是退回 0：

          · `bar_ts` 比缓冲里所有 K 都新（计划那根还没到）→ 返回 `len(b15)`，
            这一轮什么也不做；下一根新 K 到达后自然从 `len` 处接上。
          · `bar_ts` 比缓冲最老一根还旧（进程停机超过缓冲长度 400×15m≈4 天）
            → 也返回 `len(b15)`，并留 ERROR + 风险事件。
            这里**绝不能返回 0**：返回 0 就是"把一条几万根之后才成立的计划
            扔进历史里跑"，也就是 11-24 那次事故本身。
            宁可让这条 thesis 停在原地等人看见，也不能让它凭历史行情
            产出一张市价单砸在真实持仓上。
        """
        try:
            want = int(bar_ts or 0)
        except (TypeError, ValueError):
            return 0
        if want <= 0 or not b15:
            return 0
        arr = [int(getattr(b, "ts", 0) or 0) for b in b15]
        i = bisect.bisect_left(arr, want)
        if i >= len(arr):
            return len(arr)          # 计划那根还没到，或恰好是下一根 → 不动
        if arr[0] > want:
            logger.error(
                "[挂单] 计划的 bar_ts=%d 早于行情缓冲最早一根 %d"
                "（缓冲 %d 根）→ 拒绝从历史重放，本轮跳过该 thesis",
                want, arr[0], len(arr))
            return len(arr)
        return max(0, i)

    def _pending_plan_ts(self, row) -> int:
        """这条预期**最初那根计划 K** 的时间戳（稳定锚点）。

        ★★ 为什么不能直接用 `row["bar_ts"]`：那一列在每次推进之后会被覆盖成
          当前 5m 的 ts（`upsert_thesis(..., bar_ts=ts)`），因为动作 id 是
          `thesis_id:bar_ts:kind`，bar_ts 固定就会让不同步骤的同类动作撞成
          同一个 id、被幂等逻辑当重放丢掉。所以它**必须**往前走 ——
          但也因此**不能**用来回答「这条计划是哪根出来的」。

        ★ 稳定事实在哪：`thesis_id` 是 `_pending_plan_key()` 造出来的
          `形态|方向|声明时刻|挂单价`，落库后永不改写。取倒数第二个字段
          就是那条计划自己的声明时刻，进程重启、游标推进都不影响。

        ★ 解析不出来（自检单 `selfcheck-<ts>` 这类）就退回 `bar_ts` ——
          宁可拿到一个会漂的值，也不能抛错让整条链停摆。
        """
        tid = str((row or {}).get("thesis_id") or "")
        parts = tid.split("|")
        if len(parts) >= 3:
            try:
                ts = int(parts[-2])
                if ts > 0:
                    return ts
            except (TypeError, ValueError):
                pass
        try:
            return int((row or {}).get("bar_ts") or 0)
        except (TypeError, ValueError):
            return 0

    def _pending_repair_params(self, row, th):
        """把**旧代码落库的 0 参数**按当前口径补上（一次性数据修复）。

        ★ 为什么需要：P1 修好的是"新计划不再丢参数"，但 2026-11-25 之前
          落库的行里 `horizon_bars/stale_distance/surprise_pts` 全是 0。
          这些行还在 live 列表里，一旦挂上/成交，它们就是**没有超时、
          没有跑偏撤销、没有惊喜欢出场**的仓位 —— 等于规则依旧关闭。

        ★ 只补 0 值，不覆盖任何非 0 值：将来的口径变化不会把历史行改掉。
        ★ stale_distance 用行内已有的 `order_price` / `stop_price` 反算
          （|挂单价−止损价| 就是 1R，与 `_stop_distance` 同一口径），
          所以不需要原始 evidence。
        """
        try:
            params = self._pending_params()
        except Exception:
            return th
        from . import thesis as _THESIS
        import dataclasses
        # ★ `Thesis` 是 **frozen dataclass**：直接赋值会抛 FrozenInstanceError
        #   （而 `_step_pending` 外面那层 `except` 会把它记成「推进失败」
        #   然后跳过这条预期 —— 静默失效，正是本项目最难查的一类）。
        #   所以一律用 `dataclasses.replace` 造新对象。
        patch = {}
        if not int(getattr(th, "horizon_bars", 0) or 0) \
                and int(getattr(params, "horizon_bars", 0) or 0) > 0:
            patch["horizon_bars"] = int(params.horizon_bars)
        if not float(getattr(th, "surprise_pts", 0.0) or 0.0) \
                and float(getattr(params, "surprise_pts", 0.0) or 0.0) > 0:
            patch["surprise_pts"] = float(params.surprise_pts)
        if not float(getattr(th, "stale_distance", 0.0) or 0.0) \
                and getattr(th, "family", "") == _THESIS.FAMILY_REVERSAL:
            sd = abs(float(getattr(th, "order_price", 0.0) or 0.0)
                     - float(getattr(th, "stop_price", 0.0) or 0.0))
            mult = float(getattr(params, "stale_stop_mult", 1.0) or 0.0)
            if sd > 0 and mult > 0:
                patch["stale_distance"] = round(sd * mult, 4)
        if not patch:
            return th
        logger.warning(
            "[挂单] %s 旧行参数全是 0（规则处于关闭状态）→ 按当前口径补：%s",
            str(getattr(th, "thesis_id", "") or "")[:40],
            "、".join("%s=%r" % (k, v) for k, v in patch.items()))
        return dataclasses.replace(th, **patch)

    async def _step_pending(self, bars_by_iv, price, equity, now_ms) -> None:
        """把每条 live 预期往前推一根 5m，然后执行状态机产出的动作。

        ★ 三个不变量（少一个都会静默错账）：

        1. **只在新的 5m 收盘推进一次**。`_step` 由 3s 桶触发，一根 5m 里
           会进来约 100 次。不判重就等于把同一根 K 推进 100 次：成交判定
           会被重复执行，游标会被推过头。
        2. **先执行动作、再前移游标**。反过来的话「游标前移了但单没挂上」
           会漏掉一次挂单，而且下一根不会补 —— 因为那根已经算「处理过」。
        3. **动作按 (thesis, bar_ts, kind) 幂等**。进程重启后同一根 K 会被
           重新算出来一次（游标可能刚好没落盘），止损不能平第二次。
        """
        if self.pending is None:
            self._pending_warn_once(
                "strategy=pending 但没有注入预期仓库（PendingThesisStore）。"
                "本轮不交易 —— 绝不能落进剥头皮链：那是一个已证伪的负策略，"
                "而且日志里策略名还写着 pending。")
            return

        raw5 = list((bars_by_iv or {}).get(300000) or ())
        if not raw5:
            return
        ts = int(getattr(raw5[-1], "open_time", 0) or 0)
        if ts <= self._pending_5m_ts:
            return                      # 还没有新的 5m 收盘
        self._pending_5m_ts = ts

        # ★★ 接管来的仓：挂单链**不挂新单**，但**要正确管它的出场**
        #    （2026-11-24 生产事故 + 用户 2026-11-24 明确要求）。
        #
        #   事故原貌：用户 11-23 手工买的 0.032 BTC 被接管，11-24 09:00
        #   挂单链拿**4 天前**的 K 线推出「观察线拒绝」，用市价平在 61289.4。
        #   用户的原话是「系统帮我管理手工单的出场我是满意的，只是要正确」——
        #   所以正确起见不是「不管」，而是「用当前行情管」。
        #
        #   接管仓的出场判据见 `_step_adopted_exit`（**BUS-D 之后只有两条**）：
        #     1) P0 保命：用**交易所真实爆仓价**算"离爆仓还有几个止损宽度"
        #     2) P1 止损：价格越过止损价才走（带一次交易所权威价复核）
        #
        #   ★ 2026-11-24 审查（F11）：这里原来还列着「观察线穿透后收回」
        #     与「结构失效」两条 —— 它们已被 BUS-D **删除**。
        #     文档比代码多讲两条判据，比缺文档更危险：读的人会以为
        #     那两条还在跑，排查时永远找不到对得上的代码。
        #
        #   ★ 不设固定止盈：人工仓没有 R 依据（没按风险预算定过仓），
        #     硬编一个 4R 目标等于凭空造规则，而且用户明确说「准备拿久一点」。
        #
        # ★★ 2026-11-24 第二次纠正（用户）：「有手工仓时不挂新单，不代表系统
        #   不决策。决策行为要落库留痕，以便回溯，只是因为手工的存在，所以不
        #   真实下单而已。系统决策跟下不下单没一点关系，系统不停，决策不止。」
        #
        #   所以这里**不是**「接管仓就整段跳过」，而是——
        #     · 决策照常做：采集计划、推进每一条 live thesis（含 ARMED 的成交判定）
        #     · 只把 `place_order` 那一个动作压住，并把它写成一条决策
        #     · ARMED 的 thesis 可能已经在交易所有真实挂单，它的成交属于
        #       **加仓**，必须照样落账（用户：「属于加仓行为，当然要执行」）
        #   用一个布尔带下去，不改变主流程结构 —— 少一个分叉就少一处
        #   「同一件事两个来源」。
        b5 = self._to_pending_bars(raw5)
        b15 = self._to_pending_bars((bars_by_iv or {}).get(900000))
        try:
            book = self.pending_lines() if self.pending_lines else None
        except Exception as e:
            logger.error("[挂单] 取线值失败，本轮按「无线」处理：%s", e)
            book = None
        if book is None:
            from . import thesis as _T
            book = _T.LineBook()

        # 接管仓的出场管理**不在这里**（2026-11-24 审查 F2 修正）。
        #
        # ★ 原来这里有一句 `await self._step_adopted_exit(...)`，但它写在
        #   `ts <= self._pending_5m_ts: return` 这条 5m 节流**之后** ——
        #   于是人工仓的止损/爆仓保护实际上每 5 分钟才判一次。
        #   现在唯一的调用点在 `_step` 里（3 秒粒度、与策略无关），
        #   这里只保留"有没有人工仓"这个事实用于压制新挂单。
        _adopted = self._is_adopted()

        # ★ 先采集新计划，再取 live 列表：
        #   采集出来的新计划应该在**同一根 K** 被推进一步，
        #   而它们恰恰就是刚生效的那一根。
        self._pending_harvest(b15, b5)

        # ★ 链路自检单：只有请求文件存在时才触发一次（见 trading/selfcheck.py）。
        #   必须放在这里而不是启动时：它要「当前这根 15m」的游标和现价，
        #   启动那一刻两者都还没有；游标给错会让状态机从历史第一根重放成交。
        try:
            await self._step_selfcheck(b15, b5)
        except Exception as e:
            logger.error("[自检] 执行失败：%s", e, exc_info=True)
            try:
                self.db.log_risk("selfcheck_error", str(e)[:220])
            except Exception:
                pass

        # ★★ S5 出场判据的输入。
        #   拿不到就**必须喊**：否则三道闸全部退化为「永不触发」，
        #   而看板上显示的是一笔一笔正常持仓 —— 没人会怀疑闸没数据。
        kbar_view = self._pending_kbar_view()

        rows = self.pending.live(limit=200)
        if not rows:
            # 空仓不等于出错：可能这段时间就是没有计划。
            # 但要让看板能回答「为什么没动静」，所以写一条决策日志。
            self._log_decision(bars_by_iv, price, equity, now_ms, None, "watch",
                               "挂单策略：当前没有 live 预期（计划源为空）")
            return

        from . import pending_thesis_store as _PTS
        from . import thesis as _T

        _briefs = []          # 每根 bar 的逐条处境，循环外汇总成一条决策
        # ★ BUS-B：本根是否有预期被闸门拦下、以及具体被哪道闸拦的。
        #   `_blocked_any` 决定汇总决策的 action 名字 ——
        #   "被拦下"和"在等触发"必须是两种不同的动作，
        #   否则决策流上二者长得一样，用户又要靠猜。
        _blocked_any = False
        _gates_all = []
        for row in rows:
            tid = str(row.get("thesis_id") or "")
            try:
                th = _PTS.thesis_of(row)
                prog = _PTS.progress_of(row)
                where_px = float(getattr(th, "order_price", 0.0) or 0.0)
                # ★ 旧行参数补 0 修复（见 `_pending_repair_params` 的 docstring）：
                #   不补的话历史行永远带着「无超时/无跑偏撤销/无惊喜欢出场」。
                th = self._pending_repair_params(row, th)
                # ★ 稳定的计划锚点：`bar_ts` 列会被推进覆盖，只有 `thesis_id`
                #   里的声明时刻不变（见 `_pending_plan_ts`）。
                _plan_ts = self._pending_plan_ts(row)

                # ★★★ 幽灵成交自愈（2026-11-24）。
                #
                #   生产库里有一条 `state=FILLED` 但 `filled_ts=0`、
                #   `filled_price=0.0`、成交动作一条都没有的 thesis，
                #   而交易所那张 `AI-pend-*` 单从未存在过（orders 表 0 行），
                #   账本和交易所都是空仓。它是**旧代码从 K 线推断出来的假成交**。
                #
                #   不修的话它会永远停在 FILLED：每根 5m 都推一个 close 动作，
                #   `_pending_close` 看到空仓就返回、不 mark_applied ——
                #   于是每 5 分钟重试一次、永远重试，日志和决策表被噪声淹没，
                #   真正该关注的信号被埋掉。
                #
                #   判据只用 `filled_ts`：真成交一定带成交时刻
                #   （`_pending_book_fill` 落账时写），推断出来的假成交没有。
                #   这不会误伤真仓 —— 真仓若要清理，那是 reconcile 的活。
                if (str(row.get("state") or "") == "FILLED"
                        and not float(row.get("filled_ts") or 0)
                        and not self.pos
                        and not getattr(self, "adopted_pos", None)):
                    logger.warning(
                        "[挂单] %s 状态是 FILLED 但没有任何成交记录、"
                        "账本与交易所都空仓 → 判定为旧的幽灵成交，按 CANCELLED 终结",
                        tid)
                    try:
                        self.db.log_risk(
                            "pending_phantom_filled",
                            "%s: filled_ts=0, filled_price=%r, 无仓无成交"
                            % (tid, row.get("filled_price")))
                    except Exception:
                        pass
                    _row = dict(row)
                    _row["state"] = "CANCELLED"
                    _row["note"] = ("判定为幽灵成交：无成交记录、"
                                    "账本与交易所均空仓（2026-11-24 修复）")
                    self.pending.upsert_thesis(_row)
                    _briefs.append("幽灵成交已按 CANCELLED 终结")
                    continue

                # ★★ 入场价已被穿过 → 这条预期作废（2026-11-25 交易域审查）。
                #
                #   三条形态都是「等价格**回到**这个位置再进」（限价/止损单），
                #   前提是价格还没到过那儿。可一旦状态机被冻住（见下面游标
                #   那段），计划会在 EXPECTED 里躺几十个小时 —— 期间价格早就
                #   穿过去了。
                #
                #   这时再挂出去是什么后果，实测算得出来：
                #     本地 4 条预期挂单价 60221.10 / 60447.90 / 59892.90，
                #     而当时市价 59329.60 —— 多头限价挂在市价**上方**。
                #     限价买单一挂出**立刻按市价成交**，可止损还是按原计划
                #     算的 59289.77：离成交价只剩 0.07%，22 倍杠杆下等于
                #     开仓即爆仓。这不是「滑点」，是拿一个已经不存在的
                #     入场价在算风险。
                #
                #   判据是**二值事实**，不是可调阈值：价格有没有穿过入场价。
                #   方向复用 `triggers_when_price_falls`（全仓唯一来源），
                #   不在这里按 side/direction 另写一份。
                if (str(row.get("state") or "") == "EXPECTED"
                        and price > 0 and where_px > 0):
                    _crossed = (price < where_px) if th.triggers_when_price_falls \
                        else (price > where_px)
                    if _crossed:
                        logger.warning(
                            "[挂单] %s 入场价 %.2f 已被穿过（现价 %.2f）→ "
                            "「等价格回到这里」的前提不成立，计划作废",
                            tid, where_px, price)
                        try:
                            self.db.log_risk(
                                "pending_entry_crossed",
                                "%s: 入场价 %.2f，现价 %.2f，"
                                "挂出去会立即成交且止损只剩 %.3f%%"
                                % (tid, where_px, price,
                                   abs(where_px - float(th.stop_price))
                                   / where_px * 100.0))
                        except Exception:
                            pass
                        _row = dict(row)
                        _row["state"] = "CANCELLED"
                        _row["note"] = ("入场价已被穿过：计划那根之后价格"
                                        "已越过 %.2f，不再具备「等回踩」的前提"
                                        % where_px)
                        self.pending.upsert_thesis(_row)
                        _briefs.append("%s 入场价已被穿过 → 计划作废" % tid[:12])
                        continue

                # ★★ 首次推进：起点必须是这条 thesis 自己的那根计划 K。
                #
                #   `row["bar_ts"]` 在第一次 upsert 之前仍然是计划的那根
                #   （`_pending_harvest` 写的是 `pl.bar_ts`），推进过一次之后
                #   会被覆盖成当前 5m 的 ts —— 我们只用它在 next_index==0 时，
                #   所以这个「只在首步有效」的取值恰好是对的。
                #
                #   不能改成「让 bar_ts 永不更新」：action_id 是
                #   `thesis_id:bar_ts:kind`，bar_ts 一旦固定，不同步骤产出的
                #   同类动作 id 会撞，第二次会被幂等逻辑当成重放而丢掉。
                # ★★ 游标冻结修复（2026-11-25 交易域审查）。
                #
                #   `_pending_start_index` 在「计划那根还没进 15m 缓冲」时
                #   返回 `len(b15)` 当哨兵（见它自己的 docstring）。但历史上
                #   这个哨兵被**当成合法游标落了库**。而下面的 `if not next_index`
                #   只对 0 重算，于是下一轮直接拿 `start_at = len(b15)`：
                #
                #       range(len(b15), len(b15))  →  一根都不遍历
                #
                #   → 状态机**永久冻结**：既不挂单、也不过期，决策里还每 5 分钟
                #     复述一遍「N 条预期在管」。实测生产 4 条 EXPECTED 全部
                #     `next_index=400`（`feed.history` 的 deque maxlen 正好 400）、
                #     `armed_at_bar=None`、动作表零行 —— 就是冻在这里。
                #
                #   修法两件：
                #     1. 哨兵值不再落库（否则 0 与非 0 的语义就混了）；
                #     2. 已经落库的哨兵（下一句的重算能算进缓冲了）清回 0 重算。
                _ni = int(prog.get("next_index") or 0)
                _nb = len(b15)
                #: 落库时要写的游标。None = 用状态机返回的 newp；
                #: 数字 = 「计划那根还没进缓冲」，本轮空转且**不许前移游标**。
                _persist_next = None
                if _ni and _ni >= _nb and _nb > 0:
                    # 游标停在缓冲末尾：只有在「重算能拿到合法下标」时才清 —
                    # 拿不到就说明计划那根还没进来，保持不动是对的。
                    _re = self._pending_start_index(b15, _plan_ts)
                    if _re < _nb:
                        logger.warning(
                            "[挂单] %s 游标卡在 %d（=缓冲长度 %d），哨兵值被误落库 → "
                            "清回重算（真实起点 %d）", tid, _ni, _nb, _re)
                        _ni = 0
                        prog = dict(prog)
                        prog["next_index"] = 0
                if not _ni:
                    prog = dict(prog)
                    _start = self._pending_start_index(b15, _plan_ts)
                    if _start >= _nb:
                        # ★ 计划那根比缓冲里所有 K 都新 → 本轮不推进。
                        #   但**必须照常走完一轮**（决策连续性：被压制/被闸门
                        #   拦下的计划也要逐条留痕），所以让 step 空转
                        #   （start=len），并把游标锁回 0 落库 ——
                        #   落 len 的话下一轮 `if not next_index` 不重算，
                        #   就固化成上面那个永久冻结。
                        _persist_next = 0
                        _start = _nb
                    prog["next_index"] = _start
                res, newp = _T.step(th, b15, book, progress=prog,
                                    entry_bars=b5, kbar=kbar_view)
            except Exception as e:
                logger.error("[挂单] 推进 %s 失败：%s", tid, e, exc_info=True)
                self.db.log_risk("pending_step_error", "%s: %s" % (tid, str(e)[:200]))
                continue

            placed = []
            failed_place = False
            suppressed = False
            # ★ BUS-B：这条预期的挂单被**哪道闸**拦住、理由是什么。
            #   空串 = 没被拦（要么挂了、要么压根没到挂单那一步）。
            blocked = ""
            # ★ MGN-3：本轮算出的手数（只有真走到仓位闸才有）。每条预期重置。
            #   不重置的话，上一条的手数会被写进这一条的决策文案 ——
            #   与 F9 记录的 `_last_route` 串台是同一个坑。
            _placed_qty = 0.0
            for act in res.actions:
                # ★ 有接管来的人工仓时：**只压住 place_order 这一个动作**，
                #   其它动作（成交落账、出场）照常执行 —— 用户 2026-11-24：
                #   「系统决策跟下不下单没一点关系」，而已挂在交易所的系统单
                #   成交属于加仓，必须执行。
                if _adopted and str(getattr(act, "kind", "") or "") == "place_order":
                    suppressed = True
                    logger.warning(
                        "[挂单] %s 决策为【应挂单】但存在接管的人工仓 → 本根不下单"
                        "（决策已落库，人工仓平掉后若条件仍成立会自动重挂）", tid)
                    # ★ 这里**不写**决策行：`_log_decision` 按 bar_time 去重，
                    #   在循环里写会把本根的汇总行顶掉（只留下第一条被压制的计划）。
                    #   压制详情统一由循环外的汇总写入，且每条计划单独列明
                    #   挂单价/止损/数量 —— 那才是回溯时要看的东西。
                    #
                    # ★★ 必须 `break` 而不是 `continue`：同一批 actions 里
                    #   排在 place_order 后面的成交/出场动作**全部以「这张单存在」
                    #   为前提**。单没挂出去还去执行它们，等于拿一个不存在的仓位
                    #   下单 —— 11-24 那 15 条幽灵动作正是这么来的
                    #   （状态机从 K 线推出 FILLED，再推出 close，交易所那头没单）。
                    #   宁可这一轮什么都不做，下一根重新推导。
                    logger.warning(
                        "[挂单] %s 丢弃本批剩余的 %d 个动作（它们都以该挂单存在为前提）",
                        tid, len(res.actions) - len(placed) - 1)
                    break
                done = await self._apply_pending_action(act, th, ts)
                if done:
                    placed.append(act.kind)
                elif str(getattr(act, "kind", "") or "") == "place_order":
                    failed_place = True
                    # ★ BUS-B：把路由层的拒绝原因取出来（哪道闸 + 为什么）。
                    #   以前这里只知道"没挂成"，于是决策行只能写"等触发"，
                    #   而真正的原因（安全闸/仓位不可行/交易所拒单）只进了
                    #   risk_events —— 决策流上看不见，用户就被误导了。
                    _r = getattr(self, "_last_route", None)
                    if _r is not None and not _r.placed:
                        blocked = str(getattr(_r, "rejection", "") or "")
                    if not blocked:
                        blocked = "挂单未成功（路由层未给出原因，查 risk_events）"
                    # ★ MGN-3：手数记在路由过程上（与 `_last_route` 同源、同时清空），
                    #   决策文案才能写出「本应挂多少」。取不到就是 0，文案不写手数。
                    _placed_qty = float(getattr(self, "_last_route_qty", 0.0) or 0.0)
                    # ★★ 挂单没成 → 本批**后续动作一律不做**。
                    #
                    #   同一批 actions 里 place_order 后面的成交/出场动作，
                    #   全部都以「这张单存在」为前提。单没挂出去还去执行它们，
                    #   等于拿一个不存在的仓位下单 —— 这正是事故的形态：
                    #   交易所没单，状态机却推出 FILLED 再推出 close。
                    #   宁可这一轮什么都不做（状态退回 EXPECTED、下一根重来）。
                    logger.warning(
                        "[挂单] %s 挂单未成功 → 丢弃本批剩余的 %d 个动作",
                        tid, len(res.actions) - len(placed) - 1)
                    break

            # ★★ 挂单没成功 → 状态**退回 EXPECTED**，绝不落成 ARMED。
            #
            #   状态机判成交只看 K 线（`_fill_condition` 比对挂单价），
            #   它不会去问交易所「我那张单还在吗」。所以只要状态是 ARMED，
            #   后面就会照着价格走出 FILLED → 出场动作 —— 而交易所那头
            #   从来没有这张单（2026-11-24：15 条 thesis 全是这种幽灵动作）。
            #
            #   退回 EXPECTED 而不是 CANCELLED：这类失败（安全闸、仓位不可行、
            #   交易所拒单）大多是**可恢复**的，下一根重新 claim + 重挂
            #   才是原本的设计意图（「挂单，容忍5/6/7……直到成交或撤单」）。
            #   坏消息不静默：仍然打 WARNING + risk 事件（在 _pending_place 里）。
            _p_state = newp["state"]
            _p_armed = newp.get("armed_at_bar")
            _p_armed_sub = newp.get("armed_at_sub_ts")
            _p_ref = newp.get("armed_ref_close")
            # ★★ 自相矛盾终态拦截（2026-11-25 真钱事故）。
            #
            #   `CLOSED_TP/SL/THESIS` 的前提是「有一笔仓被平掉了」。
            #   而引擎的仓位事实（`self.pos`）才是唯一口径 ——
            #   状态机只从 K 线推演，它不知道单到底有没有挂出去、有没有成交。
            #
            #   实测：14:50 挂单成功、14:55 推出 CLOSED_TP，而交易所
            #   executed_qty=0、self.pos 为空。落成终态之后就不会再有
            #   任何一轮来管这张单，于是它变成孤儿。
            #
            #   所以：没有仓位事实就不许进 CLOSED_*。退回 ARMED 让下一轮
            #   继续用「挂单之后的 K」判成交（配合 thesis.py 的
            #   `i > armed_at_bar` 守卫），并留一条风险事件说明这次拦截。
            if (str(_p_state) in (_T.CLOSED_TP, _T.CLOSED_SL,
                                 _T.CLOSED_THESIS)
                    and not float((self.pos or {}).get("qty") or 0.0)):
                logger.error(
                    "[挂单] %s 状态机推出终态 %s，但引擎没有对应的仓位事实"
                    " → 拦截（不落终态，退回 ARMED 重新判）",
                    tid, _p_state)
                self.db.log_risk(
                    "pending_terminal_without_position",
                    "%s: 状态机给出 %s，但 self.pos 为空" % (tid, _p_state))
                _p_state = _T.ARMED
            if failed_place:
                _p_state = _T.EXPECTED
                _p_armed = None
                _p_armed_sub = None
                _p_ref = None
                logger.warning(
                    "[挂单] %s 本根未成功挂出 → 状态退回 EXPECTED（不判成交、不平仓）",
                    tid)
            elif suppressed:
                # 被人工仓压住：同理，**不能落成 ARMED**。状态机判成交只看 K 线，
                # 一旦 ARMED 就会顺着价格推出 FILLED → 出场动作，
                # 而交易所那头根本没有这张单（11-24 那 15 条幽灵动作）。
                _p_state = _T.EXPECTED
                _p_armed = None
                _p_armed_sub = None
                _p_ref = None

            # ★ 动作落地之后才前移游标（不变量 2）
            #   `_persist_next` 非 None = 计划那根还没进缓冲、本轮空转：
            #   此时**绝不能**把 `len(b15)` 这个哨兵写进库（写进去就永久冻结）。
            _next_index = newp["next_index"] if _persist_next is None \
                else int(_persist_next)
            try:
                self.pending.upsert_thesis(_PTS.row_of(
                    th, state=_p_state, next_index=_next_index,
                    armed_at_bar=_p_armed,
                    armed_at_sub_ts=_p_armed_sub,
                    armed_ref_close=_p_ref,
                    bar_ts=ts, ambiguous=bool(res.ambiguous),
                    note=",".join(placed) or ("place_failed" if failed_place else "")))
            except Exception as e:
                logger.error("[挂单] 落库 %s 失败：%s", tid, e, exc_info=True)
                self.db.log_risk("pending_store_error", "%s: %s" % (tid, str(e)[:200]))

            if placed:
                self._log_decision(
                    bars_by_iv, price, equity, now_ms, th.kind, "pending",
                    "挂单策略 %s %s：%s → %s（%s）"
                    % (th.direction, tid, res.state, newp["state"],
                       "只记计划（dry-run）" if self._pending_is_dry() else ",".join(placed)))

            # ★★ 成交那一刻必须落账。
            #
            #   2026-11-20 实测：自检单 10:05:06 成交，什么都没写库；
            #   10:09:30 周期对账发现「数据库 +0.000000(空仓)，交易所 +0.005000」，
            #   按 §13.2 判成**人工仓接管** → 止损被换成 25% 兜底、止盈清零、
            #   且「接管仓不判策略出场」。于是挂单策略的每一笔真实成交，
            #   5 分钟后都会变成一笔不受策略保护的裸仓。
            if str(newp["state"]) == _T.FILLED and str(prog.get("state")) != _T.FILLED:
                await self._pending_book_fill(th)

            # ★ 逐条留痕，但**不在这里落库** —— 攒到循环外写一条汇总，
            #   因为 `_log_decision` 本身按 3s 桶去重（同一根 bar 只保留第一条），
            #   在循环里写会被静默丢掉，只剩最后一条/第一条，等于没有。
            _desc = self._pending_thesis_brief(th, _p_state, price, suppressed,
                                               blocked=blocked,
                                               qty=_placed_qty)
            if _desc:
                _briefs.append(_desc)
            if blocked:
                _blocked_any = True
                _gates_all.append({"gate": self._blocked_gate(blocked), "ok": False,
                                   "detail": blocked})

        # ★★ A1：**每根 5m 收盘，只要还有 live 预期，就写一条决策**。
        #
        #   用户 2026-11-24：「系统决策跟下不下单没一点关系，系统不停，决策不止，
        #   你所有的决策不用管有一个存储，用于在 web 端不断的显示出系统决策行为吗？」
        #   事故前这条链只在两种情况下写 decisions：
        #     · 没有任何 live 行（计划源为空）
        #     · 本根**真的挂出去了**（`placed` 非空）
        #   于是「在等触发价」「挂单还挂在交易所」「被人工仓压住没下」这三种
        #   最需要回溯的状态，一条记录都没有 —— 生产库 11-24 全天只有 1 行决策，
        #   而那 1 行还是事故本身。这就是「决策不止」名存实亡的根因。
        if _briefs:
            # ★ BUS-B：动作名必须区分「被闸门拦下」和「正常在等」。
            #   action 是决策流的筛选项和颜色依据（看板按 pending* 着色），
            #   如果拦截也写成 pending，用户过滤时根本看不出"整晚没动"是因为被挡着。
            if _blocked_any:
                _act = "pending_blocked"
            elif _adopted:
                _act = "pending_suppressed"
            else:
                _act = "pending"
            self._log_decision(
                bars_by_iv, price, equity, now_ms,
                ("pending_adopted" if _adopted else "pending"),
                _act,
                "挂单策略：%d 条预期在管｜%s" % (len(_briefs), "；".join(_briefs)),
                # ★ 拦截信息进 gates 列：和另外两条开仓路径（scalp/trend）
                #   的决策形状对齐 —— 那边被闸门拦下时都会带 gates，
                #   挂单这边以前是空的，看板上就少了一整列因果。
                gates=_gates_all or None,
                # ★ force：本根可能已经被 `_step_adopted_exit` 写过一行"在管"，
                #   但那是**另一件事**（人工仓出不出场）。这条是「系统决策了什么」，
                #   少了它，用户要的「决策不止」在库面上就断了。
                force=True)

    @staticmethod
    def _blocked_gate(rejection: str) -> str:
        """从路由层的拒绝文本里抽出闸门名。

        路由层给的形状是 ``"闸门名：理由"``（见 `order_router.route`），
        而 `decisions.gates` 要的是 ``{"gate":..., "ok":..., "detail":...}``。
        这里拆开，保证两个读者（日志 / 决策表）看到的是同一份事实。
        """
        s = str(rejection or "")
        head, sep, tail = s.partition("：")
        if sep and head and len(head) <= 40:
            return head
        return "出站拦截"

    def _pending_thesis_brief(self, th, state, price, suppressed=False,
                              blocked="", qty=0.0) -> str:
        """把一条预期的当前处境压成一句话（给决策表用）。

        ★ 只描述**事实**，不做判断：这条 thesis 现在在哪一步、在等什么、
          离触发还有多远。回溯时缺的正是这些，而不是又一句「跳过」。

        ★ BUS-B `blocked`：这一条为什么**没能挂出去**。
          以前挂单失败时决策行写的是「等触发（挂单 X / 止损 Y）」——
          那句话在失败场景下是**假的**：单根本没挂出去，价格永远等不到它。
          现在把闸门名和理由原样带出来，决策流上看到的就是真因。
        """
        try:
            tid = str(getattr(th, "thesis_id", "") or "")
            short = tid.split("|")[0][:12] if tid else "?"
            where = float(getattr(th, "order_price", 0.0) or 0.0)
            stop = float(getattr(th, "stop_price", 0.0) or 0.0)
            dist = ""
            if where > 0 and price > 0:
                dist = "，距挂单价 %.2f%%" % (abs(price - where) / where * 100.0)
            # ★ MGN-3（2026-11-25）：手数**只能**由调用方传进来。
            #   原来这两支写 `getattr(th, "qty", 0.0)`，而 `Thesis` 根本没有
            #   qty 字段 —— 于是决策行长期写着「应挂 up 0.000000 @ 58930.00」，
            #   读起来像"算出来的手数是 0"，真实手数要到仓位闸里才产生。
            #   （我自己就被这行字误导过一次，以为定仓算出了 0。）
            #   未知就不写手数，绝不编一个 0 出来。
            try:
                _qty = float(qty or 0.0)
            except (TypeError, ValueError):
                _qty = 0.0
            _qty_s = (" %.6f" % _qty) if _qty > 0 else ""
            st = str(state or "")
            if suppressed:
                # ★ A2：被压制的那条计划必须**逐条**记清「本应挂什么价、多少量、
                #   止损在哪、为什么没下」。只记一个计数等于没记 —— 回溯时
                #   看不出系统当时到底想做什么。
                _tgt = float(getattr(th, "target_price", 0.0) or 0.0)
                return ("%s 应挂%s%s @ %.2f（止损 %.2f / 止盈 %.2f）%s"
                        "——因存在接管的人工仓，只决策不下单"
                        % (short, str(getattr(th, "direction", "") or ""),
                           _qty_s, where, stop, _tgt, dist))
            if blocked:
                # 决策为「应挂单」但被闸门拦下：写清**本应挂什么** + **被谁拦的**。
                _tgt = float(getattr(th, "target_price", 0.0) or 0.0)
                return ("%s 应挂%s%s @ %.2f（止损 %.2f / 止盈 %.2f）%s"
                        "——被【%s】拦下，未下单"
                        % (short, str(getattr(th, "direction", "") or ""),
                           _qty_s, where, stop, _tgt, dist, blocked))
            if st == "EXPECTED":
                # ★ P2（2026-11-25 交易域审查）：EXPECTED 的真实含义是
                #   「计划已采集，但**声明(claim)还没成立，因此一张单都没挂**」。
                #
                #   原来这里写「等触发（挂单 60221.10 / 止损 59289.77）」——
                #   读起来完全是「交易所上有一张单挂在 60221 等成交」。
                #   用户据此以为有 4 张单在等工作，实际交易所上一张都没有
                #   （日志每 5 分钟都在喊「对账：交易所 1 张 / 本地 1 张」，
                #   而那张是别人的遗留单）。
                #
                #   BUS-B 已经修过一次同类问题（被闸门拦下时不能说"等触发"），
                #   但 EXPECTED 这一支漏了 —— 它压根不是失败，是"还没到那一步"。
                #   所以这里必须把「未挂单」写出来，而不是留一个能被读成
                #   "已挂单"的句子。
                return ("%s 计划已采集、**未挂单**（拟挂 %.2f / 止损 %.2f）"
                        "%s——等声明成立才下单" % (short, where, stop, dist))
            if st == "ARMED":
                return "%s 挂单在交易所（%.2f）%s" % (short, where, dist)
            if st in ("FILLED", "PARTIALLY_FILLED"):
                _b = float((self.pos or {}).get("best") or 0.0)
                _w = float((self.pos or {}).get("worst") or 0.0)
                return "%s 已成交（最优 %.2f / 最差 %.2f，止损 %.2f）" % (
                    short, _b, _w, stop)
            return "%s %s%s" % (short, st or "?", dist)
        except Exception:
            return ""

    async def _pending_book_fill(self, th) -> None:
        """挂单成交 → 写一条开仓 trade，并把仓位设进 `self.pos`。

        仓位事实取自交易所（`ex.position()`），不拿计划量顶替：
        部分成交、滑点后的实际手数与计划不是一个数，而止损/平仓都要用实际量。

        ★★ 但「交易所有仓」≠「这笔仓是我的」（2026-11-24 生产事故）。

          `ex.position()` 返回的是**账户级**净持仓，它分不清这笔仓是谁开的。
          用户手工买的仓在对账接管之前（最多 5 分钟窗口）也会出现在这里，
          于是「我的限价单成交了」和「用户手工买入了」在代码里长得一模一样 ——
          一旦按自己成交落账，这笔人工仓就被「洗」成了策略仓，
          之后 `_pending_close` 的归属校验也拦不住（因为判据正是我们写进去的）。

          ★ 判据只能问交易所：`query_order(client_id=...)`。
            我这张单在不在、成交了没有、成交了多少 —— 只有交易所答得上来。

          ★ 为什么不用「价格不变量」（我第一版写错了，留在这里当反例）：
            限价买单的成交价**可以低于**挂单价（更优成交），而事故那笔
            手工仓 entry=60880 恰好**低于**挂单价 61178.10 ——
            「买价不高于限价」这条规则会**放行**它。价格长得对，
            不证明仓位归属对。所以这个思路根本救不了，必须查单。
        """
        from . import thesis as _T

        qty = 0.0
        entry = float(th.order_price)
        try:
            actual = await self.ex.position()
            qty = abs(float(actual.get("qty") or 0.0))
            entry = float(actual.get("entry") or 0.0) or entry
        except Exception as e:
            logger.error("[挂单] %s 成交后查持仓失败：%s", th.thesis_id, e)

        if qty <= 0:
            # 查不到就**记一笔风险**而不是编一个量：编量会让后面的止损
            # 保护一个不存在的仓，比不记账更危险。
            logger.error("[挂单] %s 已成交但查不到持仓量，无法落账", th.thesis_id)
            self.db.log_risk("pending_fill_unbooked", str(th.thesis_id)[:200])
            return

        # ★ 归属证据：我那张单真的成交了吗？
        _filled_qty = await pending_own_filled_qty(self.ex, th.thesis_id)
        if _filled_qty is None:
            logger.error(
                "[挂单] %s 判定成交，但交易所查不到这张单的成交"
                "（client_id=%s）—— 那笔仓不是我们开的，不落账、不接管",
                th.thesis_id, pending_client_id(th.thesis_id))
            try:
                self.db.log_risk(
                    "pending_fill_not_ours",
                    "%s: entry=%.2f qty=%.6f"
                    % (th.thesis_id, entry, qty))
            except Exception:
                pass
            return

        side = "long" if str(th.direction).lower() == _T.UP else "short"
        now_ms = int(time.time() * 1000)
        stop = float(th.stop_price)
        target = float(th.target_price)
        # ★★ 入账数量必须是【我这张单成交了多少】，不是账户净持仓（2026-11-24）。
        #
        #   旧写法 `qty = abs(actual["qty"])` 是账户级净量：
        #     用户手工仓 0.032 + 我这单成交 0.010 → 净 0.042
        #     → 写一条 qty=0.042、open_reason='挂单成交' 的交易
        #     → 用户那 0.032 被"洗"成策略仓，它自己的入场价和止损一起消失。
        #   这正是 11-24 事故的同一族缺陷：一个来源覆盖了另一个来源。
        #   用户 2026-11-24 明确要求「已存在的单子是系统单子，属于加仓行为，
        #   当然要执行」—— 加仓要执行，但只能记自己那一份。
        book_qty = float(_filled_qty)
        if book_qty <= 0:
            logger.error("[挂单] %s 查单成交量为 %r，不落账", th.thesis_id, book_qty)
            return
        if book_qty > qty + 1e-9:
            # 理论上不可能：我们这单的量不可能超过账户净量。
            # 出现就是数据不自洽，宁可停手也不写一个超量。
            logger.error("[挂单] %s 查单成交量 %.6f 超过账户净量 %.6f，拒绝落账",
                         th.thesis_id, book_qty, qty)
            self.db.log_risk("pending_fill_qty_inconsistent",
                             "%s: own=%.6f net=%.6f"
                             % (th.thesis_id, book_qty, qty))
            return
        # 我这一份的成交价：账户均价里混着人工仓的成本，能查到就用自己的均价
        entry = float(getattr(th, "order_price", 0.0) or 0.0) or entry
        # ★★ 开仓手续费必须记真实值（2026-11-24 统一口径）。
        #   以前这里写死 `fee: 0.0`，于是 `close_booking()` 算净盈亏时
        #   开仓费永远是 0 —— 就算平仓路径改对了，这一侧的窟窿还在。
        #   查不到真实 commission 就按 taker 估算（保守方向：多算成本）。
        open_fee = await pending_own_fill_fee(self.ex, th.thesis_id)
        if open_fee <= 0:
            try:
                _is_spot = bool(getattr(self.tcfg, "is_spot", False))
                _rate = self.tcfg.taker_fee_for(_is_spot)
            except Exception:
                _rate = 0.0005
            open_fee = abs(book_qty * entry * _rate)
            logger.warning(
                "[挂单] %s 查不到开仓真实手续费，按 taker %.4f%% 估算为 %.6fU",
                th.thesis_id, _rate * 100.0, open_fee)
        else:
            logger.info("[挂单] %s 开仓手续费（交易所真实值）%.6fU",
                        th.thesis_id, open_fee)
        try:
            self.db.log_trade({
                "symbol": self.tcfg.symbol, "side": side, "qty": book_qty,
                "entry_ts": now_ms, "entry_price": entry,
                "stop_price": stop, "target_price": target,
                "fee": open_fee, "open_reason": PENDING_FILL_REASON, "paper": 0,
                "strategy": self.tcfg.strategy,
                "max_hold_bars": self.tcfg.max_hold_bars})
        except Exception as e:
            logger.error("[挂单] %s 成交落账失败：%s", th.thesis_id, e, exc_info=True)
            self.db.log_risk("pending_fill_book_error",
                             "%s: %s" % (th.thesis_id, str(e)[:200]))
            return

        # ★ 加仓：**系统仓**同向就累加，绝不碰 `adopted_pos`（人工仓）。
        #
        #   以前这里是 `self.pos = {...}` 无条件覆盖 —— 有手工仓在时不只
        #   吞掉人工仓，还会把它的止损换成本 thesis 的止损。
        #   用户 2026-11-24：「假如已存在的单子是系统单子，属于加仓行为，
        #   当然要执行」—— 执行，而且必须记成系统仓，不是覆盖整笔仓。
        if self.pos and norm_side(self.pos.get("side")) == side:
            _oq = float(self.pos.get("qty") or 0.0)
            _oe = float(self.pos.get("entry") or 0.0)
            _nq = _oq + book_qty
            self.pos["entry"] = ((_oe * _oq + entry * book_qty) / _nq) if _nq > 0 else entry
            self.pos["qty"] = _nq
            # 止损/止盈取**更近的那一条**（更保守的一侧）：
            # 加仓后暴露变大，保护必须跟着收紧的方向走，不能放宽。
            for _k in ("stop", "target"):
                _old = float(self.pos.get(_k) or 0.0)
                _new = float((stop if _k == "stop" else target) or 0.0)
                if _old > 0 and _new > 0:
                    if side == "long":
                        self.pos[_k] = min(_old, _new) if _k == "stop" else max(_old, _new)
                    else:
                        self.pos[_k] = max(_old, _new) if _k == "stop" else min(_old, _new)
                elif _new > 0:
                    self.pos[_k] = _new
            logger.warning("[挂单] %s 系统仓加仓：+%.6f @ %.2f → 合计 %.6f，均价 %.2f",
                           th.thesis_id, book_qty, entry, _nq, self.pos["entry"])
        elif self.pos:
            # 反向成交 = 先平后反手，不属于"加仓"，拒绝自动处理交人工。
            logger.error("[挂单] %s 成交方向 %s 与现有系统仓 %s 相反，拒绝覆盖",
                         th.thesis_id, side, self.pos.get("side"))
            self.db.log_risk("pending_fill_opposite_side",
                             "%s: new=%s/%s old=%s/%s"
                             % (th.thesis_id, side, book_qty,
                                self.pos.get("side"), self.pos.get("qty")))
            try:
                self.guard.position_mismatch(
                    "挂单成交方向与现有系统仓相反：%s，需人工核查" % th.thesis_id)
            except Exception:
                pass
            return
        else:
            self.pos = {"side": side, "qty": book_qty, "entry": entry,
                        "stop": stop, "target": target, "entry_ts": now_ms,
                        "bars": 0, "best": entry, "worst": entry, "trade_id": None,
                        "max_hold_bars": self.tcfg.max_hold_bars,
                        "open_reason": PENDING_FILL_REASON, "paper": 0,
                        "fee": open_fee}
        logger.warning("[挂单] %s 成交落账：%s %.6f @ %.2f（止损 %.2f / 止盈 %.2f）",
                       th.thesis_id, side, book_qty, entry, stop, target)

    @staticmethod
    def _to_pending_bars(seq) -> list:
        """把行情池的 `Bar` 转成状态机要的 `(ts/o/h/l/c)` 形状。

        ★★ 这一步**不能省**，而且不能交给 `pending_plan._norm`。

          `feed` 的 `Bar` 是 NamedTuple，字段顺序是
              (interval_ms, open_time, open, high, low, close, volume)
          而 `pending_plan._norm` 对“没有 .ts/.o 属性”的对象按
              (ts, o, h, l, c) 取下标。

          两边一对上就是灾难：time 变成 interval_ms(3000)、open 变成
          open_time(1.7e12)、close 变成 high。不报错、不抛异常，只是所有价位
          判定全部错位。上一次这类型的错在这个项目里叫做“同一事实两个来源”，
          已经出事过 8 次。所以这里**按字段名显式取**，不靠位置对齐。
        """
        from .pending_plan import _Bar
        out = []
        for b in (seq or ()):
            if hasattr(b, "open_time") and hasattr(b, "close"):
                out.append(_Bar(b.open_time, b.open, b.high, b.low, b.close))
            elif hasattr(b, "ts") and hasattr(b, "c"):
                out.append(b)
            else:
                out.append(_Bar(b[0], b[1], b[2], b[3], b[4]))
        return out

    async def _apply_pending_action(self, act, thesis_obj, bar_ts: int) -> bool:
        """把一条动作真正落地。返回 True = 已执行（或已按 dry-run 记账）。

        ★ 幂等：`put_action` 返回 False 表示这批动作以前算出来过
          （进程重启后重算同一根 K），此时直接跳过执行。
        """
        tid = str(getattr(act, "thesis_id", "") or "")
        kind = str(getattr(act, "kind", "") or "")
        if not kind:
            return False
        try:
            fresh = self.pending.put_action(
                thesis_id=tid, kind=kind, state_after=str(getattr(act, "state_after", "") or ""),
                bar_ts=bar_ts, price=float(getattr(act, "price", 0.0) or 0.0),
                payload=getattr(act, "to_dict", lambda: {})())
        except Exception as e:
            logger.error("[挂单] 记动作失败 %s %s：%s", tid, kind, e)
            return False
        if not fresh:
            return False                # 幂等：这条动作已经处理过

        dry = self._pending_is_dry()
        if dry:
            logger.info("[挂单][dry-run] %s %s %s @ %.1f（只记计划，不挂单）",
                        tid, kind, getattr(act, "side", ""), float(getattr(act, "price", 0.0) or 0.0))
            self.pending.mark_applied(tid, kind, bar_ts)
            return True

        try:
            if kind == "place_order":
                # ★ 下单没成 → 这条动作**不能**记成已执行。
                #
                #   以前无论成败都往下走到 `mark_applied`，于是交易所那头
                #   什么都没有、库里写着「已挂单」。状态机接着按 K 线判成交，
                #   一路推出幽灵平仓单（2026-11-24：15 条 thesis 全部如此）。
                #   留一个「未 applied」的动作，上层才能把状态退回 EXPECTED。
                if not await self._pending_place(act, thesis_obj):
                    return False
            elif kind == "cancel":
                await self._pending_cancel(act)
            elif kind == "close":
                await self._pending_close(act, thesis_obj)
            else:
                logger.warning("[挂单] 未知动作 %r（%s），已记录但未执行", kind, tid)
                return False
            self.pending.mark_applied(tid, kind, bar_ts)
            return True
        except Exception as e:
            logger.error("[挂单] 执行动作失败 %s %s：%s", tid, kind, e, exc_info=True)
            self.db.log_risk("pending_action_error",
                             "%s %s: %s" % (tid, kind, str(e)[:200]))
            return False

    # ---------------- 挂单策略：仓位与执行 ----------------

    def _pending_size(self, entry: float, stop: float,
                      order_type: str = "") -> Tuple[float, str]:
        """按**项目已定的仓位口径**算一笔挂单的手数。返回 ``(qty, 拒绝理由)``。

        ★★ 唯一口径是 `sizing.plan_position()` —— 我不是"参考"它，是把
          判定权完全交给它。这里只额外做**交易所规格**那一层（步长取整、
          最小名义复查），因为那是币安的一手/最小额，不是风控口径。

        ★ 为什么必须这么改（2026-11-17 修正的一个真缺陷）：
          上一版我写了

              notional = min(risk_usd / stop_frac, margin_cap x leverage)

          也就是**把保证金上限当成"夹一下"**。但项目规则（sizing.py 模块
          docstring §3.1）明确写的是「仓位由风险定仓，保证金比例只是
          **上限校验**，不参与定仓」，并给了理由：夹完之后的止损距离会落到
          双边手续费+滑点的噪音里。两条实现给出相反答案 —— 实测同一批
          11 条真实计划：

              plan_position()    → 0/11 可开（保证金 27.0~76.6U 超上限 20U）
              _pending_size()    → 11/11 可开，但**实际风险只有 0.45~1.31U**
                                   而配置说 2U

          也就是说：看着像在下 5% 风险的仓，实际一半都不到，而且不报错。
          这就是这个项目反复吃亏的「同一件事两个来源」。现在只留一个来源。

        ★ 50U 本金下的真实约束（写在这里免得下次又去猜）：

              能开仓 ⟺ 止损% ≥ 单笔风险% ÷ (保证金上限% × 杠杆)

          本金在不等式两边约掉了 —— 所以「本金加大」并不能让窄止损变得可开，
          除非同时动这三个量里的某一个。
        """
        entry = float(entry or 0.0)
        stop = float(stop or 0.0)
        if entry <= 0 or stop <= 0:
            return 0.0, "计划缺少入场价或止损价（entry=%r stop=%r）" % (entry, stop)
        stop_frac = abs(entry - stop) / entry
        if stop_frac <= 0:
            return 0.0, "止损距离为 0：这笔单没有任何保护"

        # ★★ 成本必须进风险预算。
        #
        #   只用 `R ÷ 止损距离` 定仓时，止损被打时亏的是
        #   「风险 + 手续费 + 滑点」，不是 R。实测 11 条真实计划：
        #   用形态止损定仓时实际亏 2.61~3.74 倍 R（手续费占风险 31%~87%）。
        #
        #   口径：`round_trip_costs()`就是成本的唯一来源，
        #   与 `plan_prices()` 同一个函数 —— 不允许各算一份。
        from .pending_plan import PendingParams, round_trip_costs
        try:
            _cw, cost_pts = round_trip_costs(
                entry, (order_type or "LIMIT").upper(), PendingParams())
        except Exception:
            logger.warning("[挂单] 成本口径不可用，拒绝开仓", exc_info=True)
            return 0.0, "成本口径不可用，拒绝开仓"
        cost_frac = cost_pts / entry if entry else 0.0
        # 风险预算 = 止损距离 + 成本。两者合起来才是「最坏亏 R」。
        risk_frac = stop_frac + cost_frac

        # ★ 取数必须走 `_sizing_params()`，不要直接 `self.settings.sizing()`。
        #
        # 两者不是一回事：`settings.sizing()` 只有 3 个键（本金/风险/杠杆），
        # 而 `_sizing_params()` 还带 `sizing.margin_cap_pct`（100%）—— 它是守卫
        # 实际用的那份。直接拿 settings.sizing() 去建 SizingConfig，
        # 保证金上限会静默落回 dataclass 默认的 40%，于是
        # 同一笔计划“守卫说能开、挂单说不能”。
        params = self._sizing_params()
        if params is None:
            return 0.0, "仓位参数不可用（读不到 Web 配置），拒绝开仓"

        try:
            from .sizing import SizingConfig, plan_position
            cfg = SizingConfig(
                capital=float(params["sizing.capital"]),
                risk_pct=float(params["sizing.risk_pct"]),
                leverage=float(params["sizing.leverage"]),
                # 保证金上限：与守卫同一份（默认 100% = 不额外截断）。
                # 注意这不是“放宽”—— 它只在**风险定仓**路径上生效，
                # 真正限制仓位的仍然是 R/止损%。
                margin_cap_pct=float(params.get("sizing.margin_cap_pct", 100.0)))
            # ★ 传入的是**含成本**的风险距离 —— 这才是真正会亏的那个量。
            #   计划层已把止损往里让出成本（d_max - cost），
            #   所以 risk_frac ≈ d_max，正好贴着爆仓安全上限，不会超。
            pp = plan_position(cfg, risk_frac * 100.0)
        except Exception as e:
            return 0.0, "仓位参数不可用：%s" % str(e)[:120]

        # ★ 判定权在 plan_position：它说不行就是不行，这里不许"夹一下再说"。
        if not pp.ok:
            return 0.0, "风控拒绝：%s" % "；".join(pp.reasons)

        # ---- 以下才是这一层独有的：交易所规格 ----
        notional = float(pp.notional)
        if notional <= 0:
            return 0.0, "风控算出名义为 0（本金过小或止损距离异常）"
        step = float(self._qty_step() or 0.0)
        if step <= 0:
            return 0.0, "步长为 0，无法取整"
        lots = int(notional / entry / step)
        qty = round(lots * step, 10)
        if qty <= 0:
            return 0.0, ("取整后为 0（名义 %.2fU / 价 %.1f / 步长 %r）"
                         % (notional, entry, step))
        real_notional = qty * entry
        if real_notional < self._min_notional():
            return 0.0, ("取整后名义 %.2fU 低于最小下单 %.2fU"
                         % (real_notional, self._min_notional()))
        # 取整只会变小，所以保证金不会反而超限；这里只是把数字带出去记账。
        margin = real_notional / max(float(cfg.leverage), 1e-9)
        if margin > pp.margin + 1e-9:      # pragma: no cover - 防御性
            return 0.0, "取整后保证金 %.2fU 反而超过 %.2fU（取整方向错了）" % (
                margin, pp.margin)
        return qty, ""

    async def _pending_place(self, act, thesis_obj) -> bool:
        """挂一张单。仓位来自 `_pending_size`，不在这里算任何数。

        Returns:
            True  = 这张单**真的挂出去了**（交易所接受了）。
            False = 没挂出去（安全闸拦下 / 仓位不可行 / 交易所拒绝）。

        ★ 为什么必须返回这个布尔（2026-11-24 生产事故）：

          以前这个函数是 `-> None`，所有失败分支都只是「打个日志 + return」。
          于是上层 `_apply_pending_action` 无从区分「挂上了」和「没挂上」，
          照样 `mark_applied` + 把 thesis 写成 ARMED。而状态机的下一步
          是按**K 线**判成交的（`_fill_condition` 只看价格有没有走到挂单价），
          它不会去问交易所「我那张单还在吗」。

          实测：-4015 让每一张单都挂不出去，但 18 条 thesis 里 15 条都被
          推成了 FILLED 又平掉 —— 全是没有成交基础的幽灵动作。

          失败必须是**可观测的返回值**，不能只活在一行日志里。
        """
        tid = str(getattr(act, "thesis_id", "") or "")
        price = float(getattr(act, "price", 0.0) or 0.0) or float(thesis_obj.order_price)
        stop = float(thesis_obj.stop_price)

        # ★★ 2026-11-24 BUS-A：拦截与下单搬进**出站路由层**。
        #
        #   改之前是三段 if 平铺在这个函数里：
        #       guard.can_open()  →  _pending_size()  →  ex.place_order()
        #   同样的逻辑在另外三条开仓路径上各写了一遍，而且已经漂移过：
        #   2026-11-17 挂单路径**漏了** guard.can_open()，于是数据源 DOWN、
        #   日亏损上限、连亏冷却、熔断、持仓不一致这些保护在最新那条策略上
        #   全部静默失效，且不报错。
        #
        #   现在三关变成三个**具名闸门**，顺序在 `_pending_router()` 里一眼可数。
        #   行为零变化：每个闸内部的日志、risk 事件、返回语义全部逐字保留，
        #   连"闸门抛异常要冒到上层记 pending_action_error 并让动作不算已执行"
        #   这条也保留（路由层不吞异常，见 order_router 模块注释）。
        intent = EntryIntent(
            strategy=str(self.tcfg.strategy or "pending"),
            side=norm_side(getattr(act, "side", "")),
            entry=price, stop=stop,
            target=float(getattr(thesis_obj, "target_price", 0.0) or 0.0),
            order_type=str(getattr(act, "order_type", "") or "LIMIT").upper(),
            client_id=pending_client_id(tid),
            reason=str(getattr(act, "reason", "") or ""),
            thesis_id=tid,
            bar_ts=int(getattr(act, "bar_ts", 0) or 0),
        )
        # ctx 只承载"闸门之间要传递的数据"（手数），不放任何交易状态。
        ctx = {"qty": 0.0, "size_why": ""}
        # ★ F9（2026-11-24 审查）：**先清空再路由**。
        #   以前只在路由返回后才赋值，而路由层刻意不吞异常
        #   （见 order_router 模块注释）。于是路由抛异常时 `_last_route`
        #   仍是**上一条 thesis** 的拒绝原因，上层 3245 行读到它，
        #   就把别人的闸门名字写进了这一条的决策行 —— 一条张冠李戴的
        #   "证据"比没有证据更糟，排查时会一路追错方向。
        #   清空之后，上层走的是那句诚实的兜底文案。
        self._last_route = None
        # ★ MGN-3：手数与 `_last_route` **同时清空、同时赋值**。
        #   只清 `_last_route` 不清手数，就会出现「闸门名字是这一条的、
        #   手数是上一条的」——F9 已经因为同类串台追错一次方向。
        self._last_route_qty = 0.0
        res = await self._pending_router().route(intent, ctx)
        # ★ BUS-B：把路由结果留在引擎上，让上层能把**是哪道闸拦的**写进决策。
        #   这是"拒绝也必然产出一条决策"的传话通道：路由层不碰数据库
        #   （它只做编排），落库仍然只有引擎一个写入口。
        self._last_route = res
        self._last_route_qty = float(ctx.get("qty") or 0.0)
        return res.placed

    # ---------------- 挂单路径的闸门与下单动作 ----------------
    def _pending_router(self) -> OrderRouter:
        """挂单开仓的拦截链。**顺序即优先级，改顺序等于改风控。**

        1. 无人值守闸 —— 数据源/日亏损/连亏/熔断/持仓不一致
        2. 仓位闸     —— `sizing.plan_position()` 唯一口径 + 交易所规格
        3. 可用保证金闸 —— 账户付得起吗；不够先撤外来占位单（MGN-2）
        4. 下单       —— 只有前面的闸都放行才会执行

        ★ 为什么可用保证金闸必须排在仓位闸**之后**：
          它要用仓位闸算出来的手数（`ctx["qty"]`）去算"需要多少保证金"。
          排在前面就只能算用配置本金估的缺口，那和真实手数不是一个数。

        ★ 为什么必须排在**下单之前**（而不是等交易所回 -2019）：
          2026-11-25 生产上每一笔挂单都被 -2019 拒绝，而系统每 5 分钟
          重试一次、每次只落一行 ERROR，永远不恢复 —— 因为占住保证金的
          是别人的单，不清理就永远不会变得够用。

        每次调用现建一个（闸门是绑定方法，构造成本可忽略；
        缓存反而会让"配置改了但链还是旧的"这种问题重新出现）。
        """
        return OrderRouter(
            gates=[self._gate_pending_unattended, self._gate_pending_size,
                   self._gate_pending_margin],
            placer=self._place_pending_order)

    async def _gate_pending_unattended(self, intent, ctx) -> Optional[GateResult]:
        """无人值守闸：只在**开仓**时拦，撤单/平仓绝不能过这道闸。

        熔断期间反而是最需要能平仓的时候。
        """
        if self.guard is None:
            return None
        g_ok, g_why = self.guard.can_open()
        if g_ok:
            return GateResult(gate=GATE_UNATTENDED, ok=True)
        logger.warning("[挂单] %s 被安全闸拦下，不挂单：%s", intent.thesis_id, g_why)
        self.db.log_risk("pending_blocked_by_guard",
                         "%s: %s" % (intent.thesis_id, g_why))
        return GateResult(gate=GATE_UNATTENDED, ok=False, detail=str(g_why or ""))

    async def _gate_pending_size(self, intent, ctx) -> Optional[GateResult]:
        """仓位闸：手数由 `_pending_size()` 唯一决定，这里只搬运结论。"""
        qty, why = self._pending_size(
            intent.entry, intent.stop,
            str(intent.order_type or "").upper())
        if qty <= 0:
            logger.warning("[挂单] %s 计划不可行，本次不挂单：%s",
                           intent.thesis_id, why)
            self.db.log_risk("pending_unaffordable",
                             "%s: %s" % (intent.thesis_id, str(why)[:220]))
            return GateResult(gate=GATE_SIZING, ok=False, detail=str(why or ""))
        ctx["qty"] = qty
        ctx["size_why"] = why
        return GateResult(gate=GATE_SIZING, ok=True,
                          detail="%.6f 张" % qty)

    async def _gate_pending_margin(self, intent, ctx) -> Optional[GateResult]:
        """可用保证金闸：**下单前**确认账户还付得起这张单的保证金。

        ★ 为什么需要这道闸（2026-11-25 生产实录）：
          游标修好之后，预期第一次真的推进到"去挂单"，结果交易所回

              HTTP 400 /fapi/v1/order: {"code":-2019,
                                        "msg":"Margin is insufficient."}

          账户权益 114.18U，但交易所上有一张**遗留的、不属于系统的**
          挂单 `0.06 @ 57839`（名义 3470U，22 倍下要 ≈158U 保证金）——
          它把可用余额吃成了负数。于是每一笔新挂单都必然被拒，
          而系统每 5 分钟重试一次、每次都只留一行 ERROR，永远不恢复。

          这不是策略问题，是**账户资源管理**问题：余额被别人的单占着。

        ★ 用户的授权口径（2026-11-25 原话）：
          「你每次挂单前看一下当前可用保证金够不够，有没有挂单占用，
            这个账户余额完全归系统管理，不够就要撤单！系统完全拥有操作权限」

        ★ 判定只用**一个数**：`availableBalance`。
          它由交易所扣除了「持仓占用 + 所有未成交委托占用」，
          所以"够不够"不需要我们自己再减一遍挂单占用 ——
          自己减就会出现两个来源，而这个项目已经栽过很多次。

        ★★ 为什么不撤 `reduceOnly` 的外来单（重要）：
          reduceOnly 单只能减仓、开不出新仓，占用的保证金近乎为零；
          撤掉它**唯一**的后果是某个真实持仓失去保护（那可能是用户手工
          给人工仓挂的止损）。所以它落在"宁可不动手"那一侧：
          跳过并记一笔日志，绝不当成占位单清理。
          这条不是我在放宽授权 —— 撤它不解决保证金问题，只会制造裸仓。
        """
        tid = str(getattr(intent, "thesis_id", "") or "")
        # 纸面/不可查余额的执行器：跳过（没有"可用保证金"这个概念）。
        fn = getattr(self.ex, "available_margin", None)
        if fn is None:
            return GateResult(gate=GATE_MARGIN, ok=True,
                              detail="执行器不支持查可用保证金，跳过")

        need = self._pending_need_margin(ctx.get("qty"), intent.entry)
        if need <= 0:
            # 算不出需要多少，就不该用这个数去拦单 —— 但也不能默认放行。
            return GateResult(gate=GATE_MARGIN, ok=False,
                              detail="算不出本单需要的保证金（qty=%r entry=%r）"
                                     % (ctx.get("qty"), intent.entry))

        avail = await fn()
        if avail < 0:
            # ★ 未知 ≠ 没钱。查不到就按"不够"处理（保守），但要写明是查不到，
            #   否则报告会把"网络失败"说成"余额不足"，下次排查会一路追错方向。
            logger.warning("[挂单] %s 读不到可用保证金，本轮不挂单", tid)
            self.db.log_risk("pending_margin_unknown", tid)
            return GateResult(gate=GATE_MARGIN, ok=False,
                              detail="读不到可用保证金（交易所查询失败），不挂单")

        ctx["need_margin"] = need
        ctx["avail_margin"] = avail
        if avail + 1e-9 >= need:
            # 够就放行，**一张别人的单都不碰**。
            return GateResult(gate=GATE_MARGIN, ok=True,
                              detail="可用 %.2fU ≥ 需要 %.2fU" % (avail, need))

        # ---- 不够：清掉外来占位单 ----
        freed, kept_reduce_only, failures = await self._reclaim_margin(tid, need)
        avail2 = await fn()
        if avail2 >= 0:
            ctx["avail_margin"] = avail2
        if avail2 + 1e-9 >= need:
            logger.warning("[挂单] %s 可用保证金不足（%.2fU < %.2fU），"
                           "已撤 %d 张外来占位单释放保证金，现可用 %.2fU → 放行",
                           tid, avail, need, freed, avail2)
            self.db.log_risk(
                "pending_margin_reclaimed",
                "%s: 可用 %.2fU < 需要 %.2fU，撤销 %d 张外来挂单后可用 %.2fU"
                % (tid, avail, need, freed, avail2))
            return GateResult(gate=GATE_MARGIN, ok=True,
                              detail="撤销 %d 张外来挂单后可用 %.2fU ≥ %.2fU"
                                     % (freed, avail2, need))

        why = ("可用 %.2fU < 需要 %.2fU，已撤 %d 张外来挂单仍不足"
               % (avail2 if avail2 >= 0 else avail, need, freed))
        if kept_reduce_only:
            why += "（另跳过 %d 张 reduceOnly 外来单：撤它只会让持仓失去保护）" \
                   % kept_reduce_only
        if failures:
            why += "（%d 张撤单失败）" % failures
        logger.error("[挂单] %s %s", tid, why)
        self.db.log_risk("pending_margin_short", "%s: %s" % (tid, why))
        return GateResult(gate=GATE_MARGIN, ok=False, detail=why)

    def _pending_need_margin(self, qty, entry) -> float:
        """这张单需要多少保证金 = 名义 ÷ 杠杆。

        ★ 杠杆取 `_sizing_params()["sizing.leverage"]` —— 与
          `_pending_size()` 算仓位时用的**同一个来源**。
          另取一个"交易所实际杠杆"会造出两个真相：算仓位用 22 倍、
          查保证金用别的倍数，于是"算出来能开"和"实际开不了"永远对不上。
        """
        try:
            q = float(qty or 0.0)
            px = float(entry or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if q <= 0 or px <= 0:
            return 0.0
        params = self._sizing_params() or {}
        try:
            lev = float(params.get("sizing.leverage", 0) or 0)
        except (TypeError, ValueError):
            lev = 0.0
        if lev <= 0:
            return 0.0
        return q * px / lev

    async def _reclaim_margin(self, tid: str, need: float):
        """撤掉**外来**占位挂单，直到可用保证金够用（或没得撤了）。

        Returns:
            (撤掉张数, 跳过的 reduceOnly 张数, 撤单失败张数)

        ★ 只撤「开仓方向」的外来单：
          `client_id` 不以 `AI-` 开头 = 不是系统发的（`AI-pend-` / `AI-SL-`
          都带这个前缀）；且 `reduce_only` 为假。
          两个条件都满足它才可能是"占着保证金不让系统开仓"的那一类。

        ★ 每撤一张就重读一次可用余额并**立刻停止**：
          撤单是真实动作，多撤一张就是多改变一次账户状态。
          够用即停，不做"一次清空"。
        """
        freed = 0
        kept_ro = 0
        failed = 0
        try:
            rows = await self.ex.open_orders()
        except Exception as e:
            logger.warning("[挂单] %s 拉挂单列表失败，无法清理外来占位单：%s",
                           tid, e)
            return 0, 0, 1
        # 先按"能腾出的保证金"从大到小排：一张大单往往就够了，
        # 避免为了凑小钱撤掉一堆单。
        cands = []
        for o in (rows or []):
            try:
                cid = str(getattr(o, "client_id", "") or "")
                if cid.startswith(AI_CLIENT_PREFIX):
                    continue            # 自己的单，交给状态机管
                if bool(getattr(o, "reduce_only", False)):
                    kept_ro += 1        # 保护性单：撤它只会制造裸仓
                    continue
                q = float(getattr(o, "qty", 0) or 0)
                px = float(getattr(o, "price", 0) or 0) \
                    or float(getattr(o, "stop_price", 0) or 0)
                cands.append((q * px, cid, o))
            except Exception:
                continue
        cands.sort(key=lambda x: -x[0])

        fn_avail = getattr(self.ex, "available_margin", None)
        for notion, cid, o in cands:
            try:
                # 撤之前确认还挂着：已成交的单撤不掉，而"撤单失败"会
                # 把一次正常成交记成故障。
                qo = None
                qfn = getattr(self.ex, "query_order", None)
                if qfn is not None:
                    qo = await qfn(client_id=cid)
                if qo is not None and not getattr(qo, "working", True):
                    continue
                ok = await self.ex.cancel_order(
                    client_id=cid, order_id=str(getattr(o, "order_id", "") or ""))
            except Exception as e:
                logger.warning("[挂单] %s 撤外来挂单 %s 失败：%s", tid, cid, e)
                failed += 1
                continue
            if not ok:
                failed += 1
                continue
            freed += 1
            logger.warning(
                "[挂单] %s 撤掉外来占位挂单 %s（名义≈%.0fU，"
                "它把账户保证金占住了，系统无法开仓）", tid, cid, notion)
            self.db.log_risk("pending_foreign_order_cancelled",
                             "%s: 撤销外来挂单 %s（名义≈%.0fU）"
                             % (tid, cid, notion))
            if fn_avail is None:
                break
            cur = await fn_avail()
            if cur >= 0 and cur + 1e-9 >= need:
                break               # 够用即停
        return freed, kept_ro, failed

    async def _place_pending_order(self, intent, ctx):
        """真正的下单动作。**只有全部闸门放行才会被调用。**"""
        tid = intent.thesis_id
        qty = float(ctx.get("qty") or 0.0)
        side = str(intent.side or "").upper()
        otype = str(intent.order_type or "LIMIT").upper()
        # 下单接口认 buy/sell，不认 long/short —— 这里转一次，别把口径带下去。
        api_side = "BUY" if side == "LONG" else "SELL"
        order, err = await self.ex.place_order(
            api_side, qty, order_type=otype, price=intent.entry,
            client_id=intent.client_id)
        if err:
            logger.error("[挂单] %s 下单失败：%s", tid, err)
            self.db.log_risk("pending_place_failed", "%s: %s" % (tid, err[:220]))
            return False, None, str(err)
        logger.info("[挂单] %s %s %.6f @ %.1f（止损 %.1f，%s） -> %s",
                    tid, api_side, qty, intent.entry, intent.stop, otype,
                    getattr(order, "status", ""))
        self.db.log_risk("pending_placed",
                         "%s %s %.6f @ %.1f" % (tid, api_side, qty, intent.entry))
        return True, order, ""

    async def _pending_cancel(self, act) -> None:
        """撤挂单。“已经不在了”按成功处理（目的状态已达成）。

        ★ client_id 必须和 `_pending_place` 逐字一致（同一个 ``AI-pend-``
          前缀）。两处各写一份就会撤不掉自己的单，而报错只有
          "撤单失败" —— 项目里这类"同一事实两个来源"已经出过很多次，
          所以这里和下单处调**同一个函数** `pending_client_id()`。
        """
        tid = str(getattr(act, "thesis_id", "") or "")
        cid = pending_client_id(tid)
        fn = getattr(self.ex, "cancel_order", None)
        if fn is None:
            logger.warning("[挂单] %s 需要撤单，但执行器不支持撤单", tid)
            return
        ok = await fn(client_id=cid)
        if ok:
            logger.info("[挂单] %s 已撤单（%s）", tid, getattr(act, "reason", ""))
        else:
            logger.error("[挂单] %s 撤单失败（%s）", tid, getattr(act, "reason", ""))
            self.db.log_risk("pending_cancel_failed", tid)

    async def _pending_close(self, act, thesis_obj) -> None:
        """平掉持仓。手数用**仓位事实**（self.pos），不用计划里的 qty。

        ★ 为什么不能用计划 qty：计划的 qty 是「打算开多少」，而平仓要平「
          实际持有多少」。两者不一致的场景很常见（部分成交、手工干预、
          强平）。用计划量会平不干净或超卖 —— 而超卖在合约上等于反向开仓。
        """
        tid = str(getattr(act, "thesis_id", "") or "")
        pos = self.pos or {}
        qty = abs(float(pos.get("qty") or 0.0))
        if qty <= 0:
            # ★★ 空仓却要平仓 = 状态机推演出了「不存在的仓位」。
            #
            #   2026-11-25 线上实证：14:50 挂单成功、14:55 就来了一条 close，
            #   而交易所那两张 `AI-pend-*` 单始终是 NEW、executed_qty=0。
            #   原来这里只 `return` —— 于是那张**真实挂单**留在交易所，
            #   没有任何预期再管它（孤儿单）。若它之后成交，会被归因成
            #   "我方成交"从而触发硬停；不成交则白占保证金。
            #
            #   正确动作：这张单本来就不该存在，**撤掉它**。
            #   撤单是安全的：没有仓位，撤的是挂单，不可能亏钱。
            #
            # ★★ 只有「真的一无所有」才算幽灵（2026-11-25 二次实证）：
            #
            #   当天 16:45 自检单平仓时这里误判了一次：`self.pos` 是空的，
            #   但那笔仓被对账接管进了 `self.adopted_pos`（人工仓），
            #   于是日志写着「判定为幽灵成交」，**真实持仓却没被平掉**，
            #   仓就那么裸着开在交易所上。
            #
            #   所以判据必须是「系统仓 + 接管仓都为 0」。接管仓非空时
            #   绝不能撤单走人 —— 那等于一边说"没有仓"、一边把保护单撤了。
            #   也不能替用户平掉（§13.2：接管仓不判策略出场），
            #   所以记一条风险事件交给人看着，这是唯一不越权的处置。
            _adopted_qty = abs(float(
                (getattr(self, "adopted_pos", None) or {}).get("qty") or 0.0))
            if _adopted_qty > 0:
                logger.error("[挂单] %s 需要平仓，但系统仓为空、接管仓还有"
                             " %.6f → 这不是幽灵成交，是成交被归到了人工仓；"
                             "不动仓、不撤单，记风险事件等人处理",
                             tid, _adopted_qty)
                self.db.log_risk(
                    "pending_close_hits_adopted_position",
                    "%s 系统仓空 / 接管仓 %.6f / 差额待归因"
                    % (tid, _adopted_qty))
                return
            logger.warning("[挂单] %s 需要平仓但引擎认为空仓 →"
                           "判定为幽灵成交，撤掉交易所上这张单", tid)
            self.db.log_risk("pending_close_without_position", tid)
            from . import thesis as _T
            await self._pending_cancel(_T.Action(
                kind="cancel", thesis_id=tid,
                reason="幽灵成交（本地认为已成交，实际未成交）"))
            return

        # ★★ 只能平**自己开的**仓（2026-11-24 生产事故的第二道闸）。
        #
        #   判据用 `open_reason`：挂单成交写的是 `"挂单成交"`，而对账接管
        #   写的是 `ADOPT_REASON`（`实盘对账接管`）。手单/接管仓永远不等于
        #   `"挂单成交"`，所以这条能挡住「拿策略动作平人工仓」。
        #
        #   ★ 为什么不在这里看 `_is_adopted()`：那道闸在 `_step_pending`
        #     入口（更早、覆盖面更大）。这里是**纵深防御**：即使将来有人
        #     在别处（自检、补单、脚本）直接调 `_pending_close`，
        #     也不会把不属于这条 thesis 的仓卖掉。
        #
        #   实测事故：11-24 09:00 的 close 动作平掉的是
        #   `open_reason='实盘对账接管'`、qty 0.032、entry 60880 的人工仓，
        #   而那条 thesis 计划的量是 0.010 且**一张单都没挂出去**。
        _oreason = str(pos.get("open_reason") or "")
        if _oreason != PENDING_FILL_REASON:
            logger.error(
                "[挂单] %s 拒绝平仓：当前持仓不是挂单策略开的"
                "（open_reason=%r，数量 %.6f）—— 不碰它",
                tid, _oreason, qty)
            try:
                self.db.log_risk(
                    "pending_close_not_owned",
                    "%s: open_reason=%r qty=%.6f" % (tid, _oreason, qty))
            except Exception:
                pass
            return

        side = "SELL" if str(pos.get("side") or "long") == "long" else "BUY"

        # ★★ 平仓必须用 `market_order`，不能走 `place_order(order_type="MARKET")`。
        #
        #   2026-11-20 实测（链路自检单）：`place_order` 只接受
        #   LIMIT/STOP_MARKET/STOP/TAKE_PROFIT_MARKET，传 MARKET 会被执行器直接拒：
        #       [挂单] ... 平仓失败：不支持挂单类型 'MARKET'
        #   也就是说**挂单策略从来平不掉仓**。一天里唯一的证伪机会就是真成交一笔。
        #   平仓的专用入口一直是 `market_order(reduce_only=True)`（_open/_close 都走它，
        #   而且它会对 reduce_only 放行熔断）。
        fill = await self.ex.market_order(
            side, qty, reason=("挂单平仓 %s" % tid), reduce_only=True,
            client_id=_close_client_id("pend", tid))
        if not getattr(fill, "ok", False):
            err = str(getattr(fill, "reason", "") or "未知原因")
            logger.error("[挂单] %s 平仓失败：%s", tid, err)
            self.db.log_risk("pending_close_failed", "%s: %s" % (tid, err[:220]))
            raise RuntimeError(err)

        exit_price = float(getattr(fill, "price", 0.0) or 0.0)
        if exit_price <= 0:
            try:
                exit_price = float(await self.ex.mark_price())
            except Exception:
                exit_price = float(pos.get("entry") or 0.0)

        # ★ 平仓必须同步关账。不关的后果不是"少一行统计"：
        #   下一轮对账会看到「交易所有平、库里还开着」，而解释差额的成交
        #   带 `AI-` 前缀 → 判 ours → **硬停**。平仓路径自己制造硬停。
        entry = float(pos.get("entry") or 0.0)
        # ★★ 统一口径（2026-11-24）：毛/费/净一律走 `close_booking()`。
        #   这里以前写的是 `"fee": 0.0, "pnl_net": pnl` —— 开仓费和平仓费
        #   两边都不记，pnl_net 等于毛盈亏，系统性地偏乐观。
        pnl, fee_total, pnl_net = close_booking(
            pos, exit_price, getattr(fill, "fee", 0.0), qty,
            pos.get("side"))
        try:
            self.db.close_trade({
                "symbol": self.tcfg.symbol, "side": pos.get("side"),
                "qty": qty, "entry_ts": pos.get("entry_ts"),
                "entry_price": entry, "exit_ts": int(time.time() * 1000),
                "exit_price": exit_price,
                "stop_price": pos.get("stop"), "target_price": pos.get("target"),
                "pnl_gross": pnl, "fee": fee_total, "pnl_net": pnl_net,
                "mfe": 0.0, "mae": 0.0,
                "open_reason": pos.get("open_reason", ""),
                "close_reason": "挂单策略出场（%s）" % str(getattr(act, "reason", "")),
                "paper": int(pos.get("paper") or 0)})
        except Exception as e:
            logger.error("[挂单] %s 平仓已成交但关账失败：%s", tid, e, exc_info=True)
            self.db.log_risk("pending_close_book_failed", "%s: %s" % (tid, str(e)[:200]))
        # ★★ P3：把 R 倍数补上（2026-11-25 交易域审查）。
        #
        #   `pending_thesis_store.set_exit()` 从写出来那天起**零调用** ——
        #   全仓 grep 过：只有定义，没有任何调用点。于是生产库 22/22 行
        #   `net_r` 全是 0.0，连 6 笔止盈、9 笔止损都是 0。
        #
        #   这不是"少一列统计"：回测按 R 说话、实盘只记了 USDT，两者之间
        #   就少了一个兑换率 —— 库里根本回答不了「这套策略到底赚不赚」。
        #
        #   1R 的口径与开仓时**同一个**：`qty × |入场价 − 止损价|`
        #   （止损距离就是 1R，见 `pending_plan._stop_distance` 的 docstring）。
        #   分母为 0（没止损）时**不写**，而不是写 0 —— 写 0 会被读成
        #   "打平"，把"量不了"伪装成一个结论。
        try:
            _stop = float(pos.get("stop") or 0.0) or float(
                getattr(thesis_obj, "stop_price", 0.0) or 0.0)
            _risk = abs(entry - _stop) * qty
            if _risk > 0:
                self.pending.set_exit(
                    tid, int(time.time() * 1000), exit_price, pnl_net / _risk,
                    note="R = 净盈亏 ÷ (手数 × 入场到止损的距离)")
                logger.info("[挂单] %s 落 R：净 %+.2f / 1R %.4f = %+.3fR",
                            tid, pnl_net, _risk, pnl_net / _risk)
            else:
                logger.warning("[挂单] %s 平仓但算不出 1R（入场 %.2f / 止损 %r）"
                               "→ net_r 不写，保持原值", tid, entry, _stop)
        except Exception as e:
            logger.error("[挂单] %s 写 net_r 失败：%s", tid, e, exc_info=True)
            self.db.log_risk("pending_net_r_failed", "%s: %s" % (tid, str(e)[:200]))

        self.pos = None
        logger.info("[挂单] %s %s 平 %.6f @ %.2f（%s，毛 %+.2f / 费 %.4f / 净 %+.2f）",
                    tid, side, qty, exit_price, getattr(act, "reason", ""),
                    pnl, fee_total, pnl_net)

    def _plan_leverage(self) -> float:
        """计划杠杆。永续口径（现货那条 `return 1.0` 已删）。

        ★★ 2026-11-17 修：**挂单路径固定用配置的 `max_leverage`**。

        原来这里是 `min(trend_leverage, max_leverage)`，而
        `trend_leverage` 默认 **1.0** —— 于是 `main.py` 拿着
        1.0 去交易所设杠杆，而 `sizing` 那边按 22 倍算保证金。
        同一个仓位在两处有两个杠杆 —— 又一个「同一件事两个来源」。

        永续挂单的仓位是“风险定仓”算出来的，它的保证金、
        爆仓距离、最远止损都由这个杠杆推出来，不能取小。

        保留原引擎对 trend/scalp 的口径（它们是满仓思路，
        与风险定仓不同）—— 所以按策略分流，不是一刀切改。
        """
        if self._is_pending():
            lev = float(self.tcfg.max_leverage)
        else:
            lev = min(float(self.tcfg.trend_leverage),
                      float(self.tcfg.max_leverage))
        if lev <= 0:
            raise ValueError("杠杆必须 > 0（当前 %r），拒绝带着 0 杠杆下单"
                             % (lev,))
        return lev

    def _qty_step(self) -> float:
        """数量步长。★ 只有一个来源：`cfg.trading.qty_step`。

        以前这里有一个 `if _is_spot(): return 0.00001`。现货步长和永续差
        **100 倍**，用错不会报错，只会把仓位算成 0（永续按现货步长）
        或者差 100 倍（现货按永续步长）。分支删了，不存在“选错”这件事。
        """
        return self.tcfg.qty_step

    def _fee_buffer(self) -> float:
        """下单时预留的手续费/滑点缓冲。

        现货 0.3%：买入要付 taker 0.1%，再算滑点和取整余量，
        不留够的话账户 100U 按 100U 下单会被交易所按余额不足直接拒掉。

        永续也要留，只是以前留的是 0。以前没暴露是因为 100U 账户被
        step 取整救了：名义 100U / 价 78015 / step 0.001 -> 只有 0.001 手，
        真实名义 78U，自然空出 22U。
        但这个"空出来"是运气，不是设计：一旦权益刚好落在
        step 的整数倍上（例如 780.15U 时 floor 出 0.01 手 = 780.15U 名义），
        1x 下保证金 = 名义额 = 全部权益，再加上 taker 费，
        下单就会以「保证金不足」被拒——而且是在趋势信号刚出现的时刻。
        留 0.1%（覆盖 0.05% taker 与滑点）把这个洞堵上。
        """
        # ★ 永续 0.1%（覆盖 0.05% taker 与滑点）。现货那个 0.3% 已删。
        return 0.001

    def _affordability_note(self, price: float, equity: float) -> str:
        """资金不够建仓时返回说明文字，够则返回空串。

        为什么要单独判断：策略的"空仓等待"有两种完全不同的原因——
        信号没到（继续等就好）、或者钱不够（等再久也不会成交）。
        混在同一句日志里，运维会一直等一个永远不会来的成交。
        """
        if equity <= 0:
            return "余额为 0，无法交易｜请确认 USDT 在【合约钱包】"
        step = self._qty_step()
        lev = self._plan_leverage()
        need_notional = max(self._min_notional(), 1.0)
        # 一手 = 一个步长；连一手都不够就没法建仓
        one_lot = step * price / lev
        if equity < one_lot or equity * lev < need_notional:
            return ("资金不足：权益 %.2fU，建一手至少需要 %.2fU 名义（一手 %.6f BTC ≈ %.2fU）"
                    "｜请确认 USDT 在合约钱包" % (equity, need_notional, step, step * price))
        return ""

    def _min_notional(self) -> float:
        """最小名义（永续 55U 带 10% 余量）。现货那个 5U 已删。

        ★ 这个值对小账户是“能不能建仓”的硬门槛，不是建议值。
        """
        return self.tcfg.trend_min_notional

    def _sizing_params(self):
        """读 4 个仓位参数（本金/风险%/杠杆/保证金上限）。

        两层来源，顺序不能反：

        1. **settings（Web 配置库）优先**，每次都现读。
           这 4 个值可以在页面上实时修改，缓存一份就意味着
           "改完之后第一笔还按旧参数下单"。

        2. 读不到时退回 self.tcfg。
           main.py 启动时已经跑过 settings.apply_to(cfg.trading)，
           所以 tcfg 本身就是这 4 个参数的一份快照 —— 退回去用的
           仍然是真配置，不是硬编码默认值。这点必须说清楚：
           它不是"猜一个值继续下单"，而是"用启动时那份真值"。

        只有两层都拿不到（tcfg 缺字段）时才返回 None 并拒绝开仓。
        """
        # getattr 而不是 self.settings：有测试用 __new__ 直接造引擎再塞属性，
        # 绕过 __init__。硬属性访问会在那里抛 AttributeError，
        # 而那个异常长得像"代码写错了"，其实只是测试的构造方式不同。
        settings = getattr(self, "settings", None)
        if settings is not None:
            try:
                s = settings.sizing()
                if s:
                    return s
            except Exception:
                logger.warning("[ENGINE] 读 Web 仓位参数失败，退回启动快照",
                               exc_info=True)
        # 字段名必须和 SettingsStore.apply_to() 里的映射一致，否则会静默错位：
        #   sizing.capital  -> paper_balance   （TradingCfg 没有 capital 字段）
        #   sizing.risk_pct -> risk_pct
        #   sizing.leverage -> max_leverage
        # 这里曾经按 "capital"/"leverage" 去取，两个属性都不存在，
        # 于是每一笔都返回 None、每一笔都拒绝开仓 —— 全量测试里 60 个用例
        # 一起变红才暴露出来。硬属性名和实际 dataclass 对不上，就是这么静默。
        snap = {
            "sizing.capital": float(getattr(self.tcfg, "paper_balance", 0.0) or 0.0),
            "sizing.risk_pct": float(getattr(self.tcfg, "risk_pct", 0.0) or 0.0),
            "sizing.leverage": float(getattr(self.tcfg, "max_leverage", 0.0) or 0.0),
            # 保证金上限没有对应的 TradingCfg 字段，配置页也不暴露它，
            # 默认 100% = 不额外截断。
            "sizing.margin_cap_pct": 100.0,
        }
        if snap["sizing.capital"] <= 0 or snap["sizing.leverage"] <= 0:
            return None
        return snap

    def _pending_params(self):
        """生成挂单计划用的 `PendingParams`。

        ★★ 生产生成计划**必须**走这一个函数。

        它把 `max_stop_pct` 从 `sizing.derive()` 填进去，于是止损就是
        公式算出的最远距离。不走它——比如直接 `PendingParams()`——
        `max_stop_pct` 默认 0，止损会**静默落回振幅规则**，
        于是仓位被放大 10 倍以上、保证金装不下。

        这正是本项目反复吃亏的「同一件事两个来源」，所以这里把它变成
        **一个函数**，而不是让调用方自己拼参数。
        """
        from .pending_plan import PendingParams
        params = self._sizing_params()
        if params is None:
            return PendingParams()
        return PendingParams(max_stop_pct=self._max_stop_distance_pct(params))

    def _max_stop_distance_pct(self, params: dict) -> float:
        """止损距离上限 = 爆仓距离 / 安全倍数。

        纯公式，由 4 个仓位参数推出来，不依赖任何策略：
            杠杆 22x、维持保证金率 0.5%、安全倍数 2.5
            -> 爆仓距离 (1/22 - 0.005) x 100 = 4.045%
            -> 上限 4.045 / 2.5 = 1.618%

        它和配置页显示的那个 1.618% 必须是同一个来源（sizing.derive()），
        否则页面写一个数、闸门用另一个数，用户会以为闸门坏了。
        """
        try:
            from .sizing import SizingConfig, derive
            c = SizingConfig(
                capital=float(params.get("sizing.capital", 0.0) or 0.0),
                risk_pct=float(params.get("sizing.risk_pct", 0.0) or 1.0),
                leverage=float(params.get("sizing.leverage", 0.0) or 0.0))
            return float(derive(c).max_stop_distance_pct)
        except Exception:
            logger.warning("[ENGINE] 算止损距离上限失败，本项跳过校验", exc_info=True)
            return 0.0

    def _stop_distance_pct(self, ref_price: float = 0.0) -> float:
        """**任何一个仓位的止损距离（价格 %）** 的唯一来源。

        ★ 为什么要有这个函数（2026-11-24 生产缺陷）：

          同一个概念在这个项目里有三个数：
            `trend_stop_pct = 25.0`        ← 1 倍杠杆时代的价格跌幅，给趋势仓用
            `sizing.derive().max_stop...`  ← 1.618%，爆仓距离 / 安全倍数，给挂单用
            保证金亏损 25%                  ← 用户在 22 倍杠杆下的直觉（≈1.14% 价格）

          25% 价格跌幅在 22 倍杠杆下**永远晚于爆仓**（爆仓只差 4.15%），
          所以那是一个不可能触发、却在看板上显示成"保护已设好"的止损。
          三个数里只有 `爆仓距离 / 安全倍数` 这一个在数学上保证
          「止损一定先于爆仓」，所以其他需要兜底止损的地方一律用它。

        返回的是**价格百分比**（止损价 = 入场价 x (1 - 返回/100)），
        不是保证金百分比 —— 这个名字必须让人一眼看出量纲。

        `ref_price` 目前不参与计算，留着是为了将来按交易所返回的
        `liquidationPrice` 精确核对（现货/逐仓/全仓的爆仓价不同口径）。
        """
        params = self._sizing_params()
        if params is None:
            return 0.0
        return self._max_stop_distance_pct(params)

    def _trend_plan(self, price: float, equity: float, sig: dict):
        """趋势单的仓位：**只有一道闸门（仓位参数），由 gates.position_gate 统一算。**

        为什么改成调外部函数：老实现把"算 qty"和"查条件"写在同一段里，
        两处逻辑迟早漂移；一旦漂移，闸门拦的是它以为的数，下单用的是另一个数。
        现在算与查是同一个函数，返回值直接拿去下单，调用方不许重算。

        已被明确弃用的五道门（强平距离/成本门/波动门/可达性/止损结构）：
        它们原本属于剥头皮路径，donchian 从来没走过；按决定全部不要，
        等策略重新设计后再议。所以这里没有 stop/target/强平距离校验。
        """
        from .risk import Plan
        from .gates import position_gate
        lev = self._plan_leverage()
        cap = float(self.tcfg.max_leverage)
        # 关键：不能拿 100% 权益去买。买入还要付手续费，
        # 账户 100U 时按 100U 下单会被交易所按"余额不足"直接拒掉——
        # 也就是说这里少留一点，第一笔真实交易就会失败。
        # 留 0.3% 缓冲（taker 0.1%~0.075%，再加滑点与取整余量）。
        params = self._sizing_params()
        if params is None:
            return Plan(ok=False, reason="仓位参数不可用（读不到 Web 配置），拒绝开仓")
        g = position_gate(
            equity=equity, price=price, leverage=lev,
            qty_step=self._qty_step(), min_qty=self._qty_step(),
            min_notional=self._min_notional(), max_leverage=cap,
            capital=params.get("sizing.capital", 0.0),
            risk_pct=params.get("sizing.risk_pct", 0.0),
            # 保证金上限对趋势路径按"不额外截断"处理，理由是它和 1x 满仓互斥：
            # trend_leverage=1.0 时保证金 == 名义 == 权益，恒等于本金的 100%，
            # 任何小于 100% 的上限都会把每一笔都拦掉（实测 14 个既有用例全红）。
            # sizing.py 里那个 40% 是【风险定仓路径】plan_position() 的口径，
            # 两条路径的杠杆模式不同，不能共用一个上限。
            # 闸门本身支持这个参数，策略接上后按需传值即可。
            margin_cap_pct=params.get("sizing.margin_cap_pct", 100.0),
            fee_buffer=self._fee_buffer(),
            # 下面三个是「上游算好交给闸门复核」的入参。
            # 现在策略还没定、止损距离给不出来，所以全传 0 -> 闸门按
            # "有有效入参才校验"跳过，不会拿编出来的数去拦单。
            # 等策略接上，把信号算出的止损距离和计划量填进来即可，
            # 闸门这边一行都不用改。
            stop_distance_pct=0.0,
            max_stop_distance_pct=self._max_stop_distance_pct(params),
        )
        if not g.ok:
            return Plan(ok=False, reason=g["detail"], gates=[dict(g)])
        qty = g["qty"]
        real_notional = g["notional"]
        # 唐奇安【刻意不设硬停损】：它的出场就是跌破 M 根新低。
        # 这里给 0 不是漏写——实测加 2ATR 停损使每笔 +2.42%->+1.39%、
        # 100U 账户终值 689U->559U。给 0 之后引擎里所有 "price <= p['stop']"
        # 的判据恒为假，硬熔断这条路径对唐奇安自动失效，与回测一致。
        if self._is_donchian():
            stop = 0.0
            stop_pct = 0.0
        else:
            stop = price * (1 - self.tcfg.trend_stop_pct / 100.0)
            stop_pct = self.tcfg.trend_stop_pct
        return Plan(ok=True, reason="ok", side="long", qty=qty, entry=price,
                    stop=stop, target=0.0,
                    stop_pct=stop_pct, tp_pct=0.0,
                    notional=real_notional, leverage=real_notional / equity if equity else 0.0,
                    risk_usdt=qty * (price - stop) if stop else 0.0,
                    liq_distance_pct=(1.0 / (real_notional / equity) - 0.004) * 100
                    if equity and real_notional else 0.0,
                    gates=[dict(g)], max_hold_bars=10 ** 9)

    async def _open(self, sig, plan, price, equity, now_ms, feats, bars_by_iv) -> None:
        # 权益数据过期时一律不开仓：不知道有多少钱就下注，
        # 轻则被交易所拒单，重则按错误规模建仓。
        # 放在这里而不是各调用点，是因为进场有三条路径，
        # 将来加第四条时在这里自动被覆盖。
        if getattr(self, "_equity_stale", False):
            self._log_decision(bars_by_iv, price, equity, now_ms, sig["name"], "skip",
                               "权益数据过期（查询失败），暂停开仓",
                               gates=[], risk_info=None)
            return
        side_txt = plan.side
        order_side = "buy" if side_txt == "long" else "sell"
        # ★ 现货零头接管已删（2026-11-17 只做合约）。
        #
        # 原来这段在开仓前读一次账上基础币，把小于一个 step 的零头一起卖掉；
        # 它存在的原因是现货手续费扣在基础币上，平仓取整必然留残留。
        # 永续按名义合约交易，不存在这个残留，而且它会多查一次权益
        # （实盘里是一次 HTTP）、在行情最激烈的时候。
        pre_dust = 0.0
        # ★ 2026-11-25：id 必须**在下单前**定好并下发给交易所。
        #   原来这里先下单、后算 `client_id`，于是交易所那张单上是它自己
        #   生成的随机 id，本地这条 `client_id` 永远对不上任何成交 ——
        #   成交归因（靠 `AI-` 前缀认领）对此完全失明。
        self._seq += 1
        client_id = "AI-%d-%d" % (now_ms, self._seq)
        fill = await self.ex.market_order(
            order_side, plan.qty,
            reason="%s 开仓 %s" % (sig["name"], side_txt),
            client_id=client_id)
        self.db.log_order({"ts": now_ms, "client_id": client_id, "symbol": self.tcfg.symbol,
                           "side": order_side, "type": "MARKET", "qty": plan.qty,
                           "price": fill.price if fill.ok else None,
                           "reduce_only": 0, "status": fill.status,
                           "exchange_id": fill.order_id, "reason": sig["reason"][:200]})
        if not fill.ok:
            # 失败必须退避，否则下一根 3s K 线会立刻再下一次单：
            # 一天 28,800 次下单，币安会按限频直接封 IP——
            # 那不是亏钱的问题，是彻底不能再交易，而且解除要等封禁期。
            # 注意：这不是理论风险。余额不足、最小名义不满足这类错误
            # 都是【持久性】的，策略信号成立期间每 3 秒都会重试。
            self._order_fail_count = int(getattr(self, "_order_fail_count", 0)) + 1
            delay_s = min(60 * (2 ** (self._order_fail_count - 1)), 1800)
            self._order_retry_ts = now_ms + delay_s * 1000
            self._log_decision(bars_by_iv, price, equity, now_ms, sig["name"], "skip",
                               "下单失败：%s（第 %d 次，%d 秒内不再重试）"
                               % (fill.reason or fill.status,
                                  self._order_fail_count, delay_s),
                               gates=plan.gates, features=feats)
            self.db.log_risk("order_failed",
                             "%s｜第 %d 次，退避 %d 秒"
                             % (fill.reason or fill.status,
                                self._order_fail_count, delay_s))
            logger.error("下单失败（第 %d 次）：%s，%d 秒内不再尝试",
                         self._order_fail_count, fill.reason or fill.status, delay_s)
            return
        # 下单成功：清零失败计数，让下一次失败从最短退避开始
        self._order_fail_count = 0
        self._order_retry_ts = 0

        # 记账必须用【实际成交量】而不是计划量：执行层可能因可用余额截断，
        # 交易所也可能部分成交。用计划量记账会让引擎以为仓位更大，
        # 平仓时会试图卖出超过实际持有的币（现货会直接被拒，永续会变成反向开仓）。
        got_qty = fill.qty if (fill.qty and fill.qty > 0) else plan.qty
        if abs(got_qty - plan.qty) > 1e-9:
            logger.warning("实际成交 %.8f 与计划 %.8f 不一致，按实际记账",
                           got_qty, plan.qty)
        self.db.log_fill({"ts": now_ms, "symbol": self.tcfg.symbol, "side": order_side,
                          "price": fill.price, "qty": got_qty, "fee": fill.fee,
                          "fee_asset": "USDT", "paper": 1 if self.ex.name == "paper" else 0})
        self.pos = {"side": side_txt, "qty": got_qty, "entry": fill.price,
                    "stop": plan.stop, "target": plan.target, "entry_ts": now_ms,
                    "bars": 0, "best": fill.price, "worst": fill.price,
                    "trade_id": None, "dust": pre_dust,
                    "max_hold_bars": plan.max_hold_bars or self.tcfg.max_hold_bars,
                    "open_reason": sig["reason"], "fee": fill.fee, "signal": sig["name"]}
        self.db.log_trade({"symbol": self.tcfg.symbol, "side": side_txt, "qty": got_qty,
                           "entry_ts": now_ms, "entry_price": fill.price,
                           "stop_price": plan.stop, "target_price": plan.target,
                           "max_hold_bars": plan.max_hold_bars or self.tcfg.max_hold_bars,
                           "open_reason": sig["reason"][:300], "fee": fill.fee,
                           "paper": 1 if self.ex.name == "paper" else 0,
                           "strategy": self.tcfg.strategy})
        # 启动对账到这里才算兑现。不在这里清掉的话，标志会一直挂着：
        # 这笔仓一旦靠熔断止损平掉而价格仍在均线上方，就会立刻无条件补回，
        # 绕过"必须等下一根 4h 才能重入"的规则。
        if getattr(self.trend, "need_reconcile", False):
            self.trend.mark_reconciled()
        self.risk.trades_today += 1
        self._log_decision(bars_by_iv, price, equity, now_ms, sig["name"], "open_" + side_txt,
                           "开仓 " + plan.reason, gates=plan.gates, features=feats,
                           risk_info={"qty": got_qty, "notional": plan.notional,
                                      "leverage": plan.leverage, "stop": plan.stop,
                                      "target": plan.target, "stop_pct": plan.stop_pct,
                                      "tp_pct": plan.tp_pct, "risk_usdt": plan.risk_usdt,
                                      "liq_distance_pct": plan.liq_distance_pct,
                                      "cost_pct": plan.cost_pct,
                                      "max_hold_bars": plan.max_hold_bars},
                           signal_reason=sig["reason"])
        logger.info("开仓 %s %.3f @ %.1f 止损 %.1f 目标 %.1f 风险 %.2fU",
                    side_txt, plan.qty, fill.price, plan.stop, plan.target, plan.risk_usdt)

    # ---------------- 管仓 ----------------
    # ==================== BUS-C：守卫观察期（只记录，不执行）====================
    def _guard_config_for_observe(self):
        """守卫参数（观察期与人工仓出场共用）。

        ★ 两个 0 是用户 2026-11-24 的明确裁决：
          「不要分批止盈」→ `max_partials = 0`
          「不要加仓」    → `max_adds = 0`

          所以 P3/P4 分支永远不会命中 —— 这不是配置失误，是裁决结果。
          `target_1_R` 因此失去意义（它只是 P3 的输入）。

        ★★ `risk_per_trade` 必须有值，否则**整条出场链会中断**。

          这不是理论风险：`GuardConfig.__post_init__` 在
          `risk_per_trade <= 0` 时抛 ValueError。而如果让它传播出去，
          `_step_adopted_exit` 就会在止损判断**之前**崩掉 ——
          于是"因为算不出 R，所以不检查止损"，
          人工仓变成完全无人保护的裸仓。这是最坏的一种安全。

          所以这里宁可给一个占位值也不抛：R 只影响
          `profit_R` 的**显示**和 P3（已按裁决关闭），
          P0 用的是缓冲倍数、P1 用的是止损价，两者都不需要 R。
          真实值拿不到时记 `_guard_r_known=False`，日志里能看出这是占位。
        """
        from .position_guard import GuardConfig

        risk_u = 0.0
        try:
            params = self._sizing_params()
            if params:
                from .sizing import SizingConfig, derive
                risk_u = float(derive(SizingConfig(
                    capital=float(params.get("sizing.capital", 0.0) or 0.0),
                    risk_pct=float(params.get("sizing.risk_pct", 0.0) or 1.0),
                    leverage=float(params.get("sizing.leverage", 0.0) or 0.0),
                )).risk_per_trade)
        except Exception:
            logger.warning("[守卫] 算 R 失败（只影响 R 的显示，不影响止损）",
                           exc_info=True)

        if risk_u <= 0:
            # 占位 1.0，不是"猜一个真实风险"：它只让 profit_R 显示为
            # 名义金额，而 P0/P1 完全不读它。
            self._guard_r_known = False
            risk_u = 1.0
            # ★ F6：这个标记必须**真的被看见**。以前只赋值、从不输出，
            #   文档却说"日志里能看出这是占位" —— 一行装成可观测的假字段。
            #   每进程只吼一次，避免每 3 秒刷一行。
            if not getattr(self, "_guard_r_warned", False):
                self._guard_r_warned = True
                logger.warning(
                    "[守卫] 算不出 risk_per_trade，用占位 1.0 —— "
                    "profit_R 会显示成名义金额，P0/P1 不读它，止损照常")
        else:
            self._guard_r_known = True

        return GuardConfig(risk_per_trade=risk_u,
                           max_adds=0,          # 用户裁决：不加仓
                           max_partials=0,      # 用户裁决：不分批止盈
                           safety_multiple=2.5,
                           allow_new_risk=True)

    def _resolve_liq(self, side: str, entry: float):
        """给某一笔仓选一个爆仓价，并**说明它从哪来**：`(liq_price, 来源)`。

        ★ F7（2026-11-24 全链路审查）：观察期与人工仓过去各用一套来源 ——
          观察期用配置推算（`_liq_distance_pct`），人工仓用交易所真实值。
          同一个账户的净仓只有一个真实爆仓价，两个数字算出来的
          「离爆仓还有几个止损宽度」不可比，观察期的分歧统计也就白做了。

          现在两边走同一个函数：**能用真实值就用真实值**，
          用不了才退回推算，并且把来源返回给调用方去记录。

        ★ 真实值必须落在入场价的**亏损侧**：
          多单爆仓价在下方、空单在上方。交易所偶尔返回 0 或异常值，
          方向不对一律视为不可用 —— 用错一侧会让 P0 得出
          「已经很危险」或「绝对安全」的相反结论。
        """
        if entry <= 0:
            return 0.0, "不可用"
        sign = 1 if norm_side(side) == "long" else -1
        cached = getattr(self, "_liq_cache", None)
        try:
            real = float(cached[1]) if cached else 0.0
        except (TypeError, IndexError, ValueError):
            real = 0.0
        if real > 0 and (sign * (entry - real)) > 0:
            return real, "交易所真实"
        pct = self._liq_distance_pct()
        if pct <= 0:
            return 0.0, "不可用"
        return entry * (1.0 - sign * pct / 100.0), "配置推算"

    def _guard_position_for_observe(self, price):
        """把系统仓翻译成守卫眼里的持仓。**纯读，不碰 `self.pos`。**

        ★ 为什么只翻译**系统仓**（`self.pos`）而不是人工仓：
          BUS-C 的范围是「系统仓出场」。人工仓按用户裁决只走 P0+P1
          （没有入场结构，不许套 P2），那是 BUS-D 的事。

        ★ 爆仓价来源：走 `_resolve_liq()`，与人工仓**同一个函数**。
          以前这里固定用配置推算，注释还写着"补真实爆仓价是 BUS-D 的范围"
          —— BUS-D 已经补完了，注释留在原地就是错的信息（F11）。

        ★★ F4：`stop <= 0` 直接返回 None，**不许把它翻译成"止损在 0 元"**。

          唐奇安策略合法地不设硬止损（见 `_fallback_stop`），所以它的
          `stop` 就是 0。而守卫看到 stop=0 会算成
          `d_stop = 入场价 - 0 = 入场价`，于是结构性缓冲
          = 爆仓距离 / 入场价 ≈ 0.04 倍 → 判「减仓 98%」。

          那不是守卫激进，是我们在用错的语言问它：它以为这家公司
          在 0 元设了止损。观察期若照此记录，报表上会天天出现
          「守卫要砍 98%」的假分歧，把真正的分歧淹掉。

          返回 None 之后由 `_guard_observe` 写明"该仓无止损，守卫不适用"。
        """
        from .position_guard import GuardPosition, ST_OPEN

        p = self.pos
        if not p:
            return None
        side = norm_side(p.get("side"))
        entry = float(p.get("entry") or 0.0)
        qty = abs(float(p.get("qty") or 0.0))
        stop = float(p.get("stop") or 0.0)
        if entry <= 0 or qty <= 0:
            return None
        if stop <= 0:
            return None

        liq, src = self._resolve_liq(side, entry)
        if liq <= 0:
            return None
        self._liq_src = src

        tgt = float(p.get("target") or 0.0)
        init_risk = abs(entry - stop) * qty
        return GuardPosition(
            symbol=str(self.tcfg.symbol),
            direction=side,
            avg_entry=entry,
            qty=qty,
            stop_price=stop,
            liq_price=liq,
            target_price=(tgt if tgt > 0 else None),
            initial_risk=init_risk,
            adds_done=0,
            partials_done=0,
            stop_delay_used=False,
            state=ST_OPEN,
        )

    def _guard_observe(self, bars_by_iv, price, now_ms) -> None:
        """观察期对照：每根 15m 收盘让守卫算一次，**只记录不执行**。

        ★★ 这个函数最重要的性质是**它什么都不做**。
          一个"观察者"如果顺手改一下 `self.pos`、或者调一次 `_close`，
          就立刻变成第二个下单引擎 —— 两只手同时管一笔仓，
          而其中一只的手还没被验证过。所以：

            · 不写 `self.pos`、不调用任何下单/平仓函数
            · 只调 `decide()`（纯函数：无 IO、无副作用）
            · 只写一行 `decisions(action="guard_compare")`

        ★ 为什么按 15m 节流而不是每根 3s：
          守卫生来就是给 15m 收盘设计的（`kline_intent.read_kline`
          的阈值 BIG_WICK/放量线都按这个尺度标定）。每 3s 跑一次等于
          用同一根 K 线重复问同一个问题，只会灌满决策表。
          15m 一根，一小时 4 条，一天 96 条 —— 可读。
        """
        from . import kline_intent as ki
        from .position_guard import HOLD, decide

        b15 = bars_by_iv.get(900000)
        if not b15:
            return
        ts15 = int(getattr(b15[-1], "open_time", 0) or 0) or \
            self._bar_ts(b15[-1])
        if ts15 <= 0 or ts15 <= int(getattr(self, "_guard_obs_ts", 0) or 0):
            return
        self._guard_obs_ts = ts15

        last = getattr(self, "_guard_obs_last", None)

        # ---- 方向二：上次观察到有仓、这次没了 → 引擎（或交易所）先平了仓。
        #
        # ★ 这条覆盖「守卫说 HOLD，引擎却平了仓」的分歧，也就是"策略比守卫更急"。
        #   不覆盖它，观察报告只会告诉我们"守卫想平而引擎没平"，
        #   另一半真相（引擎频繁进出、守卫认为该拿住）就永远看不见。
        if not self.pos:
            if last:
                _act = str(last.get("guard_action") or "")
                _agree = (_act != HOLD)
                self._log_decision(
                    bars_by_iv, price, 0.0, now_ms, "guard_compare",
                    "guard_compare",
                    "守卫观察：仓位已不在（上次观察 guard=%s）。%s"
                    % (_act, "双方都要出场，一致" if _agree else
                       "分歧：guard 认为该继续持有（HOLD），引擎/交易所先平了仓"),
                    gates=[], features=None, risk_info=None, force=True,
                    observe=True)
                self._guard_obs_last = None
            return

        gp = self._guard_position_for_observe(price)
        if gp is None:
            # ★ F4：绝大多数情况是「该仓没有硬止损」（唐奇安合法如此），
            #   少数是拿不到爆仓价。两种都**必须写一句话**再返回 ——
            #   静默 return 会让观察期看起来"一切正常"，
            #   而实际上这一根根本没有对照记录。
            _p = self.pos or {}
            _why = ("该仓没有硬止损（stop=%.2f），守卫的 P0/P1 不适用 —— "
                    "把它当成「止损在 0 元」会算出要减仓 98%%"
                    % float(_p.get("stop") or 0.0)) \
                if float(_p.get("stop") or 0.0) <= 0 else \
                "拿不到爆仓价，本根无法对照"
            self._log_decision(
                bars_by_iv, price, 0.0, now_ms, "guard_compare",
                "guard_compare", "守卫观察：跳过 —— " + _why,
                gates=[], features={"skipped": _why}, risk_info=None,
                force=True, observe=True)
            return

        # 15m 收盘序列 → read_kline 要求的 (ts,o,h,l,c,v) 元组
        #   ★ 时间/价量都用 `_bar_ts` / `_bar_ohlcv`：
        #     真实 `feed.Bar` 是 open/high/low/close，没有 `.ts`。
        seq = [((self._bar_ts(b),) + self._bar_ohlcv(b)) for b in b15[-60:]]
        kline = ki.read_kline(seq) if seq else None

        cfg = self._guard_config_for_observe()
        d = decide(gp, float(price), cfg, kline=kline)
        self._guard_obs_last = {"guard_action": d.action, "ts": ts15,
                                "price": float(price)}

        # 引擎此刻的处境（只用于对照，不改任何东西）
        _eng_stop = float(self.pos.get("stop") or 0.0)
        _eng_tgt = float(self.pos.get("target") or 0.0)
        _sign = 1 if gp.direction == "long" else -1
        _profit_R = gp.profit_R(float(price))
        _stop_would_hit = gp.remaining_to_stop(float(price)) <= 0

        # 分歧定义：守卫要动手（非 HOLD）而引擎此刻仍在持有。
        # 引擎自己会在同一根 bar 里止损/止盈，所以"引擎也会出场"这种一致情形
        # 在下一次观察（仓位消失）时由上面那段补判。
        _diverged = (d.action != HOLD)
        self._log_decision(
            bars_by_iv, price, 0.0, now_ms, "guard_compare",
            "guard_compare",
            "守卫观察：guard=%s（%s）｜引擎持仓 %s %.6f @ %.1f 止损 %.1f 目标 %.1f"
            "｜profit %.2fR 爆仓缓冲 %.2f 倍%s"
            % (d.action, d.reason, gp.direction, gp.qty, gp.avg_entry,
               _eng_stop, _eng_tgt, _profit_R,
               gp.liq_buffer(float(price)),
               "" if _diverged else "｜双方一致（都持有）"),
            gates=[{"gate": "守卫P判断", "ok": not _diverged,
                    "detail": "%s：%s" % (d.action, d.reason)}],
            features={"guard": d.as_dict(), "kline": (kline.as_dict() if kline else None),
                      "engine_stop": _eng_stop, "engine_target": _eng_tgt,
                      "stop_would_hit": _stop_would_hit,
                      # ★ F7：记下这个爆仓价是交易所真实值还是配置推算
                      "liq_src": getattr(self, "_liq_src", "未知")},
            risk_info=None, force=True, observe=True)

    async def _live_liq_price(self) -> float:
        """交易所返回的**真实爆仓价**。取不到返回 0。

        ★ 为什么要缓存：`_step` 每 3 秒跑一次，而 `position()` 是一次真网络
          请求。每 3 秒问一次会把限频吃掉，而爆仓价是个慢变量（它随
          维持保证金和浮盈动，不随秒级价格动）。缓存 5 分钟，
          与周期对账同频。

        ★ 为什么必须在失败时**明确返回 0** 而不是退回推算值：
          调用方要靠 0 来区分"我拿到了真实值"和"我没拿到"。
          悄悄用推算值顶替，就等于让调用方以为自己在用真实爆仓价 ——
          这正是这个项目最反复的缺陷类型（看起来对，其实是另一个来源）。

        ★ 失败也要缓存（60 秒）：BUS-D 之后人工仓的出场判断跑在 3 秒循环里
          （F2 修的），一次取不到就每 3 秒重试一次，等于用限频去撞一个
          正在出故障的接口。成功缓存 5 分钟、失败缓存 1 分钟 ——
          恢复得快，又不打爆接口。
        """
        now = int(time.time() * 1000)
        cached = getattr(self, "_liq_cache", None)
        if cached and now - cached[0] < cached[2]:
            return cached[1]
        fn = getattr(self.ex, "position", None)
        if fn is None:
            return 0.0
        try:
            d = await fn()
        except Exception as e:
            logger.warning("[守卫] 取真实爆仓价失败（本轮用推算值）：%s", e)
            self._liq_cache = (now, 0.0, 60000)
            return 0.0
        liq = float((d or {}).get("liq_price") or 0.0)
        self._liq_cache = (now, liq, 300000)
        return liq

    async def _guard_position_for_adopted(self, pos, price):
        """把**人工仓**翻译成守卫眼里的持仓（BUS-D：用真实爆仓价）。

        与 `_guard_position_for_observe` 的差别只有一处、但很关键：
        这里优先用交易所返回的 `liquidationPrice`，取不到才退回推算值，
        并且**把用的是哪一种记进日志**（`self._liq_src`），
        因为"守卫以为离爆仓还有 2.5 倍"这件事，取决于这个数字从哪来。

        真实值为什么重要：推算公式 `(1/杠杆 - 维持保证金)` 假设逐仓、
        且忽略维持保证金率档位与浮盈。11-24 实测同一笔仓：
        推算 4.0455%、逐仓真实 4.15%、全仓真实 5.48%。
        差异方向不固定，所以不能用"保守起见"一句话带过。

        ★ F7：解析逻辑已收进 `_resolve_liq()`，与观察期**同一个函数**。
          这里只负责"先问一次交易所"（结果进缓存），然后交给它选。
          以前两处各写一套，同一账户的净仓能算出两个不同的缓冲倍数。
        """
        from .position_guard import GuardPosition, ST_OPEN

        if not pos:
            return None
        side = norm_side(pos.get("side"))
        entry = float(pos.get("entry") or 0.0)
        qty = abs(float(pos.get("qty") or 0.0))
        stop = float(pos.get("stop") or 0.0)
        if entry <= 0 or qty <= 0:
            return None

        # 先刷新缓存（内部 5 分钟节流 / 失败 1 分钟节流），再统一解析
        await self._live_liq_price()
        liq, src = self._resolve_liq(side, entry)
        if liq <= 0:
            return None
        self._liq_src = src

        init_risk = abs(entry - stop) * qty
        return GuardPosition(
            symbol=str(self.tcfg.symbol), direction=side, avg_entry=entry,
            qty=qty, stop_price=stop, liq_price=liq,
            # 人工仓不设止盈：没有 R 依据，硬编目标位等于凭空造规则
            target_price=None, initial_risk=init_risk,
            adds_done=0, partials_done=0, stop_delay_used=False, state=ST_OPEN)

    def _persist_adopted_stop(self, pos) -> None:
        """止损变动**不落库**，这里只把这件事说清楚。

        为什么不落库：这个仓库根本没有"持仓行"可写 —— `self.pos` /
        `self.adopted_pos` 都是内存结构，重启时由 `_pos_from_row()` 从
        `trades` 表重建。`_manage()` 抬高止损时同样是仅内存的。
        给它单独造一张持仓表等于引入第二个真相源。

        后果（可接受，方向也是安全的）：重启后止损退回接管时算出的那个值。
        棘轮抬高的是"保护利润"，退回原值意味着仓位不会被过早保护掉，
        而不是把仓位暴露到爆仓 —— 爆仓侧由 P0 用真实爆仓价每次重算。

        故意不写成 `self.db.update_pos(...)`：那个方法不存在，
        写了就是一行永远走不到的死代码，而"看起来保存了"比"没保存"更危险。
        """
        _ = pos   # 仅为签名一致；真实语义见 docstring

    # ---------- A1：人工仓在交易所侧的保护性止损单 ----------
    #
    # ★ 这张单与进程内的守卫**是两套独立保护**，不是同一套逻辑的两个出口：
    #     · 进程内（P0/P1）：3 秒轮询，判的是"该不该走"，用守卫的全部依据；
    #     · 交易所侧（这里）：挂在交易所的条件单，进程死了也照触发。
    #   两者并存是**故意的**：一张 reduceOnly 单无论如何都不会开出反向仓，
    #   所以宁可两边都动、也不许"以为有人管其实没人管"。

    def _adopted_stop_cid(self, pos) -> str:
        """人工仓止损单的 clientOrderId：`AI-SL-<entry_ts>`。

        稳定（同一笔仓永远同一个 id）→ 重启后能认回同一张单，
        不会每重启一次就在交易所多挂一张。
        币安要求 clientOrderId < 36 字符，这个形状恒为 13~19 字符。
        """
        try:
            ts = int(float(pos.get("entry_ts") or 0))
        except (TypeError, ValueError):
            ts = 0
        return "%s%d" % (ADOPTED_STOP_CID_PREFIX, ts)

    def _adopted_stop_state(self) -> dict:
        st = getattr(self, "_adopted_stop", None)
        return st if isinstance(st, dict) else {}

    async def _cancel_adopted_stop_order(self, why: str = "") -> None:
        """撤掉我们挂的那张保护性止损单。失败只记事件，不抛。

        为什么允许失败：撤不掉的最坏后果是"多一张触发不了的挂单"
        （仓已经平了，reduceOnly 单触发也开不出反向仓），
        比"因为撤单失败把主循环崩掉"轻得多。
        """
        oid = str(self._adopted_stop_state().get("oid") or "")
        if not oid:
            self._adopted_stop = {}
            return
        fn = getattr(self.ex, "cancel_order", None)
        ok = False
        err = ""
        if fn is None:
            err = "执行器不支持撤单"
        else:
            try:
                ok = bool(await fn(order_id=oid))
            except Exception as e:
                err = str(e)[:180]
        if ok:
            logger.info("[接管] 已撤掉交易所侧止损单 %s（%s）", oid, why or "-")
            try:
                self.db.log_risk("adopted_stop_cancelled",
                                 "%s（%s）" % (oid, why[:100]))
            except Exception:
                pass
        else:
            logger.error("[接管] 撤止损单失败 %s（%s）：%s", oid, why, err)
            try:
                self.db.log_risk("adopted_stop_cancel_failed",
                                 "%s：%s" % (oid, err))
            except Exception:
                pass
        # 无论成败都清掉本地记录：下一轮 `_sync` 会按现状重新收敛
        # （成功 → 不再挂；失败 → 重挂一张新的，而不是永远卡在旧 id 上）。
        self._adopted_stop = {}

    async def _sync_adopted_stop_order(self, pos, price: float) -> None:
        """让交易所侧的保护性止损单与内存里的止损/手数保持一致。

        ★ A1（2026-11-25 交易域审查）。这是"进程死了仓位还在裸奔"的修法。

        收敛规则（幂等，每 3 秒跑一次，只在真的变了才发网络请求）：

          · 没有人工仓            → 撤单
          · stop <= 0 / qty <= 0  → 撤单（没有可保护的目标）
          · stop 已在现价内侧      → 不挂（进程内这一 tick 就会平，
                                     挂上去等于立刻触发，多一层滑点）
          · 张数/止损价与本地记录一致 → 什么都不做
          · 否则                  → 撤旧挂新

        ★ 只对实盘生效。纸面执行器的挂单要靠 `on_bar_5m` 才会触发，
          而人工仓这条链根本不走 `on_bar_5m` —— 在纸面挂上去只会造成
          "看起来有交易所保护、其实没有"的错觉。这比不挂更危险。
        """
        if str(getattr(self.ex, "name", "") or "") != "live":
            return

        if not pos:
            await self._cancel_adopted_stop_order("已无人工仓")
            return

        stop = float(pos.get("stop") or 0.0)
        qty = abs(float(pos.get("qty") or 0.0))
        entry = float(pos.get("entry") or 0.0)
        if stop <= 0 or qty <= 0:
            await self._cancel_adopted_stop_order(
                "停止位或手数无效（stop=%.4f qty=%.6f）" % (stop, qty))
            return

        step = float(self._qty_step() or 0.0)
        if step > 0:
            qty = round(float(int(qty / step + 1e-9)) * step, 12)
        if qty <= 0 or (step > 0 and qty < step - 1e-12):
            await self._cancel_adopted_stop_order("手数取整后不足一个步长")
            return

        is_long = norm_side(pos.get("side")) == "long"
        close_side = "SELL" if is_long else "BUY"
        # 已经触发过了就别挂：进程内这一 tick 就该平掉它。
        if price > 0:
            if (is_long and price <= stop) or ((not is_long) and price >= stop):
                return

        cid = self._adopted_stop_cid(pos)
        state = self._adopted_stop_state()
        if (state.get("oid") and abs(float(state.get("px") or 0.0) - stop) < 1e-9
                and abs(float(state.get("qty") or 0.0) - qty) < 1e-12):
            return                        # 已对齐，不发请求

        place_fn = getattr(self.ex, "place_order", None)
        if place_fn is None:
            return

        # 旧的还在 → 先撤掉再挂新的（不做"改单"，改单失败会留下两张单）
        if state.get("oid"):
            await self._cancel_adopted_stop_order(
                "止损/手数变化 %.4f→%.4f / %.6f→%.6f"
                % (float(state.get("px") or 0.0), stop,
                   float(state.get("qty") or 0.0), qty))

        # ★ 重启幂等：内存里没有 order id，但交易所可能已经有同一张单。
        #   先按 clientOrderId 查一次，查到就直接接管，避免挂出第二张。
        q_fn = getattr(self.ex, "query_order", None)
        if q_fn is not None:
            try:
                ex_order = await q_fn(client_id=cid)
            except Exception:
                ex_order = None
            if ex_order is not None and str(getattr(ex_order, "status", "")
                                            ).upper() in ("NEW", "PARTIALLY_FILLED"):
                self._adopted_stop = {
                    "oid": str(getattr(ex_order, "order_id", "") or ""),
                    "px": float(getattr(ex_order, "stop_price", 0.0) or 0.0),
                    "qty": float(getattr(ex_order, "qty", 0.0) or 0.0),
                }
                st2 = self._adopted_stop_state()
                if (st2.get("oid")
                        and abs(float(st2.get("px") or 0.0) - stop) < 1e-9
                        and abs(float(st2.get("qty") or 0.0) - qty) < 1e-12):
                    logger.info("[接管] 接管已有的交易所侧止损单 %s（%.2f）",
                                st2["oid"], stop)
                    return
                if st2.get("oid"):
                    await self._cancel_adopted_stop_order("重启后参数已变")

        try:
            order, err = await place_fn(
                close_side, qty, order_type="STOP_MARKET", stop_price=stop,
                reduce_only=True, client_id=cid,
                # 与触发价同源（A2）：币安按最新成交价触发，不是标记价。
                working_type="CONTRACT_PRICE")
        except Exception as e:
            order, err = None, str(e)[:200]

        oid = str(getattr(order, "order_id", "") or "") if order else ""
        if err or not oid:
            logger.error("[接管] 挂交易所侧止损单失败（%.2f / %.6f）：%s",
                         stop, qty, err or "无返回 id")
            try:
                self.db.log_risk("adopted_stop_order_failed",
                                 "%.2f / %.6f：%s" % (stop, qty, (err or "")[:180]))
            except Exception:
                pass
            self._adopted_stop = {}       # 下一轮重试
            return

        self._adopted_stop = {"oid": oid, "px": stop, "qty": qty}
        logger.info("[接管] 交易所侧止损单已挂 %s %s %.6f 触发 %.2f（进程外保护）",
                    oid, close_side, qty, stop)
        try:
            self.db.log_risk("adopted_stop_ordered",
                             "%s %s %.6f @ 触发 %.2f（%s）"
                             % (oid, close_side, qty, stop, cid))
        except Exception:
            pass

    async def _manage(self, price, bars_by_iv) -> bool:
        p = self.pos
        bar = bars_by_iv[3000][-1] if bars_by_iv.get(3000) else None
        if not bar:
            return False
        p["bars"] += 1
        direction = 1 if p["side"] == "long" else -1
        hi, lo = bar.high, bar.low
        p["best"] = max(p["best"], hi) if direction > 0 else min(p["best"], lo)

        stop, target = p["stop"], p["target"]
        hit_stop = lo <= stop if direction > 0 else hi >= stop
        hit_tp = hi >= target if direction > 0 else lo <= target

        # 保本/移动止损
        r_dist = abs(p["entry"] - p["stop"])
        profit = (p["best"] - p["entry"]) * direction
        if r_dist > 0:
            if profit >= self.tcfg.trail_after_r * r_dist:
                trail = p["best"] - direction * 0.5 * r_dist
                p["stop"] = max(stop, trail) if direction > 0 else min(stop, trail)
            elif profit >= self.tcfg.breakeven_r * r_dist:
                p["stop"] = max(stop, p["entry"]) if direction > 0 else min(stop, p["entry"])

        # 先判止损（保守），再判目标
        if hit_stop:
            return await self._close("止损 %.1f" % p["stop"], p["stop"], price, bars_by_iv)
        if hit_tp:
            return await self._close("止盈 %.1f" % p["target"], p["target"], price, bars_by_iv)
        hold_limit = p.get("max_hold_bars") or self.tcfg.max_hold_bars
        if p["bars"] >= hold_limit:
            return await self._close("超时 %d 根 3s 未达目标（上限 %d，按波动反推）"
                                     % (p["bars"], hold_limit), price, price, bars_by_iv)
        return False

    def _sync_close_fail_streak(self) -> None:
        """把平仓失败次数同步给 Guard（第 116 轮）。

        方向和主流相反：这条闸的【触发在引擎、判定在 Guard】。
        因为「该不该继续开新仓」是风控判断，而风控判断的唯一出口是 Guard——
        留在引擎里，看板只是个数字，没有任何东西会因为它停下来。
        """
        if self.guard is not None:
            self.guard.close_fail_streak_changed(
                int(getattr(self, "_close_fail_count", 0) or 0))

    async def _close(self, reason: str, exit_px: float, price: float, bars_by_iv) -> bool:
        p = self.pos
        # 退避闸放在 _close 入口，所有平仓路径（止损/止盈/超时/趋势出场/熔断）都受它约束。
        # 上限 300s：止损不能停，但也不需要 3 秒一次地把自己打到封禁。
        now_gate = int(time.time() * 1000)
        if now_gate < getattr(self, "_close_retry_ts", 0):
            return False
        direction = 1 if p["side"] == "long" else -1
        order_side = "sell" if p["side"] == "long" else "buy"
        # 用市价平（纸面=当前价，滑点在执行器里扣）；exit_px 只用于归因。
        #
        # 把开仓时接管的零头一起卖掉：那部分是上几笔 step 取整留下的残留，
        # 不卖的话会一直堆在账上（每笔约 0.68U），既不参与策略、跌的时候又跟着跌。
        # 执行器会再按 step 向下取整，所以这里多给一点是安全的，不会超卖。
        # p.get("dust") 在开仓时已保证 0 < dust < step，用户自己的币不会被带进来。
        sell_qty = p["qty"] + float(p.get("dust") or 0.0)
        # ★ 平仓【抛异常】必须和【返回失败】一样计入 (第 124 轮)。
        #
        # market_order() 有两条失败路径：
        #   1. 返回 FillResult(ok=False)  -> 下面 if not fill.ok 会计数
        #   2. 抛异常                      -> 原本直接冒泡出去，计数【一次都不加】
        #
        # 第 2 条怎么发生的：market_order 的 try 只包住了下单那【一句】，
        # 而它前面的 spec() 和 equity() 都在 try 外面，两者内部走 _req，
        # 而 _req 把所有网络失败/HTTP 非 200 统一转成 SpotError 抛出。
        # 于是异常会冒泡出 market_order -> _close(不捕获) -> _step_trend(不捕获)
        # -> _on_bar 捕获 -> 只记一条 engine_error。
        #
        # 后果：_close_fail_count 永远是 0，第 116 轮那条
        # 「平仓连续失败 5 次 -> 停止开新仓」的闸【永远到不了 5】。
        #
        # 而 equity() 是每次平仓【必经】的一次 HTTP 请求（要按账上持币量
        # 截断卖出量）。交易所 API 最容易超时的时刻正是行情剧烈波动的时候——
        # 也就是最需要止损的时候。所以这条路径不是边角，是主路径。
        #
        # 真实事故形态：行情暴动 -> equity() 超时 -> 平仓抛异常 ->
        # 计数器不动 -> 闸不触发 -> 系统一边报 engine_error 一边继续开新仓。
        try:
            fill = await self.ex.market_order(
                order_side, sell_qty, reason="平仓 " + reason,
                reduce_only=True,
                client_id=_close_client_id(
                    "close", "%s|%d" % (reason, int(time.time() * 1000))))
        except Exception as e:
            # 兜住它而不是让它冒泡：冒泡的后果是"整个决策步骤中断"，
            # 而止损是急事——这一轮卖不出去，下一轮必须接着试。
            # 所以这里的行为要和 fill.ok=False 完全一致：计数、退避、再试。
            now_ms = int(time.time() * 1000)
            self._close_fail_count = int(getattr(self, "_close_fail_count", 0)) + 1
            delay_s = min(3 * (2 ** (self._close_fail_count - 1)), 300)
            self._close_retry_ts = now_ms + delay_s * 1000
            self.db.log_risk(
                "close_failed",
                "%s｜连续第 %d 次（异常路径），%.0f 秒后再试"
                % (str(e)[:180], self._close_fail_count, delay_s))
            logger.error("平仓抛异常（连续第 %d 次）：%s，%.0f 秒后再试",
                         self._close_fail_count, e, delay_s)
            self._sync_close_fail_streak()
            return False
        now_ms = int(time.time() * 1000)
        self.db.log_order({"ts": now_ms, "client_id": "AI-CLOSE-%d" % now_ms,
                           "symbol": self.tcfg.symbol, "side": order_side, "type": "MARKET",
                           "qty": p["qty"], "price": fill.price, "reduce_only": 1,
                           "status": fill.status, "exchange_id": fill.order_id,
                           "reason": reason[:200]})
        if not fill.ok:
            # 指数退避：3s -> 6s -> 12s ... 上限 300s。
            # 和开仓的退避（60s 起、上限 1800s）不同，止损是急事，
            # 起步必须快，否则一根针就能把浮亏放大一大截。
            self._close_fail_count = int(getattr(self, "_close_fail_count", 0)) + 1
            delay_s = min(3 * (2 ** (self._close_fail_count - 1)), 300)
            self._close_retry_ts = now_ms + delay_s * 1000
            self.db.log_risk("close_failed",
                             "%s｜连续第 %d 次，%.0f 秒后再试"
                             % (fill.reason, self._close_fail_count, delay_s))
            logger.error("平仓失败（连续第 %d 次）：%s，%.0f 秒后再试",
                         self._close_fail_count, fill.reason, delay_s)
            # ★ 止损失效必须自己成为一条闸（第 116 轮）。
            #   只写 risk_events 是不够的：实测看板 /api/trade/state、
            #   哨兵、健康自检、Guard 四个监控面【一个都不读】close_failed。
            #   钱在漏，而所有仪表都指着"正常"。
            self._sync_close_fail_streak()
            return False
        # 平仓成功：清零失败计数
        self._close_fail_count = 0
        self._close_retry_ts = 0
        self._sync_close_fail_streak()

        # 用【实际卖出量】算盈亏，不用计划量。
        # 现货卖出会被 step 取整（向下），实际成交可能比 p["qty"] 少一点点；
        # 拿计划量算，盈亏会被系统性高估。
        # 这和开仓时"记账必须用实际成交量"是同一个道理，只是方向相反。
        sold_qty = fill.qty if (fill.qty and fill.qty > 0) else p["qty"]
        if abs(sold_qty - p["qty"]) > 1e-9:
            logger.info("平仓实际成交 %.8f 与持仓 %.8f 不一致（step 取整残留 %.8f）",
                        sold_qty, p["qty"], p["qty"] - sold_qty)
        # ★ 统一口径：毛/费/净走 `close_booking()`（和接管仓、挂单仓同一个公式）。
        #   这一份原本就是正确的（`p.fee + fill.fee`），保留为基准；
        #   提成纯函数的目的是让另外两条路能一起对齐，而不是三份实现各自演化。
        gross, fee_total, net = close_booking(
            p, fill.price, fill.fee, sold_qty, p["side"])
        self.db.log_fill({"ts": now_ms, "symbol": self.tcfg.symbol, "side": order_side,
                          "price": fill.price, "qty": sold_qty, "fee": fill.fee,
                          "fee_asset": "USDT", "paper": 1 if self.ex.name == "paper" else 0})
        self.db.close_trade({
            "symbol": self.tcfg.symbol, "side": p["side"], "qty": sold_qty,
            "entry_ts": p["entry_ts"], "entry_price": p["entry"],
            "exit_ts": now_ms, "exit_price": fill.price,
            "stop_price": p["stop"], "target_price": p["target"],
            "pnl_gross": round(gross, 6), "fee": round(fee_total, 6), "pnl_net": round(net, 6),
            # MFE/MAE 用持仓期间的真实最高/最低价。
            # 旧实现里 mae = (exit - entry) * -direction，那是【出场移动】，
            # 不是最大不利偏移——两者在价格中途反复时完全不同。
            # 这个字段没人读过，所以一直没暴露；但将来要用它标定止损档位，
            # 假数据会直接把结论带偏。mae 按行业惯例为负（不利方向）。
            "mfe": round((p["best"] - p["entry"]) * direction, 6),
            "mae": round((p.get("worst", p["entry"]) - p["entry"]) * direction, 6),
            "open_reason": p.get("open_reason", "")[:300], "close_reason": reason[:200],
            "paper": 1 if self.ex.name == "paper" else 0,
            "strategy": self.tcfg.strategy})

        self.risk.day_pnl += net
        if net < 0:
            self.risk.consecutive_losses += 1
            if self.risk.consecutive_losses >= self.tcfg.max_consecutive_losses:
                self.risk.cooldown_until = now_ms + self.tcfg.cooldown_min * 60000
                self.risk.consecutive_losses = 0
                self.db.log_risk("cooldown",
                                 "连亏触发冷却 %d 分钟" % self.tcfg.cooldown_min)
        else:
            self.risk.consecutive_losses = 0

        logger.info("平仓 %s %s 净 %.4fU (毛 %.4f 费 %.4f)",
                    p["side"], reason, net, gross, fee_total)
        # 清持仓【不能】排在查权益后面。
        #
        # 卖单已经成交、成交也已落库 —— 这一笔在事实上结束了，
        # 而 equity() 是 HTTP 调用（现货是 /api/v3/account），它会超时，
        # 而且最容易超时的时刻正是行情剧烈波动的时候。
        # 一旦它抛异常而 self.pos 还挂在后面：
        #   引擎此后认为自己仍持仓 -> 不会在新金叉时进场
        #   （按本文件 412 行的实测，漏掉最好那笔进场 = 总收益少 46%），
        #   还会每 3~300 秒反复去卖一个已经为 0 的余额。
        # 而且 _on_bar 会吞掉这个异常继续跑，所以症状是"安静地卡住"。
        self.pos = None
        try:
            eq = await self.ex.equity()
            eq_now = float(eq["equity"])
        except Exception as e:
            # 拿不到真实权益时用上一次已知值记账，并留下痕迹。
            # 这只是展示字段；下一跳 _step 会把真实权益写进 equity 表。
            eq_now = float(getattr(self, "_last_equity", 0.0) or 0.0)
            logger.warning("平仓后查权益失败，用上一次已知值 %.2f 记账：%s",
                           eq_now, e)
            self.db.log_risk("close_equity_failed", str(e)[:200])
        self._log_decision(bars_by_iv, price, eq_now, now_ms,
                           p.get("signal"), "close",
                           "平仓 %s：净 %.4fU" % (reason, net),
                           gates=[], induce_close=reason)
        # 平完立刻刷新真账，而不是等下一轮对账（最多 5 分钟）。
        # 用户刚看到「平仓」就想看到这一单的分数，等 5 分钟等于没做。
        #
        # ★ 必须是"丢出去"而不是 await：_close() 是在 self._lock 里跑的
        # （_step 的 async with self._lock），在这里等一次 K 线拉取，
        # 就等于把每一次报价、每一个止损判断都按住几秒钟。
        # 止损被延后的代价远大于"报表晚 3 秒"。
        # _ledger_busy 保证同一时刻只有一次同步在跑。
        try:
            asyncio.get_event_loop().create_task(self._sync_live_ledger())
        except Exception:
            logger.error("平仓后调度真账同步失败（不影响交易）", exc_info=True)
        return True

    # ---------------- 记录 ----------------
    def _log_decision(self, bars_by_iv, price, equity, now_ms, signal, action, reason,
                      gates=None, features=None, risk_info=None, signal_reason="",
                      induce_close="", force=False, observe=False) -> None:
        """写一条决策。`observe=True` 表示这是**观察记录**而不是决策本体。

        ★ 为什么要区分（2026-11-24 全链路审查发现）：
          `guard_compare` 以前用 `force=True` 写行，而这条路径会顺带把
          `_last_bar_time` 推进到当前 3s 桶。于是同一 tick 里策略自己那条
          `force=False` 的决策被去重规则挡掉 —— 每 15 分钟丢一条真实决策，
          而顶替它的是一行"观察对照"。时间线看起来更热闹，信息反而更少。

          观察记录必须满足两件事，缺一就会污染它想测量的东西：
            · 不推进去重游标（否则挤掉被测对象的记录）
            · 不计入 `self.decisions`（否则看板上的"决策数"把观察也算进去，
              策略真的卡死时这个数字仍然在涨 —— 一条会撒谎的健康指标）
        """
        bar = bars_by_iv.get(3000)
        bar_time = bar[-1].open_time if bar else now_ms
        if not force and bar_time <= self._last_bar_time:      # 同一根桶只留一条决策
            return
        if not observe and bar_time > self._last_bar_time:
            self._last_bar_time = bar_time
        if not observe:
            self.decisions += 1
        d = {"ts": now_ms, "bar_time": bar_time,
             "feature_time": (features or {}).get("feature_time", bar_time),
             "symbol": self.tcfg.symbol, "price": price, "signal": signal,
             "action": action, "reason": (signal_reason + " | " if signal_reason else "") + reason,
             "gates": gates, "features": features, "risk": risk_info,
             "equity": round(equity, 4)}
        self.last_decision = d
        self.db.log_decision(d)
        if action.startswith("open") or action in ("close", "reduce"):
            self.feed.hub.publish({"type": "decision", **{
                "ts": now_ms, "action": action, "signal": signal, "reason": d["reason"],
                "price": price, "equity": round(equity, 4),
                "risk": risk_info}})

    def _pos_risk(self, price, equity) -> dict:
        p = self.pos
        if not p:
            return {}
        direction = 1 if p["side"] == "long" else -1
        notional = p["qty"] * p["entry"]
        return {"side": p["side"], "qty": p["qty"], "entry": p["entry"],
                "stop": p["stop"], "target": p["target"],
                "notional": round(notional, 2),
                "leverage": round(notional / equity, 2) if equity else 0.0,
                "stop_pct": round(abs(p["entry"] - p["stop"]) / p["entry"] * 100.0, 3),
                "unrealized": round((price - p["entry"]) * p["qty"] * direction, 4),
                "bars": p["bars"]}

    def trend_state(self) -> dict:
        """把趋势策略的决策因果摊开给看板用。

        这是"我要知道你在做什么"的答案：不是给一个结论，
        而是把【数据 -> 判断 -> 动作 -> 触发条件】整条链都摆出来。
        """
        if not self._is_chan():
            return {"mode": self.tcfg.strategy, "ready": False}
        snap = getattr(self, "_trend_snapshot", None)
        if not snap:
            return {"mode": self.tcfg.strategy, "ready": False}
        return snap

    def guard_state(self) -> dict:
        """安全闸状态，供大屏展示。

        为什么必须上大屏：大屏的趋势链会写
            「动作：空仓等待 / 开多触发线：涨到 X（还需 +Y%）并收盘确认」
        这行字暗示「价格到了就会开」。但六道闸里任何一道拦住，
        实际都不会开——数据源 DOWN、峰值回撤熔断、连亏冷却、当日亏损超限。

        用户的原话是「我要知道你在做什么」。
        一个漏掉「为什么不能开」的因果链不是因果，只是愿望。

        没有 guard 时 can_open 返回 None（未知），绝不默认 True：
        把「不知道」显示成「可以开」是最糟的一种谎。
        """
        if self.guard is None:
            return {"can_open": None, "block_reason": "",
                    "note": "本次运行没有接入安全闸"}
        ok, why = self.guard.can_open()
        snap = self.guard.snapshot()
        return {"can_open": ok, "block_reason": why or "",
                "halted_hard": snap.get("halted_hard"),
                "halt_reason": snap.get("halt_reason") or "",
                "temp_block": snap.get("temp_block") or "",
                "drawdown_pct": snap.get("drawdown_pct"),
                "peak_equity": snap.get("peak_equity"),
                "equity": snap.get("equity"),
                "day_start_equity": snap.get("day_start_equity"),
                "rules": snap.get("rules") or {}}

    def executor_state(self) -> dict:
        """执行器是否已熔断，供大屏展示。

        为什么必须上大屏：SpotExecutor 熔断后所有下单都返回「已熔断」，
        系统就此停止交易。但它的表现是「一直空仓」——
        和大屏上「空仓等待 / 涨到 X 就开」完全一样。
        用户会以为在等信号，其实永远等不到。

        哨兵确实会报（实测 trend_blocked_2h>=1），但那是 2 小时一次的后台提醒；
        人盯着大屏时看到的仍然是"一切正常"。
        和上一轮把 guard 画进因果链是同一个道理：
        状态对了不等于因果说清楚了。
        """
        halted = getattr(self.ex, "halted", "")
        return {"name": getattr(self.ex, "name", ""),
                "halted": bool(halted),
                "reason": str(halted)[:300] if halted else "",
                # 平仓连续失败次数（第 116 轮）。必须上大屏：
                # 止损失效时看板原本显示「一切正常」，因为 halted 只在
                # 余额核对失败时才置位，而平仓失败【不碰 halted】。
                "close_fail_count": int(getattr(self, "_close_fail_count", 0) or 0)}

    def state(self) -> dict:
        # ★ `pos` 只放系统仓（拆仓后的语义），人工仓单独给 `adopted_pos`。
        #   两个都必须吐出去：只给 `pos` 的话，用户明明手上有仓，
        #   看板却显示"空仓" —— 那正是 2026-11-24 用户困惑的来源
        #   （"系统帮我在 61250 出场了，我没明白为什么"）。
        #   `positions` 是给"我到底有多少仓"这个问题用的合计视图。
        return {"trend": self.trend_state(),
                "equity": round(self._last_equity, 4),
                "pos": self.pos,
                "adopted_pos": getattr(self, "adopted_pos", None),
                "positions": [
                    dict(p, slot=s) for s, p in
                    (("system", self.pos),
                     ("adopted", getattr(self, "adopted_pos", None))) if p],
                "block_stats": self.block_stats,
                "guard": self.guard_state(),
                "executor": self.executor_state(),
                "risk": {"day_pnl": round(self.risk.day_pnl, 4),
                                          "consecutive_losses": self.risk.consecutive_losses,
                                          "cooldown_until": self.risk.cooldown_until,
                                          "trades_today": self.risk.trades_today},
                "decisions": self.decisions,
                "last_error": self.last_error,
                "last_error_ts": self.last_error_ts,
                "mode": self.ex.name}
