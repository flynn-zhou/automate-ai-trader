# -*- coding: utf-8 -*-
"""默认配置必须是安全的那一边。

事故背景：TradingCfg.strategy 的默认值原本是 "scalp"——一个在回测里
已被否决的负期望策略。生产靠 systemd 注入 AI_STRATEGY=trend 覆盖它，
于是线上是对的。但任何【读不到 systemd 环境】的执行（手动排查脚本、
新机器、env 文件丢失）都会静默拿到 scalp，跑一个已知亏钱的策略，
而且完全不报错。

日志里一度同时存在 scalp 和 trend 的记录，我差点没意识到那是两个不同世界。
默认值应该指向"即使出错也不会亏钱"的那一边。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg


def test_default_strategy_is_trend_not_scalp():
    c = AppCfg()
    assert c.trading.strategy == "trend", (
        "默认策略不能是 scalp：它是回测里已否决的负期望策略。"
        "默认值一旦指向它，任何漏配环境都会静默亏钱。")


def test_default_exec_market_is_spot_for_long_only():
    """只做多策略默认走现货：永续的资金费是长期纯损耗。"""
    c = AppCfg()
    assert c.trading.exec_market == "spot"


def test_default_mode_is_not_live():
    """默认绝不能是实盘——那意味着任何误运行都会下真单。"""
    c = AppCfg()
    assert c.trading.mode != "live", "默认模式不能是 live"


def test_load_without_env_yields_safe_defaults(monkeypatch):
    """即使环境变量全空，load() 也必须给出安全配置。"""
    for k in ("AI_STRATEGY", "AI_EXEC_MARKET", "AI_TRADE_MODE",
              "AI_API_KEY", "AI_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    from ai_trader.config import load
    c = load()
    assert c.trading.strategy == "trend"
    assert c.trading.mode != "live"
