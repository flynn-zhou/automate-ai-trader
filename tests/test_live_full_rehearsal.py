# -*- coding: utf-8 -*-
"""实盘完整生命周期排练：真实引擎 + 真实 HTTP + 真签名 + 严格假币安。

为什么还要写这个：项目里已有两类 e2e，各缺一半。

  test_spot_round_trip.py  : 真实 HTTP，但只到执行器层，没有引擎
  test_engine_spot_e2e.py  : 到引擎层，但用的是进程内假交易所，
                             它【不收手续费、不校验最小名义、不校验步长】

后者尤其危险：它第 158 行断言"平仓后 btc == 0"，而真实币安买入时
手续费扣在 BTC 上，平仓按 step 向下取整，必然留下粉尘。
换句话说，那条断言在真实环境下会失败——它验证的是一个不存在的世界，
同时也正好把粉尘缺陷藏在绿灯底下。

这里把两边合起来：引擎 -> 执行器 -> 真实 HTTP -> 真 HMAC 签名 ->
严格假币安（会拒步长、拒最小名义、拒余额不足、按币种扣手续费）。
这是不用真钱能做到的最接近实盘的验证。

排练的剧本就是真实运行的剧本：
  1. 启动对账（空仓）
  2. 预热 4h K 线
  3. 价格上穿 SMA -> 开多（走引擎 _step_trend）
  4. 持仓中：无新信号、不开新仓
  5. 价格下穿 SMA -> 平仓（走引擎 -> 执行器 -> HTTP）
  6. 平仓后对账：粉尘必须被当作空仓，否则引擎会接管一个卖不掉的仓
  7. 账户金额必须自洽（买入+卖出两次手续费后略有减少，但不多）
"""
import os
import sys
import tempfile

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor
from ai_trader.market.agg import Bar
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy
from tools.fake_binance import FakeBinance

IV4H = 14400000
IV3S = 3000
PX = 30000.0


class _Hub:
    def publish(self, m):
        pass


class _Agg:
    def __init__(self, px):
        self._b = Bar(interval_ms=IV3S, open_time=0, open=px, high=px,
                      low=px, close=px, volume=1.0, closed=True)

    def current(self, iv):
        return self._b if iv == IV3S else None


class _Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self.agg = _Agg(px)
        self.src = "ws"


def _bars4h(prices):
    return [{"t": i * IV4H, "o": p, "h": p, "l": p, "c": p, "v": 1.0}
            for i, p in enumerate(prices)]


def _snap(px):
    b = Bar(interval_ms=IV4H, open_time=0, open=px, high=px, low=px,
            close=px, volume=1.0, closed=True)
    b.t = 0
    d = {iv: [] for iv in (3000, 15000, 60000, 180000, 900000, 14400000)}
    return d


@pytest_asyncio.fixture
async def env():
    fake = await FakeBinance(usdt=100.0, btc=0.0, price=PX).start()
    tmp = tempfile.mkdtemp()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    cfg.trading.sma_lookback = 5
    cfg.trading.trend_stop_pct = 25.0
    cfg.trading.trend_min_notional = 5.0
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = _Feed(PX)
    ex = SpotExecutor(feed, cfg, fake.api_key, fake.api_secret, base=fake.base())
    eng = DecisionEngine(cfg, feed, ex, tdb)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    try:
        yield eng, ex, fake, tdb, feed, cfg
    finally:
        tdb.stop()
        # 执行器持有 aiohttp session，不关的话 pytest 会因为
        # "Unclosed client session" 报错——而那个错误会盖住真正的断言失败，
        # 让人分不清"测试失败了"还是"清理没做好"。
        try:
            await ex.close()
        except Exception:
            pass
        await fake.stop()


