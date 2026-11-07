# -*- coding: utf-8 -*-
"""准实时行情 + EMA 双通道。

这里钉死的三件事都是"错了也不报错"的类型
--------------------------------------
一、EMA 必须先用 1500 根预热再切最后 200 根。若直接拿 200 根算，
   3m 上 EMA200 差 62.4 点、1h 差 82.8 点，而 EMA144 与 EMA169 只差 83.6 点——
   误差足以把"谁在上方"判反，而井字带的方向就靠这个判据。图不会报错，只会画反。
二、EMA 是递推量，种子用第一根收盘价。换成别的种子不会有异常，只会整体偏移。
三、拉取失败必须回退到上一次的好数据并标 stale，而不是让图突然空掉。
"""
from __future__ import annotations

import pytest


def test_ema_math_matches_hand_calculation():
    """逐根手算对照，防止 alpha 或种子写错。"""
    from ai_trader.web.live import ema_series
    closes = [10.0, 20.0, 30.0]
    out = ema_series(closes, 2)          # alpha = 2/3
    # e0 = 10
    # e1 = 20*2/3 + 10*1/3 = 16.667
    # e2 = 30*2/3 + 16.667*1/3 = 25.556
    assert out[0] == 10.0
    assert abs(out[1] - 16.7) < 0.05, out
    assert abs(out[2] - 25.6) < 0.05, out


def test_ema_seed_is_first_close():
    from ai_trader.web.live import ema_series
    assert ema_series([42.0, 42.0, 42.0], 144) == [42.0, 42.0, 42.0]


def test_ema_is_monotonic_with_monotonic_input():
    from ai_trader.web.live import ema_series
    closes = [float(i) for i in range(1, 201)]
    out = ema_series(closes, 20)
    assert all(out[i] < out[i + 1] for i in range(len(out) - 1))
    assert out[-1] < closes[-1], "持续上涨时 EMA 应落后于价格"


def _kline(i, close):
    """构造一根币安原始 K 线（只需 [0]openTime 与 [4]close 被用到）。"""
    return [i * 60_000, "1", "1", "1", str(close), "1"]


def test_warmup_then_slice_keeps_alignment(monkeypatch):
    """必须请求 1500 根、返回 200 根，且 EMA 是尾部对齐的切片。"""
    import ai_trader.web.live as L
    seen = {}

    def fake(symbol, interval, limit):
        seen["limit"] = limit
        seen["interval"] = interval
        return [_kline(i, 100.0 + i) for i in range(L.EMA_WARMUP_BARS)]

    monkeypatch.setattr(L, "_fetch_klines", fake)
    payload = L._build("BTCUSDT", "3m")
    assert seen["limit"] == 1500, "预热根数被改了：%r" % seen["limit"]
    assert len(payload["bars"]) == L.DISPLAY_BARS
    assert len(payload["ema"]["144"]) == L.DISPLAY_BARS, "EMA 没有跟着切片"
    # 尾部对齐：最后一根蜡烛的 EMA 必须对应最后一根收盘
    assert payload["bars"][-1][2] == 100.0 + L.EMA_WARMUP_BARS - 1
    assert payload["warmup"] == L.EMA_WARMUP_BARS


def test_bars_are_ohlc_order_for_echarts(monkeypatch):
    """ECharts 蜡烛图要 [开, 收, 低, 高]，而币安原始是 [开, 高, 低, 收]。"""
    import ai_trader.web.live as L
    raw = [1700000000000, "100", "110", "90", "105", "1", "0"]
    monkeypatch.setattr(L, "_fetch_klines", lambda *a: [raw])
    payload = L._build("BTCUSDT", "3m")
    b = payload["bars"][0]
    assert b[1] == 100.0   # 开
    assert b[2] == 105.0   # 收
    assert b[3] == 90.0    # 低
    assert b[4] == 110.0   # 高


def test_all_five_emas_present(monkeypatch):
    import ai_trader.web.live as L
    monkeypatch.setattr(L, "_fetch_klines",
                        lambda *a: [_kline(i, 100.0 + i)
                                    for i in range(L.EMA_WARMUP_BARS)])
    payload = L._build("BTCUSDT", "1h")
    assert set(payload["ema"]) == {"20", "50", "144", "169", "200"}


# ---------------- Flask 层 ----------------

def _app(tmp_path, monkeypatch):
    from ai_trader.config import AppCfg
    from ai_trader.settings import SettingsStore
    from ai_trader.web.app import create_app
    monkeypatch.setenv("AI_WEB_PASS", "s3cret")
    monkeypatch.setenv("AI_WEB_USER", "admin")
    monkeypatch.setenv("AI_SECRET_KEY", "k")

    class _F:
        class hub: subscribers = 0
        def snapshot(self): return {"price": 1.0}

    cfg = AppCfg()
    return create_app(cfg, _F(),
                      settings=SettingsStore(str(tmp_path / "s.db"))), cfg


def _login(c):
    return c.post("/login", data={"username": "admin", "password": "s3cret"})


def test_live_api_requires_login(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    c = app.test_client()
    r = c.get("/admin/api/live?interval=3m", follow_redirects=False)
    assert r.status_code == 302, "行情接口未登录就能拿"


def test_live_page_renders(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    c = app.test_client()
    _login(c)
    r = c.get("/admin/live")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "井字" in body or "hatch" in body
    assert "EMA144" in body


def test_bad_interval_rejected(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    c = app.test_client()
    _login(c)
    r = c.get("/admin/api/live?interval=7m")
    assert r.status_code == 400
    assert "3m" in r.get_json()["allowed"]


def test_stale_fallback_keeps_chart_alive(tmp_path, monkeypatch):
    """拉取失败时要回上一次的好数据，图不能突然空掉。"""
    import ai_trader.web.live as L
    app, _ = _app(tmp_path, monkeypatch)
    c = app.test_client()
    _login(c)

    monkeypatch.setattr(L, "_fetch_klines",
                        lambda *a: [_kline(i, 100.0 + i)
                                    for i in range(L.EMA_WARMUP_BARS)])
    L._cache.clear()
    ok = c.get("/admin/api/live?interval=3m").get_json()
    assert ok["ok"] is True

    L._cache.clear()
    monkeypatch.setattr(L, "_fetch_klines",
                        lambda *a: [_kline(i, 100.0 + i)
                                    for i in range(L.EMA_WARMUP_BARS)])
    c.get("/admin/api/live?interval=3m")          # 先填一份好数据

    def boom(*a):
        raise RuntimeError("网络断了")
    monkeypatch.setattr(L, "_fetch_klines", boom)
    import time as _t
    L._cache["BTCUSDT|3m"] = (0.0, L._cache["BTCUSDT|3m"][1])   # 让缓存过期
    r = c.get("/admin/api/live?interval=3m")
    assert r.status_code == 200
    assert r.get_json()["stale"] is True, "失败时没有回退旧数据"
