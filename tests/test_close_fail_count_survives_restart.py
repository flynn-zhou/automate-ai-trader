# -*- coding: utf-8 -*-
"""平仓失败闸第 127 轮只修了一半：Guard 记住了，引擎忘了（第 128 轮）。

要守的不变量
------------
1. 平仓连败到 6 次的闸，重启之后【两边】都还看得见（Guard 和引擎）。
2. 重启之后引擎一厢情愿清零，不能把 Guard 记录的失败放过去。
3. 没失败的时候重启，不会凭空长出一个计数。

缺陷的形状
----------
计数器有两个副本：

    engine._close_fail_count     引擎自己的，决定【退避 3/6/12…/300 秒】
    Guard.st.close_fail_streak    第 127 轮落库的那个，决定【能不能开新仓】

_sync_close_fail_streak() 是 engine -> Guard 的单向管道，
而 _close() 里清计数走的是 self._close_fail_count = 0 再 sync，
**没有任何一条路能把库里的值读回引擎**。

于是重启后：

    Guard   : 恢复了 6，闸锁着                           (127 轮修的)
    engine  : _close_fail_count = 0，_close_retry_ts = 0  (__init__ 里的)

后果一：止损失效时，退避从「已经退到 300 秒」变回「3, 6, 12…」，
  每次重启都把重试频率打回最激进档，刚好在最不该努力下单的时候努力下单。

后果二（更隐蔽）：Guard 是【边沿触发】的——
  close_fail_streak_changed 只在计数变化时改 temp_reasons。
  重启后引擎以为自己是 0，成功平仓一次就 sync(0)，
  Guard 的闸被解除——而库里记着的那 6 次失败是真的发生过的。

这和第 123 / 126 / 127 轮是同一族：**内存副本扛不过重启**，
只不过这回副本有两份，我上一轮只落了其中一份。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.base import FillResult
from ai_trader.guard import Guard
from ai_trader.trade_db import TradeDB
from ai_trader.trading.engine import DecisionEngine

PX = 78000.0


class _Bar:
    def __init__(self, ot, c, iv=14400000):
        self.interval_ms = iv
        self.open_time = ot
        self.o = self.open = c
        self.h = self.high = c * 1.001
        self.l = self.low = c * 0.999
        self.c = self.close = c
        self.v = self.volume = 1.0


class _Feed:
    src = "WS"

    def __init__(self, px=PX):
        self.last_price = px
        self.cfg = AppCfg()
        self.bar_hooks = []
        self.hub = type("H", (), {"publish": staticmethod(lambda m: None)})()
        self._cur = _Bar(0, px, iv=3000)
        self.agg = type("A", (), {"current": staticmethod(lambda iv: self._cur)})()


class _Spot:
    name = "spot"

    def __init__(self):
        self.orders = []

    async def market_order(self, side, qty, reason="", reduce_only=False):
        self.orders.append((side, qty, reason))
        return FillResult(ok=True, price=PX, qty=qty, fee=qty * PX * 0.001,
                          order_id="1", status="filled", reason="")

    async def equity(self):
        return {"balance": 100.0, "equity": 100.0, "position_qty": 0.0}

    async def position(self):
        return {"qty": 0.0, "entry": None, "side": "long"}


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    t = TradeDB(path)
    t.start()
    return t, path


def _engine(tdb, guard):
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.exec_market = "spot"
    return DecisionEngine(cfg, _Feed(), _Spot(), tdb, guard=guard)


def _flush(tdb, want, timeout=5.0):
    """等到库里的值真的等于 want，不是「有一行就算」。"""
    end = time.time() + timeout
    while time.time() < end:
        st = tdb.load_guard_state()
        if st and int(st.get("close_fail_streak") or 0) == want:
            return st
        time.sleep(0.05)
    raise AssertionError(u"等了 %.1f 秒，库里的 close_fail_streak 还不是 %d：%r"
                         % (timeout, want, tdb.load_guard_state()))


def _cleanup(tdb, path):
    tdb.stop()
    try:
        os.unlink(path)
    except OSError:
        pass


def test_an_engine_without_a_guard_still_attaches():
    """★ guard=None 时 attach() 必须照常工作。

    这条测试是变异「Guard 为空不返回」逼出来的——它变异落空，
    说明根本没有一条路径覆盖 guard=None。
    而 main.py 里 guard 是可选的（纸面/回放场景就是 None），
    真崩了会是在启动路径上，7x24 最怕这种。
    """
    tdb, path = _db()
    try:
        eng = _engine(tdb, None)
        eng.attach()
        assert eng._close_fail_count == 0
    finally:
        _cleanup(tdb, path)


def test_the_backoff_picks_up_where_it_left_off():
    """★ 重启后退避应当从第 6 档起步，而不是从第 1 档重新数。

    这条把「计数恢复了」和「计数有用」连起来：
    只断言 _close_fail_count == 6 的话，一个恢复了但不用它的实现也能过。
    """
    tdb, path = _db()
    try:
        g1 = Guard(None, tdb=tdb)
        eng1 = _engine(tdb, g1)
        eng1._close_fail_count = 5
        eng1._sync_close_fail_streak()
        _flush(tdb, 5)

        g2 = Guard(None, tdb=tdb)
        eng2 = _engine(tdb, g2)
        eng2.attach()

        # 第 6 次失败：退避应当是 min(3*2^(6-1),300) = 96 秒，不是 3 秒
        eng2._close_fail_count += 1
        delay = min(3 * (2 ** (eng2._close_fail_count - 1)), 300)
        assert delay == 96, (
            u"重启后第 6 次失败的退避是 %d 秒，应该是 96 秒——计数没有真的接上"
            % delay)
    finally:
        _cleanup(tdb, path)


def test_the_engine_gets_the_streak_back_too():
    """★ 重启后引擎的计数器必须和 Guard 一致，不能一个 6 一个 0。"""
    tdb, path = _db()
    try:
        g1 = Guard(None, tdb=tdb)
        eng1 = _engine(tdb, g1)
        eng1._close_fail_count = 6
        eng1._sync_close_fail_streak()
        assert g1.can_open(0)[0] is False, u"前提不成立：闸没锁上"
        _flush(tdb, 6)

        # ---- 重启 ----
        g2 = Guard(None, tdb=tdb)
        assert g2.st.close_fail_streak == 6, (
            u"前提不成立：Guard 没恢复（第 127 轮那条）")
        eng2 = _engine(tdb, g2)
        eng2.attach()

        assert eng2._close_fail_count == 6, (
            u"重启后引擎的 _close_fail_count 是 %r，而 Guard 认为是 %r。\n"
            u"两份副本不一致：退避会从 300 秒掉回 3 秒。"
            % (eng2._close_fail_count, g2.st.close_fail_streak))
    finally:
        _cleanup(tdb, path)


def test_an_one_sided_clear_cannot_lift_the_gate_early():
    """★ 第二重后果：引擎以为自己是 0 -> 同步一次 -> 闸被解除。

    而库里记着的那 6 次失败是真的发生过的。
    第 127 轮的变异「不恢复 daily」就是被这同一类东西掩盖的：
    闸看起来解除了，其实是根本没走到判定那一支。
    """
    tdb, path = _db()
    try:
        g1 = Guard(None, tdb=tdb)
        eng1 = _engine(tdb, g1)
        eng1._close_fail_count = 6
        eng1._sync_close_fail_streak()
        _flush(tdb, 6)

        g2 = Guard(None, tdb=tdb)
        eng2 = _engine(tdb, g2)
        eng2.attach()

        # 引擎这边全忘了，成功平一次仓就 sync(0)
        eng2._sync_close_fail_streak()

        ok, why = g2.can_open(0)
        assert ok is False, (
            u"引擎只因为自己那边是 0，就把库里记着的 6 次平仓失败放过了（%r）"
            % (why,))
    finally:
        _cleanup(tdb, path)


def test_no_streak_means_no_streak_after_a_restart():
    """★ 反向：没失败过的时候重启，不能凭空长出一个计数。"""
    tdb, path = _db()
    try:
        g1 = Guard(None, tdb=tdb)
        eng1 = _engine(tdb, g1)
        eng1._sync_close_fail_streak()
        time.sleep(0.3)

        g2 = Guard(None, tdb=tdb)
        eng2 = _engine(tdb, g2)
        eng2.attach()
        assert eng2._close_fail_count == 0, (
            u"从来没失败过，重启后引擎计数却变成 %r" % (eng2._close_fail_count,))
        assert g2.can_open(0)[0] is True, u"没失败过，闸却是锁着的"
    finally:
        _cleanup(tdb, path)
