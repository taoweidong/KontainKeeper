# KontainKeeper 服务端镜像
#
# 修 P2-14：改用 uv 安装，与仓库根 uv.lock 对齐（不再 pip 手装依赖导致与锁文件脱节）。
#
# 仓库是 uv workspace（根 pyproject.toml 声明 members=[server, agent]，锁在根 uv.lock），
# 因此构建上下文必须是仓库根（docker-compose 里 dockerfile: server/Dockerfile，context: .），
# .dockerignore 已把前端 / node_modules 等大目录排除，上下文很小。
FROM python:3.12-slim

# 装入 uv 官方静态二进制（无需联网安装 uv 本身）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uv
ENV PATH="/uv:$PATH" \
    # 镜像自带 python3.12，禁止 uv 再下载 Python
    UV_PYTHON_DOWNLOADS=never

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
