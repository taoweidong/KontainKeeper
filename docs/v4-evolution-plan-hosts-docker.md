# KontainKeeper 演进方案：通用 Linux 主机 + Docker 容器管理 · 前端核心页重设计

> 目标两份：① 从「K8S/vscode-server 容器 IDE 专用」扩为「所有 Linux 主机 + 其上的 Docker 容器」，
> **架构不变**（Agent 出站 MQTT → Broker → 无状态 FastAPI 桥 → DB → 服务端托管前端）；
> ② 前端在保持 pure-admin-thin 风格与 `kk-*` 设计基座的前提下重设计核心页面，
> 主链路流程走通、核心交互无坑。
>
> 本文件只做设计决策与落地分解，不含逐行代码实现。代码与本文冲突时以代码与 `git` 历史为准。

---

## 0. 结论速览

| 问题 | 决策 |
|---|---|
| 架构要改吗 | **不改**。仍是「目标机装 Agent，Agent 主动出站连 Broker，服务端无状态桥接」。扩展的是**被管理对象的层级**，不是连接模型 |
| 「和容器直连」怎么实现 | **不装容器内 Agent、不在服务端开 docker.sock**。容器命令由**所在主机的 Agent 代理**（`docker exec`），经同一 MQTT 通道下发/回传。对使用者就是「直连容器」，对架构零新增组件 |
| 协议 | bump 到 **v4**：主题新增 `containers`，`cmd`/`result` 帧新增 `target`，`status`/`hb` 帧新增主机元信息与能力声明。**服务端设 v3/v4 双版本接收窗口**，避免一次升级全网闪断 |
| 数据库 | **不改** `kk_containers`/`pod` 列名（AGENTS.md 约定）；新增列走 `_ADD_COLUMNS`；新增 `kk_docker_containers` / `kk_docker_hb` / `kk_docker_hourly` 三张表 |
| API | 新命名 `/api/hosts*`，`/api/containers*` 保留为**兼容别名**；响应同时给 `host` 与 `pod`（同值，后者标记 deprecated） |
| 前端 | 静态路由新增「容器总览 /containers」「系统统计 /system」；重设计 6 个核心页；新增 `TargetPicker`（主机→容器两级选择）组件。**不引新依赖** |
| Docker 能力 | Agent **探测到可用 docker 才启用**（`KK_DOCKER=0` 显式关闭）。物理机不挂 socket = 无容器能力，主机级功能完全不受影响 |

---

## 1. 现状盘点（设计的事实基线）

### 1.1 已有能力（直接复用，别重造）

| 层 | 位置 | 关键事实 |
|---|---|---|
| 协议 | `proto/messages.md` | v3。主题 `kk/v1/{host}/{status,hb,result,cmd}`；status QoS1+retain+LWT、hb QoS0 不 retain、result/cmd QoS1；上行帧带自报 `ip` 走 `KK_AGENT_IPS` 白名单 |
| Agent | `agent/src/kk_agent/` | `transport.py`(paho) / `collector.py`(psutil，8 采集项) / `executor.py`(argv 直 exec + `sh -c` 双模，进程组回收) / `updater.py`(sha256+HMAC) / `plugin_loader.py`(热加载) / `main.py`(事件循环 + 线程池) |
| 服务端 | `server/src/kk_server/` | `create_app` 工厂装配；`MqttBridge` 无状态桥接（白名单入口在 `_on_message` 一处）；`Store` 全协程，三库方言只收在 `_upsert`/`_ensure_schema`；`controllers/` 9 个路由文件；`web/` 托管前端产物 |
| 前端 | `web/src/` | 纯静态路由 `router/modules/kk.ts`；5 个业务页 + 命令中心 3 个复用组件（`HostPicker` 抽屉多选 / `CommandWorkbench` 双栏 / `CommandHistory`）；轮询统一 `utils/kkPoll.ts` 的 `usePolls()`；视觉基座在 `style/kk.scss`（`--kk-page-h` 单高度锚点、`--kk-font-num` 等宽读数栈、`--kk-live/--kk-alert/--kk-stale/--kk-idle` 四档状态色） |
| 约束 | `AGENTS.md` §关键约束 | 协议改动四件套；Windows 用 `.venv` 直调；新增列必登记 `_ADD_COLUMNS`；日志只走 loguru 适配层且禁 `import logging`；安全红线 = 黑名单 + 审计不可绕过 |

### 1.2 阻碍通用化的具体点（逐条对应改造）

1. `status` 帧只有 `image`，没有 OS/内核/虚拟化类型 —— 物理机没有 image，列表信息量塌掉。
2. 无分组/标签概念，500 台只能按名字搜。
3. 无能力声明：服务端不知道某台 Agent 能不能管容器，无法在源头拦住容器命令。
4. `cmd` 帧只有 `argv`，执行目标恒为主机；容器执行只能靠 `ssh`/`docker -H` 绕，且结果无法与主机命令区分。
5. 采集项 8 项全是主机视角（`cpu/mem/disk/disk_io/net/proc/user/sys`），无 Docker 维度。
6. DB 无容器表；REST 无容器端点；前端无容器视图。
7. 部署文档/脚本只为「容器镜像叠加」写死（`scripts/build.sh` 只能叠进已有镜像），物理机无安装路径。

