# 生产部署文档

> 2026-11-07 按服务器**实测**重写。旧 `docs/INFRA.md` 第五节有若干已过期之处，
> 以本文为准（差异见「十、与旧文档的出入」）。

## 一、生产环境速查

| 项 | 值 |
|---|---|
| 服务器 | `47.76.238.212:22`（直连 22 不通，必须走隧道） |
| 密钥 | `D:\workspace\automate-k-trader.pem` |
| 应用目录 | `/home/cryptobot/ai-trader`（属主 `cryptobot:cryptobot`） |
| venv | `/home/cryptobot/ai-trader/.venv`（Python 3.10.12） |
| 环境变量 | `/etc/ai-trader/env`（`600`，属主 `root:root`） |
| 应用日志 | `/home/cryptobot/ai-trader/logs/ai-trader.log`（**不是 journalctl**） |
| 健康日志 | `/home/cryptobot/ai-trader/logs/healthcheck.log` |
| Web 端口 | `8090`（Flask） |
| WS 端口 | `8091`（看板推送） |
| 对外域名 | `cbot.speaklingo.top`（Caddy 反代） |
| 磁盘 | 59G，当前用 12G（22%）；`data/` 占 747M，`.venv` 占 38M |

## 二、连接（第一个卡点）

直连 `47.76.238.212:22` 不通，必须本地起隧道：

    # 终端 A：起隧道（需要本机 clash 在 127.0.0.1:4780）
    python tools/tcp_tunnel.py 2222 47.76.238.212 22

    # 终端 B：连
    ssh -p 2222 -i D:\workspace\automate-k-trader.pem root@127.0.0.1

看板另开一条端口转发：

    ssh -p 2222 -i D:\workspace\automate-k-trader.pem -N \
        -L 18090:127.0.0.1:8090 -L 18091:127.0.0.1:8091 root@127.0.0.1

然后浏览器开 `http://127.0.0.1:18090`。

**隧道会随会话断。** 断线表现为 ssh 卡住或 `Connection refused`，
不是服务器挂了，先检查隧道进程。

## 三、必须知道的四条铁律

### 铁律 1：`scp` 一次只能传一个源文件，且目标必须写全路径

多个源文件丢到同一个目录会把 basename 拍平。**这个事故真实发生过**：

    # 错误：app.py / dashboard.py / live.py 全部被拍进 ai_trader/ 根目录
    scp ai_trader/web/app.py ai_trader/web/dashboard.py ai_trader/web/live.py \
        root@host:/home/cryptobot/ai-trader/ai_trader/

正确做法：一个文件一条命令，目标写完整路径。

    scp -P 2222 -i $k ai_trader/web/app.py \
        root@127.0.0.1:/home/cryptobot/ai-trader/ai_trader/web/app.py

`tests/test_deploy_layout.py` 就是为这件事立的：它会检查有没有文件被拍到错误层级。

### 铁律 2：`md5sum` 必须双向比对

**服务 `active` 不等于部署成功。** 曾经出现过代码没传上去但服务正常重启、
看起来一切正常的情况。

    # 本地（注意 Get-FileHash 默认大写，要 ToLower）
    (Get-FileHash ai_trader/web/app.py -Algorithm MD5).Hash.ToLower()
    # 服务器
    md5sum /home/cryptobot/ai-trader/ai_trader/web/app.py

两边不一致就是没传成功，不要往下走。

### 铁律 3：`data/` 永远不部署

`data/` 是运行时数据库（`market.db` / `market_range.db` / `market_trade.db` / `hist.db`），
747M，而且是**生产正在写的**。传上去会覆盖行情与成交记录。

打包时显式排除，或用白名单只打代码目录。

### 铁律 4：**绝不 `systemctl stop`** 这个服务

停它会卡在 `stop-sigterm` 约 90 秒，期间 SSH 会掉。
**用 `restart`，不要用 `stop` + `start`。**

