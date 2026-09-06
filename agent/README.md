# kk-agent

KontainKeeper 的**主机端 Agent**（客户端）。常驻在目标主机（典型形态：vscode-server 容器内），主动出站 **MQTT** 连接 Broker，负责：

- 周期采集心跳指标（CPU / 内存 / 磁盘 / 磁盘 IO / 网络 / 进程 / 用户 / 系统）并上报；
- 接收服务端下发的远程命令并分块回传结果；
- 按项采集（8 个采集项，服务端可指定只采其中几项）；
- 上报 `plugins/` 下的自定义采集数据；
- 二进制形态下支持**自更新**（拉取版本清单 → 下载 → SHA256 校验 → `os.execv` 自重启）。

设计目标：空闲时 0 CPU、常驻 RSS **25–35MB**，对主机用户无感知，不触碰宿主机、不使用 K8S 集群能力。

> v1 曾要求「纯标准库、RSS < 15MB」。v2 改为**引入成熟依赖 + 单文件打包**：
> 采集交给 psutil（替代 233 行手工 `/proc` 解析），传输交给 paho-mqtt
> （替代 261 行自研 WebSocket）。内存涨约 10–20MB，换来跨平台采集与协议可靠性，
> 产物仍是零依赖的单文件二进制（8–12MB）。

## 架构

Agent 采用**单线程事件循环 + 一次性 daemon 工作线程**模型。MQTT socket 只由
paho 的后台网络线程触碰，业务结果统一回主线程：

```
                 ┌─────────────────────────────┐
  Broker 消息 ──▶ │  main.run() 主循环（单线程）  │
                 │  · 调度（monotonic 时钟）     │
                 │  · 心跳 / 自更新定时          │
                 │  · 统一收帧、统一发帧          │
                 └───────────┬─────────────────┘
                             │ queue.Queue
                 ┌───────────▼─────────────────┐
                 │  一次性 daemon 工作线程        │
                 │  · collector 采集心跳指标      │
                 │  · executor 执行命令           │
                 │  · plugin_loader 采集插件      │
                 │  结果投递回队列，不碰 socket    │
                 └─────────────────────────────┘
                             │
                 ┌───────────▼─────────────────┐
                 │  paho-mqtt 后台网络线程        │
                 │  · 连接 / 重连退避 / 保活      │
                 │  · QoS1 out-queue（离线排队）  │
                 └─────────────────────────────┘
```

**模块划分（`src/kk_agent/`）**

| 模块 | 职责 |
|---|---|
| `config.py` | 全部配置来自 `KK_*` 环境变量（`load()` 解析） |
| `__main__.py` | 入口，`python -m kk_agent` 调用 `main.run()` |
| `main.py` | 主循环：调度、心跳/更新定时、命令分发、结果分块回传 |
| `transport.py` | MQTT 传输（paho）：LWT、QoS 分级、重连、离线队列 |
| `collector.py` | psutil 采集，含 `collect_items()` 按项采集（8 项） |
| `executor.py` | 命令执行：exec 数组（**不经 shell**）、限时、输出封顶 |
| `plugin_loader.py` | 热加载 `plugins/` 下 `*.py`（按 mtime 比较，失败隔离） |
| `updater.py` | 自更新：拉清单 / 下载 / SHA256 校验 / 原子替换 / `os.execv` |
| `logutil.py` | 日志工具（可输出到文件或 stdout） |
| `plugins/` | 自定义采集插件目录（`plugins/README.md` 说明写法） |

> `ws.py` / `conn.py`（自研 WebSocket）已随协议 v2 删除。

## 主题与 QoS

主题前缀 `KK_TOPIC_PREFIX`（默认 `kk/v1`，与服务端同名配置必须一致）：

| 主题 | 方向 | QoS | Retain |
|---|---|---|---|
| `kk/v1/{host}/status` | A→S | 1 | **是**（兼作 LWT 遗嘱） |
| `kk/v1/{host}/hb` | A→S | 0 | 否 |
| `kk/v1/{host}/result` | A→S | 1 | 否 |
| `kk/v1/{host}/cmd` | S→A | 1 | 否 |

两条硬约束：

- **`hb` 绝不 retain**——retained 心跳会在服务端每次建订阅时整批回放，落库成幽灵数据点。
- **QoS1 发布不要做 `is_connected()` 预检再丢弃**——paho 会把消息投入 out-queue、
  重连自动重发，前置判断等于把离线排队能力自己短路掉。

帧格式详见 [`proto/messages.md`](../../proto/messages.md)。

## 运行形态

### 1. 源码形态（开发 / 调试）

```bash
cd agent
uv run kk-agent mqtt://127.0.0.1:1883      # 最简形态：位置参数 = 服务端地址（v3 零凭据）
# 或用环境变量：
KK_SERVER=mqtt://127.0.0.1:1883 \
KK_HOST_NAME=demo-host KK_INTERVAL=5 uv run kk-agent
```

### 2. 二进制形态（生产，嵌入镜像）

