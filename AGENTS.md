# AGENTS.md — KontainKeeper

Linux 主机（含 K8S 下 vscode-server 容器 IDE）的直连管理与指标提取平台：主机内置 Agent 主动出站 MQTT 连 Broker，服务端无状态桥接（心跳指标 + 远程命令 + 自定义采集）。不使用 K8S 集群能力、不触碰宿主机、对容器用户无感知。

## 一句话架构

```
Agent (kk-agent, psutil 采集 + paho-mqtt 发布)
  │ hb QoS0 / result QoS1
  ▼
Mosquitto 2.x（匿名开放 · LWT 离线 · retain status · QoS1 cmd 离线队列）
  ▼
kk-server (FastAPI + MqttBridge 无状态桥接 + SQLAlchemy async 三库)
  ▼
web/ Vue3 前端（REST 轮询 + ECharts，构建产物由 kk-server 托管）
```

可靠性（连接/重连/离线排队/在线判定）整体交给 Broker，服务端只做「消息路由到 DB / 命令发布到主题」。

## 目录与架构边界

- `agent/src/kk_agent/` — 主机内客户端（**独立 UV 项目**）。**不再是纯标准库**：采集用 `psutil`（跨平台、8 个采集项），传输用 `paho-mqtt`（重连退避/保活/out-queue），可编译为单文件二进制嵌入镜像，常驻 RSS 口径 **25–35MB**。模块：`transport.py`（MQTT，替代已删的 `ws.py`+`conn.py`）、`collector.py`（psutil，含 `collect_items()` 按项采集）、`executor.py`、`updater.py`（自更新 sha256/HMAC）、`main.py`（事件循环）。
- `server/src/kk_server/` — FastAPI 服务端，MVC 分层：`models/`（SQLAlchemy 2 Core + async engine，SQLite/PG/MySQL 三库通用）→ `services/`（`mqtt_bridge.py` 无状态桥接、命令黑名单 security）→ `controllers/`（REST `/api/*`）→ `web/`（Vue3 构建产物，随包打包、服务端直接托管）；`main.py` 的 `create_app` 只做装配。**没有 WS 入口**（`agent_ws.py`/`hub.py` 已删）。
- `web/` — **独立 pnpm 工程**（Vue3 + TS + Element Plus + Vite + Pinia + ECharts，底座 pure-admin-thin v6.2.0）。`src/api/` 业务 API 层、`src/views/` 五个业务页（host/monitor 总览、host/detail 详情、command/shell 命令面板、command/collect 采集面板、audit 审计）、`src/router/modules/kk.ts` 静态路由。`web/dist/` 被 .gitignore 忽略，产物需人工同步到 `server/src/kk_server/web/`。
- `proto/messages.md` — 双端通信协议契约（**v3 = 去 token：匿名 Broker + 服务端 `KK_AGENT_IPS` 白名单，上行帧携带自报 `ip`**）。改协议必须同步：`agent/src/kk_agent/config.py` 的 `PROTO_VER`、`server/src/kk_server/__init__.py` 的 `PROTO_VER`、协议文档、双端测试。
- `agent/tests/`、`server/tests/`、`scripts/build.sh`（把 agent 叠加进 vscode-server 镜像）。
- `Jenkinsfile` — **CI/CD 流水线**（测试 → Agent 二进制 → 服务端镜像 → 镜像冒烟 → 推送 → 部署 → 部署验证），
  走 Jenkins 而非 GitHub Actions（仓库无 `.github/workflows/`）。配套 `scripts/ci_smoke.sh`（镜像级部署冒烟，
  真起容器 + 真跑 Agent 二进制）与 `docs/ci-jenkins.md`（节点要求 / 凭据 ID / 参数 / 排障 / 回滚）。
  `scripts/mqtt_e2e.py` 被流水线复用作 Broker 语义冒烟。

## 常用命令