## 四、标准部署流程

### 4.1 部署前：本地自检

    # 1) 全仓测试必须绿
    python -m pytest tests -q

    # 2) 语法与导入自检（不要等到服务器上才发现语法错）
    python -c "import ai_trader.main"

    # 3) 看工作区状态，确认你改的就是你想部署的
    git status --short

### 4.2 传文件（推荐：逐文件 scp，最不容易出错）

    $k = "D:\workspace\automate-k-trader.pem"

    scp -P 2222 -i $k ai_trader/trading/gates.py `
        root@127.0.0.1:/home/cryptobot/ai-trader/ai_trader/trading/gates.py

需要整体更新时用 tar 白名单（**`data/` 不在列表里**）：

    tar -czf D:/workspace/ai-trader-dist.tar.gz `
        --exclude=__pycache__ --exclude=live_state.json `
        -C D:/workspace/automate-ai-trader `
        ai_trader tests tools deploy config docs

    scp -P 2222 -i $k D:/workspace/ai-trader-dist.tar.gz root@127.0.0.1:/tmp/

    ssh -p 2222 -i $k root@127.0.0.1 "cd /home/cryptobot/ai-trader && `
        tar -xzf /tmp/ai-trader-dist.tar.gz && `
        chown -R cryptobot:cryptobot ai_trader tests tools deploy config docs"

> **tar 的风险**：它是整体覆盖，解错一层就会大面积替换。
> 改动少时优先逐文件 scp。

### 4.3 核对 md5

    # 本地
    $files = @("ai_trader/trading/gates.py", "ai_trader/trading/engine.py")
    foreach ($f in $files) {
      $h = (Get-FileHash $f -Algorithm MD5).Hash.ToLower()
      "$($f.Split("/")[-1]) $h"
    }

    # 服务器
    ssh -p 2222 -i $k root@127.0.0.1 `
        "cd /home/cryptobot/ai-trader && md5sum ai_trader/trading/gates.py ai_trader/trading/engine.py"

### 4.4 重启前先在服务器上验导入

    ssh -p 2222 -i $k root@127.0.0.1 `
        "cd /home/cryptobot/ai-trader && .venv/bin/python -c 'import ai_trader.main; print(1)'"

**这一步不能省。** 语法错或循环导入在这里暴露，而不是在服务反复重启的日志里。

### 4.5 重启并验证

    ssh -p 2222 -i $k root@127.0.0.1 "systemctl restart ai-trader"
    # 等 15 秒以上（有初始化与行情预热）
    ssh -p 2222 -i $k root@127.0.0.1 "systemctl is-active ai-trader"

    # 自检脚本
    ssh -p 2222 -i $k root@127.0.0.1 `
        "cd /home/cryptobot/ai-trader && .venv/bin/python tools/verify_alive.py"

### 4.6 验证端点真的通（不能只看服务 active）

    # 健康端点（配了 AI_WEB_TOKEN 要带令牌）
    ssh -p 2222 -i $k root@127.0.0.1 `
        "TOK=\$(grep -E ^AI_WEB_TOKEN= /etc/ai-trader/env | cut -d= -f2-); `
         curl -s -m 5 \"http://127.0.0.1:8090/api/health?token=\$TOK\""

    # 页面（登录后）
    curl -s -b /tmp/cj http://127.0.0.1:8090/admin/ | head -20

## 五、服务与守护

### 5.1 当前实际状态（实测）

| 单元 | 状态 | 作用 |
|---|---|---|
| `ai-trader` | **active** | 主进程：行情 + 引擎 + Web |
| `ai-trader-sentinel` | **active** | 哨兵守护，`--interval 300` |
| `ai-trader-snapshot.timer` | **inactive** | 已停用 |
| `ai-trader-healthcheck.timer` | **inactive** | 已停用，改用 cron |

**注意：旧文档写“四个全 active”，那是过期信息。**

### 5.2 健康检查走 cron，不是 systemd timer

