# KontainKeeper 项目手册

> 版本口径：服务端 `0.1.0` / Agent `0.3.0` / 协议 `v4`（兼容窗口 `(3,4)`）。
> 本手册基于最新代码（含 QR1124 动态 IP 白名单、QR1125 Phase 0 HTTP/SSE 双通道、QR1126 PG18 调优）编写，
> 是面向**开发者、运维与二次开发者**的综合指南。深入细节交叉引用：
> 架构 [`design.md`](design.md) · 开发环境 [`development.md`](development.md) · 部署 [`deployment.md`](deployment.md) ·
> 协议 [`../proto/messages.md`](../proto/messages.md) · 传输 redesign [`design-transport.md`](design-transport.md) ·
> CI [`ci-jenkins.md`](ci-jenkins.md) · 真环境 E2E [`e2e-real-env.md`](e2e-real-env.md)。

## 目录

1. [项目概述](#1-项目概述)
2. [快速开始](#2-快速开始)
3. [本地开发](#3-本地开发)
4. [部署](#4-部署)
5. [详细使用手册（Web 界面）](#5-详细使用手册web-界面)
6. [客户端（Agent）使用与配置](#6-客户端agent使用与配置)
7. [服务端配置与 API 参考](#7-服务端配置与-api-参考)
8. [二次开发指南](#8-二次开发指南)
9. [运维与排障](#9-运维与排障)
10. [附录：文档索引与术语](#10-附录文档索引与术语)

---

## 1. 项目概述

**KontainKeeper** 是 Linux 主机（含 K8S 下 vscode-server 容器 IDE，但不限于容器）的**直连管理与指标提取平台**。
主机内置 Agent 主动**出站**连接服务端，服务端无状态桥接，提供心跳指标、远程命令、自定义采集与自更新。
**不使用 K8S 集群能力、不触碰宿主机、对容器用户无感知**；规模目标 **500 台**。

### 1.1 架构（双通道并存）

```
┌──────────────────────── 管理端（独立部署） ────────────────────────┐
│  Web UI (Vue3 + Element Plus + ECharts，构建产物由服务端托管)        │
│        │ REST 轮询（总览 10s / 详情 30s / 命令历史自适应 3s/10s）     │
│  kk-server (FastAPI)                                             │
│   ├─ REST API：主机/指标/命令/审计/统计/RBAC/IP白名单/自更新          │
│   ├─ MqttBridge：MQTT 无状态桥接（订阅 hb/status/result，发布 cmd）  │
│   ├─ HTTP ingest + SSE 下行（QR1125 Phase 0，与 MQTT 并存）         │
│   └─ Store：SQLAlchemy 2 Core + async（SQLite / PG / MySQL 三库）   │
└──────────────▲──────────────────────────▲────────────────────────┘
               │ MQTT（经 Broker）          │ HTTP push 上行 + SSE 下行（直连）
┌──────────────┴───────────────┐           │
│  Mosquitto 2.x（MQTT Broker） │           │
│  LWT 离线 / retain status /   │           │
│  持久会话 QoS1 离线命令排队     │           │
└──────────────▲───────────────┘           │
               │ 出站 MQTT                  │ 出站 HTTP/SSE
┌──────────────┴───────────────────────────┴───────────────────────┐
│  主机内 Agent（kk-agent，psutil 采集 + paho-mqtt，单文件二进制）      │
│  采集 8 项 / 命令执行 / 插件热加载 / 自更新（RSS 25–35MB）            │
└──────────────────────────────────────────────────────────────────┘
```

两条核心思路：
1. **主机发起出站连接（反向通道）**——不动宿主机、不用 K8S 入站，主机主动连出去，**不开入站端口**。
2. **可靠性分层**——MQTT 通道把连接保活/重连/离线排队/在线判定（LWT）交给 Broker；HTTP/SSE 通道（QR1125）
   用 DB 补偿层（queued 命令队列 + 重连 flush + sweep 兜底）承担同等职责，目标是逐步**减少独立 Broker 依赖**。

> **通道现状**：MQTT 是生产主通道；HTTP/SSE 为 QR1125 Phase 0 已落地的并存通道（上行 ingest 仍需配置
> `KK_MQTT_URL`，Agent 侧 `KK_TRANSPORT` 切换属 Phase 1 未实施）。详见 [`design-transport.md`](design-transport.md)。

### 1.2 技术栈与仓库结构

| 层 | 技术 |
|---|---|
| Agent | Python 3.12，psutil 采集 + paho-mqtt 传输，PyInstaller 单文件二进制（独立 uv 项目） |
| 服务端 | FastAPI + SQLAlchemy 2 Core async（SQLite/PG/MySQL）+ loguru，MVC 分层 |
| 前端 | Vue3 + TS + Element Plus + Vite + Pinia + ECharts（pure-admin-thin v6.2.0 底座，独立 pnpm 工程） |
| Broker | Mosquitto 2.x（ubuntu 自建镜像，生产清单无 eclipse-mosquitto） |
| 数据库 | 默认 SQLite；可选 PostgreSQL 18.6（QR1126 调优）/ MySQL |

```
agent/src/kk_agent/      # 主机客户端：transport/collector/executor/updater/plugin_loader/main/config
server/src/kk_server/    # 服务端：models/(Store+域Mixin) services/(bridge/sse_hub/downlink/security)
                         #         controllers/(REST) web/(前端构建产物) main.py(create_app)
web/                     # Vue3 前端工程（src/api 业务API层 / src/views 业务页 / src/utils kk*）
proto/messages.md        # 双端协议契约 v4（含 §8 HTTP/SSE 绑定）
docker/                  # Dockerfile + broker/ + compose{,.prod,.offline,.pg,.secure-mqtt}.yml + offline/
scripts/                 # build.sh / install.sh / quality_gate.sh / mqtt_e2e.py / db_smoke.py / pg_wsl_relay.py
deploy/systemd/          # kk-agent.service（非容器主机）
docs/                    # 本手册与设计/部署/评审文档
```

---

## 2. 快速开始

### 2.1 一键全栈（开发/演示）

```bash
# 仓库根目录；匿名 Broker + 未配白名单（=放行所有，仅开发）
docker compose -f docker/docker-compose.yml up --build
# 浏览器打开 http://localhost:8443，默认账号 admin / admin123
```

### 2.2 让一台主机上报

容器场景把 Agent 叠加进你的镜像（见 [§6.2](#62-容器镜像叠加)）；非容器 Linux 主机：

```bash
sudo KK_SERVER=mqtt://<broker地址>:1883 bash scripts/install.sh
# 服务端「系统管理 → IP 白名单」放行该主机 IP（或开发期留空放行全部）
```

Agent 上线后在「主机总览」即可看到；**指标要等首帧心跳**（默认 60s，可 `KK_INTERVAL=1` 加速）。

### 2.3 生产部署最小命令

```bash
cp .env.example .env   # 填 KK_AGENT_IPS / KK_ADMIN_PASS（必填）
docker compose -f docker/docker-compose.prod.yml --env-file .env up -d
```

生产启用 `KK_ENV=production` 两道自检：默认口令拒启、白名单（env 或 DB 任一非空）拒启。详见 [§4](#4-部署)。

---

## 3. 本地开发

> 完整步骤见 [`development.md`](development.md)；本节是速查与关键约定。

### 3.1 前置

- Python 3.12（uv 管理）、Node ^20.19 或 ≥22.13、pnpm（前端强制）、Docker（起 Broker/PG）。
- **Windows 开发机铁律**：`uv run` 会尝试下载 Python 而失败，**务必用 `.venv` 直调**：
  `.venv/Scripts/python.exe`（Linux/macOS 为 `.venv/bin/python`）。

### 3.2 安装依赖与起服务

```bash
# 后端（仓库根）：uv workspace（server + agent）
uv sync --all-packages                 # 服务端 + dev；--extra postgres / --extra mysql 按需

# 起 Broker（三种任选）
docker build -f docker/broker/Dockerfile -t kk-broker:latest .
docker run -d -p 1883:1883 -v "$PWD/docker/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" kk-broker:latest

# 起服务端（默认 admin/admin123，SQLite）
KK_MQTT_URL=mqtt://127.0.0.1:1883 .venv/Scripts/python.exe -m kk_server

# 前端（web/ 目录）
pnpm install
pnpm dev                               # http://localhost:8848，/api/* 代理到 127.0.0.1:8443（VITE_PROXY 可覆盖）
```

### 3.3 起 Agent（本地调试）

```bash
KK_SERVER=mqtt://127.0.0.1:1883 KK_INTERVAL=1 .venv/Scripts/python.exe -m kk_agent
```

### 3.4 测试与质量门禁

**唯一门禁入口** `bash scripts/quality_gate.sh [all|be|fe]`：
- 后端：`pytest agent/tests server/tests` 全量，以 **junitxml 计数为准**——`failures=0`、`errors=0`、`skipped≤5`
  （无 Broker 时恰好 5 条跳过：4 条 Broker 级集成 + 1 条 POSIX-only；`>5` 即红）。
- 前端：`pnpm typecheck` + `pnpm test`（vitest）。
- **提交前必须全绿**；禁止跳过/注释测试。汇总**别用 `| tail`**（失败行在进度条前会被截掉），用 junitxml 解析。

```bash
# 手工等价（后端）
.venv/Scripts/python.exe -m pytest agent/tests server/tests -q --junitxml=test-results.xml
.venv/Scripts/python.exe -c "import xml.etree.ElementTree as ET; s=ET.parse('test-results.xml').getroot().find('testsuite'); print({k:s.get(k) for k in ('tests','failures','errors','skipped')})"
rm test-results.xml
# 有 Broker 时全 passed：KK_IT_MQTT_URL=mqtt://127.0.0.1:1883（集成用例）
```

前端脚本：`pnpm dev / build / typecheck / test / test:watch / test:coverage / lint:eslint`（CI 零豁免）。
**`pnpm build` 产物在 `web/dist/`，需人工同步到 `server/src/kk_server/web/`**（服务端托管该目录）；`web/mock/` 目录须存在（空即可）。

### 3.5 真 PostgreSQL 18 开发（WSL）

```bash
# WSL 内装 PG18（端口看 pg_lsclusters，本机 5433）；Windows 侧免管理员转发：
.venv/Scripts/python.exe scripts/pg_wsl_relay.py --listen 15432 --pg-port 5433
# 服务端切 PG
KK_DB_URL=postgresql://kk:kk-pg-pass@127.0.0.1:15432/kontainkeeper .venv/Scripts/python.exe -m kk_server
# 整套测试打 PG（每测试 TRUNCATE + 重放种子隔离）
KK_TEST_DB_URL=postgresql://kk:kk-pg-pass@127.0.0.1:15432/kk_test .venv/Scripts/python.exe -m pytest server/tests -q
```

### 3.6 关键开发约定（承重）

- **三库方言差异只许在 store 既定汇聚点**：`_engine_kwargs` / `Store._upsert` / `_ensure_schema`+`_pg_tune` /
  批删助手 `_delete_batched`/`_purge_outputs`。MySQL 大字段 LONGTEXT、主键定长 `String(n)`、upsert `prefix_with("IGNORE")`；
  新列必须登记 `tables._ADD_COLUMNS`（`create_all` 只建表不加列）。
- **日志只用 loguru**（Agent `logutil.get_logger` / Server `logsetup.get_logger`），`%s` 懒格式化，**不写 f-string 拼消息**、
  新代码不 `import logging`；维度用 `log.bind(host=…, cmd=…)`；生产 `diagnose=False`，日志不含口令/token。
- **前端轮询统一 `usePolls()`**（`web/src/utils/kkPoll.ts`，按 key 覆盖、在途防重入、卸载只清本页）；菜单**完全由 DB 下发**，
  新增页面必须同步 `_store_rbac.py` 内置菜单种子。
- **安全红线**：命令黑名单 + 审计不可绕；IP 白名单校验收在 `MqttBridge._route_message` 单入口（MQTT）/ `agent_ip_auth` 真实源 IP（HTTP）。
- 提交信息中文，`feat/fix/docs/test/chore` 前缀，按模块分批，标注缺陷编号（QRxxxx）。

---

## 4. 部署

> 权威细节见 [`deployment.md`](deployment.md)；本节覆盖形态与关键决策。

### 4.1 部署形态

| 形态 | 文件 | 适用 |
|---|---|---|
| 开发/演示全栈 | `docker/docker-compose.yml` | 匿名 Broker、放行全部，本机试用 |
| 生产全栈 | `docker/docker-compose.prod.yml` | `KK_AGENT_IPS`/`KK_ADMIN_PASS` 强制必填、`KK_ENV=production` |
| 离线内网 | `docker/docker-compose.offline.yml` | 纯 image 引用无 build（内网 build 会联网失败）；镜像经 `offline/pack.sh`→`load.sh` |
| 可选 PG18 | `docker/docker-compose.pg.yml`（override） | 叠加 prod/offline 把 SQLite 换调优 PG18（QR1126） |
| 可选 MQTT 鉴权 | `docker/docker-compose.secure-mqtt.yml`（override） | Broker 双账号 passwd/aclfile 加固 |

```bash
# 生产 + PG18
docker compose -f docker/docker-compose.prod.yml -f docker/docker-compose.pg.yml --env-file .env up -d
# 离线 + PG18（外网打包需 PACK_OPT=1 打入 postgres:18.6）
docker compose -f docker/docker-compose.offline.yml -f docker/docker-compose.pg.yml --env-file .env up -d
```

### 4.2 镜像合规（生产清单约束）

可用镜像：`python:3.12` / `node:20` / `nginx:1.20` / `postgres:18.6` / `redis:7.2.11` / `mongo:8.2.3` / `ubuntu:latest`
（redis/mongo 本项目不用）。**不含 eclipse-mosquitto**——Broker 由 `docker/broker/Dockerfile` 基于 ubuntu 自建 `kk-broker:latest`；
服务端镜像 `FROM python:3.12` + pip 装 uv + `uv sync --extra postgres`（bundled asyncpg，PG 开箱可用）；基础镜像留 ARG，内网 registry 用 `--build-arg` 覆盖。

### 4.3 非容器主机（物理机/VM）

`scripts/install.sh` + `deploy/systemd/kk-agent.service`：同一个 `agent/dist/kk-agent` 二进制、同一套 `KK_*`，
supervisor 换成 systemd `Restart=always`。**env 文件只创建不覆盖**、二进制 sha256 未变不 restart、单元刻意不做沙箱
（收口文件系统会让 root 命令通道静默失效）。测试钩子 `KK_INSTALL_BIN`/`KK_INSTALL_UNIT_DIR`/`KK_INSTALL_DRY_RUN`
（不带钩子会往 `/etc/systemd/system` 真装，别在开发机 WSL 裸跑）。

### 4.4 TLS 反向代理（生产硬约束）

管理界面必须经 HTTPS 反代（会话 token 明文跨网会被截获）；kk-server 只听 HTTP，前置 nginx 终结 TLS，随后收回 8443 公网暴露。
**SSE 通道必须关缓冲**：

```nginx
location /api/v1/commands/stream {
    proxy_pass http://127.0.0.1:8443;
    proxy_buffering off;            # 关键：SSE 逐帧透传
    proxy_read_timeout 300s;        # > 15s ping 间隔
    proxy_http_version 1.1;
    proxy_set_header Connection "";
}
```

> **源 IP 口径**：HTTP ingest / SSE / 自更新下载用 `agent_ip_auth` 读**真实 TCP 源 IP**（不读 X-Forwarded-For）。
> 经公网反代时源 IP 退化为代理地址——**Agent 的 `KK_SERVER`/`KK_UPDATE_URL` 应配内网直连地址**，不走公网反代。
> MQTT 通道不受影响（自报 ip + Broker 直连）。

### 4.5 安全自检与白名单

`KK_ENV=production` 触发两道启动自检：① 默认口令 `admin123` 拒启；② 白名单 **env 或 DB 任一非空**才放行
（QR1124：`KK_AGENT_IPS` 退为启动种子，DB `kk_ip_rules` 是运行时真相，界面可配、即时生效无需重启）。
跨不可信网络 MQTT 必须 `mqtts://` 且**配 CA**（无 CA 仅加密不认证，双端打 warning）。

### 4.6 CI/CD 与离线包

- **Jenkins**（`Jenkinsfile`，非 GitHub Actions）：测试 → Agent 二进制 → 服务端镜像 → 镜像冒烟 → 推送 → 部署 → 验证；
  夜测真库（postgres:18.6 + `scripts/db_smoke.py` 回读 PG18 调优 catalog）。详见 [`ci-jenkins.md`](ci-jenkins.md)。
- **离线包**：外网 `docker/offline/pack.sh`（`PACK_OPT=1` 含可选镜像）→ `images/*.tar.gz` + SHA256SUMS → 内网 `load.sh` 导入。

---

## 5. 详细使用手册（Web 界面）

### 5.1 登录与角色

- 登录页：账号 + 密码（**不预填默认凭据**），Enter 提交，右上角明暗切换。默认 `admin / admin123`（仅非 production）。
- **限流**：同用户名**或**同 IP 连续失败 5 次锁 300s（429）；禁用账号口令再对也 403。
- **密码口径提示**：登录页前端要求 8–18 位、数字/字母/符号任意两种组合；用户管理创建只要求 ≥6 位——
  **建议统一用 ≥8 位混合密码**，否则短密码账号会被登录表单挡住。
- **角色（权限只控菜单）**：`admin`（全部菜单，含系统管理）、`operator`（业务菜单，**无系统管理 Rbac 子树**）。
  菜单完全由 DB 下发（`/api/auth/menus`），未授权菜单**根本不注册**（直连 URL 落 404，非 403）；接口失败 fail-closed 空树。
- **无自助改密端点**：改密/重置走管理员「用户管理」编辑（`PUT /api/system/users/{username}`）。

### 5.2 菜单树与轮询速查

| 组 | 菜单 | 路由 | 权限 | 轮询 |
|---|---|---|---|---|
| 首页 | 首页 | `/welcome` | 全员 | 10s 固定 |
| 主机管理 | 主机总览 | `/hosts/monitor` | 全员 | 默认 10s（5/10/30/停） |
| | 主机详情 | `/hosts/detail/:pod`（隐藏，下钻） | 全员 | 30s 固定 |
| | 使用趋势 | `/hosts/trend/:pod`（隐藏，下钻） | 全员 | 30s 固定 |
| | 版本与更新 | `/hosts/update` | 全员 | 默认 10s（5/10/30/停） |
| 命令中心 | 命令面板 | `/command/shell` | 全员 | 历史自适应 3s/10s |
| | 采集面板 | `/command/collect` | 全员 | 同上 |
| 审计日志 | 审计日志 | `/audit/index` | 全员 | 无自动轮询 |
| 系统统计 | 系统统计 | `/system/index` | 全员 | 默认 30s（停/10/30/60） |
| 系统管理 | 用户管理 | `/rbac/users` | **admin** | 无 |
| | 角色管理 | `/rbac/roles` | **admin** | 无 |
| | 菜单管理 | `/rbac/menus` | **admin** | 无 |
| | IP 白名单 | `/rbac/ip-whitelist` | **admin** | 变更即刷 |
| | 拦截记录 | `/rbac/ip-rejects` | **admin** | 默认 30s（10/30/60） |

### 5.3 首页 Welcome

四张可点统计卡：**主机总数**（在线/离线）、**磁盘告警**（>0 红色，列最多 5 台告警主机）、**命令总数**（失败/执行中）、
**Agent 当前版本**（落后 N 台）。左栏最近命令表（12 条，行点击跳命令中心）；右栏快速入口、离线主机（最多 5 台）、
系统状态（Broker 链路三态：正常/断开/**未知（没读到）**、服务/协议版本、心跳样本、运行时长、升级汇总）。
红线语义：「没读到」≠「读到 0」，每路数据独立记账。

### 5.4 主机总览 HostMonitor

- **机队仪表带**：在线/离线比例条 + 「N 台 / N 在线 / N 磁盘告警 / N 待升级」+ 同步时刻（失败变冷）。
- **筛选**（全下推服务端）：关键字（主机名/镜像/系统/分组）、类型（物理机/虚拟机/容器）、协议（v4/v3）、仅在线/仅告警、清空。
- **选中横幅**：已选 N 台 + **批量采集**（采集项勾选对话框）/ **批量执行命令**（跳命令面板带 pods）/ **批量升级**（含落后主机时可用）。
- **表格列**：勾选（跨页保留）/主机（链接跳详情）/状态四档圆点/类型/系统/地址/CPU 计量条/内存/磁盘计量条（≥85% 变红）/
  Agent 版本（落后琥珀色）/最近心跳（三格信号：≤30s 满格/≤2min 两格/≤10min 一格）/操作（详情/趋势/采集）。
- 前端分页 100/200/500，默认 last_seen 倒序；「导出清单」CSV 与筛选同参数。

### 5.5 主机详情 HostDetail

页头：主机名 + 在线三态圆点（在线/更新中/离线）+ 磁盘告警红点 + 最近心跳。按钮：时间窗（6h/24h/7d，默认 24）/刷新/
**在此主机执行命令**/**使用趋势**/**升级到 vX**（本机落后时）/**导出指标**（CSV，>24h 走小时聚合）。
描述列表（镜像/版本/间隔/系统/CPU/内存/磁盘 IO/运行时长/标签/内存上限/磁盘上限）+ 自定义采集键值流 +
**资源曲线**（ECharts 双轴 CPU%/内存 MB）+ 四张表（磁盘逐挂载点/网卡速率/Top 进程/登录用户）。

### 5.6 使用趋势 HostTrend

内存使用率% 与磁盘使用率%（多挂载点取最大）双图，各带越限 markLine。**上限编辑**：内存/磁盘上限数字输入（1–100，空=清除回落全局 85）+
保存（审计 `host_limits_set`）。时间窗 6h/24h/7d（7d 注明小时聚合）。

### 5.7 版本与更新 HostUpdate

页头读数（待分发版本/落后 N 台/最近上传/size+sha256）。**升级台账**（最近 50 条，状态汇总）。
主表「待升级主机」（勾选跨页保留）+ **选中所有落后主机**。**上传新版本**（版本号+二进制+进度条）、
**回滚待分发**（当前↔上一版互换，只影响新上线/重启 Agent，再点撤销）、**批量升级**（离线主机标 queued 上线补投）。

### 5.8 命令面板 CommandShell / 采集面板 CommandCollect

双栏（左表单/右历史）。**目标主机** HostPicker 抽屉（搜索/仅在线/多选/全选反选/已选 chips）。
- 命令面板：输入方式 radio——**命令行**（恒经 `sh -c`，支持管道/重定向/变量）/ **argv 数组**（每行一参数，直 exec）；超时 1–600s（默认 30）；
  Enter/Ctrl+Enter 下发；多主机确认框如实告知离线排队。
- 采集面板：采集项 checkbox（清单来自后端 `/api/collect/items` 白名单，**加载失败显式报错+重试，绝不摆硬编码清单**），默认预勾 cpu/mem/disk。
- **安全**：唯一约束是服务端黑名单 `KK_CMD_BLACKLIST`（命中 400+审计 `command_blocked`）；Agent `KK_ALLOW_SHELL=0` 可关 sh -c。
- **执行历史**（右栏共用）：自适应轮询（有未终态 3s/全终态 10s）；筛选关键字/状态（pending/queued/sent/running/done/failed/timeout/lost）/批次；
  导出 CSV；**输出抽屉**（>256K 字符切片渲染：头 128K+尾 64K，中段折叠，可下载）。

### 5.9 审计日志 AuditIndex

无自动轮询。筛选关键字（300ms 防抖）/操作者/动作下拉（31 种中文映射，含 `agent_ips_update`/`ip_rejected`/`command_blocked` 等）；
导出 CSV（导出=所见同筛选）；列 #/时间/操作者/动作圆点/明细 chips（>120 字符截断+tooltip）。operator 可见 `ip_rejected` 事件。

### 5.10 系统统计 SystemStats（排障面板）

Broker 链路四态 + 机队/版本卡 + 命令状态分布 + 存储卡（heartbeats/hourly 行数 + **两次刷新间增量**）+
**桥接计数器表**（19 个已知计数器各配「什么值该担心」提示；后端新增键自动补列标「尚未收录说明」——QR1125 的 `ingest_*` 暂以未收录行出现）。
红线：没读到显「—」不补 0。

### 5.11 系统管理（admin-only）

- **用户管理**：新增（用户名 2–64/密码 ≥6/角色）、编辑（改密/启停）、删除（不能删自己）。**最后一个启用管理员不可禁用/降级/删除**；禁用即吊销会话。
- **角色管理**：新增角色 + el-tree 菜单授权；admin 授权固定全量不可改；内置角色/有用户的角色不可删。
- **菜单管理**：树表编辑标题/图标/排序/侧边栏显隐/启用；**标识与组件只读，不支持新建/删除菜单**（页面集合固定，新建只会 404）。
- **IP 白名单**（QR1124）：新增 CIDR（前端轻量校验 + 服务端权威闸，非法 422/重复 409）、表内启停 switch、编辑/删除；
  **停用/删除最后一条启用规则**警告「白名单为空将放行所有上报」；变更即时生效无需重启（审计 `agent_ips_update`）。
- **拦截记录**（QR1124）：按源 IP 聚合（hits/最近主机/帧类型/首末次），IP 模糊筛选 + 自动刷新 + 导出 CSV。operator 不可见本页（可经审计查 `ip_rejected`）。

---

## 6. 客户端（Agent）使用与配置

### 6.1 运行形态

- **容器**：单文件二进制叠加进镜像（[§6.2](#62-容器镜像叠加)），entrypoint-wrapper 后台监管（崩溃 5s 拉起，execv 自更新复用同 PID）。
- **非容器**：`scripts/install.sh` + systemd（[§4.3](#43-非容器主机物理机vm)）。
- **源码调试**：`KK_SERVER=mqtt://… .venv/Scripts/python.exe -m kk_agent`。

Agent 全部配置走 `KK_*` 环境变量（**无配置文件**）。`KK_SERVER` 必填（缺则拒启动）。

### 6.2 容器镜像叠加

```bash
KK_SERVER=mqtt://broker:1883 BASE_IMAGE=<你的vscode-server镜像> scripts/build.sh <输出tag>
# 流程：编译单文件二进制 → 读原镜像 ENTRYPOINT/CMD → 叠加 COPY 二进制/plugins/wrapper → 烧入 KK_* ENV
```

产物运行时不依赖容器内 Python。wrapper 变量：`KK_SUPERVISOR_LOG`（默认 stderr，**绝不能指向 `KK_LOG`**——一个文件一个写入者）、`KK_NICE`（默认 19）。

### 6.3 Agent KK_* 配置表

| 键 | 默认 | 含义 |
|---|---|---|
| `KK_SERVER` | 空（必填） | `mqtt://host[:port]` / `mqtts://`（1883/8883） |
| `KK_MQTT_USERNAME`/`KK_MQTT_PASSWORD` | 空 | Broker 鉴权（匿名留空） |
| `KK_TOPIC_PREFIX` | `kk/v1` | 主题前缀，**双端必须一致**，禁含 `+`/`#` |
| `KK_KEEPALIVE` | 60（≥10） | MQTT keepalive 秒 |
| `KK_TLS_CA`/`KK_TLS_INSECURE` | 空/false | mqtts CA / 跳过校验 |
| `KK_CLIENT_ID` | `kk` | 前缀，实际 `{值}-{host}` |
| `KK_ADVERTISE_IP` | 空=自动探测 | 自报出口 IP（白名单用） |
| `KK_HOST_NAME` | gethostname() | 主机唯一标识（正则约束，克隆机必改） |
| `KK_IMAGE`/`KK_GROUP`/`KK_LABELS` | 空 | 镜像标识/单值分组/`k=v,k2=v2` 标签 |
| `KK_INTERVAL` | 60（≥1） | 心跳秒，±10% 抖动 |
| `KK_DISK_PATHS`/`KK_TOP_N` | 空/5 | 指定挂载点 / proc Top N（1–50） |
| `KK_HB_ITEMS` | 空=全采 8 项 | 心跳采集项精简 |
| `KK_PLUGIN_DIR`/`KK_PLUGIN_TIMEOUT` | 包内 plugins/5（≥1） | 插件目录 / collect 超时（超时隔离到 mtime 变化） |
| `KK_MAX_OUT_MB`/`KK_MAX_WORKERS`/`KK_MAX_QUEUED` | 4/8/128 | 单命令输出封顶 / 线程池并发 / 断线 out-queue（≈8MB） |
| `KK_UPDATE_URL`/`KK_UPDATE_INTERVAL` | 空/300（≥30） | 管理 API 基址 / 轮询秒（±20% 抖动） |
| `KK_UPDATE_DISABLED` | false | **同时关轮询与推送** |
| `KK_UPDATE_HMAC_KEY`/`KK_UPDATE_REQUIRE_SIG`/`KK_UPDATE_ALLOW_UNSIGNED` | 空/false/false | 验签密钥 / 无签名一律拒 / 仅推送路径放行未签名 |
| `KK_UPDATE_INSECURE`/`KK_AGENT_BIN` | 空/空 | 下载跳过 TLS / 自替换目标（源码形态不自替换） |
| `KK_ALLOW_SHELL` | `1` | use_shell 二次开关（`0` 关闭） |
| `KK_LOG`/`KK_LOG_LEVEL` | 空(只 stderr)/INFO | 日志文件（loguru 独占轮转）/级别 |

> Agent 侧**无 `KK_UPDATE_MODE`**（升级策略纯服务端键）。

### 6.4 采集项与自定义插件（二次开发）

**8 个内置采集项**：`cpu`（总%/核数/load）、`mem`（总量/已用/%/swap）、`disk`（逐挂载点 total/used/pct）、
`disk_io`（读写 MB/s/iops，差分型）、`net`（逐网卡收发 MB/s/pps）、`proc`（Top N 按 CPU）、`user`（登录用户）、`sys`（os/kernel/arch/uptime）。

**自定义采集插件**（`KK_PLUGIN_DIR` 下 `*.py`，**下划线开头不加载**）：

```python
# /opt/kk-agent/plugins/myprobe.py  （勿以 _ 开头）
def collect() -> dict:
    # 每次心跳前调用；≤ KK_PLUGIN_TIMEOUT 秒返回；JSON 可序列化；序列化后 ≤64KB
    return {"queue_len": 3, "service_ok": True}
```

- 契约：**只有 `collect() -> dict`**（无参数），返回值汇入心跳 `custom` 字段 `{<插件名>: data}`。
- **mtime 热加载**（改文件即生效，无需重启）；超时/坏输出**隔离到 mtime 变化**；偶发异常只 warning。
- `kind=plugin_reload` 命令立即触发一轮重扫。现成示例 `agent/src/kk_agent/plugins/_example.py`（去下划线改名即生效）。
- **没有「命令插件」机制**：命令 kind 只有 `shell/collect/plugin_reload`（+内部 `update`）。自定义命令逻辑请用 `kind=shell` 下发 argv 调主机脚本。

### 6.5 自更新（双端联动）

三条路径收口到 `updater.apply_manifest_receipt`：① Agent 轮询 `/agent/latest`；② auto 模式上线自动推；③ manual 模式人工选机 `POST /api/system/agent/upgrade`。
应用清单铁律：版本不更新跳过 → 流式下载边写边算 sha256（64MB/300s 封顶）→ size→sha256→HMAC 校验 → copy2 旧版到 `.kkbak` →
`os.replace` 原子替换 → execv 前发 `reason=updating` 等 PUBACK → `os.execv`；失败回滚 `.kkbak`，同版本失败按 5/10/30min 退避。
**权威成功佐证是重启后 status 帧的新 agent_ver**（execv 前来不及发回执也不算漏）。服务端上传/回滚见 [§7.3](#73-自更新接口)。

---

## 7. 服务端配置与 API 参考

### 7.1 服务端 KK_* 配置表

| 键 | 默认 | 含义 |
|---|---|---|
| `KK_DB_PATH`/`KK_DB_URL` | `kk-server.db`/空 | SQLite 路径 / 选库 URL（PG/MySQL，优先于 PATH） |
| `KK_DB_POOL_SIZE`/`KK_DB_MAX_OVERFLOW` | 10/20 | **仅 PG** 连接池 |
| `KK_ADMIN_USER`/`KK_ADMIN_PASS` | admin/admin123 | 初始管理员（production 默认口令拒启） |
| `KK_CMD_BLACKLIST` | `rm -rf /,mkfs,reboot,…` | 命令黑名单（逗号分隔，小写归一） |
| `KK_INTERVAL_MIN` | 空=不检查 | 最小上报间隔**检测**（低于照常落库，只审计+计数） |
| `KK_AGENT_IPS` | 空=放行全部 | 白名单**启动种子**→播种 DB（QR1124）；production 且 env+DB 皆空拒启 |
| `KK_MQTT_URL` | 空 | Broker 地址；**空=只读管理模式**（无指标/命令通道，POST /commands 503） |
| `KK_TOPIC_PREFIX`/`KK_MQTT_CLIENT_ID` | kk/v1 / kk-server | 前缀（双端一致）/ **多实例必须逐实例唯一** |
| `KK_MQTT_KEEPALIVE`/`KK_MQTT_USERNAME`/`KK_MQTT_PASSWORD`/`KK_MQTT_TLS_*` | 60/空/空/空 | Broker 保活/鉴权/TLS |
| `KK_PUBLIC_URL` | 空 | 对外基址，配了下发绝对下载 url（镜像零配置可升级） |
| `KK_UPDATE_MODE` | `manual` | manual=不自动推+轮询答否；auto=自动推+轮询（未知回落 manual） |
| `KK_UPDATE_QUEUE_TTL`/`KK_CMD_QUEUE_TTL` | 7d/24h（≥3600） | queued 升级台账 / SSE queued 命令存活上限 |
| `KK_UPDATE_HMAC_KEY` | 空 | 上传时对二进制算 HMAC 写清单 sig（与 Agent 一致） |
| `KK_DROP_PROTO_V3` | false | 关兼容窗口只收 v4（**关前确认 `proto_v3_received` 归零**） |
| `KK_AGENT_BIN_DIR`/`KK_WEB_DIR` | agent_assets/包内 web | 待分发二进制目录 / 前端静态目录 |
| `KK_ENV` | 空 | `production` 触发两道自检 |
| `KK_LOG`/`KK_LOG_LEVEL`/`KK_LOG_JSON` | 空(只 stdout)/INFO/false | 日志文件/级别/JSON Lines |
| `KK_HOST`/`KK_PORT` | 0.0.0.0/8443 | uvicorn 监听 |

### 7.2 REST API 目录

鉴权三类：**登录会话**（`Authorization: Bearer <token>`）、**AdminUser**（登录+实时查库 admin）、**agent_ip_auth**（真实 TCP 源 IP 白名单）。

**认证 / 健康**
| 方法 路径 | 鉴权 | 用途 |
|---|---|---|
| GET `/api/health` | 无 | 探活 `{ok,version,proto_ver,agents_online,broker,bridge}` |
| POST `/api/login` | 无 | `{username,password}`→`{token,username,roles}`；限流 5 次锁 300s |
| POST `/api/logout` | 无 | 删会话 |
| GET `/api/me` | 登录 | `{username,roles}` |
| GET `/api/auth/menus` | 登录 | 当前角色可见菜单（前端动态注册，fail-closed） |

**主机 / 指标**（登录）
| 方法 路径 | 用途 |
|---|---|
| GET `/api/containers` | 列表 `view=summary/full` + 筛选 `group/host_type/online/proto_ver/keyword/alert/sort/order` + `limit/offset` |
| GET `/api/containers/{pod}` | 详情 + 最近 20 条命令 |
| GET `/api/containers/{pod}/metrics` | 指标序列 `hours`（1–2160；≤24h raw，>24h hourly） |
| PUT `/api/containers/{pod}/limits` | 单机告警上限 `{mem_limit?,disk_limit?}`（1–100 或 null） |

**命令 / 采集**（登录）
| 方法 路径 | 用途 |
|---|---|
| GET `/api/collect/items` | 采集项白名单 |
| POST `/api/commands` | 批量下发 `{pods[],kind,argv[]/cmdline,items[],use_shell,timeout}`；黑名单 400；无通道 503 |
| GET `/api/commands/batches` | 最近批次分布（注册在 `/{cid}` 前） |
| GET `/api/commands` | 列表 `pod/batch/status/kind/keyword/limit/offset` |
| GET `/api/commands/{cid}` | 详情（只给 out_tail，全量输出走 /out） |
| GET `/api/commands/{cid}/out` | 完整输出 `format=text/base64` |

**导出**（CSV，UTF-8 BOM + 公式注入防护，上限 20000 行，落审计）
| 方法 路径 | 鉴权 |
|---|---|
| GET `/api/export/hosts` `/commands` `/audit` `/metrics` | 登录 |
| GET `/api/export/ip-rejects` | **AdminUser** |

**审计 / 统计**（登录）
| 方法 路径 | 用途 |
|---|---|
| GET `/api/audit` | `limit/offset/keyword/actor/action` |
| GET `/api/system/stats` | `{hosts,commands,storage,agent_latest_ver,agents_outdated,updates,broker:{connected,stats,last_msg_age_sec}}`（计数器在 `broker.stats`） |

**RBAC**（AdminUser，除 menus 读取）
| 方法 路径 | 用途 |
|---|---|
| GET/POST `/api/system/users`，PUT/DELETE `/api/system/users/{username}` | 用户 CRUD（**改密在此**；最后管理员保护） |
| GET/POST `/api/system/roles`，PUT/DELETE `/api/system/roles/{name}` | 角色 CRUD + 菜单授权 |
| GET `/api/system/menus`，PUT `/api/system/menus/{name}` | 菜单全量读 / 改展示元数据（path/component 只读） |

**IP 白名单 / 拦截**（QR1124，AdminUser）
| 方法 路径 | 用途 |
|---|---|
| GET/POST `/api/system/ip-whitelist`，PUT/DELETE `…/{rule_id}` | 规则 CRUD（即时刷新快照，无需重启） |
| GET `/api/ip-rejects` | 拦截记录按 IP 聚合 `limit/offset/ip` |

**自更新**
| 方法 路径 | 鉴权 | 用途 |
|---|---|---|
| POST `/api/system/agent` | AdminUser | 上传二进制（multipart file+version，≤64MB，算 sha256/HMAC） |
| POST `/api/system/agent/rollback` | AdminUser | 当前↔上一版互换（在跑 Agent 不降级） |
| POST `/api/system/agent/upgrade` | AdminUser | 批量选机升级 `{hosts[],version?}`（离线记 queued） |
| GET `/api/system/agent/current` `/api/system/updates` | 登录 | 待分发版本 / 升级台账 |
| GET `/api/system/agent/latest?ver=` | agent_ip_auth | 轮询清单（manual 恒 available=false） |
| GET `/api/system/agent/download` | agent_ip_auth | 流式下载二进制 |

**HTTP/SSE 通道**（QR1125 Phase 0，agent_ip_auth）
| 方法 路径 | 用途 |
|---|---|
| POST `/api/v1/ingest/status` `/hb` `/result` | 上行帧（v4 格式不变）；≤1MB(413)/JSON(422)/host≤120(422)；bridge 未配 503 |
| GET `/api/v1/commands/stream?host=` | SSE 下行命令（15s ping，retry 3000，连接即 flush queued，断连置 offline） |

### 7.3 协议与通道

- **MQTT v4**：主题 `kk/v1/{host}/{status,hb,result,cmd}`；QoS/retain 硬约束（status QoS1+retain+LWT、hb QoS0、result QoS1 失败终态 QoS0、cmd QoS1）。
  帧格式与兼容窗口见 [`../proto/messages.md`](../proto/messages.md)。
- **HTTP/SSE**（§8 of proto）：上行 ingest 复用 bridge 处理器；下行 SSE 事件 `id:<命令id>\ndata:<cmd帧>`；
  下行路由 `get_downlink` SSE 优先/MQTT 兜底（**升级帧 Phase 0 仍走 MQTT**）。
- **命令离线排队**：MQTT 走 Broker 持久会话；SSE 走 DB `commands.status='queued'` + 重连 flush（`KK_CMD_QUEUE_TTL`）。
  > 注：Phase 0 REST 下发对离线主机仍建 pending 走 MQTT；queued 命令机制为 Phase 1 httpsse Agent 备好。升级台账 queued 早已生产使用。

---

## 8. 二次开发指南

### 8.1 架构边界（先读 [`design.md`](design.md) 与 AGENTS.md）

- 服务端 MVC：`models/`（Store 门面 + 域 Mixin）→ `services/`（bridge/sse_hub/downlink/security）→ `controllers/`（REST）→ `web/`。
  `main.py:create_app` 只装配，**无模块级 app**；运行 `python -m kk_server`。
- Agent 单线程事件循环 + 一次性 daemon worker；MQTT socket 只由 paho 后台线程触碰；回调不做阻塞操作。

### 8.2 新增 REST 端点

1. 在 `controllers/` 新建/扩展模块，模块级 `APIRouter(prefix=…, dependencies=[Depends(current_user)])`（router 级默认拒绝）。
2. 写操作需管理员用 `AdminUser`；Agent 侧用 `agent_ip_auth`。
3. 在 `controllers/__init__.py:register()` 追加 `include_router`（赶在静态 catch-all mount 前）。
4. 落审计 `store.add_audit`；前端 `web/src/api/` 加对应封装 + spec。

### 8.3 新增前端页面

1. `web/src/views/<域>/<页>/index.vue`（+ `utils/hook.ts(x)` 数据流，co-located spec）。
2. **必须同步 `models/_store_rbac.py` 的 `_BUILTIN_MENUS` 种子**（name/path/component 与 views 一一对应）——菜单 DB 下发，漏种子则页面不可达。
3. 轮询走 `usePolls()`；图标用 `kkIcons.ts` 离线词汇表（**运行时零网络**，离线部署红线）；状态/格式化用 `kk.ts` 单一真相。
4. admin-only 页挂 Rbac 子树（operator 自动排除）；`pnpm build` 后产物同步到 `server/src/kk_server/web/`。

### 8.4 新增采集项 / 插件

- **内置采集项**：Agent `collector.py` 写 `fn(state,cfg)->(part_dict,state_patch)` 注册进 `ITEMS`，**同时同步服务端 `config.py:COLLECT_ITEMS`**（双端白名单一致，漂移由测试抓）。
- **自定义插件**：见 [§6.4](#64-采集项与自定义插件二次开发)（`collect()->dict`，无需改代码）。

### 8.5 改协议

必须同步四处：Agent `config.py:PROTO_VER`、Server `__init__.py:PROTO_VER`、`proto/messages.md`、双端测试。
兼容窗口 `ACCEPT_PROTO_VERS` 是精确集合；关窗口前看 `proto_v3_received` 确认存量升完（否则全网闪断）。

### 8.6 数据库改动（三库收敛）

- 新表 `kk_` 前缀、定长 `String(n)` 主键、大字段 `_long_text()`（MySQL LONGTEXT）；新列登记 `tables._ADD_COLUMNS`。
- 方言差异**只许在 store 汇聚点**；upsert 走 `_upsert`、批删走 `_delete_batched`（按主键 IN，不用 DELETE…LIMIT）。
- PG 专属调优进 `_pg_tune`（表级幂等、表属主即可、fail-fast）；PG18 专属 reloption 须 `server_version_num` 版本守卫。
- 新表登记进 `tests/_kk_db.py` TRUNCATE 清单。

### 8.7 测试要求

- 新用例不得依赖真实 Broker/网络/真实时钟/执行顺序；落单元层（ASGITransport/FakePublish），不顶 skipped 门禁。
- 改语义的修复随附锁定新语义的回归测试（docstring 标缺陷编号）；新增模块（尤其协议字段生产者）必须连带单测。
- 提交前 `bash scripts/quality_gate.sh` 全绿。

---

## 9. 运维与排障

### 9.1 观测入口

- **系统统计页**（`/system/index`）：Broker 链路态、命令状态分布、存储增速、桥接计数器（各配「什么值该担心」）。
- **`/api/system/stats`**：`broker.stats` 计数器（`status/hb/result/rejected/cmd_published/cmd_failed/upgrade_*/swept_*/proto_v3_received/bridge_errors/oversized_dropped/…` + QR1125 `ingest_*`）。
- **审计页**：`ip_rejected`（拦截）、`command_blocked`（黑名单）、`interval_violation`（误配高频）、`proto_mismatch`（协议不符）。

### 9.2 常见问题

| 现象 | 排查 |
|---|---|
| 主机不上报 | 白名单是否放行（IP 白名单页/拦截记录页）；`KK_SERVER`/`KK_TOPIC_PREFIX` 双端一致；Broker 可达 |
| 上线但无指标 | 指标等首帧心跳；`KK_INTERVAL` 是否过大；hb 是否被拒（未知主机需先 status） |
| 命令一直 pending/sent | Broker 连接（`broker.connected`）；Agent 在线；超时清扫 30s 周期；离线主机走排队补投 |
| 命令输出偏少 | rc=-3（out-queue 溢出/积压）→ done+truncated；大输出 4MB 截断 |
| 关 v3 窗口后全网掉线 | 关前未确认 `proto_v3_received` 归零（存量 Agent 未升完） |
| SSE 命令不下发 | nginx `proxy_buffering off`；`proxy_read_timeout`>15s；源 IP 白名单（内网直连） |
| production 拒启 | 默认口令未改 / 白名单 env+DB 皆空 |

### 9.3 日志

双端 loguru；`KK_LOG` 一个文件一个写入者（Agent 独占，wrapper 走 `KK_SUPERVISOR_LOG`）；服务端 `KK_LOG` 留空只写 stdout 由 docker 采集
（compose 设 `max-size:50m/max-file:5`）；`KK_LOG_JSON=1` 输出 JSON Lines 接采集端。日志不含口令/token。

### 9.4 PostgreSQL 18 监控（QR1126）

`_pg_tune` 启动自动落地（autovacuum 阈值/fillfactor/BRIN 粒度/lz4 TOAST/PG18 `autovacuum_vacuum_max_threshold` 版本守卫）。
巡检 SQL（`pg_stat_io` 字节列/WAL 行、`pg_aios`、HOT 比例验证 fillfactor、`pg_stat_statements` 并行计数）见 [`deployment.md`](deployment.md) §4.6。
容器内 `io_method=worker`（**不用 io_uring**，Docker seccomp 封禁）；PGDATA 显式钉死规避镜像路径变更陷阱。

### 9.5 真环境验证

[`e2e-real-env.md`](e2e-real-env.md)（WSL + 真 Broker + 真 Agent + Playwright，32 条用例）；
`scripts/mqtt_e2e.py`（Broker 语义冒烟）、`scripts/db_smoke.py`（真库方言 + PG18 catalog 回读）、`scripts/bench_agent.py`（RSS 基线）。

---

## 10. 附录：文档索引与术语

### 10.1 文档索引

| 文档 | 定位 |
|---|---|
| [`manual.md`](manual.md) | 本手册（综合指南） |
| [`design.md`](design.md) | 总体架构设计 |
| [`development.md`](development.md) | 开发环境搭建 |
| [`deployment.md`](deployment.md) | 生产部署权威指南 |
| [`design-transport.md`](design-transport.md) | QR1125 传输 redesign（HTTP/SSE） |
| [`ci-jenkins.md`](ci-jenkins.md) | CI/CD 流水线 |
| [`e2e-real-env.md`](e2e-real-env.md) | 真环境 E2E 操作手册 |
| [`../proto/messages.md`](../proto/messages.md) | 协议契约 v4（含 §8 HTTP/SSE） |
| `architecture-review.md` / `quality-*.md` / `*-plan-*.md` | 历史评审与缺陷账本（按日期参考） |

### 10.2 术语

| 术语 | 含义 |
|---|---|
| 主机 / host | 被管理 Linux 主机（DB 列名历史沿用 `pod`/`containers`，产品术语统一「主机」） |
| Agent / kk-agent | 主机内客户端单文件二进制 |
| Broker | Mosquitto MQTT 消息中间件 |
| 桥接 / MqttBridge | 服务端 MQTT 无状态桥（消息路由到 DB / 命令发布到主题） |
| ingest / SSE | QR1125 HTTP 上行 / Server-Sent Events 下行通道 |
| queued | 离线排队态（升级台账生产使用；命令 queued 为 Phase 1 备好） |
| LWT | MQTT 遗嘱消息（Agent 异常掉线自动发 offline） |
| retain | MQTT 保留消息（status 上线真相回放） |
| RBAC | 基于角色的菜单授权（权限只控菜单） |
| 门禁 | `scripts/quality_gate.sh`（提交前必绿） |