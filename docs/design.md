# KontainKeeper 设计文档

> 场景：Linux 主机的直连管理与指标提取（典型形态是 K8S 下的 vscode-server 容器 IDE，但不限于容器）。
> 约束：不使用 K8S 集群自身能力（无 kube-api / exec / svc），不触碰宿主机，用户无感知，镜像制作期介入、上线即连。
> 规模目标：**500 台**。

---

## 1. 总体架构

```
┌──────────────────────── 管理端（独立部署） ────────────────────────┐
│  Web UI (Vue3 + Element Plus + ECharts)                          │
│        │ REST（轮询：总览 10s / 详情 30s / 命令中心 5s）             │
│  kk-server (FastAPI)                                             │
│   ├─ REST API：主机列表 / 指标 / 命令下发 / 结果 / 审计 / stats      │
│   ├─ MqttBridge：无状态桥接（订阅 hb/status/result，发布 cmd）       │
│   └─ Store：SQLAlchemy 2 Core + async（SQLite / PG / MySQL）       │
└──────────────▲───────────────────────────────────────────────────┘
               │ 订阅指标 / 发布命令（服务端 ↔ Broker，无长连接状态）
┌──────────────┴───────────────────────────────────────────────────┐
│  Mosquitto 2.x（MQTT Broker）                                     │
│   · LWT 遗嘱：Agent 异常掉线自动发布 offline                        │
│   · Retained status：服务端重启立刻恢复全量在线视图                   │
│   · 持久会话 + QoS1：离线期间命令排队，重连自动补投                   │
│   · 匿名开放（v3）：零凭据接入，管控上收服务端白名单                   │
└──────────────▲───────────────────────────────────────────────────┘
               │ 出站 MQTT（主机 → Broker，唯一通道，主机无需暴露端口）
┌──────────────┴───────────────────────────────────────────────────┐
│  主机内 Agent（kk-agent，随镜像内置 / 独立进程，后台守护）             │
│   ├─ psutil 采集（cpu/mem/disk/disk_io/net/proc/user/sys）         │
│   ├─ paho-mqtt 发布（hb QoS0 / result QoS1）                      │
│   ├─ 命令执行器（argv 直传 exec，结果分块回传）                       │
│   └─ 自定义采集插件目录（mtime 热加载）                              │
└──────────────────────────────────────────────────────────────────┘
```

核心思路有两条：

1. **主机发起出站连接（反向通道）**。不能动宿主机、不能用 K8S Service/Ingress 做入站，
   唯一可行路径是主机主动连出去。
2. **把可靠性难题整体移交给开源组件**。连接可靠性、重连退避、离线命令排队、在线判定（LWT）
   这四件事，v1 是自研的（内存连接表 + 手写补发 + 超时判定 ≈ 200 行且有多处缺陷），
   现在全部由 MQTT Broker 承担。服务端因此**无状态**，可水平扩容。

主题布局与帧格式见 [`proto/messages.md`](../proto/messages.md)（协议 v3：匿名 Broker + 服务端 IP 白名单）。

## 2. 为什么是 MQTT

| 能力 | v1 自研 WS | v2 MQTT |
|---|---|---|
| 掉线检测 | 服务端心跳超时猜测（半开连接误判） | Broker LWT，1.5×keepalive 内必达 |
| 离线命令 | 手写 `pending_for` 补发（存在 sent 状态丢命令缺陷） | 持久会话 + QoS1，Broker 排队自动补投 |
| 服务端重启 | 在线状态全丢，等一轮心跳 | retained `status` 立刻恢复 |
| 重连 | 自写指数退避 | paho 内置 |
| 扩容 | 内存连接表不可 HA | 无状态，多实例（共享订阅钩子） |

代价是引入一个 Broker 部署单元——但它换掉的是 200 行自研且带缺陷的代码，且 Mosquitto 是成熟的
EPL/EDL 许可组件。

## 3. 客户端 kk-agent

### 3.1 依赖策略：引入依赖 + 单文件打包

v1 强调「纯标准库」，代价是手工解析 `/proc`（233 行，且只覆盖 Linux）与自研 WebSocket（261 行）。
v2 改为**引入成熟依赖 + 编译成单文件二进制**，运行时仍是零依赖分发：

| 依赖 | 替代的自研代码 | 许可 |
|---|---|---|
| psutil 7.x | 手工 `/proc` 解析（233 行） | BSD |
| paho-mqtt 2.x | `ws.py` + `conn.py` 自研 RFC6455（261 行，已删） | EPL/EDL |

打包后产物 8–12MB（PyInstaller，含 psutil + paho 增量约 3MB），嵌入镜像无需目标主机有 Python。

预期常驻开销：**RSS 25–35MB**（原 <15MB 口径作废），空闲 CPU <0.1%。
相比纯标准库方案内存涨了约 10–20MB，换来跨平台采集与协议可靠性——Linux 主机场景完全可接受。

