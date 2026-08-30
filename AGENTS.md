# AGENTS.md — KontainKeeper

K8S 下 vscode-server 容器 IDE 的直连管理平台：容器内置 Agent 主动出站 WebSocket 连管理服务端（心跳指标 + 远程命令 + 自定义采集）。不使用 K8S 集群能力、不触碰宿主机、对容器用户无感知。

## 目录与架构边界

- `agent/src/kk_agent/` — 容器内客户端（**独立 UV 项目**，纯标准库，可编译为单文件二进制直接嵌入容器）。**必须保持纯 Python 标准库**（无 fastapi/psutil 等任何第三方依赖），目标常驻 RSS < 15MB。新增 `updater.py` 负责自更新（拉取版本清单 / 下载 / `sha256` 校验 / `os.execv` 自重启），**同样禁止第三方依赖**；服务端对应接口在 `server/src/kk_server/api/agent_update.py`。
- `server/src/kk_server/` — FastAPI 服务端（hub 连接中枢 / store SQLite / api REST）；`server/src/kk_server/web/` — 管理界面静态文件（无框架单页，随包一起打包，由服务端直接托管）。
- `proto/messages.md` — 双端通信协议契约。改协议必须同步：`agent/src/kk_agent/config.py` 的 `PROTO_VER`、`server/src/kk_server/__init__.py` 的 `PROTO_VER`、协议文档、双端测试。
- `tests/`、`scripts/build.sh`（把 agent 叠加进 vscode-server 镜像的 CI 脚本）。

## 常用命令

```bash
uv run pytest tests -q        # 全部测试（当前 52 项），仓库根目录执行（需先 uv sync）
uv run kk-server   # 本地起服务端（默认 admin/admin），仓库根目录执行
uv run pytest tests/test_integration.py -q   # 仅端到端集成（起真实 uvicorn + agent 线程）
```

依赖：`uv sync --all-packages（服务端依赖 + dev 组，agent 无第三方依赖）`。无 lint/typecheck 配置。

## 关键约束与陷阱

- **Agent 禁止第三方 import**，也禁止重线程常驻：主循环是单线程 select 事件循环，WebSocket socket 只允许主线程触碰；工作（心跳采集/命令/插件）在一次性 daemon 线程跑，结果经 `queue.Queue` 回主线程发帧。
- **Windows 开发机兼容**：`resource` 模块不存在，`agent/src/kk_agent/collector.py` 已做 try/except 导入；采集逻辑全部经 `fs_root`（KK_FS_ROOT）注入，测试用 `tests/conftest.py::make_fake_fs` 伪造 /proc 树——采集相关测试不要直接读真实 `/proc`。
- **命令下发优先用 argv 数组**，`cmdline` 走 shlex.split，Windows 上含空格路径（如 `D:\Program Files\...`）会被拆坏（集成测试曾踩坑）。
- **服务端入口是工厂** `kk_server.main:create_app`，没有模块级 `app`；运行走 `uv run kk-server`（即 `python -m kk_server`，经 `__main__.py`）。测试用 uvicorn.Server 线程 + `create_app(env)`。
- 心跳间隔下限 1s（`agent/src/kk_agent/config.load`），集成测试依赖 1s 间隔在数秒内积累多个序列点；别把下限调回去。
- Agent 上线（hello）即可在 API 看到容器，但**指标要等首帧心跳**；集成测试的等待条件必须同时检查 `metrics.mem_mb` 非空。
- 插件热加载按 mtime 比较，Windows 文件时间粒度粗：测试写文件后需显式 `os.utime` 递增时间戳。
- Web UI（`server/src/kk_server/web/app.js`）：hash 路由 + 轮询；轮询定时器一律用 `setPoll()`/`clearPolls()` 管理，不要直接 `setInterval` 散落各处。中文界面。
- 安全红线：命令黑名单（`KK_CMD_BLACKLIST`）+ 审计（`store.add_audit`）不能绕过；hello 校验失败 close code 语义见 `proto/messages.md`（4401 token / 4402 协议版本 / 4403 替换）。

## 背景阅读

改协议、Agent 资源策略或部署方式前先读 `docs/design.md`（总体设计）与 `proto/messages.md`（帧格式）；部署流程在 `README.md`。

## 约定

- 提交信息用中文，`feat/fix/docs/test` 前缀，按模块分批提交。
- 所有配置走 `KK_*` 环境变量（双端均是），不引入配置文件。
