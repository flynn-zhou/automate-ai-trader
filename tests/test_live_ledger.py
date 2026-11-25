# -*- coding: utf-8 -*-
"""真实持仓回合的还原与打分（第 130 轮）。

为什么这一层必须存在：trades 表只记「引擎经手过的」仓。
生产实证（11-11 09:04 → 11-12 22:57）：

    交易所 userTrades 25 笔：实现盈亏 +109.80、手续费 -16.92、资金费 -0.30
                            → 净 ≈ +92.59
    trades 表同区间 4 条   → 净 -20.46

差 113U。用户手工来回买卖的绝大多数根本没进库。所以这一层的输入
只能是交易所的逐笔成交，**不读 trades**。

本文件钉的是「还原对不对」和「分数有没有说谎」两件事：
  - 一单的定义是「从空仓到空仓」，加仓/部分平仓不能拆成多单
  - 反手必须拆成两单，且手续费按量摊（不拆会把平仓量混进入场价）
  - 净额 = 毛 - 费 + 资金费，符号不能错
  - 重复同步必须幂等，且**不能把已经算好的分数清掉**
  - 分数必须能由明细数字复算出来，分析文字里要有那些数字
"""
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader import live_ledger as L
from ai_trader.trade_db import TradeDB

MIN = 60_000
T0 = 1794300000000          # 固定基准时间，避免测试依赖"现在"


def fill(ts, side, qty, price, rp=0.0, fee=0.0, fid=None):
    return {"id": fid if fid is not None else ts // 1000,
            "order_id": "o", "ts": ts, "side": side, "qty": qty,
            "price": price, "realized_pnl": rp, "commission": fee,
            "asset": "USDT", "maker": False}


# --------------------------------------------------------------- 还原
def test_single_round_trip_reconstructs_vwap_and_net():
    rounds = L.build_rounds([
        fill(T0, "BUY", 0.1, 100.0, fee=0.02),
        fill(T0 + 30 * MIN, "SELL", 0.1, 110.0, rp=1.0, fee=0.055),
    ])
    assert len(rounds) == 1, "一开一平应是一单"
    r = rounds[0]
    assert r["side"] == "long"
    assert r["qty"] == pytest.approx(0.1)
    assert r["entry_price"] == pytest.approx(100.0)
    assert r["exit_price"] == pytest.approx(110.0)
    assert r["gross"] == pytest.approx(1.0)
    assert r["fee"] == pytest.approx(0.075)
    assert r["net"] == pytest.approx(0.925), "净额必须是 毛-费+资金费"
    assert r["hold_sec"] == 30 * 60
    assert r["n_fills"] == 2
    assert r["notional"] == pytest.approx(10.0)


def test_scale_in_and_scale_out_stay_one_round():
    """加仓 + 部分平仓 = 同一单。拆开记会把一笔仓变成好几笔「交易」，
    胜率和平均持仓时间全部失真。"""
    rounds = L.build_rounds([
        fill(T0, "BUY", 0.04, 100.0, fee=0.01),
        fill(T0 + MIN, "BUY", 0.06, 110.0, fee=0.01),
        fill(T0 + 2 * MIN, "SELL", 0.05, 120.0, rp=0.6, fee=0.01),
        fill(T0 + 3 * MIN, "SELL", 0.05, 90.0, rp=-0.9, fee=0.01),
    ])
    assert len(rounds) == 1, "仓位只回到空仓一次，就该只有一单"
    r = rounds[0]
    assert r["qty"] == pytest.approx(0.1)
    # 开仓 VWAP = (0.04*100 + 0.06*110)/0.1
    assert r["entry_price"] == pytest.approx(106.0), \
        "入场价必须是成交量加权，不是首笔价"
    # 平仓 VWAP = (0.05*120 + 0.05*90)/0.1
    assert r["exit_price"] == pytest.approx(105.0)
    assert r["gross"] == pytest.approx(-0.3)
    assert r["fee"] == pytest.approx(0.04)
    assert r["n_fills"] == 4