---

## 2. 总体设计：三层目标模型

```
机队 Fleet（分组 group / 标签 labels）
 └─ 主机 Host   ← 现有全部概念，只是补齐元信息与能力
     └─ 容器 Container（Docker；仅当主机具备 docker 能力）
```

**目标标识符**（协议、API、前端统一）：

- 主机：`host`（沿用现有字符串，DB 列仍叫 `pod`）
- 容器：`(host, container_id)`，UI 展示用 `name`，key 用 `id`

**命令/采集的目标**：`target = host | container`。`target=container` 时帧内带 `container`（容器 id 或 name，二者等价由 Agent 解析）。

**关键取舍：容器为什么不单独装 Agent**

| 方案 | 结论 |
|---|---|
| A. 服务端直连各主机 docker daemon（TCP/TLS） | ✗ 违背「服务端无状态」；500 台 = 500 条 TLS 长连 + 500 份凭据；K8S 集群里根本没有 docker.sock |
| B. 每个容器塞一个 Agent | ✗ 容器里跑常驻进程违背 IDE 场景「用户无感知」；升级面爆炸 |
| C. **主机 Agent 代理 `docker exec`（选定）** | ✓ 架构零变化；凭据仍在 Broker 侧；容器生命周期与 Agent 解耦；服务端只管发布主题 |

---

## 3. 协议设计（v4）

> 协议真相源是 `proto/messages.md`，改协议必须同步四件套：`agent/src/kk_agent/config.py` 的 `PROTO_VER`、`server/src/kk_server/__init__.py` 的 `PROTO_VER`、协议文档、双端测试。

### 3.1 主题布局（在 v3 基础上只增不改语义）

```
kk/v1/{host}/status      A→S  在线 + 主机元信息 + 能力声明     QoS1  retain
kk/v1/{host}/hb          A→S  心跳指标（含 docker 摘要）        QoS0  不 retain
kk/v1/{host}/containers  A→S  Docker 容器清单+每容器指标快照    QoS1  retain   ← 新增
kk/v1/{host}/result      A→S  命令结果（新增 target 回显）      QoS1
kk/v1/{host}/cmd         S→A  命令下发（新增 target/container） QoS1
```

**为什么容器清单独立成主题而不塞进 hb**：hb 是 QoS0 且明文规定「绝不 retain」；100 容器的清单 JSON 约 30–60KB，塞进去会把 2–4KB 的心跳帧撑大 20 倍，且丢一帧就丢整份清单。独立 QoS1+retain 主题 = 服务端重启即恢复、丢帧由 Broker 重传。**MQTT 主题名 `containers` 与既有 REST `/api/containers`（已改为 `/api/hosts` 别名）不冲突——不同命名空间**。

### 3.2 帧字段变更

**`status`（新增字段全部可选，v3 Agent 无这些字段不影响现有解析）**

```json
{"online":true,"host":"web-01","ip":"10.0.0.5","agent_ver":"0.4.0","proto_ver":4,
 "image":"vscode-server:1.2","interval":60,"reason":"online","ts":1690000000,
 "env":{"os":"Ubuntu 22.04.3 LTS","kernel":"5.15.0-118-generic","arch":"x86_64",
        "virt":"container","in_container":true,"runtime":"docker",
        "hostname":"web-01","ips":["10.0.0.5"]},
 "group":"ops-web","labels":{"env":"prod","role":"web"},
 "caps":{"docker":true,"docker_ver":"27.0.3","shell":true},
 "docker":{"total":12,"running":11,"unhealthy":1,"restart_loop":0},
 "interval":60}
```

| 字段 | 说明 |
|---|---|
| `env` | `psutil`/`/proc`/`platform` 探测；`virt ∈ container/vm/metal`；`in_container` 让 UI 区分「容器里的主机」与「物理机」 |
| `group` / `labels` | 来自 Agent `KK_GROUP` / `KK_LABELS=k=v,k=v`；是通用化管理的核心筛选维度 |
| `caps` | **能力声明**。`docker=true` 才允许对该主机下发 `target=container` 的命令；服务端在源头拒绝而不是降级执行 |
| `docker` | 摘要标量（落库为 `kk_containers` 的三个摘要列），供总览页一屏读完，**不解析 last_metrics** |

**`hb`**：`metrics` 新增可选键 `docker`（与既有 8 项同级的第 9 个采集项），内容为同上的四个摘要标量。`KK_HB_ITEMS` 白名单同步扩为 9 项（`agent.collector.ITEM_NAMES` 与 `kk_server.config.COLLECT_ITEMS` 必须一致，已有测试守漂移）。

**`containers`（新帧）**

```json
{"host":"web-01","ip":"10.0.0.5","ts":1690000000,"partial":false,
 "items":[{"id":"3f7a...","name":"web-01-nginx","image":"nginx:1.27",
           "state":"running","status":"Up 3 hours (healthy)",
           "created":1690000000,"started_at":1690001000,
           "cpu_pct":1.2,"mem_used_mb":36.4,"mem_limit_mb":256.0,"mem_pct":14.2,
           "net_rx_mb":12.5,"net_tx_mb":3.1,"block_read_mb":0.4,"block_write_mb":1.2,
           "pids":7,"restarts":0,"health":"healthy","ports":["0.0.0.0:8080->80/tcp"],
           "labels":{"com.docker.compose.project":"edge"}}]}
```

