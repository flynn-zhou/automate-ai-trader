# -*- coding: utf-8 -*-
"""真账同步有没有真的接上（第 130 轮）。

这里钉两件事，都是"接不上"这种最隐蔽的错：

1. **早退路径**。reconcile_live() 在"对账通过"时会提前 return ——
   那是绝大多数情况。钩子挂在原方法末尾的话，就只在账实不符时才抓真账，
   正好反了。所以这里专门测"对账通过"这条路径必须也抓到真账。

2. **记账不许搞崩交易**。真账同步是附加功能，交易所接口超时、
   K 线拉不到、解析异常——任何一种都不许让对账失败、
   更不许让平仓流程卡住。
"""
import io
import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine
from ai_trader.trading.trend import TrendStrategy

MIN = 60_000
T0 = 1794300000000
ENGINE_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "ai_trader", "trading", "engine.py")


class _Hub:
    def publish(self, *_a, **_k):
        pass


class _Agg:
    def __init__(self, px):
        self._px = px

    def current(self, iv):
        from ai_trader.market.agg import Bar

        return Bar(interval_ms=3000, open_time=0, open=self._px,
                   high=self._px, low=self._px, close=self._px,
                   volume=1.0, closed=True)


class Feed:
    def __init__(self, px=100.0):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = _Hub()
        self.agg = _Agg(px)
        self.src = "ws"


class LedgerEx:
    """名 live、持仓为 0、带真账三个接口的假执行器。"""

    name = "live"

    def __init__(self, fills=None, bars=None, boom=False):
        self.cfg = AppCfg()
        self._fills = list(fills or [])
        self._bars = list(bars or [])
        self._boom = boom
        self.user_trades_calls = 0
        self.klines_calls = 0

    async def position(self):
        return {"qty": 0.0, "entry": 0.0, "side": ""}

    async def equity(self):
        return {"balance": 100.0, "unrealized": 0.0, "equity": 100.0,
                "position_qty": 0.0, "position_side": ""}

    async def user_trades(self, days_back=30, max_pages=10):
        self.user_trades_calls += 1
        if self._boom:
            raise RuntimeError("userTrades 超时")
        return list(self._fills)

    async def funding_income(self, days_back=30, max_pages=10):
        return []

    async def klines_1m(self, start_ms, end_ms, max_pages=4):
        self.klines_calls += 1
        return list(self._bars)

    async def verify_price(self, px):
        return px


def fill(ts, side, qty, price, rp=0.0, fee=0.0):
    return {"id": ts // 1000, "order_id": "o", "ts": ts, "side": side,
            "qty": qty, "price": price, "realized_pnl": rp,
            "commission": fee, "asset": "USDT", "maker": False}


def _eng(tmp_path, ex):
    cfg = AppCfg()
    cfg.trading.strategy = "donchian"
    cfg.trading.exec_market = "perp"
    tdb = TradeDB(str(tmp_path / "t.db"))
    tdb.start()
    eng = DecisionEngine(cfg, Feed(), ex, tdb, guard=None)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    return eng, tdb


# --------------------------------------------------------- 1. 早退路径也要抓
@pytest.mark.asyncio
async def test_reconcile_that_passes_still_syncs_the_ledger(tmp_path):
    """★ 对账通过（提前 return 的那条路径）必须也抓真账。

    钩子挂在原方法末尾时，这条测试会红——而生产上"对账通过"是
    99% 的情况，等于功能根本没接上。
    """
    ex = LedgerEx(fills=[fill(T0, "BUY", 0.1, 100.0, fee=0.02),
                         fill(T0 + 10 * MIN, "SELL", 0.1, 110.0, rp=1.0,
                              fee=0.05)],
                  bars=[[T0, 112.0, 98.0]])
    eng, tdb = _eng(tmp_path, ex)
    try:
        await eng.reconcile_live()          # 交易所与库都是空仓 → 对账通过
        assert ex.user_trades_calls == 1, \
            "对账通过时没有去抓真账 —— 钩子挂在了提前 return 后面"
        time.sleep(1.0)
        rows = tdb.recent_live_rounds(20)
        assert len(rows) == 1, "真账没落库"
        assert rows[0]["net"] == pytest.approx(1.0 - 0.07)
        assert rows[0]["score_total"] is not None, "分数没算出来"
    finally:
        tdb.stop()


