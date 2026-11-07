# 基础设施：服务器、隧道、端口、数据位置

> 这一份解决「连不上 / 找不到数据」。策略结论不在这里，在 HANDOFF-NEXT-SESSION.md。

## 一、服务器

| 项 | 值 |
|---|---|
| 公网地址 | 47.76.238.212:22 |
| 密钥 | D:\workspace\automate-k-trader.pem（**注意是兄弟项目的目录**） |
| 登录用户 | root（隧道进来之后） |
| 应用用户 | cryptobot |
| 应用根目录 | /home/cryptobot/ai-trader |
| Python | 3.10.12，venv 在 ./.venv |
| 环境变量文件 | /etc/ai-trader/env（权限 600） |

四个 systemd 单元，必须全部 active：

    ai-trader                    主服务，Restart=always，RestartSec=5
    ai-trader-sentinel           哨兵
    ai-trader-healthcheck.timer  健康自检
    ai-trader-snapshot.timer     快照

**应用日志在 /home/cryptobot/ai-trader/logs/ai-trader.log，不在 journalctl。**
这一条坑过很多人，journalctl 里只有 systemd 自己的起停记录。

## 二、网络：为什么必须走隧道

本机直连 47.76.238.212 的 22 端口会握手卡死（Windows 上的 ssh ProxyCommand stdio 问题）。
解决办法是本地先起一个纯 socket 转发器，再用它做 SSH 端口。

    # 终端 A（一直开着）
    python tools\tcp_tunnel.py 2222 47.238.212.22 22

    # 终端 B
    ssh -i D:\workspace\automate-k-trader.pem -p 2222 root@127.0.0.1

tcp_tunnel.py 的原理：本地监听 2222 → 每个连接经 clash 代理（127.0.0.1:4780）
建 CONNECT 隧道到 47.76.238.212:22 → 双线程双向转发。属于纯 socket 转发，
不经过 ssh 的 ProxyCommand stdio，绕开 Windows 握手卡顿。

**前置条件：clash 必须在 127.0.0.1:4780 上跑着。** 本机跑任何要联网的脚本都需要
    AI_HTTP_PROXY=http://127.0.0.1:4780
**服务器上绝对不能设代理**（AI_HTTP_PROXY 必须为空，否则币安接口全挂）。

隧道会因网络抖动或重启服务断掉，等 15~90 秒再重连即可。

## 三、看板端口（两个，别搞混）

| 端口 | 绑定 | 作用 |
|---|---|---|
| 8090 | 0.0.0.0 | HTTP 大屏 + REST API（Flask/waitress） |
| 8091 | 0.0.0.0 | WebSocket 实时推送（aiohttp） |

本地访问方式：

    ssh -i D:\workspace\automate-k-trader.pem -p 2222 -N \
        -L 18090:127.0.0.1:8090 -L 18091:127.0.0.1:8091 root@127.0.0.1

然后浏览器打开 **http://127.0.0.1:18090/** ，标题是「AI Trader · 3s 行情 + 交易因果大屏」。
前端 board.js 会自动把端口 +1 当 WS 端口，所以两个隧道缺一不可，只开 18090 会连不上实时流。

**当前 AI_WEB_TOKEN 是空的**，所以不需要 token 就能访问 —— 这意味着 8090/8091 是裸奔的，
ufw 也没放行规则（靠绑定在 0.0.0.0 上但被云安全组挡着）。这是待办。

### REST 端点全清单（ai_trader/web/app.py）

    GET  /                     大屏页面
    GET  /static/<path>        静态资源
    GET  /api/health           健康
    GET  /api/bars             K 线数据（3s/15s/1m/3m/15m/1h/4h）
    GET  /api/events           喂数事件
    GET  /api/snapshot         行情快照
    GET  /api/intervals        可用周期
    GET  /api/trade/state      **交易状态主接口**（策略/上下轨/计划/风控闸）
    GET  /api/trade/decisions  最近决策
    GET  /api/trade/trades     成交记录
    GET  /api/trade/equity     权益曲线
    GET  /api/guard            风控闸状态
    POST /api/guard/reset      重置风控闸
    GET  /api/journal          日志/纪事

## 四、行情数据位置（这是最常被问的）

