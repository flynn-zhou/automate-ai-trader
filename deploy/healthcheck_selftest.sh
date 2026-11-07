#!/bin/bash
# healthcheck 自测：验证「停滞计数写不进去」这件事会被报出来，而不是被吞掉。
#
# 为什么必须存在：
#   deploy/healthcheck.sh 的自愈靠一个计数文件 logs/.stale_state 跨进程累计。
#   一旦这个文件写不进去（logs/ 被清理、属主变化、磁盘满），计数每轮从 0 开始，
#   永远到不了 3 —— 自愈被【静默】关闭，而日志还在说「连续第 1 次」。
#   2026-11-02 实测：写失败时 9 轮轮询一次都没重启，且没有任何地方报错。
#
# 为什么放在这里而不在 tests/：
#   本地是 Windows、没有 bash，服务器 venv 里没有 pytest。
#   这个自测只能在 Linux 上跑 —— 见 HANDOFF §六点九十三。
#
# 用法：bash deploy/healthcheck_selftest.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/healthcheck.sh"
[ -f "$SRC" ] || { echo "找不到 healthcheck.sh"; exit 1; }

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin"
# systemctl 桩：一律报 active，避免自测去动机器上的真服务
printf '#!/bin/bash\nexit 0\n' > "$W/bin/systemctl"
chmod +x "$W/bin/systemctl"

FAIL=0
run_case() {
  local NAME=$1 WITH_LOGS=$2 WANT=$3
  local D="$W/$NAME"
  mkdir -p "$D/data"
  [ -d "$HERE/../.venv" ] && ln -s "$HERE/../.venv" "$D/.venv"
  if [ "$WITH_LOGS" = yes ]; then mkdir -p "$D/logs"; fi
  sed -e "s#^DEST=.*#DEST=$D#" "$SRC" > "$D/hc.sh"
  local OUT
  OUT=$(cd "$D" && PATH="$W/bin:$PATH" bash "$D/hc.sh" 2>&1)
  if echo "$OUT" | grep -q "无法持久化"; then GOT=reported; else GOT=silent; fi
  if [ "$GOT" = "$WANT" ]; then
    echo "  OK   $NAME: 期望 $WANT，实际 $GOT"
  else
    echo "  FAIL $NAME: 期望 $WANT，实际 $GOT"
    echo "$OUT" | sed "s/^/       /"
    FAIL=1
  fi
}

echo "[selftest] 计数写不进去时必须报出来（logs/ 不存在）"
run_case no_logs no reported
echo "[selftest] 反向：计数写得进去时不许乱报（logs/ 存在）"
run_case has_logs yes silent

if [ "$FAIL" -eq 0 ]; then
  echo "[selftest] 通过"
  exit 0
fi
echo "[selftest] 失败"
exit 1
