#!/bin/bash
# 服务器侧健康自检：服务活着、数据在动、决策在跑、磁盘够。
# 由 systemd timer 每 5 分钟跑（deploy/ai-trader-healthcheck.timer）；
# 异常时写进研究日志（看板可见）并自动拉起服务。
#
# 2026-11-01 事故 1：这两个"数据是否在动"的检查原本用 sqlite3 命令行，
# 而服务器上根本没装 sqlite3 —— 查询失败后 LAST=0，条件永远不成立，
# 于是检查从上线第一天起就一直是"通过"。哨兵是瞎的，比没有更危险。
# 现在改用 venv 里的 python，并且查不到数据本身就算异常。
#
# 2026-11-01 事故 2：这个脚本从来没被任何定时器调用过。
# 它一直只是我手动执行、然后看输出的东西——
# 「验证了脚本能跑」不等于「验证了系统会自动跑它」。
# 已补 deploy/ai-trader-healthcheck.timer（每 5 分钟）。
#
# 2026-11-01 事故 3：只要发现问题就 exit 1，
# 于是"服务未运行 -> 拉起成功"这种完全正常的自愈执行，
# 在 systemd 里被记成 FAILED。运维看到红色会以为脚本坏了，
# 真正失败时反而没人信。现在只有"拉起后仍起不来"才返回 1。
set -u
DEST=/home/cryptobot/ai-trader
PY="$DEST/.venv/bin/python"
PROBLEMS=""
# 本次执行有没有【真的】重启/拉起过什么。
# 2026-11-02 事故 4：数据停滞时脚本既没重启任何东西，也没有修复任何东西，
# 却照样打印「已自愈」并 exit 0。运维读日志会以为已经好了，
# 而实际上行情/决策仍然停滞，只是还没到自动重启的计数阈值。
# 「说出了什么问题」和「做了什么处置」必须分开讲，不能用一个词糊过去。
HEALED=0

if ! systemctl is-active --quiet ai-trader; then
  PROBLEMS="${PROBLEMS}服务未运行;"
  # Restart=always + StartLimitBurst=5 意味着短时间内崩 5 次后 systemd 会永久放弃。
  # 必须 reset-failed 清掉计数再 start，否则所谓的自动重启在崩溃循环面前是失效的。
  systemctl reset-failed ai-trader 2>/dev/null
  systemctl start ai-trader 2>/dev/null
  HEALED=1
  sleep 5
  if systemctl is-active --quiet ai-trader; then
    PROBLEMS="${PROBLEMS}拉起成功;"
  else
    PROBLEMS="${PROBLEMS}拉起失败;"
  fi
fi

if ! systemctl is-active --quiet ai-trader-sentinel; then
  PROBLEMS="${PROBLEMS}哨兵未运行(已拉起);"
  systemctl reset-failed ai-trader-sentinel 2>/dev/null
  systemctl start ai-trader-sentinel 2>/dev/null
  HEALED=1
fi

