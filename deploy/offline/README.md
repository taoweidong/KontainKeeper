# 离线部署镜像包（内网无网场景）

内网机器无法访问 Docker Hub / GHCR，需在外网构建机提前把镜像打成 tar 包，
随仓库（或 U 盘/内网文件共享）带入，导入后直接以本地镜像启动。

## 1. 镜像清单与下载地址

| 镜像 | 级别 | 用途 | 官方地址 |
|---|---|---|---|
| `kk-server:latest`（构建产物） | core | 服务端（含前端托管产物） | 外网机 `docker build -f server/Dockerfile` 产出，无公共地址 |
| `eclipse-mosquitto:2` | core | MQTT Broker | https://hub.docker.com/_/eclipse-mosquitto |
| `<KK_AGENT_IMAGE>`（构建产物） | core | 叠加了 kk-agent 二进制的 vscode-server 镜像 | 基于私有基础镜像经 `scripts/build.sh` 产出，无公共地址 |
| `python:3.12-slim` | build | kk-server 镜像的构建基础 | https://hub.docker.com/_/python |
| `ghcr.io/astral-sh/uv:latest` | build | kk-server 构建期 `COPY --from` 取 uv 二进制 | https://github.com/astral-sh/uv/pkgs/container/uv |
| `node:20-alpine` / `nginx:stable-alpine` | opt | 仅 `web/Dockerfile` 独立部署前端时才需要（默认不需要，前端产物由服务端镜像托管） | https://hub.docker.com/_/node · https://hub.docker.com/_/nginx |

**级别说明**：`core` 是内网运行必需；`build` 仅外网构建机需要（产物 tar 已包含其内容，
内网不重建就不需要）；`opt` 非常规场景。

> 内网最小集合 = `kk-server_latest.tar.gz` + `eclipse-mosquitto_2.tar.gz` + agent 产物 tar。

## 2. 外网打包（有网机器，一次执行）

前置：外网机装好 Docker，能访问 Docker Hub 与 GHCR；仓库源码已 checkout。

```bash
# 1) 构建 agent 叠加产物镜像（基础镜像为你的私有 vscode-server 镜像）
scripts/build.sh <私有基础镜像> kk-vscode-server:2026-09

# 2) 打包（自动构建 kk-server、拉取依赖镜像、save 并生成校验单）
KK_AGENT_IMAGE=kk-vscode-server:2026-09 ./deploy/offline/pack.sh
# 连 opt 级（前端独立镜像基础）一起打：PACK_OPT=1 ./deploy/offline/pack.sh
```

产物在 `deploy/offline/images/`：`*.tar.gz` + `SHA256SUMS`。

无 Docker 的外网机备选：用 [skopeo](https://github.com/containers/skopeo)
`skopeo copy docker://eclipse-mosquitto:2 docker-archive:eclipse-mosquitto_2.tar:eclipse-mosquitto:2`
拉单镜像（产物镜像仍必须有 Docker 构建）。

## 3. 归档入库

- 脚本、清单（`manifest.txt`）、本说明随 git 入库；**tar 本体默认被 .gitignore 忽略**。
- tar 合计约 300–600MB（gzip 后）。若要求 tar 也进 git：启用
  [Git LFS](https://git-lfs.com)（`git lfs track "deploy/offline/images/*.tar.gz"`），
  或改用仓库 Release assets / 内网文件共享分发——直接 commit 大二进制会让仓库不可逆膨胀。

## 4. 内网导入与启动（离线机器）

前置：内网机已装 Docker（装 Docker 本身也离线时，提前下载发行版离线包：
Ubuntu 用 `apt download docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`，
CentOS 用 `yumdownloader`，U 盘带入安装）。

```bash
# 1) 拷入仓库（含 deploy/offline/images/），校验并导入全部镜像
./deploy/offline/load.sh

# 2) 配置凭据/白名单（与在线部署一致）
cp .env.example .env   # 填 KK_AGENT_IPS、KK_ADMIN_PASS

# 3) 启动——必须用离线 compose 文件（kk-server 走 image 引用，不触发 build）
docker compose -f docker-compose.offline.yml --env-file .env up -d
```

**切勿用 `docker-compose.prod.yml up --build`**：build 段会在镜像内 `uv sync` 联网
安装依赖，内网必然失败；即使命中镜像缓存，缓存 miss 的层一样会尝试联网。

## 5. 验证与注意事项

- 验证同 [docs/deployment.md §8](../../docs/deployment.md)（登录、Agent 上线、命令下发、白名单负向）。
- **架构一致**：pack.sh 固定 `--platform linux/amd64` 拉取；内网机为 arm64 时改为
  `linux/arm64` 重打，或在多架构机器上用 `docker buildx` 分别出包。
- **tag 对齐**：`manifest.txt` 里的 tag 与 compose / Dockerfile 引用一一对应，
  改 tag（如固定次版本）须四处同步：`manifest.txt`、`docker-compose.prod.yml`、
  `docker-compose.offline.yml`、对应 Dockerfile。
- 内网有多台机器时，也可在其中一台起 [registry](https://hub.docker.com/_/registry)
  私有仓库（`docker run -d -p 5000:5000 registry:2`，load 后 tag+push），
  其余机器 `docker pull`——机器数量少时直接逐台 load.sh 更简单。