def test_reversal_splits_into_two_rounds_and_splits_the_fee():
    """一笔单把多打成空：平仓段归上一单，开仓段起新单，手续费按量摊。

    不拆的话，新回合的入场价里会混进平仓量，成本率算出来是错的；
    手续费全算到旧单上，新单就"零成本"了。
    """
    rounds = L.build_rounds([
        fill(T0, "BUY", 0.10, 100.0, fee=0.02),
        # 卖 0.15：平掉 0.10 多的，同时开 0.05 空。手续费 0.15
        fill(T0 + 10 * MIN, "SELL", 0.15, 110.0, rp=1.0, fee=0.15),
    ])
    assert len(rounds) == 2, "反手必须拆成两单"
    old, new = rounds
    assert old["side"] == "long" and old["exit_price"] == pytest.approx(110.0)
    assert old["qty"] == pytest.approx(0.10)
    assert old["gross"] == pytest.approx(1.0)
    # 0.10/0.15 = 2/3 的手续费归旧单
    assert old["fee"] == pytest.approx(0.02 + 0.15 * (0.10 / 0.15))
    assert new["side"] == "short", "反手后的新仓方向必须是空"
    assert new["qty"] == pytest.approx(0.05)
    assert new["entry_price"] == pytest.approx(110.0)
    assert new["exit_ts"] is None, "新仓还开着"
    assert new["fee"] == pytest.approx(0.15 * (0.05 / 0.15)), \
        "新单只能分摊开仓那部分手续费，否则它的成本率是假的高"


def test_open_round_is_recorded_but_unscored():
    rounds = L.build_rounds([fill(T0, "BUY", 0.1, 100.0, fee=0.02)])
    assert len(rounds) == 1
    r = rounds[0]
    assert r["exit_ts"] is None and r["exit_price"] is None
    assert r["score_total"] is None and r["grade"] == ""


def test_round_key_is_stable_across_rebuilds():
    """同一个回合重复还原必须得到同一个 key，否则每次同步都插一行新记录。"""
    fills = [fill(T0, "BUY", 0.1, 100.0, fee=0.02),
             fill(T0 + MIN, "SELL", 0.1, 110.0, rp=1.0, fee=0.05)]
    a = L.build_rounds(list(fills))
    b = L.build_rounds(list(fills))
    assert a[0]["round_key"] == b[0]["round_key"]
    assert "BTCUSDT" in a[0]["round_key"] and "long" in a[0]["round_key"]


def test_zero_qty_fills_are_ignored():
    rounds = L.build_rounds([
        fill(T0, "BUY", 0.0, 100.0),
        fill(T0 + MIN, "BUY", 0.1, 100.0, fee=0.02),
    ])
    assert len(rounds) == 1 and rounds[0]["n_fills"] == 1


def test_funding_goes_only_to_the_round_that_was_open():
    """资金费按持仓时间窗摊。摊错了会把上一单的持仓成本记到下一单头上。"""
    fills = [fill(T0, "BUY", 0.1, 100.0, fee=0.0),
             fill(T0 + 10 * MIN, "SELL", 0.1, 100.0, rp=0.0, fee=0.0),
             fill(T0 + 100 * MIN, "BUY", 0.1, 100.0, fee=0.0),
             fill(T0 + 110 * MIN, "SELL", 0.1, 100.0, rp=0.0, fee=0.0)]
    funding = [{"ts": T0 + 5 * MIN, "income": -0.5},
               {"ts": T0 + 105 * MIN, "income": -0.2}]
    rounds = L.build_rounds(fills, funding)
    assert len(rounds) == 2
    assert rounds[0]["funding"] == pytest.approx(-0.5)
    assert rounds[1]["funding"] == pytest.approx(-0.2)
    assert rounds[0]["net"] == pytest.approx(-0.5), "资金费必须进净额"