# 数据新鲜度：用 python 查，查不到也算异常（不能因为查不到就当没问题）
DATA_CHK=$(cd "$DEST" && "$PY" - <<'PY' 2>&1
import sqlite3, time
now = int(time.time() * 1000)
out = []
try:
    c = sqlite3.connect("data/market_trade.db", timeout=5)
    mx = c.execute("SELECT COALESCE(MAX(ts),0) FROM decisions").fetchone()[0] or 0
    # 决策有 10 分钟节流，阈值放到 20 分钟，避免误报
    if mx and now - mx > 20 * 60 * 1000:
        out.append("决策停滞 %ds" % ((now - mx) // 1000))
except Exception as e:
    out.append("读决策库失败:" + str(e)[:60])
try:
    c = sqlite3.connect("data/market.db", timeout=5)
    mx = c.execute("SELECT COALESCE(MAX(open_time),0) FROM bars WHERE interval_ms=3000").fetchone()[0] or 0
    if mx and now - mx > 120000:
        out.append("行情停滞 %ds" % ((now - mx) // 1000))
    elif not mx:
        out.append("无 3s K 线")
except Exception as e:
    out.append("读行情库失败:" + str(e)[:60])
print(";".join(out))
PY
)
if [ -n "$DATA_CHK" ]; then PROBLEMS="${PROBLEMS}${DATA_CHK};"; fi
# ---- 持续停滞必须【自愈】，不能只报警 ----
#
# 为什么加这一段：上面的检查只把"行情停滞/决策停滞"写进 PROBLEMS，然后记一条日志。
# 但 is-active 那一段只处理"进程不在了"。真正的失败模式是【进程活着但不再干活】：
#   - WS 任务内部悄悄死掉，REST 轮询也停了，asyncio 主循环还在服务看板
#   - 权益查询持续失败 -> _step 每次都 return（engine.py:344），决策不再落库
# 这两种情况下 systemd 看到的是 active，永远不会重启，
# 于是持仓的止损【永远不再被评估】——仓位裸奔，而所有监控都在报"服务正常"。
#
# 为什么多等一会儿（连续 3 次 = 15 分钟）：
# 行情源短暂抖动是常态，一次停滞就重启会造成重启风暴，
# 而重启本身要重新回补 420 根 4h + 重新对账。
# 15 分钟仍远小于"一波趋势走完"的尺度，来得及。
#
# 为什么 30 分钟最多重启一次：
# 如果是网络真的断了，重启也连不上。不加节流就会每 5 分钟重启一次，
# 每次都清掉内存状态、重做对账，反而把日志和数据库打乱。
STALE_FILE="$DEST/logs/.stale_state"
CNT=0
LAST_RESTART=0
if [ -f "$STALE_FILE" ]; then
  read -r CNT LAST_RESTART < "$STALE_FILE" 2>/dev/null || true
  CNT=${CNT:-0}
  LAST_RESTART=${LAST_RESTART:-0}
fi
NOW_EP=$(date +%s)
if [ -n "$DATA_CHK" ]; then
  CNT=$((CNT + 1))
  if [ "$CNT" -ge 3 ] && [ $((NOW_EP - LAST_RESTART)) -ge 1800 ]; then
    PROBLEMS="${PROBLEMS}持续停滞 ${CNT} 次已重启;"
    systemctl reset-failed ai-trader 2>/dev/null
    systemctl restart ai-trader 2>/dev/null
    HEALED=1
    LAST_RESTART=$NOW_EP
    CNT=0
    sleep 5
  fi
else
  CNT=0
fi
# 计数写不进去 -> CNT 每轮都从 0 开始 -> 永远到不了 3 -> 自愈被【静默】关闭，
# 而日志还在说「连续第 1 次」，运维看不出这台机器已经不会自救了。
# 2026-11-02 实测（独立进程模型，和 timer 一样）：写失败时 9 轮轮询一次都没重启。
# logs/ 被清理、属主变化、磁盘写满都会走到这里，且都不会有别的地方报错。
# 写失败必须说出来，不能靠 || true 吞掉。
#
# 为什么加时间窗：这一条一旦成立就是持续状态，每 5 分钟报一次会淹掉研究日志。
# timer 每 5 分钟一轮，挑每小时的第 5 分钟内报一次，约 12 条/天。
# 有数据停滞时无条件报——那正是自愈该起作用却没起作用的时刻。
if ! echo "$CNT $LAST_RESTART" > "$STALE_FILE" 2>/dev/null; then
  if [ -n "$DATA_CHK" ] || [ "$(date +%M)" -lt 5 ]; then
    PROBLEMS="${PROBLEMS}停滞计数无法持久化，自动重启已失效($STALE_FILE);"
  fi
fi

# 时钟偏移：币安签名请求带 recvWindow=5000，
# 服务器时钟漂移超过 5 秒，所有签名请求都会被拒（-1021），
# 而表现是"下单失败"而不是"时钟错了"，很难第一眼定位。
# RestMarket.server_time() 早就写好了但从未被调用——又一个"存在但没接线"。
CLOCK_CHK=$(cd "$DEST" && "$PY" - <<'PY' 2>&1
import json, time, urllib.request
try:
    req = urllib.request.Request("https://fapi.binance.com/fapi/v1/time",
                                 headers={"User-Agent": "ai-trader/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        srv = json.loads(r.read())["serverTime"] / 1000.0
    off = srv - time.time()
    if abs(off) > 3.0:
        print("时钟偏移 %.1fs（超过 3s，签名请求会失败）" % off)
except Exception:
    pass        # 网络问题不在这里报，行情停滞那条会覆盖
PY
)
if [ -n "$CLOCK_CHK" ]; then PROBLEMS="${PROBLEMS}${CLOCK_CHK};"; fi

DISK=$(df -P "$DEST" | tail -1 | awk '{print $5}' | tr -d '%')
if [ "${DISK:-0}" -gt 90 ]; then PROBLEMS="${PROBLEMS}磁盘 ${DISK}%;"; fi

if [ -n "$PROBLEMS" ]; then
  echo "[healthcheck] $PROBLEMS"
  cd "$DEST" && PROBLEMS="$PROBLEMS" "$PY" - <<'PY'
import os, sys
sys.path.insert(0, ".")
from ai_trader.journal import Journal
j = Journal("data/market_trade.db"); j.start()
j.add("alert", "健康自检异常", os.environ.get("PROBLEMS", ""), None)
j.stop()
PY
  # 自愈成功不算失败：拉起后再确认一次，仍然不健康才返回 1。
  if ! systemctl is-active --quiet ai-trader || ! systemctl is-active --quiet ai-trader-sentinel; then
    echo "[healthcheck] 仍有服务未恢复，需人工介入"
    exit 1
  fi
  # 进程都在，但要说清楚【这一轮到底有没有做处置】——
  # 数据停滞时我们是故意不立刻重启的（避免重启风暴），
  # 那就不能宣称「已自愈」，否则等于告诉运维一件没发生的事。
  if [ "$HEALED" -eq 1 ]; then
    echo "[healthcheck] 已自愈（本次确实重启过）"
  elif [ -n "$DATA_CHK" ]; then
    echo "[healthcheck] 进程在运行，但数据仍在停滞：已告警，连续第 ${CNT} 次；未重启（满 3 次且距上次重启超 30 分钟才自动重启）"
  else
    echo "[healthcheck] 进程在运行，属告警类问题（本次未做重启处置），已记入研究日志"
  fi
  exit 0
fi
echo "[healthcheck] OK"
