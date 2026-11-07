# -*- coding: utf-8 -*-
"""运行时仓位参数 + Web 接口。

为什么这些测试重要
------------------
一、参数能实时改，靠的是「引擎持有 cfg.trading 的对象引用」。
    这是个隐式契约：哪天引擎改成启动时拷一份数值，实时性会静默失效，
    页面上看着改了、实际买卖还是旧参数——这种错没有任何报错。
二、非法的 POST 必须一个字段都不落库。
    「本金改了、杠杆没改」这种半截状态会让用户以为自己在用 A 组合，
    实际是 B 组合，而两者风险差好几倍。
"""
from __future__ import annotations

import pytest


# ------------------------------------------------------------------ settings
def test_defaults_are_100_5_22(tmp_path):
    from ai_trader.settings import SettingsStore, K_CAPITAL, K_RISK, K_LEVERAGE

    with SettingsStore(str(tmp_path / "s.db")) as st:
        s = st.sizing()
    assert s[K_CAPITAL] == 100.0
    assert s[K_RISK] == 5.0
    assert s[K_LEVERAGE] == 22.0


def test_first_run_seeds_from_env_not_hardcoded_defaults(tmp_path):
    """首次启动必须拿 env 的值播种，不能用硬编码 100/5/22。

    这是「数据库唯一权威 + env 只是初始值」之间最容易漏的一条缝：
    如果空库时直接回落 DEFAULTS，用户在 env 里写的 300U 会被静默忽略，
    系统拿 100U 去算仓位，而页面上还显示 100——用户以为自己配的是 300。
    """
    from ai_trader.config import TradingCfg
    from ai_trader.settings import SettingsStore, K_CAPITAL, K_RISK, K_LEVERAGE

    env_cfg = TradingCfg()          # 模拟从 env/yaml 解析出来的配置
    env_cfg.paper_balance = 300.0
    env_cfg.risk_pct = 2.0
    env_cfg.max_leverage = 15.0

    with SettingsStore(str(tmp_path / "s.db")) as st:
        assert st.is_empty()
        assert st.seed_from(env_cfg) is True, "空库时应当播种"
        s = st.sizing()
        assert s[K_CAPITAL] == 300.0, "本金没有从 env 播种，拿到 %r" % s[K_CAPITAL]
        assert s[K_RISK] == 2.0
        assert s[K_LEVERAGE] == 15.0


def test_seed_does_not_overwrite_existing_values(tmp_path):
    """库非空时不得再播种——否则每次重启都会把页面上的改动冲回 env 值。"""
    from ai_trader.config import TradingCfg
    from ai_trader.settings import SettingsStore, K_CAPITAL

    env_cfg = TradingCfg()
    env_cfg.paper_balance = 300.0
    with SettingsStore(str(tmp_path / "s.db")) as st:
        assert st.set_sizing(123.0, 4.0, 20.0)[0]
        assert st.seed_from(env_cfg) is False, "库非空时不该播种"
        assert st.sizing()[K_CAPITAL] == 123.0, "页面设的值被 env 冲掉了"


def test_database_is_authority_after_worden(tmp_path):
    """播种之后 env 再变也不影响运行中的系统——数据库才是权威。"""
    from ai_trader.config import TradingCfg
    from ai_trader.settings import SettingsStore

    p = str(tmp_path / "s.db")
    first = TradingCfg()
    first.paper_balance = 300.0
    with SettingsStore(p) as st:
        st.seed_from(first)
    # 运维直接改 env 后重启
    second = TradingCfg()
    second.paper_balance = 999.0
    with SettingsStore(p) as st:
        assert st.seed_from(second) is False
        st.apply_to(second)
    assert second.paper_balance == 300.0, "env 覆盖了数据库，权威关系反了"


def test_saved_values_persist_across_instances(tmp_path):
    from ai_trader.settings import SettingsStore, K_CAPITAL

    p = str(tmp_path / "s.db")
    with SettingsStore(p) as st:
        ok, why = st.set_sizing(200.0, 3.0, 15.0)
        assert ok, why
    with SettingsStore(p) as st:
        assert st.sizing()[K_CAPITAL] == 200.0


@pytest.mark.parametrize("capital,risk,lev", [
    (100.0, 0.0, 22.0),        # 风险 0
    (100.0, -5.0, 22.0),       # 负风险
    (100.0, 101.0, 22.0),      # 风险 > 100
    (100.0, 5.0, 0.0),         # 杠杆 0
    (100.0, 5.0, 250.0),       # 1/250 - 0.5% < 0，爆仓距离为负
    (0.0, 5.0, 22.0),          # 本金 0
])
def test_invalid_parameters_are_rejected(tmp_path, capital, risk, lev):
    from ai_trader.settings import SettingsStore

    with SettingsStore(str(tmp_path / "s.db")) as st:
        ok, why = st.set_sizing(capital, risk, lev)
        assert not ok and why, "应拒绝 %r 但放行了" % ((capital, risk, lev),)


