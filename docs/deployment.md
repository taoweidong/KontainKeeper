# KontainKeeper 生产环境部署指南

本文面向**第一次接触本项目**的运维人员，从零完成一套生产环境的部署：
基础设施（MQTT Broker）→ 服务端 kk-server（含前端托管）→ TLS 反向代理 →
制作内置 Agent 的主机镜像 → 验证 → 升级与运维。

> 本地开发 / 冒烟测试请看 [开发环境搭建指南](development.md)，不适用本文的强安全要求。

## 0. 部署架构与组件

```
┌─────────────────────────┐        ┌──────────────────────────────────┐
│  被管理主机（N 台）      │        │  管理侧（1 台服务器）             │
│  vscode-server 容器/物理机│        │                                  │
│  ┌────────────────────┐ │  MQTT   │  ┌────────────┐   ┌──────────┐  │
│  │ kk-agent（镜像内置）│─┼──出站──▶│  │ Mosquitto  │◀──│ kk-server│  │
│  └────────────────────┘ │ 1883    │  │ Broker     │   │ FastAPI  │  │
└─────────────────────────┘         │  └────────────┘   └────┬─────┘  │
                                    │                        │8443    │
                                    │  ┌─────────────────────▼─────┐  │
                                    │  │ 管理界面（Vue3，服务端托管）│  │
                                    │  └───────────────────────────┘  │
                                    └──────────────────────────────────┘
```

| 组件 | 形态 | 端口 | 职责 |
|---|---|---|---|
| Mosquitto 2.x | Docker 容器 | 1883 | 消息中枢（匿名开放）：LWT 离线判定、离线命令排队 |
| kk-server | Docker 容器（或裸机） | 8443 | REST API + MQTT 桥接 + **`KK_AGENT_IPS` 接入白名单** + 数据库读写 + 前端静态托管 |
| 管理前端 | kk-server 托管 | 同 8443 | 浏览器访问，无需单独部署 |
| kk-agent | 编译进目标主机镜像 | 仅出站 | psutil 指标采集 + 远程命令执行 + 自更新（零凭据接入） |

关键认知（决定部署动作）：

- **Agent 只出站、零凭据**：被管理主机只需能主动连到 Broker 的 1883，
  无需任何入站端口，也不需要注入 token / 账号密码。
- **接入管控在服务端**：Agent 上行帧携带自报 `ip`，由 kk-server 的
  `KK_AGENT_IPS` 白名单（IP / CIDR 列表）校验，名单外全部拒绝并审计。
- **服务端无状态**：连接可靠性全在 Broker；kk-server 可多实例扩容（见 §9.3）。
- **前端零部署**：构建产物随 kk-server 包内分发（`server/src/kk_server/web/`），
  只有改过前端代码才需要重建（见 §5）。

## 1. 前置条件

| 项 | 要求 |
|---|---|
| 管理侧服务器 | Linux x86_64，2C4G 起（500 台规模估算见 [design.md](design.md) §6） |
| Docker | ≥ 24，含 `docker compose` 插件 |
| git | 任意新版 |
| TLS 证书 | 生产**必须**：管理界面须经 HTTPS 反向代理暴露（见 §6） |
| 目标主机侧 | 任意能跑 Docker 镜像的主机；制作管理镜像需要该基础镜像的拉取权限 |
| 出网 | 管理侧构建镜像时需访问 pip 源（uv 装依赖）与 apt 源（Broker 镜像装 mosquitto）；独立部署前端时另需 npm 源。运行期都不需要 |

## 2. 获取代码

```bash
git clone https://github.com/taoweidong/KontainKeeper.git
cd KontainKeeper
```

## 3. 部署 MQTT Broker（Mosquitto）

> **镜像来源（生产镜像清单约束）**：环境提供的镜像为 python-3.12 / node-20 /
> nginx-1.20 / postgres:18.6 / redis-7.2.11 / mongo-8.2.3 / ubuntu_latest——不含
> eclipse-mosquitto。Broker 统一由 `docker/broker/Dockerfile` 基于 **ubuntu:latest**
> 自建（apt 源的 mosquitto 2.0.x，满足架构要求的 2.x 语义），compose 里默认引用
> `kk-broker:latest`。清单中的 redis / mongo 本项目不使用。

推荐直接使用仓库自带的生产栈（`docker/docker-compose.prod.yml` 里的 `mosquitto` 服务，
Broker 与 kk-server 一起起）。若 Broker 必须独立部署，参照
[docker/mosquitto/README.md](../docker/mosquitto/README.md) 用同一套配置文件单独起。

### 3.1 配置要点（`docker/mosquitto/mosquitto.conf`）

- **匿名开放**（`allow_anonymous true`）：Agent 零凭据接入，无需生成
  passwordfile / aclfile / 逐主机账号；
- `persistence true`：status retain 帧落盘，Broker 重启后在线视图立即可用；
- 接入管控由 kk-server 的 `KK_AGENT_IPS` 白名单承担（见 §4.1），不在 Broker 做；
- **网络层隔离**：匿名 Broker 下唯一不可伪造的边界，建议用防火墙 / 安全组
  限制 1883 端口只对服务端与被管理主机网段开放。

### 3.2 验证 Broker（§4 栈启动后执行）

匿名即可正常订阅（挂起等待消息，Ctrl-C 退出即可）：

```bash
docker compose -f docker/docker-compose.prod.yml run --rm mosquitto \
  mosquitto_sub -h mosquitto -p 1883 -t 'kk/v1/#' -C 1 -W 5
```

## 4. 部署服务端 kk-server

### 4.1 方式 A：docker compose 一键起（推荐）

**第一步：准备 `.env`**（不入库，已在 `.gitignore`）：

```bash
cp .env.example .env
```

填写（完整字段见 `.env.example` 注释）：

```ini
KK_AGENT_IPS=<Agent 接入白名单：逗号分隔 IP / CIDR，如 10.0.0.0/24,192.168.1.5>
KK_ADMIN_PASS=<管理界面强口令>
```

> 白名单是接入管控手段：只有名单内 IP 的 Agent 才能上报数据，其余全部拒绝并审计。
> **QR1124 起白名单动态化**：运行时名单存数据库，经「系统管理 → IP 白名单」界面配置
> （增删 / 启停 / 备注），变更即时生效无需重启；`KK_AGENT_IPS` 退为启动种子。
> `KK_ENV=production` 时 env 与 DB 启用规则皆空才拒绝启动。

**白名单工作机制（v3 替代 token 认证，QR1124 动态化）**：

1. **自报**：Agent 连 Broker 时自动探测「到 Broker 方向的出口 IP」（UDP connect），
   写入每条上行帧的 `ip` 字段——MQTT 经 Broker 中转拿不到发布者真实 TCP 源 IP，
   白名单基于自报值，适合内网可信环境；