# --------------------------------------------------------- 2. 记账不许搞崩交易
@pytest.mark.asyncio
async def test_ledger_failure_does_not_break_reconcile(tmp_path):
    """交易所接口炸了，对账必须照样跑完、照样不抛。

    真账是"附加报表"。它失败最多是少一张表，绝不能反过来
    让对账（全系统唯一敢说"我到底有多少仓"的地方）失败。
    """
    ex = LedgerEx(boom=True)
    eng, tdb = _eng(tmp_path, ex)
    try:
        await eng.reconcile_live()          # 不应抛出
        assert ex.user_trades_calls == 1
        assert tdb.recent_live_rounds(20) == []
    finally:
        tdb.stop()


@pytest.mark.asyncio
async def test_ledger_failure_does_not_swallow_reconcile_errors(tmp_path):
    """反向约束：兜住真账的异常，不等于把对账自己的异常也吞掉。

    只测"不抛"的话，把 reconcile_live 整段包进 try/except: pass 也能过，
    而那样对账失败就彻底静默了——比不抓真账严重得多。
    """
    class BadEx(LedgerEx):
        async def position(self):
            raise RuntimeError("positionRisk 挂了")

    ex = BadEx()
    eng, tdb = _eng(tmp_path, ex)
    try:
        await eng.reconcile_live()
        assert eng.last_error.startswith("对账失败"), \
            "对账自己的异常必须被记录并保留，不能被真账同步吞掉"
    finally:
        tdb.stop()


@pytest.mark.asyncio
async def test_spot_executor_is_skipped_not_an_error(tmp_path, caplog):
    """现货执行器没有 userTrades 这套接口。

    必须**根本不调用**它，而不是调用之后吞掉 AttributeError——
    后者会在生产日志里每 5 分钟留一条 ERROR，把真正的错误淹掉。

    检查方式是"有没有打出 ERROR"：`hasattr` 探测本身也会碰到属性，
    用计数器量不出来（第一版就是被这一点骗过的）。
    """
    class SpotEx:
        name = "spot"
        cfg = AppCfg()

        async def position(self):
            return {"qty": 0.0, "entry": 0.0, "side": ""}

        async def equity(self):
            return {"balance": 100.0, "unrealized": 0.0, "equity": 100.0,
                    "position_qty": 0.0, "position_side": ""}

        async def verify_price(self, px):
            return px

    ex = SpotEx()
    assert not hasattr(ex, "user_trades"), "构造前提：现货确实没有这个接口"
    eng, tdb = _eng(tmp_path, ex)
    try:
        with caplog.at_level(logging.ERROR):
            await eng.reconcile_live()
        errs = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.ERROR]
        assert not errs, ("对现货执行器也去调了真账接口，每 5 分钟报一次错：%r"
                          % (errs,))
        assert eng.last_error == ""
    finally:
        tdb.stop()


@pytest.mark.asyncio
async def test_a_second_reconcile_does_not_double_write(tmp_path):
    """每 5 分钟一次心跳。重复抓不能重复记账。"""
    ex = LedgerEx(fills=[fill(T0, "BUY", 0.1, 100.0, fee=0.02),
                         fill(T0 + 10 * MIN, "SELL", 0.1, 101.0, rp=0.1,
                              fee=0.05)],
                  bars=[[T0, 102.0, 99.0]])
    eng, tdb = _eng(tmp_path, ex)
    try:
        await eng.reconcile_live()
        time.sleep(1.0)
        await eng.reconcile_live()
        time.sleep(1.0)
        assert len(tdb.recent_live_rounds(20)) == 1, \
            "两次心跳写出了两条重复记录"
        assert ex.user_trades_calls == 2, "第二次心跳没有重新抓（数据就不会更新）"
    finally:
        tdb.stop()


# --------------------------------------------------------- 3. 平仓后刷新
def test_close_schedules_the_sync_instead_of_awaiting_it():
    """_close() 是在引擎锁里跑的（_step 的 async with self._lock）。

    在里面 await 一次真账同步，就等于把每一次报价、每一个止损判断
    都按在锁外面等几秒——止损被延后的代价远大于"报表晚 3 秒"。
    所以必须是 create_task 丢出去。

    这条用结构断言而不是计时断言：真跑一遍 _close() 需要造出完整的
    持仓+行情+权益环境；而要守的性质就是「这里不许有 await」，
    结构断言更直接，也不会因为机器快慢而闪。
    """
    src = io.open(ENGINE_PATH, encoding="utf-8").read()
    i = src.index("    async def _close(")
    body = src[i:src.index("    # ---------------- 记录", i)]
    assert "await self._sync_live_ledger()" not in body, \
        "又在平仓路径上 await 真账同步了 —— 会把引擎锁按住几秒"
    assert "create_task(self._sync_live_ledger())" in body, \
        "平仓后没有调度真账同步（这一单的分数要等 5 分钟）"