全部在 **/home/cryptobot/ai-trader/data/** 。

| 文件 | 大小 | 内容 | 谁写 |
|---|---|---|---|
| **hist.db** | 661 MB | **历史 K 线（回测用）** | 一次性灌入，不更新 |
| market.db | 8.6 MB | 实时 3s/15s/1m/3m/15m/1h/4h K 线 | 服务实时写 |
| market_trade.db | 6.3 MB | 交易记账（决策/成交/权益/风控） | 服务实时写 |
| backtest.db | 7.9 MB | 旧回测缓存 | 一次性 |
| spot.db | 1.3 MB | 旧现货缓存 | 一次性 |
| funding_btcusdt.json | 940 KB | 资金费历史 | 一次性 |

### hist.db（661 MB，回测的唯一数据源）

    表 k       1081170 行   （旧格式，别用）
    表 k2      6074345 行   **主表**：symbol, interval, open_time, o, h, l, c
    表 funding   43988 行   资金费：symbol, ts, rate

k2 覆盖范围：**2019-09-08 ~ 2026-11-02（7.15 年）**

k2 明细（品种 / 周期 / 行数）：

    BNBUSDT   15m:235870   1h:58968   4h:14742   5m:707608
    BTCUSDT   15m:250710   1h:62679   4h:15670   5m:752126
    DOGEUSDT  15m:221369   1h:55343   4h:13836   5m:664107
    ETHUSDT   15m:243070   1h:60769   4h:15193   5m:729209
    PAXGUSDT               1h:14038                        （只有 1h）
    SOLUSDT   15m:215041   1h:53761   4h:13441   5m:645122
    XRPUSDT   15m:239229   1h:59808   4h:14952   5m:717684

**没有 3m 历史。** 3m 只有实时的（market.db 里 180000ms 那部分，从 2026-10-26 开始，
至今 7 天左右）。所以任何 3m 形态都无法做多年回测，只能实盘观察。这是硬边界。

查询示例：

    import sqlite3
    c = sqlite3.connect('/home/cryptobot/ai-trader/data/hist.db')
    rows = c.execute(
        'select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time',
        ('BTCUSDT', '5m')).fetchall()

### market.db（实时采集，8.6 MB）

    bars         67597 行，按 interval_ms 分（3000/15000/60000/180000/900000/3600000/14400000）
    feed_events    837 行
    bar_gaps        14 行   断流记录
    范围：2026-10-26 11:30 ~ 现在

### market_trade.db（交易记账，6.3 MB）

    decisions       12499 行  每次决策
    equity          24731 行  权益曲线（**5 秒节流，不能用它推断循环频率**）
    journal           216 行  纪事
    risk_events       219 行  风控事件
    orders/fills        7 行  真实下单（全来自旧的现货实验）
    trades              4 行  成交（全是旧的现货实验，保留作审计）
    executor_state      1 行  **balance=100.0**（纸面账本，第 131 轮重置过）
    guard_state         1 行  风控闸持久化
    engine_state        0 行  空 = 没有卡住

### 数据层的两个已知问题

1. **54 根历史 K 线数据损坏**（price=0 之类），分布在 hist.db 早期。
2. 实时流的畸形数据会被丢弃并打 WARNING，日志里那种
   「丢弃畸形成交(第 N 条): p=0 q=0 X=NA」是币安推送的空数据，属正常防护，不用管。

## 五、部署流程

> **已迁移到 `docs/DEPLOY.md`，那里是唯一权威。**
>
> 本节只留一句话指针，原因是旧内容有三处与服务器实测不符
>（四个 systemd 单元并非全 active、健康检查走 cron 不走 timer、
>install_server.sh 的默认 env 与生产不符），
>留着会误导。完整流程、四条铁律、回滚、核对清单都在 DEPLOY.md。

    docs/DEPLOY.md
## 六、PowerShell 侧的坑（会浪费很多时间）

1. **不要往 ssh 命令里塞多行 Python**（`ssh host \"python3 -c '<多行>'\"`）——
   pwsh 会把它当自己的语法解析然后报一堆 ParserError。正确做法：写成本地文件 → scp → 按路径跑。
2. `%` 在 pwsh 里有优先级问题：`\"%.2f%%\" % 0.002*100` 会算错，要加括号。
3. `grep -E 'a|b'` 里再套单引号会截断字符串。
4. 服务器上**没有 sqlite3 命令行**，用 Python 查。
5. md5 要用 `.Hash.ToLower()`，Get-FileHash 默认大写。
6. `tools.write` 在复用一个刚删掉的文件名时会报「file no longer exists」，换个新名字。
7. run_code 里的 TypeScript 模板字符串碰到反引号会断，用 String.fromCharCode(96)。