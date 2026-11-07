# -*- coding: utf-8 -*-
"""值守员（Sentinel）：7x24 自己看盘、自己判断、自己汇报。

和 guard.py 的区别：
- guard 是刹车：异常时禁止开仓，保护资金。
- sentinel 是眼睛：定期巡检并把"发生了什么、该不该叫醒人"写进日志 + 推送。

它回答的正是"我不在的时候谁在监督"这个问题：
1. 每 5 分钟巡检：服务、数据流、决策流、持仓、权益、回撤、被拒信号。
2. 状态变化才汇报（不刷屏）；持续异常才升级告警。
3. 每天定时出一份"值班报告"，进研究日志。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

# 和 Guard 共用同一份峰值实现，避免同一个「回撤」概念算出两个数。
from .trade_db import equity_has_paper, peak_equity_at, strategy_start_ts_at

logger = logging.getLogger(__name__)


def _q(db: str, sql: str, args=()):
    if not os.path.exists(db):
        return []
    c = sqlite3.connect(db)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


@dataclass
class SentinelState:
    last_check_ts: int = 0
    last_report_day: str = ""
    last_alert_ts: int = 0
    last_sig: str = ""
    issues: dict = field(default_factory=dict)
    checks: int = 0


class Sentinel:
    def __init__(self, trade_db: str, market_db: str, journal=None, notify=None,
                 api_base: str = "http://127.0.0.1:8090", mode: str = ""):
        self.trade_db = trade_db
        self.market_db = market_db
        self.journal = journal
        self.notify = notify
        self.api_base = api_base
        # 纸面/实盘必须分开算回撤和本次盈亏，否则纸面历史会污染实盘告警：
        # 实测服务器上 paper=1 的峰值 104.74、实盘从 100 起步时，
        # 哨兵会报 "回撤 4.5%"，而这段回撤在实盘里根本没发生过。
        self.mode = mode or os.environ.get("AI_TRADE_MODE", "paper")
        self.st = SentinelState()
        self.stale_decision_s = 90
        self.stale_bar_s = 90
        self.dd_warn_pct = 30.0

    # ---------------- 巡检 ----------------
    def check(self) -> dict:
        now_ms = int(time.time() * 1000)
        issues = []
        facts = {}

        # 1) 决策流是否在动
        d = _q(self.trade_db, "SELECT MAX(ts) FROM decisions")
        last_dec = (d[0][0] if d and d[0][0] else 0)
        gap_s = (now_ms - last_dec) / 1000.0 if last_dec else 99999
        facts["decision_gap_s"] = round(gap_s, 1)
        # 阈值必须跟着策略走：趋势策略的决策日志有 10 分钟节流（状态没变就不重复写库），
        # 用 90 秒的门槛会每轮都误报，然后把真正的问题淹掉。
        limit_s = 20 * 60 if self._trend_mode() else self.stale_decision_s
        if gap_s > limit_s:
            issues.append("决策停滞 %.0fs" % gap_s)

        # 2) 行情是否在动
        b = _q(self.market_db, "SELECT MAX(open_time) FROM bars WHERE interval_ms=3000")
        last_bar = (b[0][0] if b and b[0][0] else 0)
        bar_gap = (now_ms - last_bar) / 1000.0 if last_bar else 99999
        facts["bar_gap_s"] = round(bar_gap, 1)
        if bar_gap > self.stale_bar_s:
            issues.append("行情停滞 %.0fs" % bar_gap)

        # 3) 持仓与权益
        # 策略不同，"多久算异常"完全不同：
        # 剥头皮持仓超过半天就是卡住了；趋势策略持仓几周是设计如此。
        trend_mode = self._trend_mode()
        facts["strategy"] = "trend" if trend_mode else "scalp"
        pos = _q(self.trade_db, "SELECT id,side,qty,entry_price,stop_price,target_price,entry_ts"
                               " FROM trades WHERE exit_ts IS NULL ORDER BY id DESC LIMIT 1")
        if pos:
            pid, side, qty, ep, stop, tgt, ets = pos[0]
            hold_h = (now_ms - ets) / 3600000.0
            facts["position"] = {"id": pid, "side": side, "qty": qty, "entry": ep,
                                 "stop": stop, "target": tgt, "hold_h": round(hold_h, 2)}
            limit_h = 24 * 90 if trend_mode else 12
            if hold_h > limit_h:
                issues.append("持仓已 %.1f 小时（%s 上限 %.0f，检查是否卡住）"
                              % (hold_h, "趋势" if trend_mode else "剥头皮", limit_h))
        else:
            facts["position"] = None

        eq = _q(self.trade_db, "SELECT equity,balance FROM equity ORDER BY ts DESC LIMIT 1")
        equity = eq[0][0] if eq else 0.0
        facts["equity"] = round(equity, 4)

        # 「本次运行累计」的基准也必须是【当前策略时代的第一条权益】。
        #
        # 以前取全历史最早那条（= 上一代剥头皮开工时的 100U），于是生产上
        # 日报长期输出：
        #   权益 71.77U，本次运行累计 -28.23U（-28.23%），距峰值回撤 0.0%
        #   巡检无异常。
        #   判断：本次运行已亏损超过 25%，建议立刻停手复盘（可 systemctl stop ai-trader）。
        # 同一份报告既说「回撤 0.0% / 无异常」，又建议关掉系统。
        # 那 -28.23% 全部是已废弃剥头皮的亏损；趋势策略 0 笔成交、权益没动过。
        #
        # 对一个「不做策略决策」的用户，这条建议会让他关掉一个没出问题的系统。
        # 基准 = 策略开始【之前】最后一条权益，也就是这段策略开局时带着的资本。
        # 不能用 ts >= since 取之后第一条：库里通常没有正好落在那一刻的记录，
        # 取到的会是「已经亏过之后」的值，基准于是变成当前值，累计永远报 0。
        since_ts = strategy_start_ts_at(self.trade_db, "trend" if trend_mode else "")
        # 本次盈亏同样只能看当前账本：纸面跑过 100 -> 120 -> 71.77，
        # 实盘从 100 起步时若混着算，用户会看到一段不属于实盘的累计盈亏。
        paper_flag = 0 if self.mode == "live" else 1
        if equity_has_paper(self.trade_db):
            # 旧库没有这一列 -> 退化成分账本不过滤，不能让它抛异常把巡检打死
            base_eq = _q(self.trade_db,
                         "SELECT equity FROM equity WHERE ts <= ? AND COALESCE(paper,1)=?"
                         " ORDER BY ts DESC LIMIT 1", (since_ts, paper_flag))
            if not (base_eq and base_eq[0][0]):
                base_eq = _q(self.trade_db,
                             "SELECT equity FROM equity WHERE COALESCE(paper,1)=?"
                             " ORDER BY ts ASC LIMIT 1", (paper_flag,))
        else:
            base_eq = _q(self.trade_db,
                         "SELECT equity FROM equity WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                         (since_ts,))
            if not (base_eq and base_eq[0][0]):
                base_eq = _q(self.trade_db,
                             "SELECT equity FROM equity ORDER BY ts ASC LIMIT 1")
        start_eq = base_eq[0][0] if base_eq and base_eq[0][0] else equity
        facts["session_pnl"] = round(equity - start_eq, 4)
        facts["session_pnl_pct"] = round((equity - start_eq) / start_eq * 100, 2) if start_eq else 0.0

        # 峰值必须只算【当前策略开始之后】的权益。
        # 用全历史 MAX 会把上一代策略（剥头皮）赚过又亏掉的 100U 当成基准，
        # 于是趋势模式一开局就「回撤 28%」，这个警报永远消不掉——等于没有警报。
        #
        # 这里和 Guard 必须用【同一份实现】。以前它们各写各的：
        #   sentinel 从库里算（持久、正确）
        #   guard 从内存算（重启归零、漏判）
        # 同一个「回撤」概念给出两个数，而 guard 才是真正下闸的那个。
        peak_eq = peak_equity_at(self.trade_db,
                                 "trend" if trend_mode else None,
                                 paper_flag) or equity
        dd = (peak_eq - equity) / peak_eq * 100 if peak_eq else 0.0
        facts["drawdown_pct"] = round(dd, 2)
        if dd >= self.dd_warn_pct:
            issues.append("回撤 %.1f%%（警戒线 %.0f%%）" % (dd, self.dd_warn_pct))

        # 3.5) 风控硬停（第 125 轮）
        #
        # 这是「谁在看着刹车」这个问题的答案。
        #
        # 第 123 轮让硬停能扛过重启（落库），第 124 轮让止损失效能锁闸。
        # 两轮都在让闸更【持久】。但持久之后有个更糟的后果：
        # 闸一旦停住，它就一直停着，而【没有任何人会知道】。
        #
        # 查证当时的实际情况：
        #   guard.check() 读 decisions / bars / trades / equity / 被拒信号，
        #   一点都不读 risk_events 和 guard_state。
        #   而 AI_FEISHU_WEBHOOK 是空的 -> notify 是 None -> _alert 只能写日志。
        # 于是账实不符硬停之后：
        #   risk_events 写 guard_halt  (没人读)
        #   guard_state.halted_hard=1  (没人读)
        #   notify 是 None             (发不出去)
        #   看板要人主动打开才看得见
        # 系统永久停机，而所有主动渠道都不出声。
        #
        # 这和第 116 轮是同一个形状（钱在漏，仪表都指着正常），
        # 只是这次漏的不是钱，是「系统已经停了」这个事实。
        halt = self._halt_state()
        facts["guard_halt"] = halt
        if halt:
            kind, reason, ts = halt
            days = (now_ms - ts) / 86400000.0 if ts else 0.0
            issues.append(
                "风控硬停中（%s，已停 %.1f 天）：%s｜%s"
                % (kind, days, reason[:120], self._halt_release_hint(kind)))

        # 4) 被拒信号（说明信号在出但全被拦，值得关注）
        rej = _q(self.trade_db,
                 "SELECT COUNT(*) FROM decisions WHERE action='skip' AND ts > ?", (now_ms - 3600000,))
        facts["skips_1h"] = rej[0][0] if rej else 0

        # 4.5) 信号想要开仓，却一直没开成
        # 这是唯一会静默失效的状态：价格在均线上方、信号该出，
        # 但被 资金不足 / 退避 / 计划被拒 / 权益查不到 挡住，
        # 策略一直空仓却不报错。
        #
        # 【曾经漏掉最严重的那两种】
        # 以前这里写死 signal='trend_entry'，但趋势信号自己的名字是 trend_up：
        #   engine.py:541/628 -> "trend_entry"（计划被拒 / 闸门拦截）
        #   engine.py:801/825 -> sig["name"] = "trend_up"（权益过期 / 下单失败）
        # 于是「权益查不到所以不开仓」和「下单失败」这两条永远查不到。
        #
        # 后果正好落在最需要告警的场景上：
        #   用户注资后切成 live，API key 拼错/权限不足/IP 白名单没配 ->
        #   凭证【存在】所以启动校验放行 -> 每次 equity() 失败 ->
        #   _equity_stale -> 入场被拦，记 trend_up/skip「权益数据过期」->
        #   哨兵一声不吭。进程活着、行情在动、决策在写、看板有数，
        #   只有钱不会动。
        #
        # 修法：不要写死某一个信号名，用 signal LIKE 'trend%' 覆盖全部趋势信号，
        # 并把「权益」相关的原因也纳入。将来新增信号名也不会再漏。
        # 【不要把 action='skip' 提成整条查询的前置条件】
        # 我第一版就是这么改的，结果把真实阻塞全漏掉了。
        # 线上实测 4 条 trend_wait / action='hold' /
        #   「资金不足：权益 71.77U，建一手至少需要 55.00U 名义」——
        # 这是货真价实的"想开仓但钱不够"，但它记在 hold 上，不是 skip。
        # 所以 action 只能约束 signal LIKE 'trend%' 那一支，
        # reason 那几支必须保持顶层 OR，不限制 action。
        #
        # 这个回归是单元测试全绿的情况下被【线上真实数据】查出来的：
        # 新口径在本地新库上是 0/0（看不出来），在老库上旧=4 新=0。
        blocked = _q(self.trade_db,
                     "SELECT COUNT(*) FROM decisions"
                     " WHERE ts > ? AND ("
                     "   (signal LIKE 'trend%' AND action='skip')"
                     "   OR reason LIKE '%资金不足%'"
                     "   OR reason LIKE '%下单失败%'"
                     "   OR reason LIKE '%退避%'"
                     "   OR reason LIKE '%权益数据过期%'"
                     "   OR reason LIKE '%查询失败%')",
                     (now_ms - 7200000,))
        blocked_n = blocked[0][0] if blocked else 0
        facts["trend_blocked_2h"] = blocked_n
        if blocked_n > 0 and not pos:
            # 最近一条的具体原因要带上：只报次数，人还得自己去翻日志。
            _last = _q(self.trade_db,
                       "SELECT reason FROM decisions WHERE ts > ? AND ("
                       "   (signal LIKE 'trend%' AND action='skip')"
                       "   OR reason LIKE '%资金不足%'"
                       "   OR reason LIKE '%下单失败%'"
                       "   OR reason LIKE '%退避%'"
                       "   OR reason LIKE '%权益数据过期%'"
                       "   OR reason LIKE '%查询失败%')"
                       " ORDER BY ts DESC LIMIT 1", (now_ms - 7200000,))
            _why = (_last[0][0] if _last and _last[0][0] else "")[:60]
            facts["trend_blocked_reason"] = _why
            issues.append("信号想开仓但被挡住 %d 次/2h 且当前空仓（最近原因：%s）"
                          % (blocked_n, _why))

        # 5) 已平仓统计
        st = _q(self.trade_db, "SELECT COUNT(*), SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END),"
                               " SUM(pnl_net), SUM(fee) FROM trades WHERE exit_ts IS NOT NULL")
        if st:
            n, w, pnl, fee = st[0]
            facts["closed"] = {"n": n or 0, "wins": w or 0,
                               "pnl_net": round(pnl or 0.0, 4), "fee": round(fee or 0.0, 4)}

        self.st.checks += 1
        self.st.last_check_ts = now_ms
        return {"ts": now_ms, "issues": issues, "facts": facts, "ok": not issues}

    def _halt_state(self):
        """当前是否硬停。停着返回 (kind, reason, ts)，否则 None。

        ★ 只认【当前状态】，不认历史事件。
          risk_events 里的 guard_halt 是只增不减的流水，
          拿它判断"是否停着"，在一次自动解除/人工复位之后
          会永远报下去——又一个永远消不掉的警报。
          第 122 轮加的自动解除和第 123 轮加的持久化，
          都要求这里读的是【状态表】而不是【事件表】。
        """
        try:
            r = _q(self.trade_db,
                   "SELECT halted_hard,halt_kind,halt_reason,halt_ts"
                   " FROM guard_state WHERE id=1")
        except Exception:
            # 老库 / 表还没建：不能把巡检打死。
            # 退回读最近一条 halt/release 事件。
            try:
                e = _q(self.trade_db,
                       "SELECT kind,detail,ts FROM risk_events"
                       " WHERE kind IN ('guard_halt','guard_reset','guard_auto_release')"
                       " ORDER BY id DESC LIMIT 1")
            except Exception:
                return None
            if not e:
                return None
            k, detail, ts = e[0]
            if k != "guard_halt":
                return None
            return ("", str(detail or "")[:200], int(ts or 0))
        if not r or not r[0]:
            return None
        halted, kind, reason, ts = r[0]
        if not halted:
            return None
        return (str(kind or ""), str(reason or "")[:200], int(ts or 0))

    @staticmethod
    def _halt_release_hint(kind: str) -> str:
        """这条闸会不会自己好——报告必须说清楚，否则人会一直等。"""
        if str(kind or "") == "drawdown":
            return "回撤熔断，冷却期满会自动解除，不用人工干预"
        if str(kind or "") == "mismatch":
            return "账实不符，不会自动恢复——必须人工核对持仓后复位"
        return "不会自动恢复——必须人工核对后复位"

    def _trend_mode(self) -> bool:
        """从最近的决策动作反推当前策略，避免哨兵还要额外读配置。"""
        # 必须查 signal 列，不是 action 列！
        # 决策表里 action 存的是动作类别（watch/hold/open_long/skip），
        # 而 "trend_watch" 这类策略前缀存在 signal 列。
        # 原来查 action 永远不匹配，于是哨兵一直把趋势模式当剥头皮，
        # 由此 20 分钟停滞线、90 天持仓上限、回撤基准全部用错——
        # 三个阈值修复全被这一个字段名挡住了。
        #
        # 回看 24 小时而不是 10 分钟：策略身份不会小时级变化，
        # 窗口太短会在系统短暂停机/决策延迟时判错策略。
        r = _q(self.trade_db, "SELECT signal FROM decisions WHERE ts > ?"
                              " ORDER BY id DESC LIMIT 50", (int(time.time() * 1000) - 86400000,))
        return any(str(x[0] or "").startswith("trend") for x in r)

    # ---------------- 汇报 ----------------
    def run_once(self) -> dict:
        r = self.check()
        # 签名必须剥掉数字再比：原来的签名是 issues 原文拼接，
        # "决策停滞 357s" 变成 "199s" 就算"变了"，
        # 于是去重完全失效、每 5 分钟喊一次同样的警。
        # 一个永远在响的警报等于没有警报——用户会直接静音。
        sig = "|".join(re.sub(r"[0-9.]+", "#", x) for x in r["issues"])
        changed = sig != self.st.last_sig
        self.st.last_sig = sig
        if r["issues"] and (changed or r["ts"] - self.st.last_alert_ts > 1800000):
            self._alert("值守告警", "；".join(r["issues"]), r)
            self.st.last_alert_ts = r["ts"]
        elif not r["issues"] and changed:
            self._note("值守恢复", "上一轮问题已消失：" + (self.st.last_sig or "（无）"), r)
        return r

    def daily_report(self) -> dict:
        r = self.check()
        f = r["facts"]
        pos = f.get("position")
        lines = [
            "权益 %.2fU，本次运行累计 %+.2fU（%+.2f%%），距峰值回撤 %.1f%%"
            % (f["equity"], f["session_pnl"], f["session_pnl_pct"], f["drawdown_pct"]),
            ("持仓：%s %.3f @ %.1f，止损 %.1f，目标 %.1f，已持有 %.1f 小时"
             % (pos["side"], pos["qty"], pos["entry"], pos["stop"], pos["target"], pos["hold_h"]))
            if pos else "当前空仓",
        ]
        c = f.get("closed") or {}
        if c.get("n"):
            lines.append("已平仓 %d 笔，胜 %d，净 %+.3fU，手续费 %.3fU"
                         % (c["n"], c["wins"], c["pnl_net"], c["fee"]))
        if r["issues"]:
            lines.append("存在问题：" + "；".join(r["issues"]))
        else:
            lines.append("巡检无异常。")
        body = "\n".join(lines)
        # 报告里带上"该不该叫醒人"的判断，避免只报数字不表态
        if f.get("session_pnl_pct", 0) <= -25:
            body += "\n判断：本次运行已亏损超过 25%，建议立刻停手复盘（可 systemctl stop ai-trader）。"
        elif f.get("drawdown_pct", 0) >= self.dd_warn_pct:
            body += "\n判断：回撤超过 %.0f%%，建议检查信号质量与止损设置。" % self.dd_warn_pct
        elif c.get("n", 0) >= 20 and c.get("pnl_net", 0) < 0:
            body += "\n判断：样本已够 %d 笔且为负，建议按 review.py 的结论调整信号。" % c["n"]
        self._note("值班报告", body, r)
        return r

    def _alert(self, title: str, body: str, data) -> None:
        logger.warning("[SENTINEL] %s: %s", title, body)
        if self.journal is not None:
            try:
                self.journal.add("alert", title, body,
                                 json.dumps(data, ensure_ascii=False, default=str))
            except Exception:
                pass
        if self.notify is not None:
            try:
                self.notify("[AI Trader] " + title, body)
            except Exception:
                logger.error("值守推送失败", exc_info=True)

    def _note(self, title: str, body: str, data) -> None:
        logger.info("[SENTINEL] %s: %s", title, body.replace("\n", " / "))
        if self.journal is not None:
            try:
                self.journal.add("note", title, body,
                                 json.dumps(data, ensure_ascii=False, default=str))
            except Exception:
                pass
