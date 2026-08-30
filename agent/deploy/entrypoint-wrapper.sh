#!/usr/bin/env bash
# KontainKeeper Agent 独立监管脚本（PID1 形态）。
#
# 职责：
#   1. 以「独立服务」方式常驻监管 kk-agent 二进制：崩溃自动拉起；
#      自更新时 Agent 内部 os.execv 复用同一 PID，本脚本不会误判为退出。
#   2. 可选并行拉起原 vscode-server 入口（KK_ORIG_ENTRYPOINT），两者生命周期解耦：
#      Agent 的崩溃/重启不影响 IDE，IDE 退出也不影响 Agent（除非本脚本退出）。
#
# 环境变量：
#   KK_AGENT_BIN         Agent 二进制路径（默认 /opt/kk-agent/kk-agent）
#   KK_AGENT_LOG         日志路径（默认 /var/log/kk-agent.log，>1MB 自动轮转一份）
#   KK_ORIG_ENTRYPOINT   原容器入口命令（JSON 数组字符串），留空则不拉起 IDE
set -u

KK_BIN="${KK_AGENT_BIN:-/opt/kk-agent/kk-agent}"
KK_LOG="${KK_AGENT_LOG:-/var/log/kk-agent.log}"
MAX_LOG=$((1024 * 1024))

mkdir -p "$(dirname "$KK_LOG")"

rotate_log() {
  if [ -f "$KK_LOG" ] && [ "$(stat -c%s "$KK_LOG" 2>/dev/null || echo 0)" -gt "$MAX_LOG" ]; then
    mv -f "$KK_LOG" "$KK_LOG.1"
  fi
}

# 原 IDE 入口：在独立子 shell 中拉起，与本监管循环互不干扰
if [ -n "${KK_ORIG_ENTRYPOINT:-}" ]; then
  ( exec /bin/sh -c "$KK_ORIG_ENTRYPOINT" ) &
  echo "$(date) launched orig entrypoint: $KK_ORIG_ENTRYPOINT" >>"$KK_LOG"
fi

echo "$(date) kk-entrypoint: supervising $KK_BIN" >>"$KK_LOG"
while true; do
  rotate_log
  "$KK_BIN" >>"$KK_LOG" 2>&1 &
  AGENT_PID=$!
  wait "$AGENT_PID"          # execv 自更新会复用该 PID，此处不会提前返回
  echo "$(date) kk-agent exited ($?), restart in 5s" >>"$KK_LOG"
  sleep 5
done