```bash
# 后端（仓库根目录；uv run 会去下载 Python 3.12 而失败，务必用 .venv 直调）
.venv/Scripts/python.exe -m pytest agent/tests -q      # Agent 单测
.venv/Scripts/python.exe -m pytest server/tests -q      # Server 单测 + 集成
.venv/Scripts/python.exe -m pytest agent/tests server/tests -q   # 全量 236 条：Broker 可达时 236 passed；不可达时 232 passed + 4 skipped（集成用例）
# 汇总别用 `| tail -3`：失败行在进度条之前，会被截掉（曾因此漏看红灯两轮）。
# 要看清结果用 --junitxml 再解析 tests/failures/errors/skipped 计数。
.venv/Scripts/python.exe -m kk_server                   # 起服务端（默认 admin/admin123）

# 前端（web/ 目录）
pnpm dev         # 开发（VITE_PROXY 代理到后端，默认 http://127.0.0.1:8443；可被 VITE_PROXY 覆盖）
pnpm typecheck   # TS 类型检查
pnpm build       # 产物输出到 web/dist/
```

依赖：`uv sync --all-packages`（服务端 + dev）；`--extra postgres` / `--extra mysql` 按需装驱动。前端 `pnpm install`。无 lint 配置，前端有 typecheck。

```bash
# CI/CD（定义在根 Jenkinsfile，节点要求与凭据见 docs/ci-jenkins.md）
docker run -d --name kk-ci-broker -p 127.0.0.1:18830:1883 \
  -v "$PWD/deploy/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" eclipse-mosquitto:2
KK_IT_MQTT_URL=mqtt://127.0.0.1:18830 .venv/Scripts/python.exe -m pytest agent/tests server/tests -q  # 有 Broker 才是 236 passed
KK_MQTT_URL=mqtt://127.0.0.1:18830 .venv/Scripts/python.exe scripts/mqtt_e2e.py                      # Broker 语义冒烟（LWT/离线队列）
docker build -f server/Dockerfile -t kontainkeeper-server:local .                                    # 上下文必须是仓库根
bash scripts/ci_smoke.sh kontainkeeper-server:local agent/dist/kk-agent                              # 镜像级部署冒烟
```

> CI 用的临时 Broker 端口是 **18830**（不是 1883），避免与开发机上的 Broker 抢端口；
> 集成用例读 `KK_IT_MQTT_URL`，`mqtt_e2e.py` 读 `KK_MQTT_URL` —— 两者都要设。

## 关键约束与陷阱

