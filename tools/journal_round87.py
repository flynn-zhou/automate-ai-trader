# -*- coding: utf-8 -*-
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_trader.journal import Journal
L = json.loads(r'''["这轮验的是「注资后敢不敢开」那道闸门。","","=== 一、闸门本身在工作 ===","","在服务器上跑真实的 tools/preflight_live.py（当前确实没有密钥）：","","    [NO-GO] AI_API_KEY / SECRET  缺失","    硬阻断 1 项   提醒 1 项","    结论：先解决上面的硬阻断，不要切 live","    退出码 1","","这一条是对的。**但它也是唯一一条被真实运行验过的**——脚本在凭证处就 return 了，","后面 6 个硬阻断从来没执行过：","","    取不到牌价 / 交易对规则取不到 / 账户查询失败","    USDT 余额为 0 / 够不够开一手 / 按权益的计划开不出来 / 时钟偏差过大","","一个从没红过的闸门不算闸门。而这些正是「注资后敢不敢开」的实际依据。","","=== 二、逐条做失败测试 ===","","把脚本当模块导入（只有顶层函数定义，import 安全），直接调检查函数注入坏输入。","新增 tests/test_preflight_blocks.py 共 10 条。","","过程中报了两条失败，**两条都是我的测试前提错了**：","","错 1：我用现货口径测「计划不可行」。","  权益 10U 配 trend_min_notional=55，我以为该拦——但 _min_notional()","  对现货返回 5.0（第 55 轮查过并记录），10U 在现货下确实开得出仓（9.36U 名义）。","  要测这个阻断必须用永续口径。","","错 2：我造了 server_time() 的桩，而 check_misc 走的是 ex._req(\"GET\",\"/api/v3/time\")。","","顺带补了一条对照用例：现货下 10U 必须【放行】。","","=== 三、双向变异：会拦，也会放行 ===","","只测「会拦」不够——一个永远报错的闸门能通过全部拦截断言。所以两个方向都变异：","","  该拦必须拦（8 条全红）：","    牌价失败放行 / 交易规则失败放行 / 账户查询失败放行 / 余额为 0 放行","    开不出一手放行 / 计划不可行放行 / 时钟偏差放行 / 时钟阈值放宽到 1 小时","","  不该拦不许误拦（3 条全红）：","    资金永远报 NO-GO / 计划永远不可行 / 时钟永远报偏差","","11 条变异全部变红。","","=== 四、验证 ===","","全量 438 条全绿（428 -> 438）。","服务器实跑：缺密钥 -> NO-GO、退出码 1。","","账户仍未到账（现货 USDT = 0）。"]''')
meta = json.loads(r'''{"round":87,"theme":"实盘体检闸门做失败测试：6 个硬阻断从未被执行过，逐个验证会拦且不误拦","live_run":{"result":"[NO-GO] AI_API_KEY / SECRET 缺失","exit_code":1},"gap":"脚本在凭证处 return，后续 6 个硬阻断从未被任何真实运行执行","new_tests":10,"my_errors":["用现货口径测「计划不可行」：_min_notional() 现货返回 5.0（第55轮已记录），10U 确实开得出仓，要测必须用永续口径","给 check_misc 造了 server_time() 桩，实际走 ex._req('GET','/api/v3/time')"],"mutation":{"must_block":"8/8 红","must_not_false_block":"3/3 红","total":"11/11"},"tests_total":438,"account_state":{"spot_usdt":0}}''')
J = Journal('data/market_trade.db')
J.start()
J.add('info', u'实盘体检闸门失败测试：6 个硬阻断从未被执行过，逐个验证',
      chr(10).join(L), json.dumps(meta, ensure_ascii=False))
print('journal ok')
J.stop()