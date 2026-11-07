# -*- coding: utf-8 -*-
"""凭证存在但无效时，系统会怎样？

这是用户注资之后最可能出错的地方，而且没有任何真实演练过：
  - key 少复制一位
  - key 有读权限但没有交易权限
  - IP 白名单没配
  - 现货与合约的 key 权限搞混

这些情况下凭证【存在】（所以 main.py 的启动校验会放行），但所有签名请求都会失败。

现在的行为链路：
  main.py 校验凭证非空 -> 放行
  reconcile_live() -> position() 抛异常 -> catch 住、日志 critical、return
  main.py 继续往下走 -> 系统"启动成功"
  spot.py 的 auth breaker 记下 _auth_dead，之后停止重试
  -> 每轮 _step 里 equity() 一律失败 -> _equity_stale=True -> 拒绝入场
  -> 系统看起来在跑，看板有行情有决策，但永远不会交易

这和前面几个缺陷同一类：安静。日志里只有几行 critical，没有 ERROR 循环，
哨兵如果只看"进程活着"就会报 OK。

我要验证的是：启动流程会不会【响亮地拒绝】。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from aiohttp import web

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor


class _Agg:
    def __init__(self, px):
        from ai_trader.market.agg import Bar
        self._b = Bar(interval_ms=3000, open_time=0, open=px, high=px,
                      low=px, close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._b if iv == 3000 else None


class _Feed:
    def __init__(self, px=30000.0):
        self.last_price = px
        self.cfg = AppCfg()
        self.src = "ws"
        self.agg = _Agg(px)
        self.bar_hooks = []


@pytest.fixture()
def bad_auth_server():
    """一个【所有签名请求都拒绝】的假交易所。"""
    state = {"calls": 0}

    async def handle(request):
        state["calls"] += 1
        return web.json_response(
            {"code": -2015,
             "msg": "Invalid API-key, IP, or permissions for action."},
            status=401)

    async def exchange_info(request):
        return web.json_response({"symbols": [{
            "baseAsset": "BTC", "quoteAsset": "USDT", "baseAssetPrecision": 8,
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00001",
                 "minQty": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "5.0"}]}]})

    async def ticker(request):
        return web.json_response({"price": "30000.0"})

    app = web.Application()
    app.router.add_get("/api/v3/exchangeInfo", exchange_info)
    app.router.add_get("/api/v3/ticker/price", ticker)
    app.router.add_get("/api/v3/account", handle)
    app.router.add_post("/api/v3/order", handle)
    app.router.add_get("/api/v3/myTrades", handle)
    return app, state


@pytest.mark.asyncio
async def test_invalid_credentials_are_detected_at_startup(bad_auth_server):
    """凭证无效必须在启动时就被发现，而不是安静地跑到"看起来正常"。"""
    app, state = bad_auth_server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = None
    for s in runner.addresses:
        port = s[1]
        break
    base_url = "http://127.0.0.1:%d" % port

    try:
        cfg = AppCfg()
        cfg.trading.symbol = "BTCUSDT"
        cfg.trading.exec_market = "spot"
        ex = SpotExecutor(_Feed(), cfg, "badkey", "badsecret", base=base_url)

        # 这是关键：能不能拿到一个明确的"凭证不可用"结论？
        err = None
        try:
            await ex.equity()
        except Exception as e:
            err = e

        assert err is not None, (
            u"equity() 在凭证无效时居然成功了——那意味着它返回了假数据，",
            u"比抛异常更危险")

        # 熔断器必须生效，否则每 3 秒一次 401，一天 28800 次
        assert state["calls"] >= 1, "至少要真的发过一次请求"
        calls_after_first = state["calls"]
        for _ in range(5):
            try:
                await ex.equity()
            except Exception:
                pass
        assert state["calls"] == calls_after_first, (
            u"熔断器没生效：又发了 %d 次无效请求。"
            u"一天 28800 次 401 会被交易所限频，而且日志会被刷满。"
            % (state["calls"] - calls_after_first))

        assert ex.halted or getattr(ex, "_auth_dead", ""), (
            u"凭证无效却没有标记 halted/_auth_dead，",
            u"上层无法据此做出「拒绝启动」的判断")

        await ex.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_startup_with_bad_credentials_reports_unhealthy(bad_auth_server):
    """启动流程必须能对外报告「我无法交易」，而不是假装健康。

    用户注资后我把 AI_TRADE_MODE 切成 live，如果 key 有问题，
    最坏的结果不是报错退出，而是：进程活着、看板有行情、决策在写，
    但每一笔下单都失败，系统永远不交易。

    那时用户看到的是「系统在运行」，我看日志也只能看到几行 critical。
    """
    from ai_trader.trade_db import TradeDB
    from ai_trader.trading.engine import DecisionEngine
    import tempfile

    app, state = bad_auth_server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = None
    for s in runner.addresses:
        port = s[1]
        break
    base_url = "http://127.0.0.1:%d" % port

    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    try:
        cfg = AppCfg()
        cfg.trading.symbol = "BTCUSDT"
        cfg.trading.exec_market = "spot"
        cfg.trading.strategy = "trend"
        feed = _Feed()
        ex = SpotExecutor(feed, cfg, "badkey", "badsecret", base=base_url)
        eng = DecisionEngine(cfg, feed, ex, tdb, guard=None)

        # 模拟 main.py 的启动顺序
        await eng.reconcile_live()

        # 走一轮决策，看看引擎给出什么结论
        snap = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
        await eng._step(snap)

        st = eng.state()

        # 关键：有没有一个明确的、机器可读的"我不能交易"信号？
        healthy = True
        reasons = []

        if not st.get("pos") and st.get("equity") in (0, None, 0.0):
            healthy = False
        if getattr(eng, "_equity_stale", False):
            reasons.append("equity_stale")
        if getattr(eng, "last_error", ""):
            reasons.append(eng.last_error)
        if getattr(ex, "halted", ""):
            reasons.append("halted: " + ex.halted[:80])
        if getattr(ex, "_auth_dead", ""):
            reasons.append("auth_dead")

        assert not healthy or reasons, (
            u"凭证无效，但引擎状态里【没有任何一项】能表明它无法交易。"
            u"这意味着哨兵、看板、健康检查都会认为系统正常。")

        # 至少要能看到 halted 或 auth_dead 之一，
        # 因为那是唯一能明确告诉上层"别指望它下单"的标志。
        assert (getattr(ex, "halted", "") or getattr(ex, "_auth_dead", "")), (
            u"执行器没有把「凭证不可用」这个事实暴露出来。",
            u"期望 halted 或 _auth_dead 至少有一个非空。")

        await ex.close()
    finally:
        tdb.stop()
        await runner.cleanup()
