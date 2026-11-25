#!/bin/bash
# 服务器一键安装/更新：代码 -> venv -> systemd -> 定时任务
set -e
DEST=/home/cryptobot/ai-trader
mkdir -p "$DEST/data" "$DEST/logs" /etc/ai-trader

[ -f /tmp/ai-trader-dist.tar.gz ] && tar -xzf /tmp/ai-trader-dist.tar.gz -C "$DEST"

[ -x "$DEST/.venv/bin/python" ] || python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"

[ -f /etc/ai-trader/env ] || cat > /etc/ai-trader/env <<'EOF'
AI_LOG_LEVEL=INFO
AI_WEB_PORT=8090
# 区间演化实时推进间隔（秒）；0 = 关闭。默认 60
AI_RANGE_TICK_S=60
AI_WEB_TOKEN=
AI_WEB_USER=admin
AI_WEB_PASS=
AI_SECRET_KEY=
AI_DB_PATH=/home/cryptobot/ai-trader/data/market.db
AI_HTTP_PROXY=
# K 线演化增量编排时记进 kbar_meta，用于复现来源。留空只少 command 一项。
AI_KBAR_BUILD_CMD=
AI_SYMBOL=BTCUSDT
AI_TRADE_MODE=paper
AI_PAPER_BALANCE=100
# 策略与参数：这三行缺了会静默退回 config.py 的默认值。
# 默认值现在都取"被采纳的那一侧"，但显式写出来的好处是
# 一眼能看出这台机器跑的是什么配置。
AI_STRATEGY=trend
# ★ 只做合约（2026-11-17 用户裁决）。spot 会被拒绝启动。
AI_EXEC_MARKET=perp
AI_TREND_HYST=0.2
# 惊喜K线阈值（点，单根 5m 顺向幅度）。Web 配置可改。
AI_SURPRISE_PTS=500
# 挂单策略：1 = 只出计划不挂单。0 = 真单。先跑 1。
AI_PENDING_DRY_RUN=1
# 唐奇安通道参数（AI_STRATEGY=donchian 时生效）。
# 150/75 是 7.15 年 1h 数据上唯一通过全部五道关的那一组，
# 换成邻居参数会掉一档（100/50：每笔 +0.87%，滚动正窗只有 5/7）。
AI_DONCHIAN_ENTRY=150
AI_DONCHIAN_EXIT=75
# 逐笔流名：trade 或 aggTrade。本环境 aggTrade 连得上但不推数据
# （2026-11-02 实测 45s 0 条，同时 @trade 45s 195 条），故用 trade。
AI_WS_STREAM=trade
# 飞书告警 webhook（install_sentinel.sh 会尝试从 /etc/cryptobot/env 自动补）
AI_FEISHU_WEBHOOK=
# 实盘凭证：只在这里填，不要写进代码或配置库
AI_API_KEY=
AI_API_SECRET=
EOF
chmod 600 /etc/ai-trader/env

cp "$DEST/deploy/ai-trader.service" /etc/systemd/system/
cp "$DEST/deploy/ai-trader-snapshot.service" /etc/systemd/system/
cp "$DEST/deploy/ai-trader-snapshot.timer" /etc/systemd/system/
# ★ K 线演化管道。不装它的后果不是报错，而是：
#   kbar.db 从不增长 → S5 三道出场闸永不触发，
#   计划源也构不出开仓窗口（窗口依赖区间 tag）。
#   看板上只会显示“策略没信号”，和真没信号一模一样。
cp "$DEST/deploy/ai-trader-kbar.service" /etc/systemd/system/
cp "$DEST/deploy/ai-trader-kbar.timer" /etc/systemd/system/
chmod +x "$DEST/deploy/healthcheck.sh"
chown -R cryptobot:cryptobot "$DEST"

systemctl daemon-reload
systemctl enable --now ai-trader >/dev/null
systemctl enable --now ai-trader-snapshot.timer >/dev/null
# K 线管道每 5 分钟一次（用户口径：出场全看 5m）。
chmod +x "$DEST/tools/kbar_tick.sh"
systemctl enable --now ai-trader-kbar.timer >/dev/null
systemctl restart ai-trader

# cron：每 10 分钟健康自检
CRON="*/10 * * * * root bash $DEST/deploy/healthcheck.sh >> $DEST/logs/healthcheck.log 2>&1"
grep -qF "healthcheck.sh" /etc/cron.d/ai-trader 2>/dev/null || {
  echo "$CRON" > /etc/cron.d/ai-trader
  chmod 644 /etc/cron.d/ai-trader
}

sleep 12
systemctl is-active ai-trader
# 第 129 轮：/api/* 现在全部要令牌。这里直接裸 curl 会在配了令牌之后拿到 401，
# 而安装脚本会把 401 当成"服务没起来"——一次安全检查变成一次假故障。
# 从 env 文件里取令牌（没配就为空，行为与以前一致）。
TOK=$(grep -E "^AI_WEB_TOKEN=" /etc/ai-trader/env 2>/dev/null | cut -d= -f2-)
Q=""
if [ -n "$TOK" ]; then Q="?token=$TOK"; fi
curl -s -m 5 "http://127.0.0.1:8090/api/health$Q" || echo HEALTH_FAIL
echo
curl -s -m 5 "http://127.0.0.1:8090/api/guard$Q" || echo GUARD_FAIL
