# -*- coding: utf-8 -*-
"""纸面执行器必须和现货执行器遵守同一套规则：扣费按市场、不残留、不反向。

为什么查这个（第 109 轮）：
  趋势策略至今 0 笔纸面成交（BTC 一直在 SMA200 下方）。也就是说
  「纸面样本」是我仅有的两个证据来源之一，却还没有任何成交去检验它。
  如果纸面撮合比现实便宜或宽松，将来攒出来的样本会系统性高估收益。

查下来三件事：

1. 扣费是对的，而且是按市场扣的（paper.py:94-98）。
   实测印证：400 次开平、notional 约等于全部权益，
       起始 100.00 -> 结束 41.71，平均每轮约 0.21% 名义成本，
   和 DECISION_MARKET 里「现货单笔约 0.2%」的口径一致。

2. 平仓后不残留仓位（下面第 1 条测试钉住）。
   400 轮随机价格开平实测：残留 0 次、反向 0 次。

3. ★ 真实缺陷：paper.py 接受 reduce_only 但【完全没用它】。

   spot.py 卖出时按可用余额截断（415 行 round_qty(max(0, before))），
   paper.py 既不截断也不按 step 取整。而 engine.py:_close 正是靠
   「执行器会再按 step 向下取整，所以这里多给一点是安全的，不会超卖」
   才敢传 sell_qty = p.qty + dust（1242 行）。这句话对 spot.py 成立，
   对 paper.py 不成立。

   后果：一笔「只减仓」的平仓单在纸面上会把多仓翻成一笔【带着旧均价的空仓】。
   之后 equity() 报「持有空仓」、止损按那个假均价算、决策日志写「持空」，
   而真实交易所根本不可能产生这个状态。

   第 3 条测试先证明了它会翻（改造前红），修好后转绿。

   为什么以前没暴露：dust 只在「纸面持仓余额小于一个 step」时才被认领
   （engine.py:1105-1112），而开仓 qty 已按 1e-05 取整、平仓又卖出全部，
   所以 pos_qty 每次都精确回到 0，dust 恒为 0，这条路过不去。
   但现在有测试钉住了：将来 dust 一旦非 0，立刻红。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from ai_trader.config import load
from ai_trader.exchange.paper import PaperExecutor


class _Agg:
    def __init__(self):
        self.p = 0.0

    def current(self, iv):
        return type("B", (), {"close": self.p})()


class _Feed:
    def __init__(self):
        self.agg = _Agg()
        self.last_price = 0.0


def _ex(px, exec_market="spot", bal=100.0):
    cfg = load()
    cfg.trading.exec_market = exec_market
    cfg.trading.paper_balance = bal
    f = _Feed()
    f.agg.p = px
    f.last_price = px
    return PaperExecutor(f, cfg)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_closing_the_whole_position_leaves_no_residue():
    """平掉全部持仓后，仓位和均价都必须精确归零。

    留一点点残留，下一步 equity() 就会把执行器报成「还持着仓」，
    而引擎的 self.pos 已经是 None，正是 paper.py 开头记的那类错位：
    「一边持着仓一边权益恒为 100，止损触发时会往一个不存在的仓位上发平仓单」。
    """
    for px in (21000.0, 78313.1, 90000.0):
        ex = _ex(px)
        qty = 0.00127
        _run(ex.market_order("buy", qty))
        assert ex.pos_qty == pytest.approx(qty)
        _run(ex.market_order("sell", qty, reduce_only=True))
        assert ex.pos_qty == 0.0, (
            u"价 %r 平掉全部持仓后 pos_qty 仍是 %r" % (px, ex.pos_qty))
        assert ex.pos_entry == 0.0, (
            u"价 %r 平仓后 pos_entry 仍是 %r（应为 0）" % (px, ex.pos_entry))


def test_fee_is_charged_per_market_not_a_single_rate():
    """手续费必须按市场取：现货 0.10%、永续 0.05%，差一倍。

    如果纸面用单一费率，「现货」配置下攒出来的样本会便宜一半，
    结论就是纸面比现实好看——而这正是我要拿纸面样本来印证回测的那件事。
    """
    px = 50000.0
    qty = 0.002

    spot = _ex(px, "spot", bal=1000.0)
    _run(spot.market_order("buy", qty))
    spot_fee = 1000.0 - spot.balance

    perp = _ex(px, "perp", bal=1000.0)
    _run(perp.market_order("buy", qty))
    perp_fee = 1000.0 - perp.balance

    assert spot_fee > 0, u"现货买入没有扣手续费"
    assert perp_fee > 0, u"永续买入没有扣手续费"
    assert spot_fee == pytest.approx(perp_fee * 2, rel=1e-6), (
        u"现货手续费 %.6f 与永续 %.6f 不是 2:1，"
        u"说明纸面没有按市场取费率，样本会比现实好看"
        % (spot_fee, perp_fee))


def test_reduce_only_sell_never_flips_into_a_phantom_position():
    """★ 只减仓的平仓单绝不允许把仓位翻成反向的「幻觉仓位」。

    改造前这条是红的：paper.py 收 reduce_only 却不用它，
    卖 0.00101 而只持 0.001 时，位置变成 -0.00001 的空仓，
    而且 pos_entry 还留着上一个多仓的均价 50000。

    真实交易所（spot.py:415）会按可用余额截断成 0.001，永远不会翻。
    纸面不截断，就等于允许一个现实里不存在的状态进入样本。
    """
    ex = _ex(50000.0, bal=1000.0)
    held = 0.001
    _run(ex.market_order("buy", held))
    assert ex.pos_qty == pytest.approx(held)

    # 模拟 engine.py:1242 的 sell_qty = p.qty + dust
    dust = 0.00001
    _run(ex.market_order("sell", held + dust, reduce_only=True))

    assert ex.pos_qty == 0.0, (
        u"reduce_only 的平仓单把多仓翻成了 %r（带着旧均价 %r）"
        % (ex.pos_qty, ex.pos_entry))
    assert ex.pos_entry == 0.0, (
        u"仓位已平但 pos_entry 仍是 %r" % (ex.pos_entry,))


def test_reduce_only_with_no_position_is_refused_not_faked():
    """空仓时发 reduce_only 必须【明确拒绝】，不能返回一个成功的 0 数量成交。

    变异实测（第 109 轮）：把这里改成返回 ok=True 之后，
    原来 4 条测试【全部仍然绿】——即这个分支没有任何测试。

    为什么它重要：_close 会拿 fill.ok 和 fill.qty 去记账，
    一个 ok=True、qty=0 的"成交"会被写成一条 0 量的平仓记录，
    盈亏为 0、状态却是"已平"，于是引擎把仓位清掉而交易所那边其实没动。
    这正是 paper.py 开头警告的"执行器与引擎对不上"。
    """
    ex = _ex(50000.0, bal=1000.0)
    assert ex.pos_qty == 0.0

    r = _run(ex.market_order("sell", 0.001, reduce_only=True))
    assert r.ok is False, (
        u"空仓时 reduce_only 返回了 ok=%r qty=%r，会被记成一条 0 量平仓"
        % (r.ok, r.qty))
    assert ex.pos_qty == 0.0, u"空仓 reduce_only 之后仓位变成了 %r" % (ex.pos_qty,)


def test_plain_sell_without_reduce_only_still_can_flip():
    """反向对照：不带 reduce_only 的普通卖单保持原行为，不被这次改动影响。

    没有这条，「平仓」和「反手」会被混成同一件事，
    将来若真要做多空翻转，会以为这里已经支持了。
    """
    ex = _ex(50000.0, bal=1000.0)
    _run(ex.market_order("buy", 0.001))
    _run(ex.market_order("sell", 0.002))          # 明确的反手卖单
    assert ex.pos_qty < 0, (
        u"不带 reduce_only 的卖出应当允许反手，实际 pos_qty=%r" % (ex.pos_qty,))