2. **校验**：kk-server 在消息入口按运行时名单逐一匹配（`ipaddress` 网段语义），
   命中任一网段即放行，全部未命中则丢弃该帧并记 WARNING 日志
   （`host=… 拒绝白名单外上报：ip='x.x.x.x'`），同时落 `ip_rejected` 审计
   （按 action 全局 60 秒节流，防审计灌水）+ 按源 IP 聚合的拦截记录
   （`kk_ip_rejects`，「系统管理 → 拦截记录」页可查可导出）；
3. **语法**：逗号分隔的 **精确 IP 与 CIDR 网段混合列表**，单个 IP 自动按 `/32`
   （IPv6 按 `/128`）处理；格式写错（如笔误的网段）**启动即失败**，好过静默全拒；
4. **动态配置**：`KK_AGENT_IPS` 首次启动播种进 DB（`kk_ip_rules`，缺则补、管理员
   改过不重置），此后 DB 为运行时真相源。界面增删/启停规则后服务端即时刷新桥接与
   REST 两处快照（多实例 30s 内收敛），无需重启。**名单为空 = 放行全部**（仅开发/测试），
   生产请至少保留一条启用规则。

配置示例（覆盖被管理主机网段，可混写）：

```ini
# 整个网段放行（推荐，新增主机无需改配置）
KK_AGENT_IPS=10.0.0.0/24
# 网段 + 个别精确 IP 混合
KK_AGENT_IPS=10.0.0.0/24,192.168.1.5
# 多网段
KK_AGENT_IPS=10.0.0.0/24,10.99.0.0/16
```

**第二步：启动**：

```bash
docker compose -f docker/docker-compose.prod.yml --env-file .env up -d --build
```

生产栈自动带上 `KK_ENV=production`，启动时做安全自检（见 4.4）。

**第三步：确认**：

```bash
docker compose -f docker/docker-compose.prod.yml ps        # 两个服务 running
curl http://127.0.0.1:8443/api/health               # {"ok":true,"broker":"connected",...}
```

`broker: connected` 表示 kk-server 已连上 Mosquitto。

### 4.2 方式 B：手动 docker build / run

镜像构建上下文是**仓库根**（uv workspace，锁在根 `uv.lock`）：

```bash
docker build -f docker/Dockerfile -t registry.example.com/kontainkeeper-server:0.1.0 .
docker run -d --name kontainkeeper \
  -p 8443:8443 \
  -v kontainkeeper-data:/data \
  -e KK_MQTT_URL=mqtt://broker:1883 \
  -e KK_AGENT_IPS=10.0.0.0/24,192.168.1.5 \
  -e KK_ADMIN_USER=admin \
  -e KK_ADMIN_PASS=<强密码> \
  -e KK_ENV=production \
  registry.example.com/kontainkeeper-server:0.1.0
```

### 4.3 方式 C：裸机运行（Python 3.12 + uv）

```bash
uv sync --all-packages --extra postgres   # 或 --extra mysql；纯 SQLite 则不加
export KK_MQTT_URL=mqtt://broker:1883 \
       KK_AGENT_IPS=10.0.0.0/24,192.168.1.5 \
       KK_ADMIN_PASS=<强密码> KK_ENV=production
uv run kk-server                            # 监听 0.0.0.0:8443
```

### 4.4 启动安全自检（KK_ENV=production）

`KK_ENV=production` 时会**直接拒绝启动**以下情况：

- 管理口令仍是默认 `admin123`（`config.py` 装配期检查）；
- IP 白名单未配置：`KK_AGENT_IPS` 为空**且** DB 无启用规则（白名单为空 = 放行所有上报）。
  QR1124 起此检查在 lifespan 建库后（`ensure_whitelist_configured`），故界面配置的
  规则也算「已配」——不必再依赖环境变量。

自检不过会抛 `RuntimeError` 并退出，按提示修正后重启。

### 4.5 服务端环境变量参考

| 变量 | 说明 | 默认 |
|---|---|---|
| `KK_MQTT_URL` | Broker 地址（`mqtt://` / `mqtts://`） | 必填 |
| `KK_AGENT_IPS` | Agent 接入白名单**启动种子**（逗号分隔 IP / CIDR；首次播种进 DB，此后界面配置为准；空 = 放行全部，生产须 env 或 DB 任一非空） | 空 |
| `KK_MQTT_CLIENT_ID` | 实例唯一 client_id；**多实例必须逐实例唯一**，否则被 Broker 互踢 | `kk-server` |
| `KK_MQTT_KEEPALIVE` | MQTT 保活秒数（下限 10） | `60` |
| `KK_MQTT_USERNAME` / `KK_MQTT_PASSWORD` | 服务端连 Broker 的凭据（Broker 匿名时留空） | 空 |
| `KK_MQTT_TLS_CA` / `KK_MQTT_TLS_INSECURE` | Broker TLS 配置 | 空 |
| `KK_TOPIC_PREFIX` | 主题前缀（**双端同名，必须一致**） | `kk/v1` |
| `KK_DB_PATH` | SQLite 文件路径 | `kk-server.db` |
| `KK_DB_URL` | 选库：`sqlite+aiosqlite:///` / `postgresql+asyncpg://` / `mysql+aiomysql://`，配了优先于 `KK_DB_PATH` | SQLite |
| `KK_DB_POOL_SIZE` | PG 连接池常驻连接数（桥接/API/janitor 共用）；仅 PG 生效 | 10 |
| `KK_DB_MAX_OVERFLOW` | PG 连接池允许的超发上限 | 20 |
| `KK_ADMIN_USER` / `KK_ADMIN_PASS` | 管理员账号 | `admin` / `admin123` |
| `KK_CMD_BLACKLIST` | 命令黑名单（逗号分隔子串，拦危险命令） | `rm -rf /,mkfs,...` |
| `KK_HOST` / `KK_PORT` | 监听地址 / 端口 | `0.0.0.0` / `8443` |
| `KK_ENV` | 设 `production` 启用启动安全自检 | 空 |
| `KK_WEB_DIR` | 前端静态目录 | 包内 `web/` |
| `KK_PUBLIC_URL` | 服务端对外访问基址（如 `http://10.0.0.1:8443`）。**启用自更新的必配项**：配了它，下发给 Agent 的更新帧就带绝对下载地址，镜像侧无需再配 `KK_UPDATE_URL`；不配则仍是相对路径，Agent 拿不到地址时只记 WARNING 并跳过更新 | `""` |
| `KK_INTERVAL_MIN` | 允许的最小上报间隔（秒）。低于此值的心跳**照常落库**，但会写审计 `interval_violation` 并在 `/api/system/stats` 的 `broker.stats.interval_violation` 计数——用于发现误配机器。留空 = 不检查 | `""` |
| `KK_UPDATE_HMAC_KEY` | 自更新清单 HMAC 密钥（QR-P0-1）。配了它：上传时对二进制本体算 HMAC 写入清单 `sig`，推送帧与 `/agent/latest` 都携带；**必须与 Agent 侧同名键一致**。不配则 Agent 推送路径默认拒绝未签名更新（除非 Agent 配 `KK_UPDATE_ALLOW_UNSIGNED=1`），见 §9.2.1 | `""` |
| `KK_UPDATE_MODE` | 自更新策略（D2.1）：`manual`（默认）下 `/agent/latest` 恒答「无需升级」，升级走管理界面「版本与更新」页选机推送；`auto` 恢复自动——Agent 按 `KK_UPDATE_INTERVAL` 轮询拉齐 + 桥接对落后主机自动推送 | `manual` |
| `KK_UPDATE_QUEUE_TTL` | 升级推送离线队列的保留时长（秒，下限 3600） | `604800`（7 天） |
| `KK_CMD_QUEUE_TTL` | QR1125：SSE 通道命令离线队列（`commands.status='queued'`）保留时长（秒，下限 3600），超期由 `sweep_queued_command_timeouts` 收敛；与在途命令的 timeout+slack 清扫互不干扰 | `86400`（24h） |
| `KK_LOG` | 服务端日志文件路径；留空 = 只写 stdout（docker 采集） | `""` |
| `KK_LOG_LEVEL` | 日志级别 | `INFO` |
| `KK_LOG_JSON` | 设 `1` 输出 JSON Lines（接日志采集端时打开） | 关闭 |
| `KK_AGENT_BIN_DIR` | Agent 二进制资产目录（自更新上传/回滚的存放位置） | `agent_assets` |

