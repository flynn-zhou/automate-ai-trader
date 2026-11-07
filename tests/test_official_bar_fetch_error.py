# -*- coding: utf-8 -*-
"""取官方 4h K 时抛异常：绝不能因此丢掉这根 K、也不能每 3 秒重试。

为什么单独写（第 105 轮）：
  第 100 轮覆盖率审计显示 engine.py:609-611 从未执行，那是：

      except Exception as e:
          logger.warning("取官方 4h K 异常，退回本地 bar：%s", e)
          off = None

  已有的 test_official_bar_wiring.py 覆盖了「取到官方」「返回 None」「老 K 不取」，
  但【没有一条让 fetch_official_bar 抛异常】。

  这不是假想的路径。trend.py:213-219 里这三行在 try 之外：

      opener = urllib.request.build_opener(...)
      q = urllib.parse.urlencode({...})
      req = urllib.request.Request(self.fapi + "/fapi/v1/klines?" + q, ...)

  fapi 地址配错（比如少了 scheme）会让 Request() 直接抛，异常一路冒到引擎。
  另外 fetch_official_bar 是经 asyncio.to_thread 调用的，
  进程优雅退出（systemd SIGTERM）时线程池已关，to_thread 自己就会抛 RuntimeError
  —— 那一刻正好有一根 4h K 要判断，是真实会发生的事。

要钉住两条：
  1. 抛异常时这根 4h K 仍然要喂进去（engine.py:593 的原话：
     「取不到（网络/限频）就退回本地 bar，绝不因为补数据失败而不判断」）；
  2. 同一根 4h K 不能每根 3s K 都重试取数 ——
     一个持续配错的 fapi 地址会让 4 小时里最多重试 4800 次 ×15s 超时，
     限频自杀，而且会把真正要做的判断全拖慢。

  ★ 关于第 2 条，我最初把功劳记错了行，变异测试抓出来的：
    那段条件是
        if (fresh and t_new > prev_t
                and t_new > int(getattr(self.trend, "last_eval_bar", 0) or 0)):
    我原先写的是「靠 _trend_fed_b4_t 挡住重试」。但实测：
        去掉 t_new > prev_t          -> 仍然通过
        去掉 last_eval_bar 条件       -> 仍然通过
        两个都去掉                    -> 红
    也就是说这两个条件是【互为备份的冗余】，各自都足以挡住重试。

    这是一件好事（防御冗余），但它带来一个测试上的事实：
    **这条测试钉的是「行为」，不是「某一行」。**
    它不能用来证明「_trend_fed_b4_t 是必要的」——那种结论需要把
    另一个条件也拿掉才能成立。把功劳写具体反而会误导将来的人
    去「保护」一个并不承重的那一行。
"""
import os
import sys
import tempfile
import time

import pytest
import pytest_asyncio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ai_trader.config import AppCfg
from ai_trader.market.agg import Bar
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

IV4H = 14400000


class _Hub:
    def publish(self, *_a, **_k):
        pass

    subscribers = 0


class Feed:
    def __init__(self, px):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()


class _Exec:
    """只够让 _step_trend 走完，不该被调用到下单。"""

    async def close(self):
        pass

    async def equity(self):
        return {"equity": 100.0, "position_qty": 0.0}

    async def market_order(self, *_a, **_k):
        raise AssertionError(u"本测试不该真的下单")


