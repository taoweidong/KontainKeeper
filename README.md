# KontainKeeper

K8S 场景下 vscode-server 容器 IDE 的**直连管理与数据提取平台**。

- 不依赖 K8S 集群能力（无 kube-api / exec / service），不触碰宿主机
- Agent 随镜像内置（镜像制作期介入），容器启动后台自启，**用户无感知**
- Agent 主动出站 WebSocket 长连服务端 → 心跳上报指标 + 远程命令 + 自定义采集
- Agent 纯 Python 标准库（无 psutil），常驻内存目标 < 15MB
- 服务端 FastAPI + SQLite，内置管理界面，客户端 / 服务端完全分离

完整设计见 [docs/design.md](docs/design.md)，协议定义见 [proto/messages.md](proto/messages.md)。

## 仓库结构

```
pyproject.toml           UV 工作区虚拟根（package=false，members=[server, agent]）；.python-version 锁定 3.12
uv.lock                  UV 锁定的依赖版本（uv sync 生成，可复现安装）
agent/                   kk-agent 客户端（独立 UV 项目，纯 stdlib，可编译单文件二进制）
  src/kk_agent/          纯标准库源码；plugins/ 含示例采集插件
  tests/                 Agent 单元测试（采集/执行/插件/ws/updater，共 36 项）
  build/                 PyInstaller 编译单文件二进制的脚本
server/                  服务端（独立 UV 项目，FastAPI + 内置 Web 管理界面）
  src/kk_server/         服务端源码；web/ 静态界面随包一起打包
  tests/                 Server 单元测试 + 端到端集成测试（共 16 项）
  Dockerfile             生产镜像（python:3.12-slim，按 pyproject 装依赖）
proto/                   双端通信协议契约
scripts/                 构建与部署脚本
```

## 快速开始（本机体验全链路）

```bash
uv sync --all-packages          # 安装服务端依赖 + dev 组（agent 无第三方依赖）

# 1. 启动服务端（默认管理员 admin/admin，生产环境务必设 KK_ADMIN_PASS）
uv run kk-server
# 浏览器打开 http://127.0.0.1:8443 → 登录管理界面

# 2. 启动一个 Agent（模拟容器内，任意目录）
KK_SERVER=ws://127.0.0.1:8443/ws/agent KK_TOKEN=dev-token \
KK_POD_NAME=demo-pod KK_INTERVAL=5 uv run kk-agent

# 3. 管理界面：容器总览出现 demo-pod → 详情页/控制台下发命令 → 结果回传
```

## 部署到生产

### 1. 服务端（与 K8S 无关，部署在任何容器可达的机器上）

```bash
docker build -t registry.example.com/kontainkeeper-server:0.1.0 server/
docker run -d --name kontainkeeper \
  -p 8443:8443 \
  -v kontainkeeper-data:/data \
  -e KK_AGENT_TOKENS=<token> \
  -e KK_ADMIN_USER=admin \
  -e KK_ADMIN_PASS=<强密码> \
  registry.example.com/kontainkeeper-server:0.1.0
```

服务端只是一个普通的 WebSocket/HTTP 服务：裸机 systemd、`docker run`、任意 PaaS 均可，唯一要求是**目标容器能出站访问到它**（仅容器 → 服务端方向，容器无需暴露任何端口）。

环境变量：

| 变量 | 说明 | 默认 |
|---|---|---|
| `KK_AGENT_TOKENS` | 逗号分隔的 Agent 接入 token | `dev-token` |
| `KK_ADMIN_USER` / `KK_ADMIN_PASS` | 管理员账号 | `admin`/`admin` |
| `KK_DB_PATH` | SQLite 路径 | `kk-server.db` |
| `KK_CMD_BLACKLIST` | 命令黑名单（逗号分隔子串） | `rm -rf /,mkfs,reboot,...` |
| `KK_ENFORCED_INTERVAL` | 强制所有 Agent 的心跳间隔（秒） | 不强制 |
| `KK_AGENT_BIN_DIR` | 上传的 Agent 二进制存放目录 | `agent_assets` |

### 2. 容器侧（镜像制作期介入）

在 vscode-server 镜像 CI 中调用构建脚本，叠加 Agent 并注入接入配置：

```bash
BASE_IMAGE=myregistry/vscode-server:1.2 \
KK_SERVER=wss://kk-server.ops:8443/ws/agent \
KK_TOKEN=<与 KK_AGENT_TOKENS 一致> \
  ./scripts/build.sh myregistry/vscode-server-managed:1.2
```