### 4.6 数据库选型

- **SQLite**（默认）：单文件、零运维，适合 ≤ 数百台规模；数据卷挂 `/data` 即可。
- **PostgreSQL / MySQL**：设置 `KK_DB_URL`，并在安装时带对应 extras
  （`--extra postgres` / `--extra mysql`）。三库方言差异已收在 store 的既定
  汇聚点（引擎装配 / `_upsert` / `_ensure_schema` / 批删助手）；CI 夜测已有真库冒烟
  （`scripts/db_smoke.py`，Jenkinsfile ⑬ 跑生产镜像清单的 PG:18.6；MySQL 不在清单内，
  由 `MY_IMAGE` 参数按需注入）覆盖建表/扩列/基础读写，
  上线前仍建议按真实规模在预发环境验证。
- **PostgreSQL 专属调优（启动自动生效，无需手工执行）**：
  引擎侧批量 INSERT 由 SQLAlchemy 2.0 insertmanyvalues 自动拼单条多值语句
  （asyncpg 不支持也不需要 `executemany_mode`，传入会被引擎直接拒绝）与
  连接池（`KK_DB_POOL_SIZE`/`KK_DB_MAX_OVERFLOW`）。维护侧 `_pg_tune` 在 `setup()`
  事务内逐条执行幂等语句（表属主账号即可，不需超级用户；失败上抛 fail-fast）：
  - **autovacuum**：四张热表行数级 scale factor——`kk_containers` 0.05/analyze 0.02
    （每秒被心跳 UPDATE 数次，默认 0.2 比例近乎永不触发）、`kk_heartbeats`/`kk_audit`
    0.02（追加+批删，批删后死元组要更勤回收，QR1126 补 audit）、`kk_commands` 0.05；
    `kk_containers` 再压绝对阈值 `autovacuum_vacuum_threshold=25`/`analyze_threshold=25`
    （500 行小表 scale 项算出的触发量仍偏高，行数下限兜底让状态翻转更早回收）。
  - **fillfactor=80**（`kk_containers`）：更新最热的表每帧心跳 upsert 整行摘要，
    留页内空间让 HOT（同页更新、免索引维护）生效，写放大与索引膨胀同步下降。
  - **BRIN 索引** `kk_heartbeats.ts`/`kk_audit.ts`：ts 与写入物理相关，时间范围扫描
    （cleanup 挑批/聚合窗口）用 KB 级索引替代整棵 btree；`pages_per_range=64`
    （默认 128 页≈全机队 36s 写入，2 天表粒度偏粗，QR1126 经 `ALTER INDEX SET` 细化，
    只影响后续 summarization，存量 range 随写入自然细化）。
  - **部分索引**：`idx_cmd_inflight`（`kk_commands(created_at) WHERE status IN
    ('pending','sent','running')`，超时清扫从全表扫变常数命中）、`idx_upd_inflight`
    （`kk_updates(pod, created_at) WHERE status IN ('pending','queued')`，QR1126，
    每主机上线的在途升级查询免排序命中）。**谓词与域查询逐字同步**（test_dialects 锁）。
  - **lz4 TOAST 压缩**（QR1126，PG14+）：`kk_heartbeats.metrics`/`kk_containers.last_metrics`/
    `kk_commands.out_b64` 三个大字段 `SET COMPRESSION lz4`，解压 CPU 比默认 pglz 低数倍；
    只影响新写入行，存量随保留期自然轮换（heartbeats 2 天全量生效）。PGDG/官方镜像
    PG14+ 均带 `--with-lz4`。
  - **PG18 `autovacuum_vacuum_max_threshold`**（QR1126，**版本守卫**）：给无界增长的
    `kk_commands` 设死元组触发上限 100 万（默认 GUC 5000 万对 >20M 行表过迟）。该
    reloption 是 PG18 新增，`_pg_tune` 现查 `server_version_num`，**PG<18 自动跳过**，
    不破坏旧版本启动。当前规模 scale 0.05 仍主导，此为极端增长的安全 ceiling。
  - **批删**在 PG 上走「子查询挑批」的单语句变体，每批省一次往返。

  以上全部是表级幂等语句，升级库与新建库都靠启动时执行这一份；CI 夜测
  `scripts/db_smoke.py` 在真 PG18 上回读 catalog（reloptions/pg_indexes/attcompression）
  验证落地，不止 compile 级。

#### PG18 部署调优（可选 PG 服务）

`_pg_tune` 只承接**表级**调优；PG18 旗舰特性**异步 I/O（AIO）**与内存/WAL/监控参数是
**postmaster/实例级**，落部署而非代码（`ALTER SYSTEM` 需超级用户 + 重启，违背 `_pg_tune`
「表属主即可」口径）。仓库提供 turnkey 的 `docker/docker-compose.pg.yml` override，叠加在
prod/offline 之上把默认 SQLite 换成调优后的 PG18（**不叠加则默认拓扑不变**）：

