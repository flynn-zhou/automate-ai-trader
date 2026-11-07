# DSH 会话交接文档 · automate-ai-trader

> 写于 2026-11-07。**新会话读完这一份就能接上，不要先读历史。**
>
> 配套：`docs/INFRA.md`（服务器/隧道/数据位置）、`docs/DISCUSSIONS.md`（为什么走这条路）、
> `CHECKLIST.md`（一条命令核对活着）、`REQUIREMENTS.md`（冻结基线）。

## 零、先读这五条，否则会重复犯错

1. **本项目现在没有任何在生产运行的交易策略，只是记录区间演化。**
   不要以为"系统在跑"就等于"有策略在跑"。引擎活着、闸门活着、但没有开仓信号。
2. **任何「这条路赚钱」的结论必须先剥 beta。** alpha = 条件均值 − 同段行情、同持有期的无条件均值。
   BTC 长期上涨，这个量级足以让任何筛选条件看起来有效。已经栽过一次（把 beta 当 alpha）。
3. **不要用终值判断策略。** 复利是混沌放大器。看单笔均值、t 值、逐年、样本外。
4. **改完必须用探针打印真实值。** 测试通过只说明旧行为没坏，不说明新逻辑生效了。
   本项目已经出现过多次「代码写了但没接线」「测试给了假信心」。
5. **用户的时间是北京时间 UTC+8，数据库和 CSV 是 UTC。** 混用过一次，排查了很久。

## 一、项目是什么

- 工作区：`D:\workspace\automate-ai-trader`（DSH 当前目录）
- 旧项目：`D:\workspace\automate-k-trader`（失败的老系统，**待挖完删除**；.pem 在那里）
- 目标：BTC 永续自动交易，最终要能自主下单与离场
- 用户角色：出钱、提方向、提观察，不做策略实现
- **当前阶段：没有策略。** 已建成的是行情、区间演化、闸门、风控、看板这一层地基

## 二、版本控制（刚建，别再手动改坏文件）

    git@github.com:flynn-zhou/automate-ai-trader.git   main
    远端 523+ 个文件，仓库内无任何凭证

- **改代码前先 `git status`，改坏了 `git checkout -- <file>`。**
- 这个仓库曾经因为一次 `write` 覆盖丢掉过 `engine.py`（1836 行 → 867 行），
  当时没有 git，靠从服务器 scp 拉回才救回来。**大文件优先 `edit` + 备份，不要 `write`。**
- `.gitignore` 已排除：`data/`（运行时库）、`_archive/`（历史脚本）、`*.bak`、凭证。
  例外：`_archive/docs_outdated/` 故意入库，让下一个会话看到"这份作废了"。

## 三、生产现状（交接时刻真实读数）

    ai-trader: active    ai-trader-sentinel: active
    AI_TRADE_MODE=live   AI_STRATEGY=donchian   AI_EXEC_MARKET=perp
    AI_SYMBOL=BTCUSDT    AI_WEB_PORT=8090      AI_RANGE_TICK_S=60

    equity = 0.007127 USDT        <-- 账户几乎是空的，开不出任何一手
    trades 表 4 行                 <-- 全是旧现货实验，作审计保留
    decisions 14368 行

**`AI_STRATEGY=donchian` 是旧系统残留，代码保留但不接。** 用户可以随时注资 100U，
但注资后能开出什么，取决于下面的闸门 —— 而不是"策略会开什么"。

## 四、代码地图（现役 vs 遗留，别搞混）

### 现役

    ai_trader/range/engine.py   区间演化 v11 四线引擎（核心资产）
    ai_trader/range/live.py     实时推进：只写已收盘桶，forming 桶永不落库
    ai_trader/range/store.py    states/bars_1h/events 落库（append_events 是纯 INSERT）
    ai_trader/trading/gates.py  开仓闸门（当前唯一一道：仓位参数）
    ai_trader/trading/sizing.py 风险定仓公式（derive 是页面与闸门的唯一来源）
    ai_trader/guard.py          账户级风控（6 个触发器）
    ai_trader/trading/position_guard.py  单仓位 P0-P5（与 guard 刻意分离）
    ai_trader/web/              Flask + 看板（总览/区间演化/仓位配置/准实时）

