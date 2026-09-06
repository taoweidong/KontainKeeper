# AGENTS.md — KontainKeeper

Linux 主机（含 K8S 下 vscode-server 容器 IDE）的直连管理与指标提取平台：主机内置 Agent 主动出站 MQTT 连 Broker，服务端无状态桥接（心跳指标 + 远程命令 + 自定义采集）。不使用 K8S 集群能力、不触碰宿主机、对容器用户无感知。

## 一句话架构

```
Agent (kk-agent, psutil 采集 + paho-mqtt 发布)
  │ hb QoS0 / result QoS1
  ▼
Mosquitto 2.x（LWT 离线 · retain status · QoS1 cmd 离线队列 · pattern ACL）
  ▼
kk-server (FastAPI + MqttBridge 无状态桥接 + SQLAlchemy async 三库)
  ▼
web/ Vue3 前端（REST 轮询 + ECharts，构建产物由 kk-server 托管）
```

可靠性（连接/重连/离线排队/在线判定）整体交给 Broker，服务端只做「消息路由到 DB / 命令发布到主题」。

## 目录与架构边界

- `agent/src/kk_agent/` — 主机内客户端（**独立 UV 项目**）。**不再是纯标准库**：采集用 `psutil`（跨平台、8 个采集项），传输用 `paho-mqtt`（重连退避/保活/out-queue），可编译为单文件二进制嵌入镜像，常驻 RSS 口径 **25–35MB**。模块：`transport.py`（MQTT，替代已删的 `ws.py`+`conn.py`）、`collector.py`（psutil，含 `collect_items()` 按项采集）、`executor.py`、`updater.py`（自更新 sha256/HMAC）、`main.py`（事件循环）。
- `server/src/kk_server/` — FastAPI 服务端，MVC 分层：`models/`（SQLAlchemy 2 Core + async engine，SQLite/PG/MySQL 三库通用）→ `services/`（`mqtt_bridge.py` 无状态桥接、命令黑名单 security）→ `controllers/`（REST `/api/*`）→ `web/`（Vue3 构建产物，随包打包、服务端直接托管）；`main.py` 的 `create_app` 只做装配。**没有 WS 入口**（`agent_ws.py`/`hub.py` 已删）。
- `web/` — **独立 pnpm 工程**（Vue3 + TS + Element Plus + Vite + Pinia + ECharts，底座 pure-admin-thin v6.2.0）。`src/api/` 业务 API 层、`src/views/` 四个业务页（host/monitor 总览、host/detail 详情、command 命令中心、audit 审计）、`src/router/modules/kk.ts` 静态路由。`web/dist/` 被 .gitignore 忽略，产物需人工同步到 `server/src/kk_server/web/`。
- `proto/messages.md` — 双端通信协议契约（**v2 = MQTT 主题布局**）。改协议必须同步：`agent/src/kk_agent/config.py` 的 `PROTO_VER`、`server/src/kk_server/__init__.py` 的 `PROTO_VER`、协议文档、双端测试。
- `agent/tests/`、`server/tests/`、`scripts/build.sh`（把 agent 叠加进 vscode-server 镜像）。

## 常用命令

```bash
# 后端（仓库根目录；uv run 会去下载 Python 3.12 而失败，务必用 .venv 直调）
.venv/Scripts/python.exe -m pytest agent/tests -q      # Agent 单测
.venv/Scripts/python.exe -m pytest server/tests -q      # Server 单测 + 集成
.venv/Scripts/python.exe -m pytest agent/tests server/tests -q   # 全量：159 passed, 3 skipped
.venv/Scripts/python.exe -m kk_server                   # 起服务端（默认 admin/admin）

# 前端（web/ 目录）
pnpm dev         # 开发（VITE_PROXY 代理到 8443）
pnpm typecheck   # TS 类型检查
pnpm build       # 产物输出到 web/dist/
```

依赖：`uv sync --all-packages`（服务端 + dev）；`--extra postgres` / `--extra mysql` 按需装驱动。前端 `pnpm install`。无 lint 配置，前端有 typecheck。

