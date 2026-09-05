#!/usr/bin/env bash
# 为 Agent（用户名 = 主机名）在 passwordfile 中写入/更新账号。
#
# 用法：
#   bash deploy/mosquitto/gen-credentials.sh <主机名> <token>
#
# 说明：
#   - 用户名即 Agent 在管理平台上的主机标识；密码即 Agent 侧 KK_TOKEN。
#   - ACL（aclfile）的 pattern 行会把该用户限制为只能读写 kk/v1/<主机名>/#。
#   - 优先调用本地 mosquitto_passwd；若未安装，则提示用容器内命令生成
#     （保证哈希算法与运行的 Broker 完全一致）。
#   - 生产务必逐主机下发独立 token；切勿多台主机共用同一 token。
set -euo pipefail

HOST="${1:?usage: gen-credentials.sh <hostname> <token>}"
TOKEN="${2:-dev-token}"
DIR="$(cd "$(dirname "$0")" && pwd)"
PF="$DIR/passwordfile"

if command -v mosquitto_passwd >/dev/null 2>&1; then
  mosquitto_passwd -b "$PF" "$HOST" "$TOKEN"
else
  echo "!! 未找到本地 mosquitto_passwd，请用容器内命令生成：" >&2
  echo "   docker compose run mosquitto mosquitto_passwd -b $PF $HOST $TOKEN" >&2
  exit 1
fi

echo ">> $HOST 已写入 $PF（密码即该主机的 Agent KK_TOKEN）"
