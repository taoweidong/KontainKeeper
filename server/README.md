# kk-server

KontainKeeper 的**管理服务端**。基于 FastAPI，提供 Agent 连接中枢（WebSocket）、REST API 与 SQLite 存储，并内置一个无框架的 Web 管理界面（随包一起打包）。

- 接收容器内 Agent 主动出站的连接与心跳，维护在线容器列表与指标；
- 提供命令下发、token 管理、审计、Agent 二进制自更新接口；
- 管理界面可直接查看容器总览/详情、下发命令、查看审计。

## 架构

```
                         ┌──────────────────────────────┐
   浏览器 ──HTTP──▶      │  内置 Web 管理界面 (web/)       │
                         │  index.html / app.js / style   │
                         └───────────────┬──────────────┘
                                         │ 调 REST
                         ┌───────────────▼──────────────┐
   容器 Agent ──WS──▶    │  FastAPI App (main.create_app) │
   /ws/agent             │  · 静态界面挂载                │
                         │  · 清理线程（周期聚合/清理）    │
                         └───────┬───────────────┬──────┘
                  ┌──────────────▼──────┐   ┌─────▼──────────────┐
                  │  hub.py (连接中枢)   │   │  api/ (REST 路由)   │
                  │  注册/鉴权/心跳入库   │   │  auth/containers/   │
                  │  命令路由/离线补发    │   │  commands/tokens/   │
                  │  版本升级推送         │   │  audit/agent_update │
                  └─────────┬──────────┘   └─────────┬──────────┘
                            └──────────┬─────────────┘
                               ┌───────▼────────┐
                               │  store.py       │
                               │  SQLite 存储层   │
                               └────────────────┘
```

**模块划分（`src/kk_server/`）**

| 模块 | 职责 |
|---|---|
| `__init__.py` | 包元信息（`__version__`、`PROTO_VER`） |
| `__main__.py` / `main.py` | 入口与 `create_app()` 工厂（**无模块级 `app`**） |
| `hub.py` | Agent WebSocket 连接中枢（`/ws/agent`）：注册、鉴权、心跳入库、命令路由、离线补发、落后版本升级推送 |
| `store.py` | SQLite 存储层：容器、心跳、命令、token、审计 |
| `api/` | REST 路由（见下表） |
| `web/` | 管理界面静态单页（随 wheel 打包，由服务端直接托管） |
| `version.py` | 版本比较工具（`version_lt`） |

> 服务端入口是**工厂** `create_app(env)`，运行走 `uv run kk-server`（即 `python -m kk_server`）。测试用 `uvicorn.Server` 线程 + `create_app(env)` 起实例。

## 运行

```bash
cd server
uv sync --all-packages                 # 安装依赖（也可在仓库根执行，统一安装全部成员）

# 启动（默认管理员 admin/admin，生产务必设 KK_ADMIN_PASS）
uv run kk-server
# 浏览器打开 http://localhost:8443 → 登录管理界面
```

默认监听 `0.0.0.0:8443`。

## 环境变量（全部 `KK_*` 前缀）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `KK_HOST` | 监听地址 | `0.0.0.0` |
| `KK_PORT` | 监听端口 | `8443` |
| `KK_DB_PATH` | SQLite 数据库路径（`:memory:` 用于测试） | `kk-server.db` |
| `KK_AGENT_TOKENS` | 允许的 Agent token，逗号分隔 | `dev-token` |
| `KK_ADMIN_USER` | 管理员用户名 | `admin` |
| `KK_ADMIN_PASS` | 管理员密码（默认 `admin` 会告警） | `admin` |
| `KK_CMD_BLACKLIST` | 命令黑名单（逗号分隔，命中拒绝并审计） | `rm -rf /,mkfs,reboot,...` |
| `KK_ENFORCED_INTERVAL` | 强制心跳间隔（秒，覆盖 Agent 上报），留空不强制 | `""` |
| `KK_AGENT_BIN_DIR` | Agent 二进制存储目录（自更新上传落盘处） | `agent_assets` |
| `KK_WEB_DIR` | 管理界面静态目录（缺省用包内 `web/`） | `<包>/web` |
| `KK_LOG_LEVEL` | 日志级别 | `info` |

## REST API 总览

所有接口前缀 `/api`；Agent 接入前缀 `/ws/agent` 与 `/api/system`。

| 分组 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 认证 | POST | `/api/login` | 管理员登录，返回 token |
| 认证 | POST | `/api/logout` | 注销 |
| 认证 | GET | `/api/me` | 当前管理员信息 |
| 容器 | GET | `/api/containers` | 容器总览（在线/指标/用户） |
| 容器 | GET | `/api/containers/{pod}` | 容器详情 |
| 容器 | GET | `/api/containers/{pod}/metrics?hours=` | 指标趋势（原始/小时聚合） |
| 命令 | POST | `/api/commands` | 下发命令（argv 直传或 cmdline） |
| 命令 | GET | `/api/commands` | 命令列表 |
| 命令 | GET | `/api/commands/{cid}` | 命令详情与结果 |
| Token | GET | `/api/tokens` | Agent token 列表（脱敏） |
| Token | POST | `/api/tokens/revoke` | 吊销 token |
| Token | POST | `/api/tokens/restore` | 恢复 token |
| 审计 | GET | `/api/audit?limit=` | 审计日志 |
| Agent 自更新 | POST | `/api/system/agent` | 管理员上传新版本二进制（附 version） |
| Agent 自更新 | GET | `/api/system/agent/latest?ver=` | 查询是否有可用更新（SHA256/size/url） |
| Agent 自更新 | GET | `/api/system/agent/download` | 下载最新二进制 |

双端通信帧格式与鉴权语义见 `proto/messages.md`（`4401` token / `4402` 协议版本 / `4403` 替换）。

## 部署

生产镜像见 `server/Dockerfile`（基于 `python:3.12-slim`，按 `pyproject.toml` 安装依赖，`CMD ["python","-m","kk_server"]`，数据卷 `/data`）：

```bash
cd server
docker build -t kk-server:0.1.0 .
docker run -d -p 8443:8443 -e KK_ADMIN_PASS=<强密码> -v kk-data:/data kk-server:0.1.0
```

> **TLS 硬约束（生产必须）**：服务端自身不终结 TLS（uvicorn 监听明文 HTTP/WS）。
> 生产部署必须**前置 TLS 终结的反向代理**（nginx / HAProxy / 云 LB，回环到
> `127.0.0.1:8443`），Agent 一律用 `wss://` 连接。切勿让 Agent 直接以 `ws://`
> 跨网络直连——`hello` 帧中的接入 token 会以明文传输。仅本机开发调试可用
> `ws://127.0.0.1`。另建议在反代层限制来源 IP（仅容器网段出站可达）。

## 测试

```bash
cd server
uv run pytest tests -q          # 17 项：存储层 / Hub 生命周期 / 自更新接口 / 端到端集成
```

集成测试（`tests/test_integration.py`）会启动真实 uvicorn + 真实 Agent 主循环，验证「连接 → 心跳入库 → 登录 → 下发命令 → 执行回传 → 插件重载 → 黑名单拒绝」全链路。
