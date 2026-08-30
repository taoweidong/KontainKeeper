#!/usr/bin/env bash
# 构建 vscode-server 管理镜像：先把 kk-agent 编译为二进制，再在原镜像基础上
# 叠加该二进制（镜像期介入，上线即连，运行时不再依赖容器内 Python）。
#
# 用法:
#   KK_SERVER=wss://kk-server.ops:8443/ws/agent KK_TOKEN=xxx \
#     BASE_IMAGE=myregistry/vscode-server:1.2 ./scripts/build.sh myregistry/vscode-server-managed:1.2
set -euo pipefail

: "${KK_SERVER:?need KK_SERVER}"
: "${KK_TOKEN:?need KK_TOKEN}"
BASE_IMAGE="${BASE_IMAGE:?need BASE_IMAGE, e.g. myregistry/vscode-server:1.2}"
OUT_IMAGE="${1:?need output image tag}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 1) 先编译 kk-agent 单文件二进制
echo ">> building kk-agent binary"
( cd "$REPO_ROOT/agent" && PYTHON="${PYTHON:-python3}" ./build/build_binary.sh )

BIN="$REPO_ROOT/agent/dist/kk-agent"
[ -x "$BIN" ] || { echo "!! binary not found: $BIN"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 原镜像的 ENTRYPOINT 需要透传：读出来塞给包装脚本
ORIG_ENTRYPOINT="$(docker image inspect "$BASE_IMAGE" --format '{{json .Config.Entrypoint}}')"
ORIG_CMD="$(docker image inspect "$BASE_IMAGE" --format '{{json .Config.Cmd}}')"

cp "$BIN" "$WORK/kk-agent"
cp -r "$REPO_ROOT/agent/src/kk_agent/plugins" "$WORK/plugins"
cp "$REPO_ROOT/agent/deploy/entrypoint-wrapper.sh" "$WORK/kk-entrypoint"

cat > "$WORK/Dockerfile" <<EOF
FROM $BASE_IMAGE
COPY kk-agent /opt/kk-agent/kk-agent
COPY plugins /opt/kk-agent/plugins
COPY kk-entrypoint /usr/local/bin/kk-entrypoint
RUN chmod +x /usr/local/bin/kk-entrypoint /opt/kk-agent/kk-agent && mkdir -p /var/log
ENV KK_SERVER="$KK_SERVER" \\
    KK_TOKEN="$KK_TOKEN" \\
    KK_AGENT_BIN=/opt/kk-agent/kk-agent \\
    KK_PLUGIN_DIR=/opt/kk-agent/plugins \\
    KK_LOG=/var/log/kk-agent.log \\
    KK_ORIG_ENTRYPOINT=${ORIG_ENTRYPOINT}
ENTRYPOINT ["/usr/local/bin/kk-entrypoint"]
CMD ${ORIG_CMD}
EOF

echo ">> building $OUT_IMAGE (base=$BASE_IMAGE, orig_entrypoint=$ORIG_ENTRYPOINT)"
docker build -t "$OUT_IMAGE" "$WORK"
echo ">> done: $OUT_IMAGE"
echo ">> 注意：镜像内已嵌入 token，请控制镜像仓库访问权限；轮换 token 需重新构建。"
