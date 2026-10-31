# KontainKeeper 服务端镜像
#
# 基础镜像对齐生产环境镜像清单（python-3.12 → python:3.12）；内网 registry 命名
# 不同时用 --build-arg PYTHON_BASE=<内网镜像> 覆盖。
#
# 修 P2-14：改用 uv 安装，与仓库根 uv.lock 对齐（不再 pip 手装依赖导致与锁文件脱节）。
#
# 仓库是 uv workspace（根 pyproject.toml 声明 members=[server, agent]，锁在根 uv.lock），
# 因此构建上下文必须是仓库根（docker-compose 里 dockerfile: docker/Dockerfile，context: .），
# .dockerignore 已把前端 / node_modules 等大目录排除，上下文很小。
ARG PYTHON_BASE=python:3.12
FROM ${PYTHON_BASE}

# uv 经 pip 安装（pip 源可达）。不再用 ghcr.io/astral-sh/uv 的 COPY --from——
# 那是对镜像清单之外第三方仓库的网络依赖，受限环境里构建会直接失败。
# 注意：必须放进 PATH 标准目录——曾用「COPY 到 /uv + ENV PATH=/uv:...」，
# wslc 构建器不吃多行 ENV 续行，RUN 阶段会 uv: not found。
RUN pip install --no-cache-dir uv
# 镜像自带 python3.12，禁止 uv 再下载 Python
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 1) 依赖清单 + 锁文件（单独一层，改代码不触发重装依赖）
COPY pyproject.toml uv.lock ./
COPY server/pyproject.toml server/pyproject.toml
COPY agent/pyproject.toml agent/pyproject.toml

# 2) 源码（uv workspace 以 editable 方式把包链接进 venv，运行时仍需 src 在盘）
COPY server/src ./server/src
COPY agent/src ./agent/src

# 3) 按锁文件精确安装：只装 kk-server 运行时依赖，不装 dev 组
RUN uv sync --frozen --no-dev --package kk-server

ENV PATH="/app/.venv/bin:$PATH" \
    KK_DB_PATH=/data/kk-server.db \
    KK_PORT=8443 \
    KK_HOST=0.0.0.0

# 前端构建产物已随包提交在 server/src/kk_server/web/，由服务端直接托管（KK_WEB_DIR 默认即该目录）
VOLUME /data
EXPOSE 8443

# main.py: uvicorn.run(create_app(), host, port)
CMD ["python", "-m", "kk_server"]
