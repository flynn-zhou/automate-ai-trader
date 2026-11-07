# -*- coding: utf-8 -*-
"""实盘体检必须真的会「拦」。

背景（2026-11-02 实测）：
    在服务器上跑 tools/preflight_live.py（当前确实没有密钥）：
        [NO-GO] AI_API_KEY / SECRET  缺失
        退出码 1
    闸门在工作。**但**脚本在凭证那一步就 return 了，
    后面 6 个硬阻断从来没被任何一次真实运行执行过：

        取不到牌价 / 交易对规则取不到 / 账户查询失败
        USDT 余额为 0 / 够不够开一手 / 按权益的实际计划开不出来
        时钟偏差过大

    一个从没红过的闸门不算闸门。它们正是"注资后敢不敢开"的实际依据，
    所以这里逐个构造坏状态，验证每一条真的会 NO-GO。

做法：把脚本当模块导入，直接调它的检查函数并注入坏输入
（它只是在顶层定义了函数，没有副作用，import 安全）。
"""
import importlib.util
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    path = os.path.join(ROOT, 'tools', 'preflight_live.py')
    spec = importlib.util.spec_from_file_location('preflight_live_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.results.clear()
    return mod


def _messages(mod):
    """取出所有被记为 NO-GO 的 (名称, 详情)。"""
    return [(n, d) for lv, n, d, _fix in mod.results if lv == mod.BAD]


@pytest.fixture()
def pf():
    m = _load()
    yield m
    m.results.clear()


# ---------------- 资金 / 计划 ----------------

def _spec():
    return {'step': '0.00001', 'min_qty': 0.00001, 'min_notional': 5.0,
            'base': 'BTCUSDT'}


def test_zero_balance_blocks(pf):
    """钱在合约钱包也要拦：现货余额 0 时开不出仓。"""
    from ai_trader.config import AppCfg
    cfg = AppCfg()
    eq = {'balance': 0.0, 'position_qty': 0.0, 'equity': 0.0}
    pf.check_funds_and_plan(cfg, cfg.trading, 78000.0, _spec(), eq, None, None)
    hits = _messages(pf)
    assert any('USDT 可用余额' == n for n, _ in hits), (
        '余额为 0 没有触发 NO-GO，实际：%r' % (hits,))


def test_equity_below_min_notional_blocks(pf):
    """权益小于最小名义额：开一手都开不出来，必须拦。"""
    from ai_trader.config import AppCfg
    cfg = AppCfg()
    eq = {'balance': 3.0, 'position_qty': 0.0, 'equity': 3.0}
    pf.check_funds_and_plan(cfg, cfg.trading, 78000.0, _spec(), eq, None, None)
    names = [n for n, _ in _messages(pf)]
    assert '够不够开一手' in names, (
        '权益 3U < minNotional 5U 却没有拦，实际：%r' % (names,))


def test_plan_infeasible_blocks(pf):
    """永续口径下 10U 低于最小名义 50U，计划必须不可行并拦下。

    注意：第一版我按现货写这个用例，断言失败——因为 _min_notional()
    对现货返回 5.0，10U 在现货下确实开得出仓（第 55 轮查过并记录）。
    是【我的测试前提错了】，不是代码有 bug。要测这个阻断必须用永续口径。
    """
    from ai_trader.config import AppCfg
    cfg = AppCfg()
    cfg.trading.exec_market = 'perp'      # 永续: _min_notional() -> trend_min_notional(55)
    cfg.trading.trend_min_notional = 55.0
    eq = {'balance': 10.0, 'position_qty': 0.0, 'equity': 10.0}
    pf.check_funds_and_plan(cfg, cfg.trading, 78000.0, _spec(), eq, None, None)
    names = [n for n, _ in _messages(pf)]
    assert '按当前权益的实际计划' in names, (
        '永续下 10U 权益低于 trend_min_notional=55，计划不可能成立，却没拦：%r'
        % (names,))


def test_spot_10u_is_not_blocked(pf):
    """对照用例：现货口径下 10U 是【能】开仓的，闸门不许误拦。

    这条和上一条互为反例——只测"会拦"而不测"该放行时放行"，
    一个永远报错的闸门也能通过全部断言。
    """
    from ai_trader.config import AppCfg
    cfg = AppCfg()
    cfg.trading.exec_market = 'spot'      # 现货: _min_notional() -> 5.0
    eq = {'balance': 10.0, 'position_qty': 0.0, 'equity': 10.0}
    pf.check_funds_and_plan(cfg, cfg.trading, 78000.0, _spec(), eq, None, None)
    assert not _messages(pf), (
        '现货下 10U 本可以开仓，闸门却拦了（误报）：%r' % (_messages(pf),))


def test_healthy_account_passes(pf):
    """对照：钱够的时候不许误报——只会拦坏状态的闸门才可信。"""
    from ai_trader.config import AppCfg
    cfg = AppCfg()
    eq = {'balance': 100.0, 'position_qty': 0.0, 'equity': 100.0}
    pf.check_funds_and_plan(cfg, cfg.trading, 78000.0, _spec(), eq, None, None)
    assert not _messages(pf), '账户正常却报了硬阻断：%r' % (_messages(pf),)


# ---------------- 交易所 ----------------

class _FeedStub:
    pass


def _patch_ex(msg, monkeypatch, mark_price=None, spec=None, equity=None):
    """把 SpotExecutor 换成一个按需失败的桩。"""
    import ai_trader.exchange.spot as spot_mod

    class _Ex:
        name = 'spot'

        def __init__(self, *a, **k):
            pass

        async def close(self):
            pass

        async def mark_price(self):
            if isinstance(mark_price, Exception):
                raise mark_price
            return mark_price or 78000.0

        async def spec(self):
            if isinstance(spec, Exception):
                raise spec
            return _spec()

        async def equity(self):
            if isinstance(equity, Exception):
                raise equity
            return {'balance': 100.0, 'position_qty': 0.0, 'equity': 100.0}

    monkeypatch.setattr(spot_mod, 'SpotExecutor', _Ex)
    return _Ex


def test_exchange_price_failure_blocks(pf, monkeypatch):
    import asyncio
    _patch_ex(pf, monkeypatch, mark_price=IOError('network unreachable'))
    from ai_trader.config import AppCfg
    cfg = AppCfg()
    ok, px, spec, eq, feed, ex = asyncio.get_event_loop().run_until_complete(
        pf.check_exchange(cfg, cfg.trading, True))
    assert ok is False and px == 0.0
    assert any(n == '取到交易所牌价' for n, _ in _messages(pf)), _messages(pf)


def test_exchange_spec_failure_blocks(pf, monkeypatch):
    import asyncio
    _patch_ex(pf, monkeypatch, spec=RuntimeError('symbol not found'))
    from ai_trader.config import AppCfg
    cfg = AppCfg()
    asyncio.get_event_loop().run_until_complete(
        pf.check_exchange(cfg, cfg.trading, True))
    assert any(n == '交易对规则' for n, _ in _messages(pf)), _messages(pf)


def test_exchange_account_failure_blocks(pf, monkeypatch):
    """密钥无效 / 没权限 / IP 白名单没放通，都要拦。"""
    import asyncio
    _patch_ex(pf, monkeypatch, equity=PermissionError('Invalid API-key'))
    from ai_trader.config import AppCfg
    cfg = AppCfg()
    asyncio.get_event_loop().run_until_complete(
        pf.check_exchange(cfg, cfg.trading, True))
    hits = _messages(pf)
    assert any(n == '账户查询（含签名）' for n, _ in hits), hits
    assert any('Invalid API-key' in d for n, d in hits), (
        '拦了但没把交易所的原话带出来，排查时看不到真正原因：%r' % (hits,))


# ---------------- 时钟 ----------------

class _SkewEx:
    name = 'spot'


def test_clock_skew_blocks(pf):
    """本地时钟偏太多会让签名请求被拒（时间戳超窗），必须拦。"""
    import asyncio

    class _Ex(_SkewEx):
        # check_misc 走的是 ex._req("GET", "/api/v3/time")，不是 server_time()
        async def _req(self, method, path, signed=False):
            return {'serverTime': int(time.time() * 1000) + 60_000}   # 偏 60 秒

    asyncio.get_event_loop().run_until_complete(pf.check_misc(None, _Ex()))
    hits = _messages(pf)
    assert any(n == '与交易所时钟偏差' for n, _ in hits), (
        '时钟偏 60 秒却没拦（币安时间戳超窗 1 秒就拒单）：%r' % (hits,))


def test_clock_ok_does_not_block(pf):
    """对照：时钟正常时不许误报。"""
    import asyncio

    class _Ex(_SkewEx):
        async def _req(self, method, path, signed=False):
            return {'serverTime': int(time.time() * 1000)}

    asyncio.get_event_loop().run_until_complete(pf.check_misc(None, _Ex()))
    assert not _messages(pf), _messages(pf)
