# 接手核对清单（一条命令）

新会话接手第一件事，跑这一条。全部 OK 再谈策略。

    cd /home/cryptobot/ai-trader && ./.venv/bin/python tools/verify_alive.py

输出 19 项检查，覆盖：四个服务 / 日志 ERROR / 大屏 API（策略、周期、上下轨、计划、风控闸）/
env 回读（读文件不读内存）/ 记账库余额。

**当前实测结果：通过 19 项，失败 0 项，结论「系统正常」。**

如果这一条不是全绿，先修好它，不要往下做策略研究。
理由：2026-11-02 唐奇安上线当晚，12 个单元测试全绿、服务 active、看板有数据，
而引擎每 3 秒抛一次 KeyError、一次决策都没评估过。
「活着」和「在干活」是两件事，这条脚本查的是后者。

## 需要单独确认的两件事（脚本查不出）

1. 大屏是否有意识地在推进 —— 隔 10 秒跑两次，看 decisions 是否增长：
       ./.venv/bin/python tools/verify_alive.py | grep decisions
2. 唐奇安口径是否与回测逐笔一致（改动策略后必跑）：
       ./.venv/bin/python tools/selfcheck_donchian2.py
   期望：回测口径开仓 128 次 == 策略口径开仓 128 次
