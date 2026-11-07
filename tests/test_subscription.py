# -*- coding: utf-8 -*-
"""多品种订阅清单。

要点：
  1. 订阅 ≠ 交易。清单只决定「收谁的行情」，引擎仍只交易一个品种。
  2. 种子只增不改：config 里有的品种首次写入，但**不得把关掉的重新打开**，
     否则运维在页面上关掉的品种，下次重启又自己回来了。
"""
from __future__ import annotations

import pytest


def test_normalize_accepts_and_uppercases():
    from ai_trader.market.subscription import normalize
    assert normalize(" btcusdt ") == "BTCUSDT"
    assert normalize("ethusdt") == "ETHUSDT"


@pytest.mark.parametrize("bad", ["", "   ", "BTC", "BTC-USDT", "BTCUSDT!",
                                 "USDT", "BTCUSDTX" * 5])
def test_normalize_rejects_garbage(bad):
    from ai_trader.market.subscription import normalize
    with pytest.raises(ValueError):
        normalize(bad)


def test_add_list_remove(tmp_path):
    from ai_trader.market.subscription import SubscriptionStore
    with SubscriptionStore(str(tmp_path / "s.db")) as st:
        st.add("BTCUSDT")
        st.add("ethusdt", note="观察")
        assert st.symbols() == ["BTCUSDT", "ETHUSDT"]
        assert st.is_subscribed("btcusdt")
        assert st.remove("ETHUSDT") is True
        assert st.symbols() == ["BTCUSDT"]
        assert not st.is_subscribed("ETHUSDT")


def test_disable_keeps_row_but_stops_streaming(tmp_path):
    """关掉不等于删除：保留备注与历史，只是不再出现在启用列表里。"""
    from ai_trader.market.subscription import SubscriptionStore
    with SubscriptionStore(str(tmp_path / "s.db")) as st:
        st.add("SOLUSDT", note="先关掉")
        assert st.set_enabled("SOLUSDT", False) is True
        assert st.symbols() == []
        assert st.symbols(only_enabled=False) == ["SOLUSDT"]
        rows = st.all()
        assert rows[0]["enabled"] is False
        assert rows[0]["note"] == "先关掉"


def test_seed_adds_but_never_reenables(tmp_path):
    """种子只增不改——否则页面上关掉的品种每次重启都自己回来。"""
    from ai_trader.market.subscription import SubscriptionStore
    with SubscriptionStore(str(tmp_path / "s.db")) as st:
        st.seed(["BTCUSDT", "ETHUSDT"])
        st.set_enabled("ETHUSDT", False)
        st.seed(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        assert st.symbols() == ["BTCUSDT", "SOLUSDT"]
        assert not st.is_subscribed("ETHUSDT"), "被重新打开了"


def test_seed_skips_invalid_symbols(tmp_path):
    from ai_trader.market.subscription import SubscriptionStore
    with SubscriptionStore(str(tmp_path / "s.db")) as st:
        st.seed(["BTCUSDT", "不是品种", ""])
        assert st.symbols() == ["BTCUSDT"]


def test_subscription_is_not_trading_symbol(tmp_path):
    """结构性断言：订阅清单里可以有多个品种，但引擎仍只交易一个。

    这条不是测行为，是防误读——将来有人看到「订阅了 5 个」就以为在 5 个币上开仓。
    """
    from ai_trader.config import TradingCfg
    from ai_trader.market.subscription import SubscriptionStore
    with SubscriptionStore(str(tmp_path / "s.db")) as st:
        st.seed(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        assert len(st.symbols()) == 3
    assert TradingCfg().symbol == "BTCUSDT"      # 交易品种仍是单一字段
