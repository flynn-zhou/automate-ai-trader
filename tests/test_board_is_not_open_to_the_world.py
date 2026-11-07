# -*- coding: utf-8 -*-
"""复位按钮没有鉴权，而它能把所有风控闸一键清空（第 129 轮）。

要守的不变量
------------
1. 设了 AI_WEB_TOKEN 之后，所有 /api/* 都必须带令牌（不是只有三个只读的）。
2. 写操作（reset）没令牌必须 401，且【不能真的复位】—— 只看状态码不够。
3. 令牌不能出现在页面 HTML 里（那是能被 curl 走的公开资源）。
4. 没设令牌时不改变行为（本地开发、回环访问照旧）。

缺陷的形状
----------
`_check_token()` 存在，也写对了，但只挂在三个端点上：

    /api/bars      ✓ 带令牌检查
    /api/events    ✓
    /api/snapshot  ✓
    /api/guard/reset      ✗ 完全没有
    /api/trade/state      ✗
    /api/journal          ✗
    /api/health           ✗

而 `reset()` 清的是 halted_hard / halt_kind / halt_ts / temp_reasons /
daily_halt_day —— 第 122/123/127 轮做的那些闸，一个匿名 POST 就能全开。

所以【哪怕】把 AI_WEB_TOKEN 填上，那个按钮仍然不需要令牌。
这是这一族缺陷的老形状：**防护写好了，但没接到所有入口上**
（和第 124 轮 _close 只包了一半 try、第 125 轮哨兵只读一个表同类）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.web.app import create_app

TOKEN = "s3cret-tok"


class _Hub:
    subscribers = 0


class _Feed:
    hub = _Hub()

    def snapshot(self):
        return {"type": "state", "symbol": "BTCUSDT", "src": "WS",
                "price": 78000.0, "price_ts": 0, "ws_connected": True,
                "ws_stream": "trade", "ws_silent": False, "ws_dropped": 0,
                "ws_idle_ms": 10, "ws_reconnects": 0, "ws_ticks": 1,
                "rest_latency_ms": 0, "rest_fails": 0, "ticks_total": 10,
                "rest_ticks": 0, "trades_per_sec": 1.0, "uptime_s": 60,
                "bars": {}, "server_ts": 0}


class _Guard:
    """只看「reset 有没有被真的调用」。"""

    def __init__(self):
        self.reset_calls = 0

    def reset(self, reason=""):
        self.reset_calls += 1

    def can_open(self, now_ms=None):
        return True, ""

    def snapshot(self):
        return {"halted_hard": False}


class _Engine:
    def state(self):
        return {"equity": 100.0}


class _TDb:
    def stats(self, strategy):
        return {}

    def recent_decisions(self, n):
        return []

    def closed_trades(self, n):
        return []

    def equity_curve(self, n):
        return []


class _Journal:
    def recent(self, n):
        return []


def _app(token=TOKEN):
    cfg = AppCfg()
    cfg.web.auth_token = token
    return create_app(cfg, _Feed(), engine=_Engine(), tdb=_TDb(),
                      journal=_Journal(), guard=_Guard())


def _with_token(token=TOKEN):
    cfg = AppCfg()
    cfg.web.auth_token = token
    g = _Guard()
    app = create_app(cfg, _Feed(), engine=_Engine(), tdb=_TDb(),
                     journal=_Journal(), guard=g)
    return app, g


#: 所有端点都必须受令牌保护（除了拿令牌用的那个）。
PROTECTED = [
    ("get", "/api/health", None),
    ("get", "/api/bars?interval=3m", None),
    ("get", "/api/events", None),
    ("get", "/api/snapshot", None),
    ("get", "/api/intervals", None),
    ("get", "/api/trade/state", None),
    ("get", "/api/trade/decisions", None),
    ("get", "/api/trade/trades", None),
    ("get", "/api/trade/equity", None),
    ("get", "/api/guard", None),
    ("get", "/api/journal", None),
    ("post", "/api/guard/reset", None),
]


def test_every_endpoint_refuses_a_request_without_the_token():
    """★ 核心：不是只有三个只读端点，是【全部】。"""
    app = _app()
    c = app.test_client()
    leaked = []
    for method, path, _ in PROTECTED:
        r = getattr(c, method)(path)
        if r.status_code != 401:
            leaked.append((method.upper(), path, r.status_code))
    assert not leaked, (
        u"这些端点没带令牌也能访问：%r\n"
        u"_check_token() 存在，但没挂到所有入口上——"
        u"复位按钮一个匿名 POST 就能清空全部风控闸。" % (leaked,))


def test_the_reset_button_does_not_actually_reset_without_a_token():
    """★ 只看状态码不够：401 了但守卫已经被复位过，一样是漏。"""
    app, g = _with_token()
    c = app.test_client()
    c.post("/api/guard/reset")
    assert g.reset_calls == 0, (
        u"没带令牌的 POST /api/guard/reset 真的执行了 reset()（%d 次）"
        % (g.reset_calls,))
    # 带令牌必须能复位（否则闸就废了）
    r = c.post("/api/guard/reset", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200
    assert g.reset_calls == 1, u"带着令牌也复位不了"


def test_the_token_is_not_shipped_inside_the_page():
    """★ 令牌不能出现在 / 的 HTML 里。

    页面是能被 curl 走的公开资源；令牌写进去就等于没有令牌。
    前端要从别处拿（地址栏 ?token= 或用户输入），不能靠服务端塞。
    """
    app = _app()
    c = app.test_client()
    r = c.get("/")
    if r.status_code == 200:
        body = r.get_data(as_text=True)
        assert TOKEN not in body, (
            u"页面 HTML 里直接印出了访问令牌——它会被未鉴权的 curl 拿走")


def test_no_token_configured_keeps_the_old_behaviour():
    """★ 反向：没设令牌时不能把本地开发也锁死。"""
    app, g = _with_token(token="")
    c = app.test_client()
    for method, path, _ in PROTECTED:
        r = getattr(c, method)(path)
        assert r.status_code != 401, (
            u"没设令牌时 %s %s 被挡了（%d）——本地开发和回环访问会被锁死"
            % (method.upper(), path, r.status_code))
    assert g.reset_calls == 1, u"没设令牌时复位按钮应当照常工作"


def test_a_query_token_works_too():
    """★ 前端在地址栏带 ?token=... 也要能用（否则页面无从携带）。"""
    app = _app()
    c = app.test_client()
    r = c.get("/api/trade/state?token=" + TOKEN)
    assert r.status_code == 200, (
        u"地址栏带令牌也访问不了，前端就没有可行的携带方式")