### 遗留（代码保留，不接）

    ai_trader/trading/donchian.py   唐奇安通道（用户明确：老系统残留，我们没用）
    ai_trader/trading/trend.py      4h SMA200
    ai_trader/trading/risk.py::build_plan()   6 道闸，但【不在 donchian 路径上】
                                —— 曾经对着它写测试，给了假信心

## 五、区间演化 v11（这是目前唯一真正建成的核心能力）

**四线，显示成 `#fam.seg`：**

| 线 | 字段 | 含义 |
|---|---|---|
| S 软边界 | `soft_up` / `soft_dn` | 软边界 |
| R 反向边界 | `hard_up` / `hard_dn` | 反向边界 |
| P 顺向边界 | `lo_b` / `hi_b` | 顺向边界 |
| K 顺向关键价 | `key` | 顺向关键价 |

**当前最新（2026-11-07 12:00 那根）：**

    S 软边界   79646.4 / 75670.0
    R 反向     79856.1 / None
    K 关键价   75158.0    fam#58.1   dir=DOWN

### 数据布局（踩过的坑，别再踩）

    states 表 11 列： (ts, ts_h, su, sd, hu, hd, key, fam, seg, d, tag)
                      JSON 索引 2/3/4/5/6 分别是 su/sd/hu/hd
    bars_1h 行序：    (ts, ts8, o, h, l, c, v)   <-- 【high 在 low 前面】
                      ECharts 要 [b[2], b[5], b[4], b[3]]
    live bars 行序：  [ts, open, close, low, high]  <-- 【和上面不一样】

- `Store.query_bars()` 返回字典，键是 **`t/o/h/l/c/v`**，不是 `open_time`。
- **1h 桶只在收盘后落库**，当前这根不在本地库，必须去交易所取（见 `web/app.py::_forming_1h`）。
- 时区换算唯一正统：`range/engine.py` 的 `ts8(ms)`。别自己写。

## 六、开仓闸门（当前只有一道：仓位参数）

设计意图：**闸门是最后一道，只否决上游算好的计划，不自己定价。**

### 四个可配参数 → 全部派生量由 `sizing.derive()` 算

    本金 100U  每笔风险 5%（向下取整）  杠杆上限 22x  保证金占比上限

    每笔风险 R       5 U
    可连亏          20 次
    爆仓距离        4.045 %     = (1/22 - 0.005) x 100
    止损距离上限     1.618 %     = 爆仓距离 / 2.5 安全系数
    单笔保证金上限   40 U

**这个 1.618% 是硬约束：止损距离超过它，爆仓会先于止损触发。**
页面（总览底部 / 仓位配置）与闸门读的是**同一份 `derive()`**，有测试钉死不许分叉。

### 已经接上但等策略传值的校验

    stop_distance_pct        真实止损距离（策略接上后填，现在传 0 = 跳过）
    plan_qty / plan_notional 上游算好的计划量（闸门复核，不一致就拒）

原则是**有有效入参才校验**：上游没给就跳过，绝不拿编出来的默认值拦单。

### 已知的坑

- `sizing.py::plan_position()`（风险定仓，`名义 = R / 止损%`）**至今零生产调用**，只有测试在调。
  它是正确的定价器；`_trend_plan` 用的是"权益 x 杠杆"，那是另一个口径。
- 保证金上限的基数用**真实权益**，不是配置本金。
  用配置本金会让账户涨到 30000U 时一笔都开不出（实测踩过）。
- 趋势路径传 `margin_cap_pct=100`，因为 `trend_leverage=1.0` 时保证金恒等于权益的 100%，
  任何小于 100% 的上限都会把每笔拦死。sizing.py 里那个 40% 是风险定仓路径的口径。

## 七、部署与运维（照做，别自创）

    服务器      47.76.238.212:22   密钥 D:\workspace\automate-k-trader.pem
    隧道        python tools/tcp_tunnel.py 2222 47.76.238.212 22   （经 clash 127.0.0.1:4780）
    连           ssh -p 2222 -i <pem> root@127.0.0.1
    路径        /home/cryptobot/ai-trader    venv: ./.venv
    日志        logs/ai-trader.log   【不是 journalctl】
    环境变量    /etc/ai-trader/env (600)

