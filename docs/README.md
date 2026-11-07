# automate-ai-trader 文档索引

## 交接入口（新会话只读这一份就能接上）

    docs/HANDOFF-NEXT-SESSION.md

## 全部文档

| 文件 | 行数 | 作用 | 什么时候读 |
|---|---|---|---|
| **docs/HANDOFF-NEXT-SESSION.md** | ~258 | 主交接文档：系统状态、已完成、未完成、路线 | **第一份** |
| docs/INFRA.md | ~190 | 服务器、隧道、端口、**行情数据位置**、部署、PowerShell 坑 | 连不上或找不到数据 |
| docs/DISCUSSIONS.md | ~286 | 最近四轮讨论：用户的问题 + 我的做法与结果，含我被证伪的想法 | 想知道为什么走这条路 |
| CHECKLIST.md | 短 | 一条命令核对系统还活着 | 每次接手先跑 |
| HANDOFF.md | 1065 | 历史开发日志，按轮次编号 | 考古某条结论怎么来的 |
| REQUIREMENTS.md | — | 冻结的十条基线 + k-trader 反模式清单 | 改动前对照 |
| DECISION_MARKET.md | 178 | 市场层面的决策记录 | 想改品种/周期时 |

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
