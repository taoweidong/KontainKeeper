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
| 出网 | 管理侧构建镜像时需访问 PyPI（uv 安装依赖）；运行期不需要 |

## 2. 获取代码

```bash
git clone https://github.com/taoweidong/KontainKeeper.git
cd KontainKeeper
```

## 3. 部署 MQTT Broker（Mosquitto）

推荐直接使用仓库自带的生产栈（`docker-compose.prod.yml` 里的 `mosquitto` 服务，
Broker 与 kk-server 一起起）。若 Broker 必须独立部署，参照
[deploy/mosquitto/README.md](../deploy/mosquitto/README.md) 用同一套配置文件单独起。

### 3.1 配置要点（`deploy/mosquitto/mosquitto.conf`）

- **匿名开放**（`allow_anonymous true`）：Agent 零凭据接入，无需生成
  passwordfile / aclfile / 逐主机账号；
- `persistence true`：status retain 帧落盘，Broker 重启后在线视图立即可用；
- 接入管控由 kk-server 的 `KK_AGENT_IPS` 白名单承担（见 §4.1），不在 Broker 做；
- **网络层隔离**：匿名 Broker 下唯一不可伪造的边界，建议用防火墙 / 安全组
  限制 1883 端口只对服务端与被管理主机网段开放。

### 3.2 验证 Broker（§4 栈启动后执行）

匿名即可正常订阅（挂起等待消息，Ctrl-C 退出即可）：

```bash
docker compose -f docker-compose.prod.yml run --rm mosquitto \
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

> 白名单是 v3 的接入管控手段：只有名单内 IP 的 Agent 才能上报数据，
> 其余全部拒绝并审计。`KK_ENV=production` 时未配置会直接拒绝启动。

**白名单工作机制（v3，替代 token 认证）**：

1. **自报**：Agent 连 Broker 时自动探测「到 Broker 方向的出口 IP」（UDP connect），
   写入每条上行帧的 `ip` 字段——MQTT 经 Broker 中转拿不到发布者真实 TCP 源 IP，
   白名单基于自报值，适合内网可信环境；
2. **校验**：kk-server 在消息入口按 `KK_AGENT_IPS` 逐一匹配（`ipaddress` 网段语义），
   命中任一网段即放行，全部未命中则丢弃该帧并记 WARNING 日志
   （`拒绝白名单外上报：host=xxx ip='x.x.x.x'`），同时在审计页留下 `ip_rejected` 记录；
3. **语法**：逗号分隔的 **精确 IP 与 CIDR 网段混合列表**，单个 IP 自动按 `/32`
   （IPv6 按 `/128`）处理；格式写错（如笔误的网段）**启动即失败**，好过静默全拒。

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
docker compose -f docker-compose.prod.yml --env-file .env up -d --build
```

生产栈自动带上 `KK_ENV=production`，启动时做安全自检（见 4.4）。

**第三步：确认**：

```bash
docker compose -f docker-compose.prod.yml ps        # 两个服务 running
curl http://127.0.0.1:8443/api/health               # {"ok":true,"broker":"connected",...}
```

`broker: connected` 表示 kk-server 已连上 Mosquitto。

### 4.2 方式 B：手动 docker build / run

镜像构建上下文是**仓库根**（uv workspace，锁在根 `uv.lock`）：

```bash
docker build -f server/Dockerfile -t registry.example.com/kontainkeeper-server:0.1.0 .
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

`config.py` 在 `KK_ENV=production` 时会**直接拒绝启动**以下情况：

- 管理口令仍是默认 `admin123`；
- `KK_AGENT_IPS` 未配置（白名单为空 = 放行所有上报）。

自检不过会抛 `RuntimeError` 并退出，按提示修正后重启。

### 4.5 服务端环境变量参考

| 变量 | 说明 | 默认 |
|---|---|---|
| `KK_MQTT_URL` | Broker 地址（`mqtt://` / `mqtts://`） | 必填 |
| `KK_AGENT_IPS` | Agent 接入白名单（逗号分隔 IP / CIDR；**生产必配**，空 = 放行全部） | 空 |
| `KK_MQTT_CLIENT_ID` | 实例唯一 client_id；**多实例必须逐实例唯一**，否则被 Broker 互踢 | `kk-server` |
| `KK_MQTT_KEEPALIVE` | MQTT 保活秒数（下限 10） | `60` |
| `KK_MQTT_USERNAME` / `KK_MQTT_PASSWORD` | 服务端连 Broker 的凭据（Broker 匿名时留空） | 空 |
| `KK_MQTT_TLS_CA` / `KK_MQTT_TLS_INSECURE` | Broker TLS 配置 | 空 |
| `KK_TOPIC_PREFIX` | 主题前缀（**双端同名，必须一致**） | `kk/v1` |
| `KK_DB_PATH` | SQLite 文件路径 | `kk-server.db` |
| `KK_DB_URL` | 选库：`sqlite+aiosqlite:///` / `postgresql+asyncpg://` / `mysql+aiomysql://`，配了优先于 `KK_DB_PATH` | SQLite |
| `KK_ADMIN_USER` / `KK_ADMIN_PASS` | 管理员账号 | `admin` / `admin123` |
| `KK_CMD_BLACKLIST` | 命令黑名单（逗号分隔子串，拦危险命令） | `rm -rf /,mkfs,...` |
| `KK_HOST` / `KK_PORT` | 监听地址 / 端口 | `0.0.0.0` / `8443` |
| `KK_ENV` | 设 `production` 启用启动安全自检 | 空 |
| `KK_WEB_DIR` | 前端静态目录 | 包内 `web/` |
| `KK_ENFORCED_INTERVAL` | 强制 Agent 心跳间隔（秒，可选） | 不强制 |