# --------------------------------------------------------------- 打分
def _scored(side="long", entry=100.0, qty=1.0, fee=0.02, funding=0.0,
            gross=2.0, bars=None, exit_ts=T0 + 10 * MIN):
    r = {"side": side, "entry_ts": T0, "exit_ts": exit_ts, "entry_price": entry,
         "exit_price": entry, "qty": qty, "notional": qty * entry,
         "fee": fee, "funding": funding, "gross": gross,
         "net": gross - fee + funding, "hold_sec": 600,
         "mfe": None, "mae": None, "score_total": None, "grade": "",
         "analysis": "", "analyzed_ts": 0}
    L.analyze_round(r, bars if bars is not None else
                    [[T0, entry + 0.001, entry - 0.001]], T0 + 20 * MIN)
    return r


def test_score_pnl_maps_return_to_the_full_range():
    # 名义 100U。净 +2U = +2% -> 满分 40；净 -2U -> 0；净 0 -> 20
    assert _scored(gross=2.0 + 0.02, fee=0.02)["score_pnl"] == L.W_PNL
    assert _scored(gross=-2.0 + 0.02, fee=0.02)["score_pnl"] == 0
    assert _scored(gross=0.0, fee=0.0)["score_pnl"] == 20


def test_entry_score_rewards_going_straight_into_profit():
    up = _scored(bars=[[T0, 101.0, 100.5]])      # 从没跌破入场价
    worst = _scored(bars=[[T0, 100.0, 99.0]])    # 涨都没涨上去，直接亏
    mixed = _scored(bars=[[T0, 100.5, 99.0]])    # 先亏后涨
    assert up["score_entry"] == L.W_ENTRY, "入场后立刻浮盈，位置最好"
    assert worst["score_entry"] == 0, "入场后立刻浮亏且没涨过，位置最差"
    assert 0 < mixed["score_entry"] < L.W_ENTRY, \
        "先亏后涨应落在中间，而不是顶到两端（那样等于没有区分度）"


def test_exit_score_measures_capture_of_max_profit():
    """出场质量 = 最大浮盈里抓回多少。抓满 = 满分，一分没抓 = 0。"""
    bars = [[T0, 110.0, 100.0]]       # mfe = +10，mae = 0
    full = _scored(gross=10.0, fee=0.0, bars=bars)
    none = _scored(gross=0.0, fee=0.0, bars=bars)
    assert full["score_exit"] == L.W_EXIT
    assert none["score_exit"] == 0
    half = _scored(gross=5.0, fee=0.0, bars=bars)
    assert half["score_exit"] == pytest.approx(L.W_EXIT / 2, abs=1)


def test_cost_score_bounds():
    # 名义 100U。费率 0.02%（maker 往返）应接近满分；1% 应是 0
    cheap = _scored(gross=0.0, fee=0.02)
    pricey = _scored(gross=0.0, fee=1.0)
    assert cheap["score_cost"] == L.W_COST
    assert pricey["score_cost"] == 0


def test_grade_cuts():
    assert L._grade(80) == "A" and L._grade(79) == "B"
    assert L._grade(65) == "B" and L._grade(64) == "C"
    assert L._grade(50) == "C" and L._grade(49) == "D"


def test_analysis_text_contains_the_numbers_it_scored_on():
    """只给一个分数等于没给分析。数字必须写出来，用户才能自己复核。"""
    r = _scored(gross=1.5, fee=0.33, funding=-0.1, bars=[[T0, 105.0, 96.0]])
    txt = r["analysis"]
    assert "105.00" not in txt or True      # 入场价必须出现
    assert "%.2f" % r["entry_price"] in txt, "分析里没有入场价"
    assert "0.3300" in txt, "分析里没有手续费数字"
    assert "-0.1000" in txt, "分析里没有资金费数字"
    assert "%+.4f" % r["net"] in txt, "分析里没有净额"
    assert r["grade"] in txt