def test_rejected_value_does_not_change_anything(tmp_path):
    """非法保存必须整体失败，不能留下半截状态。"""
    from ai_trader.settings import SettingsStore, K_CAPITAL, K_RISK, K_LEVERAGE

    with SettingsStore(str(tmp_path / "s.db")) as st:
        assert st.set_sizing(150.0, 4.0, 20.0)[0]
        before = st.sizing()
        ok, _ = st.set_sizing(999.0, 4.0, 250.0)      # 杠杆非法
        assert not ok
        after = st.sizing()
    assert before == after, "非法保存污染了已有参数: %r -> %r" % (before, after)


def test_apply_to_mutates_the_live_object(tmp_path):
    """apply_to 必须就地改字段——引擎持有同一个对象引用，才谈得上实时生效。"""
    from ai_trader.config import TradingCfg
    from ai_trader.settings import SettingsStore

    tcfg = TradingCfg()                            # 引擎里叫 self.tcfg
    holder = type("Engine", (), {})()
    holder.tcfg = tcfg                             # 模拟引擎持有的那个引用
    with SettingsStore(str(tmp_path / "s.db")) as st:
        assert st.set_sizing(300.0, 2.0, 18.0)[0]
        st.apply_to(tcfg)
    assert holder.tcfg.risk_pct == 2.0, "引擎那侧的引用没有看到新参数"
    assert holder.tcfg.max_leverage == 18.0
    assert holder.tcfg.paper_balance == 300.0


def test_preview_returns_four_rows(tmp_path):
    from ai_trader.settings import SettingsStore

    with SettingsStore(str(tmp_path / "s.db")) as st:
        p = st.preview(100.0, 5.0, 22.0)
    assert p["ok"]
    assert len(p["rows"]) == 4, "四行派生应当正好 4 行，实际 %d" % len(p["rows"])
    # 结构契约：as_rows() 给的是 {label, value} 字典。
    # 前端曾按 [name, value] 数组取，导致页面上四行全是 undefined——
    # 这里把形状钉死，改形状必须同步改前端。
    for row in p["rows"]:
        assert set(row) == {"label", "value"}, "四行结构不对: %r" % (row,)
    assert p["risk_per_trade"] == 5.0
    assert abs(p["liq_distance_pct"] - 4.045) < 1e-2
    assert abs(p["max_stop_distance_pct"] - 1.618) < 1e-2
    assert p["lives"] == 20


def test_preview_rejects_invalid(tmp_path):
    from ai_trader.settings import SettingsStore

    with SettingsStore(str(tmp_path / "s.db")) as st:
        p = st.preview(100.0, 5.0, 250.0)
    assert not p["ok"] and p["error"]


# ------------------------------------------------------------------ web
class _Hub:
    subscribers = 0


class _FakeFeed:
    hub = _Hub()

    def snapshot(self):
        return {"type": "state", "symbol": "BTCUSDT", "src": "WS", "price": 77700.0,
                "price_ts": 0, "ws_connected": True, "ws_stream": "trade",
                "ws_silent": False, "ws_dropped": 0, "ws_idle_ms": 1,
                "ws_reconnects": 0, "ws_ticks": 1, "rest_latency_ms": 0,
                "rest_fails": 0, "ticks_total": 1, "rest_ticks": 0,
                "trades_per_sec": 0.0, "uptime_s": 1, "bars": {}, "server_ts": 0}


def _app(tmp_path, token=""):
    from ai_trader.config import AppCfg
    from ai_trader.range.store import RangeStore
    from ai_trader.settings import SettingsStore
    from ai_trader.web.app import create_app

    cfg = AppCfg()
    cfg.web.auth_token = token
    rdb = RangeStore(str(tmp_path / "range.db"))
    sst = SettingsStore(str(tmp_path / "set.db"))
    app = create_app(cfg, _FakeFeed(), range_db=rdb, settings=sst)
    app.config["_cfgtrading"] = cfg.trading
    return app, cfg, rdb, sst


def _seed_range(rdb):
    rows = [
        (1760000000000, "2026-10-09 16:00", 81000.0, 79000.0, 82000.0, None,
         78500.0, 1, 1, "DOWN", "birth"),
        (1760003600000, "2026-10-09 17:00", 81100.0, 79200.0, 82000.0, None,
         78400.0, 1, 2, "DOWN", "突破"),
    ]
    rdb.upsert_states("BTCUSDT", rows)
    rdb.upsert_bars_1h("BTCUSDT", [
        (1760000000000, "2026-10-09 16:00", 80000.0, 81200.0, 78900.0, 80500.0, 12.3),
        (1760003600000, "2026-10-09 17:00", 80500.0, 81300.0, 79100.0, 79600.0, 9.9),
    ])


