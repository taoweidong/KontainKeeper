#!/bin/sh
# kk-agent 守护拉起器：容器启动时后台运行 Agent（崩溃自动重启），
# 然后 exec 原镜像 entrypoint —— 用户侧 vscode-server 的启动行为完全不变。
set -u

KK_DIR="${KK_AGENT_DIR:-/opt/kk-agent}"
KK_LOG="${KK_LOG:-/var/log/kk-agent.log}"

mkdir -p "$(dirname "$KK_LOG")" 2>/dev/null

# 常驻监督循环：Agent 退出（崩溃/被杀）后 5 秒重拉，用户无感知
(
  while :; do
    python3 -OO "$KK_DIR/agent_main.py" >> "$KK_LOG" 2>&1
    echo "$(date '+%F %T') kk-agent exited, restarting in 5s" >> "$KK_LOG"
    sleep 5
  done
) &
SUPERVISOR_PID=$!

# 日志封顶：超过 1MB 截断（无 logrotate 的精简容器里够用）
(
  while :; do
    sleep 3600
    [ -f "$KK_LOG" ] || continue
    SIZE=$(wc -c < "$KK_LOG" 2>/dev/null || echo 0)
    if [ "$SIZE" -gt 1048576 ]; then
      tail -c 262144 "$KK_LOG" > "$KK_LOG.tmp" && mv "$KK_LOG.tmp" "$KK_LOG"
    fi
  done
) &

# 执行原镜像 entrypoint/启动命令
if [ -n "${KK_ORIG_ENTRYPOINT:-}" ]; then
  exec $KK_ORIG_ENTRYPOINT
fi
exec "$@"
