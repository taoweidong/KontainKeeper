#!/usr/bin/env bash
# 构建 vscode-server 管理镜像：在原镜像基础上叠加 kk-agent（镜像期介入，上线即连）。
# 用法:
#   KK_SERVER=wss://kk-server.ops:8443/ws/agent KK_TOKEN=xxx \
#     BASE_IMAGE=myregistry/vscode-server:1.2 ./scripts/build.sh myregistry/vscode-server-managed:1.2
set -euo pipefail

: "${KK_SERVER:?need KK_SERVER}"
: "${KK_TOKEN:?need KK_TOKEN}"
BASE_IMAGE="${BASE_IMAGE:?need BASE_IMAGE, e.g. myregistry/vscode-server:1.2}"
OUT_IMAGE="${1:?need output image tag}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 原镜像的 ENTRYPOINT 需要透传：读出来塞给包装脚本
ORIG_ENTRYPOINT="$(docker image inspect "$BASE_IMAGE" --format '{{json .Config.Entrypoint}}')"
ORIG_CMD="$(docker image inspect "$BASE_IMAGE" --format '{{json .Config.Cmd}}')"

cat > "$WORK/Dockerfile" <<EOF
FROM $BASE_IMAGE
COPY kk-agent /opt/kk-agent
COPY entrypoint-wrapper.sh /usr/local/bin/kk-entrypoint
RUN chmod +x /usr/local/bin/kk-entrypoint && mkdir -p /var/log
ENV KK_SERVER="$KK_SERVER" \\
    KK_TOKEN="$KK_TOKEN" \\
    KK_AGENT_DIR=/opt/kk-agent \\
    KK_LOG=/var/log/kk-agent.log \\
    KK_ORIG_ENTRYPOINT=${ORIG_ENTRYPOINT}
ENTRYPOINT ["/usr/local/bin/kk-entrypoint"]
CMD ${ORIG_CMD}
EOF

cp -r "$REPO_ROOT/agent/kk-agent" "$WORK/kk-agent"
cp "$REPO_ROOT/agent/entrypoint-wrapper.sh" "$WORK/entrypoint-wrapper.sh"

echo ">> building $OUT_IMAGE (base=$BASE_IMAGE, orig_entrypoint=$ORIG_ENTRYPOINT)"
docker build -t "$OUT_IMAGE" "$WORK"
echo ">> done: $OUT_IMAGE"
echo ">> 注意：镜像内已嵌入 token，请控制镜像仓库访问权限；轮换 token 需重新构建。"