## 关键约束与陷阱

- **Agent 线程模型**：主循环是单线程事件循环，MQTT socket 只由 paho 的后台网络线程触碰；采集/命令/插件在一次性 daemon 线程跑，结果由工作线程直接经 paho 发帧（paho 发布线程安全，不回主线程）；回调里不要做阻塞操作，重活丢给 worker 线程。
- **三库方言差异全部收在 `Store._upsert` / `Store._ensure_schema` 两处**，不要在别处再分叉：MySQL 大字段必须 LONGTEXT（TEXT 仅 64KB，命令输出 base64 最大 5.6MB 会静默截断）、主键必须定长 `String(n)`、upsert 是 `INSERT IGNORE`（SQLAlchemy 2.0 无 `.ignore()`，必须 `prefix_with("IGNORE")`）；PG/SQLite 用 `on_conflict_do_nothing`。
- **分批删按主键 `IN`，不要用 `LIMIT`**：PG 不支持 `DELETE...LIMIT`，MySQL 的是方言专属写法，SQLite 还要编译期开关。
- **`create_all` 只建表不加列**：新增列必须登记 `tables._ADD_COLUMNS`，由 `setup()` 的 `_ensure_schema` 自动 ALTER（SQLite 查 `PRAGMA table_info`，其余查 `information_schema` 且 MySQL 要 `DATABASE()` 限定 schema）。
- **Windows 开发机兼容**：采集基于 psutil（跨平台，不解析 /proc、不注入 fs_root），`agent/tests` 直接读真机/容器指标即可，无需伪造 /proc 树；命令执行的进程树回收按平台分路——POSIX 用 `os.killpg`、Windows 用 `taskkill /F /T`（`executor.py`）。注：`resource` 模块仅 Unix 有，Agent 已不依赖它。
- **命令下发优先用 argv 数组**，`cmdline` 走 shlex.split，Windows 上含空格路径（如 `D:\Program Files\...`）会被拆坏。
- **服务端入口是工厂** `kk_server.main:create_app`，没有模块级 `app`；运行走 `python -m kk_server`。测试用 uvicorn.Server 线程 + `create_app(env)`，或用 `httpx.AsyncClient` + `ASGITransport`（`test_api.py` 的做法，不依赖真实端口）。
- 心跳间隔下限 1s（`agent/src/kk_agent/config.load`），集成测试依赖它在数秒内积累多个序列点；别把下限调回去。
- Agent 上线（status）即可在 API 看到主机，但**指标要等首帧心跳**；集成测试的等待条件必须同时检查 `metrics.mem_mb` 非空。
- 插件热加载按 mtime 比较，Windows 文件时间粒度粗：测试写文件后需显式 `os.utime` 递增时间戳。
- 前端轮询定时器统一用 `setPoll()`/`clearPolls()` 管理，不要直接 `setInterval` 散落各处。菜单**完全静态**（`getAsyncRoutes()` 返回 `[]`，走 `router/modules/`），否则 prod 下 fake server 缺失会导致菜单空白。`pnpm build` 要求 `web/mock/` 目录存在（空目录即可）。
- 安全红线：命令黑名单（`KK_CMD_BLACKLIST`）+ 审计（`store.add_audit`）不能绕过；`status` 帧带 token 供服务端校验未授权注册。

## 背景阅读

改协议、Agent 资源策略或部署方式前先读 `docs/design.md`（总体设计）与 `proto/messages.md`（v2 MQTT 主题与帧格式）；执行路线图与缺陷账本在 `docs/completion-plan-mqtt.md`；部署流程在 `README.md`。

## 约定

- 提交信息用中文，`feat/fix/docs/test/chore` 前缀，按模块分批提交；信息里标注覆盖的缺陷编号（P0-x / P1-x / R-x）。
- 所有配置走 `KK_*` 环境变量（双端均是），不引入配置文件。
- 产品术语统一用「主机 / host」；但**不改数据库表名与列名**（`containers` / `pod` 保持历史命名，改名迁移成本换不到功能收益）。
