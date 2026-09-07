#!/usr/bin/env bash
# 内网机器执行：校验并导入 deploy/offline/images/ 下全部镜像 tar。
#
# 用法（在仓库根目录）：
#   ./deploy/offline/load.sh
# 完成后按 deploy/offline/README.md 用 docker-compose.offline.yml 启动（切勿 --build）。
set -euo pipefail

cd "$(dirname "$0")"
OUT="images"
[ -d "$OUT" ] || { echo "未找到 $OUT/（先在外网执行 pack.sh 并拷贝整个目录）"; exit 1; }

cd "$OUT"
if [ -f SHA256SUMS ]; then
  echo "== 校验 =="
  sha256sum -c SHA256SUMS
fi

for f in *.tar.gz; do
  echo "== docker load: $f =="
  docker load -i "$f"
done

echo "== 已导入镜像 =="
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}'