QoS1 + retain；清单**整体发布**（不做应用层分块，Mosquitto 默认 `message_size_limit` 1MB 远大于此；真超出时 Agent 按 `partial=true` 截断并告警，见 §5.3）。发布间隔默认与心跳一致，容器数 >50 时自动降频到 `max(interval, 60)`。

**`cmd`（新增 `target` / `container`；v3 Agent 收到未知字段会忽略 → 有降级风险，见下）**

```json
{"id":"c-127","kind":"shell","timeout":30,"target":"container","container":"web-01-nginx",
 "argv":["nginx","-t"]}
{"id":"c-128","kind":"container_logs","container":"web-01-nginx","tail":200,"since":300}
{"id":"c-129","kind":"container_ctl","container":"web-01-nginx","action":"restart"}
```

**`result`（回显 `target` / `container`，服务端据此路由到「主机命令」或「容器命令」视图与审计）**

```json
{"id":"c-127","seq":0,"total":1,"out_b64":"...","done":true,"rc":0,
 "ip":"10.0.0.5","target":"container","container":"web-01-nginx",
 "elapsed_ms":82,"timed_out":false,"truncated":false}
```

**`kind` 语义收敛**：`shell`/`collect`/`plugin_reload`/`update` 语义不变；新增 `container_logs`、`container_ctl`（`start|stop|restart|pause|unpause`，**不含 `rm`**——删除容器不进 v4，留给显式 shell 路径并受黑名单约束）。

**`kind=collect` + `target=container`**：容器内无 psutil，走 `docker exec <c> sh -c` 读 `/proc` 的受限子集（`cpu/mem/load`）。标记为**二期**，v4 先只开放 host 侧 collect。

### 3.3 能力协商与降级安全（**最关键的一条**）

风险：`cmd` 帧带 `container` 字段发给一个**只有 v3 或未挂 docker.sock 的 Agent**，旧 Agent 忽略未知字段会在**宿主机上**执行本意属于容器内的命令 —— 这是静默的越权执行面。

防线（三层，缺一不可）：

1. **服务端源头拒绝**：`caps.docker != true` 的主机（由 status 帧落库）下发 `target=container` 直接 `409 caps_not_supported`，不进 Broker。
2. **容器存在性校验**：`target=container` 时容器必须在该主机 `kk_docker_containers` 中且 `state=running`；不存在 → `404 container_not_found`。离线主机用「最近一次快照」判定并在响应里标注 `stale: true`。
3. **Agent 侧硬拒绝**：v4 Agent 收到 `target=container` 但自身 docker 能力未启用，回 `rc=126 + out="docker_capability_disabled"`，**绝不降级到宿主机执行**。

另：**归属校验沿用现有语义** —— `result` 帧的 `host` 必须等于 `commands.pod`；`container` 与 `commands.container` 不一致同样按 `result_mismatch` 审计（把现有跨主机校验扩成跨主机+跨容器）。

### 3.4 兼容策略（避免一次升级全网闪断）

现有逻辑是 `proto_ver != 3` 即拒收并审计 —— 直接 bump 会让存量 Agent 全部掉线。

- 服务端 `PROTO_VER = 4` 对外广播，同时引入 `ACCEPT_PROTO_VERS = (3, 4)` 窗口期（默认开，`KK_DROP_PROTO_V3=1` 关闭）。
- 窗口期内 v3 帧照常落库；v3 主机在总览页显示「协议待升级」角标（与 `agent_outdated` 并列，复用 `kk-warn` 色，**不新增第五档状态色**）。
- 升级顺序：**先服务端（开双版本）→ 分批升 Agent → 确认仅剩 v4 → 关窗口**。窗口关闭写进阶段 4 的验收项。
- 新增帧字段一律可选；`docker`/`env`/`caps` 缺省时前端显示 `—`（未探测），**不显示 0**（0 与未探测是两种语义）。

---

## 4. 数据库设计（三库通用，方言只收在两处）

### 4.1 `kk_containers` 新增列（登记进 `tables._ADD_COLUMNS`）

| 列 | 类型 | 说明 |
|---|---|---|
| `host_type` | `VARCHAR(16) DEFAULT 'container'` | container/vm/metal；存量行回落 container，保持旧语义 |
| `os_name` / `kernel` / `arch` | `VARCHAR(80)` | 概览副行信息 |
| `group_name` | `VARCHAR(64) DEFAULT ''` | 分组长，建索引 |
| `labels` | LONGTEXT | JSON |
| `caps` | LONGTEXT | JSON（docker/shell/...） |
| `docker_total` / `docker_running` / `docker_unhealthy` | `INTEGER DEFAULT 0` | 总览摘要列，列表接口只读列不解 JSON |
| `proto_ver` | `INTEGER DEFAULT 3` | 版本窗口期判定 |

新索引：`idx_ct_group(group_name)`、`idx_ct_host_type(host_type)`。

### 4.2 新增表