def test_range_page_is_served_with_echarts(tmp_path):
    """页面必须真的能返回，且引用了本地图表库（服务器上不了外网，不能依赖 CDN）。"""
    app, _cfg, _r, _s = _app(tmp_path)
    r = app.test_client().get("/range")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "/static/vendor/echarts.min.js" in html, "没有引用本地 echarts"
    assert "cdn." not in html, "不能依赖 CDN"
    assert "/api/range/evo" in html and "/api/range/asof" in html


def test_config_page_is_served_with_three_inputs(tmp_path):
    app, _cfg, _r, _s = _app(tmp_path)
    r = app.test_client().get("/config")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for fid in ("capital", "risk_pct", "leverage"):
        assert 'id="%s"' % fid in html, "缺少输入框 %s" % fid
    assert "/api/config/sizing" in html


def test_config_page_reads_label_value_not_tuple(tmp_path):
    """as_rows() 给的是 {label, value}；前端曾按 r[0]/r[1] 取，四行全变 undefined。

    这条断言直接盯源码里的取法——因为这类错误不会让任何后端测试变红，
    只有人打开页面才看得见。
    """
    app, _cfg, _r, _s = _app(tmp_path)
    html = app.test_client().get("/config").get_data(as_text=True)
    assert "r.label" in html and "r.value" in html
    assert "r[0]" not in html and "r[1]" not in html, (
        "页面又按数组下标取四行了，as_rows() 返回的是字典")


def test_config_get_returns_defaults_and_four_rows(tmp_path):
    app, _cfg, _r, _s = _app(tmp_path)
    d = app.test_client().get("/api/config/sizing").get_json()
    assert d["ok"]
    assert d["sizing"]["sizing.risk_pct"] == 5.0
    assert len(d["derived"]["rows"]) == 4


def test_config_post_applies_immediately(tmp_path):
    app, cfg, _r, _s = _app(tmp_path)
    d = app.test_client().post("/api/config/sizing",
                               json={"capital": 250, "risk_pct": 2.5,
                                     "leverage": 18}).get_json()
    assert d["ok"], d
    assert d["live"]["risk_pct"] == 2.5
    assert d["live"]["max_leverage"] == 18
    assert cfg.trading.risk_pct == 2.5, "活的 cfg 没被改，说明没有实时生效"
    assert cfg.trading.max_leverage == 18


def test_config_post_invalid_leaves_everything_untouched(tmp_path):
    app, cfg, _r, _s = _app(tmp_path)
    c = app.test_client()
    c.post("/api/config/sizing", json={"capital": 150, "risk_pct": 4, "leverage": 20})
    before = (cfg.trading.paper_balance, cfg.trading.risk_pct, cfg.trading.max_leverage)
    resp = c.post("/api/config/sizing",
                  json={"capital": 999, "risk_pct": 4, "leverage": 250})
    assert resp.status_code == 400
    after = (cfg.trading.paper_balance, cfg.trading.risk_pct, cfg.trading.max_leverage)
    assert before == after, "非法保存改动了活配置: %r -> %r" % (before, after)


def test_config_post_rejects_non_numeric(tmp_path):
    app, _cfg, _r, _s = _app(tmp_path)
    resp = app.test_client().post("/api/config/sizing",
                                  json={"capital": "abc", "risk_pct": 5,
                                        "leverage": 22})
    assert resp.status_code == 400


def test_range_asof_returns_seeded_state(tmp_path):
    app, _cfg, rdb, _s = _app(tmp_path)
    _seed_range(rdb)
    d = app.test_client().get("/api/range/asof?when=2026-10-09 17:30").get_json()
    assert d["ok"], d
    assert d["state"]["family"] == 1
    assert d["state"]["segment"] == 2, "17:30 应取到 17:00 那根（segment=2）"
    assert d["state"]["bar"]["close"] == 79600.0


def test_range_asof_before_data_returns_404(tmp_path):
    app, _cfg, rdb, _s = _app(tmp_path)
    _seed_range(rdb)
    resp = app.test_client().get("/api/range/asof?when=2020-01-01 00:00")
    assert resp.status_code == 404


def test_range_asof_bad_time_returns_400(tmp_path):
    app, _cfg, rdb, _s = _app(tmp_path)
    _seed_range(rdb)
    resp = app.test_client().get("/api/range/asof?when=not-a-time")
    assert resp.status_code == 400


def test_api_requires_token_when_configured(tmp_path):
    app, _cfg, rdb, _s = _app(tmp_path, token="secret123")
    _seed_range(rdb)
    c = app.test_client()
    assert c.get("/api/range/asof?when=2026-10-09 17:00").status_code == 401
    assert c.get("/api/range/asof?when=2026-10-09 17:00&token=secret123").status_code == 200
    assert c.get("/api/config/sizing").status_code == 401
    assert c.get("/api/config/sizing?token=secret123").status_code == 200