```bash
docker compose -f docker/docker-compose.prod.yml -f docker/docker-compose.pg.yml \
  --env-file .env up -d        # 离线栈换 -f docker-compose.offline.yml（需 PACK_OPT=1 打入 PG 镜像）
```

服务端镜像已 bundled asyncpg（`docker/Dockerfile --extra postgres`），PG 开箱可用；
`KK_DB_URL` 由 override 自动拼好指向 `db` 服务。外置 PG（不由本 compose 管）时，把下表
参数交给运维写进 `postgresql.conf` 或超级用户 `ALTER SYSTEM` 后重启即可。

调优值（`docker-compose.pg.yml` 默认 **2GB 容器内存预算档**；4GB 档见右列）：

| 参数 | 2GB 档 | 4GB 档 | 级别 | 作用 |
|---|---|---|---|---|
| `io_method` | `worker` | `worker` | postmaster | PG18 AIO；**不用 io_uring**（见下） |
| `shared_buffers` | `512MB` | `1GB` | postmaster | 默认 128MB 对 4-6GB heartbeats 偏小，覆盖热集 |
| `effective_cache_size` | `1536MB` | `3GB` | sighup | 规划器口径（含 OS page cache） |
| `work_mem` | `16MB` | `32MB` | user | 500 组 hash 聚合很小；并行 worker 各算一份，别给太大 |
| `maintenance_work_mem` | `256MB` | `512MB` | user | vacuum/建索引 |
| `wal_compression` | `lz4` | `lz4` | sighup | 追加负载 FPW 占 WAL 大头，压缩省写带宽 |
| `max_wal_size`/`min_wal_size` | `2GB`/`512MB` | 同 | sighup | 降 checkpoint 频率→更少 FPW |
| `checkpoint_timeout` | `10min` | `10min` | sighup | 默认 5min；追加负载放宽（代价：崩溃恢复变长） |
| `effective_io_concurrency`/`maintenance_io_concurrency` | `32` | `32` | user | 默认 16；SSD/NVMe 可 32-64 |
| `track_io_timing`/`track_wal_io_timing` | `on` | `on` | superuser | 默认 off；监控前提（现代 Linux 开销可忽略） |
| `shared_preload_libraries` | `pg_stat_statements` | 同 | postmaster | 慢查询统计前提 |

- **AIO 作用面与 io_uring**：AIO 加速**顺序扫/bitmap heap scan/vacuum**（heartbeats BRIN
  范围扫、`_aggregate_hours` 宕机后追赶窗口、cleanup 批删后 vacuum 直接受益）；**索引点查
  不走 AIO**——别指望列表接口 3x（官方 3x 是 bulkread 场景）。`io_method=io_uring` 需内核
  ≥5.1 + liburing，但 **Docker 默认 seccomp 封禁 `io_uring_setup/enter/register` 三 syscall**
  （容器逃逸风险，moby/moby#46762），设了会启动 EPERM 失败；解封需 `seccomp=unconfined`
  扩大内核攻击面，**不建议**。`worker` 是容器内稳妥且默认的选择，已拿主要收益。
- **PGDATA 路径陷阱**：PG18 官方镜像把默认 `PGDATA` 改到 `/var/lib/postgresql/18/docker`、
  `VOLUME` 上移到 `/var/lib/postgresql`（旧版 `/var/lib/postgresql/data`）。沿用旧挂载路径会
  **起空库**。override 已**显式钉死 `PGDATA=/var/lib/postgresql/data`** 并把命名卷挂在该路径，
  版本无关、重启数据持久；自管 conf 时 likewise 显式设 `PGDATA`。
- **版本注**：环境镜像清单钉死 `postgres:18.6`（全部 PG18 特性都在）；18 当前 minor 为
  18.7，可 bump 时建议 18.7 或滚动 `postgres:18`。**18.x 小版本升级换二进制重启即可，无需
  pg_upgrade**；未来 18→19 大版本升级时 PG18 的 `pg_upgrade` 保留优化器统计（升完即有正常
  计划，消除「升完计划抖动」）。认证保持 SCRAM（md5 在 18 弃用）；asyncpg 走协议 3.0，
  服务端 3.2 向后兼容，零动作。

#### 监控巡检（只读 SQL，PG18 增强视图）

```sql
-- AIO 与 I/O 全景（PG18 新增 read_bytes/write_bytes/extend_bytes + WAL 行）：
-- context=bulkread 看 heartbeats 大扫，vacuum 看批删回收；evictions 高→shared_buffers 偏小
SELECT backend_type, object, context, reads, read_bytes, read_time,
       writes, write_bytes, hits, evictions, fsyncs, fsync_time
FROM pg_stat_io WHERE object = 'relation' ORDER BY read_bytes DESC NULLS LAST;

-- 在途 AIO（PG18 新视图） / 单后端 I/O（PG18 新函数，定位慢查询所属连接）
SELECT * FROM pg_aios;
SELECT * FROM pg_stat_get_backend_io(pg_backend_pid());

-- containers HOT 比例（验证 fillfactor=80 生效）+ 死元组/autovacuum 触发
SELECT relname, n_tup_upd, n_tup_hot_upd,
       round(100.0*n_tup_hot_upd/nullif(n_tup_upd,0),1) AS hot_pct,
       n_dead_tup, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE relname IN ('kk_containers','kk_heartbeats','kk_commands','kk_audit');

-- pg_stat_statements（PG18 新增并行计数列）：to_launch 远大于 launched→worker 池不够
SELECT queryid, calls, mean_exec_time, rows, shared_blks_hit, shared_blks_read,
       parallel_workers_to_launch, parallel_workers_launched
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
SELECT datname, parallel_workers_to_launch, parallel_workers_launched FROM pg_stat_database;
```

判读口径见官方 monitoring-stats 文档：`read_time` 非零前提是 `track_io_timing` 全程开启；
client backend 大量 `writes`/`fsyncs` → checkpointer/bgwriter 配置问题。

#### 评估过暂不采用的 PG18 特性（留触发条件）

下列特性对当前 500 台单实例工作负载要么无收益、要么破坏三库方言收敛（AGENTS.md：差异
只许在 store 既定汇聚点），评估后缓行：