@pytest.mark.asyncio
async def test_full_live_rehearsal(env):
    """走完整剧本：对账 -> 开多 -> 持仓 -> 平仓 -> 再对账。"""
    eng, ex, fake, tdb, feed, cfg = env

    # ---------- 1. 启动对账：空仓 ----------
    await eng.reconcile_live()
    assert eng.pos is None, "账户里没有币，引擎不该认为自己有仓"

    # ---------- 2. 预热：前 8 根在均线下方，最后一根拉起 ----------
    eng.trend.bars = _bars4h([100.0, 99, 98, 97, 96, 95, 94, 93, 120.0])
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    eng.feed.last_price = PX
    eng._last_equity = 100.0

    sig = eng.trend.want_entry(PX)
    assert sig is not None, "价格站上均线，必须有进场信号"

    # ---------- 3. 开多：走引擎真实的 _open 路径 ----------
    plan = eng._trend_plan(PX, 100.0, sig)
    assert plan.ok, plan.reason
    await eng._open(sig, plan, PX, 100.0, 1793520000000, {}, _snap(PX))

    assert eng.pos is not None, "下单成功后引擎必须记录持仓"
    pos = eng.pos
    assert pos["side"] == "long"
    assert pos["qty"] > 0

    # 交易所那边也必须有币（且比下单量少一点，因为手续费扣在 BTC 上）
    acct = await ex.equity()
    assert acct["position_qty"] > 0, "交易所必须真的持有多头"
    assert acct["position_qty"] <= pos["qty"] + 1e-12, (
        "记录持仓 %.8f 超过了实际余额 %.8f——平仓会因余额不足被拒"
        % (pos["qty"], acct["position_qty"]))

    # 手续费确实收走了：权益略低于 100
    assert acct["equity"] < 100.0, "买入必须收了手续费，否则假交易所太宽容"
    assert acct["equity"] > 99.5, "0.1%% 手续费不该扣这么多"

    # ---------- 4. 持仓中：不开新仓 ----------
    #
    # 这里测的是【引擎】而不是 want_entry。
    # want_entry 是有意无状态的：它只看上一根在均线下、这一根在均线上，
    # 同一根 4h K 线里被调一万次都会返回信号（它不知道仓位的存在）。
    # 防重复开仓是引擎在 _step_trend 里用 if self.pos: 分支拦下来的。
    # 断言 want_entry 返回 None 是测错了层——第一版就是这么写错的。
    before_orders = len(fake.orders)
    await eng._step(_snap(PX))
    assert len(fake.orders) == before_orders, (
        "持仓中引擎又下了一张单，订单数 %d -> %d"
        % (before_orders, len(fake.orders)))
    assert eng.pos is not None, "持仓不该被意外清掉"

    # ---------- 5. 平仓：价格下穿均线 ----------
    # state() 用最后一根已收盘 4h 的收盘价，所以要造一根跌到均线下方的
    eng.trend.bars = _bars4h([120.0] * 6 + [90.0])
    ok = await eng._close("测试平仓", PX * 0.9, PX * 0.9, _snap(PX * 0.9))
    assert ok, "平仓必须成功"
    assert eng.pos is None, "平仓后引擎必须清空持仓"

    # ---------- 6. 平仓后对账：粉尘必须当空仓 ----------
    sold = [o for o in fake.orders if o["side"] == "SELL"]
    assert sold, "必须真的发出了卖单"

    acct2 = await ex.equity()
    residual = acct2["position_qty"]
    step = 0.00001
    assert 0 <= residual < step, (
        "平仓后残留 %.8f 超出 step %.5f 的合理范围" % (residual, step))

    # 这才是重点：粉尘必须被 position() 当成空仓，
    # 否则周期对账会判定不一致，把粉尘当真实持仓接管，
    # 之后要平掉它时 qty 取整成 0 被拒 -> 永久卡死。
    p = await ex.position()
    assert p["qty"] == 0.0, (
        "粉尘 %.8f 没被当成空仓。下一轮周期对账会把它当持仓接管，"
        "然后永远卖不掉——系统做完一笔就停摆。" % residual)

    await eng.reconcile_live()
    assert eng.pos is None, (
        "平仓后对账又把粉尘接管成持仓了，引擎会认为自己持仓中，永不交易")

    # ---------- 7. 钱去哪了：两笔手续费 ----------
    final = acct2["equity"]
    assert final > 98.0, "两笔 0.1%% 手续费最多扣 0.4%%，权益不该掉这么多"
    assert final < 100.0, "手续费必须真的扣掉了"

    # 决策与成交都要落库，看板才有东西可展示
    n_orders = len(fake.orders)
    assert n_orders >= 2, "一买一卖至少两笔订单，实际 %d" % n_orders