def test_exit_text_says_so_when_profit_was_given_back():
    """有过浮盈、最后倒亏——这是最该被指出来的形态。

    写成「抓回 -5.40U（0%）」读起来像格式问题，等于把话说轻了。
    生产实证：20 秒那单最大浮盈 3.85U，最后毛亏 5.40U。
    """
    r = _scored(gross=-5.4, fee=0.42, bars=[[T0, 101.5, 99.0]])
    assert "倒亏" in r["analysis"], "浮盈全部还回去还说成「抓回」，把话说轻了"
    assert "抓回 -5.4000" not in r["analysis"]


def test_exit_text_still_reads_normally_when_profit_was_kept():
    r = _scored(gross=3.0, fee=0.1, bars=[[T0, 105.0, 99.5]])
    assert "抓回 3.0000" in r["analysis"]
    assert "倒亏" not in r["analysis"]


def test_missing_klines_does_not_crash_and_says_so():
    r = _scored(bars=[])
    assert r["mfe"] is None and r["score_total"] is None
    assert "K 线" in r["analysis"], "拿不到 K 线时必须说明，而不是给个假分数"


def test_a_failed_analysis_is_not_recorded_as_success():
    """★ 拉不到 K 线时不能把 analyzed_ts 写成现在——那是「把失败记成成功」。

    sync() 用 `if r.get("analyzed_ts"): continue` 跳过，所以一旦写上去，
    这一单**永远不会再被重试**。生产实证：那笔 20 秒的短单因为取数窗口
    起点没对齐分钟（已修）而返回空，analyzed_ts 被写成当时时间；
    修好取数逻辑、重启、再同步，它仍然是「缺 K 线」——修了也白修。

    反过来也不能一律置 0：接口临时故障时，20 单 × 每 5 分钟
    全是无效请求，还会烧掉下单要用的权重。所以给一个重试窗口。
    """
    fresh = _scored(bars=[])
    assert fresh["analyzed_ts"] == 0, \
        "近期拉不到 K 线的单被标成已分析，从此不会再重试"

    # 超出重试窗口的单：承认拉不到，标成已尝试、不再重试
    ancient = {"side": "long", "entry_ts": T0, "exit_ts": T0 + 600_000,
               "entry_price": 100.0, "exit_price": 101.0, "qty": 1.0,
               "notional": 100.0, "fee": 0.0, "funding": 0.0, "gross": 1.0,
               "net": 1.0, "hold_sec": 600, "mfe": None, "mae": None,
               "score_total": None, "grade": "", "analysis": "",
               "analyzed_ts": 0}
    much_later = T0 + 600_000 + L.KLINE_RETRY_MS + 60_000
    L.analyze_round(ancient, [], much_later)
    assert ancient["analyzed_ts"] == much_later, \
        "太早的单应标成已尝试（否则每 5 分钟重试一次，永远失败）"
    assert "太早" in ancient["analysis"], "要说清是太早拉不到，而不是含糊的失败"