def test_the_ledger_hook_is_wrapped_in_try_finally():
    """对账的提前 return 分支很多，钩子必须在 finally 里才覆盖得全。"""
    src = io.open(ENGINE_PATH, encoding="utf-8").read()
    i = src.index("    async def reconcile_live(")
    body = src[i:src.index("    async def _reconcile_positions(", i)]
    assert "try:" in body and "finally:" in body, \
        "reconcile_live 的钩子没包在 try/finally 里（提前 return 会跳过它）"


# --------------------------------------------------------- 4. Web 页面
def _web(tmp_path, monkeypatch, rows=None):
    from ai_trader.web.app import create_app

    monkeypatch.setenv("AI_WEB_PASS", "s3cret")
    monkeypatch.setenv("AI_WEB_USER", "admin")
    monkeypatch.setenv("AI_SECRET_KEY", "test-secret")
    cfg = AppCfg()
    tdb = TradeDB(str(tmp_path / "w.db"))
    tdb.start()
    if rows:
        tdb.upsert_live_rounds(rows)
        time.sleep(1.0)

    class _F:
        class hub:
            subscribers = 0

        def snapshot(self):
            return {}

    return create_app(cfg, _F(), tdb=tdb), tdb


def _login(c):
    return c.post("/login", data={"username": "admin", "password": "s3cret"},
                  follow_redirects=False)


def test_the_page_is_behind_the_login(tmp_path, monkeypatch):
    """真实持仓页也是后台的一部分，未登录必须进不去。"""
    app, tdb = _web(tmp_path, monkeypatch)
    try:
        r = app.test_client().get("/admin/live-rounds", follow_redirects=False)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
    finally:
        tdb.stop()


def test_the_page_renders_the_rounds_and_the_numbers(tmp_path, monkeypatch):
    app, tdb = _web(tmp_path, monkeypatch, rows=[{
        "round_key": "BTCUSDT|1|long|0.075", "symbol": "BTCUSDT",
        "side": "long", "qty": 0.075, "entry_ts": 1794338941000,
        "entry_price": 72475.0, "exit_ts": 1794342003000,
        "exit_price": 72479.4, "gross": 0.3225, "fee": 3.805099,
        "funding": 0.0, "net": -3.482599, "hold_sec": 3062, "n_fills": 2,
        "notional": 5435.625, "source": "adopted", "trade_id": 17,
        "open_reason": "实盘对账接管", "mfe": 0.5, "mae": -20.0,
        "score_pnl": 19, "score_entry": 12, "score_exit": 8, "score_cost": 9,
        "score_total": 48, "grade": "D",
        "analysis": "入场 72475.00 → 出场 72479.40｜净 -3.4826", "analyzed_ts": 9,
    }])
    try:
        c = app.test_client()
        assert _login(c).status_code == 302
        r = c.get("/admin/live-rounds")
        html = r.get_data(as_text=True)
        assert r.status_code == 200, r.status_code
        for must in ("真实持仓", "72475.00", "72479.40", "-3.4826", "接管",
                     "48", "D"):
            assert must in html, "页面缺少 %r" % (must,)
        assert "51 分" in html, "持仓时长没按人读的格式渲染（3062 秒 = 51 分）"
    finally:
        tdb.stop()


def test_the_empty_page_explains_what_to_do(tmp_path, monkeypatch):
    app, tdb = _web(tmp_path, monkeypatch)
    try:
        c = app.test_client()
        _login(c)
        html = c.get("/admin/live-rounds").get_data(as_text=True)
        assert "还没有数据" in html, "空页面必须说明数据从哪来、怎么触发"
        assert "立即刷新" in html
    finally:
        tdb.stop()


def test_nav_has_the_entry(tmp_path, monkeypatch):
    """导航里必须有入口——做完了但没人能找到，等于没做。"""
    app, tdb = _web(tmp_path, monkeypatch)
    try:
        c = app.test_client()
        _login(c)
        html = c.get("/admin/").get_data(as_text=True)
        assert "/admin/live-rounds" in html, "导航没有「真实持仓」入口"
        assert "真实持仓" in html
    finally:
        tdb.stop()