### 3.2 模块划分

```
kk_agent/                 # 独立 Python 包（编译为单文件二进制）
├── __init__.py        # AGENT_VER / PROTO_VER / __version__
├── __main__.py        # 入口（python -m kk_agent，或 ./kk-agent 二进制）
├── main.py            # 事件循环：调度、心跳、命令分发、结果分块回传
├── transport.py       # MQTT 传输（paho）：LWT、QoS 分级、重连、离线队列
├── config.py          # env: KK_SERVER, KK_ADVERTISE_IP, KK_INTERVAL, KK_TOPIC_PREFIX ...
├── collector.py       # psutil 采集，含 collect_items() 按项采集
├── executor.py        # 命令执行（timeout + 输出封顶 + 一次性工作线程）
├── plugin_loader.py   # 插件热加载（按 mtime）
├── updater.py         # 自更新：拉清单 / 下载 / sha256 / os.execv 重启
├── logutil.py         # stderr + 1MB 轮转文件日志
└── plugins/           # 自定义采集插件：任意 *.py，实现 collect() -> dict
```

- **线程模型**：主循环单线程事件驱动；MQTT socket 只由 paho 后台网络线程触碰；
  采集/命令/插件在一次性 daemon 线程跑，结果经 `queue.Queue` 回主线程发帧。
- **自定义采集**：投放插件后按 mtime 热加载，结果并入心跳帧的 `custom` 字段；
  单个插件失败只跳过自身。也可下发 `kind=plugin_reload` 立即采集回传。
- **命令安全**：argv 数组直传 exec（不经 shell 拼接）；黑名单在服务端统一拦截并审计。
- **守护方式**：镜像 entrypoint-wrapper 监督循环（崩溃 5s 重启）+ 日志轮转；不使用 systemd。

### 3.3 镜像介入（用户无感知）

由 `scripts/build.sh` 在原镜像末尾追加：把原 ENTRYPOINT/CMD 解析为 exec 形式 JSON 数组，
`kk-entrypoint` 作为首元素，原入口其余元素以参数透传。

```dockerfile
COPY kk-agent /opt/kk-agent/kk-agent
COPY plugins /opt/kk-agent/plugins
COPY kk-entrypoint /usr/local/bin/kk-entrypoint
ENV KK_SERVER=mqtt://broker:1883 \
    KK_AGENT_BIN=/opt/kk-agent/kk-agent \
    KK_PLUGIN_DIR=/opt/kk-agent/plugins
ENTRYPOINT ["/usr/local/bin/kk-entrypoint", "<原 ENTRYPOINT 元素...>"]
CMD ["<原 CMD 元素...>"]
```

```sh
#!/bin/sh
# 后台监管 Agent：崩溃 5s 拉起；自更新 execv 复用 PID 不误判
( while true; do /opt/kk-agent/kk-agent >>/var/log/kk-agent.log 2>&1; sleep 5; done ) &
exec "$@"   # 原启动命令，用户侧完全不变
```

v3 起 Agent 零凭据接入（Broker 匿名开放），镜像内只烧入 `KK_SERVER` 等
非敏感配置；接入管控由服务端 `KK_AGENT_IPS` 白名单承担（上行帧自报 `ip` 校验）。

## 4. 服务端 kk-server

技术栈：FastAPI + uvicorn + paho-mqtt（桥接）+ SQLAlchemy 2 Core async engine。
多库支持：`KK_DB_URL` 三选一（SQLite / PostgreSQL / MySQL），驱动按需安装。

```
kk_server/
├── __main__.py        # python -m kk_server 入口
├── main.py            # create_app 工厂：装配 M/C/V + lifespan（桥接 + janitor）
├── config.py          # KK_* 环境变量解析（Settings），含 COLLECT_ITEMS 白名单
├── models/
│   ├── store.py       # 异步存储：upsert/心跳/命令/审计/会话/聚合/回收
│   ├── tables.py      # 表定义 + _ADD_COLUMNS 迁移清单
│   └── helpers.py     # 工具
├── services/
│   ├── mqtt_bridge.py # MqttBridge：无状态桥接（替代已删的 hub.py）
│   └── security.py    # 命令黑名单校验（安全红线）
├── controllers/       # REST /api/*
│   ├── deps.py        # 会话鉴权
│   ├── health.py      # GET /api/health
│   ├── auth.py        # 管理员登录/登出
│   ├── containers.py  # 主机列表（?view=summary）/详情/指标
│   ├── commands.py    # 命令下发（含 kind=collect）/结果/采集项
│   ├── audit.py       # 审计日志
│   ├── stats.py       # GET /api/system/stats 可观测
│   └── agent_update.py# Agent 自更新上传/清单/下载
└── web/               # Vue3 构建产物（随包打包，服务端直接托管）
```

### 4.1 存储治理