### 铁律

- **`scp` 一次只能一个源文件，且目标写全路径。**
  多个源文件丢到同一目录会把 basename 拍平：曾经把 `app.py/dashboard.py/live.py`
  拍进了 `ai_trader/`，`tests/test_deploy_layout.py` 就是为这件事立的。
- **`md5sum` 双向比对。** 服务 active ≠ 部署成功。
- **`data/` 永远不部署。**
- **绝不 `systemctl stop`** 这个服务 —— 会卡在 stop-sigterm 约 90 秒并掉 SSH。用 `restart`。
- 重启前先 `python -c "import ai_trader.main"`。
- PowerShell 走 ssh：多行 python 用 base64 管道写 `/tmp/x.sh` 再 `bash`，不要在引号里塞多行。

## 八、研究纪律（禁止清单）

| 禁止 | 为什么 |
|---|---|
| A→B→A 循环论证 | 用结果反推前提 |
| 同一段数据既选参数又验证 | 必然过拟合 |
| 网格搜索挑最优格 | 挑到的是噪声极值 |
| `alpha > 0` 当"可盈利" | 没算成本和 beta |
| 回测当终审 | 只是必要条件 |
| 改参数当新想法 | 不是新假设 |
| 把线索当策略 | 线索只值得一次验证 |

**产出单位是「可证伪的假设」，不是「能跑的策略」。** 用户对这条很坚持。

## 九、当前空档，以及新方向

### 空档

没有开仓信号，所以闸门目前只对"钱不够开仓"这一件事生效。
`plan_position()` 缺一个止损距离输入 —— 而止损距离要由开仓信号定义。

### 新方向（本次会话提出，**尚未讨论完，不要直接实现**）

用户想要一个 **K 线识别能力**：给定一根指定周期的 K 线，结合历史背景、形态、
附近 K 线量能，判断：

1. 这根 K 线的意图与目的
2. 是不是恐慌盘 / 恐慌盘是否被清空
3. 带不带量

用户原话：**「不止这3点吧」**，目的是**辅助下单与离场**。

**先讨论，别写代码。** 讨论要点见 `docs/DISCUSSIONS.md` 的文末章节（若尚未写入，
以会话记录为准）。核心争议：

- 「意图」是不可标注的心理归因 → 必须转成**可测量、可样本外打分**的观测量；
- 我们独有的数据优势是 **3s 逐笔（含主动买卖方向 is_buy）**，这是别人拿不到的；
- 识别结果必须绑定**可执行的后续动作**，否则就是看图说话；
- 任何"这个形态之后会涨"都必须先减掉无条件漂移。

## 十、文档地图

    docs/HANDOFF-DSH-SESSION.md   ← 本文（第一份，只读这一份就能接上）
    docs/INFRA.md                 服务器、隧道、端口、行情数据位置、部署
    docs/DISCUSSIONS.md           为什么走这条路（含被证伪的想法）
    docs/README.md                全部文档索引
    docs/handoff/                 历史开发日志，按轮次编号（考古用）
    docs/handoff/00-系统现状.md   活文档：状态/下一步/参数
    CHECKLIST.md                  一条命令核对系统还活着
    REQUIREMENTS.md               冻结的十条基线 + k-trader 反模式清单
    _archive/docs_outdated/       已作废规范（v10 三线方案），不要引用

## 十一、下一步（按优先级）

1. **把 K 线识别的讨论做完，落到可证伪的假设上。** 不要先写分类器。
2. 讨论定稿后再决定：`plan_position()` 要不要接、止损距离从哪来。
3. 用户注资 100U 后，先跑 `tools/preflight_live.py`（注意：它的时钟检查用了现货端点，
   在永续下会 403，需要改成 `/fapi/v1/time`）。
4. `git init` 已完成；`automate-k-trader` 挖完即删，删前先确认没有独有的东西。

## 十二、测试与状态

    本地全仓：907 passed
    新增回归：test_docs_layout / test_gate_downstream / test_sizing_display / test_ws_silence

**测试绿不代表逻辑对。** 已经有多次「测试通过但功能没接线」的记录，
所以规则是：**先写探针打印真实值，再信测试。**