### 4.6 数据库选型

- **SQLite**（默认）：单文件、零运维，适合 ≤ 数百台规模；数据卷挂 `/data` 即可。
- **PostgreSQL / MySQL**：设置 `KK_DB_URL`，并在安装时带对应 extras
  （`--extra postgres` / `--extra mysql`）。三库方言差异已收在代码
  `Store._upsert` / `_ensure_schema` 两处；但注意 PG/MySQL 目前只做过方言编译校验，
  **未连真实库跑过测试**，上线前请先在预发环境验证。

## 5. 前端（管理界面）

**默认无需任何动作**：前端构建产物已随包提交在 `server/src/kk_server/web/`，
kk-server 启动时自动托管（`KK_WEB_DIR` 默认即该目录），浏览器访问 8443 端口即可。

仅当**修改了 `web/` 前端代码**后才需要重建：

```bash
cd web
pnpm install && pnpm build          # 产物输出 web/dist/
# 人工同步产物（web/dist 被 .gitignore 忽略，不会自动进包）
rm -rf ../server/src/kk_server/web/* && cp -r dist/* ../server/src/kk_server/web/
cd .. && docker compose -f docker-compose.prod.yml --env-file .env up -d --build
```

前端构建要求 Node ≥ 20.19（或 ≥ 22.13）、pnpm ≥ 9，且 `web/mock/` 目录存在（空目录即可）。

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
}
```

   随后把 8443 从公网暴露中收回，只允许反代访问。

2. **Agent ↔ Broker 的 MQTT 连接**：跨不可信网络必须 `mqtts://`（配
   `KK_MQTT_TLS_CA`，Agent 侧对应 `KK_TLS_CA`）；Broker 与 Agent 同处内网可信区
   时可用 `mqtt://`。

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
  ./scripts/build.sh myregistry/vscode-server-managed:1.2
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
cd agent && ./build/build_binary.sh          # 产出 dist/kk-agent（Linux 单文件，约 8–12MB）
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

**第三步（推荐）：systemd 常驻**，开机自启 + 崩溃自动拉起：

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
| `KK_TOPIC_PREFIX` | 主题前缀，须与服务端一致 | `kk/v1` |
| `KK_INTERVAL` | 心跳/采集间隔（秒，下限 1） | `60` |
| `KK_DISK_PATHS` | 采集挂载点（逗号分隔；空 = 自动发现全部物理挂载点） | 自动发现 |
| `KK_HB_ITEMS` | 心跳采集项（白名单子集；千进程主机可去掉 `proc` 降负载） | 全部 8 项 |
| `KK_PLUGIN_DIR` | 自定义采集插件目录 | `/opt/kk-agent/plugins` |
| `KK_PLUGIN_TIMEOUT` | 插件 `collect()` 超时（秒），超时隔离至重载 | `5` |
| `KK_ALLOW_SHELL` | 允许 `use_shell` 管道模式 | `1` |
| `KK_MAX_OUT_MB` | 单命令输出上限 | `4` |
| `KK_MAX_QUEUED` | 离线 out-queue 上限 | `512` |
| `KK_UPDATE_URL` | 管理 API 基址（自更新用，未配则跳过自更新） | 空 |
| `KK_UPDATE_INTERVAL` | 版本轮询间隔（秒，≥30） | `300` |
| `KK_UPDATE_DISABLED` | 设 `1/true` 关闭自更新 | 关闭 |
| `KK_AGENT_BIN` | 自更新替换目标路径（build.sh 已烧入） | 自动 |

## 8. 验证部署（第一次上线照做）

1. **服务端健康**：`curl https://kk-server.ops.example.com/api/health` 返回
   `ok:true`、`broker:"connected"`。
2. **登录管理界面**：浏览器打开 `https://kk-server.ops.example.com`，
   用 `KK_ADMIN_USER` / `KK_ADMIN_PASS` 登录。
   注意：同一用户名连续输错 5 次口令会锁定 300 秒（防爆破）。
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
git pull && docker compose -f docker-compose.prod.yml --env-file .env up -d --build
```

数据在 `kk-data` 卷（SQLite 于 `/data/kk-server.db`），不受重建影响；
新增列由启动时 `_ensure_schema` 自动 ALTER 迁移。

### 9.2 Agent 自更新（零人工干预）

Agent 内置版本监控：发现新版本后凭源 IP 白名单从服务端下载二进制
（下载接口按请求真实源 IP 校验 `KK_AGENT_IPS`），
**强制校验 sha256** 一致后原子替换并 `execv` 自重启（监管脚本不会误杀）。

发布新版本（管理员）：

```bash
cd agent && ./build/build_binary.sh
TOKEN=$(curl -s -X POST https://kk-server.ops.example.com/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<口令>"}' | jq -r .token)
curl -H "Authorization: Bearer $TOKEN" -F "version=0.2.0" \
     -F "file=@dist/kk-agent" \
     https://kk-server.ops.example.com/api/system/agent
```

各主机 Agent 按 `KK_UPDATE_INTERVAL` 轮询拉齐；镜像内已预配自更新地址时可全自动。

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

---

- 部署相关问题先查 [deploy/mosquitto/README.md](../deploy/mosquitto/README.md)（Broker 专题）
  与 [proto/messages.md](../proto/messages.md)（协议契约）。
- 本文档与代码冲突时，以代码与 `git` 历史为准。
