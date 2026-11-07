# -*- coding: utf-8 -*-
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_trader.journal import Journal

L = [
 u"前几轮都在修缺陷。这轮换个方向：把「明天注资会怎样」用最近的真实数据量化出来。",
 u"",
 u"为什么这件事缺：我有 6.39 年的回测，但那是全历史平均。",
 u"用户真正想知道的是「最近这段行情里，这套东西表现如何」。",
 u"这两个问题的答案可能完全不同。",
 u"",
 u"=== 先算当前状态（用线上真实的 state()，不自己重写）===",
 u"  最新 4h 收盘   77976.22",
 u"  SMA200         82323.7728",
 u"  偏离           -5.28%",
 u"  above=False  prev_above=False  want_entry=None",
 u"",
 u"顺手做了一次交叉验证：我自己按 mean(最近200根含最后) 再算一遍，",
 u"得 82293.75，与 state() 差 30.02——正是第 49 轮踩过的约定陷阱。",
 u"所以这次直接调 state()，不自己写一遍。",
 u"",
 u"=== 近期纸面样本（线上同一份引擎 + 同一套成本）===",
 u"驱动方式照抄 tools/rehearse.py（第 50 轮验证过能跑出 232 笔的那种）：",
 u"真实 Bar 对象、直接调 _step_trend、只按 4h 收盘判断。",
 u"",
 u"第一次跑出来【四个窗口全是 0 笔】，我没有当成「策略这段没信号」就收工。",
 u"查下去是我自己的脚手架坏了：我把 K 线当 dict 传进去，",
 u"而 on_bar 里第一句是 getattr(bar, open_time, None)，dict 拿不到就 return False。",
 u"于是每根都被静默丢弃——不报错、不警告、看起来像「策略很安静」。",
 u"（这和 bar.vol 那次事故是同一种形状：静默跳过，系统看起来在跑。）",
 u"",
 u"改成真实 Bar 后，数字才有意义：",
 u"",
 u"  窗口    价格变动    100U 变成    笔数      结论",
 u"  3 个月   +22.8%     116.16U     9    跑输买入持有",
 u"  6 个月    -1.0%     106.56U    18    跑赢",
 u"  12 个月  -29.3%     100.36U    27    大幅跑赢（超额 29.7pp）",
 u"  24 个月  +12.5%      92.59U    60    跑输",
 u"",
 u"这组数把策略的真实性格说清楚了：**涨市跑输、跌市保命。**",
 u"12 个月那个窗口最有说服力：BTC 跌 29.3%，策略 +0.4%。",
 u"这正是「崩盘保险」该有的样子——它不是收益放大器。",
 u"",
 u"而且换手率对得上：9/18/27/60 笔，约每月 2 笔，与回测的 232 笔/6.39 年一致。",
 u"手续费也很轻：12 个月只花了 4.96U。",
 u"峰值回撤 11.2%~14.0%，远低于全历史的 47.7%——",
 u"因为最近两年没遇到 2021 那种深度回撤。诚实地说：这不代表以后最大回撤只有 14%。",
 u"",
 u"=== 用户最关心的：注资后多久会有第一笔 ===",
 u"用看板同一个模块 expected_wait 查（不是我自己另算一套）：",
 u"  偏离 -5.28% -> 中位 9.0 天，P90 35.7 天，最长 55.7 天（327 个独立样本）",
 u"",
 u"所以诚实的回答是：注资后大概率要等一周半左右，",
 u"运气差要等一个多月。这段等待里系统什么都没做错——它在等价格涨 5.5% 上穿均线。",
 u"",
 u"=== 验证 ===",
 u"264 个测试仍全绿（本轮无代码改动，只有新工具与结论）。",
 u"新增常驻工具 tools/recent_sample.py，以后随时可以重跑这段对比。",
]

J = Journal("data/market_trade.db")
J.start()
meta = {
  "purpose": "Quantify what funding tomorrow would look like using recent real data, since the 6.39-year backtest is an all-history average and the recent regime may differ completely.",
  "current_state": {"last_4h_close": 77976.22, "sma200": 82323.7728, "deviation_pct": -5.28, "above": False, "prev_above": False, "want_entry": None},
  "convention_check": "Recomputed SMA my own way (mean of last 200 including the latest bar) = 82293.75, differing from state() by 30.02 -- the exact round-49 convention trap. Used state() instead of trusting my arithmetic.",
  "harness_bug": "First run produced 0 trades in all four windows. Not 'the strategy was quiet': my scaffold passed klines as dicts, and on_bar's first line is getattr(bar, 'open_time', None) which returns None for a dict, so it returned False for every bar. Silent skip, no error -- same shape as the bar.vol incident.",
  "recent_samples": {"3m": {"price_pct": 22.8, "equity": 116.16, "trades": 9, "verdict": "lags buy-hold"}, "6m": {"price_pct": -1.0, "equity": 106.56, "trades": 18, "verdict": "beats"}, "12m": {"price_pct": -29.3, "equity": 100.36, "trades": 27, "verdict": "beats by 29.7pp"}, "24m": {"price_pct": 12.5, "equity": 92.59, "trades": 60, "verdict": "lags"}},
  "character": "Rising markets: lags. Falling markets: protects. The 12m window (BTC -29.3 pct, strategy +0.4 pct) is the clearest evidence it is crash insurance, not a return amplifier.",
  "turnover_check": "9/18/27/60 trades over 3/6/12/24 months ~ 2 per month, consistent with the 232 trades over 6.39 years in the backtest. Fees are light: 4.96 U over 12 months.",
  "drawdown_caveat": "Peak drawdown in these windows is 11.2-14.0 pct, far below the all-history 47.7 pct -- because the last two years had no 2021-style episode. This does NOT mean future max drawdown will be 14 pct.",
  "first_entry_wait": "expected_wait at -5.28 pct (the board's own module, 327 samples): median 9.0 days, P90 35.7 days, max 55.7 days.",
  "verification": {"tests": 264, "code_changes": 0, "new_tool": "tools/recent_sample.py"},
  "tests": 264}
J.add("info", u"近期纸面样本：涨市跑输、跌市保命；注资后中位等 9 天才有第一笔",
      u"\n".join(L), json.dumps(meta, ensure_ascii=False))
print("journal ok")
J.stop()
