"""决策引擎：每根 3s 桶收盘跑一次，把「看行情 -> 判断 -> 开/管/平」全过程留痕。

三条铁律：
1. 决策只用已收盘 bar（feature_time 显式写入，且断言 <= 决策时间），杜绝前视。
2. 每根 bar 都留一行决策记录，包括"什么都没做"及其原因。
3. 仓位只由风险预算决定；杠杆、强平距离是校验项，不是目标。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from ..trade_db import TradeDB
from . import features as feat_mod
from . import signals as sig_mod
from .risk import RiskState, build_plan, check_risk_state, roll_day

logger = logging.getLogger(__name__)


class DecisionEngine:
    def __init__(self, cfg, feed, executor, tdb: TradeDB, guard=None,
                 settings=None):
        self.cfg = cfg
        # 4 个仓位参数（本金/风险%/杠杆/保证金上限）的唯一来源是 Web 配置库。
        # 不传就当没有：宁可按"参数缺失"拒绝开仓，也不按硬编码默认值下单。
        self.settings = settings
        self.tcfg = cfg.trading
        self.feed = feed
        self.ex = executor
        self.db = tdb
        self.guard = guard
        self.risk = RiskState()
        self.pos: Optional[dict] = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self.decisions = 0
        self.block_stats = {}
        self._last_bar_time = 0
        self.last_decision: Optional[dict] = None
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

    def _fallback_stop(self, side: str, entry: float) -> float:
        """持仓缺少止损价时，按趋势熔断比例重算一个，并大声告警。

        宁可给一个保守的保护，也不能留 0（=永不止损）。
        entry 也是 0 时返回 0，由调用方/对账逻辑处理——
        但那种情况下连成本基准都没有，属于必须人工介入的异常。
        """
        if entry <= 0:
            logger.critical("持仓缺少止损价且无开仓价，无法推算保护位，需人工介入")
            return 0.0
        # 唐奇安策略【刻意不设硬停损】（实测：加 2ATR 停损使每笔 +2.42%->+1.39%，
        # 100U 账户终值 689U->559U）。所以它的 stop 合法地就是 0。
        # 硬塞一个 25% 进去等于给这个策略套上另一个没验证过的规则，
        # 而且会在大屏上把一个不存在的保护位显示成"已设好"。
        if self._is_donchian():
            return 0.0
        pct = float(self.tcfg.trend_stop_pct) / 100.0
        stop = entry * (1 - pct) if side == "long" else entry * (1 + pct)
        logger.critical("持仓缺少止损价（脏数据），按开仓价 %.1f 与熔断 %.0f%% 重算为 %.1f",
                        entry, self.tcfg.trend_stop_pct, stop)
        self.db.log_risk("stop_missing",
                         "持仓无止损价，已按 %.0f%% 重算为 %.1f" % (self.tcfg.trend_stop_pct, stop))
        return stop

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
        open_trade = self.db.open_trade()
        if open_trade:
            self.pos = {
                "side": open_trade["side"],
                "qty": open_trade["qty"],
                "entry": open_trade["entry_price"],
                # 止损价绝不能是 0。0 的含义是"价格跌到 0 才止损"，
                # 等于这道保护静默消失，仓位会一直挂到跌破 SMA 才出场。
                # 脏数据（早期 bug 遗留、人工插入）真的会出现这一列是 NULL 的情况，
                # 事故 2 只修了 reconcile_live 的接管路径，attach() 这条被漏掉了。
                "stop": (open_trade["stop_price"] or 0.0) or self._fallback_stop(
                    open_trade["side"], float(open_trade["entry_price"] or 0.0)),
                "target": open_trade["target_price"] or 0.0,
                "entry_ts": open_trade["entry_ts"],
                # 按时间推算已走过的 3s 根数，避免重启把超时计时清零
                "bars": max(0, int((int(time.time() * 1000) - open_trade["entry_ts"]) / 3000)),
                "best": open_trade["entry_price"],
                "trade_id": open_trade["id"],
                # 必须用开仓时记录的值，不能用默认值：
                # 2026-11-01 事故 —— 一笔按 18 小时设计的单在重启后被当成 600 根(30分钟)强平，
                # 其间它本来还在正常轨道上。
                "max_hold_bars": (open_trade.get("max_hold_bars")
                                  or (10 ** 9 if self._is_chan()
                                      else self.tcfg.max_hold_bars)),
                "open_reason": open_trade["open_reason"],
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
            logger.info("恢复未平仓持仓: %s", self.pos)

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
        db_qty = 0.0
        db_side = ""
        if self.pos:
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
            db_own = float(self.pos["qty"]) + float(self.pos.get("dust") or 0.0)
            db_qty = db_own * (1 if self.pos["side"] == "long" else -1)
            db_side = self.pos["side"]
        ex_qty = float(actual["qty"])

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
            # 交易所空仓但数据库有仓：把数据库的仓标记为已平
            if self.pos:
                self.db.close_trade({
                    "symbol": self.tcfg.symbol, "side": self.pos["side"],
                    "qty": self.pos["qty"], "entry_ts": self.pos["entry_ts"],
                    "entry_price": self.pos["entry"], "exit_ts": int(time.time() * 1000),
                    "exit_price": float(actual.get("entry") or self.pos["entry"]),
                    "stop_price": self.pos["stop"], "target_price": self.pos["target"],
                    "pnl_gross": 0.0, "fee": 0.0, "pnl_net": 0.0, "mfe": 0.0, "mae": 0.0,
                    "open_reason": self.pos.get("open_reason", ""),
                    "close_reason": "实盘对账：交易所已无此仓",
                    # 按这笔仓【原本】的来历记账，不能一律记成真实交易：
                    # 从纸面切到实盘后，库里遗留的纸面仓会走到这条路径。
                    "paper": int(self.pos.get("paper") or 0)})
                self.pos = None
        else:
            # 交易所有仓（可能是手工开的或上次没记上）：接管它
            side = "long" if ex_qty > 0 else "short"
            entry = float(actual.get("entry") or 0.0)
            if entry <= 0:
                # 现货查不到开仓价（交易所只记余额）。绝不能留 0：
                # entry=0 会让止损价也变成 0，等于"永远满足止损"，一开机就把仓位平掉。
                #
                # 也不能随便拿当前市价顶替。举个真实的坏情况：
                #   40000 买入 -> 进程崩了 -> 在 28000 重启。
                #   拿 28000 当成本，止损就成了 21000——可这仓已经浮亏 30% 了，
                #   而这道止损还要再跌 25% 才动，等于保护失效。
                #   而且平仓时 pnl 按 28000 算，把真实亏损记小了，边际统计全废。
                #
                # 现货有 /api/v3/myTrades，能拿到真实成交。先用它算加权成本。
                entry = await self._cost_basis_from_trades(ex_qty)
                if entry > 0:
                    logger.warning("交易所持仓 %+.6f 无开仓价（现货特性），"
                                   "已按真实成交记录算出成本基准 %.1f", ex_qty, entry)
                else:
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
            # 唐奇安没有硬停损，接管时同样不能凭空造一个——否则这个仓位
            # 会被一条回测里不存在的规则提前砍掉，而看板还会显示"止损已设"。
            if self._is_donchian():
                stop = 0.0
            else:
                stop = (entry * (1 - self.tcfg.trend_stop_pct / 100.0) if side == "long"
                        else entry * (1 + self.tcfg.trend_stop_pct / 100.0))
            if self.pos:
                self.db.close_trade({
                    "symbol": self.tcfg.symbol, "side": self.pos["side"],
                    "qty": self.pos["qty"], "entry_ts": self.pos["entry_ts"],
                    "entry_price": self.pos["entry"], "exit_ts": int(time.time() * 1000),
                    "exit_price": entry, "stop_price": self.pos["stop"],
                    "target_price": self.pos["target"], "pnl_gross": 0.0, "fee": 0.0,
                    "pnl_net": 0.0, "mfe": 0.0, "mae": 0.0,
                    "open_reason": self.pos.get("open_reason", ""),
                    "close_reason": "实盘对账：接管交易所实际持仓", "paper": 0})
            # 这里必须是 log_trade（写入），不是 open_trade（读取未平仓记录）。
            # 原先误写 open_trade(...) 并传了 dict，签名不匹配直接抛 TypeError，
            # 而 reconcile_live() 的调用方吞掉了异常，结果是：
            #   孤儿仓位每一轮都抛错、从不被真正接管，系统始终认为自己空仓，
            #   然后在"有持仓"的状态下还可能再买一次。
            # 这个错误只在【交易所有币、数据库没仓】时才触发，
            # 正常流程不会走到——所以纸面跑了半个月也没暴露。
            self.db.log_trade({
                "symbol": self.tcfg.symbol, "side": side, "qty": abs(ex_qty),
                "entry_ts": int(time.time() * 1000), "entry_price": entry,
                "stop_price": stop, "target_price": 0.0,
                "open_reason": "实盘对账接管", "paper": 0,
                "strategy": self.tcfg.strategy,
                "max_hold_bars": 10 ** 9 if self._is_chan()
                else self.tcfg.max_hold_bars})
            # log_trade 是异步入队，拿不到自增 id。趋势策略靠 SMA 出场，
            # 不依赖 trade_id；这里保持 None 而不是编一个假 id。
            tid = None
            self.pos = {"side": side, "qty": abs(ex_qty), "entry": entry, "stop": stop,
                    "worst": entry,
                        "target": 0.0, "entry_ts": int(time.time() * 1000), "bars": 0,
                        "best": entry, "trade_id": tid,
                        "max_hold_bars": 10 ** 9 if self._is_chan()
                        else self.tcfg.max_hold_bars,
                        "open_reason": "实盘对账接管",
                        # 接管的仓位是交易所上本来就有的，它的开仓手续费
                        # 我们客观上不知道（可能是人工买的、或早于本系统上线）。
                        # 记 0 是诚实的下界，不是漏写——但代价是这笔的盈亏
                        # 会系统性偏乐观（少了开仓成本）。显式写出来，
                        # 免得以后有人看到缺项以为是 bug 又去改。
                        "fee": 0.0}
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
            self.guard.position_mismatch(why)
            if self.guard.notify is not None:
                self.guard.notify("实盘对账不一致，已按交易所持仓修正（详见日志）")

        # 对账已经完成，而且我们【已经站在趋势里】——没有"补上车"这回事了。
        # 不清这个标志的话它会一直挂着（接管路径不经过 _open，而 _open 才是
        # 正常清标志的地方），于是这笔仓一旦被 25% 熔断平掉、价格却还在
        # SMA 上方，startup_entry 会立刻把仓补回去，绕过
        # "必须等下一根 4h"的规则。止损后立刻重新进场是最坏的行为。
        if self.pos and getattr(self.trend, "need_reconcile", False):
            self.trend.mark_reconciled()

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
        if self._is_chan():
            await self._step_trend(bars_by_iv, price, equity, now_ms)
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
            ex = self.trend.want_exit(price)
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
            if self._is_donchian():
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
            snap["pos"] = {"side": p["side"], "qty": p["qty"], "entry": p["entry"],
                           "stop": p["stop"], "entry_ts": p["entry_ts"]}
            snap["pos_pnl_pct"] = (price - p["entry"]) / p["entry"] * 100.0
            snap["hold_h"] = (int(time.time() * 1000) - p["entry_ts"]) / 3600000.0
        return snap

    def _build_trend_snapshot(self, price: float, equity: float) -> dict:
        st = self.trend.state(price)
        if self._is_donchian():
            return self._build_chan_snapshot(st, price, equity)
        sma = st["sma"]
        snap = {"mode": "trend", "ready": st["ready"], "bars": st["bars"],
                "lookback": st["lookback"], "sma": sma, "price": price,
                "above": st["above"], "next_bar_h": self.trend.next_bar_eta_h(),
                "hold": bool(self.pos)}
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
                           "stop": p["stop"], "entry_ts": p["entry_ts"]}
            snap["pos_pnl_pct"] = (price - p["entry"]) / p["entry"] * 100.0
            snap["hold_h"] = (int(time.time() * 1000) - p["entry_ts"]) / 3600000.0
        return snap

    async def _confirm_stop_price(self, local_px: float) -> float:
        """向交易所复核一次价格。拿不到就返回 0（表示无法确认，按本地价执行）。"""
        fn = getattr(self.ex, "mark_price", None)
        if fn is None:
            return 0.0
        try:
            return float(await fn() or 0.0)
        except Exception as e:
            logger.warning("复核价格失败，按本地价执行：%s", e)
            return 0.0

    def _is_spot(self) -> bool:
        """是否现货执行。**单一口径**：以实际挂上的执行器为准（它才是真正下单的东西）。

        之前杠杆/步长/最小名义用 self.ex.name 判断，而手续费缓冲用配置项判断，
        两者若不一致（配置写 spot、执行器却是永续），仓位计算就会自相矛盾。
        统一到一个入口，避免这种"一半现货一半永续"的错位。
        """
        name = getattr(self.ex, "name", "")
        if name == "spot":
            return True
        if name == "live":
            return False
        # paper 执行器没有真实市场，它的规格必须服从配置——
        # 否则会出现"配置是现货、纸面却按永续的 0.001 步长和 50U 最小名义撮合"，
        # 于是纸面权益曲线和回放数字全都算在了错的品种上。
        # 实测代价：权益 71.77U 时永续一手要 77.80U，直接被判"买不起"，
        # 而现货一手只要 0.78U——同一笔钱在两种规格下结论完全相反。
        return self.tcfg.exec_market == "spot"

    def _chan_warmup(self) -> int:
        """策略变热需要多少根 K。

        唐奇安要 entry_n + 2 根（判定那根的窗口 n 根 + 上一根做"新鲜突破"判断）；
        4h SMA 要 sma_lookback + 1 根。写错这个数只有一个后果：
        策略在数据还不够时就报"ready"，用半截窗口算出错误的通道。
        """
        if self._is_donchian():
            return int(getattr(self.tcfg, "donchian_entry", 150) or 150) + 2
        return int(self.tcfg.sma_lookback) + 1

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

    def _plan_leverage(self) -> float:
        """现货没有杠杆（也不该有：资金费随名义线性增长，加杠杆等于加倍付费）。"""
        if self._is_spot():
            return 1.0
        return min(float(self.tcfg.trend_leverage), float(self.tcfg.max_leverage))

    def _qty_step(self) -> float:
        """现货步长 0.00001 BTC，永续 0.001——用错会把仓位算成 0 或差 100 倍。"""
        if self._is_spot():
            return 0.00001
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
        return 0.003 if self._is_spot() else 0.001

    def _affordability_note(self, price: float, equity: float) -> str:
        """资金不够建仓时返回说明文字，够则返回空串。

        为什么要单独判断：策略的"空仓等待"有两种完全不同的原因——
        信号没到（继续等就好）、或者钱不够（等再久也不会成交）。
        混在同一句日志里，运维会一直等一个永远不会来的成交。
        """
        if equity <= 0:
            return ("余额为 0，无法交易｜现货模式下请确认 USDT 在【现货钱包】，"
                    "不是合约钱包")
        step = self._qty_step()
        lev = self._plan_leverage()
        need_notional = max(self._min_notional(), 1.0)
        # 一手 = 一个步长；连一手都不够就没法建仓
        one_lot = step * price / lev
        if equity < one_lot or equity * lev < need_notional:
            wallet = "现货钱包" if self._is_spot() else "合约钱包"
            return ("资金不足：权益 %.2fU，建一手至少需要 %.2fU 名义（一手 %.6f BTC ≈ %.2fU）"
                    "｜请确认 USDT 在%s" % (equity, need_notional, step, step * price, wallet))
        return ""

    def _min_notional(self) -> float:
        """现货最小名义 5U，永续 50U。这个差异对 100U 账户是能不能建仓的区别。"""
        if self._is_spot():
            return 5.0
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
        # 开仓前先看一眼账上已有的基础币，用于把零头一起接管。
        #
        # 为什么要管：现货手续费扣在基础币上，买入到账净额不是 step 整数倍，
        # 平仓按 step 取整卖出必然留零头；下一笔又叠上去，零头单调长大。
        # 实测每笔约留 0.0000087 BTC（78000 时约 0.68U），一年 30 笔约 12U，
        # 约占权益 9%，而且这部分永远不参与策略、跌的时候跟着跌。
        #
        # 只接管【小于一个 step】的残留：那一定是我们自己取整留下的。
        # 用户自己放在现货钱包里的 BTC 会远大于一个 step，绝不能被卖掉。
        pre_dust = 0.0
        if self._is_spot():
            try:
                pre = await self.ex.equity()
                pre_btc = float(pre.get("position_qty") or 0.0)
                step = self._qty_step()
                if step > 0 and 0.0 < pre_btc < step:
                    pre_dust = pre_btc
            except Exception:
                logger.warning("开仓前查基础币余额失败，本轮不接管零头", exc_info=True)
        fill = await self.ex.market_order(order_side, plan.qty,
                                          reason="%s 开仓 %s" % (sig["name"], side_txt))
        self._seq += 1
        client_id = "AI-%d-%d" % (now_ms, self._seq)
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
            fill = await self.ex.market_order(order_side, sell_qty,
                                              reason="平仓 " + reason,
                                              reduce_only=True)
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
        gross = (fill.price - p["entry"]) * sold_qty * direction
        fee_total = p.get("fee", 0.0) + fill.fee
        net = gross - fee_total
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
        return True

    # ---------------- 记录 ----------------
    def _log_decision(self, bars_by_iv, price, equity, now_ms, signal, action, reason,
                      gates=None, features=None, risk_info=None, signal_reason="",
                      induce_close="") -> None:
        bar = bars_by_iv.get(3000)
        bar_time = bar[-1].open_time if bar else now_ms
        if bar_time <= self._last_bar_time:      # 同一根桶只留一条决策
            return
        self._last_bar_time = bar_time
        self.decisions += 1
        d = {"ts": now_ms, "bar_time": bar_time,
             "feature_time": (features or {}).get("feature_time", bar_time),
             "symbol": self.tcfg.symbol, "price": price, "signal": signal,
             "action": action, "reason": (signal_reason + " | " if signal_reason else "") + reason,
             "gates": gates, "features": features, "risk": risk_info,
             "equity": round(equity, 4)}
        self.last_decision = d
        self.db.log_decision(d)
        if action.startswith("open") or action == "close":
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
        return {"trend": self.trend_state(),
                "equity": round(self._last_equity, 4),
                "pos": self.pos, "block_stats": self.block_stats,
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
