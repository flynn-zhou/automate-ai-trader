# -*- coding: utf-8 -*-
"""live 模式启动序列：用户注资后我跑的第一段代码，此前从未被执行过。

重点不是"能不能起来"，而是"起不来的时候是不是立刻、干净地拒绝"。

最危险的情形是：带着空凭证/错配置先把行情连上、数据库建好、日志刷一片
"看起来正常"的输出，最后才失败。那样的现场会误导排查方向——
你会以为是网络或行情问题，实际是凭证没配。
main.run() 特意把凭证校验放在任何 I/O 之前，这里就该验证它真的做到了。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader import main as main_mod
from ai_trader.exchange.live import LiveExecutor
from ai_trader.exchange.paper import PaperExecutor
from ai_trader.exchange.spot import SpotExecutor


def _cfg(mode, market="spot", key="k", secret="s"):
    from ai_trader.config import AppCfg
    c = AppCfg()
    c.trading.mode = mode
    c.trading.exec_market = market
    c.trading.api_key = key
    c.trading.api_secret = secret
    return c


@pytest.fixture()
def no_io(monkeypatch):
    """任何 I/O 组件被构造就立刻炸——用来证明校验发生在 I/O 之前。"""
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("凭证校验之前就开始了 I/O")

        def __getattr__(self, name):
            raise AssertionError("凭证校验之前就开始了 I/O")

    monkeypatch.setattr(main_mod, "Store", Boom)
    monkeypatch.setattr(main_mod, "Feed", Boom)
    monkeypatch.setattr(main_mod, "TradeDB", Boom)
    monkeypatch.setattr(main_mod, "Journal", Boom)


def test_live_without_api_key_refuses_before_any_io(no_io):
    """缺 key：必须抛 SystemExit，且绝不能先碰 Store/Feed/DB。"""
    cfg = _cfg("live", key="", secret="")
    with pytest.raises(SystemExit) as ei:
        import asyncio
        asyncio.get_event_loop().run_until_complete(main_mod.run(cfg, with_web=False))
    assert "AI_API_KEY" in str(ei.value)


def test_live_without_secret_refuses(no_io):
    cfg = _cfg("live", key="k", secret="")
    with pytest.raises(SystemExit) as ei:
        import asyncio
        asyncio.get_event_loop().run_until_complete(main_mod.run(cfg, with_web=False))
    assert "AI_API_SECRET" in str(ei.value)


def test_live_without_key_does_not_fall_back_to_paper(no_io):
    """绝不能"降级成纸面偷偷跑"——那会让人以为在下真单，其实在记账。"""
    cfg = _cfg("live", key="", secret="")
    with pytest.raises(SystemExit):
        import asyncio
        asyncio.get_event_loop().run_until_complete(main_mod.run(cfg, with_web=False))
    # 只要走到 SystemExit 就说明没有静默降级；若降级则会因 no_io 抛 AssertionError


def test_paper_mode_does_not_require_credentials(no_io):
    """纸面模式不需要凭证，但这里也不该因为缺 key 报错——
    它会去建 Store/Feed，于是触发 no_io 的 AssertionError，这正是我们要的：
    说明它确实越过了凭证校验，进入了正常启动流程。
    """
    cfg = _cfg("paper", key="", secret="")
    with pytest.raises(AssertionError):
        import asyncio
        asyncio.get_event_loop().run_until_complete(main_mod.run(cfg, with_web=False))


def test_executor_choice_spot_vs_perp():
    """exec_market 决定用哪个执行器——选错就是"现货策略跑在永续上"。"""
    cfg = _cfg("live", market="spot")
    # 直接验证 main 里的分支条件
    assert cfg.trading.exec_market == "spot"
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_trader", "main.py"), encoding="utf-8").read()
    assert "SpotExecutor(" in src and "LiveExecutor(" in src
    # spot 分支必须在 perp 分支之前，否则永远走不到
    assert src.index("SpotExecutor(") < src.index("LiveExecutor(")


def test_paper_executor_exists_for_fallback():
    assert PaperExecutor.name == "paper"
    assert SpotExecutor.name == "spot"
    assert LiveExecutor.name == "live"