| 特性 | 不采用理由 | 触发条件 |
|---|---|---|
| `uuidv7()` 主键 | heartbeats/audit 自增 Integer 本就时间有序；`commands.id` String(32) 装不下 36 字符 UUID 且是 MQTT 协议关联键；session token 必须不可预测（v7 时间可猜是安全退化）；三库无统一 uuid 类型 | 真要时间有序字符串 ID 时，应用层 `ts+random` 前缀三库通用，优于 PG 专属 |
| JSONB 列 + GIN | 现查询只做时间范围扫+标量聚合+四列 LIKE，无 JSON 内检索；TEXT→JSONB 需 144 万行重写+锁；MySQL/SQLite 三库分叉 | 产品要「按 metrics 内字段过滤/聚合」时再评估 |
| `FOR UPDATE SKIP LOCKED` | 单实例 kk-server，命令经 MQTT 桥下发、sweep 单条 UPDATE，无多消费者认领竞争；SQLite 无行锁 | kk-server 水平扩展 + 命令改 DB 认领时 |
| 虚拟生成列 | PG18 虚拟列**不能索引**（500 行表本该全表扫，省不了）；`COALESCE(disk_limit,85)` 会改总览页全局 85 口径；STORED 版给最热表加写放大 | containers 涨到万级且 disk_alert 筛选成瓶颈，先统一 disk_limit 语义 |
| `MERGE`/`RETURNING old/new` | MERGE 是 PG 专属语法（MySQL/SQLite 无），开分支违反收敛；状态翻转审计应用层比较前后值更简单 | 审计链路要求单语句原子前后值时 |
| temporal constraints | 无「有效期区间」语义（containers 当前态、commands 状态机） | 出现区间重叠约束需求时 |

### 4.7 启用 MQTT 鉴权（可选加固）

默认模式是**匿名 Broker + 服务端 `KK_AGENT_IPS` 白名单**（v3 设计，安全边界依赖网络层
对 1883 的可达控制）。若需要 Broker 层的硬边界（安全评审 P0 建议），叠加鉴权配置：

1. 生成密码文件（两个账号；`docker/mosquitto/auth/aclfile` 已按最小权限写好：
   下行 cmd 主题仅服务端账号可发布）：

   ```bash
   docker run --rm -v "$PWD/docker/mosquitto/auth:/auth" kk-broker:latest \
     mosquitto_passwd -c /auth/passwd kk-server      # 服务端账号，交互输入口令
   docker run --rm -v "$PWD/docker/mosquitto/auth:/auth" kk-broker:latest \
     mosquitto_passwd -b /auth/passwd kk-agent <AGENT口令>
   ```

2. `.env` 增加服务端凭据：`KK_MQTT_USERNAME=kk-server`、`KK_MQTT_PASSWORD=...`；
3. 叠加启动：

   ```bash
   docker compose -f docker/docker-compose.prod.yml -f docker/docker-compose.secure-mqtt.yml --env-file .env up -d
   ```

4. Agent 侧凭据随镜像烧入：构建时带 `KK_MQTT_USERNAME=kk-agent` 与
   `KK_MQTT_PASSWORD`（`scripts/build.sh` 会写入镜像 ENV）。注意镜像 ENV 对容器内
   用户可读（与 `KK_SERVER` 同级），是该方案的已知取舍；更严格做法是运行时经编排
   系统注入环境变量。

启用后匿名接入被 Broker 直接拒绝，cmd 主题只有服务端账号能发布——安全评审定性为
「全网 RCE」的匿名下行通道即被关闭。不叠加本配置时行为与原匿名模式完全一致。

## 5. 前端（管理界面）

**默认无需任何动作**：前端构建产物已随包提交在 `server/src/kk_server/web/`，
kk-server 启动时自动托管（`KK_WEB_DIR` 默认即该目录），浏览器访问 8443 端口即可。

仅当**修改了 `web/` 前端代码**后才需要重建：

```bash
cd web
pnpm install && pnpm build          # 产物输出 web/dist/
# 人工同步产物（web/dist 被 .gitignore 忽略，不会自动进包）
rm -rf ../server/src/kk_server/web/* && cp -r dist/* ../server/src/kk_server/web/
cd .. && docker compose -f docker/docker-compose.prod.yml --env-file .env up -d --build
```

前端构建要求 Node ≥ 20.19（或 ≥ 22.13）、pnpm ≥ 9，且 `web/mock/` 目录存在（空目录即可）。
生产镜像清单为 node-20，`web/.nvmrc` 固定 v20.19.0；独立前端镜像（`web/Dockerfile`）
即用 node:20 构建、nginx:1.20 托管。

## 6. TLS 与反向代理（生产硬约束）

两条红线（明文风险详见 [architecture-review.md](architecture-review.md)）：

1. **管理界面必须走 HTTPS**：管理员会话 token 若明文跨越网络会被截获。
   kk-server 自身只听 HTTP，须前置 TLS 终结的反向代理：

```nginx
server {
    listen 443 ssl;
    server_name kk-server.ops.example.com;

    ssl_certificate     /etc/nginx/ssl/kk-server.crt;
    ssl_certificate_key /etc/nginx/ssl/kk-server.key;

    location / {
        proxy_pass http://127.0.0.1:8443;      # 指向 kk-server
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # QR1125：SSE 下行命令通道必须关缓冲，否则 ping/命令帧被 nginx 攒住不下发
    location /api/v1/commands/stream {
        proxy_pass http://127.0.0.1:8443;
        proxy_buffering off;                   # 关键：SSE 逐帧透传
        proxy_cache off;
        proxy_read_timeout 300s;               # > 15s ping 间隔
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_http_version 1.1;
    }
}
```

   随后把 8443 从公网暴露中收回，只允许反代访问。

   > **QR1125 HTTP/SSE 通道的源 IP 口径**：ingest（`/api/v1/ingest/*`）与 SSE
   > （`/api/v1/commands/stream`）的白名单鉴权用 `agent_ip_auth` 读**真实 TCP 源 IP**
   > （`request.client.host`，不读 X-Forwarded-For）。经上述公网反代时源 IP 会退化为
   > 代理地址——与自更新接口同口径（§9.2），**Agent 的 `KK_SERVER` 应配置为内网直连
   > kk-server 的地址**，不走公网反代；若必须经反代，白名单需放行代理地址（会失去
   > 逐主机 IP 管控意义，不推荐）。MQTT 通道不受此影响（自报 ip + Broker 直连）。

2. **Agent ↔ Broker 的 MQTT 连接**：跨不可信网络必须 `mqtts://`（配
   `KK_MQTT_TLS_CA`，Agent 侧对应 `KK_TLS_CA`）；Broker 与 Agent 同处内网可信区
   时可用 `mqtt://`。注意 `mqtts://` **不配 CA 时仅加密不校验证书**（中间人可截获
   Broker 凭据），服务端与 Agent 都会打显式 warning（QR-S46）——生产务必配 CA。

## 7. 部署 Agent 到被管理主机