```python
kk_docker_containers   # 容器清单快照（每主机一份，upsert 覆盖）
  PK (host, cid)                      # 注意：MySQL 主键必须定长 String(n)，cid 取前 64 位
  列：name, image, image_id, state, status_text, health, ports(JSON), labels(JSON),
      cpu_pct, mem_used_mb, mem_limit_mb, mem_pct,
      net_rx_mb, net_tx_mb, block_read_mb, block_write_mb,
      pids, restarts, created, started_at, updated_ts
  索引：idx_dk_host(host) / idx_dk_state(state) / idx_dk_image(image)

kk_docker_hb           # 容器指标短期序列（容器详情曲线）
  id PK / host / cid / ts / cpu_pct / mem_pct
  索引 idx_dkhb_cid_ts(host, cid, ts) + idx_dkhb_ts(ts)

kk_docker_hourly       # 小时聚合（与 kk_hourly 对称）
  PK (host, cid, hour)
```

保留策略并入既有 `Store.cleanup()`：`docker_hb` raw 2 天、`docker_hourly` 90 天，与 `kk_heartbeats`/`kk_hourly` 同一套参数（`raw_days=2 / hourly_days=90`），**不新增环境变量**。分批删仍按主键 `IN`，不用 `LIMIT`。

### 4.3 既有表

`kk_commands` 新增 `container VARCHAR(120) DEFAULT ''`（空 = 主机目标）+ `target VARCHAR(12) DEFAULT 'host'`；索引用现有 `idx_cmd_pod(pod, created_at)` 撑住，容器维度筛选二期再补。`kk_audit.detail` 是自由 JSON，容器维度直接写进去（`{"host":..,"container":..}`），**不加列**。

---

## 5. Agent 设计

### 5.1 新增 `docker.py`（零新依赖）

**不引入 `docker` Python SDK**（requests/urllib3 拖累 PyInstaller 二进制体积与启动时间，与「常驻 RSS 25–35MB」口径冲突）。两条采集路径，按可用性自动选：

| 路径 | 命令 | 用途 |
|---|---|---|
| CLI（主） | `docker version --format json`、`docker ps -a --no-trunc --format json`、`docker stats -a --no-stream --format json` | 清单 + 指标；容器内 `/proc` 读不到 |
| socket（备） | `POST /containers/json`、`GET /containers/{id}/stats?stream=false` | CLI 不在 PATH 但 socket 可写时 |

用**标准库** `socket` + 手写最小 HTTP/1.1 over AF_UNIX（约 60 行，`http.client` 不认 unix socket 路径，需自包装）。 fallback 失败即视为无 docker 能力。

**执行路径**：`docker exec [-i] <id> <argv>` / `docker exec <id> sh -c <cmdline>`；`docker logs --tail N --since <s> <id>`；`docker restart|stop|start|pause|unpause <id>`。全部复用现有 `executor.run_shell`（进程组回收、输出封顶、超时杀掉原样生效）。

**约束**：docker 子进程调用必须带 `timeout`（默认 10s，`KK_DOCKER_TIMEOUT`）且在工作线程池内执行 —— paho 网络线程绝不阻塞。容器数 > 阈值时 `docker stats` 改分批（每批 20 个）避免单次调用超时。

### 5.2 `collector.py` 扩展

- `env` 探测函数 `host_env()`：`platform`/`/proc/1/cgroup`/`systemd-detect-virt`（不存在就跳过），**每项 `_safe` 容错**，任一失败不影响整帧（沿用现有原则）。
- `docker_summary()`：返回 `{total, running, unhealthy, restart_loop}` 四标量；不可用返回 `None`（前端显示 `—`）。
- `ITEM_NAMES` 增加 `"docker"`，服务端 `COLLECT_ITEMS` 同步（漂移由测试抓）。

### 5.3 `transport.py` / `main.py` 扩展

- `publish_containers(items, partial)`：QoS1 + retain。
- LWT 只挂 `status`（不变）。容器清单的 retained 值在主机离线后由服务端按 `online` 过滤前端展示，**不单独发 offline**（少一条主题就少一类竞态）。
- `main.py` 的 dispatcher 增加 `container_logs` / `container_ctl` 分支；`target=container` 而 `caps.docker=false` → 回 `rc=126 docker_capability_disabled`（§3.3 防线 3）。
- 心跳调度改用「指标 60s / 容器清单 `max(interval, 60)`，容器数 >50 时 120s」的两级节流，复用同一 `busy` 事件防重入。

### 5.4 配置项（全部 `KK_*`，不引入配置文件）

| 键 | 默认 | 说明 |
|---|---|---|
| `KK_DOCKER` | 空=探测 | `1` 强制启用、`0` 强制关闭。**不配 = 探测到 socket/CLI 才启用** |
| `KK_GROUP` / `KK_LABELS` | 空 | `k=v,k=v`；分组与标签 |
| `KK_DOCKER_TIMEOUT` | 10s | docker 子进程超时 |
| `KK_DOCKER_CONTAINER_SYNC` | 0=跟随心跳 | 容器清单独立间隔（秒），>0 时优先生效 |

### 5.5 部署形态（「所有 Linux」的必要条件）

