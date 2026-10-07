#!/usr/bin/env bash
# 外网构建机执行：拉取/构建全部镜像 → docker save 到 deploy/offline/images/ → 生成校验单。
#
# 用法（在仓库根目录）：
#   ./deploy/offline/pack.sh                          # core + build 镜像（默认）
#   PACK_OPT=1 ./deploy/offline/pack.sh               # 连 opt（前端独立镜像基础）一起打
#   KK_AGENT_IMAGE=kk-vscode-server:2026-09 \
#     ./deploy/offline/pack.sh                        # 追加 agent 叠加产物镜像（须先经 scripts/build.sh 构建）
#
# 产物：deploy/offline/images/<name>.tar.gz + SHA256SUMS
set -euo pipefail

cd "$(dirname "$0")/../.."
OUT="deploy/offline/images"
mkdir -p "$OUT"

# 1. 构建服务端产物镜像（core）
echo "== build kk-server:latest =="
docker build -f server/Dockerfile -t kk-server:latest .

# 2. 按 manifest 拉取外部镜像并保存
save_one() {  # $1=image  $2=file  $3=level
  case "$3" in
    core|build) ;;
    opt) [ "${PACK_OPT:-0}" = "1" ] || { echo "-- skip(opt) $1"; return; } ;;
    *) echo "!! 未知级别 $3（$1）"; exit 1 ;;
  esac
  echo "== pull & save $1 =="
  if [ "$1" != "kk-server:latest" ]; then
    docker pull --platform linux/amd64 "$1"
  fi
  docker save "$1" | gzip > "$OUT/$2"
}

while IFS='|' read -r image file level; do
  case "$image" in ''|\#*) continue ;; esac
  save_one "$image" "$file" "$level"
done < deploy/offline/manifest.txt

# 3. agent 叠加产物镜像（可选，tag 由部署方注入）
if [ -n "${KK_AGENT_IMAGE:-}" ]; then
  fname="kk-agent-image_$(echo "$KK_AGENT_IMAGE" | tr '/:' '__').tar.gz"
  echo "== save agent image $KK_AGENT_IMAGE =="
  docker save "$KK_AGENT_IMAGE" | gzip > "$OUT/$fname"
fi

# 4. 校验单
( cd "$OUT" && sha256sum *.tar.gz > SHA256SUMS )
echo "== done =="
ls -lh "$OUT"