v3 起接入**零凭据**：Agent 不携带任何 token / 账号密码，唯一前提是该主机出口 IP
在服务端 `KK_AGENT_IPS` 白名单内（§4.1）。按主机形态二选一：

| 方式 | 适用场景 | 小节 |
|---|---|---|
| 镜像内置 | 新建主机 / 批量装机（vscode-server 容器等），上线即连、用户无感知 | §7.1–7.2 |
| 二进制直接拉起 | 已有主机，免制作镜像，一条命令接入 | §7.3 |

### 7.1 构建管理镜像

在能访问 Docker 的构建机上（需 `python3`，用于解析原镜像入口的 JSON）：

```bash
BASE_IMAGE=myregistry/vscode-server:1.2 \
KK_SERVER=mqtt://broker.ops.example.com:1883 \
  bash scripts/build.sh myregistry/vscode-server-managed:1.2
```

脚本做了什么：

1. 调 `agent/build/build_binary.sh` 把 kk-agent 编译为**单文件二进制**
   （PyInstaller，目标镜像**无需内置 Python**）；
2. `docker image inspect` 读出原镜像的 ENTRYPOINT/CMD，生成 Docker 原生
   exec 形式（JSON 数组）的透传指令：`kk-entrypoint` 作为 ENTRYPOINT 首元素、
   原入口其余元素作参数透传——不经 shell 解析，无空格路径拆坏问题；
3. 叠加二进制、插件目录、`kk-entrypoint` 监管脚本，并烧入 `KK_SERVER` 等
   **非敏感**配置。

运行期行为：`kk-entrypoint` 后台监管 Agent（崩溃 5s 拉起），前台拉起原 IDE 入口，
**容器生命周期 = 原 IDE 生命周期**，用户看到的启动行为不变。

### 7.2 方式一：镜像内置（新建主机 / 批量装机）

v3 起 Agent 不携带任何凭据（Broker 匿名开放），`docker run` 无需注入 token：

```bash
docker run ... myregistry/vscode-server-managed:1.2
```

唯一前提：该主机的出口 IP 在服务端 `KK_AGENT_IPS` 白名单内（按网段配置时
通常天然覆盖，如 `10.0.0.0/24`）。

### 7.3 方式二：已有主机直接拉起二进制（免制作镜像）

v3 的核心简化：Agent 是**零依赖单文件二进制**，目标主机**无需 Python、无需装包、
无需任何凭据配置**，一条命令拉起。

**第一步：在构建机编译并分发**（目标主机不需要出网）：

```bash
cd agent && bash build/build_binary.sh          # 产出 dist/kk-agent（Linux 单文件，约 8–12MB）
scp dist/kk-agent user@target-host:/opt/kk-agent
ssh user@target-host chmod +x /opt/kk-agent
```

**第二步：目标主机上一条命令拉起**（位置参数 = Broker 地址，零凭据）：

```bash
/opt/kk-agent mqtt://broker.ops.example.com:1883
```

常用可选参数（均非必须，详见 §7.5）：

```bash
KK_HOST_NAME=host-a KK_INTERVAL=30 /opt/kk-agent mqtt://broker.ops.example.com:1883
```

**第三步（推荐）：systemd 常驻**，开机自启 + 崩溃自动拉起。仓库提供一键安装脚本：

```bash
sudo KK_SERVER=mqtt://broker.ops.example.com:1883 bash scripts/install.sh
# 默认装 /opt/kk-agent/（二进制 + 0600 的 kk-agent.env，只创建不覆盖），
# 写 /etc/systemd/system/kk-agent.service；可选变量见脚本头注释（KK_GROUP/KK_LABELS 等）。
# 预演不落盘：KK_INSTALL_DRY_RUN=1 bash scripts/install.sh
```

下面的手写单元是它的等价手工版：

```ini
# /etc/systemd/system/kk-agent.service
[Unit]
Description=KontainKeeper Agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/kk-agent mqtt://broker.ops.example.com:1883
Restart=always
RestartSec=5
# 可选覆盖：主机标识 / 心跳间隔 / 自报 IP
# Environment=KK_HOST_NAME=host-a
# Environment=KK_INTERVAL=30
# Environment=KK_ADVERTISE_IP=10.0.0.15

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now kk-agent
journalctl -u kk-agent -f          # 看到 "mqtt connected" 即为上线成功
```

> 拉起失败的两种典型排障：报「KK_SERVER 未配置」= 没带位置参数也没设环境变量；
> 进程在跑但管理界面不出现 = 出口 IP 不在白名单（查审计 `ip_rejected`，见 §7.4）。

### 7.4 自报 IP 不准时的覆盖（多网卡 / NAT / 回环）

Agent 默认用 UDP connect 自动探测「到 Broker 方向的出口 IP」写入上行帧，
多网卡环境自动选中正确一侧。NAT / 探测不准时用 `KK_ADVERTISE_IP` 显式覆盖：

```bash
docker run -e KK_ADVERTISE_IP=10.0.0.15 ... myregistry/vscode-server-managed:1.2
# 或二进制直接拉起时：
KK_ADVERTISE_IP=10.0.0.15 /opt/kk-agent mqtt://broker.ops.example.com:1883
```

> 自报 IP 不在白名单内的主机会被服务端拒绝上报并在审计页留下 `ip_rejected`
> 记录——排查「主机上线但总也不出现」时先查这里。

典型的三种现象（真实验证环境实测）：

| 现象 | 原因 | 处置 |
|---|---|---|
| Broker 与 Agent 同机（WSL/本机回环），自报 `127.0.0.1` 被拒 | 探测到回环地址，白名单没放行回环 | 设 `KK_ADVERTISE_IP=<真实内网 IP>`，或白名单加入该网段 |
| Agent 在跑、`mqtt connected`，但界面不出现 | 自报 IP 不在白名单，帧被丢弃 | 审计页查 `ip_rejected`、服务端日志查「拒绝白名单外上报」 |
| 多网卡主机时好时坏 | 自动探测随路由选中不同网卡 | 固定 `KK_ADVERTISE_IP` |

### 7.5 Agent 环境变量参考（镜像内已注入大部分，一般无需改动）

