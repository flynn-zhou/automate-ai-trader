# -*- coding: utf-8 -*-
"""策略补历史的市场，必须和实时行情流的市场一致。

第 98 轮修过一次口径分歧，但只修了一半：
  seed_from_rest() 按 exec_market 选现货 K 线，
  而实时行情流（feed.py:74）写死 cfg.market.fapi_base = 永续合约。
于是策略的 SMA200 = 现货历史 + 合约实时，是个混合序列，
而 trend.py 的注释却声称「决策用的市场和下单用的市场保持一致」。

这组测试钉住真正的不变量：种子取数的主机 == 实时流取数的主机。

为什么不写成「源码里有 fapi 字样」那种断言：
  那种测试会匹配到我自己写的注释，变异时假通过——本项目已经栽过三次。
"""
import io
import json
import os
import sys
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.feed import Feed
from ai_trader.trading.trend import TrendStrategy


def _host(url):
    return urllib.parse.urlparse(url).netloc


def venue_of(host: str) -> str:
    """把主机归到市场：api.binance.com=现货, fapi/fstream=合约。"""
    h = host.lower()
    if h.startswith("fapi.") or h.startswith("fstream."):
        return "perp"
    if h.startswith("api.") or h.startswith("stream."):
        return "spot"
    return h


def feed_venues(cfg):
    """实时行情流的【两条】通路各用什么市场：REST 兜底 + WS 推送。"""
    f = Feed(cfg, store=object())
    return venue_of(_host(f.rest._base)), venue_of(_host(cfg.market.ws_url))


class _Captured:
    """拦下 urllib 真正发出的请求，记下 URL。"""

    def __init__(self, rows):
        self.urls = []
        self._rows = rows

    def build_opener(self, *a, **kw):
        cap = self

        class _Opener:
            def open(self, req, timeout=None):
                cap.urls.append(req.full_url if hasattr(req, "full_url") else str(req))

                class _R:
                    def __enter__(_s):
                        return _s

                    def __exit__(_s, *e):
                        return False

                    def read(_s):
                        return json.dumps(cap._rows).encode()
                return _R()
        return _Opener()


def _fake_rows(n, base_t=1700000000000, iv=14400000):
    rows = []
    for i in range(n):
        t = base_t + i * iv
        px = 30000.0 + i
        rows.append([t, str(px), str(px + 10), str(px - 10), str(px), "1.0"])
    return rows


@pytest.mark.parametrize("exec_market", ["spot", "perp", ""])
def test_seed_uses_same_venue_as_live_feed(exec_market, monkeypatch):
    """无论 exec_market 是什么，补历史都要和实时流同一个市场。"""
    cfg = AppCfg()
    cfg.trading.exec_market = exec_market
    rest_v, ws_v = feed_venues(cfg)

    import ai_trader.trading.trend as trend_mod
    cap = _Captured(_fake_rows(450))
    monkeypatch.setattr(trend_mod.urllib.request, "build_opener", cap.build_opener)

    t = TrendStrategy(cfg.trading, symbol="BTCUSDT",
                      fapi_base=cfg.market.fapi_base, proxy="",
                      exec_market=exec_market)
    n = t.seed_from_rest(need=300)
    assert n > 0, "补历史失败了，测试前提不成立"
    assert cap.urls, "没有发出任何请求"
    seed_v = venue_of(_host(cap.urls[0]))

    # 三条通路必须同源：种子 + REST 兜底 + WS 推送
    assert rest_v == ws_v, (
        u"实时流内部就不一致：REST 兜底走 %s，WS 走 %s —— "
        u"同一个 4h 桶会混进两个市场的成交" % (rest_v, ws_v))
    assert seed_v == rest_v, (
        u"补历史走的是 %s，而实时行情流走的是 %s —— "
        u"策略的 SMA200 会变成两个市场的混合序列" % (seed_v, rest_v))
