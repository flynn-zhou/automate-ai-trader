#!/bin/bash
# K 线演化增量补算 + 新鲜度闸。
#
# 为什么要有这个定时器：2026-11-10 ~ 11-16，kbar.db 静默停摆 6.5 天，
# 期间页面照常渲染、所有分析都在旧数据上跑，而没有任何告警。
# 根因是一个必崩的变量遮蔽 bug（已修，见 tests/test_ingest_kbar_cli.py），
# 但**没人盯着这一层**才是它能活 6 天的原因。这个 timer 就是那道盯梢。
#
# 节奏（2026-11-17 改）：
#   每 5 分钟 → 只跑 lines + facts（--until facts，增量、便宜）
#   每整点   → 再跑剩余的全量重算层（reactions/events/forward/shape/verify/
#              survival/dims）。这几层是全表重算，几分钟量级，不能每 5 分钟一遍。
#
# 为什么必须 5 分钟：用户定了「入场全部在 5m K」。演化层晚一小时就绪，
# 等于挂单晚一小时 —— 那不是慢，是策略失效。5m 收盘后几秒内就该能读到。
set -u
cd /home/cryptobot/ai-trader || exit 1
set -a; . /etc/ai-trader/env 2>/dev/null || true; set +a
export PYTHONIOENCODING=utf-8

LOG=logs/kbar_tick.log
HOUR_STAMP=logs/.kbar_full_hour
mkdir -p logs
echo "===== $(date '+%F %T') 开始 =====" >> "$LOG"

RC=0
fail() { echo "  $1 退出码 $2" >> "$LOG"; if [ "${2:-0}" -ne 0 ]; then RC=$2; fi; }

# ---- 轻量路径：lines + facts ------------------------------------------------
# ★ 5m 必须带 --from-1m。两条来源，顺序有意义：
#   market.db 里若有**原生** 5m（2026-11-09 起 config.INTERVALS_MS 才含 300000），
#   ingest 自己走 native 分支；若还没有（老库 / 刚部署），必须显式 --from-1m
#   从 1m 聚合 —— 否则输入源为空。
#   以前这里只传 --interval-ms，5m 那一轮**每次都拿空输入**，而当时 ingest 把
#   空输入当成功（退出码 0），于是 5m 演化层停在 11-01、比 15m 少 7 天，
#   每小时任务全部报成功。两边现在都堵了：ingest 空源 → 退出码 2；这里加回退。
FAST="--until facts"
for IV in 900000 300000; do
  if [ "$IV" = "300000" ]; then
    .venv/bin/python tools/ingest_kbar.py --interval-ms "$IV" --from-1m $FAST >> "$LOG" 2>&1
  else
    .venv/bin/python tools/ingest_kbar.py --interval-ms "$IV" $FAST >> "$LOG" 2>&1
  fi
  fail "$IV" $?
done

# ---- 整点：剩余的全量重算层 ------------------------------------------------
CUR_HOUR=$(date '+%Y-%m-%d %H')
LAST_HOUR=$(cat "$HOUR_STAMP" 2>/dev/null || echo "")
if [ "$CUR_HOUR" != "$LAST_HOUR" ]; then
  echo "  [整点全量] $CUR_HOUR 开始" >> "$LOG"
  for IV in 900000 300000; do
    if [ "$IV" = "300000" ]; then
      .venv/bin/python tools/ingest_kbar.py --interval-ms "$IV" --from-1m >> "$LOG" 2>&1
    else
      .venv/bin/python tools/ingest_kbar.py --interval-ms "$IV" >> "$LOG" 2>&1
    fi
    fail "$IV(full)" $?
  done
  echo "$CUR_HOUR" > "$HOUR_STAMP"
else
  echo "  [整点全量] 本小时已跑过（$CUR_HOUR），跳过" >> "$LOG"
fi

# ---- 新鲜度闸 --------------------------------------------------------------
# 落后超阈值 → 写 kbar_health.json、推飞书、写主日志、unit 变 failed
.venv/bin/python tools/kbar_freshness.py >> "$LOG" 2>&1
FR=$?
echo "  freshness 退出码 $FR" >> "$LOG"

# 日志留最近 2000 行，避免无限增长
if [ -f "$LOG" ]; then tail -2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi

echo "===== $(date '+%F %T') 结束 rc=$RC/$FR =====" >> "$LOG"
if [ "$RC" -ne 0 ]; then exit "$RC"; fi
exit "$FR"
