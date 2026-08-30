#!/usr/bin/env bash
# KontainKeeper Agent 独立监管入口（PID1 形态）。
#
# 职责：
#   1. 后台监管 kk-agent 二进制：崩溃自动拉起；自更新时 Agent 内部
#      os.execv 复用同一 PID，监管循环不会误判为退出。
#   2. 前台透传原镜像入口（exec "$@"，由 Docker 以参数形式传入，
#      不经任何 shell/JSON 解析）：容器生命周期 = 原 IDE 生命周期，
#      用户看到的启动行为完全不变；IDE 退出即容器退出。
#   3. 无入口参数（基础镜像无 ENTRYPOINT/CMD）时退化为纯监管模式，
#      容器常驻、Agent 继续在线。
#
# 环境变量：
#   KK_AGENT_BIN   Agent 二进制路径（默认 /opt/kk-agent/kk-agent）
#   KK_LOG         日志路径（默认 /var/log/kk-agent.log，>1MB 自动轮转一份）
set -u

KK_BIN="${KK_AGENT_BIN:-/opt/kk-agent/kk-agent}"
KK_LOG="${KK_LOG:-/var/log/kk-agent.log}"
MAX_LOG=$((1024 * 1024))

mkdir -p "$(dirname "$KK_LOG")"

rotate_log() {
  if [ -f "$KK_LOG" ] && [ "$(stat -c%s "$KK_LOG" 2>/dev/null || echo 0)" -gt "$MAX_LOG" ]; then
    mv -f "$KK_LOG" "$KK_LOG.1"
  fi
}

supervise() {
  while true; do
    rotate_log
    "$KK_BIN" >>"$KK_LOG" 2>&1 &
    AGENT_PID=$!
    wait "$AGENT_PID"          # execv 自更新会复用该 PID，此处不会提前返回
    echo "$(date) kk-agent exited ($?), restart in 5s" >>"$KK_LOG"
    sleep 5
  done
}

echo "$(date) kk-entrypoint: supervising $KK_BIN" >>"$KK_LOG"
supervise &
SUPERVISOR_PID=$!

if [ "$#" -gt 0 ]; then
  # 原镜像入口作为前台进程（exec 形式，参数由 Docker 注入）
  exec "$@"
fi

# 无入口参数：纯监管模式，保持容器常驻
wait "$SUPERVISOR_PID"
