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

# PyInstaller onefile 被 SIGKILL 时解压目录 /tmp/_MEI* 不会自清（资源评审 P3）。
# 容器启动时顺手清理 60 分钟前的残留；带存活实例的目录因 mtime 新鲜而得以保留。
cleanup_stale_mei() {
  find /tmp -maxdepth 1 -name '_MEI*' -type d -mmin +60 -exec rm -rf {} + 2>/dev/null
  return 0
}

supervise() {
  # 监管循环自身也要接信号：转发给 Agent 子进程后退出，避免 Agent 被 SIGKILL。
  trap 'kill -TERM "$AGENT_PID" 2>/dev/null; exit 0' TERM INT
  while true; do
    rotate_log
    "$KK_BIN" >>"$KK_LOG" 2>&1 &
    AGENT_PID=$!
    wait "$AGENT_PID"          # execv 自更新会复用该 PID，此处不会提前返回
    echo "$(date) kk-agent exited ($?), restart in 5s" >>"$KK_LOG"
    sleep 5
  done
}

# 信号转发（P1-7）：容器停止时把 SIGTERM/SIGINT 转发给监管循环与 Agent 子进程，
# 让 Agent 能发优雅下线帧、清理 _MEI，而不是被 SIGKILL 丢掉下线帧。
forward_signal() {
  sig="$1"
  [ -n "${SUPERVISOR_PID:-}" ] && kill -"$sig" "$SUPERVISOR_PID" 2>/dev/null
  [ -n "${IDE_PID:-}" ] && kill -"$sig" "$IDE_PID" 2>/dev/null
}
trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT

cleanup_stale_mei

echo "$(date) kk-entrypoint: supervising $KK_BIN" >>"$KK_LOG"
supervise &
SUPERVISOR_PID=$!

if [ "$#" -gt 0 ]; then
  # 原镜像入口作为后台子进程（参数由 Docker 注入，不经任何 shell/JSON 解析）。
  # 不再用 exec：exec 会替换本进程、丢失 trap，导致信号无法转发给 Agent。
  # IDE 退出即视为容器任务结束，随后收掉监管循环。
  "$@" &
  IDE_PID=$!
fi

# 等待关键子进程：有 IDE 等 IDE，无则等监管循环（纯监管模式常驻）。
if [ -n "${IDE_PID:-}" ]; then
  wait "$IDE_PID"
else
  wait "$SUPERVISOR_PID"
fi

# 收尾：IDE 退出后确保 Agent 随监管循环一起退出
[ -n "${SUPERVISOR_PID:-}" ] && kill -TERM "$SUPERVISOR_PID" 2>/dev/null
wait 2>/dev/null