- `scripts/build.sh` 保持「叠加进任意镜像」能力不变（容器 IDE 场景照旧）。
- 新增 `scripts/install.sh <target-dir>` + `deploy/systemd/kk-agent.service`：物理机/VM 用 tarball + systemd 部署，同一份二进制、同一套 `KK_*` 变量，只是 Unit 里配 `Restart=always`。
- 文档：`docs/deployment.md` 增补「§X 非容器 Linux 主机部署」（systemd / 无 docker 主机 / 多网卡 `KK_ADVERTISE_IP`）。

---

## 6. 服务端设计

### 6.1 `MqttBridge` 变更（都在现有方法体内扩，不新增线程模型）

- `_on_message`：`_sub_topics()` 加 `+/containers`。
- `_on_hb` / `_on_status`：落库新字段（`env`/`group`/`labels`/`caps`/`docker_*`/`proto_ver`）。
- 新增 `_on_containers(host, body)`：整体 upsert 容器清单；先标记该主机全量为「待更新」，再按帧内容 upsert + 删除消失项（同一事务）；帧 `partial=true` 时只 upsert 不删。
- `dispatch_command(row)`：payload 按 `target`/`container` 组装；下发前校验 `caps.docker`（failure 时落审计 `container_dispatch_rejected`，**不发布**）。
- `_on_result`：归属校验从 `cmd["pod"] != host` 扩为 `(pod, container)` 二元组。
- `stats` 增加 `containers` 计数，`/api/system/stats` 的「Broker」组同步展示。

### 6.2 REST API（新增 + 兼容）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/hosts` | **新主路径**。`/api/containers` 保留为同一 handler 的别名。新增筛选 `group / host_type / docker_unhealthy / proto_ver`，排序 `last_seen / cpu / mem / disk` |
| GET | `/api/hosts/{host}` | 详情（+ caps/labels/group/docker 摘要 + 最近命令） |
| GET | `/api/hosts/{host}/containers` | 该主机容器清单（`state/image/health` 筛选） |
| GET | `/api/hosts/{host}/containers/{cid}` | 容器详情 |
| GET | `/api/hosts/{host}/containers/{cid}/metrics` | 容器指标序列（raw→hourly 同主机逻辑） |
| GET | `/api/docker/containers` | **跨主机容器总览**（分页 + 聚合列；禁止无分页全量） |
| POST | `/api/commands` | body 增 `target` / `container`；校验链见 §3.3 |
| GET | `/api/commands` | 增 `target` 筛选 |
| GET | `/api/system/stats` | 扩展 Broker 组（容器帧计数） |

`/api/system/agent/{current,upgrade,latest,download}` 自更新链路**不变**（容器能力与 Agent 二机制完全解耦，物理机无 docker 也能升级）。

### 6.3 审计

新增 action（全部经 `store.add_audit`，**红线不可绕过**）：
`container_dispatch_rejected`（无 docker 能力/容器不存在，带 `caps` 与 `container`）、`container_mismatch`（结果归属不符）、`container_create`（容器命令下发，detail 带 `target/container/argv`）。既有 `command_blocked/command_create` 的 detail 里补 `target` 字段。审计表限速沿用 `_audit_throttled`（key 取有界维度 `(action, host, container)`）。

### 6.4 黑名单

`security.is_blacklisted` 的输入是 argv 数组，容器命令经 `docker exec` 包装后**实际执行的 argv 是 `["docker","exec",cid,...]`**，basename 是 `docker` —— 黑名单会形同虚设。**必须让校验作用于「解包后的真实 argv」**：下发时用原始 `argv/cmdline` 校验（服务端侧行为不变），Agent 侧第二次用收到的原始 argv 校验（双端同一份规则已存在，只是 Agent 侧目前不做黑名单，容器 exec 前必须补一次同名实现或直接复用服务端规则表）。这是本次最容易漏的安全点，列为 P0 验收项。

---

## 7. 前端设计（保持风格）

### 7.1 风格守则（重设计不可越界）

- 布局/主题/预设**不动**：仍是 pure-admin-thin `layout/index.vue` + 现有主题预设；不引新依赖（Element Plus + ECharts 已覆盖）。
- 只复用 `style/kk.scss` 基座与既有范式：`kk-band` 读数条、`kk-meter` 计量条、`kk-ticks` 心跳格、`kk-sticky-bar` 吸底批量条、`kk-side` 双栏、`kk-picker` 抽屉、`kk-toolbar/kk-actions/kk-sub`、`--kk-font-num`、四档状态色。
- 新语义尽量**映射到四档**而不是加色：容器 `running→live`、`restarting→stale`、`unhealthy→alert`、`exited/paused→idle`。需要第五种时用 `el-tag` 的文字区分而非新色。
- 高度仍只在页面根算一次（`--kk-page-h`），不再写第二个 `calc`。
- 轮询统一 `usePolls()`；失败不弹 toast，只更新表头「已同步/刷新失败」读数（沿用 W5 做法）。

### 7.2 信息架构（静态路由 `router/modules/kk.ts`）

