# kk-agent

KontainKeeper 的**容器端 Agent**（客户端）。纯 Python 标准库实现，**零第三方依赖**，常驻在 vscode-server 容器内，主动出站 WebSocket 连接管理服务端，负责：

- 周期采集心跳指标（CPU / 内存 / 磁盘 / 进程 / 用户会话）并上报；
- 接收服务端下发的远程命令并回传结果；
- 上报 `plugins/` 下的自定义采集数据；
- 二进制形态下支持**自更新**（拉取版本清单 → 下载 → SHA256 校验 → `os.execv` 自重启）。

设计目标：空闲时 0 CPU、常驻 RSS < 15MB，对容器用户无感知，不触碰宿主机、不使用 K8S 集群能力。

## 架构

Agent 采用**单线程 `select` 事件循环 + 一次性 daemon 工作线程**模型：

```
                 ┌─────────────────────────────┐
   服务端推送 ──▶ │  main.run() 主循环（单线程）  │
                 │  · select 阻塞等 socket 可读  │
                 │  · 退避重连（1s→60s 封顶）     │
                 │  · 心跳 / 自更新定时          │
                 │  · 统一收帧、统一发帧          │◀── 只允许主线程碰 socket
                 └───────────┬─────────────────┘
                             │ queue.Queue
                 ┌───────────▼─────────────────┐
                 │  一次性 daemon 工作线程        │
                 │  · collector 采集心跳指标      │
                 │  · executor 执行命令           │
                 │  · plugin_loader 采集插件      │
                 │  结果投递回队列，不碰 socket    │
                 └─────────────────────────────┘
```

**模块划分（`src/kk_agent/`）**

| 模块 | 职责 |
|---|---|
| `config.py` | 全部配置来自 `KK_*` 环境变量（`load()` 解析） |
| `__main__.py` | 入口，`python -m kk_agent` 调用 `main.run()` |
| `main.py` | 守护主循环：连接管理、退避重连、心跳/更新调度、消息分发 |
| `conn.py` | 连接建立、hello 帧构造、JSON 发送、安全关闭 |
| `ws.py` | WebSocket 客户端与帧编解码（纯函数，跨平台可单测） |
| `collector.py` | `/proc` + `/etc` 采集（经 `KK_FS_ROOT` 注入，Windows 亦可跑） |
| `executor.py` | 命令执行：exec 数组（**不经 shell**）、限时、输出封顶 |
| `plugin_loader.py` | 热加载 `plugins/` 下 `*.py`（按 mtime 比较，失败隔离） |
| `updater.py` | 自更新：拉清单 / 下载 / SHA256 校验 / 原子替换 / `os.execv` |
| `logutil.py` | 日志工具（可输出到文件或 stdout） |
| `plugins/` | 自定义采集插件目录（`plugins/README.md` 说明写法） |

## 运行形态

### 1. 源码形态（开发 / 调试）

```bash
cd agent
uv run kk-agent                       # 等价 python -m kk_agent，吸收 KK_* 环境变量
# 或显式指定：
KK_SERVER=ws://127.0.0.1:8443/ws/agent KK_TOKEN=dev-token \
KK_POD_NAME=demo-pod KK_INTERVAL=5 uv run kk-agent
```

### 2. 二进制形态（生产，嵌入容器）

用 PyInstaller 编译为单文件二进制，再由监管脚本常驻拉起：

```bash
cd agent
./build/build_binary.sh              # 产出 dist/kk-agent（或 .exe）
```

`deploy/entrypoint-wrapper.sh` 作为容器 PID1 形态的**独立监管脚本**：
- 后台常驻监管 `kk-agent` 二进制，崩溃 5s 后自动拉起；
- 自更新时 Agent 内部 `os.execv` 复用同一 PID，监管循环不会误判为退出；
- 前台 `exec "$@"` 透传原 vscode-server 入口（参数由 Docker 以 ENTRYPOINT 数组
  形式注入，不经 shell/JSON 解析），容器生命周期 = 原 IDE 生命周期；
- 无入口参数（基础镜像无 ENTRYPOINT/CMD）时退化为纯监管模式。

> 二进制形态下自更新才会真正替换自身；源码形态（`python -m`）只下载校验、不 `execv`，避免误改解释器。

## 环境变量（全部 `KK_*` 前缀）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `KK_SERVER` | 服务端 WebSocket 地址（建议 `wss://`） | 必填 |
| `KK_TOKEN` | Agent 鉴权 token（须服务端 `KK_AGENT_TOKENS` 之一） | 必填 |
| `KK_INTERVAL` | 心跳间隔（秒，下限 1） | `60` |
| `KK_DISK_PATHS` | 采集的磁盘挂载点，逗号分隔 | `/,/workspace` |
| `KK_PLUGIN_DIR` | 自定义采集插件目录 | `<包>/plugins` |
| `KK_FS_ROOT` | `/proc`、`/etc` 等采集根（测试可指向伪造树） | `/` |
| `KK_LOG` | 日志文件路径，留空输出到 stdout | `""` |
| `KK_LOG_LEVEL` | 日志级别 | `INFO` |
| `KK_IMAGE` | 上报的镜像名（可选） | `""` |
| `KK_POD_NAME` | 上报的容器/Pod 名（默认主机名） | 主机名 |
| `KK_MAX_OUT_MB` | 单条命令输出上限（MB） | `4` |
| `KK_UPDATE_URL` | 自更新地址（覆盖从 hello 推断的 base） | `""` |
| `KK_UPDATE_INTERVAL` | 自更新检查间隔（秒，下限 30） | `300` |
| `KK_UPDATE_DISABLED` | 置 `1/true/yes/on` 关闭自更新 | `""` |
| `KK_UPDATE_INSECURE` | 置 `1/...` 关闭证书校验（**不推荐**） | `""` |
| `KK_AGENT_BIN` | 自更新时替换的二进制路径 | 当前可执行文件 |

## 部署

完整构建见 `scripts/build.sh`：先编译二进制 → 再在原 vscode-server 镜像基础上叠加二进制 + 插件 + 监管脚本，上线即连。

手工叠加参考片段：`deploy/Dockerfile.snippet`（需提供 `dist/kk-agent`、`src/kk_agent/plugins/`、`agent/deploy/entrypoint-wrapper.sh`）。

## 测试

```bash
cd agent
uv run pytest tests -q          # 38 项：采集 / 执行 / 插件 / 自更新 / ws 帧编解码
```

## 约束（重要）

- **禁止任何第三方 `import`**（updater 同样零依赖），保持常驻轻量。
- **WebSocket socket 只允许主线程触碰**：工作（采集/命令/插件）必须在一次性 daemon 线程跑，结果经队列回主线程发帧。
- 协议版本 `PROTO_VER` 见 `config.py`，与服务端必须一致（见 `proto/messages.md`）。