@pytest_asyncio.fixture
async def env():
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.sma_lookback = 5
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    feed = Feed(100.0)
    eng = DecisionEngine(cfg, feed, _Exec(), tdb, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    base_t = ((int(time.time() * 1000) // IV4H) * IV4H) - IV4H * 20
    eng.trend.bars = [{"t": base_t + i * IV4H, "o": 100.0, "h": 100.0,
                       "l": 100.0, "c": 100.0, "v": 1.0} for i in range(6)]
    try:
        yield eng, base_t
    finally:
        tdb.stop()


def _bars(t_new, vol=7.0):
    return {IV4H: [Bar(interval_ms=IV4H, open_time=t_new, open=100.0,
                        high=100.0, low=100.0, close=100.0, volume=vol,
                        closed=True)]}


@pytest.mark.asyncio
async def test_fetch_exception_still_feeds_the_local_bar(env, monkeypatch):
    """取数抛异常时，这根 4h K 仍然要喂进去 —— 只是内容是本地聚合的。"""
    eng, base_t = env
    t_new = base_t + 6 * IV4H

    def _boom(_t):
        raise RuntimeError("模拟：fapi 地址配错 / 线程池已关闭")
    monkeypatch.setattr(eng.trend, "fetch_official_bar", _boom)

    await eng._step_trend(_bars(t_new), 100.0, 100.0, t_new + IV4H + 3000)

    got = eng.trend.bars[-1]
    assert got["t"] == t_new, (
        u"取数抛异常导致这根 4h K 被整个丢弃了。engine.py:593 的原话是"
        u"「绝不因为补数据失败而不判断」。bars[-1]=%r" % (got,))
    assert got["v"] == pytest.approx(7.0), u"退回本地 bar 时应保留本地量"


@pytest.mark.asyncio
async def test_fetch_exception_is_visible_in_the_snapshot(env, monkeypatch):
    """抛异常退回本地 bar 时，看板必须能看出「这根本地残缺 bar」。"""
    eng, base_t = env
    t_new = base_t + 6 * IV4H

    def _boom(_t):
        raise RuntimeError("模拟：网络不可达")
    monkeypatch.setattr(eng.trend, "fetch_official_bar", _boom)

    await eng._step_trend(_bars(t_new, vol=42.0), 100.0, 100.0, t_new + IV4H + 3000)

    snap = eng._trend_snapshot
    assert "回退" in str(snap.get("bar_src", "")), (
        u"抛异常回退后 bar_src 没标记：%r —— 和正常情况下长得一模一样"
        % (snap.get("bar_src"),))
    assert snap.get("bar_vol") == pytest.approx(42.0)


@pytest.mark.asyncio
async def test_fetch_exception_does_not_retry_every_3s(env, monkeypatch):
    """抛异常之后，同一根 4h K 不能每根 3s K 都重试取数。

    不更新 _trend_fed_b4_t 的话，一个持续配错的 fapi 地址会让 4 小时里
    最多重试 4800 次 ×15s 超时 —— 限频自杀，还把正常判断全拖慢。
    """
    eng, base_t = env
    t_new = base_t + 6 * IV4H
    calls = []

    def _boom(t):
        calls.append(t)
        raise RuntimeError("模拟：一直失败")
    monkeypatch.setattr(eng.trend, "fetch_official_bar", _boom)

    now = t_new + IV4H + 3000
    await eng._step_trend(_bars(t_new), 100.0, 100.0, now)
    assert len(calls) == 1, u"第一次就该尝试取数：%r" % (calls,)

    # 同一根 4h K 再来三根 3s
    for k in range(1, 4):
        await eng._step_trend(_bars(t_new), 100.0, 100.0, now + k * 3000)
    assert len(calls) == 1, (
        u"同一根 4h K 被重复取数 %d 次 —— 每次都失败、每次都 15s 超时，"
        u"会把限频打满并把正常判断拖慢。calls=%r" % (len(calls), calls))


@pytest.mark.asyncio
async def test_reverse_control_success_marks_official(env, monkeypatch):
    """反向对照：取数成功时来源必须是 official。

    没有这条，上面三条可能因为「无论成功失败都标回退」而永远绿。
    """
    eng, base_t = env
    t_new = base_t + 6 * IV4H
    raw = [t_new, "100.0", "100.0", "100.0", "100.0",
           "8558.161", t_new + IV4H - 1, "0", 100, "0", "0", "0"]
    monkeypatch.setattr(eng.trend, "fetch_official_bar",
                        lambda _t: eng.trend.bar_from_kline(raw))

    await eng._step_trend(_bars(t_new, vol=117.73), 100.0, 100.0, t_new + IV4H + 3000)

    snap = eng._trend_snapshot
    assert snap.get("bar_src") == "official", (
        u"取数成功却没标 official：%r" % (snap.get("bar_src"),))
    assert snap.get("bar_vol") == pytest.approx(8558.161)