用 PyInstaller 编译为单文件二进制，再由监管脚本常驻拉起：

```bash
cd agent
./build/build_binary.sh              # 产出 dist/kk-agent（或 .exe）
```

`deploy/entrypoint-wrapper.sh` 作为容器 PID1 形态的**独立监管脚本**：
- 后台常驻监管 `kk-agent` 二进制，崩溃 5s 后自动拉起；
- 自更新时 Agent 内部 `os.execv` 复用同一 PID，监管循环不会误判为退出；
- 前台 `exec "$@"` 透传原入口（参数由 Docker 以 ENTRYPOINT 数组形式注入），
  容器生命周期 = 原应用生命周期；
- 无入口参数（基础镜像无 ENTRYPOINT/CMD）时退化为纯监管模式。

> 二进制形态下自更新才会真正替换自身；源码形态（`python -m`）只下载校验、不 `execv`，避免误改解释器。

## 环境变量（全部 `KK_*` 前缀）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `KK_SERVER` | Broker 地址（`mqtt://` / `mqtts://`，**不含凭据**；也可作位置参数传入） | 必填 |
| `KK_ADVERTISE_IP` | 自报出口 IP 覆盖（默认 UDP connect 自动探测；服务端按 `KK_AGENT_IPS` 白名单校验） | 自动探测 |
| `KK_HOST_NAME` | 主机标识（旧名 `KK_POD_NAME` 兼容） | 主机名 |
| `KK_TOPIC_PREFIX` | 主题前缀，与服务端一致 | `kk/v1` |
| `KK_KEEPALIVE` | MQTT 保活（秒，下限 10） | `60` |
| `KK_TLS_CA` / `KK_TLS_INSECURE` | TLS 配置 | 空 |
| `KK_CLIENT_ID` | MQTT client_id | `kk` |
| `KK_INTERVAL` | 心跳间隔（秒，下限 1） | `60` |
| `KK_DISK_PATHS` | 采集的磁盘挂载点，逗号分隔；空 = 自动发现全部物理挂载点 | `""` |
| `KK_HB_ITEMS` | 心跳采集项精简（逗号分隔，取值同 collect 白名单；空=全采 8 项） | `""` |
| `KK_TOP_N` | procs_top 进程数上限（1–50） | `5` |
| `KK_PLUGIN_DIR` | 自定义采集插件目录 | `<包>/plugins` |
| `KK_PLUGIN_TIMEOUT` | 插件 collect 超时（秒），超时隔离到重载为止 | `5` |
| `KK_MAX_WORKERS` | 命令执行线程池上限（1–64） | `8` |
| `KK_FS_ROOT` | 采集根（测试可指向伪造树） | `/` |
| `KK_ALLOW_SHELL` | 允许 `use_shell` 管道模式 | `1` |
| `KK_MAX_OUT_MB` | 单条命令输出上限（MB） | `4` |
| `KK_MAX_QUEUED` | 离线 out-queue 上限 | `512` |
| `KK_LOG` | 日志文件路径，留空输出到 stdout | `""` |
| `KK_LOG_LEVEL` | 日志级别 | `INFO` |
| `KK_IMAGE` | 上报的镜像名（可选） | `""` |
| `KK_UPDATE_URL` | 自更新地址（覆盖从服务端推断的 base） | `""` |
| `KK_UPDATE_INTERVAL` | 自更新检查间隔（秒，下限 30） | `300` |
| `KK_UPDATE_DISABLED` | 置 `1/true/yes/on` 关闭自更新 | `""` |
| `KK_UPDATE_INSECURE` | 置 `1/...` 关闭证书校验（**不推荐**） | `""` |
| `KK_UPDATE_HMAC_KEY` / `KK_UPDATE_REQUIRE_SIG` | 自更新 HMAC 校验 | 空 |
| `KK_AGENT_BIN` | 自更新时替换的二进制路径 | 当前可执行文件 |

## 部署

完整构建见 `scripts/build.sh`：先编译二进制 → 再在原镜像基础上叠加二进制 + 插件 + 监管脚本，上线即连。

手工叠加参考片段：`deploy/Dockerfile.snippet`。

## 测试

```bash
cd agent
uv run pytest tests -q    # 101 项：MQTT 传输 / psutil 采集 / 执行 / 插件 / 自更新 / 主循环链路
```

## 约束（重要）

- **MQTT socket 只允许 paho 网络线程触碰**：工作（采集/命令/插件）必须在一次性 daemon 线程跑，
  结果经队列回主线程发帧。
- **结果必须收敛**：任一分块发送失败要补发 `rc=-3` 失败终态，绝不让服务端那行命令停在 `running`。
- **依赖限于 psutil + paho-mqtt**（均为成熟 BSD/EPL 组件）。新增第三方依赖前先确认
  能否编译进单文件二进制、以及 RSS 增量是否可接受。
- 协议版本 `PROTO_VER = 3` 见 `config.py`，与服务端必须一致（见 `proto/messages.md`）。
