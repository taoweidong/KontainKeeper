# KontainKeeper 容器直连管理方案设计

> 场景：K8S 部署的 vscode-server 容器 IDE，管理员后端需要直连容器进行管理与数据提取。
> 约束：不使用 K8S 集群自身能力（无 kube-api / exec / svc），不触碰宿主机，用户无感知，镜像制作期介入、上线即连。

---

## 1. 总体架构

```
┌───────────────────────── 管理端（独立部署，可达容器网络或经反代） ─────────────────────────┐
│  Admin Web UI (管理界面)                                                                   │
│        │ HTTP                                                                              │
│  Server (FastAPI)                                                                          │
│   ├─ REST API：容器列表 / 历史指标 / 命令下发 / 结果查看                                      │
│   ├─ WebSocket Hub：接受客户端反向长连接，指令下发 / 结果回传                                  │
│   └─ SQLite(默认) / PostgreSQL：心跳、指标、命令、审计日志                                    │
└──────────────▲──────────────────────────────────────────────────────────────────────────┘
               │ 出站 WebSocket 长连接（容器 → 服务端，唯一通道，用户无感知）
┌──────────────┴──────────────────────────────────────────────────────────────────────────┐
│  容器内 Agent（kk-agent，随镜像内置，entrypoint 拉起，后台守护）                              │
│   ├─ 心跳 + 定时指标上报（CPU/内存/磁盘/进程/用户）                                            │
│   ├─ 命令执行器（白名单受限 shell，结果异步回传）                                               │
│   ├─ 自定义采集插件目录（热加载，无需重启）                                                     │
│   └─ 仅 stdlib（无 psutil 等重依赖），常驻 RSS 目标 < 15MB                                     │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

核心思路：**容器发起出站长连接（反向通道）**。因为不能动宿主机、不能用 K8S Service/Ingress 做入站，唯一可行路径是容器主动连服务端。管理员在 Web 界面下发命令，经 WebSocket 推到目标容器内的 Agent 执行并回传——全程对使用 vscode-server 的用户透明。

## 2. 通信协议

- 传输：`wss://kk-server:8443/ws/agent`（客户端仅出站 443/8443，符合常见网络策略）
- 鉴权：镜像构建时注入的 `KK_TOKEN`（每镜像/每环境一个），服务端校验；容器身份 = `hostname`(pod名) + `KK_TOKEN` 指纹
- 消息：JSON 帧，带 `id` 关联请求/响应

```jsonc
// 上行：心跳+指标（默认 60s，可配置）
{"t":"hb","id":"...","ts":1690000000,
 "meta":{"pod":"vscode-7f9c-x2","image":"vscode-server:1.2","ver":"0.1.0"},
 "metrics":{"cpu":3.2,"mem_mb":412,"mem_pct":4.1,
            "disks":{"/":{"total":2064,"used":812},"/workspace":{"total":10240,"used":930}},
            "procs_top":[{"name":"node","cpu":2.1,"mem":180}],
            "users":[{"name":"dev01","login":"2026-08-30T01:00Z","clients":2}]}}

// 下行：命令
{"t":"cmd","id":"c-123","kind":"shell","argv":["du","-sh","/workspace"],"timeout":30}

// 上行：命令结果（异步，分块回传大输出）
{"t":"cmd_result","id":"c-123","seq":0,"rc":0,"out_base64":"..."}
```

- 断线重连：指数退避 1s→2s→…→60s 封顶；重连后先发 `hello` 补报身份，服务端按 pod 名关联历史。
- 安全：WSS + token；服务端可下发命令需管理员账号登录 Web 后二次确认（审计入库）。

## 3. 客户端 kk-agent（资源优先，纯 stdlib）

### 3.1 资源控制手段

| 手段 | 说明 |
|---|---|
| 纯标准库 | 不用 psutil（psutil 进程扫描内存占用高），直接读 `/proc`：`/proc/meminfo`、`/proc/stat`、`/proc/*/stat`、`statvfs`(os) |
| 单线程 + selectors | 一个线程跑事件循环：WebSocket 读、定时器、命令子进程；不用 asyncio 多任务膨胀 |
| 指标采集低频 | 默认 60s 一报，CPU 采样 1s×2 次即停，非采样期 0 CPU |
| 子进程限时 | 命令用 `subprocess` + timeout，输出超 1MB 分块/截断 |
| 内嵌帧精简 | 心跳 JSON 帧通常 < 2KB（未启用压缩，避免额外复杂度） |

预期常驻开销：RSS 8–15MB，空闲 CPU <0.1%。

### 3.2 模块划分

```
kk-agent/
├── __main__.py        # 入口（python3 __main__.py，由 entrypoint-wrapper 拉起）
├── agent_main.py      # 守护主循环：select 事件循环、重连、心跳调度、队列回传
├── kk_ws.py           # 自研最小 WebSocket 客户端（RFC6455，纯 stdlib）
├── kk_conn.py         # 连接建立、hello 构建、JSON 发送
├── kk_config.py       # env: KK_SERVER, KK_TOKEN, KK_INTERVAL, KK_PLUGIN_DIR ...
├── kk_collector.py    # /proc 采集：cpu/mem/disk/proc/users
├── kk_executor.py     # 命令执行（timeout + 输出封顶 + 一次性工作线程）
├── kk_plugins.py      # 插件热加载（按 mtime）
├── kk_logutil.py      # stderr + 1MB 轮转文件日志
└── plugins/           # 自定义采集插件：任意 *.py，实现 collect() -> dict
```