def test_a_round_that_failed_once_gets_retried_and_then_scored(tmp_path):
    """失败的回合必须能在下一次同步里补上分——这是上一条的真实目的。"""
    import asyncio

    db = _db(tmp_path)
    entry = T0 + 20_000
    try:
        class Once(FakeEx):
            def __init__(self):
                super().__init__(
                    [fill(entry, "BUY", 0.026, 80000.0, fee=0.2),
                     fill(entry + 20_000, "SELL", 0.026, 81007.7, rp=26.2,
                          fee=0.22)],
                    bars=[])
                self.round = 0

            async def klines_1m(self, start_ms, end_ms, max_pages=4):
                self.round += 1
                if self.round == 1:
                    return []          # 第一次拉不到
                self.klines_calls += 1
                return [[(entry // MIN) * MIN, 81100.0, 79900.0]]

        ex = Once()
        asyncio.get_event_loop().run_until_complete(
            L.sync(ex, db, now_ms=T0 + 60 * MIN))
        time.sleep(1.0)
        a = db.recent_live_rounds(20)[0]
        assert a["score_total"] is None, "构造前提：第一次应该算不出来"
        assert a["analyzed_ts"] == 0, "失败被记成已分析，不会再重试"

        asyncio.get_event_loop().run_until_complete(
            L.sync(ex, db, now_ms=T0 + 62 * MIN))
        time.sleep(1.0)
        b = db.recent_live_rounds(20)[0]
        assert b["score_total"] is not None, \
            "第二次拉到 K 线了，分数却没补上（失败的回合被永久跳过）"
    finally:
        db.stop()


def test_mfe_mae_follow_the_trades_table_sign_convention():
    """mfe 为正、mae 为负，都是相对入场价的**价格偏移**——与 trades 表一致。
    两处口径不同的话，「同一笔仓」在两个页面上会显示两个不同的数。"""
    r = _scored(entry=100.0, bars=[[T0, 108.0, 95.0]], gross=8.0)
    assert r["mfe"] == pytest.approx(8.0)
    assert r["mae"] == pytest.approx(-5.0)
    short = _scored(side="short", entry=100.0, bars=[[T0, 105.0, 90.0]],
                    gross=10.0)
    assert short["mfe"] == pytest.approx(10.0), "空头的有利方向是下跌"
    assert short["mae"] == pytest.approx(-5.0), "空头的不利方向是上涨"


@pytest.mark.asyncio
async def test_a_20_second_round_still_gets_analyzed(tmp_path):
    """★ 生产实证出来的缺陷：20 秒的单永远拿不到分数。

    币安 /fapi/v1/klines 只返回 openTime >= startTime 的 K 线，而 openTime
    永远落在整分钟上。如果拿入场时刻当 startTime，**装着入场价的那根
    K 线就被排除了**——持仓 20 秒的单窗口内一根整分钟 K 线都没有，
    返回空，于是永远没有 MFE/MAE、永远没有分数。

    而"持仓几十秒"正是手工来回买卖的典型形态，等于把最该被看见的那批单
    排除在分析之外。生产上部署后 30 单里唯一缺 K 线的就是这个形态。
    """
    db = _db(tmp_path)
    entry = T0 + 20_000            # 故意不在整分钟上
    exit_ = entry + 20_000         # 持仓 20 秒
    bar_open = (entry // MIN) * MIN   # 装着入场的那根
    try:
        ex = FakeEx([fill(entry, "BUY", 0.026, 80000.0, fee=0.2),
                     fill(exit_, "SELL", 0.026, 81007.7, rp=26.2, fee=0.22)],
                    bars=[[bar_open, 81100.0, 79900.0]])
        await L.sync(ex, db, now_ms=T0 + 120 * MIN)
        time.sleep(1.0)
        r = db.recent_live_rounds(20)[0]
        assert r["score_total"] is not None, (
            "20 秒的单没算出分数 —— 取数窗口的 startTime 没对齐到分钟：%r"
            % (r["analysis"],))
        assert r["mfe"] is not None
        # 必须如实说明这是分钟级近似，不能装作精确
        assert "上界" in r["analysis"], \
            "极短持仓的 MFE 是上界，分析里必须写出来，否则分数会被当成精确值"
    finally:
        db.stop()


def test_klines_fetch_window_starts_on_a_minute_boundary():
    """取数起点必须向下取整到分钟 —— 这是上面那条的行为层原因。

    直接测参数而不是只测结果：结果层能过、参数层仍可能错（比如某天换了个
    K 线源，参数错但碰巧返回了数据），参数层是"为什么对"的那一层。
    """
    import asyncio

    seen = {}

    class _T:
        def __init__(self):
            self.cfg = SimpleNamespace(
                trading=SimpleNamespace(symbol="BTCUSDT"))

        async def _req(self, method, path, params, signed=True):
            seen["startTime"] = params["startTime"]
            seen["endTime"] = params["endTime"]
            # 返回一根 openTime 正好是取整后起点的 K 线
            return [[params["startTime"], "1", "81200.0", "79800.0",
                     "80500.0", "10", 0, 0, 0, 0, 0, 0]]

        _req.__self__ = None

    from ai_trader.exchange.live import LiveExecutor

    ex = LiveExecutor.__new__(LiveExecutor)
    ex.cfg = SimpleNamespace(trading=SimpleNamespace(symbol="BTCUSDT"))
    ex._req = _T()._req

    entry = T0 + 37_412        # 不在整分钟上
    asyncio.get_event_loop().run_until_complete(
        ex.klines_1m(entry, entry + 20_000))
    assert seen["startTime"] == (entry // MIN) * MIN, (
        "startTime=%d 没对齐到分钟（应为 %d）—— 装着入场的那根 K 线会被漏掉"
        % (seen["startTime"], (entry // MIN) * MIN))
    assert seen["startTime"] < entry, "起点必须早于入场时刻"


# --------------------------------------------------------------- 来源标注
def test_mark_source_distinguishes_adopted_strategy_and_manual():
    rounds = [
        {"entry_ts": T0, "source": "manual", "trade_id": None, "open_reason": ""},
        {"entry_ts": T0 + 1000, "source": "manual", "trade_id": None,
         "open_reason": ""},
        {"entry_ts": T0 + 500 * MIN, "source": "manual", "trade_id": None,
         "open_reason": ""},
    ]
    idx = {
        T0 + 2: {"id": 7, "open_reason": "实盘对账接管", "strategy": ""},
        T0 + 1003: {"id": 8, "open_reason": "趋势进场", "strategy": "donchian"},
    }
    L.mark_source(rounds, idx)
    assert rounds[0]["source"] == "adopted" and rounds[0]["trade_id"] == 7
    assert rounds[1]["source"] == "strategy" and rounds[1]["trade_id"] == 8
    assert rounds[2]["source"] == "manual", "匹配不上就是手工，不能猜"


def test_mark_source_respects_the_tolerance():
    rounds = [{"entry_ts": T0, "source": "manual", "trade_id": None,
               "open_reason": ""}]
    far = T0 + L.TRADE_MATCH_TOL_MS + 60_000
    L.mark_source(rounds, {far: {"id": 1, "open_reason": "实盘对账接管",
                                 "strategy": ""}})
    assert rounds[0]["source"] == "manual", "超出容差还敢标来源就是编"


# --------------------------------------------------------------- 同步
class FakeEx:
    def __init__(self, fills, funding=(), bars=None, boom=False):
        self.cfg = SimpleNamespace(trading=SimpleNamespace(symbol="BTCUSDT"))
        self._fills = list(fills)
        self._funding = list(funding)
        self._bars = list(bars or [])
        self._boom = boom
        self.klines_calls = 0
        self.user_trades_calls = 0

    async def user_trades(self, days_back=30, max_pages=10):
        self.user_trades_calls += 1
        if self._boom:
            raise RuntimeError("网络失败")
        return list(self._fills)

    async def funding_income(self, days_back=30, max_pages=10):
        return list(self._funding)

    async def klines_1m(self, start_ms, end_ms, max_pages=4):
        self.klines_calls += 1
        return list(self._bars)


def _db(tmp_path):
    db = TradeDB(str(tmp_path / "t.db"))
    db.start()
    return db


@pytest.mark.asyncio
async def test_sync_writes_rounds_and_is_idempotent(tmp_path):
    db = _db(tmp_path)
    try:
        ex = FakeEx([fill(T0, "BUY", 0.1, 100.0, fee=0.02),
                     fill(T0 + 10 * MIN, "SELL", 0.1, 110.0, rp=1.0, fee=0.05)],
                    bars=[[T0, 112.0, 98.0]])
        await L.sync(ex, db, now_ms=T0 + 60 * MIN)
        time.sleep(1.0)
        rows = db.recent_live_rounds(20)
        assert len(rows) == 1, "一次同步应只写一条"
        assert rows[0]["net"] == pytest.approx(1.0 - 0.07)
        first_calls = ex.klines_calls

        # 再同步一次：不能多出一行，也不能重新拉 K 线
        await L.sync(ex, db, now_ms=T0 + 61 * MIN)
        time.sleep(1.0)
        rows2 = db.recent_live_rounds(20)
        assert len(rows2) == 1, "重复同步写出了重复记录（幂等被破坏）"
        assert ex.klines_calls == first_calls, \
            "已经算过的单被重新拉了一遍 K 线（analyzed_ts 没起作用）"
        assert rows2[0]["analysis"] == rows[0]["analysis"]
    finally:
        db.stop()


@pytest.mark.asyncio
async def test_second_sync_keeps_scores_when_klines_become_unavailable(tmp_path):
    """★ 这条是给一个具体 bug 准备的。

    同步是「整行覆盖」。币安的 1m K 线只保留近一段时间，越老的单越拉不到；
    接口也可能临时失败。如果同步不把上次算好的分数搬回来，
    已经算过的单会在某次同步里被清空——账面看起来像"分析功能坏了"，
    而不是"这次没取到数据"，而且不报任何错。

    （分数可以在数据还在时重算，所以这里模拟的是**第二次拉不到 K 线**。）
    """
    db = _db(tmp_path)
    try:
        fills = [fill(T0, "BUY", 0.1, 100.0, fee=0.02),
                 fill(T0 + 10 * MIN, "SELL", 0.1, 110.0, rp=1.0, fee=0.05)]

        class Flaky(FakeEx):
            def __init__(self, boom=False):
                super().__init__(fills, bars=[[T0, 112.0, 98.0]])
                self.boom = boom

            async def klines_1m(self, start_ms, end_ms, max_pages=4):
                if self.boom:
                    raise RuntimeError("K 线拉不到了")
                return await super().klines_1m(start_ms, end_ms, max_pages)

        ex = Flaky()
        await L.sync(ex, db, now_ms=T0 + 60 * MIN)
        time.sleep(1.0)
        a = db.recent_live_rounds(20)[0]
        assert a["score_total"] is not None, "第一次同步就该算出分数"

        ex.boom = True
        await L.sync(ex, db, now_ms=T0 + 120 * MIN)
        time.sleep(1.0)
        b = db.recent_live_rounds(20)[0]
        assert b["score_total"] == a["score_total"], \
            "第二次拉不到 K 线，已算好的分数被清掉了（merge_prior 没生效）"
        assert b["grade"] == a["grade"] and b["mfe"] == a["mfe"]
        assert a["analysis"] == b["analysis"]
    finally:
        db.stop()


@pytest.mark.asyncio
async def test_repeated_sync_does_not_refetch_klines_for_every_round(tmp_path):
    """已经算过的单不能每 5 分钟重拉一次 K 线。

    最坏情况是 20 单 × 每 5 分钟 = 一天 5760 次请求，
    而币安是有权重配额的——把配额烧在"重算一个不会变的数"上，
    真正要下单的时候就没配额了。
    """
    db = _db(tmp_path)
    try:
        ex = FakeEx([fill(T0, "BUY", 0.1, 100.0, fee=0.02),
                     fill(T0 + 10 * MIN, "SELL", 0.1, 110.0, rp=1.0, fee=0.05)],
                    bars=[[T0, 112.0, 98.0]])
        await L.sync(ex, db, now_ms=T0 + 60 * MIN)
        time.sleep(1.0)
        first = ex.klines_calls
        await L.sync(ex, db, now_ms=T0 + 120 * MIN)
        time.sleep(1.0)
        assert ex.klines_calls == first, \
            "已算过的单被重新拉了 K 线（analyzed_ts / merge_prior 没起作用）"
    finally:
        db.stop()


@pytest.mark.asyncio
async def test_sync_survives_a_failing_funding_endpoint(tmp_path):
    db = _db(tmp_path)

    class BadFunding(FakeEx):
        async def funding_income(self, days_back=30, max_pages=10):
            raise RuntimeError("income 端点挂了")

    try:
        ex = BadFunding([fill(T0, "BUY", 0.1, 100.0, fee=0.02),
                         fill(T0 + 10 * MIN, "SELL", 0.1, 110.0, rp=1.0,
                              fee=0.05)])
        r = await L.sync(ex, db, now_ms=T0 + 60 * MIN)
        time.sleep(1.0)
        assert r["rounds"] == 1, "资金费拉不到不该让整次同步失败"
        assert len(db.recent_live_rounds(20)) == 1
    finally:
        db.stop()


@pytest.mark.asyncio
async def test_analysis_limit_only_scores_the_most_recent_ones(tmp_path):
    """只给最近 N 单拉 K 线。全历史都算的话，一次同步要打几十个请求。"""
    db = _db(tmp_path)
    fills = []
    for i in range(5):
        base = T0 + i * 1000 * MIN
        fills.append(fill(base, "BUY", 0.1, 100.0, fee=0.0, fid=i * 2))
        fills.append(fill(base + 10 * MIN, "SELL", 0.1, 101.0, rp=0.1,
                          fee=0.0, fid=i * 2 + 1))
    try:
        ex = FakeEx(fills, bars=[[0, 101.0, 99.0]])
        await L.sync(ex, db, analysis_limit=2, now_ms=T0 + 1000 * MIN)
        time.sleep(1.0)
        assert ex.klines_calls == 2, \
            "analysis_limit=2 却拉了 %d 次 K 线" % ex.klines_calls
    finally:
        db.stop()


def test_trade_db_upsert_is_idempotent_and_keeps_trade_id(tmp_path):
    db = _db(tmp_path)
    try:
        row = {"round_key": "K", "symbol": "BTCUSDT", "side": "long",
               "qty": 1.0, "entry_ts": 1, "entry_price": 100.0,
               "exit_ts": 2, "exit_price": 101.0, "gross": 1.0,
               "fee": 0.1, "funding": 0.0, "net": 0.9, "hold_sec": 1,
               "n_fills": 2, "notional": 100.0, "source": "manual",
               "trade_id": 42, "analyzed_ts": 9}
        db.upsert_live_rounds([dict(row)])
        time.sleep(1.0)
        # 第二次抓到的行没有 trade_id（引擎还没写 trades）
        again = dict(row)
        again["trade_id"] = None
        again["net"] = 0.95
        db.upsert_live_rounds([again])
        time.sleep(1.0)
        rows = db.recent_live_rounds(20)
        assert len(rows) == 1, "round_key 没起唯一约束的作用"
        assert rows[0]["net"] == pytest.approx(0.95), "新值没覆盖旧值"
        assert rows[0]["trade_id"] == 42, \
            "trade_id 被空值抹掉了 —— 已经关联上的仓又变回「不知道谁开的」"
    finally:
        db.stop()


def test_summary_counts_only_closed_rounds_for_win_rate(tmp_path):
    """未平仓的 net 是浮的，混进胜率就是错的。"""
    db = _db(tmp_path)
    try:
        db.upsert_live_rounds([
            {"round_key": "A", "symbol": "B", "side": "long", "qty": 1.0,
             "entry_ts": 1, "entry_price": 100.0, "exit_ts": 2,
             "exit_price": 101.0, "gross": 1.0, "fee": 0.1, "net": 0.9,
             "n_fills": 2, "notional": 100.0, "score_total": 70,
             "analyzed_ts": 9},
            {"round_key": "B", "symbol": "B", "side": "long", "qty": 1.0,
             "entry_ts": 3, "entry_price": 100.0, "exit_ts": None,
             "gross": 0.0, "fee": 0.1, "net": -0.1,
             "n_fills": 1, "notional": 100.0, "analyzed_ts": 0},
        ])
        time.sleep(1.0)
        s = db.live_rounds_summary(20)
        assert s["n"] == 2 and s["n_closed"] == 1 and s["n_open"] == 1
        assert s["n_win"] == 1 and s["win_rate"] == pytest.approx(1.0)
        assert s["net"] == pytest.approx(0.9), "未平仓的浮动净额不能算进合计"
        assert s["avg_score"] == pytest.approx(70.0)
    finally:
        db.stop()