```
主机管理 (rank 1)
  ├─ /hosts/monitor            主机总览        ← 重设计
  ├─ /hosts/detail/:host       主机详情        ← 重设计（Tab 化）
  ├─ /containers               容器总览        ← 新增（跨主机 Docker 视图）
  └─ /hosts/update             版本与更新      ← 适配通用化
命令中心 (rank 2)
  ├─ /command/shell            命令面板        ← 重设计（TargetPicker）
  └─ /command/collect          采集面板        ← 适配
审计日志 (rank 3)                                    ← 筛选增强
系统统计 (rank 4)  /system                           ← 新增（低优先）
```

`/hosts/detail/:pod` 的路由参数名保持 `pod`（旧书签兼容），内部改名 `host`。

### 7.3 核心页面重设计

#### 7.3.1 主机总览 `/hosts/monitor`

**头部读数条（`kk-band`）**：主机数 / 在线 / 离线 / Docker 异常 / 待升级 / 协议待升级 / 同步读数（保留现有 `pollFailed` 态）。
**筛选行**：关键词（主机/IP/镜像/分组）、分组下拉、主机类型 segmented（全部/容器/虚拟机/物理机）、勾选「仅在线 · 仅告警 · Docker 异常」、刷新间隔（5/10/30/停）。
**表格列**：

| 列 | 内容 |
|---|---|
| 选择 | 46px |
| 状态 | 四态点 + 文字；`updating` 显示「更新中」（沿用 `OFFLINE_REASON_LABEL`） |
| 主机 | 名称（等宽，点击进详情）+ 副行 `分组 · OS · IP` |
| 类型 | tag：容器 / 虚拟机 / 物理机（tooltip 给 `virt`/`in_container` 探测值） |
| Docker | `11 运行 / 1 异常`；无能力显示 `—` + tooltip「该主机无 Docker 能力」；异常数复用 `kk-warn` 色 |
| CPU / 内存 / 磁盘 | `kk-meter`（内存同时给 MB 与百分比 tooltip） |
| Agent | 版本（`agent_outdated` 或 `proto_ver<4` 时 `kk-warn`） |
| 最近心跳 | `kk-ticks` + `ageText` |
| 操作 | 详情 / 采集 / 更多（执行命令） |

**吸底批量条**：已选 N 台 · 批量采集 · 批量执行 · 批量升级 · 导出。
**交互红线**：整行点击进详情但跳过 selection 列（现有逻辑保留）；告警行整行浅红；空态引导「还没有主机上报…」（现有文案保留）。

#### 7.3.2 主机详情 `/hosts/detail/:host`（Tab 化）

**Tab 概览**：`el-descriptions`（主机、IP、系统/内核/虚拟化、Agent 版本、上报间隔、分组、标签 chips、能力 chips：`docker 27.0.3`/`shell`）、指标曲线（现有 ECharts，时间窗 1h/6h/24h/7d，`source=raw/hourly` 提示）、快捷操作（采集 / 执行命令 / 升级）。
**Tab 容器**：容器表（名称/镜像/状态/CPU/内存（pct+MB）/重启/端口/健康），行操作：`查看日志`（下发 `container_logs` 并跳命令中心结果）、`进入容器执行`（跳命令面板并预填 target）、`重启/停止`（`container_ctl`，**二次确认弹窗，红字列出容器名与影响**）。无 docker 能力 → 引导空态（说明怎么开 `KK_DOCKER`/挂 socket）。
**Tab 进程 / 磁盘 / 网络 / 登录用户**：现有 `procs_top`、`disks`、`net`、`users` 直出，各配 meter/表格。
**Tab 最近命令**：复用 `CommandHistory`，新增 `target` 列（主机 / 容器名 badge）。

#### 7.3.3 容器总览 `/containers`（新增）

服务端聚合分页（默认 100/页），筛选：关键词（容器名/镜像/主机）、状态、分组、主机。
表格：容器名 + 副行 `host · image`、状态 tag、CPU meter、内存 meter、重启、最近更新。行点击 → `router.push('/hosts/detail/<host>', { tab: 'containers', focus: '<cid>' })`。
规模保护：页脚固定提示「按主机筛选可查看该主机全量容器」；500 台 × 50 容器场景**禁止全量拉取**（后端必须分页，前端禁止 `limit=0`）。

#### 7.3.4 命令面板 `/command/shell`（TargetPicker 是本次核心交互）

左栏（34%，`kk-col--form`）：
1. **目标选择器**（新组件 `TargetPicker.vue`，扩展自 `HostPicker.vue`）—— segmented 两种模式：
   - **按主机**：现有抽屉多选（兼容 `v-model:pods`，旧链路不破）。
   - **按容器**：抽屉分两级——先选主机（仅列 `caps.docker=true` 的主机，无能力主机置灰 + tooltip），再对每台已选主机勾选容器（支持「该主机全部容器」快捷项）。契约 `v-model:targets: Target[]`，`Target = { host, containers?: string[] }`（`containers` 缺省 = 该主机全部容器）。
2. 命令输入（textarea + `use_shell` 说明 + 快捷模板 chips：`uptime`、`df -h`、`docker ps`、`journalctl -u nginx -n 200`）。
3. 超时选择 + 下发按钮（按钮文案带明细：`下发到 3 台主机 + 7 个容器`）。

