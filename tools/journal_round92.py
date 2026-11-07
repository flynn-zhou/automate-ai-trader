# -*- coding: utf-8 -*-
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_trader.journal import Journal
L = json.loads(r'''["接着上一轮那个盲区做完：页头（行情面）的渲染逻辑，此前零覆盖。","","=== 一、盲区是怎么形成的 ===","","上一轮发现 harness 里的 WebSocket 是纯空桩，而 applyState 只由 WS 推送触发。","于是 board.js 里两块逻辑的覆盖情况差得很远：","","    交易面（pollTrade / fetch）  —— 有 10 个状态的参数化用例","    行情面（applyState / WS）    —— 一个字都没测","","「健康时喊 WS 静默」正是从这个盲区漏出去的。一个字段没盖到，","就能让大屏在最正常的时候说最吓人的话。","","=== 二、这轮把行情面补齐 ===","","用 feed.snapshot()（feed.py:328）的真实键集做 9 个状态：","","    行情正常 / 源是REST降级 / 源全断 / 缺ws_idle_ms / 缺rest_latency_ms","    缺uptime_s / 缺bars / 3s根开价是0 / 缺ws_silent","","断言两件事：","  1) 任何缺字段都不许把 undefined / NaN / Infinity 印到页头","  2) 反过来——行情正常时页头必须真的有数（兜底过度会变成另一种谎）","","另加一条：缺 ws_silent 字段时必须按【健康】兜底。","理由是真正的告警必须有明确信号，不能靠字段缺失触发；","否则升级前的老快照会在前线满屏假告警——那和我上轮修的是同一类错误，只是方向相反。","","=== 三、变异 ===","","4 条全部变红：","  缺字段就喊静默（silent = ws_silent !== false）/ 去掉开价非零保护 /","  空闲秒数不做兜底 / REST 延迟不做兜底","","其中「去掉开价非零保护」验证的是一个具体的除零风险：","3s 根 o=0 时 dp=(c-o)/o*100 会是 NaN，页头会印出 NaN%。","","=== 四、验证 ===","","全量 468 条全绿（457 -> 468）。","","账户仍未到账（现货 USDT = 0）。"]''')
meta = json.loads(r'''{"round":92,"theme":"补齐页头（行情面）渲染覆盖：此前零覆盖，是「健康时喊静默」的漏出通道","asymmetry":{"交易面 fetch":"10 个参数化状态","行情面 applyState/WS":"此前零覆盖"},"new_cases":["行情正常","源是REST降级","源全断","缺ws_idle_ms","缺rest_latency_ms","缺uptime_s","缺bars","3s根开价是0","缺ws_silent"],"rule":"缺 ws_silent 必须按健康兜底：真告警要有明确信号，不能靠字段缺失触发","div_zero_risk":"3s 根 o=0 时 (c-o)/o*100 = NaN，页头会印 NaN%","new_tests":11,"mutation":"4/4 红","tests_total":468,"account_state":{"spot_usdt":0}}''')
J = Journal('data/market_trade.db')
J.start()
J.add('info', u'补齐页头渲染覆盖：此前零覆盖，是「健康时喊静默」的漏出通道',
      chr(10).join(L), json.dumps(meta, ensure_ascii=False))
print('journal ok')
J.stop()