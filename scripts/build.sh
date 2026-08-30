#!/usr/bin/env bash
# 构建 vscode-server 管理镜像：先把 kk-agent 编译为二进制，再在原镜像基础上
# 叠加该二进制（镜像期介入，上线即连，运行时不再依赖容器内 Python）。
#
# 用法:
#   KK_SERVER=wss://kk-server.ops:8443/ws/agent \
#     BASE_IMAGE=myregistry/vscode-server:1.2 ./scripts/build.sh myregistry/vscode-server-managed:1.2
#
# 入口透传方式（docker 原生机制，不经 shell 解析）：
#   原镜像 ENTRYPOINT/CMD 在构建期解析为 JSON 数组，写入生成 Dockerfile 的
#   ENTRYPOINT/CMD 指令：kk-entrypoint 作为 ENTRYPOINT 首元素，原入口其余
#   元素作为其参数透传。运行时 wrapper 后台监管 Agent、前台 exec "$@"，
#   容器生命周期 = 原 IDE 生命周期，用户看到的启动行为不变。
#
# 安全说明：KK_TOKEN 不得写入镜像层（docker inspect 即可提取）。请在运行时通过
#   `docker run -e KK_TOKEN=xxx` 或 k8s Secret 注入；本脚本只把 KK_SERVER 地址等
#   非敏感配置烧入镜像。
set -euo pipefail

: "${KK_SERVER:?need KK_SERVER}"
BASE_IMAGE="${BASE_IMAGE:?need BASE_IMAGE, e.g. myregistry/vscode-server:1.2}"
OUT_IMAGE="${1:?need output image tag}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"

# 1) 先编译 kk-agent 单文件二进制
echo ">> building kk-agent binary"
( cd "$REPO_ROOT/agent" && PYTHON="$PY" ./build/build_binary.sh )

BIN="$REPO_ROOT/agent/dist/kk-agent"
[ -x "$BIN" ] || [ -f "$BIN" ] || { echo "!! binary not found: $BIN"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 2) 读出原镜像 ENTRYPOINT/CMD（Go 模板 JSON：数组或 null），
#    构建期解析为 Dockerfile 指令行，避免运行时解析 JSON 的脆弱性
ORIG_ENTRYPOINT_JSON="$(docker image inspect "$BASE_IMAGE" --format '{{json .Config.Entrypoint}}')"
ORIG_CMD_JSON="$(docker image inspect "$BASE_IMAGE" --format '{{json .Config.Cmd}}')"

ENTRYPOINT_LINE="$("$PY" -c '
import json, sys
ep = json.loads(sys.argv[1] or "null")
cmd = json.loads(sys.argv[2] or "null")
wrap = ["/usr/local/bin/kk-entrypoint"]
# 原入口存在：kk-entrypoint 打头，原 ENTRYPOINT 元素作为参数透传
# 原入口为空：kk-entrypoint 单独成 ENTRYPOINT（CMD 仍透传给 wrapper）
entry = wrap + ([str(x) for x in ep] if ep else [])
print("ENTRYPOINT " + json.dumps(entry))
if cmd:
    print("CMD " + json.dumps([str(x) for x in cmd]))
' "$ORIG_ENTRYPOINT_JSON" "$ORIG_CMD_JSON")"

cp "$BIN" "$WORK/kk-agent"
cp -r "$REPO_ROOT/agent/src/kk_agent/plugins" "$WORK/plugins"
cp "$REPO_ROOT/agent/deploy/entrypoint-wrapper.sh" "$WORK/kk-entrypoint"

# 3) 生成叠加 Dockerfile：ENTRYPOINT/CMD 均为 exec 形式（JSON 数组），
#    不产生 shell 形式的歧义行，也不出现 "CMD null" 这类非法指令
{
  echo "FROM $BASE_IMAGE"
  echo "COPY kk-agent /opt/kk-agent/kk-agent"
  echo "COPY plugins /opt/kk-agent/plugins"
  echo "COPY kk-entrypoint /usr/local/bin/kk-entrypoint"
  echo "RUN chmod +x /usr/local/bin/kk-entrypoint /opt/kk-agent/kk-agent && mkdir -p /var/log"
  echo "ENV KK_SERVER=\"$KK_SERVER\" \\"
  echo "    KK_AGENT_BIN=/opt/kk-agent/kk-agent \\"
  echo "    KK_PLUGIN_DIR=/opt/kk-agent/plugins \\"
  echo "    KK_LOG=/var/log/kk-agent.log"
  echo "$ENTRYPOINT_LINE"
} > "$WORK/Dockerfile"

echo ">> building $OUT_IMAGE (base=$BASE_IMAGE)"
echo ">> generated entrypoint config:"
sed 's/^/     /' <<<"$ENTRYPOINT_LINE"
docker build -t "$OUT_IMAGE" "$WORK"
echo ">> done: $OUT_IMAGE"
echo ">> 运行镜像时务必通过 -e KK_TOKEN=xxx 或 k8s Secret 注入令牌（切勿写死在镜像中）。"
