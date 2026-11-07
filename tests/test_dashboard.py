# -*- coding: utf-8 -*-
"""带登录的后台：总览 / 区间演化 / 仓位配置。

三条必须钉死的性质
------------------
一、没登录一律进不去。后台能改本金/风险/杠杆，裸奔一次就够出事。
二、密码未配置时**登不进去**，而不是默认放行。
   "默认口令"比"没有口令"更危险：它给人已经配好的错觉。
三、改参数必须落到活的 cfg 上（引擎读的是那个对象引用），否则页面上显示新值、
   实际下单还是旧参数——这种错不会有任何报错。
"""
from __future__ import annotations

import pytest


def _app(tmp_path, monkeypatch, pwd="s3cret", token=""):
    from ai_trader.config import AppCfg
    from ai_trader.range.store import RangeStore
    from ai_trader.settings import SettingsStore
    from ai_trader.web.app import create_app

    if pwd is None:
        monkeypatch.delenv("AI_WEB_PASS", raising=False)
    else:
        monkeypatch.setenv("AI_WEB_PASS", pwd)
    monkeypatch.setenv("AI_WEB_USER", "admin")
    monkeypatch.setenv("AI_SECRET_KEY", "test-secret")

    class _Hub:
        subscribers = 0

    class _Feed:
        hub = _Hub()
        def snapshot(self):
            return {"src": "WS", "symbol": "BTCUSDT", "price": 77700.0,
                    "ws_connected": True, "ws_silent": False, "ws_dropped": 0,
                    "ws_idle_ms": 1, "rest_latency_ms": 1, "uptime_s": 1,
                    "ticks_total": 1, "rest_ticks": 0, "ws_stream": "trade"}

    cfg = AppCfg()
    cfg.web.auth_token = token
    rdb = RangeStore(str(tmp_path / "r.db"))
    sst = SettingsStore(str(tmp_path / "s.db"))
    app = create_app(cfg, _Feed(), range_db=rdb, settings=sst)
    return app, cfg, sst


def _login(c, user="admin", pwd="s3cret"):
    return c.post("/login", data={"username": user, "password": pwd},
                  follow_redirects=False)


def test_admin_requires_login(tmp_path, monkeypatch):
    app, _cfg, _s = _app(tmp_path, monkeypatch)
    c = app.test_client()
    for path in ("/admin/", "/admin/range", "/admin/config"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 302, "%s 未登录竟然可直接访问" % path
        assert "/login" in r.headers["Location"]


def test_login_then_pages_render(tmp_path, monkeypatch):
    app, _cfg, _s = _app(tmp_path, monkeypatch)
    c = app.test_client()
    assert _login(c).status_code == 302
    for path, marker in (("/admin/", "总览"), ("/admin/range", "区间演化"),
                         ("/admin/config", "仓位配置")):
        r = c.get(path)
        assert r.status_code == 200, "%s -> %d" % (path, r.status_code)
        assert marker in r.get_data(as_text=True)


def test_wrong_password_rejected(tmp_path, monkeypatch):
    app, _cfg, _s = _app(tmp_path, monkeypatch)
    c = app.test_client()
    assert _login(c, pwd="wrong").status_code == 401
    assert c.get("/admin/", follow_redirects=False).status_code == 302


def test_disabled_when_no_password(tmp_path, monkeypatch):
    """没配密码时必须是"进不去"，不能是"随便进"，也不能是默认口令。"""
    app, _cfg, _s = _app(tmp_path, monkeypatch, pwd=None)
    c = app.test_client()
    assert c.get("/login").status_code == 503
    r = c.post("/login", data={"username": "admin", "password": ""})
    assert r.status_code == 503, "空密码竟然能登录"


def test_config_save_applies_to_live_cfg(tmp_path, monkeypatch):
    app, cfg, _s = _app(tmp_path, monkeypatch)
    c = app.test_client()
    _login(c)
    r = c.post("/admin/config", data={"capital": 250, "risk_pct": 2.5,
                                      "leverage": 18})
    assert r.status_code == 200
    assert cfg.trading.risk_pct == 2.5, "活的 cfg 没被改，页面会骗人"
    assert cfg.trading.max_leverage == 18
    assert cfg.trading.paper_balance == 250


def test_config_rejects_invalid_without_partial_apply(tmp_path, monkeypatch):
    app, cfg, _s = _app(tmp_path, monkeypatch)
    c = app.test_client()
    _login(c)
    c.post("/admin/config", data={"capital": 150, "risk_pct": 4, "leverage": 20})
    before = (cfg.trading.paper_balance, cfg.trading.risk_pct,
              cfg.trading.max_leverage)
    r = c.post("/admin/config", data={"capital": 999, "risk_pct": 4,
                                      "leverage": 250})
    assert r.status_code == 400
    after = (cfg.trading.paper_balance, cfg.trading.risk_pct,
             cfg.trading.max_leverage)
    assert before == after, "非法保存污染了活配置: %r -> %r" % (before, after)


def test_config_rejects_non_numeric(tmp_path, monkeypatch):
    app, _cfg, _s = _app(tmp_path, monkeypatch)
    c = app.test_client()
    _login(c)
    assert c.post("/admin/config",
                  data={"capital": "abc", "risk_pct": 5,
                        "leverage": 22}).status_code == 400


def test_open_redirect_blocked(tmp_path, monkeypatch):
    """?next= 只能是站内路径，否则登录页变成钓鱼跳板。"""
    app, _cfg, _s = _app(tmp_path, monkeypatch)
    c = app.test_client()
    r = c.post("/login?next=https://evil.example/x",
               data={"username": "admin", "password": "s3cret"},
               follow_redirects=False)
    assert r.status_code == 302
    assert "evil.example" not in r.headers["Location"]


def test_logout_clears_session(tmp_path, monkeypatch):
    app, _cfg, _s = _app(tmp_path, monkeypatch)
    c = app.test_client()
    _login(c)
    assert c.get("/admin/").status_code == 200
    c.get("/logout")
    assert c.get("/admin/", follow_redirects=False).status_code == 302


def test_public_pages_still_work(tmp_path, monkeypatch):
    """加了后台不能把运维正在用的公开页弄坏。"""
    app, _cfg, _s = _app(tmp_path, monkeypatch)
    c = app.test_client()
    for path in ("/", "/range", "/config"):
        assert c.get(path).status_code == 200, "公开页 %s 被弄坏了" % path