| 变量 | 说明 | 默认 |
|---|---|---|
| `KK_SERVER` | Broker 地址（`mqtt://` / `mqtts://`，**不含凭据**） | 必填 |
| `KK_ADVERTISE_IP` | 自报出口 IP 覆盖（多网卡 / NAT 下用） | 自动探测 |
| `KK_HOST_NAME` | 主机标识（旧名 `KK_POD_NAME` 兼容） | hostname |
| `KK_TOPIC_PREFIX` | 主题前缀，须与服务端一致；含通配符 `+`/`#` 启动即拒（QR29-A11） | `kk/v1` |
| `KK_INTERVAL` | 心跳/采集间隔（秒，下限 1） | `60` |
| `KK_DISK_PATHS` | 采集挂载点（逗号分隔；空 = 自动发现全部物理挂载点） | 自动发现 |
| `KK_HB_ITEMS` | 心跳采集项（白名单子集；千进程主机可去掉 `proc` 降负载） | 全部 8 项 |
| `KK_PLUGIN_DIR` | 自定义采集插件目录 | `/opt/kk-agent/plugins` |
| `KK_PLUGIN_TIMEOUT` | 插件 `collect()` 超时（秒），超时隔离至重载 | `5` |
| `KK_ALLOW_SHELL` | 允许 `use_shell` 管道模式 | `1` |
| `KK_MAX_OUT_MB` | 单命令输出上限 | `4` |
| `KK_MAX_QUEUED` | 离线 out-queue 上限（≈64KB/块，128 块 ≈8MB 驻留预算） | `128` |
| `KK_UPDATE_URL` | 管理 API 基址（自更新用，未配则跳过自更新） | 空 |
| `KK_UPDATE_INTERVAL` | 版本轮询间隔（秒，≥30） | `300` |
| `KK_UPDATE_DISABLED` | 设 `1/true` 关闭自更新。**同时关掉轮询与推送两条路**（QR-P0-1）：推送式升级命令会收到 `update_disabled` 回执 | 关闭 |
| `KK_UPDATE_ALLOW_UNSIGNED` | 设 `1/true` 放行**未签名**的推送更新（QR-P0-1 逃生口）。默认拒绝：未配 `KK_UPDATE_HMAC_KEY` 时，任何能连 Broker 的客户端都能向 cmd 主题发推送更新帧。仅建议内网可信部署显式开启；轮询路径不受影响 | 关闭 |
| `KK_UPDATE_HMAC_KEY` | 与服务端同名键配同一密钥：推送更新强制验签（对二进制本体的 HMAC，QR-P0-1）。配了它未签名的清单同样被拒 | 空 |
| `KK_UPDATE_REQUIRE_SIG` | 设 `1/true` 后**轮询下载路径**也强制清单携带有效 HMAC 签名（须配 `KK_UPDATE_HMAC_KEY`）；默认轮询路径只校验 sha256 | 关闭 |
| `KK_GROUP` / `KK_LABELS` | 机队分组（单值，截 64 字符）与标签（`k=v,k2=v2` 多值，key 截 64 / value 截 128），随 status 帧上报供总览多维筛选 | 空 |
| `KK_AGENT_BIN` | 自更新替换目标路径（build.sh 已烧入） | 自动 |
| `KK_NICE` | Agent 进程 CPU 调度优先级（**由镜像 entrypoint wrapper 读取**，非 Agent 自身）；`19` = 最低，与用户 IDE 争抢 CPU 时主动让位；置 `0` 等价关闭降权 | `19` |

## 8. 验证部署（第一次上线照做）

1. **服务端健康**：`curl https://kk-server.ops.example.com/api/health` 返回
   `ok:true`、`broker:"connected"`。
2. **登录管理界面**：浏览器打开 `https://kk-server.ops.example.com`，
   用 `KK_ADMIN_USER` / `KK_ADMIN_PASS` 登录。
   注意：登录失败按**用户名**与**客户端 IP** 两个维度各自计数，任一维度在
   5 分钟窗口内输错 5 次即锁定 300 秒（用户名挡定向爆破，IP 挡用户名枚举）；
   失败计数在最后一次失败 300 秒后自动清零，更早的失败不累积（QR29-S7）。
   经反向代理时源 IP 会退化为代理地址（服务端只认直连地址、不读
   `X-Forwarded-For`），此场景请改由网关限流。
3. **启动一台被管理主机**（零凭据：镜像内置见 §7.2，或二进制直接拉起见 §7.3）：
   管理界面「主机总览」应在数十秒内出现该主机（状态 = 在线）。
   **指标要等首帧心跳**（默认 60s 间隔，可临时调小 `KK_INTERVAL` 加速验证），
   详情页出现 CPU/内存曲线即为全链路通。
4. **命令下发**：命令中心对该主机下发一条简单命令（如 `uname -a`），
   历史列表出现结果、审计页出现 `command` 记录。
5. **离线判定**：停掉该主机容器，管理界面状态很快变为离线
   （优雅停止由 Agent 发离线帧，强杀/崩溃则由 Broker 的 LWT 机制接管）。
6. **白名单负向验证（建议）**：在一台**不在白名单内**的主机上拉起 Agent，
   确认它**不会**出现在管理界面、审计页出现 `ip_rejected` 记录、
   服务端日志出现「拒绝白名单外上报」——证明接入管控真实生效。

任一步失败，排查顺序：`docker logs`（kk-server 与 mosquitto 两容器）→
Agent 日志（容器内 `/var/log/kk-agent.log`）→ 该主机自报 IP 是否在
`KK_AGENT_IPS` 白名单内（审计页查 `ip_rejected`，见 §7.4）。

## 9. 升级与扩容

### 9.1 服务端升级

服务端无状态，直接换镜像滚动重启：

```bash
git pull && docker compose -f docker/docker-compose.prod.yml --env-file .env up -d --build
```

数据在 `kk-data` 卷（SQLite 于 `/data/kk-server.db`），不受重建影响；
新增列由启动时 `_ensure_schema` 自动 ALTER 迁移。

### 9.2 Agent 自更新

Agent 内置版本监控：发现新版本后凭源 IP 白名单从服务端下载二进制
（下载接口按请求真实源 IP 校验 `KK_AGENT_IPS`），
**强制校验 sha256** 一致后原子替换并 `execv` 自重启（监管脚本不会误杀）。
轮询拉齐仅在服务端 `KK_UPDATE_MODE=auto` 时生效——默认 `manual` 下
`/agent/latest` 恒答「无需升级」，升级走管理界面「版本与更新」页选机推送
（`POST /api/system/agent/upgrade`）。

发布新版本（管理员）：

```bash
cd agent && bash build/build_binary.sh
TOKEN=$(curl -s -X POST https://kk-server.ops.example.com/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<口令>"}' | jq -r .token)
curl -H "Authorization: Bearer $TOKEN" -F "version=0.2.0" \
     -F "file=@dist/kk-agent" \
     https://kk-server.ops.example.com/api/system/agent
```

`KK_UPDATE_MODE=auto` 时各主机 Agent 才按 `KK_UPDATE_INTERVAL` 轮询拉齐
（±20% 抖动）；默认 `manual` 模式下请在管理界面「版本与更新」页选机推送，
逐台结果在 `/api/system/updates` 台账核验。

