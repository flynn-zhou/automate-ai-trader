# -*- coding: utf-8 -*-
"""用真实引擎跑历史回放，钉住"生产代码 = 回测策略"。

为什么需要这个测试：
tools/verify_parity.py 比对的是回测实现和一份独立的线上实现——两者都是我写的模型，
它从没证明过【真正跑在服务器上的 DecisionEngine】能复现回测。
本测试直接把历史 4h K 线一根根喂给真实的 _step_trend，用 PaperExecutor 撮合。

实测结论：181 笔（与回测笔数一致），逐笔进出价一致，
累计 +507% vs 回测 +474%。差异来自 paper 模式用永续步长 0.001 取整
（权益低时取整会略微超过 100%，等价于轻微加杠杆），不是信号差异。

需要 data/backtest.db，缺失时跳过。
"""
import asyncio
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "backtest.db")
pytestmark = pytest.mark.skipif(not os.path.exists(DB),
                                reason="需要 data/backtest.db")

NL = 200


class _Hub:
    def publish(self, *a, **k): pass


class Feed:
    def __init__(self, px, cfg):
        self.last_price = px
        self.cfg = cfg
        self.hub = _Hub()
        self.src = "WS"
        outer = self

        class _B:
            def __init__(self, c): self.close = c

        class _Agg:
            def current(self, iv): return _B(outer.last_price)

        self.agg = _Agg()


async def _replay(hyst_pct=0.0):
    """hyst_pct 必须显式传：靠 AppCfg() 默认值会让「测试跑的口径」随默认值漂移。

    以前这里不传，取默认 0.0；而生产跑 0.2。
    于是这条最强的 parity 测试从没验证过生产配置。
    """
    import tools.backtest as bt
    from ai_trader.config import AppCfg
    from ai_trader.exchange.paper import PaperExecutor
    from ai_trader.trade_db import TradeDB
    from ai_trader.trading.engine import DecisionEngine
    from ai_trader.trading.trend import TrendStrategy

    rows = [b for b in bt.load(DB, "4h") if b[5] > 0]
    cfg = AppCfg()
    cfg.trading.strategy = "trend"
    cfg.trading.sma_lookback = NL
    cfg.trading.trend_stop_pct = 25.0
    cfg.trading.trend_leverage = 1.0
    cfg.trading.taker_fee = 0.001
    cfg.trading.paper_balance = 100.0
    cfg.trading.exec_market = "spot"      # 与生产部署一致：现货 1x
    cfg.trading.trend_hysteresis_pct = float(hyst_pct)

    feed = Feed(rows[NL][4], cfg)
    tmp = tempfile.mkdtemp()
    tdb = TradeDB(os.path.join(tmp, "t.db"))
    tdb.start()
    ex = PaperExecutor(feed, cfg)
    ex.restore_from_db(tdb)
    ex._slip = lambda s, p: p

    eng = DecisionEngine(cfg, feed, ex, tdb)
    eng.trend = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    eng.trend.seeded = True
    eng.trend.need_reconcile = False
    eng._seed_retry_ts = 2 ** 31

    # 必须像生产一样先喂满历史：少了前段，策略在前几十根还没 ready，
    # 会漏掉最早那笔大趋势（实测漏掉 +73.7%，总收益从 474% 掉到 224%）。
    eng.trend.bars = [{"t": int(r[0]), "o": r[1], "h": r[2], "l": r[3],
                       "c": r[4], "v": r[5]} for r in rows[:NL + 1]]

    trades = []
    orig = eng._close

    async def wrapped(reason, exit_px, price, bars_by_iv):
        trades.append((eng.pos["entry"], price))
        return await orig(reason, exit_px, price, bars_by_iv)

    eng._close = wrapped

    for i in range(NL + 1, len(rows)):
        r = rows[i]
        feed.last_price = r[4]
        eng.trend.bars.append({"t": int(r[0]), "o": r[1], "h": r[2],
                               "l": r[3], "c": r[4], "v": r[5]})
        if len(eng.trend.bars) > 1000:
            eng.trend.bars = eng.trend.bars[-1000:]
        eng.trend.last_eval_bar = int(r[0])
        eq = await ex.equity()
        await eng._step_trend({}, r[4], float(eq["equity"]), int(r[0]))

    final = (await ex.equity())["equity"]
    tdb.stop()
    return final, trades, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("hyst_pct", [0.0, 0.2])