右栏（`kk-col--result`）：
- **本次下发批次卡**：总数/成功/失败/进行中 5s 轮询，失败行可「重试」（复用批量下发，自动带原 target）。
- **执行历史**：筛选（目标/状态/关键字）、`target` 列、点行展开 `out_tail` + 「查看完整输出」抽屉；`out_purged=1` 时明示「输出已清理」。

#### 7.3.5 采集面板 `/command/collect`

同骨架。指标项 9 个（新增 `docker`，描述为「Docker 容器摘要」）。容器模式下 `docker` 项置灰（容器内 collect 走 `/proc` 子集属二期）。

#### 7.3.6 审计日志 `/audit/index`

筛选增强：actor、action 下拉（新增 3 个 container 类）、host、container、时间范围、关键字；表格新增 `目标` 列（`host` 或 `host / container`）。导出 CSV 不变。

#### 7.3.7 版本与更新 /hosts/update、系统统计 /system

前者适配新字段（主机类型/能力列），后者用已有 `GET /api/system/stats` 做一个四组（主机/命令/存储/Broker）读数页，低优先、可延后。

### 7.4 五条主链路验收流程（写成走查脚本，前端交付前逐条过）

| # | 链路 | 步骤 |
|---|---|---|
| A | 异常发现→定位→处置 | 总览开「仅告警」→ 磁盘 92% 主机进详情 → 概览看曲线 → 进程 Tab 找元凶 → 执行命令 → 结果回显 → 审计留痕 |
| B | 容器异常处置 | 总览开「Docker 异常」→ 进详情容器 Tab → unhealthy 容器 → `查看日志` → `重启`（二次确认）→ 最近命令验证 rc=0 → 审计含 container 字段 |
| C | 批量采集 | 多选 10 台 → 批量采集勾 `cpu/mem/disk` → 下发成功 toast → 命令中心按批次查到 10 条 done |
| D | 批量升级 | 多选落后主机 → 批量升级 → 台账逐台核验（done/failed/timeout） |
| E | 跨主机容器操作 | 容器总览筛选 `state=exited` → 进某主机 → 对该容器 `start`/`logs` → 结果按容器维度可查 |

### 7.5 核心交互红线（core interaction rules，代码评审逐条对）

1. 任何批量动作前必须有「已选 N」粘滞条，空选时按钮禁用（不得点了才报错）。
2. 命令下发必须展示**目标计数明细**（N 台主机 + M 个容器分开报），且在提交前可见。
3. 结果必须可达：状态行 → `out_tail` → 完整输出入口，三层不缺；`out_purged` 必须明示。
4. 主机无 `caps.docker` 时，容器模式的目标项禁用并 tooltip 说明，**不给「选了却静默降级」的机会**。
5. 危险动作（`stop`/`restart` 容器、黑名单命中前的命令）二次确认，容器动作的弹窗必须写出容器名与所属主机。
6. 轮询统一 `usePolls()`，切页即停；静默失败只更新同步读数，不弹 toast。
7. 空态三分：没主机 / 没容器 / 没命令，各自的引导文案不同（不能都用「暂无数据」）。
8. 未探测与 0 必须可区分：Docker 列无能力显示 `—`，有能力且 0 容器显示 `0`。
9. 1366×768 与 dark 主题下无横向滚动、无对比度破版（8 套预设抽查 light/dark 两套即可）。
10. `pnpm build` 前确认 `web/mock/` 存在（空目录即可），产物同步到 `server/src/kk_server/web/` 并重启服务端验证。

### 7.6 新增/改动文件清单（前端）

```
web/src/api/containers.ts      # HostSummary/HostDetail 增字段（host 与 pod 并存）
web/src/api/docker.ts          # 新增：容器清单/详情/序列 + 容器命令
web/src/api/targets.ts         # 新增：能力与目标查询（/api/collect/items 同源思路）
web/src/views/command/components/TargetPicker.vue   # 新增（两级：主机→容器）
web/src/views/command/components/ContainerPicker.vue# 新增（抽屉内二级）
web/src/views/host/{monitor,detail}/index.vue       # 重设计
web/src/views/containers/index.vue                  # 新增（跨主机容器总览）
web/src/views/command/{shell,collect}/index.vue     # 重设计 + 适配
web/src/views/audit/index.vue                        # 筛选增强
web/src/router/modules/kk.ts                         # 路由增补
web/src/style/kk.scss                               # 仅追加容器状态映射类，不改既有取值
```

---

## 8. 分期计划与验收标准

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P1 协议 v4 + 主机通用化**（无 Docker） | `env/group/labels/caps/proto_ver`、双版本窗口、DB 加列、`/api/hosts` 别名+筛选、总览适配、`scripts/install.sh` + systemd | 存量 v3 Agent 不掉线；总览显示 OS/分组/类型；`/api/containers` 旧调用方零改动；全量 pytest 通过；`pnpm typecheck` 通过 |
| **P2 Docker 采集** | `docker.py`、`containers` 主题、`kk_docker_*` 三表、容器 REST、详情容器 Tab、容器总览页 | 真起 Docker 的主机上容器清单/指标可见；无 Docker 主机全页显示 `—` 且无报错；三库 `db_smoke.py` 建表通过；`docker_hb` 保留策略生效 |
| **P3 容器直连命令** | `target/container`、`docker exec`、`container_logs/container_ctl`、caps 与存在性门禁、归属校验扩展、审计扩展、Agent 侧黑名单补齐、TargetPicker | §3.3 三条防线各有测试；§7.4 链路 B/E 走查通过；黑名单对容器命令仍生效（P0）；无 docker 能力主机下发被 409 |
| **P4 前端重设计收口** | 剩余页面（审计/更新/统计）+ 交互红线逐条对 + 文档四件套同步 + 关闭 v3 窗口 | §7.5 十条红线全过；§7.4 五条链路全过；`proto/messages.md` + `docs/development.md` + `docs/deployment.md` + README 同步；`KK_DROP_PROTO_V3=1` 后仅 v4 在线 |

