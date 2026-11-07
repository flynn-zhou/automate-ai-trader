#!/bin/bash
# 安装值守进程 + 从老系统继承飞书 webhook
set -e
DEST=/home/cryptobot/ai-trader

# 1) 复用老系统的飞书配置（只取 webhook，不覆盖已有值）
if [ -f /etc/cryptobot/env ]; then
  HOOK=$(grep -E '^FEISHU_WEBHOOK_URL=' /etc/cryptobot/env | head -1 | cut -d= -f2-)
  if [ -n "$HOOK" ] && ! grep -q '^AI_FEISHU_WEBHOOK=' /etc/ai-trader/env 2>/dev/null; then
    echo "AI_FEISHU_WEBHOOK=$HOOK" >> /etc/ai-trader/env
    echo "[sentinel] 已从老系统继承飞书 webhook"
  fi
fi
chmod 600 /etc/ai-trader/env

# 2) 主交易进程也需要 webhook（guard 告警用）
grep -q '^AI_FEISHU_WEBHOOK=' /etc/ai-trader/env || echo "AI_FEISHU_WEBHOOK=" >> /etc/ai-trader/env

# 3) 值守进程
cp "$DEST/deploy/ai-trader-sentinel.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ai-trader-sentinel >/dev/null
systemctl restart ai-trader
sleep 8
echo "--- sentinel ---"
systemctl is-active ai-trader-sentinel
echo "--- trader ---"
systemctl is-active ai-trader
echo "--- 首份报告已写入研究日志 ---"
cd "$DEST" && PYTHONPATH=. .venv/bin/python -m ai_trader.sentinel_daemon --report 2>&1 | tail -2