`/etc/cron.d/ai-trader`：

    */10 * * * * root bash /home/cryptobot/ai-trader/deploy/healthcheck.sh >> /home/cryptobot/ai-trader/logs/healthcheck.log 2>&1

每 10 分钟一次，输出进 `logs/healthcheck.log`。
**查健康不是看 systemctl，是看这个日志。**

### 5.3 主服务单元

    [Service]
    Type=simple
    User=cryptobot
    WorkingDirectory=/home/cryptobot/ai-trader
    EnvironmentFile=-/etc/ai-trader/env
    ExecStart=/home/cryptobot/ai-trader/.venv/bin/python -m ai_trader.main
    Restart=always
    RestartSec=5
    StandardOutput=append:/home/cryptobot/ai-trader/logs/ai-trader.log
    StandardError=append:/home/cryptobot/ai-trader/logs/ai-trader.log

> `EnvironmentFile` 前面那个 `-` 表示文件不存在也不报错。
> **这意味 env 文件缺失时服务照样起来，但会跑在默认配置上** ——
> 这是最容易陷入的陷阱：服务看起来正常，实际连的是纸面模式或错的策略。
> 部署后必须核对 `AI_TRADE_MODE`。

### 5.4 日志与轮转

    tail -f /home/cryptobot/ai-trader/logs/ai-trader.log
    grep -iE "error|异常|失败|拒绝" /home/cryptobot/ai-trader/logs/ai-trader.log | tail -20

轮转配置在 `/etc/logrotate.d/ai-trader`。
**不要用 `journalctl`** —— 日志被重定向到文件了。

## 六、环境变量

`/etc/ai-trader/env`（`600` root，服务以 `cryptobot` 身份通过 systemd 读取）。

### 6.1 当前生产值（脱敏）

    AI_TRADE_MODE=live
    AI_STRATEGY=donchian          # 旧残留，代码保留但不接
    AI_EXEC_MARKET=perp
    AI_SYMBOL=BTCUSDT
    AI_WEB_PORT=8090
    AI_RANGE_TICK_S=60

其余（令牌、账号、API 凭证）不入文档、不入仓库。

### 6.2 改 env 的规矩

    # 1) 先备份
    cp /etc/ai-trader/env /etc/ai-trader/env.bak-$(date +%Y%m%d-%H%M%S)

    # 2) 改完权限必须还是 600
    chmod 600 /etc/ai-trader/env

    # 3) 重启才生效（systemd 只在启动时读 EnvironmentFile）
    systemctl restart ai-trader

**改 env 必须重启，改 Web 里的仓位参数不用重启**（那些在数据库里，引擎每次决策现读）。

### 6.3 凭证纪律

- 只写在 `/etc/ai-trader/env`，**不写进 config、不写进代码、不写进 git**
- 需要临时传递时暂存到 `/tmp`，合并后立刻删除
- 仓库有自动化检查：`git grep` 扫 64 位 hex 与已知凭证形态，零命中才算通过

## 七、对外暴露（Caddy）

    cbot.speaklingo.top {
        reverse_proxy /ws* 127.0.0.1:8091
        reverse_proxy 127.0.0.1:8090
        encode gzip
    }

- `/ws*` 必须**排在前面**，否则 WebSocket 会被普通反代吞掉
- Caddy 自动签发证书，443/80 由 caddy 监听
- 改完 `systemctl reload caddy`（用 reload 不用 restart，不断连接）

**Flask 只监听 0.0.0.0:8090，本身没有 TLS，也没有限流。**
公网访问安全性完全依赖 Caddy + 应用自己的令牌。

## 八、回滚