---

## 9. 风险与取舍（明确记账，不许默默吞）

| # | 风险 | 处置 |
|---|---|---|
| R1 | protocol_ver bump 导致存量 Agent 全网掉线 | `ACCEPT_PROTO_VERS=(3,4)` 窗口期 + 升级顺序固化 + 待升级角标 |
| R2 | 旧 Agent 忽略 `target=container` → **在宿主机静默执行** | §3.3 三层防线（服务端 caps/存在性 + Agent 硬拒绝），列为 P0 验收 |
| R3 | 容器命令经 `docker exec` 包装后**黑名单失效** | 校验作用于解包后的真实 argv，Agent 侧补齐同名实现；P0 验收项 |
| R4 | 挂 `/var/run/docker.sock` ≈ 给 Agent 宿主 root 权限 | 默认**探测+可显式关闭**（`KK_DOCKER=0`）；文档写明「不挂 socket = 无容器能力，主机管理不受影响」；生产建议配合防火墙/最小化挂载 |
| R5 | `docker stats` 在 200+ 容器主机上的开销 | CLI 分批（20/批）+ 超时 + 清单降频；`KK_DOCKER_CONTAINER_SYNC` 可调 |
| R6 | 容器 id 重建即变、UI 用 name 会撞 | UI 展示 name、key 用 id；compose 项目展示在副行 |
| R7 | 500 台 × 50 容器的容器总览全量查询 | 后端强制分页 + 每主机聚合列；前端禁用 `limit=0` |
| R8 | v3/v4 窗口期双份解析路径的复杂度 | 只做「多字段可选解析」，**不写两套 handler**；窗口关闭后删 v3 分支单测 |
| R9 | PyInstaller 二进制体积 | 零新依赖（CLI + 标准库 socket）；`agent/dist/` 体积进 CI 冒烟对比 |
| R10 | 三库方言扩散 | 新表/新列类型仍只允许出现在 `tables.py` 与 `_ADD_COLUMNS`；`scripts/db_smoke.py` 扩容器表 |

---

## 10. 测试与验证

```bash
# 后端（Windows 用 .venv 直调，勿用 uv run）
uv sync --all-packages
.venv/Scripts/python.exe -m pytest agent/tests server/tests -q        # 全量
KK_IT_MQTT_URL=mqtt://127.0.0.1:18830 .venv/Scripts/python.exe -m pytest agent/tests server/tests -q
.venv/Scripts/python.exe -m pytest -q scripts/db_smoke.py              # 三库建表/扩列/读写（含新表）
KK_MQTT_URL=mqtt://127.0.0.1:18830 .venv/Scripts/python.exe scripts/mqtt_e2e.py   # Broker 语义冒烟

# 前端
cd web && pnpm typecheck && pnpm build
rm -rf ../server/src/kk_server/web/* && cp -r dist/* ../server/src/kk_server/web/
```

新增专项：
- `agent/tests/test_docker_*.py`：CLI 解析（mock `docker ps/json` 输出）、无 docker 降级、`target=container` 硬拒绝、容器 exec 的黑名单二次校验。
- `server/tests/test_docker_*.py`：容器清单 upsert/删除、`caps` 门禁 409、跨主机/跨容器归属 mismatch、容器指标序列 raw→hourly。
- `server/tests/test_integration.py` 扩一条「真实 Docker + 真实 Agent 线程」E2E（Linux runner，无 Docker 自动 skip，沿用现有 skip 约定）。
- 前端：§7.4 五条链路 + §7.5 十条红线的人工走查单（进 `docs/quality-review-*.md` 同族记录）。

## 11. 文档同步清单（提交前）

- `proto/messages.md`：v4 主题表、帧字段、QoS/retain 表、能力协商与三层防线（**本文 §3 的事实源**）。
- `docs/development.md`：新增 `KK_DOCKER*`/`KK_GROUP`/`KK_LABELS`、容器 E2E 跑法、新表与三库注意。
- `docs/deployment.md`：新增「非容器 Linux 主机部署（systemd）」+「Docker 能力开关与安全边界」。
- `README.md` / `AGENTS.md`：产品定位句改为「Linux 主机（含 K8S 容器）与其 Docker 容器」；目录清单补 `kk_docker_*`；前端页数从 6 改 8。
- `docs/architecture-review.md` / `completion-plan-mqtt.md`：登记本方案为下一阶段计划，R1–R10 记入缺陷台账格式。
