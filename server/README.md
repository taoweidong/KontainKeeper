# kk-server

KontainKeeper 的**管理服务端**。基于 FastAPI，通过 **MqttBridge 无状态桥接** MQTT Broker，提供 REST API 与多数据库存储，并托管 Vue3 前端构建产物（随包一起打包）。

- 订阅 Broker 上的主机心跳/状态/结果，落库并维护在线主机列表与指标；
- 向 Broker 发布命令（含按项采集），离线期间由 Broker 排队、主机重连自动补投；
- 提供命令下发、token 管理、审计、Agent 二进制自更新、系统可观测接口。

服务端**不持有长连接状态**，因此可水平扩容：多实例时 `KK_MQTT_CLIENT_ID` 必须逐实例唯一。

## 架构

```
                       ┌──────────────────────────────┐
   浏览器 ──HTTP──▶     │  Vue3 前端 (web/ 构建产物)     │
                       │  index.html + static/         │
                       └───────────────┬──────────────┘
                                       │ 调 REST
                       ┌───────────────▼──────────────┐
                       │  FastAPI App (main.create_app)│
                       │  · 静态界面挂载               │
                       │  · lifespan：桥接 + janitor    │
                       └───────┬───────────────┬──────┘
              ┌────────────────▼────┐   ┌──────▼──────────────┐
              │ services/mqtt_bridge│   │ controllers/ (REST)  │
              │ 订阅 hb/status/result│   │ auth/containers/     │
              │ 发布 cmd             │   │ commands/tokens/     │
              │ 超时清扫 / 僵尸下线    │   │ audit/stats/         │
              │ 版本升级推送          │   │ agent_update         │
              └────────┬───────────┘   └─────────┬───────────┘
                       │  订阅/发布                │
              ┌────────▼──────────────────────────▼─────────┐
              │          Mosquitto 2.x (MQTT Broker)         │
              └─────────────────────────────────────────────┘
                       │
              ┌────────▼────────────┐
              │ models/store.py      │
              │ SQLAlchemy 2 Core    │
              │ async（SQLite/PG/MySQL）│
              └─────────────────────┘
```

**模块划分（`src/kk_server/`）**

| 模块 | 职责 |
|---|---|
| `__init__.py` | 包元信息（`__version__`、`PROTO_VER = 2`） |
| `__main__.py` / `main.py` | 入口与 `create_app()` 工厂（**无模块级 `app`**） |
| `config.py` | `KK_*` 环境变量解析；`COLLECT_ITEMS` 采集项白名单 |
| `services/mqtt_bridge.py` | MqttBridge：无状态桥接（替代已删的 `hub.py`） |
| `services/security.py` | 命令黑名单校验（安全红线） |
| `models/store.py` | 异步存储层（三库通用）+ 聚合 + 回收 + 摘要视图 |
| `models/tables.py` | 表定义 + `_ADD_COLUMNS` 迁移清单 |
| `controllers/` | REST 路由（见下表） |
| `web/` | Vue3 构建产物（随 wheel 打包，由服务端直接托管） |

> 服务端入口是**工厂** `create_app(env)`，运行走 `python -m kk_server`（`uv run kk-server`）。
> 测试用 `uvicorn.Server` 线程 + `create_app(env)`，或用 `httpx.AsyncClient` + `ASGITransport`。

## 运行

```bash
uv sync --all-packages                 # 仓库根执行，统一安装全部成员
# 按需装其他库驱动：uv sync --extra postgres / --extra mysql

# 启动（默认管理员 admin/admin，生产务必设 KK_ADMIN_PASS）
.venv/Scripts/python.exe -m kk_server   # 或 uv run kk-server
# 浏览器打开 http://localhost:8443 → 登录管理界面
```

默认监听 `0.0.0.0:8443`。需要有一个可达的 MQTT Broker（`KK_MQTT_URL`）。

## 环境变量（全部 `KK_*` 前缀）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `KK_HOST` | 监听地址 | `0.0.0.0` |
| `KK_PORT` | 监听端口 | `8443` |
| `KK_MQTT_URL` | Broker 地址（`mqtt://` / `mqtts://`） | 必填 |
| `KK_MQTT_USERNAME` / `KK_MQTT_PASSWORD` | Broker 鉴权（生产必须） | 空 |
| `KK_MQTT_CLIENT_ID` | 实例唯一 client_id | `kk-server` |
| `KK_MQTT_KEEPALIVE` | 保活（秒，下限 10） | `60` |
| `KK_MQTT_TLS_CA` / `KK_MQTT_TLS_INSECURE` | TLS 配置 | 空 |
| `KK_TOPIC_PREFIX` | 主题前缀（**双端同名，与 Agent 一致**） | `kk/v1` |
| `KK_DB_URL` | `sqlite:///...` / `postgresql://...` / `mysql://...` | SQLite |
| `KK_DB_PATH` | SQLite 路径（`KK_DB_URL` 未设时生效） | `kk-server.db` |
| `KK_AGENT_TOKENS` | 允许的 Agent token，逗号分隔 | `dev-token` |
| `KK_ADMIN_USER` / `KK_ADMIN_PASS` | 管理员账号 | `admin`/`admin` |
| `KK_CMD_BLACKLIST` | 命令黑名单（逗号分隔，命中拒绝并审计） | `rm -rf /,mkfs,reboot,...` |
| `KK_ENFORCED_INTERVAL` | 强制心跳间隔（秒），留空不强制 | `""` |
| `KK_AGENT_BIN_DIR` | Agent 二进制存储目录 | `agent_assets` |
| `KK_WEB_DIR` | 前端静态目录（缺省用包内 `web/`） | `<包>/web` |
| `KK_LOG_LEVEL` | 日志级别 | `info` |
| `KK_ENV` | 置 `production` 时启用安全熔断：仍用默认口令 `admin` 会拒绝启动 | `""` |

