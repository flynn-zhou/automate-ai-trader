# -*- coding: utf-8 -*-
"""硬停必须被值守员看见（第 125 轮）。

要守的不变量
------------
1. 硬停时 check() 必须报出来——进 issues，不能只在 facts 里躺着。
2. 账实不符尤其要报得明确：它不会自己解除，等人核对。
3. 没硬停时不能报——一个永远在响的警报等于没有警报。
4. 库是老库 / 表不存在时不能把巡检打死。

为什么这轮做
------------
第 123 轮让硬停能扛过重启（落库），第 124 轮让止损失效能锁闸。
两轮都在让闸更**持久**。但持久之后有个更糟的后果：

    闸一旦误停或正常停住，它就一直停着，而【没有任何人会知道】。

查证：`sentinel.check()` 读 decisions / bars / trades / equity /
rejected signals，**一点都不读 risk_events 和 guard_state**。
它的职责说明写的是「服务、数据流、决策流、持仓、权益、回撤、被拒信号」——
**没有「风控闸」这一项**。

而 AI_FEISHU_WEBHOOK 是空的，notify 是 None，所以 `_alert` 只能写日志：

    guard.position_mismatch() -> risk_events 写 guard_halt  (没人读)
                              -> guard_state.halted_hard=1 (没人读)
                              -> notify 是 None           (发不出去)
                              -> 看板要人主动打开才看得见

于是系统永久停机，而所有主动渠道都不出声。
这和第 116 轮的形状一模一样（钱在漏，仪表都指着正常），
只是这次漏的不是钱，是「系统已经停了」这个事实。
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.guard import Guard
from ai_trader.sentinel import Sentinel
from ai_trader.trade_db import TradeDB

HOUR = 3600000


def _market_db(d):
    """哨兵要读行情库的 bars 表；没有它 _q 会抛 no such table。"""
    p = os.path.join(d, "m.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE bars(interval_ms INTEGER, open_time INTEGER)")
    c.execute("INSERT INTO bars VALUES(3000, ?)", (int(time.time() * 1000) - 3000,))
    c.commit()
    c.close()
    return p


def _rig():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")
    tdb = TradeDB(path)
    tdb.start()
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    g = Guard(cfg, tdb=tdb)
    g.stall_grace_ms = 10 ** 13
    s = Sentinel(path, _market_db(d), mode="paper")
    return g, s, tdb, path


def _flush(tdb, want=1):
    """等单写队列落盘。

    guard_state 走 TradeDB 的异步单写队列（不直连 SQLite），
    所以 position_mismatch() 返回时库里可能还没有那一行。
    哨兵每 5 分钟巡检一次，这点延迟无所谓；
    但测试必须等，否则测的是"队列还没写"，不是"哨兵没报"。
    """
    for _ in range(40):
        time.sleep(0.05)
        try:
            c = sqlite3.connect(tdb._path)
            r = c.execute("SELECT halted_hard FROM guard_state WHERE id=1").fetchone()
            c.close()
            # 必须等到【值】达到预期，不能只等"有一行"：
            # 复位之后那一行变成 halted_hard=0，
            # 只等"有行"会立刻返回，测到的还是复位前的旧值。
            if r is not None and int(r[0]) == int(want):
                return
        except Exception:
            pass


def _fresh_rig():
    """决策/行情都新鲜的库，保证 issues 里出现的只会是硬停。"""
    g, s, tdb, path = _rig()
    now = int(time.time() * 1000)
    tdb._put(("decision", {"ts": now, "signal": "trend_watch", "action": "watch",
                           "reason": "-", "features": {}, "gates": []}))
    time.sleep(0.5)
    s.stale_decision_s = 10 ** 9
    s.stale_bar_s = 10 ** 9
    return g, s, tdb, path


def test_a_hard_halt_shows_up_in_the_sentinel_issues():
    """★ 硬停必须在 issues 里，不能只在 facts 里。

    只有进 issues 才会走 _alert / 值班报告；
    躺在 facts 里等于"记录下来了但没人被叫醒"。
    """
    g, s, tdb, path = _fresh_rig()
    try:
        r0 = s.check()
        assert not any(u"硬停" in x or u"熔断" in x for x in r0["issues"]), (
            u"前提不成立：还没硬停就已经在报了：%r" % (r0["issues"],))

        g.update(100.0, "WS", int(time.time() * 1000))
        g.position_mismatch(u"账实不符：库里 0.001，交易所 0.002")
        _flush(tdb)

        r = s.check()
        blob = " ".join(r["issues"])
        assert u"硬停" in blob, (
            u"账实不符硬停已经发生，巡检却一声不吭。issues=%r" % (r["issues"],))
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_the_mismatch_halt_says_it_will_not_release_itself():
    """账实不符不会自动解除——报告里必须说清楚，否则人会以为等一等就好。"""
    g, s, tdb, path = _fresh_rig()
    try:
        g.update(100.0, "WS", int(time.time() * 1000))
        g.position_mismatch(u"账实不符：库里 0.001，交易所 0.002")
        _flush(tdb)
        r = s.check()
        # ★ 必须只断言【提示那一段】，不能断言整条消息。
        # 第一版写的是 "账实不符" in blob or "不会自动恢复" in blob —— 是个「或」，
        # 而 fixture 的 reason 里本来就有「账实不符」，第一支永远命中，
        # 于是把提示改成"会自动恢复"或"继续观察"，测试照样绿。
        # 变异「账实不符说会自动恢复」「不说解除方式」两个都 SURVIVED，
        # 就是这个「或」放过去的。
        #
        # 这是第 119 轮那个缺陷的翻版：断言的范围比被测对象大，
        # 命中了别处的文字。所以这里按分隔符切出提示段再断言。
        line = [x for x in r["issues"] if u"硬停" in x][0]
        hint = line.split(u"｜")[-1]
        assert u"不会自动恢复" in hint, (
            u"报告没在提示段说清这条闸不会自己解除：hint=%r 全文=%r"
            % (hint, line))
        assert u"人工" in hint, (
            u"提示段没说该怎么办：hint=%r" % (hint,))
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_no_halt_means_no_noise():
    """★ 反向：没硬停时不能报。

    只测"停住了要报"，一个"永远报硬停"的实现也能过——
    而那正是一个永远在响的警报，用户会直接静音。
    """
    g, s, tdb, path = _fresh_rig()
    try:
        g.update(100.0, "WS", int(time.time() * 1000))
        r = s.check()
        blob = " ".join(r["issues"])
        assert u"硬停" not in blob, u"没硬停却报了硬停：%r" % (r["issues"],)
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_a_drawdown_halt_says_it_will_release_itself():
    """★ 回撤硬停要说"会自动好"，不能和账实不符说成同一句。

    变异「回撤说成人工」SURVIVED —— 因为我只测了 mismatch 这一支。
    而这两支对用户的意义完全相反：
      回撤  -> 等 30 天它会自己解除，别去动它
      账实  -> 它永远不会自己好，必须人工核对
    说反了，用户就会在"该等"的时候去乱动，
    或者在"该动手"的时候一直等下去。
    """
    import time as _t
    g, s, tdb, path = _fresh_rig()
    try:
        g.update(100.0, "WS", int(_t.time() * 1000))
        g.update(35.0, "WS", int(_t.time() * 1000) + 1000)   # 65% 回撤 -> 硬停
        assert g.st.halted_hard is True, u"前提不成立：回撤没触发硬停"
        _flush(tdb)
        r = s.check()
        line = [x for x in r["issues"] if u"硬停" in x][0]
        hint = line.split(u"｜")[-1]
        assert u"自动" in hint, (
            u"回撤熔断在提示段没说会自动解除：hint=%r" % (hint,))
        assert u"不用人工" in hint or u"不用人工干预" in hint, (
            u"回撤熔断没说清不用人工干预：hint=%r" % (hint,))
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_a_released_halt_stops_being_reported():
    """★ 解除之后必须不再报。

    第 122 轮加了自动解除，如果哨兵只看 risk_events 里的历史 guard_halt，
    那解除之后它会永远报下去——又是一个永远消不掉的警报。
    """
    g, s, tdb, path = _fresh_rig()
    try:
        g.update(100.0, "WS", int(time.time() * 1000))
        g.position_mismatch(u"账实不符")
        _flush(tdb)
        assert u"硬停" in " ".join(s.check()["issues"])
        g.reset()
        _flush(tdb, want=0)
        r = s.check()
        assert u"硬停" not in " ".join(r["issues"]), (
            u"已经人工复位，巡检还在报硬停（警报永远消不掉）：%r" % (r["issues"],))
    finally:
        tdb.stop()
        try:
            os.unlink(path)
        except OSError:
            pass