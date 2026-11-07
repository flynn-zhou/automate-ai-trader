# -*- coding: utf-8 -*-
"""把到目前为止的推理过程写进研究日志（看板「研究日志」页可见）。"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_trader.journal import Journal  # noqa: E402

J = Journal(os.path.join("data", "market_trade.db"))
J.start()

E = [
    ("hypothesis", "为什么只留两个信号",
     "k-trader 用 21 个高度相关的价格行为信号 + 10 道否决门，376 条决策只开出 12 笔（3.2%），"
     "而放行的那批里 range_fakeout_reversal 一个信号就亏掉 -214U（占净亏 80%）。"
     "结论不是'信号不够多'，而是'信号太多且互相打架'。\n"
     "新项目只留两个，且每个必须回答'它赚的是谁的钱'：\n"
     " · breakout_3m（突破跟随）赚追单动量盘 + 被打止损的单；k-trader 里 breakout 族是唯一稳定为正的（156 笔 +96U）。\n"
     " · fakeout_fade（插针回归）赚插针打止损的人。k-trader 的同类玩法血亏，但那是 15m 级区间 + 100x + 0.3% 止损；"
     "这里改成 3s 级、止损放在插针外侧、杠杆不拉满，尺度完全对齐。",
     json.dumps({"signals": ["breakout_3m", "fakeout_fade"]}, ensure_ascii=False)),

    ("pivot", "100U 的数学：先承认这是高风险赌注",
     "按 1% 单笔风险（1U）算：最小下单 0.001 BTC ≈ 77.5U 名义，止损必须在 1.29% 以内才刚好。"
     "这个止损宽度落在 15m 波动范围内，会被正常噪音扫掉，等于必输（k-trader 就是这么死的）。\n"
     "算清楚三条路：\n"
     " A 保守：单笔 1U，需要 63% 胜率才正期望 → 做不到，直接出局。\n"
     " B 低风险档：单笔 2U，止损 2.58%，胜率 34%、赔率 0.89 才能不亏 → 勉强，但没有安全边际。\n"
     " C 高风险档（选定）：单笔 35U（本金的 35%），止损 0.45%，目标 +0.85R。\n"
     "    此时赢一次 +32.6U，输一次 -35U，只需要 52% 胜率就正期望；"
     "亏三次≈归零，剩下的 10% 只够手续费。\n"
     "用户说'亏完不要紧'，那就不做注定被噪音扫死的保守版，直接上 C 档，"
     "把命门放在'止损必须真的在行情外'而不是'止损很小'。",
     json.dumps({"risk_pct": 35, "stop_pct": 0.45, "rr": 0.85}, ensure_ascii=False)),

    ("experiment", "第一版风控参数试验（失败）",
     "初版：risk_pct=1.5，min_stop=0.40%，max_stop=1.5%，rr=1.6，cost_multiple=3.5，"
     "min_atr=0.004%。跑 4 分钟（80 根 3s 桶）→ 0 笔开仓，0 个信号。\n"
     "全部卡在'无信号'，连一道路由门都没走到。这不是风控保护，这是空转。",
     json.dumps({"decisions": 80, "opened": 0}, ensure_ascii=False)),

    ("result", "诊断：信号为什么永远不触发",
     "把每根桶的特征 dump 出来看：atr3s_pct 长期在 0.0001~0.0009%，"
     "而波动门要求 min_atr=0.004% 才允许交易 → 99% 的时间被判'波动太小'。\n"
     "更深一层：3s 桶的成交量只有 0.02~2 BTC，而 3m 桶是 30 BTC 量级；"
     "我用 3m 量能 z-score（vol_z_3m）当突破确认，但 3m 桶在 3s 时钟上**不断被更新**，"
     "z-score 分母被实时值自己污染 → 永远到不了 1.0 的阈值。\n"
     "结论：突破确认不能用量能 z-score，要么改口径（前 20 根已收盘 3m 的滚动量，不含当前根），"
     "要么换成价格结构确认。先降阈值把链路跑通，再用真实数据决定留哪个。",
     json.dumps({"atr3s_pct_range": [0.0001, 0.0009], "min_atr_pct": 0.004}, ensure_ascii=False)),

    ("verify", "桶正确性对账（这条过不了，后面全是假的）",
     "拿同一分钟的官方 REST K 线与我们的逐笔聚合逐项比：\n"
     " · 00:09 成交量 19.626 vs 19.629（差 0.015%）；主动买 11.706 vs 11.709（差 0.026%）；\n"
     " · 00:10 成交量 16.062 vs 16.060（差 0.012%）；主动买 10.057 vs 10.055（差 0.020%）；\n"
     " · 收盘价完全一致；taker 买量口径完全一致。\n"
     "差额来自窗口边界（我们只有最近 1000 笔）。结论：3s 桶是真事实，可作为唯一数据源。",
     json.dumps({"vol_diff_pct": 0.015, "buyvol_diff_pct": 0.026}, ensure_ascii=False)),

    ("alert", "工程事实：币安合约 WS 在本网络零推送",
     "fstream.binance.com:443 握手成功但 0 条消息（本机经代理、生产服务器直连都一样）；"
     "官方测试网 WS 反而能收到。所以'3s 数据'不能依赖 WS。\n"
     "改用 REST /fapi/v1/aggTrades，按 aggTradeId 游标增量拉：实测 5.6 笔/秒、延迟 238ms，"
     "每 3s 一轮约 20 笔，1000 笔容量可覆盖 3 分钟断流 → 3s 桶里照样有真实成交量与主动买卖方向。\n"
     "WS 保留为加速通道，谁健康用谁，看板可见降级。这条直接抄 k-trader 的'WS 只保活、REST 保确定性'。",
     json.dumps({"ws_msgs": 0, "rest_trades_per_sec": 5.6, "rest_latency_ms": 238}, ensure_ascii=False)),

    ("note", "账户与环境的硬事实",
     " · 账户余额 0.53 USDT（还没打钱），可用杠杆上限 150x，BTCUSDT 已是逐仓 150x；\n"
     " · 最小下单 0.001 BTC / 最小名义 50 USDT / 步长 0.001 / 价格步长 0.1；\n"
     " · 费率 maker 0.02% / taker 0.05% → 吃单开平一轮 0.10%，用 0.001 BTC(77.5U) 做一轮 = 0.0775U；\n"
     " · 维持保证金率首档 0.4% → 150x 理论强平距离约 0.27%，10x 约 9.6%；\n"
     " · 老系统 cryptobot 当前 inactive/disabled，账户干净。",
     json.dumps({"balance": 0.53, "max_leverage": 150, "taker": 0.0005}, ensure_ascii=False)),

    ("experiment", "第二阶段试验：打通开仓链路",
     "放开到 risk_pct=35%、止损 0.15%~3.0%、rr=0.85、min_atr=0.0005%，"
     "并把'无信号'改成带数字的'盯盘中'：距上破还差 x%、距下破还差 y%、量能 z=z。\n"
     "这样即使不开仓，看板上也能看到'系统此刻在等什么'——k-trader 的 96.7% 观望没有任何解释，"
     "这里必须每一根桶都说清楚为什么没动手。",
     json.dumps({"risk_pct": 35, "watch_state": True}, ensure_ascii=False)),
]

for kind, title, body, data in E:
    jid = J.add(kind, title, body, data)
    print(jid, kind, title)

J.stop()