## REST API 总览

所有接口前缀 `/api`。

| 分组 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 健康 | GET | `/api/health` | 健康检查 |
| 认证 | POST | `/api/login` | 管理员登录，返回 token |
| 认证 | POST | `/api/logout` | 注销 |
| 认证 | GET | `/api/me` | 当前管理员信息 |
| 主机 | GET | `/api/containers?view=summary\|full` | 主机列表（**summary 只读摘要列**，500 台场景用） |
| 主机 | GET | `/api/containers/{pod}` | 主机详情 |
| 主机 | GET | `/api/containers/{pod}/metrics?hours=` | 指标趋势（原始/小时聚合） |
| 命令 | GET | `/api/collect/items` | 可采集项白名单（8 项） |
| 命令 | POST | `/api/commands` | 下发命令（`kind=shell/collect/plugin_reload/update`） |
| 命令 | GET | `/api/commands` | 命令列表 |
| 命令 | GET | `/api/commands/{cid}` | 命令详情与结果 |
| 命令 | GET | `/api/commands/{cid}/out` | 完整输出（`?format=text\|base64`） |
| Token | GET | `/api/tokens` | Agent token 列表（脱敏） |
| Token | POST | `/api/tokens/revoke` | 吊销 token |
| Token | POST | `/api/tokens/restore` | 恢复 token |
| 审计 | GET | `/api/audit?limit=` | 审计日志 |
| 可观测 | GET | `/api/system/stats` | 主机/命令/存储/Broker 统计与 uptime |
| 自更新 | POST | `/api/system/agent` | 管理员上传新版本二进制（附 version） |
| 自更新 | GET | `/api/system/agent/latest?ver=` | 查询是否有可用更新（SHA256/size/url） |
| 自更新 | GET | `/api/system/agent/download` | 下载最新二进制 |

双端通信主题布局与帧格式见 [`proto/messages.md`](../../proto/messages.md)（协议 v2）。
v1 的 WebSocket close code（`4401/4402/4403/4404`）已随 `/ws/agent` 一并删除，
改用 MQTT 连接返回码 + `status` 帧内 `token`/`proto_ver` 校验。

## 存储治理

- **摘要视图**：`?view=summary` 只读 `pod/image/agent_ver/hb_interval/online/last_seen/cpu/mem_mb/disk_pct`，
  避免 500 台逐行解析 `last_metrics`。
- **自动补列**：新增列登记 `models/tables.py::_ADD_COLUMNS`，`setup()` 自动 ALTER，既有库零手工迁移。
- **回收**：原始心跳 2 天聚合 → hourly 保留 90 天；命令状态行 30 天；
  命令输出单独 7 天清文本（保留状态行、`out_purged=1`）；大表按主键分批删。

## 部署

生产镜像见 `server/Dockerfile`：

```bash
cd server
docker build -t kk-server:0.1.0 .
docker run -d -p 8443:8443 \
  -e KK_MQTT_URL=mqtt://broker:1883 \
  -e KK_ADMIN_PASS=<强密码> \
  -v kk-data:/data kk-server:0.1.0
```

> **TLS 建议**：管理端 Web 应前置 TLS 终结的反向代理；Agent ↔ Broker 生产应走 `mqtts://`
> 或置于内网可信区。另建议在反代层限制来源 IP。
>
> **跨库验证边界**：PostgreSQL / MySQL 目前只做了 DDL 与语句的跨方言编译校验，
> **未连过真实库**，上线前需补真库跑测。

## 测试

```bash
cd server
.venv/Scripts/python.exe -m pytest tests -q
# 66 passed, 3 skipped：存储层（含三库方言静态校验）/ 桥接路由与归属校验 /
# 黑名单 / 审计 / REST 接口契约（ASGI Transport）/ 端到端集成
```

集成测试（`tests/test_integration.py`）会启动真实 uvicorn + 真实 Agent 线程 + 真实 Broker，
验证「上线 → 心跳入库 → 批量 collect → 批量 shell → 结果回传 → LWT 置离线」全链路。
Broker 端口不可达时自动 skip，保证无 docker 环境仍全绿。