- **Agent 线程模型**：主循环是单线程事件循环，MQTT socket 只由 paho 的后台网络线程触碰；采集/命令/插件在一次性 daemon 线程跑，结果由工作线程直接经 paho 发帧（paho 发布线程安全，不回主线程）；回调里不要做阻塞操作，重活丢给 worker 线程。
- **三库方言差异全部收在 `Store._upsert` / `Store._ensure_schema` 两处**，不要在别处再分叉：MySQL 大字段必须 LONGTEXT（TEXT 仅 64KB，命令输出 base64 最大 5.6MB 会静默截断）、主键必须定长 `String(n)`、upsert 是 `INSERT IGNORE`（SQLAlchemy 2.0 无 `.ignore()`，必须 `prefix_with("IGNORE")`）；PG/SQLite 用 `on_conflict_do_nothing`。
- **分批删按主键 `IN`，不要用 `LIMIT`**：PG 不支持 `DELETE...LIMIT`，MySQL 的是方言专属写法，SQLite 还要编译期开关。
- **`create_all` 只建表不加列**：新增列必须登记 `tables._ADD_COLUMNS`，由 `setup()` 的 `_ensure_schema` 自动 ALTER（SQLite 查 `PRAGMA table_info`，其余查 `information_schema` 且 MySQL 要 `DATABASE()` 限定 schema）。
- **Windows 开发机兼容**：采集基于 psutil（跨平台，不解析 /proc、不注入 fs_root），`agent/tests` 直接读真机/容器指标即可，无需伪造 /proc 树；命令执行的进程树回收按平台分路——POSIX 用 `os.killpg`、Windows 用 `taskkill /F /T`（`executor.py`）。注：`resource` 模块仅 Unix 有，Agent 已不依赖它。
- **命令执行语义**：前端 cmdline 恒 `use_shell=true`（整条命令经 `sh -c`，内网灵活优先，管道/重定向/glob 全支持）；argv 数组直 exec 不经 shell。服务端 API 层对 `use_shell=false` 的 cmdline 仍走 shlex.split——Windows 上含空格路径（如 `D:\Program Files\...`）会被拆坏，调 API 时优先 argv 数组。
- **服务端入口是工厂** `kk_server.main:create_app`，没有模块级 `app`；运行走 `python -m kk_server`。测试用 uvicorn.Server 线程 + `create_app(env)`，或用 `httpx.AsyncClient` + `ASGITransport`（`test_api.py` 的做法，不依赖真实端口）。
- 心跳间隔下限 1s（`agent/src/kk_agent/config.load`），集成测试依赖它在数秒内积累多个序列点；别把下限调回去。
- **自更新窗口的离线语义（B6）**：execv 前 Agent 必须调 `Transport.announce_update()`（发 `reason=updating` 且**等 PUBACK**，否则帧随进程替换一起丢），服务端把它落进 `containers.status_reason`；空 reason 的 LWT 在 120s 内不覆盖它（`store.set_online`）。更新轮询带 ±20% 抖动，同版本失败按 5/10/30min 退避（成功或版本变化清零，门禁在 `apply_manifest_receipt`，轮询与推送两条路径共用）。
- Agent 上线（status）即可在 API 看到主机，但**指标要等首帧心跳**；集成测试的等待条件必须同时检查 `metrics.mem_mb` 非空。
- 插件热加载按 mtime 比较，Windows 文件时间粒度粗：测试写文件后需显式 `os.utime` 递增时间戳。
- 前端**轮询**定时器统一走 `web/src/utils/kkPoll.ts` 的 `usePolls()` + `setPoll(key, fn, ms)`（按 key 覆盖、卸载自动 `clearPolls()`），不要直接 `setInterval` 散落各处；长按等**交互计时器**不在此列（`directives/longpress`），别顺手套上去。菜单**完全静态**（`getAsyncRoutes()` 返回 `[]`，走 `router/modules/`），否则 prod 下 fake server 缺失会导致菜单空白。`pnpm build` 要求 `web/mock/` 目录存在（空目录即可）。
- 安全红线：命令黑名单（`KK_CMD_BLACKLIST`）+ 审计（`store.add_audit`）不能绕过；Agent 接入管控靠上行帧自报 `ip` 按服务端 `KK_AGENT_IPS` 白名单校验（白名单校验收在 `MqttBridge._on_message` 一处入口，REST 自更新接口走 `deps.agent_ip_auth` 的真实源 IP），`KK_ENV=production` 未配白名单直接拒绝启动。

## 背景阅读

改协议、Agent 资源策略或部署方式前先读 `docs/design.md`（总体设计）与 `proto/messages.md`（v3 MQTT 主题与帧格式）；执行路线图与缺陷账本在 `docs/completion-plan-mqtt.md`；生产部署流程在 `docs/deployment.md`，开发环境搭建在 `docs/development.md`，CI/CD 流水线在 `docs/ci-jenkins.md`。

## 约定

- 提交信息用中文，`feat/fix/docs/test/chore` 前缀，按模块分批提交；信息里标注覆盖的缺陷编号（P0-x / P1-x / R-x）。
- 所有配置走 `KK_*` 环境变量（双端均是），不引入配置文件。
- 产品术语统一用「主机 / host」；但**不改数据库表名与列名**（`containers` / `pod` 保持历史命名，改名迁移成本换不到功能收益）。
