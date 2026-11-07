# -*- coding: utf-8 -*-
"""唐奇安通道策略（AI_STRATEGY=donchian）。

这些用例的重点不是"跑通"，而是**钉住几条会静默出错的口径**：
  1. 通道不含被判定的那根（含了就是把未来信息算进去，回测数字全废）
  2. 出场不能要求"新鲜跌破"（要求了 = 重启后永远不出场）
  3. 进场必须用"突破事件"，否则重启后错过整段趋势
  4. 引擎里所有写死 4h 的地方都要跟着策略主周期走
  5. 唐奇安的止损合法地就是 0，不能被兜底逻辑塞进一个 25%

口径提醒：state() 要 n+2 根才 ready（判定那根要 n 根窗口 + 上一根做事件判断）。
下面用例统一用 entry=5 / exit=3，喂 7 根。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Cfg(object):
    def __init__(self, **kw):
        self.donchian_entry = kw.get("entry", 5)
        self.donchian_exit = kw.get("exit", 3)
        self.donchian_interval_ms = kw.get("iv", 3600000)
        self.exec_market = kw.get("exec_market", "perp")
        self.trend_hysteresis_pct = 0.0


def _mk(entry=5, exit_=3):
    from ai_trader.trading.donchian import DonchianStrategy
    return DonchianStrategy(_Cfg(entry=entry, exit=exit_), symbol="BTCUSDT",
                            fapi_base="https://example.invalid", proxy="")


def _feed(s, bars):
    """直接塞已收盘 K（绕过时间判断），模拟 seed 完成后的状态。"""
    s.bars = [{"t": i * s.iv, "o": float(o), "h": float(h),
               "l": float(l), "c": float(c), "v": 1.0}
              for i, (o, h, l, c) in enumerate(bars)]
    s.last_eval_bar = s.bars[-1]["t"]
    s.seeded = True
    return s


# 7 根：判定那根(6)创 5 根新高，上一根(5)没有创 -> 新鲜的突破
FRESH = [(9, 9, 9, 9), (8, 8, 8, 8), (10, 10, 10, 10), (11, 11, 11, 11),
         (18, 18, 18, 18), (9, 9, 9, 9), (20, 20, 20, 20)]


def test_channel_excludes_the_bar_being_judged():
    """通道窗口必须【不含】被判定的那根。

    这是整份回测的地基。inclusive 会让上轨吃到当根的高点，
    于是"收盘 > 上轨"永远为假——策略一笔都不开，而且不报错。
    """
    s = _mk()
    _feed(s, FRESH)
    st = s.state(20.0)
    assert st["ready"] is True
    # 判定那根的窗口 = 第 1..5 根 high = 8,10,11,18,9 -> 上轨 18
    assert abs(st["upper"] - 18.0) < 1e-9, "上轨必须是前 5 根的最高，不含当根"
    assert st["above_upper"] is True


def test_inclusive_window_would_kill_every_signal():
    """反证：如果通道把当根算进去，突破判定恒为假。

    写这个用例是为了让「含不含当根」这件事有可观测的后果，
    否则改错了没有任何测试会红。
    """
    s = _mk()
    _feed(s, FRESH)
    st = s.state(20.0)
    highs = [b[1] for b in FRESH]
    inclusive = max(highs[-5:])          # 含当根
    assert inclusive == 20.0
    assert not (20.0 > inclusive)        # 含当根时永远不开仓
    assert st["upper"] < inclusive


def test_entry_needs_a_fresh_breakout_event():
    """进场认的是"突破"这个事件：当根在上轨上方、上一根不在。"""
    s = _mk()
    _feed(s, FRESH)
    st = s.state(20.0)
    assert st["above_upper"] is True and st["above_upper_prev"] is False
    sig = s.want_entry(20.0)
    assert sig is not None and sig["name"] == "donchian_up"


def test_no_entry_when_breakout_happened_earlier():
    """上一根就已经在上轨上方 -> 不是新事件 -> 不发进场信号。

    这条和上一条配对：只有两者同时成立，才说明判定的是【事件】而不是【状态】。
    如果只测"突破时能进"，那么把逻辑写成纯状态判定也能通过。
    """
    s = _mk()
    _feed(s, [(9, 9, 9, 9), (8, 8, 8, 8), (10, 10, 10, 10), (11, 11, 11, 11),
              (18, 18, 18, 18), (20, 20, 20, 20), (21, 21, 21, 21)])
    st = s.state(21.0)
    assert st["above_upper"] is True and st["above_upper_prev"] is True
    assert s.want_entry(21.0) is None


def test_exit_fires_even_when_the_break_happened_long_ago():
    """出场【不能】要求"新鲜跌破"。

    要求了就会出现这种断裂：进程在跌破之后重启、持仓还在，
    条件永远不成立 -> 仓位一路持有穿过整个下跌趋势，
    而唐奇安没有硬停损，等于永远不出场。
    """
    s = _mk()
    # lows 第 3..5 根 = 9,8,7 -> 出场线 7；当根收盘 1 跌破
    # 上一根(5) 的出场窗口 = 第 2..4 根 = 10,9,8 -> 8，收盘 7 也跌破
    _feed(s, [(10, 10, 10, 10), (10, 10, 10, 10), (10, 10, 10, 10),
              (9, 9, 9, 9), (8, 8, 8, 8), (7, 7, 7, 7), (1, 1, 1, 1)])
    st = s.state(1.0)
    assert st["below_lower"] is True
    assert st["below_lower_prev"] is True, "上一根也已经跌破——正是重启场景"
    ex = s.want_exit(1.0)
    assert ex is not None
    assert "补出场" in ex["reason"], "补出场与正常出场必须能被日志区分开"


def test_startup_reconcile_keeps_owing_until_it_really_opens():
    """启动对账：站在通道上方却没仓位，就要一直"欠着"这笔补仓。

    第一次评估时就把标志清掉的话，一旦那次开仓失败（资金不足/闸门拦截），
    对账机会被永久消费，资金到位后也不会补——只能等下一次突破，可能几周。
    """
    s = _mk()
    _feed(s, FRESH)
    assert s.need_reconcile is True
    rec = s.startup_entry(20.0)
    assert rec is not None and rec["name"] == "donchian_reconcile"
    assert s.need_reconcile is True, "还没真的开成，不能清标志"
    s.mark_reconciled()
    assert s.need_reconcile is False


def test_reconcile_releases_when_price_is_below_the_channel():
    """站在通道下方本来就该空仓 -> 对账完成，标志必须清掉。

    这是上一条的配对用例。只有"欠着"那一半测试的话，
    一个永远返回 recon 的实现也能通过，而它会导致每根 K 都尝试开仓。
    """
    s = _mk()
    _feed(s, [(10, 10, 10, 10), (10, 10, 10, 10), (10, 10, 10, 10),
              (10, 10, 10, 10), (10, 10, 10, 10), (10, 10, 10, 10),
              (2, 2, 2, 2)])
    assert s.startup_entry(2.0) is None
    assert s.need_reconcile is False


def test_channel_width_order_is_enforced():
    """exit_m >= entry_n 会让"刚进场就被同一根判出场"，必须退回默认值。"""
    from ai_trader.trading.donchian import DonchianStrategy
    s = DonchianStrategy(_Cfg(entry=3, exit=10), symbol="BTCUSDT",
                         fapi_base="https://example.invalid", proxy="")
    assert s.entry_n == 150 and s.exit_m == 75


def test_engine_uses_the_strategy_interval_not_a_hardcoded_4h():
    """引擎喂数必须跟着策略的主周期走，不能写死 14400000。

    写死的后果：唐奇安永远拿不到自己要的 1h K，b4 指向 4h 桶，
    策略每根 4h 才被喂一次——表现是"策略在跑、但一天只判断 6 次"，
    而且完全不会报错。
    """
    src = (ROOT / "ai_trader" / "trading" / "engine.py").read_text(encoding="utf-8")
    seg = src[src.index("async def _step_trend"):]
    seg = seg[:seg.index("# 看板快照必须")]
    assert "_civ" in seg, "喂数段必须从策略实例取主周期"
    assert "bars_by_iv.get(14400000)" not in seg, "不能写死 4h 桶"


def test_donchian_leaves_the_hard_stop_empty():
    """唐奇安的止损合法地是 0，兜底逻辑不能塞进一个 25%。

    塞了会怎样：大屏显示"距熔断 25%"，而回测里根本没有这条规则；
    更糟的是这条不存在的规则会真的把仓位砍在半路。
    """
    src = (ROOT / "ai_trader" / "trading" / "engine.py").read_text(encoding="utf-8")
    j = src.index("def _fallback_stop")
    seg = src[j:j + 1800]
    assert "_is_donchian()" in seg, "兜底止损必须对唐奇安短路"
    k = src.index("def _trend_plan")
    seg2 = src[k:k + 3000]
    assert "_is_donchian()" in seg2, "下单计划必须对唐奇安给出空止损"


def test_strategy_predicate_covers_both_channel_families():
    """_is_chan 必须同时认 trend 和 donchian。

    只认 trend 的后果：唐奇安会掉进剥头皮那条链，
    而剥头皮的可达性门会直接把趋势单全部拒掉——系统看起来在跑，一笔不做。
    """
    from ai_trader.trading.engine import DecisionEngine
    for name, want in (("trend", True), ("donchian", True),
                       ("scalp", False), ("", False)):
        class _C:
            strategy = name
        e = DecisionEngine.__new__(DecisionEngine)
        e.tcfg = _C()
        assert e._is_chan() is want, "strategy=%r" % name


def test_warmup_matches_each_strategy_window():
    """变热所需的根数必须各自算对。

    唐奇安少算 1 根 -> 用半截窗口算出错误通道且自称 ready；
    4h SMA 少算 1 根 -> state() 直接不 ready，策略永远不判断。
    """
    from ai_trader.trading.engine import DecisionEngine

    class _C:
        strategy = "donchian"
        donchian_entry = 150
        sma_lookback = 200
    e = DecisionEngine.__new__(DecisionEngine)
    e.tcfg = _C()
    assert e._chan_warmup() == 152
    _C.strategy = "trend"
    assert e._chan_warmup() == 201

# ============================================================================
# 下面两条是变异测试跑出来的缺口。变异测试用到的两个变异：
#   "出场要求新鲜跌破"、"喂数写死 4h"——当时都【存活】了。
# 存活的原因是断言比被测对象宽：前者的输入恰好让两个分支输出同一句话，
# 后者的断言只扫了函数体前几百字节，没覆盖真正读 _civ 的位置。
# 补法：前者逐字节比较两种输入下的 reason，后者扫整段并按真实变量断言。
# ============================================================================

def test_exit_reason_distinguishes_fresh_break_from_catch_up():
    """两种输入必须给出【不同】的 reason，且各自带对自己的标记。

    只断言"含补出场"是不够的：把分支写反、或者让两种输入走同一句，
    原用例都可能碰巧通过。这里直接比较两条输出。
    """
    # 口径：m=3 -> 当根下轨 = min(low[-4:-1])，上一根下轨 = min(low[-5:-2])
    fresh = _mk()
    # 第 6 根收盘 10 >= 它的下轨 9 -> 上一根【没有】跌破；第 7 根收盘 1 < 下轨 9
    _feed(fresh, [(10, 10, 10, 10), (10, 10, 10, 10), (10, 10, 10, 10),
                  (9, 9, 9, 9), (10, 10, 10, 10), (10, 10, 10, 10),
                  (10, 10, 9, 10), (1, 1, 1, 1)])
    a = fresh.want_exit(1.0)
    catch = _mk()
    # 第 6 根收盘 6 < 它的下轨 10 -> 上一根【已经】跌破（进程没赶上那次）
    _feed(catch, [(10, 10, 10, 10), (10, 10, 10, 10), (10, 10, 10, 10),
                  (12, 12, 12, 12), (10, 10, 10, 10), (10, 10, 10, 10),
                  (6, 6, 5, 6), (1, 1, 1, 1)])
    b = catch.want_exit(1.0)
    assert a is not None and b is not None
    assert a["reason"] != b["reason"], "两种处境必须能被日志区分开"
    assert "补出场" not in a["reason"], "新鲜跌破不是补出场"
    assert "补出场" in b["reason"], "早已跌破才是补出场"


def test_step_trend_reads_interval_from_the_strategy_instance():
    """喂数段必须真的把 _civ 用在取桶和新鲜度判断上。

    扫源码是弱断言，但这里要防的恰恰是"某个字面量被改回去"这种退化。
    所以按真实代码断言：_civ 必须来自 trend.iv，
    且取桶用的是 _civ、不是 14400000。
    """
    src = (ROOT / "ai_trader" / "trading" / "engine.py").read_text(encoding="utf-8")
    seg = src[src.index("async def _step_trend"):]
    seg = seg[:seg.index("# 看板快照必须")]
    body = [l for l in seg.splitlines()
            if l.strip() and not l.strip().startswith("#")]
    joined = "\n".join(body)
    assert 'getattr(self.trend, "iv"' in joined, "_civ 必须取自策略实例"
    assert "bars_by_iv.get(_ckey)" in joined, "取桶必须用策略周期"
    assert "_civ if _civ in bars_by_iv" in joined, "_ckey 必须优先认策略周期"
    # 新鲜度判断也必须按策略周期，不能按 4h
    assert "_civ" in joined.split("fresh =")[1].split("\n")[0], \
        "fresh 判据必须按策略周期"

def test_chan_view_normalises_donchian_state():
    """_chan_view 必须把唐奇安的 state 翻译成趋势版字段名。

    这条测试存在的唯一原因是一个真实的线上故障：唐奇安上线后
    _step_trend 还在读 st["above"]，唐奇安的 state() 没有这个键，
    每 3 秒一次 KeyError 被上层吞掉——服务 active、看板有数据、一笔不评估。
    """
    from ai_trader.trading.engine import DecisionEngine

    class _C:
        strategy = "donchian"
        donchian_entry = 150
        sma_lookback = 200
    e = DecisionEngine.__new__(DecisionEngine)
    e.tcfg = _C()
    raw = {"ready": True, "upper": 81953.7, "lower": 76270.0,
           "above_upper": False, "below_lower": False,
           "entry_n": 150, "exit_m": 75, "bars": 62679}
    v = e._chan_view(raw)
    for k in ("sma", "above", "lookback", "entry_line", "exit_line",
              "exit_above", "hyst_pct"):
        assert k in v, "归一化后必须带 %r，否则决策分支会 KeyError" % k
    assert v["sma"] == 81953.7 and v["lookback"] == 150
    assert v["above"] is False
    assert v["exit_above"] is True, "没跌破下轨时 exit_above 必须为真（继续持有）"


def test_chan_view_leaves_trend_state_untouched():
    """趋势版不能被改动：它已经是看板契约的一部分。

    配对用例——只测唐奇安那一半的话，一个"总是返回构造结果"的实现也能过，
    而它会悄悄改掉 SMA200 策略的 above 语义。
    """
    from ai_trader.trading.engine import DecisionEngine

    class _C:
        strategy = "trend"
        donchian_entry = 150
        sma_lookback = 200
    e = DecisionEngine.__new__(DecisionEngine)
    e.tcfg = _C()
    raw = {"ready": True, "sma": 82197.9, "above": True, "lookback": 200}
    assert e._chan_view(raw) is raw, "趋势版必须原样返回（同一个对象）"

# ============================================================================
# 端到端：整条 _step_trend 必须能在唐奇安配置下走完，不抛任何异常。
#
# 这一组是被线上事故逼出来的。当时 12 个单元用例全绿，
# 但它们只测了 DonchianStrategy 自己；把策略接进引擎之后，
# _step_trend 里还有三处读 st["above"]/st["sma"] 的地方，
# 唐奇安的 state() 没有这两个键 -> 每 3 秒一次 KeyError
# -> 被 except 吞成"决策步骤异常"。
# 症状：服务 active、看板有数据、日志只有一行 KeyError，一笔都没评估过。
#
# 「单元测试全绿而系统跑废」正是这个项目反复栽的坑，所以这一条不能省。
# ============================================================================
import asyncio
import os
import tempfile
import time

import pytest


def _mk_engine(strategy, equity=100.0):
    from ai_trader.config import AppCfg
    from ai_trader.trade_db import TradeDB
    from ai_trader.trading.engine import DecisionEngine

    cfg = AppCfg()
    cfg.trading.strategy = strategy
    cfg.trading.exec_market = "perp"
    cfg.trading.sma_lookback = 5
    cfg.trading.donchian_entry = 5
    cfg.trading.donchian_exit = 3

    class _Hub:
        def publish(self, *_a, **_k):
            pass
        subscribers = 0

    class _Feed:
        def __init__(self):
            self.last_price = 100.0
            self.cfg = cfg
            self.bar_hooks = []
            self.hub = _Hub()

    class _Exec:
        name = "paper"
        # class 体在 Python 里【不是】闭包作用域，直接写 balance = equity 会
        # NameError（这个用例第一次就是这样红的）。类建好之后再补属性。
        balance = 0.0

        async def close(self):
            pass

        async def equity(self):
            # 注意：class 体不是闭包作用域，方法里读不到 _mk_engine 的局部变量，
            # 所以走 self.balance（这本来就是执行器真实的取值口径）。
            return {"equity": self.balance, "position_qty": 0.0}

        async def market_order(self, *_a, **_k):
            raise AssertionError(u"本测试不该真的下单")

    _Exec.balance = float(equity)
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    eng = DecisionEngine(cfg, _Feed(), _Exec(), tdb, guard=None)
    return eng, tdb


def _prep_chan(eng, strategy, bars):
    from ai_trader.trading.donchian import DonchianStrategy
    from ai_trader.trading.trend import TrendStrategy
    s = (DonchianStrategy(eng.tcfg, symbol="BTCUSDT", fapi_base="https://x.invalid",
                          proxy="") if strategy == "donchian"
         else TrendStrategy(eng.tcfg, symbol="BTCUSDT", fapi_base="https://x.invalid",
                            proxy=""))
    iv = int(getattr(s, "iv", 14400000))
    base_t = ((int(time.time() * 1000) // iv) * iv) - iv * 40
    s.bars = [{"t": base_t + i * iv, "o": float(c), "h": float(c),
               "l": float(c), "c": float(c), "v": 1.0}
              for i, c in enumerate(bars)]
    s.seeded = True
    s.need_reconcile = False
    s.last_eval_bar = s.bars[-1]["t"]
    eng.trend = s
    return base_t + len(bars) * iv


def _feed_dict(eng, base_t):
    """喂一根【刚收盘】的主周期 K，外加一个 3s 桶（_log_decision 靠它去重）。

    必须用真正的 Bar 对象，不能用 dict：引擎里走的是
    getattr(local, "open_time", 0)，dict 上取不到属性会静默变成 0，
    于是"这根 K 不新鲜"，官方 K 那条路根本不会被走到——
    这个坑我第一次就踩了，测试表面上"跑了"但什么都没验证。
    """
    from ai_trader.market.agg import Bar
    iv = int(getattr(eng.trend, "iv", 14400000))
    now = int(time.time() * 1000)
    cur = (now // iv) * iv

    def _bar(t, ivv):
        return Bar(interval_ms=ivv, open_time=t, open=100.0, high=100.0,
                   low=100.0, close=100.0, volume=5.0)
    return {iv: [_bar(cur, iv)], 3000: [_bar(now, 3000)]}


def test_step_trend_runs_end_to_end_under_donchian():
    """唐奇安配置下 _step_trend 必须完整走完，且记录一条决策。

    这里不关心它得出什么结论，只关心**它有没有真的跑完**。
    线上那次故障的表现就是它从来没跑完。
    """
    eng, tdb = _mk_engine("donchian")
    try:
        base_t = _prep_chan(eng, "donchian", [100.0] * 8)
        bars_by_iv = _feed_dict(eng, base_t)
        # 用引擎自己的计数器，不读库：TradeDB 是单写者队列，
        # 刚 submit 的那条还没落盘，读库会看到 0——那是测试在骗自己。
        before = eng.decisions
        asyncio.get_event_loop().run_until_complete(
            eng._step_trend(bars_by_iv, 100.0, 100.0, int(time.time() * 1000)))
        assert eng.decisions > before, "一步都没评估 -> 引擎在唐奇安配置下是空转的"
    finally:
        tdb.stop()


def test_step_trend_runs_end_to_end_under_trend():
    """配对用例：趋势配置也必须照样跑完，归一化没有破坏旧路径。

    只测唐奇安那一半的话，一个"把趋势分支也改坏"的实现能蒙混过关。
    """
    eng, tdb = _mk_engine("trend")
    try:
        base_t = _prep_chan(eng, "trend", [100.0] * 8)
        bars_by_iv = _feed_dict(eng, base_t)
        before = eng.decisions
        asyncio.get_event_loop().run_until_complete(
            eng._step_trend(bars_by_iv, 100.0, 100.0, int(time.time() * 1000)))
        assert eng.decisions > before
    finally:
        tdb.stop()


def test_step_trend_logs_a_donchian_shaped_reason():
    """决策日志里必须出现"新高"而不是"SMA"。

    日志是用户唯一能看懂系统在干什么的东西。唐奇安跑起来却对外宣称
    "等 SMA200"，等于把因果链说成另一套系统，比不写更坏。
    """
    eng, tdb = _mk_engine("donchian")
    try:
        base_t = _prep_chan(eng, "donchian", [100.0] * 8)
        bars_by_iv = _feed_dict(eng, base_t)
        asyncio.get_event_loop().run_until_complete(
            eng._step_trend(bars_by_iv, 100.0, 100.0, int(time.time() * 1000)))
        d = eng.last_decision
        assert d, "一条决策都没写"
        reason = d.get("reason") or ""
        assert "SMA" not in reason, "唐奇安不该说自己等 SMA：%r" % reason
    finally:
        tdb.stop()
