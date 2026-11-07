# -*- coding: utf-8 -*-
"""现货下单路径必须能对着"真 HTTP 服务器"跑通，而不只是对着假对象。

背景（2026-11-02）：
    SpotExecutor 的 market_order 从来没有真正执行过——账户没密钥，
    preflight 在凭证那步就 return。文档里记着五六个在这条路上踩过的血坑
    （部分成交被判成对账失败、手续费扣在基础币导致多卖、熔断拒绝平仓……），
    也就是说这些修复【一次都没在真实 HTTP + 签名 + 真实回报格式下跑过】。

    单个假 exchange 对象测不到这些：它把手续费记在 USDT 上、
    永远整笔成交、永远返回 200。所以这里起一个本地 aiohttp 服务器，
    完整模拟币安的三件事：签名校验、MARKET 订单回执格式、-2010 余额不足。

为什么不打真测试网：需要注册拿密钥，本轮无法自主完成。
假服务器至少能把 _req 的签名、URL 拼接、状态码分支、回执解析全跑一遍。
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
from urllib.parse import parse_qs, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from aiohttp import web

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotExecutor

KEY = 'testkey'
SECRET = 'testsecret'


class _Feed:
    src = 'WS'
    last_price = 78000.0


def _mk_ex(base_url):
    cfg = AppCfg()
    cfg.trading.symbol = 'BTCUSDT'
    return SpotExecutor(_Feed(), cfg, KEY, SECRET, base=base_url, proxy='')


class _Srv:
    """假币安：记录收到的请求，按预设回执应答。"""

    def __init__(self):
        self.orders = []
        self.sigs_ok = []
        self.balance = {'USDT': 100.0, 'BTC': 0.0}
        self.spec_qty_prec = 8
        self.fill_mode = 'full'      # full | partial | reject -2010
        self.fee_asset = 'BTC'
        self.fee_rate = 0.001
        self.no_fills = False
        self.trades = []          # /api/v3/myTrades 的回放数据
        self.runner = None
        self.port = 0

    async def _verify(self, req):
        qs = req.query_string or (await req.text())
        p = {k: v[0] for k, v in parse_qs(qs).items()}
        sig = p.pop('signature', '')
        # 必须按【发送时的原始字符串】验签，不能重排。
        # 第一版我写成 urlencode(sorted(...))，于是签名对不上——
        # 但那是我的验签器错，不是客户端错：币安只要求签名与发出去的
        # 那串 query string 完全一致，参数顺序随便。
        payload = qs.split('&signature=')[0]
        want = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        self.sigs_ok.append((sig == want, req.headers.get('X-MBX-APIKEY') == KEY))
        return p

    async def exchange_info(self, req):
        await self._verify(req)
        return web.json_response({'symbols': [{
            'baseAsset': 'BTC', 'quoteAsset': 'USDT',
            'baseAssetPrecision': self.spec_qty_prec,
            'filters': [
                {'filterType': 'LOT_SIZE', 'stepSize': '0.00001000',
                 'minQty': '0.00001000'},
                {'filterType': 'NOTIONAL', 'minNotional': '5.00000000'}]}]})

    async def ticker(self, req):
        return web.json_response({'symbol': 'BTCUSDT', 'price': '78000.00'})

    async def account(self, req):
        await self._verify(req)
        bals = [{'asset': a, 'free': str(v), 'locked': '0'}
                for a, v in self.balance.items()]
        return web.json_response({'balances': bals})

    async def order(self, req):
        p = await self._verify(req)
        side = p.get('side', '')
        qty = float(p.get('quantity', 0) or 0)
        self.orders.append({'side': side, 'qty': qty, 'raw': p})
        if self.fill_mode == 'reject':
            return web.json_response({'code': -2010,
                                      'msg': 'Account has insufficient balance'}, status=400)
        px = 78000.0
        filled = qty if self.fill_mode == 'full' else qty * 0.6
        quote = filled * px
        # 更新余额，模拟真实成交与手续费
        fee = filled * self.fee_rate
        if side == 'BUY':
            self.balance['USDT'] -= quote
            self.balance['BTC'] += (filled - fee if self.fee_asset == 'BTC' else filled)
        else:
            self.balance['BTC'] -= filled
            self.balance['USDT'] += (quote - fee if self.fee_asset == 'USDT' else quote)
        resp = {'symbol': 'BTCUSDT', 'side': side, 'type': 'MARKET',
                'executedQty': '%.8f' % filled,
                'cummulativeQuoteQty': '%.8f' % quote,
                'status': 'FILLED', 'orderId': 12345}
        if not self.no_fills:
            resp['fills'] = [
                {'price': '%.2f' % px, 'qty': '%.8f' % filled,
                 'commission': '%.8f' % fee, 'commissionAsset': self.fee_asset}]
        return web.json_response(resp)

    async def my_trades(self, req):
        await self._verify(req)
        return web.json_response(self.trades)

    async def start(self):
        app = web.Application()
        app.router.add_get('/api/v3/exchangeInfo', self.exchange_info)
        app.router.add_get('/api/v3/ticker/price', self.ticker)
        app.router.add_get('/api/v3/account', self.account)
        app.router.add_post('/api/v3/order', self.order)
        app.router.add_get('/api/v3/myTrades', self.my_trades)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return 'http://127.0.0.1:%d' % self.port

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()


def scenario(fn, **knobs):
    """在【同一个 event loop】里起服务器、跑场景、收服务器。

    第一版把 start() 和客户端放在两个不同的 loop 里：
    start() 用 new_event_loop().run_until_complete 起完就把 loop 停了，
    服务器那边的 asyncio 任务全部挂在一个不再运行的 loop 上，
    客户端连过去只能超时（实测 65 秒全挂）。
    aiohttp 的 server 和 client 必须共享同一个正在运行的 loop。
    """

    async def go():
        s = _Srv()
        for k, v in knobs.items():
            setattr(s, k, v)
        url = await s.start()
        try:
            return await fn(s, url)
        finally:
            await s.stop()

    return asyncio.new_event_loop().run_until_complete(go())

def test_buy_signs_and_parses_fill():
    """买入：签名必须对，回执数量必须解析对。"""

    async def go(s, url):
        ex = _mk_ex(url)
        try:
            return s, await ex.market_order('buy', 0.0009)
        finally:
            await ex.close()

    s, r = scenario(go)
    assert r.ok, '买入失败：%s' % r.reason
    assert s.orders and s.orders[0]['side'] == 'BUY'
    assert abs(s.orders[0]['qty'] - 0.0009) < 1e-9, '下单数量不对：%r' % s.orders[0]['qty']
    sig_ok, key_ok = s.sigs_ok[-1]
    assert sig_ok, 'HMAC 签名对不上——实盘会直接被 -1022 拒掉'
    assert key_ok, 'X-MBX-APIKEY 头没带对'


def test_sell_is_truncated_to_actual_holdings():
    """卖出量超过账上持币必须截断，绝不能变成卖空。"""

    async def go(s, url):
        s.balance['BTC'] = 0.0004
        ex = _mk_ex(url)
        try:
            return s, await ex.market_order('sell', 0.0010)
        finally:
            await ex.close()

    s, r = scenario(go)
    assert s.orders, '完全没有发出订单'
    assert s.orders[0]['qty'] <= 0.0004 + 1e-9, (
        '卖出了 %r，超过账上 0.0004 —— 实盘会报余额不足，仓位平不掉'
        % s.orders[0]['qty'])


def test_insufficient_balance_is_reported_not_swallowed():
    """交易所 -2010 必须变成 ok=False，而不是抛出去或假装成功。"""

    async def go(s, url):
        ex = _mk_ex(url)
        try:
            return s, await ex.market_order('buy', 0.0009)
        finally:
            await ex.close()

    s, r = scenario(go, fill_mode='reject')
    assert not r.ok, '交易所拒单了却报成功'
    assert 'insufficient' in r.reason.lower() or '2010' in r.reason, (
        '拒单原因没有带出来，排查时看不到真实原因：%r' % r.reason)


def test_partial_fill_is_accepted():
    """部分成交是正常事件，不能被判成对账失败（历史上会永久熔断）。"""

    async def go(s, url):
        ex = _mk_ex(url)
        try:
            return s, await ex.market_order('buy', 0.0010)
        finally:
            await ex.close()

    s, r = scenario(go, fill_mode='partial')
    assert r.ok, ('部分成交被判成失败：%s。历史上这会把 halted 置位，'
                  '之后所有下单都返回「已熔断」，只有人工重启能恢复。' % r.reason)


def test_fee_in_base_asset_is_deducted():
    """手续费扣的是基础币时，账面持仓必须小于 executedQty。"""

    async def go(s, url):
        ex = _mk_ex(url)
        try:
            return s, await ex.market_order('buy', 0.0010)
        finally:
            await ex.close()

    s, r = scenario(go, fee_asset='BTC', fee_rate=0.001)
    assert r.ok, r.reason
    held = s.balance['BTC']
    assert held < 0.0010, (
        '交易所实际只到账 %r（手续费扣了 BTC），如果当成 0.0010 持仓，'
        '平仓时会多卖 0.1%%，实盘报余额不足、仓位永远平不掉' % held)

# ---------------- 启动对账要用到的读接口 ----------------

def test_equity_counts_both_usdt_and_btc():
    """权益 = USDT + BTC 市值。少算任何一边，仓位就会算错。"""

    async def go(s, url):
        s.balance['USDT'] = 40.0
        s.balance['BTC'] = 0.0005       # 0.0005 * 78000 = 39U
        ex = _mk_ex(url)
        try:
            return s, await ex.equity()
        finally:
            await ex.close()

    s, eq = scenario(go)
    assert abs(eq['balance'] - 40.0) < 1e-6, 'USDT 余额读错：%r' % eq['balance']
    assert abs(eq['position_qty'] - 0.0005) < 1e-9, '持仓数量读错：%r' % eq['position_qty']
    assert abs(eq['equity'] - 79.0) < 0.5, (
        '权益应约 79U（40 + 0.0005×78000），实际 %r —— 少算一边会让仓位和熔断全错'
        % eq['equity'])


def test_equity_without_local_price_fetches_exchange_price():
    """本地没价且手里有币时，必须向交易所要牌价，不能按 0 估值。

    现场：进程重启时 feed 只拉起后台任务、不等第一个 tick，
    紧接着就调 reconcile_live()，那一刻 last_price 是 0。
    若把持仓估值成 0，一个持有 79U 的账户会被算成只剩 40U。
    """

    async def go(s, url):
        s.balance['USDT'] = 40.0
        s.balance['BTC'] = 0.0005
        cfg = AppCfg()
        cfg.trading.symbol = 'BTCUSDT'

        class _NoPrice(_Feed):
            last_price = 0.0

        ex = SpotExecutor(_NoPrice(), cfg, KEY, SECRET, base=url, proxy='')
        try:
            return s, await ex.equity()
        finally:
            await ex.close()

    s, eq = scenario(go)
    assert eq['equity'] > 70, (
        '本地无价却把持仓估成 0：equity=%r（应约 79）。'
        '后果是权益表写进垃圾、仓位算错、熔断误判。' % eq['equity'])


def test_dust_position_is_flat_but_still_valued():
    """小于一个步长的粉尘要按空仓处理（否则永远卡死），但钱仍算在权益里。

    踩过的场景：账上剩 0.00000872（小于 step 0.00001）。
    对账若把它当持仓，引擎再也不开新仓；要平又因取整成 0 被拒——系统做完一笔就永久卡住。
    """

    async def go(s, url):
        ex = _mk_ex(url)
        try:
            s.balance['USDT'] = 99.0
            s.balance['BTC'] = 0.00000872
            pos = await ex.position()
            eq = await ex.equity()
            return s, pos, eq
        finally:
            await ex.close()

    s, pos, eq = scenario(go)
    assert pos['qty'] == 0.0, (
        '粉尘 0.00000872 < step 0.00001 却没有按空仓处理：qty=%r' % pos['qty'])
    assert eq['equity'] > 99.0, (
        '粉尘的钱不能凭空消失，权益应略高于 99U，实际 %r' % eq['equity'])


def test_cost_basis_deducts_base_asset_fee():
    """按成交回放成本时，手续费若扣的是基础币，净持仓必须小于毛量。

    不扣的后果（第 58 轮实测）：7 笔之后偏差 0.689%，
    超过 reconcile_live 的 0.5% 容差 —— 重启时若正持仓，成本基准被判「对不上」而弃用，
    退回按当前价当成本并打 CRITICAL。成本错了止损价跟着错，pnl 也会记错。
    """

    async def go(s, url):
        # 买 0.0010 @ 78000，手续费 0.001 扣在 BTC 上
        s.trades = [
            {'time': 1700000000000, 'qty': '0.00100000', 'price': '78000.00',
             'isBuyer': True, 'commission': '0.00000100', 'commissionAsset': 'BTC'},
        ]
        ex = _mk_ex(url)
        try:
            return s, await ex.cost_basis()
        finally:
            await ex.close()

    s, cb = scenario(go)
    assert cb['ok'], '成交回放被判不可信：%r' % (cb,)
    assert abs(cb['qty'] - 0.000999) < 1e-9, (
        '净持仓应是 0.0010-0.000001=0.000999，实际 %r。'
        '不扣手续费会系统性偏大，最终被判对账失败。' % cb['qty'])


def test_cost_basis_quote_fee_does_not_change_qty():
    """对照：手续费以 USDT 计价（返佣/BNB 抵扣）时，持仓就是毛量。

    这条和上一条互为反例——只测「会扣」的话，一个无脑扣减的实现也能通过。
    """

    async def go(s, url):
        s.trades = [
            {'time': 1700000000000, 'qty': '0.00100000', 'price': '78000.00',
             'isBuyer': True, 'commission': '0.078', 'commissionAsset': 'USDT'},
        ]
        ex = _mk_ex(url)
        try:
            return s, await ex.cost_basis()
        finally:
            await ex.close()

    s, cb = scenario(go)
    assert abs(cb['qty'] - 0.0010) < 1e-9, (
        '手续费记在 USDT 上，持仓应仍是 0.0010，实际 %r（不该按基础币扣）' % cb['qty'])