**更新期间的可见性与健壮性（B6）**：

| 机制 | 行为 |
|---|---|
| 更新窗口可辨 | execv 前 Agent 发一帧 `reason=updating` 并等 PUBACK；主机列表的离线记录显示 `updating`，而非「原因未知」 |
| 轮询抖动 | `KK_UPDATE_INTERVAL` 上下浮动 ±20%（与心跳同理由），避免 500 台同相启动齐拉二进制 |
| 失败退避 | 同一版本失败后 5→10→30min 退避，不再重试刷屏；**版本号变化或成功即清零**（上传修好的包立刻生效） |
| 完整性 | 下载后先比清单 `size` 再比 `sha256`（截断更早暴露）；`.kk-agent.download.*` 下载残留由 wrapper 启动时清理 |
| execv 失败回滚 | 替换前旧二进制留 `.kkbak`，execv 失败自动还原并按 `exec_error` 回执，不会再「磁盘已是新二进制、下次重启变砖」（QR-A2） |
| 推送签名 | 推送路径默认要求签名，见下节（QR-P0-1） |

#### 9.2.1 推送更新签名策略（QR-P0-1，2026-09-29 起）

`kind=update` 命令等价于远程替换二进制，而 v3 的 Broker 默认匿名开放——任何能连上
Broker 的客户端都能直接向 `kk/v1/{host}/cmd` 发帧。因此 **Agent 的推送路径默认拒绝
未签名清单**（HTTP 轮询路径不受影响，行为不变）：

| 部署形态 | 需要的配置 | 行为 |
|---|---|---|
| 推荐（端到端防伪造） | 服务端与 Agent 都配 `KK_UPDATE_HMAC_KEY`（同一密钥） | 上传时服务端对二进制本体算 HMAC 写入清单 `sig`；推送帧与 `/agent/latest` 都携带，Agent 强制验签 |
| 内网可信（现状兼容） | Agent 显式配 `KK_UPDATE_ALLOW_UNSIGNED=1` | 放行未签名推送；其余防线（源 IP 白名单、sha256、黑名单、审计）不变 |
| 默认（未做任何配置） | — | 推送更新被拒绝，回执原因码 `unsigned_push_rejected` 进服务端更新台账，逐台可查 |

另外：`KK_UPDATE_DISABLED=1` 现在**同时关掉轮询与推送两条路**（此前推送可绕过该
开关）；Agent 侧按命令 id 去重，QoS1 重发不会重复执行。签名密钥丢失或换钥后，
需用新钥重新上传一版（sig 跟随清单走，回滚互换天然继承）。

#### 9.2.2 回滚待分发版本

上传新版本后发现问题，可把服务端**待分发**的二进制换回上一版：

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     https://kk-server.ops.example.com/api/system/agent/rollback
```

| 语义 | 说明 |
|---|---|
| 影响面 | **只换服务端待分发的二进制与版本清单** —— 只有「新上线 / 重启」的 Agent 会拿到旧版 |
| 已在跑的实例 | **不会**随之降级：`version_lt` 只升不降，桥接不会反向推送升级。要让它们回退，只能重启后重新走 `latest` 判定 |
| 可撤销 | 接口是「当前 ↔ 另一版」互换，再调一次即撤销回滚（被换下来的版本留作 `.rollback`） |
| 无上一版 | 返回 `404`，现有二进制不受影响（不会把正在分发的版本弄丢） |
| 想让全网退回旧代码 | **必须上传一个版本号更高**的包（如 `0.3.1`）用「升版本」实现「退代码」—— 这是 `version_lt` 防降级攻击的必然代价 |

> 常见误解：把本接口当成「一键回滚全网 500 台」。它做不到，也不该做到 ——
> 全网降级意味着攻击者拿下上传权限后即可把全网推回带漏洞的旧版。

### 9.3 多实例扩容

kk-server 可水平扩容，唯一硬性要求：**每实例 `KK_MQTT_CLIENT_ID` 唯一**
（MQTT 里 client_id 即会话标识，重复会被 Broker 判为互踢）。数据库换 PG/MySQL
（`KK_DB_URL`），前端流量经反代轮询分发。

## 10. 生产检查清单

- [ ] `KK_AGENT_IPS` 白名单已配置（只覆盖被管理主机网段；`KK_ENV=production` 自检通过）
- [ ] 已做过白名单负向验证：名单外主机上报被拒且审计留痕 `ip_rejected`（§8 第 6 步）
- [ ] 1883 端口已用防火墙 / 安全组限制可达范围（匿名 Broker 下唯一不可伪造的边界）
- [ ] `KK_ADMIN_PASS` 为强口令（`KK_ENV=production` 自检通过）
- [ ] 管理界面经 HTTPS 反代暴露；8443 未直接暴露公网
- [ ] 跨不可信网络的 MQTT 走 `mqtts://`
- [ ] 多实例（如有）`KK_MQTT_CLIENT_ID` 逐实例唯一
- [ ] `kk-data` 卷已纳入备份计划（SQLite 文件级备份）
- [ ] 已按 §8 完成五步验证（健康/登录/上线/命令/离线）

## 11. 日常运维

- **日志**：kk-server 与 Mosquitto 均打到容器 stdout，`docker logs -f` 或接入
  采集器；Agent 日志在主机容器内 `/var/log/kk-agent.log`（>1MB 自动轮转一份）。
- **存储治理**：服务端 janitor 每 5 分钟自动清理过期指标/命令/审计，无需人工收缩。
- **观测**：`GET /api/system/stats`（需管理员 token）返回主机/命令/存储/Broker
  四组统计；`GET /api/health` 适合探活。
- **审计**：登录、命令下发、黑名单拦截全量留痕，管理界面「审计日志」页可查。

## 12. 离线部署（内网无网场景）

内网目标机访问不到 Docker Hub / GHCR 的场景：用外网构建机把镜像打成 tar 包带入。
镜像清单、外网打包、内网加载的完整流程见 [docker/offline/README.md](../docker/offline/README.md)。

要点速览：
- 最小集合 = `kk-server_latest.tar.gz` + `kk-broker_latest.tar.gz` + Agent 产物 tar；
- 内网 compose 用 `docker/docker-compose.offline.yml`（与 `docker/docker-compose.prod.yml` 唯一差异：
  产物镜像只做本地 image 引用而非 build 段）；
- `docker/offline/manifest.txt` 是单一事实源，新增/删减镜像改这里 + `pack.sh`。

---

- 部署相关问题先查 [docker/mosquitto/README.md](../docker/mosquitto/README.md)（Broker 专题）
  与 [proto/messages.md](../proto/messages.md)（协议契约）。
- 本文档与代码冲突时，以代码与 `git` 历史为准。