前提：目标镜像**无需内置 Python**——Agent 已编译为单文件二进制（运行时零依赖）随镜像分发；`agent/src/kk_agent/` 下仍保留纯标准库源码，用于开发、重建二进制与本地调试。

产物镜像用 entrypoint-wrapper 以「独立服务」方式常驻监管 Agent 二进制（崩溃自动拉起、自更新后 `execv` 复用 PID 不误重启），并可选地并行拉起原 vscode-server 入口（`KK_ORIG_ENTRYPOINT`）——两者生命周期解耦，用户看到的 IDE 启动行为不变。

### 3. Agent 环境变量（镜像内已注入，一般无需改动）

| 变量 | 说明 | 默认 |
|---|---|---|
| `KK_SERVER` | 服务端 WebSocket 地址 | 必填 |
| `KK_TOKEN` | 接入 token | 必填 |
| `KK_INTERVAL` | 心跳/采集间隔（秒） | `60` |
| `KK_DISK_PATHS` | 采集的挂载点 | `/,/workspace` |
| `KK_PLUGIN_DIR` | 自定义采集插件目录 | `/opt/kk-agent/plugins` |
| `KK_AGENT_BIN` | 自更新替换目标（运行中的二进制路径） | `sys.executable` |
| `KK_UPDATE_URL` | 管理 API 基址（缺省由 `KK_SERVER` 的 ws/wss 推导） | 自动推导 |
| `KK_UPDATE_INTERVAL` | 版本轮询间隔（秒，≥30） | `300` |
| `KK_UPDATE_DISABLED` | 设为 `1/true` 关闭自更新 | 关闭 |
| `KK_UPDATE_INSECURE` | 设为 `1/true` 关闭 TLS 校验（不推荐） | 关闭 |

### Agent 自更新（零人工干预）

Agent 作为独立服务运行，内置版本监控：连上服务端时若版本落后会立即收到 `upgrade` 帧，
运行中亦按 `KK_UPDATE_INTERVAL` 定时轮询。发现新版本后，Agent 用自身 token 下载二进制，
校验 `sha256` 一致后原子替换自身文件并 `execv` 自重启——**全程无需人工执行更新命令**。

发布新版本（管理员）：

```bash
# 先编译新二进制
cd agent && ./build/build_binary.sh
# 以管理员会话上传（服务端计算 sha256 并记录为最新）
curl -H "Authorization: Bearer <ADMIN_TOKEN>" -F "version=0.2.0" \
     -F "file=@dist/kk-agent" https://kk-server.ops:8443/api/system/agent
```

此后所有在线及后续上线的 Agent 会自动升级到 `0.2.0`。详见 `proto/messages.md`。

## 自定义采集插件

在目标镜像里往 `/opt/kk-agent/plugins/` 放任意 `*.py`（实现 `collect() -> dict`），随心跳自动上报，mtime 变化即热加载，无需重启容器：

```python
def collect():
    return {"extension_count": 42}
```

也可在管理界面「命令控制台」对勾选的容器下发 `plugin_reload` 立即采集回传。

## 管理界面

- **容器总览**：在线状态、CPU/内存/磁盘、磁盘超 85% 告警、自定义采集数据
- **容器详情**：24 小时 CPU/内存趋势、用户会话（含 vscode-server 标记）、Top 进程、快速命令
- **命令控制台**：批量勾选容器下发命令（argv 直传不经 shell），离线容器命令排队补发
- **审计日志**：登录、命令下发、黑名单拦截全量留痕

## 测试

```bash
uv sync --all-packages
uv run pytest agent/tests -v     # Agent 单元测试（36 项）
uv run pytest server/tests -v    # Server 单元测试 + 端到端集成（16 项）
```

两个项目各持独立单测：`agent/tests`（WebSocket 帧编解码、/proc 采集（伪造 proc 树，跨平台可跑）、命令执行、插件热加载、自更新）与 `server/tests`（存储层、Hub 生命周期、Agent 自更新接口、以及**真实服务端 + 真实 Agent 主循环**的端到端集成测试）。

## 安全说明

- Agent → 服务端：WSS + 构建期注入 token（镜像内嵌 token，需控制镜像仓库权限；轮换需重新构建）
- 管理端：会话 token（默认 12 小时过期），命令下发全量审计
- 命令黑名单拦截危险操作；argv 数组直传 exec，不经容器内 shell 拼接