- **自定义采集**：管理端下发 `{"t":"cmd","kind":"plugin_reload"}` 或投放插件后，agent 扫描 `plugins/` 目录并 import，采集结果并入下一次心跳帧的 `custom` 字段。单个插件失败只跳过自身，不影响主循环。
- **用户信息**：`/etc/passwd` × `/proc/<pid>/status` Uid 进程计数 × `~/.vscode-server` 目录存在性（无家目录的系统账户过滤）。
- **命令安全**：argv 数组直传 exec（不经 shell 拼接）；黑名单在服务端统一拦截并审计。
- **守护方式**：镜像 entrypoint-wrapper 拉起带监督循环（崩溃 5s 重启）+ 每小时日志截断；不使用 systemd（容器内不必要）。

### 3.3 Dockerfile 介入（用户无感知）

```dockerfile
# ---- 在现有 vscode-server 镜像末尾追加 ----
COPY kk-agent /opt/kk-agent
ENV KK_SERVER=wss://kk-server.ops.svc:8443/ws/agent \
    KK_TOKEN=__BUILD_TIME_INJECT__ \
    KK_INTERVAL=60
# 包装原 entrypoint：先起 agent 后台，再 exec 原 vscode-server 启动
COPY entrypoint-wrapper.sh /usr/local/bin/kk-entrypoint
ENTRYPOINT ["kk-entrypoint"]
```

`entrypoint-wrapper.sh`：

```sh
#!/bin/sh
python3 -OO /opt/kk-agent/__main__.py >> /var/log/kk-agent.log 2>&1 &
exec "$@"   # 原 vscode-server 启动命令，用户侧完全不变
```

- 镜像构建流水线（CI）在 `docker build` 前用 `--build-arg` 注入 token 与 server 地址；上线即连，无需任何运行时配置。
- 日志写到容器内 `/var/log/kk-agent.log` 并限制 1MB 轮转，避免吃磁盘。

## 4. 服务端 kk-server

技术栈：FastAPI + uvicorn（内置 WebSocket 支持）+ 标准库 sqlite3（WAL，可平滑切 PG）。单进程即可管理数千长连接。

```
kk-server/
├── __main__.py        # python -m kk_server 入口
├── main.py            # create_app 工厂：REST + /ws/agent + 静态托管 + 清理线程
├── hub.py             # 连接表 {pod: websocket}，指令路由、离线补发
├── store.py           # SQLite：容器/心跳/命令/审计/会话，小时聚合与过期清理
├── api/
│   ├── deps.py        # 会话鉴权依赖
│   ├── auth_routes.py # 管理员登录/登出（PBKDF2 + 会话 token）
│   ├── containers.py  # 容器列表/详情/指标序列
│   ├── commands.py    # 下发命令（黑名单 + 审计）、结果查询
│   └── audit_routes.py# 审计日志查询
├── web/               # 管理界面（无框架原生 JS 单页，见下）
└── Dockerfile
```

管理界面（服务端内置）：
- **容器总览**：在线/离线、CPU/内存/磁盘卡片、按 pod 搜索；磁盘超阈值告警标红
- **容器详情**：指标趋势图（最近 1h/24h）、用户会话列表、插件自定义数据展示
- **命令控制台**：选容器 → 输入命令/选预设 → 实时看回传结果；支持批量下发
- **审计日志**：所有下发的命令、操作者、时间、结果归档

前端从简：无框架原生 JS 单页（hash 路由 + 轮询），由服务端直接托管，零构建步骤、零额外部署单元。

## 5. 仓库结构（git）

```
KontainKeeper/
├── README.md
├── docs/design.md            # 本文档
├── agent/                    # 客户端（随镜像分发）
│   ├── kk-agent/             # 纯 stdlib Agent 源码
│   ├── entrypoint-wrapper.sh
│   └── Dockerfile.snippet    # 叠加到 vscode-server 镜像的片段
├── server/                   # 服务端
│   ├── kk-server/
│   ├── web/
│   └── Dockerfile
├── proto/messages.md         # 协议消息定义（双端共用契约）
├── tests/                    # 单测 + 端到端集成测试
├── scripts/build.sh          # CI：注入 token 构建镜像
└── pyproject.toml
```

分支策略：`main` 稳定，`dev` 集成，`feat/*` 功能分支；agent 与 server 版本号联动（协议 `proto_ver` 字段，不匹配时服务端拒绝并提示升级镜像）。

## 6. 实施里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1 | 协议冻结 + agent 心跳/指标 + server 落库 | 界面看到容器在线与基础指标 |
| M2 | 命令下发/回传 + 审计 | 远程执行 `du -sh` 并回传 |
| M3 | 插件热加载 + 自定义采集 | 投放示例插件后数据出现在详情页 |
| M4 | 资源压测与加固：内存/CPU 基线、重连风暴、WSS、token 轮换 | 常驻 RSS < 15MB；1000 连接压测通过 |

## 7. 风险与对策

- **Agent 被用户发现/杀掉**：以同用户或 root 运行；wrapper 循环保活；进程名可伪装为常见辅助进程（视合规决定）。
- **token 泄露**：token 仅做接入鉴权 + 服务端 IP 白名单 + WSS；支持在线吊销（server 端 revocation list，agent 重连时校验）。
- **命令通道滥用**：管理端二次确认 + 全量审计；可配置命令黑名单（如 `rm -rf`、`reboot`）。
- **大规模心跳**：默认 60s + 服务端聚合入库（原始明细仅存 24h），避免 DB 膨胀。
