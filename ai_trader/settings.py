# -*- coding: utf-8 -*-
"""运行时可调参数（本金 / 每笔风险% / 杠杆），Web 上改立刻影响决策。

为什么要这个模块
----------------
用户的要求是「在 Web 显眼位置能配置，我实时调整」。所以参数不能只在
/etc/ai-trader/env 里——改一次要重启服务，等于没有实时性。

实时性从哪来
------------
Engine 构造时保存的是 **对象引用**：self.tcfg = cfg.trading，
并且每次决策现读 self.tcfg.risk_pct / max_leverage（不缓存数值）。
因此本模块直接改那个 dataclass 的字段，**下一次决策立即生效**，无需重启。
这是「引用共享」带来的性质，不是巧合——如果哪天引擎改成启动时拷贝一份数值，
这里会静默失效，所以测试里有一条专门盯这件事。

校验规则只有一处
----------------
范围校验全部委托给 ai_trader.trading.sizing.SizingConfig：
它已经在 __post_init__ 里拒绝 risk_pct<=0 / >100、1/leverage-mmr<=0 等。
本模块**不再写第二套 if**，只负责「存 / 取 / 套用到 cfg」。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading

from .dbutil import synchronized
from typing import Dict, Optional, Tuple

from .trading.sizing import SizingConfig, derive

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

K_CAPITAL = "sizing.capital"
K_RISK = "sizing.risk_pct"
K_LEVERAGE = "sizing.leverage"

# 缺省值与 TradingCfg 保持一致（100U / 5% / 22x）
DEFAULTS = {K_CAPITAL: 100.0, K_RISK: 5.0, K_LEVERAGE: 22.0}


class SettingsStore:
    """app_settings 键值表的薄封装。只存三类仓位参数。"""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # 跨线程使用（waitress 工作线程），线程安全由 _lock 保证
        self._lock = threading.RLock()
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)
        self._con.commit()

    @synchronized
    def close(self) -> None:
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---------- 读写 ----------
    @synchronized
    def get(self, key: str, default=None):
        row = self._con.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        if row is None or row[0] is None:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return default

    @synchronized
    def put(self, key: str, value) -> None:
        self._con.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "  updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (key, json.dumps(value)))
        self._con.commit()

    @synchronized
    def is_empty(self) -> bool:
        """库里一条仓位参数都没有 = 首次启动。"""
        n = self._con.execute(
            "SELECT COUNT(*) FROM app_settings WHERE key IN (?,?,?)",
            (K_CAPITAL, K_RISK, K_LEVERAGE)).fetchone()[0]
        return n == 0

    @synchronized
    def seed_from(self, trading_cfg) -> bool:
        """首次启动时，把 env/yaml 里的值写进库，作为**初始值**。

        为什么必须有这一步（这不是可有可无的便利）：
        如果省略它，空库时 sizing() 会回落到硬编码的 DEFAULTS(100/5/22)，
        于是 env 里写的 AI_PAPER_BALANCE=300 会被静默忽略，
        系统拿着 100U 去算仓位——**用户以为自己配的是 300**。
        这就是「数据库唯一权威」与「env 只是初始值」之间的那条缝。

        之后库是唯一权威：改 env 不再影响运行中的系统，
        改页面（写库）才影响。要让 env 重新生效，必须显式清空这张表。
        """
        if not self.is_empty():
            return False
        self.put(K_CAPITAL, float(getattr(trading_cfg, "paper_balance", DEFAULTS[K_CAPITAL])))
        self.put(K_RISK, float(getattr(trading_cfg, "risk_pct", DEFAULTS[K_RISK])))
        self.put(K_LEVERAGE, float(getattr(trading_cfg, "max_leverage", DEFAULTS[K_LEVERAGE])))
        logger.info("仓位参数首次播种（来源 env/yaml）: %s", self.sizing())
        return True

    @synchronized
    def sizing(self) -> Dict[str, float]:
        """当前有效的三类参数（缺省回落 DEFAULTS）。"""
        return {K_CAPITAL: float(self.get(K_CAPITAL, DEFAULTS[K_CAPITAL])),
                K_RISK: float(self.get(K_RISK, DEFAULTS[K_RISK])),
                K_LEVERAGE: float(self.get(K_LEVERAGE, DEFAULTS[K_LEVERAGE]))}

    # ---------- 校验 ----------
    @synchronized
    def validate(self, capital: float, risk_pct: float,
                 leverage: float) -> Tuple[bool, str, Optional[SizingConfig]]:
        """把「本金 / 风险% / 杠杆」变成一个合法的 SizingConfig，或明确拒绝原因。"""
        try:
            cfg = SizingConfig(capital=float(capital), risk_pct=float(risk_pct),
                               leverage=float(leverage))
        except (ValueError, TypeError) as e:
            return False, str(e), None
        return True, "", cfg

    # ---------- 套用 ----------
    @synchronized
    def set_sizing(self, capital: float, risk_pct: float, leverage: float
                   ) -> Tuple[bool, str]:
        ok, why, _cfg = self.validate(capital, risk_pct, leverage)
        if not ok:
            return False, why
        self.put(K_CAPITAL, float(capital))
        self.put(K_RISK, float(risk_pct))
        self.put(K_LEVERAGE, float(leverage))
        return True, ""

    @synchronized
    def apply_to(self, trading_cfg) -> Dict[str, float]:
        """把库里的参数写进活着的 TradingCfg（就地改字段，引擎下一次决策即生效）。"""
        s = self.sizing()
        trading_cfg.paper_balance = s[K_CAPITAL]
        trading_cfg.risk_pct = s[K_RISK]
        trading_cfg.max_leverage = s[K_LEVERAGE]
        return s

    # ---------- 四行派生（给 Web 展示）----------
    @synchronized
    def preview(self, capital: float, risk_pct: float, leverage: float) -> dict:
        """给定参数返回四行派生；参数非法时返回错误而不是瞎算。"""
        ok, why, cfg = self.validate(capital, risk_pct, leverage)
        if not ok:
            return {"ok": False, "error": why}
        d = derive(cfg)
        return {"ok": True, "rows": d.as_rows(),
                "risk_per_trade": d.risk_per_trade,
                "liq_distance_pct": d.liq_distance_pct,
                "max_stop_distance_pct": d.max_stop_distance_pct,
                "margin_cap_amount": d.margin_cap_amount,
                "lives": d.lives,
                "notional_at_max_stop": d.notional_at_max_stop}