- **摘要视图**：`containers` 表冗余 `cpu/mem_mb/disk_pct` 三列，`?view=summary` 只读这几列——
  500 台列表不再逐行 `json.loads(last_metrics)`（每帧 2–4KB，全量解析是列表接口主要开销）。
- **自动补列**：`create_all` 只建表不加列，新增列登记 `tables._ADD_COLUMNS`，
  由 `setup()` 的 `_ensure_schema` 自动 ALTER，既有库零手工迁移。
- **回收**：原始心跳 2 天聚合成小时、hourly 保留 90 天、命令状态行 30 天、
  **命令输出单独 7 天清文本保留状态行**（`out_purged=1`）——审计可追溯与存储成本分开权衡。
  大表按主键分批删（跨方言通用，避免长事务锁表）。
- **在线判定**：`containers.online` 列 + `status_ts`，由桥接写；不再逐行算宽限。

### 4.2 前端

`web/` 是独立 pnpm 工程（Vue3 + TS + Element Plus + Vite + Pinia + ECharts，
底座 pure-admin-thin v6.2.0）。四个业务页：主机总览（10s 轮询 + 多选批量）、
主机详情（ECharts 曲线 + 磁盘/网卡/进程/登录用户，30s）、命令中心（采集面板 + 命令面板，5s）、
审计日志。

构建产物同步到 `server/src/kk_server/web/`，`kk-server` 单端口托管全栈，
也可独立 nginx 反代。菜单完全静态注册（`getAsyncRoutes()` 返回 `[]`），不依赖 mock/fake server。

## 5. 仓库结构

```
KontainKeeper/
├── README.md
├── docs/
│   ├── design.md                  # 本文档
│   ├── completion-plan-mqtt.md    # 执行路线图与缺陷账本
│   └── architecture-review.md     # 代码评审记录
├── agent/                         # kk-agent（独立 UV 项目）
├── server/                        # kk-server（独立 UV 项目）
├── web/                           # Vue3 前端（独立 pnpm 工程）
├── proto/messages.md              # 协议 v3（MQTT 主题与帧格式，匿名 Broker + IP 白名单）
├── scripts/                       # 构建与部署脚本
└── pyproject.toml                 # UV 工作区虚拟根
```

## 6. 容量估算（500 台）

- 心跳：500 × 60s ≈ 8.3 msg/s，每帧 2–4KB → Broker 与 DB 写入均无压力。
- 命令风暴：批量 500 台属极端场景，受 Agent `max_out=4MB` + 48KB 分块约束；
  500 次 QoS1 发布对 Mosquitto（万级 msg/s）无压力。**不引入广播主题**：
  省下的 500 次发布要换来主机分组维护、结果关联、部分失败可见性一整套协议面，与「减少代码」相悖。
- 存储：原始心跳 2 天 + hourly 90 天 + 命令 30 天 ≈ 几百 MB/年，单机无压力。

## 7. 安全模型

> v3 安全面收缩：服务仅运行在**内网可信环境**，Agent 去 token 化以换取极简部署
>（单二进制 + 一个地址即拉起）。Broker 匿名开放后，不可伪造的边界只剩网络层。

| 层 | 机制 |
|---|---|
| Agent 接入 | Broker 匿名开放；上行帧（status/hb/result）统一携带自报 `ip`，服务端按 `KK_AGENT_IPS` 白名单（IP/CIDR）校验，名单外拒收并审计 `ip_rejected` |
| 自更新下载 | REST 接口按请求**真实 TCP 源 IP** 校验同一白名单（比 MQTT 自报值可靠） |
| 命令滥用 | 服务端黑名单（`KK_CMD_BLACKLIST`）在源头拦截 + argv 直传不经 shell + 全量审计 |
| 自更新完整性 | sha256 强制校验（可选 HMAC），防损坏/篡改 |
| 管理端 | 会话 token（12h 过期）；生产必须前置 TLS 终结 |
| 网络边界 | 匿名 Broker 下唯一不可伪造的隔离：防火墙/安全组限制 1883 端口可达范围；`KK_ENV=production` 未配白名单拒绝启动 |

## 8. 风险与对策

- **Broker 单点**：生产应部署 Mosquitto 2.x 并做持久化 + 监控；它是新的关键依赖。
- **`MAX_QUEUED` 溢出**：离线期间大输出命令（4MB ≈ 86 块）只能积压约 6 条，超出会被静默淘汰。
  对策：`MAX_QUEUED` 可配，且 `send_result` 感知入队失败回 `rc=-3` 失败终态（不静默丢）。
- **跨库验证边界**：PostgreSQL / MySQL 目前只做了 DDL 与语句的跨方言编译校验，
  **未连过真实库**；上线前需补真库跑测（MySQL 排序规则大小写、PG 标识符小写折叠等）。
- **Agent 被发现/杀掉**：以同用户或 root 运行；wrapper 循环保活。
