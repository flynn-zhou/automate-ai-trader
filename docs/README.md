# automate-ai-trader 文档索引

## 交接入口（新会话只读这一份就能接上）

    docs/HANDOFF-DSH-SESSION.md

## 上一版交接文档

原来的 `docs/HANDOFF-NEXT-SESSION.md`（2026-11-02 版，donchian + paper 那一代）
已归档为 `docs/handoff/r133-旧主线-唐奇安与B路径.md`，原位置只留一张指针。
**它写的东西现在不对了**，但两节结论还值钱：唐奇安 7.15 年验证口径、
剥头皮 B 路径"提前挂限价单等着"（唯一活着的候选）。

## 历史日志（拆分了）

原来的 1066 行 `HANDOFF.md` 已按阶段拆进 `docs/handoff/`，**正文一字未改**，
根目录的 `HANDOFF.md` 现在只是一张指针表。

- 只想看现状 → `docs/handoff/00-系统现状.md`
- 想考古某条结论 → `docs/handoff/` 里按主题挑，编号跳跃是正常的
- 拆分逻辑与校验 → `tools/split_handoff.py`（可重跑，会断言 1..N 行无缝覆盖）

## 全部文档

| 文件 | 行数 | 作用 | 什么时候读 |
|---|---|---|---|
| **docs/HANDOFF-DSH-SESSION.md** | 219 | 主交接文档：现状、代码地图、闸门、纪律、新方向 | **第一份** |
| **docs/K线演化-设计讨论稿.md** | 174 | K 线演化：逐根状态机设计、与区间演化的关系、4 个待定项 | 动手做这个能力前 |
| docs/INFRA.md | ~190 | 服务器、隧道、端口、**行情数据位置**、部署、PowerShell 坑 | 连不上或找不到数据 |
| docs/DISCUSSIONS.md | ~286 | 最近四轮讨论：用户的问题 + 我的做法与结果，含我被证伪的想法 | 想知道为什么走这条路 |
| CHECKLIST.md | 短 | 一条命令核对系统还活着 | 每次接手先跑 |
| **docs/handoff/** | 12 份 | 历史开发日志，按轮次编号拆分 | 考古某条结论怎么来的 |
| docs/handoff/00-系统现状.md | 87 | 活文档：状态 / 下一步 / 参数 | 每次接手 |
| REQUIREMENTS.md | — | 冻结的十条基线 + k-trader 反模式清单 | 改动前对照 |
| DECISION_MARKET.md | 178 | 市场层面的决策记录 | 想改品种/周期时 |
| _archive/docs_outdated/ | 2 | **已作废的规范**（v10 三线方案） | 只有想搞清"为什么废掉"时 |

> ⚠️ `_archive/docs_outdated/` 里的东西**不要当现状引用**。那里的 v10 规范
> 已被 v11 四线取代，按它读代码会对不上号。留着是为了记录推导过程。

## 三十秒上手

    1) 起隧道（本地，需要 clash 在 127.0.0.1:4780）
       python tools\tcp_tunnel.py 2222 47.238.212.22 22

    2) 连服务器
       ssh -i D:\workspace\automate-k-trader.pem -p 2222 root@127.0.0.1

    3) 核对系统（必须全绿才往下做）
       cd /home/cryptobot/ai-trader && ./.venv/bin/python tools/verify_alive.py

    4) 看大屏（另一个终端）
       ssh -i D:\workspace\automate-k-trader.pem -p 2222 -N \
           -L 18090:127.0.0.1:8090 -L 18091:127.0.0.1:8091 root@127.0.0.1
       浏览器打开 http://127.0.0.1:18090/

## 关键路径速查

    服务器根目录      /home/cryptobot/ai-trader
    环境变量          /etc/ai-trader/env（600）
    应用日志          /home/cryptobot/ai-trader/logs/ai-trader.log （不是 journalctl）
    历史行情（回测）  /home/cryptobot/ai-trader/data/hist.db   661MB, 7.15 年, 607 万根
    实时行情          /home/cryptobot/ai-trader/data/market.db  8.6MB
    交易记账          /home/cryptobot/ai-trader/data/market_trade.db  6.3MB
    大屏 HTTP         http://127.0.0.1:18090/  （服务器 8090）
    大屏 WebSocket    本地 18091（服务器 8091）