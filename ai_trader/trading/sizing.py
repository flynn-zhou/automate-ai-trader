# -*- coding: utf-8 -*-
"""仓位与风险派生计算（《下单与守卫_运行规则.md》§3 / §4 / §6 的唯一实现）。

设计要点：
  1. 纯函数，无 IO、无全局状态 —— 可以被 Web、守卫、回测、测试同时调用，
     保证「配置页显示的数字」和「守卫实际用的数字」是同一份代码算出来的。
  2. 仓位由**风险定仓**（先有结构止损距离，再算名义仓位），
     保证金比例只是**上限校验**，不参与定仓。
     理由见文档 §3.1：把保证金比例当输入会让 40% 对应 0.57% 的止损，
     落在双边手续费+滑点（约 0.15%）的噪音里，物理上不可交易。
  3. 时区只在这里转换一次：用户口径北京时间(UTC+8) -> UTC。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

# 用户口径时区：北京时间 UTC+8（固定偏移，无夏令时，故不用 zoneinfo 依赖）
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")

# 维持保证金率近似（BTC/ETH 小仓位一档）。生产应改为按交易所档位表读取。
DEFAULT_MAINTENANCE_MARGIN_RATE = 0.005
# 安全倍数：爆仓距离必须是止损距离的这么多倍，保证「止损永远先于爆仓触发」
DEFAULT_SAFETY_MULTIPLE = 2.5


@dataclass(frozen=True)
class SizingConfig:
    """三个可配置输入 + 两个物理常数。Web 配置页将来编辑的就是前三个。"""

    capital: float = 100.0          # 账户本金 U
    risk_pct: float = 5.0           # 每笔风险占本金 %
    leverage: float = 22.0          # 交易所杠杆
    margin_cap_pct: float = 40.0    # 单笔保证金占本金上限 %
    maintenance_margin_rate: float = DEFAULT_MAINTENANCE_MARGIN_RATE
    safety_multiple: float = DEFAULT_SAFETY_MULTIPLE

    def __post_init__(self) -> None:
        if self.capital <= 0:
            raise ValueError("本金必须为正: %r" % (self.capital,))
        if not 0 < self.risk_pct <= 100:
            raise ValueError("风险比例必须在 (0,100]: %r" % (self.risk_pct,))
        if self.leverage <= 1:
            raise ValueError("杠杆必须 > 1: %r" % (self.leverage,))
        if self.maintenance_margin_rate <= 0:
            raise ValueError("维持保证金率必须为正")
        if self.safety_multiple <= 0:
            raise ValueError("安全倍数必须为正")
        # 1/L - m 必须为正，否则「爆仓距离」为零或负，任何仓位都瞬间爆掉
        if 1.0 / self.leverage - self.maintenance_margin_rate <= 0:
            raise ValueError(
                "杠杆过高：1/%r - %r <= 0，爆仓距离非正（上限约 %.0f 倍）"
                % (self.leverage, self.maintenance_margin_rate,
                   1.0 / self.maintenance_margin_rate))


@dataclass(frozen=True)
class Derived:
    """配置页要显示的四行 + 若干辅助量。全部由 SizingConfig 推导。"""

    risk_per_trade: float        # 第1行 R（向下取整，U）
    liq_distance_pct: float      # 第2行 爆仓距离 %
    max_stop_distance_pct: float  # 第3行 止损距离上限 %
    margin_cap_amount: float     # 第4行 单笔保证金上限 U
    lives: int                   # 生命数 = floor(本金 / R)
    notional_at_max_stop: float  # 止损用满上限时的名义仓位（参考）

    def as_rows(self) -> list:
        """给 Web 配置页直接渲染的四行。"""
        return [
            {"label": "每笔风险 R", "value": "%g U" % self.risk_per_trade},
            {"label": "爆仓距离", "value": "%.3f%%" % self.liq_distance_pct},
            {"label": "止损距离上限", "value": "%.3f%%" % self.max_stop_distance_pct},
            {"label": "单笔保证金上限", "value": "%g U" % self.margin_cap_amount},
        ]


def derive(cfg: SizingConfig) -> Derived:
    """把配置算成四行派生结果。改任何输入都会立刻反映到这里。"""
    R = math.floor(cfg.capital * cfg.risk_pct / 100.0)
    if R < 1:
        R = 0  # 本金太小，连 1U 风险都分不出来 -> 0 表示不可交易
    liq_dist = (1.0 / cfg.leverage - cfg.maintenance_margin_rate) * 100.0
    max_stop = liq_dist / cfg.safety_multiple
    margin_cap = cfg.capital * cfg.margin_cap_pct / 100.0
    lives = int(cfg.capital // R) if R > 0 else 0
    notional_at_max = (R / (max_stop / 100.0)) if (R > 0 and max_stop > 0) else 0.0
    return Derived(
        risk_per_trade=float(R),
        liq_distance_pct=liq_dist,
        max_stop_distance_pct=max_stop,
        margin_cap_amount=margin_cap,
        lives=lives,
        notional_at_max_stop=notional_at_max,
    )


@dataclass(frozen=True)
class PositionPlan:
    """给定一个结构止损距离，算出来的仓位方案 + 是否允许开仓。"""

    stop_distance_pct: float
    notional: float
    margin: float
    margin_pct_of_capital: float
    liq_buffer: float          # 爆仓距离 / 止损距离，必须 >= safety_multiple
    ok: bool
    reasons: tuple = field(default=())

    def as_dict(self) -> dict:
        return {
            "stop_distance_pct": self.stop_distance_pct,
            "notional": self.notional,
            "margin": self.margin,
            "margin_pct_of_capital": self.margin_pct_of_capital,
            "liq_buffer": self.liq_buffer,
            "ok": self.ok,
            "reasons": list(self.reasons),
        }


def plan_position(cfg: SizingConfig, stop_distance_pct: float) -> PositionPlan:
    """按结构止损距离定仓（风险定仓），并逐条检查开仓前置门（文档 §4.2）。

    stop_distance_pct: 结构止损距离，正数，单位 %。例如 1.2 表示 1.2%。
    """
    if stop_distance_pct <= 0:
        raise ValueError("止损距离必须为正: %r" % (stop_distance_pct,))

    d = derive(cfg)
    reasons = []

    # 名义仓位由风险倒推：N = R / d
    notional = d.risk_per_trade / (stop_distance_pct / 100.0) if d.risk_per_trade else 0.0
    margin = notional / cfg.leverage
    margin_pct = (margin / cfg.capital * 100.0) if cfg.capital else 0.0
    liq_buffer = d.liq_distance_pct / stop_distance_pct

    if d.risk_per_trade <= 0:
        reasons.append("本金过小，无法分出 1U 风险")
    if stop_distance_pct > d.max_stop_distance_pct:
        reasons.append(
            "止损 %.3f%% 超过上限 %.3f%%（爆仓会先于止损触发）"
            % (stop_distance_pct, d.max_stop_distance_pct))
    if liq_buffer < cfg.safety_multiple:
        reasons.append(
            "爆仓缓冲 %.2f 倍 < 要求 %.2f 倍" % (liq_buffer, cfg.safety_multiple))
    if margin > d.margin_cap_amount:
        reasons.append(
            "保证金 %.2fU 超过上限 %.2fU（占本金 %.1f%%）"
            % (margin, d.margin_cap_amount, margin_pct))

    return PositionPlan(
        stop_distance_pct=stop_distance_pct,
        notional=notional,
        margin=margin,
        margin_pct_of_capital=margin_pct,
        liq_buffer=liq_buffer,
        ok=not reasons,
        reasons=tuple(reasons),
    )


# ------------------------------------------------------------------ 开仓时段闸门
# 星期编号：周一=0 … 周日=6（与 datetime.weekday() 一致，全系统统一）
# 窗口用「一天内的分钟数」表示，[start, end)；end=1440 表示到当天 24:00
# 只用显式名字构造，禁止写裸数字当星期 —— 曾经把周日写成 5（那是周六），
# 会让闸门整体错一天且不报错。


def default_session_windows() -> dict:
    """默认开仓时段：周日 12:00 起，周一到周四全天，周五周六关闭。

    用户口径（北京时间）：故意跳过周五、周六与周日上午。
    """
    MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
    return {
        MON: [(0, 1440)],
        TUE: [(0, 1440)],
        WED: [(0, 1440)],
        THU: [(0, 1440)],
        FRI: [],
        SAT: [],
        SUN: [(12 * 60, 1440)],
    }


@dataclass(frozen=True)
class SessionConfig:
    windows: dict = field(default_factory=default_session_windows)
    enabled: bool = True

    def to_beijing(self, ts_utc: datetime) -> datetime:
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        return ts_utc.astimezone(BEIJING)


def in_trade_window(cfg: SessionConfig, ts_utc: datetime) -> bool:
    """该 UTC 时刻是否允许**开新仓**（北京时间口径判定）。"""
    if not cfg.enabled:
        return True
    bj = cfg.to_beijing(ts_utc)
    minute = bj.hour * 60 + bj.minute
    for start, end in (cfg.windows.get(bj.weekday()) or []):
        if start <= minute < end:
            return True
    return False


def session_label(cfg: SessionConfig) -> str:
    """人类可读的时段描述，供日志与将来的配置页使用。"""
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    parts = []
    for dow in range(7):
        wins = cfg.windows.get(dow) or []
        if not wins:
            parts.append("%s 关闭" % names[dow])
            continue
        seg = []
        for start, end in wins:
            seg.append("%02d:%02d-%02d:%02d" % (start // 60, start % 60, end // 60, end % 60))
        parts.append("%s %s" % (names[dow], "、".join(seg)))
    return "北京时间：" + "；".join(parts)