async def _raw_order(fake, side, qty, price):
    """绕过执行器，直接向假交易所的 HTTP 端口下一张单。

    为什么不走执行器：SpotExecutor 自己也有 min_notional / step 的本地校验，
    所以下 0.6U 的单会在本地就被挡掉，根本到不了交易所。
    这是正确的纵深防御，但它导致「测假交易所严不严」变成「测执行器严不严」。
    要验证假交易所本身，必须自己构造签名请求直接打过去。
    """
    import hashlib
    import hmac
    import time as _t
    from urllib.parse import urlencode

    params = {"symbol": "BTCUSDT", "side": side.upper(), "type": "MARKET",
              "quantity": "%.8f" % qty, "timestamp": int(_t.time() * 1000)}
    qs = urlencode(params)
    sig = hmac.new(fake.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    # 参数要走 form body：假交易所的 _spot_order 用 await request.text() 取待签串，
    # 放 query 里会签不到（返回 -1022 签名无效，而不是我们想测的那条拒绝理由）。
    payload = qs + "&signature=" + sig

    import aiohttp
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
                fake.base() + "/api/v3/order", data=payload,
                headers={"X-MBX-APIKEY": fake.api_key,
                         "Content-Type": "application/x-www-form-urlencoded"}) as resp:
            body = await resp.text()
            return resp.status, body


@pytest.mark.asyncio
async def test_fake_exchange_really_rejects_what_binance_rejects(env):
    """假交易所的严格本身必须被验证。

    这条测的不是产品代码，是测试工具。
    整个实盘路径的验证都建立在这个假交易所「够严」的前提上——
    它要是不严，上面那条排练的绿灯就没有意义。

    实测真币安 BTCUSDT 现货：minNotional=5.0、stepSize=0.00001、minQty=0.00001。
    """
    eng, ex, fake, tdb, feed, cfg = env

    # 1. 名义不足：0.00002 BTC x 30000 = 0.6 USDT < 5 -> 必须 400
    st, body = await _raw_order(fake, "BUY", 0.00002, PX)
    assert st == 400, (
        u"假交易所没拒名义 0.6U 的订单（返回 %d）。真币安会拒（minNotional=5），"
        u"于是这条路径上的测试全是假绿灯。响应：%s" % (st, body[:120]))
    assert "NOTIONAL" in body or "minNotional" in body, (
        u"拒绝原因不是最小名义，可能拒绝的理由不对：%s" % body[:120])

    # 2. 数量不是 step 整数倍 -> 必须 400
    #
    # 数量必须同时满足「名义合法」，否则会被第 1 条先拒掉，
    # 这条断言就变成"因为错误的原因通过"——和错误的原因失败一样没价值。
    # 0.000315 BTC x 30000 = 9.45 USDT（名义合法），
    # 但 0.000315 / 0.00001 = 31.5，不是整数倍（step 非法）。
    st2, body2 = await _raw_order(fake, "BUY", 0.000315, PX)
    assert st2 == 400, (
        u"假交易所没拒非 step 整数倍的订单（返回 %d，名义 9.45U 已合法）。"
        u"真币安会拒。响应：%s" % (st2, body2[:120]))
    assert "stepSize" in body2 or "multiple" in body2, (
        u"拒绝理由不是 step 倍数，可能被别的规则拒了：%s" % body2[:120])

    # 3. 合法订单必须能过，否则上面两条可能只是「什么都拒」
    st3, body3 = await _raw_order(fake, "BUY", 0.00033, PX)
    assert st3 == 200, (
        u"合法订单（0.00033 BTC = 9.9U，合法 step）被拒了（返回 %d）。"
        u"说明前两条的拒绝不是严格，而是什么都拒。响应：%s" % (st3, body3[:120]))