出问题时按代价从低到高：

    # 1) 只回滚代码（本地有 git 就最省事）
    git checkout -- <file>
    # 然后重新 scp 那一个文件 + md5 核对 + restart

    # 2) 回滚 env
    cp /etc/ai-trader/env.bak-<时间戳> /etc/ai-trader/env
    chmod 600 /etc/ai-trader/env && systemctl restart ai-trader

    # 3) 回滚数据库（最后手段，会丢期间的数据）
    systemctl restart ai-trader    # 先让它松开文件句柄
    cp /root/market_trade.db.bak<轮次> /home/cryptobot/ai-trader/data/market_trade.db
    chown cryptobot:cryptobot /home/cryptobot/ai-trader/data/market_trade.db

**仓库没有版本控制之前，`engine.py` 曾经被一次写操作截断过（1836 → 867 行），
当时只能从服务器把它拉回来。现在有 git 了，改之前先提交。**

## 九、首次部署 / 重装服务器

`deploy/install_server.sh` 可以一键装，但**它只在 env 不存在时才创建 env**，
所以脚本里的默认值不会覆盖现有的生产配置。

**它的默认值是旧的**（`AI_STRATEGY=trend` / `AI_TRADE_MODE=paper` / `AI_EXEC_MARKET=spot`），
和当前生产不符。重装后必须手工核对 env，不要以为装完就是对的。

首次部署顺序：

    1. tar 打包（白名单，不含 data/）→ scp 到 /tmp/
    2. 解包到 /home/cryptobot/ai-trader
    3. 建 venv：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    4. 写 /etc/ai-trader/env（chmod 600）
    5. cp deploy/*.service /etc/systemd/system/ && systemctl daemon-reload
    6. systemctl enable --now ai-trader ai-trader-sentinel
    7. 写 /etc/cron.d/ai-trader（健康检查）
    8. chown -R cryptobot:cryptobot /home/cryptobot/ai-trader
    9. 验证：is-active + verify_alive.py + /api/health

## 十、与旧文档的出入（以本文为准）

| 旧说法 | 实测 |
|---|---|
| 四个 systemd 单元全 active | 只有 `ai-trader` 与 `ai-trader-sentinel` active；两个 timer 已 inactive |
| 健康检查靠 `ai-trader-healthcheck.timer` | 实际走 `/etc/cron.d/ai-trader`，每 10 分钟 |
| `install_server.sh` 的默认 env 可用 | 默认值是 `trend` / `paper` / `spot`，与生产不符 |

## 十一、部署后核对清单

每一条都要过，不要跳过：

    [ ] python -m pytest tests -q            全绿
    [ ] python -c "import ai_trader.main"     本地导入 OK
    [ ] 服务器上再 import 一次                 导入 OK
    [ ] md5 双向一致                           逐文件核过
    [ ] systemctl is-active ai-trader          active
    [ ] tools/verify_alive.py                  通过
    [ ] /api/health 返回 ok                    带令牌
    [ ] grep AI_TRADE_MODE /etc/ai-trader/env  确认模式是你想要的
    [ ] tail 日志没有新增 ERROR/异常           看一眼
    [ ] 看板页面能打开且价格在动               打开 /admin/

**最后两条最容易被跳过，但也最能说明问题：**
服务 active + 端点 200 仍然可能是「进程活着但行情不推」。

## 十二、常见故障速查

| 现象 | 先查什么 |
|---|---|
| ssh 连不上 | 隧道进程还在吗（`python tools/tcp_tunnel.py`）；clash 是否在跑 |
| 服务 active 但看板没数据 | `AI_TRADE_MODE` 是否被 env 缺失悄悄改成默认值 |
| 看板价格不动 | 日志有没有「多品种采集完成」；WS 静默检测是否报警 |
| 一直重启 | 日志尾部有 traceback；先在服务器上 `import ai_trader.main` |
| 401 / 403 | 端点要令牌；令牌从 `/etc/ai-trader/env` 取，不是写死的 |
| 部署后行为没变 | md5 对不上，文件其实没传上去；`__pycache__` 未清 |
| 磁盘涨太快 | `du -sh data logs .venv`；`data/` 747M 属正常 |