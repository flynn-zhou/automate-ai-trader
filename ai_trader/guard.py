"""无人值守闸（Watchdog）：我不在的时候，谁替系统踩刹车。

分工必须说清：
- 决策权在引擎（确定性代码，7x24 在服务器上跑）。
- 刹车权在 Guard（本文件）：它只做一件事——在异常条件下**禁止开新仓**，
  已有持仓仍然按止损/止盈正常管理（永不让一笔仓位因为"系统慌了"而失去保护）。
- 通知：状态跳变时写 journal + risk_events + 看板，并（若配置了）推飞书。

六条触发条件（阈值由我按 100U 高风险档定，理由是"归零前必须先停下来复盘"）：
 1. 权益从峰值回撤 >= 60%     -> 硬停，必须人工 reset（本金基本没了，不能再自动赌）
 2. 数据源 DOWN 持续 >= 30s    -> 临时停开仓（看不见行情就不许开枪），恢复自动解除
 3. 引擎卡住（30s 无新决策）   -> 临时停开仓（主循环假死是最危险的状态）
    心跳由 update() 自己维护；判定在【读的那一刻】也做一次——
    因为主循环死了以后，就没有人再调 update() 了。
 4. 当日亏损 >= 50%            -> 当日停开仓，次日自动解除
 5. 连续亏损 >= 3 次           -> 冷却 30 分钟（在引擎里执行，这里只做展示）
 6. 持仓与账户不一致           -> 硬停（真金白银时对账不一致 = 必须先查）
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class GuardState:
    peak_equity: float = 0.0
    halted_hard: bool = False
    halt_reason: str = ""
    # 硬停的【种类】（第 122 轮）。
    #
    # 为什么必须区分：_halt_hard() 有两个调用方，而它们的性质完全不同——
    #   "drawdown"  回撤熔断：市场走得差。市场会变，冷却期过后可以自己恢复。
    #   "mismatch"  账实不符：账和交易所说不到一块去（漏记成交/有人手工动了仓位/
    #               交易所侧出问题）。这三种都得【人去看一眼】，自动解除等于
    #               在一个自己不理解的仓位上继续下注。
    # 没有这个字段时，两者共用一个 halted_hard 布尔，任何"自动解除"的修法
    # 都会同时把账实不符放行——那比停摆危险得多。
    halt_kind: str = ""
    # 硬停发生在哪个决策时刻（用决策时钟 now_ms，不是墙钟：
    # 回放/回测里时间是被注入的，用墙钟算冷却期会得出荒唐的间隔）。
    halt_ts: int = 0
    temp_block: str = ""
    down_since: int = 0
    last_decision_ts: int = 0
    # 真实的墙钟：update() 上一次【真的被执行】的时刻。
    # 与 last_decision_ts 分开是有意的——后者是"决策时钟"（可被测试注入假时间），
    # 前者是"代码有没有在跑"。主循环死掉时，只有墙钟会继续走。
    last_heartbeat_ms: int = 0
    day: str = ""
    day_start_equity: float = 0.0
    # 当日亏损这条闸触发在哪一天。空串 = 没触发过。
    # 必须和 temp_block 分开记：temp_block 是三条闸共用的一个字符串，
    # 靠它自己分辨不了"这条闸是哪天触发的"。
    daily_halt_day: str = ""
    # 平仓连续失败次数（第 116 轮新增）。
    # 为什么它要进 Guard 而不是只留在引擎里：止损失效的时候
    # 「该不该继续开新仓」是个风控判断，而风控判断的唯一出口是 Guard。
    # 留在引擎里，看板/哨兵/健康自检全都看不到（实测四个监控面一个都不读）。
    close_fail_streak: int = 0
    # 每条临时闸【各自一格】（第 117 轮）。
    # 原来是四条闸共用一个 temp_block 字符串，每条只擦自己那一句，
    # 于是后写的会【直接覆盖】先写的：实测
    #   close_fail_streak_changed(6)  -> 拦住
    #   数据源 DOWN 33 秒 + 恢复      -> (True, '')，而计数还是 6
    # 止损通道还坏着，系统却放行开仓。
    # temp_block 保留为【展示用的合成字符串】，由 _refresh_temp_block() 派生。
    temp_reasons: dict = field(default_factory=dict)
    last_equity: float = 0.0
    alerts_sent: int = 0
    history: list = field(default_factory=list)


class Guard:
    def __init__(self, cfg, tdb=None, journal=None, notify: Optional[Callable] = None):
        self.cfg = cfg
        self.tdb = tdb
        self.journal = journal
        self.notify = notify
        self.st = GuardState()
        # ★ 这个数字和策略的真实回撤是【一对】，不能单独改任何一个（第 121 轮）。
        #
        # 用部署代码把全历史 4h 喂进真实引擎（tools/guard_on_history.py，
        # 数据 2017-09-14 -> 2026-11-02，20000 根）实测：
        #
        #     不带 Guard                201 次开仓  100U -> 2300.19U  回撤 77.10%
        #     带 60% 硬停（就是这里）      14 次开仓  100U ->  118.30U  <- 2018-03-08 停死
        #
        # 停死之后 8 年 0 次开仓：_halt_hard() 只能人工复位，
        # 而 AI_FEISHU_WEBHOOK 是空的 —— 没有任何人会知道该去复位。
        #
        # 扫一遍阈值（tools/dd_limit_sweep.py）：
        #
        #     60% -> 118.30U   70% -> 91.21U   75% -> 75.78U
        #     78% 及以上 -> 2300.19U（等于不设 Guard）
        #
        # 分水岭正好是 77.10%，也就是策略自己的历史最大回撤。
        # **硬停线低于策略的正常回撤范围时，它不是保护，是自毁。**
        # 而且越"保守"越糟：60% 比 75% 好，但两个都是 -95% 量级。
        #
        # 同样用 60% 硬停，只要允许每隔一段时间人工复位一次：
        #
        #     每 30 天复位 -> 2447.50U    每 180 天复位 -> 2574.44U
        #
        # 所以根因不是阈值高低，是【停摆之后没有任何恢复通道】。
        # 改阈值只是把问题藏起来；真正要补的是一条自动恢复路径。
        self.max_drawdown_pct = 60.0

        #: 回撤硬停的冷却期（天）。到期【自动解除】，不需要人。
        #:
        #: 为什么需要它（第 121/122 轮实测）：
        #:   策略历史最大回撤 77.10%，硬停线 60% —— 这条硬停【必然】触发。
        #:   触发后只有 POST /api/guard/reset 能解开，而 AI_FEISHU_WEBHOOK 是空的，
        #:   没有任何人会知道该去复位。于是"硬停 + 人工介入"在现实里
        #:   等于"硬停 + 无人知晓 = 永久停摆"：
        #:
        #:       不带 Guard    201 次开仓  100U -> 2300.19U
        #:       带 60% 硬停    14 次开仓  100U ->  118.30U  <- 2018-03-08 停死
        #:       之后 8 年 0 次开仓
        #:
        #:   同一条 60% 硬停，只加"每隔一段时间复位一次"：
        #:       每 30 天 -> 2447.50U   每 180 天 -> 2574.44U
        #:   说明根因不是阈值高低，是【停摆之后没有恢复通道】。
        #:
        #: 设 <= 0 表示关闭自动解除（退回"只能人工复位"的旧行为），便于回退。
        self.hard_halt_cooldown_days = 30.0

        self.feed_down_grace_ms = 30000
        self.stall_grace_ms = 30000
        self.daily_loss_halt_pct = 50.0
        # 峰值必须从库里恢复，不能只活在内存里。
        #
        # 这里曾经只在 update() 里累加，进程一重启峰值就归零，
        # 第一次 update() 把它设成【当时权益】——
        # 于是第 1 条闸度量的不是"从峰值回撤"，而是"从上次重启回撤"。
        #
        # 实测过的漏判：真实峰值 190、权益跌到 40（真实回撤 78.9%），
        # 但重启后峰值为 80，Guard 只看到 50%，60% 硬停不触发。
        # 部署、服务器重启、OOM 都会重启——这不是理论风险。
        #
        # 而 reset() 之后仍然从当前权益重新起算（人工复位的语义没变），
        # 因为这里的恢复只发生在【构造时】，也就是进程启动那一刻。
        self._restore_peak(tdb)

        # 硬停状态也要从库里恢复（第 123 轮）。
        #
        # 上面那段注释只讲了【峰值】，而 halted_hard 一个字都没做：
        # 它只活在内存里，进程一重启就是 False。
        # 而重启不是理论风险——部署、服务器重启、OOM 都会重启，
        # 我自己每轮都在部署。于是"停下等人查账"这条闸的
        # 【实际有效期只到下一次重启为止】。
        #
        # 对账实不符尤其致命：它意味着我们不知道真实敞口，
        # 重启把它清掉等于"换个进程继续在不懂的仓位上交易"。
        #
        # 对回撤则会让冷却期永远不到期：halt_ts 每次重启归零，
        # 只要重启比 30 天更频繁，30 天就永远走不完——又变回永久停摆。
        self._restore_halt(tdb)

    def _restore_peak(self, tdb) -> None:
        """从审计库恢复当前策略时代内的最高权益。失败就退回从 0 起算。"""
        if tdb is None:
            return
        try:
            strategy = getattr(getattr(self.cfg, "trading", None), "strategy", "") or ""
            # 必须按账本过滤：纸面的钱不是真钱，不能拿来当实盘的风控基准。
            # 过滤条件和 engine._open 写库时用的完全一致
            # （paper = 1 if 执行器是纸面 else 0，而执行器由 mode 决定）。
            paper = 1 if (getattr(getattr(self.cfg, "trading", None), "mode", "paper")
                          != "live") else 0
            # 基线时刻（第 128 轮）：资金被提走又注入后，旧峰值属于另一笔钱。
            # 只把起点往后推，绝不往前拉 —— 往前拉等于把见过的回撤藏起来。
            since = 0
            try:
                gs = tdb.load_guard_state() or {}
                since = int(gs.get("baseline_ts") or 0)
            except Exception:
                since = 0
            self.st.baseline_ts = since
            peak = tdb.peak_equity(strategy, paper, since)
        except Exception:
            # 恢复失败不能让进程起不来：退回旧行为（从当前权益起算），
            # 但要留下明确日志——否则又是"静默失效"。
            logger.warning("[GUARD] 从库恢复峰值失败，退回从当前权益起算", exc_info=True)
            return
        if peak > 0:
            self.st.peak_equity = float(peak)
            logger.info("[GUARD] 已从库恢复峰值 %.4f（策略 %s）", peak, strategy or "-")

    def _restore_halt(self, tdb) -> None:
        """从库里恢复硬停状态（第 123 轮）。

        恢复失败不能让进程起不来：退回"未硬停"的旧行为，但要留下明确日志。
        注意这个退回方向和 _restore_peak 一样是【宽松】的——
        严格来说恢复不了应该"宁停勿开"，但那会让一次库损坏变成永久停机，
        而 7x24 运行最怕的就是"升级即停摆"。日志必须足够显眼。
        """
        if tdb is None:
            return
        try:
            st = tdb.load_guard_state()
        except Exception:
            logger.warning("[GUARD] 从库恢复硬停状态失败，按未硬停处理", exc_info=True)
            return
        if not st:
            return

        # ---- 当日基准 / 日亏损闸 / 平仓失败计数（第 127 轮）----
        #
        # 不恢复这四个字段的后果，和 123 轮那次是同一族：
        #   day=""/day_start_equity=0 -> 第一次 update() 把基准设成【当时权益】
        #   -> "今天已经亏了 55%" 变成 "今天亏 0%" -> 日亏损闸自动解开。
        # 也就是：重启等于给这条闸按了一次人工复位。
        #
        # 跨日怎么处理：故意【不在这里判断】。
        # 存进来的 day 可能是昨天，而 update() 本来就有跨日逻辑——
        # day != today 就重置基准、daily_halt_day != day 就解除闸。
        # 在这里再写一份"跨日判断"，就是同一个规则有两处实现，
        # 迟早会出现两处不一致（第 126 轮那个变异的教训）。
        self.st.day = st.get("day") or ""
        self.st.day_start_equity = float(st.get("day_start_equity") or 0.0)
        self.st.daily_halt_day = st.get("daily_halt_day") or ""
        streak = int(st.get("close_fail_streak") or 0)
        if streak:
            # 走公开入口，不直接写字段：它同时负责把闸设上 + 合成 temp_block。
            # 直接赋值的话，计数回来了但闸没设上——"状态有了，闸没锁"。
            self.close_fail_streak_changed(streak)
            logger.warning("[GUARD] 已从库恢复平仓连续失败计数 %d 次", streak)
        if self.st.day_start_equity > 0:
            logger.info("[GUARD] 已从库恢复当日基准：%s %.4fU（日亏损闸 %s）",
                        self.st.day or "-", self.st.day_start_equity,
                        self.st.daily_halt_day or "未触发")

        if not st.get("halted_hard"):
            return
        self.st.halted_hard = True
        self.st.halt_reason = st.get("halt_reason") or ""
        self.st.halt_kind = st.get("halt_kind") or ""
        self.st.halt_ts = int(st.get("halt_ts") or 0)
        logger.warning("[GUARD] 已从库恢复硬停（kind=%s，触发于 %s）：%s",
                       self.st.halt_kind or "-", self.st.halt_ts, self.st.halt_reason)

    def _persist_halt(self) -> None:
        """把硬停状态写回库。任何改动这三个字段的地方都必须调它。

        ★ 写和清【走同一条路】，而且是同一条数据（第 116/117 轮的教训：
          写用一句话、清用另一个字面量，就是只会锁死不会解除）。
        """
        if self.tdb is None:
            return
        try:
            self.tdb.save_guard_state({
                "halted_hard": self.st.halted_hard,
                "halt_reason": self.st.halt_reason,
                "halt_kind": self.st.halt_kind,
                "halt_ts": self.st.halt_ts,
                # 下面四个是第 127 轮补的：不存的话，
                # 重启会把"今天已经亏了 55%"重置成"今天亏 0%"，
                # 等于给日亏损闸按了一次人工复位。
                "day": self.st.day,
                "day_start_equity": self.st.day_start_equity,
                "daily_halt_day": self.st.daily_halt_day,
                "close_fail_streak": self.st.close_fail_streak,
                "baseline_ts": int(getattr(self.st, "baseline_ts", 0) or 0),
            })
        except Exception:
            logger.error("[GUARD] 硬停状态落库失败", exc_info=True)

    # ---------------- 对外 ----------------
    def can_open(self, now_ms: Optional[int] = None) -> tuple:
        """现在能不能开新仓。

        now_ms 可由调用方（或测试）注入，默认走真实时钟。
        停滞判定必须在【读的那一刻】也做一次，见 _stall_reason 的说明。
        """
        if self.st.halted_hard:
            return False, self.st.halt_reason
        stall = (self._stall_reason(now_ms) if now_ms is not None
                 else self._stall_reason_wall())
        if stall:
            # 读侧判定出不等于把状态写回去：update() 恢复调用时会自己清掉，
            # 两处写同一个 temp_block 反而会让"谁负责解除"变得含糊。
            return False, stall
        if self.st.temp_block:
            return False, self.st.temp_block
        return True, ""

    def _stall_reason(self, now_ms: int) -> str:
        """引擎停顿时返回原因，健康时返回空串。

        ★ 为什么判定要设计成【可以被读侧调用】（第 113 轮）：
          这条闸原本只写在 update() 里。但 update() 是【主循环自己】调的——
          主循环一旦假死/卡死/抛异常退出，就没有人再调它，
          状态永远停在最后一次 update 的样子，也就是"一切正常"。
          真正需要踩刹车的时刻，恰好是这条闸唯一不会被执行到的时刻。
          所以判定放在这里，update() 和 can_open() 都能调，共用一份逻辑。

        两侧都必须成立：
          - 写侧：心跳会过期（叫"停"要叫得动）
          - 读侧：没人写的时候也能看出它停了（叫"停"要有人听得见）
        """
        last = self.st.last_decision_ts
        if not last:
            # 进程刚起、一次决策都还没有：不能凭空报"卡住"。
            return ""
        gap = now_ms - last
        if gap >= self.stall_grace_ms:
            return "引擎已 %.0fs 无新决策，禁止开仓" % (gap / 1000.0)
        return ""

    def _stall_reason_wall(self) -> str:
        """读侧的停滞判定：用【墙钟】看 update() 上一次真的被执行是什么时候。

        为什么读侧不能复用 _stall_reason(决策时钟)：
        主循环死掉以后没有任何人再调 update()，决策时钟就冻在那儿；
        而调 can_open() 的是看板和 /api/guard——它们活在别的线程/进程里，
        问的是"现在"，用的是墙钟。两把尺子不一样，混用会得出荒谬的差值。
        """
        if not self.st.last_heartbeat_ms:
            return ""
        gap = int(time.time() * 1000) - self.st.last_heartbeat_ms
        if gap >= self.stall_grace_ms:
            return "引擎已 %.0fs 无新决策，禁止开仓" % (gap / 1000.0)
        return ""

    def notify_decision(self, ts_ms: int) -> None:
        """（保留）人工盖心跳。生产代码【不应】调用它，见下。"""
        self.st.last_decision_ts = ts_ms

    def update(self, equity: float, src: str, now_ms: int) -> None:
        """每个决策周期调用一次。负责更新峰值、判定六条闸、状态跳变时留痕。"""
        today = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
        if self.st.day != today:
            self.st.day = today
            self.st.day_start_equity = equity
            # 当日基准变了就必须落库（第 127 轮）：
            # 不落的话重启后 day_start_equity=0，第一次 update() 会把它
            # 设成【当时权益】——"今天已经亏了 55%" 变成 "今天亏 0%"，
            # 日亏损闸被重启解开。
            self._persist_halt()

        self.st.last_equity = equity
        if equity > self.st.peak_equity:
            self.st.peak_equity = equity

        # 0) 回撤硬停的自动解除（第 122 轮）。
        #
        # 顺序必须在【峰值更新之后、回撤判定之前】：
        #   - 之后：解除时要拿"当前权益"当新峰值，得先有 last_equity。
        #   - 之前：解除会把峰值重新基线到当前权益，紧接着的回撤判定
        #     算出来是 0%，不会在同一拍里又立刻停回去。
        #   放反了就是"解除 -> 同一拍重新触发 -> 永远解不开"，
        #   看起来有恢复通道，实际一次都没走通。
        self._maybe_release_hard_halt(now_ms)

        # 1) 峰值回撤硬停
        if self.st.peak_equity > 0:
            dd = (self.st.peak_equity - equity) / self.st.peak_equity * 100.0
            if dd >= self.max_drawdown_pct and not self.st.halted_hard:
                self._halt_hard("从峰值 %.2fU 回撤 %.1f%%（>%.0f%%），停止开新仓，等待人工复盘"
                                % (self.st.peak_equity, dd, self.max_drawdown_pct),
                                kind="drawdown", now_ms=now_ms)

        # 2) 数据源 DOWN
        if src == "DOWN":
            if not self.st.down_since:
                self.st.down_since = now_ms
            elif now_ms - self.st.down_since >= self.feed_down_grace_ms:
                self._set_temp("feed_down", "数据源 DOWN 已 %.0fs，禁止开仓" % (
                    (now_ms - self.st.down_since) / 1000.0))
        else:
            self.st.down_since = 0
            self._set_temp("feed_down", "")

        # 3) 引擎卡住
        #
        # ★ 心跳由这里维护，不由调用方在进门前自己盖（第 113 轮）。
        #   原来的写法是 engine.py 先 notify_decision(now_ms) 再
        #   update(equity, src, now_ms)——判定用的是同一个 now_ms，
        #   now_ms - last_decision_ts 恒等于 0，这条闸【结构上永不成立】。
        #   线上佐证：risk_events 里 guard_temp 一条都没有（62 条全是 engine_error）。
        # 顺序【必须】是"先判定、后盖章"：反过来就又是自我免疫
        # （盖完再判，差值恒为 0——我第一版的修法就踩了这个坑，
        #   测试 test_the_heartbeat_can_actually_expire 当场抓住）。
        stall = self._stall_reason(now_ms)
        self.st.last_decision_ts = now_ms
        # 用真实墙钟而不是传入的 now_ms：这个字段的用途是
        # 「update() 这个函数上一次真的被执行是什么时候」，
        # 调用方给什么时刻都不影响它。
        self.st.last_heartbeat_ms = int(time.time() * 1000)
        self._set_temp("stall", stall)

        # 4) 当日亏损
        #
        # ★ 这条闸原本【没有解除路径】（第 114 轮修复）。
        #   temp_block 有三个写入点，前两个都配了清除分支：
        #       数据源 -> elif startswith("数据源"): 清
        #       引擎   -> elif startswith("引擎"):   清
        #       日亏损 -> （无）
        #   实测效果不是"当日停开仓"，而是"亏到 50% 就永久停摆"：
        #   跨日之后 day_start_equity 已经重置成 48，当日亏损 0%，
        #   但 temp_block 还挂着那句"今日停止开新仓"，直到有人手工 reset。
        #   7x24 无人值守下这就是一条单程票。
        #
        # 语义必须是【当日】而不是【当前】：
        #   触发之后当天即使反弹回 -40% 也不许接着赌（文档写的是"当日停开仓"），
        #   所以用一个 latch（daily_halt_day）记住是哪一天触发的，
        #   只有"日子变了"才解除。写成"当前亏损超过 50% 才拦"会变成软闸。
        if self.st.day_start_equity > 0:
            day_pct = (equity - self.st.day_start_equity) / self.st.day_start_equity * 100.0
            if day_pct <= -self.daily_loss_halt_pct:
                self.st.daily_halt_day = self.st.day
                self._set_temp("daily_loss",
                               "当日亏损 %.1f%% 已达上限，今日停止开新仓" % day_pct)
            elif self.st.daily_halt_day and self.st.daily_halt_day != self.st.day:
                # 跨日：解除。
                self.st.daily_halt_day = ""
                self._set_temp("daily_loss", "")
            elif self.st.daily_halt_day == self.st.day and self.st.day:
                # ★ 当天已经触发过、现在反弹回来了 —— 闸必须【重新挂上】（第 127 轮）。
                #
                # 缺这一支的后果，只有「触发过 + 反弹 + 重启」三者同时成立时才出现：
                #   亏到 -55% -> latch=今天 -> 反弹到 -20% -> 重启
                #   -> day_start_equity / daily_halt_day 都从库里恢复了
                #   -> 但 temp_reasons 是空的，亏损也没到 -55%
                #   -> 上面两个分支都不走 -> 闸静默打开。
                #
                # 也就是说：存了 latch 却没有用它重新挂闸，等于白存。
                # 这条闸的语义是【当日】而不是【当前】——
                # 文档写的是"触发之后当天即使反弹回 -40% 也不许接着赌"。
                #
                # 每拍都重新挂是安全的：latch 为空表示今天没触发过，
                # 那是绝大多数字拍的情形，不会误锁。
                self._set_temp("daily_loss",
                               # 措辞必须保留「当日亏损」这个前缀：
                               # 全量测试当场抓住了我第一版改成的「当日已触发过…」——
                               # 少了这四个字，既有测试（以及靠这段文字识别
                               # 状态的哨兵）就认不出来。
                               # 这是"改措辞"变成"改契约"的典型：文字在这里是接口。
                               "当日亏损已达上限（基准 %.2fU），今日停止开新仓"
                               % self.st.day_start_equity)

        # 四条闸各自求值完之后，再合成展示用的那一句。
        self._refresh_temp_block()

    def reset(self, reason: str = "人工复位") -> None:
        self.st.halted_hard = False
        self.st.halt_reason = ""
        # 人工复位连 halt_kind / halt_ts 一起清，否则
        # 残留的 halt_ts 会让下一次硬停的冷却期从很久以前算起，
        # 刚停下就被判"已满 30 天"立刻放行。
        self.st.halt_kind = ""
        self.st.halt_ts = 0
        # 人工复位同样要落库：不写的话重启会把硬停读回来，系统再也起不来。
        self._persist_halt()
        self.st.temp_reasons.clear()
        self.st.temp_block = ""
        self.st.daily_halt_day = ""
        self.st.peak_equity = 0.0
        self._event("guard_reset", reason)

    def snapshot(self) -> dict:
        last = self.st.last_equity
        dd = None
        if self.st.peak_equity > 0 and last:
            dd = round((self.st.peak_equity - last) / self.st.peak_equity * 100.0, 2)
        # 冷却期还剩多少天。None = 不会自动解除（账实不符，或冷却期关闭）。
        #
        # 为什么必须报出来：如果只显示"硬停"两个字，用户没有任何办法
        # 判断它是"等 30 天自己好"还是"必须人去处理"。而这两种情况的
        # 应对方式完全相反——一个什么都不用做，一个必须立刻查账。
        release_in_days = None
        if self.st.halted_hard and self.st.halt_kind == "drawdown":
            cd = float(self.hard_halt_cooldown_days or 0.0)
            if cd > 0:
                left = cd - (int(self.st.last_decision_ts or 0)
                             - int(self.st.halt_ts or 0)) / 86400000.0
                release_in_days = round(max(0.0, left), 2)
        return {
            "halted_hard": self.st.halted_hard,
            "halt_reason": self.st.halt_reason,
            "halt_kind": self.st.halt_kind or "",
            "release_in_days": release_in_days,
            "temp_block": self.st.temp_block,
            "peak_equity": round(self.st.peak_equity, 4),
            "equity": round(last, 4) if last else None,
            "drawdown_pct": dd,
            "day_start_equity": round(self.st.day_start_equity, 4),
            "rules": {
                "max_drawdown_pct": self.max_drawdown_pct,
                "hard_halt_cooldown_days": self.hard_halt_cooldown_days,
                "feed_down_grace_s": self.feed_down_grace_ms / 1000.0,
                "stall_grace_s": self.stall_grace_ms / 1000.0,
                "daily_loss_halt_pct": self.daily_loss_halt_pct,
            },
        }

    # ---------------- 临时闸的存储 ----------------
    #: 展示优先级：数字越小越先显示。
    #:
    #: 为什么必须【定死】而不是"谁最后写谁赢"（第 117 轮）：
    #: 看板上只有一格，"今天显示数据源、明天显示止损"取决于调度顺序，
    #: 用户就没法判断到底哪个是真问题。
    #: 排序依据是「这条闸背后有多少不确定性」：
    #:   止损失效 > 引擎卡住 > 硬性日亏损 > 数据源抖动
    #: 前两条意味着系统可能已经在不受控地漏钱。
    TEMP_ORDER = ("close_fail", "stall", "daily_loss", "feed_down")

    def _set_temp(self, key: str, reason: str) -> None:
        """写/清一条临时闸。只动自己那一格。"""
        if reason:
            self.st.temp_reasons[key] = reason
        else:
            self.st.temp_reasons.pop(key, None)

    def _refresh_temp_block(self) -> None:
        """把各条闸合成看板用的那一句，并在合成结果变化时留痕。

        temp_block 现在只是【展示用的派生值】，不再是闸状态本身。
        状态在 temp_reasons 里，谁也覆盖不了谁。
        """
        prev = self.st.temp_block
        for key in self.TEMP_ORDER:
            why = self.st.temp_reasons.get(key)
            if why:
                self.st.temp_block = why
                break
        else:
            self.st.temp_block = ""
        if prev != self.st.temp_block:
            self._event("guard_temp", self.st.temp_block or "临时限制解除")
            # 闸的状态变了就落库。放在【变化时】而不是每次 _set_temp 里：
            # _set_temp 每 3 秒被调好几次（stall / feed_down 每拍都写一遍空值），
            # 无差别落库等于一天 28800 次 INSERT，纯属浪费。
            self._persist_halt()

    # ---------------- 内部 ----------------
    def _maybe_release_hard_halt(self, now_ms: int) -> None:
        """冷却期到了就自动解除【回撤】硬停（第 122 轮）。

        只管 kind="drawdown"。账实不符（kind="mismatch"）永远不自动解除：
        那意味着我们不知道真实敞口，自动放行等于在不理解的仓位上继续下注。
        留空 kind 的也按"不放行"处理（安全的一侧）。

        为什么恢复方式是【把峰值重新基线到当前权益】而不是"等回撤缩小"：
        硬停只停开新仓，此时通常已经空仓；空仓状态下权益不动，
        "回撤"就永远停在触发时那个数字上——按回撤恢复的通道
        结构上永远不会被触发（和 113 轮那条"读侧判定"是同一类陷阱：
        条件看起来合理，但在它自己的前提下一辈子不成立）。

        自动解除之后，60% 这条闸仍在，只是从【新峰值】重新起算。
        实测（tools/dd_limit_sweep.py）：每 30 天复位一次 -> 2447.50U，
        比不设 Guard 的 2300.19U 还高（复利时点不同），
        远好于永久停摆的 118.30U。
        """
        if not self.st.halted_hard:
            return
        if self.st.halt_kind != "drawdown":
            return
        days = float(self.hard_halt_cooldown_days or 0.0)
        if days <= 0:
            return
        elapsed_ms = int(now_ms or 0) - int(self.st.halt_ts or 0)
        if elapsed_ms < days * 86400000.0:
            return
        self._release_hard_halt(now_ms, days)

    def _release_hard_halt(self, now_ms: int, days: float) -> None:
        """解除硬停，并把峰值重新基线到当前权益。

        注意【只】清硬停相关的三个字段：
        临时闸（temp_reasons）、当日亏损、平仓失败计数一概不动。
        它们各自有自己的解除条件，"顺手一起清"会把
        止损通道失效、数据源 DOWN 这些真问题一起放行——
        这正是 reset() 不适合拿来做自动解除的原因（它是给人工复盘用的，
        语义是"人已经把一切都看过了"）。
        """
        was = self.st.halt_reason
        self.st.halted_hard = False
        self.st.halt_reason = ""
        self.st.halt_kind = ""
        self.st.halt_ts = 0
        # 峰值重新基线：否则下一拍的回撤判定从旧峰值算，立刻又停回去。
        self.st.peak_equity = float(self.st.last_equity or 0.0)
        msg = (u"回撤硬停已满 %.0f 天，自动解除并将峰值重新基线到 %.2fU"
               u"（原停因：%s）" % (days, self.st.peak_equity, was))
        self.st.history.append({"ts": int(time.time() * 1000), "what": "auto_release",
                                "reason": msg})
        # 解除也要落库，否则重启会把刚解除的硬停又读回来。
        self._persist_halt()
        self._event("guard_auto_release", msg, alert=True)

    def _halt_hard(self, reason: str, kind: str = "", now_ms: int = 0) -> None:
        """硬停。kind 决定它能不能自动解除（第 122 轮）。

        kind="drawdown" 可自动解除（市场会变）；
        kind="mismatch" 永不自动解除（要人看一眼）。
        留空 = 按"不可自动解除"处理：新增调用方默认走安全的一侧，
        想自动解除必须【显式申明】，避免以后有人加了个新硬停
        顺手被冷却期放行。
        """
        self.st.halted_hard = True
        self.st.halt_reason = reason
        self.st.halt_kind = kind
        # 决策时钟优先；调用方没给才退回墙钟。
        self.st.halt_ts = int(now_ms or (time.time() * 1000))
        self.st.history.append({"ts": int(time.time() * 1000), "what": "halt",
                                "kind": kind, "reason": reason})
        # 必须先落库再发通知：通知可能重入/抛异常，而"停住了"这个事实
        # 不能依赖通知成功。
        self._persist_halt()
        self._event("guard_halt", reason, alert=True)

    #: 平仓连续失败多少次算「止损通道失效」。
    #:
    #: 5 次不是拍的：_close 的退避是 3/6/12/24/48 秒，
    #: 第 5 次失败时已经过去约 93 秒，价格在下跌里足够走出很远了。
    #: 而偶发失败（交易所 5xx 抖一下、网络丢包）一般第 2 次就恢复，
    #: 设成 2 会把正常抖动升级成停机。
    close_fail_limit: int = 5

    #: temp_block 里属于「止损失效」这条闸的前缀。
    #: 写入和清除【必须】引用同一个常量，否则就是只会锁死不解除。
    CLOSE_FAIL_TAG = u"止损通道失效"

    def close_fail_streak_changed(self, streak: int) -> None:
        """平仓连续失败次数变化（第 116 轮）。

        为什么这件事必须进 Guard：
        止损失效的完整链路是
            价格跌破熔断线 -> _close() 发单 -> 交易所 5xx
            -> FillResult(ok=False) -> 退避 3/6/12... -> 下一轮再来
            -> 仓位仍在手上，价格继续跌
        退避逻辑本身是对的（该重试的确实在重试），但**「止损打不出去」这个事实
        没有任何出口**：实测看板 /api/trade/state、哨兵、健康自检、Guard
        四个监控面【一个都不读】close_failed。它不是靠 bug 静默，
        是靠「没有人订阅这个事件」静默。

        所以在这里落成一条真闸：连续失败到 close_fail_limit 就停止开新仓。
        注意只停【开新仓】——平仓路径完全不受影响，越失效越要继续试着平。

        计数归零即解除：通道恢复后不需要人工复位。
        """
        self.st.close_fail_streak = int(streak or 0)
        # ★ 前缀必须引用【同一个常量】，不能写两遍（第 116 轮踩到）。
        #   我第一版写的是 tag = "平仓连续失败"，而写进 temp_block 的是
        #   "止损通道失效：平仓已连续失败 N 次…"，两者根本不是同一个开头，
        #   startswith(tag) 永远为假 —— 于是这条闸【只会锁死，不会解除】。
        #   这和第 114 轮「当日亏损没有清除分支」是同一家族：
        #   写和清用了两个不同的字面量，代码读起来完全对称。
        #   test_a_recovered_close_channel_releases_the_guard 当场抓住。
        if self.st.close_fail_streak >= self.close_fail_limit:
            self._set_temp("close_fail", self.CLOSE_FAIL_TAG + "：平仓已连续失败 %d 次，"
                           "仓位保护不住，禁止开新仓" % self.st.close_fail_streak)
        else:
            self._set_temp("close_fail", "")
        # 合成 + 留痕统一由这里做：写和清走【同一条路】，
        # 不会再出现"写用一句话、清用另一个字面量"那种只会锁死不会解除的漏洞。
        self._refresh_temp_block()

    def position_mismatch(self, detail: str) -> None:
        """第 6 条闸：持仓与账户不一致 -> 硬停。

        为什么必须是【硬停】而不是临时限制（第 115 轮）：
        账实不符意味着我们不知道真实敞口是多少。这时候继续交易，
        等于在一个自己不理解的仓位上叠加下注。
        而且它不会是"抖一下就好"——要么漏记了一笔成交，要么有人手工动了仓位，
        要么交易所侧出了问题，三种都得人去看一眼。

        ★ 这条闸原本【根本没有接线】：
          _halt_hard() 在 ai_trader/ 里只有 1 个调用点（回撤硬停），
          reconcile_live() 发现不一致时只做了"按交易所修正认知"就继续跑了。
          文档写着「真金白银时对账不一致 = 必须先查」，
          实际行为是「不一致？没关系，修正一下继续交易」。
          生产佐证：日志里「实盘对账不一致」出现 0 次，
          risk_events 里也从来没有 guard_halt。

        重复触发不重复告警：已在硬停态就不再写事件（和回撤硬停一致）。
        """
        if self.st.halted_hard:
            return
        # ★ kind="mismatch"：永不自动解除（第 122 轮）。
        #   回撤能吃冷却期，账实不符不能——后者意味着我们不知道真实敞口，
        #   冷却期结束之后它依然不知道。自动放行等于赌"没人动过仓位"。
        self._halt_hard(detail, kind="mismatch")

    def executor_halt(self, detail: str, recovered: bool = False) -> None:
        """执行器熔断的状态跳变。

        为什么要走这里而不是让执行器自己推通知：
        执行器（spot.py / live.py）手里没有 db、没有 journal、没有 webhook，
        它们只该管下单。而 _event 已经是「risk_events + journal + 飞书」的唯一通道，
        状态跳变本来就该从这条路过——README 第一条就写着「通知：状态跳变时」。

        以前的洞：halted 只是执行器的内存属性，sentinel 只读库，健康自检只看
        进程和 K 线，于是熔断发生时【没有任何人会收到通知】，
        只能靠人打开看板。7x24 运行不会有人一直盯着看板。
        """
        self._event("executor_halt_recovered" if recovered else "executor_halted",
                    detail, alert=True,
                    title=u"执行器熔断已解除" if recovered else u"执行器熔断")

    def _event(self, kind: str, detail: str, alert: bool = False,
               title: Optional[str] = None) -> None:
        """状态跳变的唯一出口：risk_events + journal + 飞书。

        title 只影响给人看的标题（journal/飞书），kind 仍是稳定的机器标识，
        查询口径（按 kind 统计）不受影响。不传 title 时沿用 kind，
        与历史行为一致。
        """
        logger.warning("[GUARD] %s: %s", kind, detail)
        shown = title or kind
        if self.tdb is not None:
            try:
                self.tdb.log_risk(kind, detail)
            except Exception:
                pass
        if self.journal is not None:
            try:
                self.journal.add("alert", "无人值守闸：" + shown, detail,
                                 json.dumps(self.snapshot(), ensure_ascii=False))
            except Exception:
                pass
        if alert and self.notify is not None:
            try:
                self.notify("[AI Trader] " + shown, detail)
            except Exception:
                logger.error("通知发送失败", exc_info=True)


def feishu_notifier(webhook: str) -> Optional[Callable]:
    """飞书自定义机器人推送（复用 k-trader 已配好的 webhook）。失败静默，不影响主流程。"""
    if not webhook:
        return None
    import urllib.request

    def _send(title: str, body: str) -> None:
        payload = json.dumps({"msg_type": "text",
                              "content": {"text": "%s\n%s" % (title, body)}},
                             ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(webhook, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()

    return _send