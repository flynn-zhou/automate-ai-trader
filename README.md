# automate-ai-trader

自主 BTC 永续交易系统。与 `automate-k-trader` 平级独立，复用其服务器与账户，
但交易设计完全另起一套（理由见 `docs/k-trader-failure-notes.md`）。

## 当前阶段：阶段 1 —— 3s 准实时行情 + 实时看板

```
WS aggTrade 逐笔 ─┐
                  ├─> 3s 桶（原子事实）─> 15s/1m/3m/15m 聚合 ─> SQLite + 实时广播
REST 3s 兜底 ─────┘                                              │
                                                       ├─> WebSocket :8091 -> 看板（Canvas 实时画 K）
                                                       └─> Flask API :8090 -> 历史 K / 健康度 / 事件
```

## 运行

```bash
python -m ai_trader.main                 # 常驻（Flask:8090，看板 WS:8091）
python -m ai_trader.main --seconds 30    # 自检 30 秒
```

浏览器打开 http://localhost:8090/

## 数据源健康模型

| 状态 | 含义 | 允许交易 |
|---|---|---|
| WS | aggTrade 有消息且 10s 内 | 是 |
| REST | WS 停摆，3s 轮询取价 | 是（无成交量，需降级判定） |
| DOWN | 两条都不通 | **否，硬闸** |