async def test_engine_replay_matches_backtest(hyst_pct):
    """一次回放，三组断言：笔数、逐笔进出价、总收益区间。

    h=0.0 是与历史一致的老口径；h=0.2 是【生产实际在跑的口径】。
    两档都要过——只跑 h=0 等于最强的 parity 测试从没验证过生产配置。

    放在同一个测试里而不是用 module 级 fixture：
    asyncio.run() 会清掉事件循环策略，把后面所有 pytest-asyncio 测试搞崩。
    """
    final, trades, rows = await _replay(hyst_pct)
    h = hyst_pct / 100.0
    C = [r[4] for r in rows]
    ideal, pos, entry = [], False, 0.0
    for i in range(NL + 1, len(C)):
        s_now = sum(C[i - NL:i]) / NL
        s_prev = sum(C[i - 1 - NL:i - 1]) / NL
        # 参照实现必须建模滞回带，且出场用压低档（与 want_exit 一致）。
        # 以前这里写的是裸均线比较，只在 h=0 时才对得上。
        enter_now = C[i] > s_now * (1.0 + h)
        enter_prev = C[i - 1] > s_prev * (1.0 + h)
        exit_now = C[i] <= s_now * (1.0 - h)
        if not pos and enter_now and not enter_prev:
            pos, entry = True, C[i]
        elif pos and exit_now:
            ideal.append((entry, C[i]))
            pos = False
    # 1) 引擎笔数必须等于参照实现的笔数（不再用写死的 181：
    #    那个数字钉的是"参照实现没变"，不是"引擎等于回测"）
    assert len(trades) == len(ideal), (
        u"h=%.2f%% 笔数不一致：参照实现 %d 笔，引擎 %d 笔"
        % (hyst_pct, len(ideal), len(trades)))
    if hyst_pct == 0.0:
        assert len(trades) == 181, "h=0 的历史基准变了：%d 笔（应为 181）" % len(trades)

    # 2) 逐笔进出价必须完全一致——这是"信号口径没跑偏"的硬证据
    assert len(ideal) == len(trades)
    for k, ((ie, ix), (ae, ax)) in enumerate(zip(ideal, trades)):
        assert abs(ie - ae) < 0.01 and abs(ix - ax) < 0.01, (
            "第 %d 笔不一致：回测 %.1f->%.1f，引擎 %.1f->%.1f"
            % (k + 1, ie, ix, ae, ax))

    # 3) 总收益。只用不含拟合成分的断言，区间不再写死成某个 h 的实测值。
    mult = final / 100.0
    assert mult > 1.0, "h=%.2f%% 六年 %.2f 倍：策略在这个口径下不赚钱了" % (hyst_pct, mult)
    if hyst_pct == 0.0:
        # h=0 是与历史对齐的基准，保留当年的实测区间做回归护栏。
        # 现货步长 0.00001 几乎不产生欠配，实测 5.713 倍 vs 回测 5.745 倍。
        # 更早以前这条是 4.0~8.0，因为 paper 用永续步长 0.001 撮合。
        assert 5.2 < mult < 6.3, "h=0 六年 %.2f 倍偏离基准 5.745 倍太多" % mult


@pytest.mark.asyncio
async def test_hysteresis_beats_baseline_on_real_data():
    """标定的核心主张：h=0.2% 在这段真实历史上应当优于 h=0。

    这条断言不是从某次运行结果拟合出来的——它检验的是
    config.py 里记录的那次实测（CAGR 41.70% -> 45.06%，笔数 213 -> 147）。
    如果哪天这个方向反了，说明参数或数据出了问题，必须重新标定。
    """
    f0, t0, _ = await _replay(0.0)
    f2, t2, _ = await _replay(0.2)
    assert f2 > f0, (
        u"h=0.2%% 六年 %.2f 倍，没有优于 h=0 的 %.2f 倍 —— "
        u"与 config.py 记录的标定结论相反，必须重新核对参数" % (f2, f0))
    assert len(t2) < len(t0), (
        u"h=0.2%% 交易 %d 笔，不少于 h=0 的 %d 笔 —— "
        u"滞回带应当减少擦边交易" % (len(t2), len(t0)))
    print("h=0: %.2f 倍/%d 笔    h=0.2%%: %.2f 倍/%d 笔" % (f0 / 100, len(t0), f2 / 100, len(t2)))
