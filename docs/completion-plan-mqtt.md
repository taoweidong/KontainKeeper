# KontainKeeper 完成方案 v2（MQTT 收尾 + 开源复用 + Vue3 前端）

> 编写日期：2026-09-04（v2，替代同日 v1 草案）
> 基线：`main` @ 1c0d0c5 + 工作区未提交改动（Agent 已重写为 MQTT + psutil；server 已归位标准 src-layout `server/src/kk_server/`）
> 本文件是**执行路线图**，只描述方案，不在此改代码。执行时按阶段推进，每阶段完成即跑对应测试并提交。

## 0. 目标与总体思路

**核心诉求**：Agent 上报 Linux 服务器各项指标 + 主动心跳；服务端批量下发「采集数据」与「执行 Linux 命令」两类命令；要求性能与稳定性；**最大化复用开源组件**，减少自研代码；**前端引入 Vue3**，以开源管理项目为底座适配；规模目标 **500 台机器**。

**总体思路**：把「连接可靠性 / 离线排队 / 在线判定」三大难题从自研代码里整体移交给成熟开源组件——传输层用 **MQTT Broker（Mosquitto 2.x）**，采集层用 **psutil**，HTTP 层继续用 **FastAPI**，前端底座用 **soybean-admin**。自研代码收缩到三条薄层：Agent 的「采集-执行-发布」循环、服务端的「桥接-存储-API」管线、前端的 5 个业务页面。

**开源组件复用清单**（每项都替代一段自研代码）：

| 组件 | 角色 | 替代的自研代码 | 版本/协议 |
|---|---|---|---|
| Mosquitto 2.x | MQTT Broker，接管连接/遗嘱/离线队列/共享订阅 | hub.py 内存连接表 + 手写补发 + 心跳超时判定（约 150 行） | EPL/EDL 双许可，生产免费 |
| paho-mqtt 2.x | Agent 与服务端的 MQTT 客户端 | ws.py + conn.py 自研 RFC6455（261 行，已删） | EPL/EDL |
| psutil 7.x | 指标采集（CPU/内存/磁盘/网络/进程/用户） | 手工 /proc 解析（233 行） | BSD |
| FastAPI + uvicorn | REST API 与静态托管 | （沿用） | MIT |
| aiosqlite | 异步 SQLite 写入 | 同步 sqlite3 阻塞事件循环 | MIT |
| soybean-admin | 前端管理底座（Vue3 + Naive UI + UnoCSS） | web/app.js 无框架单页（361 行，已到上限） | MIT |
| ECharts（soybean 内置封装） | 指标图表 | 手写 SVG 折线 | Apache-2.0 |

**一句话架构**：

```
Agent (kk-agent, PyInstaller 单文件)
  │ psutil 采集 ──► paho-mqtt 发布
  ▼
Mosquitto Broker（LWT / retain / QoS1 / 共享订阅）
  ▼
kk-server (FastAPI + aiomqtt 桥接 + aiosqlite)
  ▼
soybean-admin 前端（REST 轮询 + ECharts）
```

---

## 1. 当前基线（代码审计结论，2026-09-04 实测）

### 1.1 已完成（工作区未提交）

| 模块 | 文件 | 状态 |
|---|---|---|
| Agent 配置 | `agent/src/kk_agent/config.py` | ✅ PROTO_VER=2、MQTT 键、`agent_bin` 不默认 `sys.executable`（防解释器被覆盖） |
| 指标采集 | `agent/src/kk_agent/collector.py`（291 行） | ✅ psutil 全量重写，含 `collect_items()`（按项采集，供 `kind=collect` 命令） |
| MQTT 传输 | `agent/src/kk_agent/transport.py`（216 行，新增） | ✅ 持久会话、LWT、QoS 分级、`status/hb/result/cmd` 主题 |
| 主循环 | `agent/src/kk_agent/main.py`（176 行） | ✅ 事件驱动 + 一次性工作线程；`send_result` 分块回传 |
| 命令执行 | `agent/src/kk_agent/executor.py`（150 行） | ✅ argv 直传 + 进程组清理 + 截断；有界线程池 |
| 自更新 | `agent/src/kk_agent/updater.py`（261 行） | ✅ P0-1 已修：先判形态再下载；sha256 + 可选 HMAC |
| WS 删除 | `agent/src/kk_agent/ws.py`、`conn.py` | ✅ 已删除 |
| Server 布局 | `server/src/kk_server/` | ✅ 标准 src-layout 归位（此前是 `server/src/` 直铺 + hatch sources 重映射） |
| Server 打包 | `server/pyproject.toml` | ✅ 加 paho-mqtt/aiosqlite 依赖；uv build 可用 |

### 1.2 测试基线（实测）

- `uv run pytest agent/tests -q`：**11 failed / 12 passed** —— `test_ws_framing.py` 因 ws.py 已删而收集失败；`test_collector.py` 5 项全部失败（仍测旧的 `/proc` 解析函数，psutil 化后函数已不存在）；`test_executor.py` 2 项失败（ Runner 构造签名变了）；`test_updater.py` 4 项失败（`_http_get` 签名、verify_and_replace 语义变化）。
- `uv run pytest server/tests -q`：**2 failed / 15 passed** —— 两个集成测试仍按旧 WS 协议起 Agent（`KK_SERVER=ws://...`），Agent 拒绝非 mqtt:// 地址。其余 15 项（store/hub/黑名单/审计）仍绿。
- 结论：**代码已跑在 MQTT 协议上，测试还停在 WS 时代**——这是阶段 F 要还的债，也是本次提交前必须先解决的一致性问题。

### 1.3 服务端现状（尚未动，仍是 WS 架构）

`hub.py`（150 行）仍是内存连接表 + 手写补发；`store.py`（356 行）同步 SQLite；`agent_ws.py` 仍是 WS 入口；`web/app.js`（361 行）无框架单页。缺陷 P0-2/3/4、P1-5/6/7/8/11 全部原样存在（详见 `docs/architecture-review.md`）。

---

## 2. 方案总览（七个阶段）

| 阶段 | 内容 | 修掉的缺陷 |
|---|---|---|
| A | 服务端 MQTT 桥接（MqttBridge 替代 Hub） | P0-3、P0-4、P1-8、P1-11 |
| B | Store 异步化 + 存储治理 | P0-2、P1-5、P1-6、P2-14/15 |
| C | 命令状态机 + 身份/签名加固 | P1-9、P1-10、P2-16/17 |
| D | **前端 Vue3（soybean-admin 底座）** | P1-7、P2-19 |
| E | 协议文档与周边同步 | P1-12 |
| F | 测试与 CI 还债 | 全部 |
| G | 构建与部署（docker compose 全栈） | P2-13 |

执行顺序：A → B → C → D → E → F/G 串行推进；其中 B 与 D 无依赖关系可并行。每阶段单独提交。

---

## 3. 阶段 A：服务端 MQTT 桥接

**目标**：服务端变为「无状态桥接」——连接可靠性、离线命令排队、在线检测全部交给 Broker，服务端只负责「消息路由到 DB / 命令发布到主题」。这是整个架构的分水岭。

**文件**：`server/src/kk_server/services/mqtt_bridge.py`（新建）、`config.py`、`main.py`、`controllers/agent_ws.py`（删除）、`controllers/containers.py`、`controllers/commands.py`。

### A1. 桥接配置

```
KK_MQTT_URL=mqtt://broker:1883        # mqtts:// 启用 TLS
KK_MQTT_PREFIX=kk/v1                  # 与 Agent 的 KK_TOPIC_PREFIX 一致
KK_MQTT_CLIENT_ID=kk-server           # 稳定 client_id（共享订阅分组用）
KK_MQTT_KEEPALIVE=60
KK_MQTT_USERNAME / KK_MQTT_PASSWORD   # Broker 鉴权（生产必须）
KK_MQTT_TLS_CA= / KK_MQTT_TLS_INSECURE=
```

`kk_server/__init__.py` 的 `PROTO_VER` 改为 `2`（与 Agent 对齐）。Settings dataclass 增加对应字段。

### A2. MqttBridge 实现（核心，约 120 行）

技术选型已定：**paho-mqtt 线程模型 + 提交到 FastAPI 事件循环**，不引入 aiomqtt。理由：与 Agent 侧同一客户端库（心智一致、版本联动）；paho 回调跑在自己的网络线程，通过 `loop.call_soon_threadsafe` 把消息处理调度回事件循环，避免在回调里直接触碰 SQLite 连接；aiosqlite 在阶段 B 到位前，桥接的写库调用先经 `asyncio.to_thread` 过渡。

```python
class MqttBridge:
    def __init__(self, store, settings, loop): ...
    # paho Client(MQTTv5, CallbackAPIVersion.VERSION2, clean_session=False)
    # 订阅：$share/kk-srv/kk/v1/+/status   QoS1
    #      $share/kk-srv/kk/v1/+/hb       QoS0
    #      $share/kk-srv/kk/v1/+/result   QoS1
    def _on_status(self, host, payload): ...   # upsert_container + set_online
    def _on_hb(self, host, payload): ...       # record_hb（阶段 B 前经 to_thread）
    def _on_result(self, host, payload): ...   # 归属校验 + append_result
    def dispatch_command(self, row) -> bool:   # publish QoS1 到 kk/v1/{pod}/cmd
    def push_upgrade(self, host, manifest): ...# 版本落后时发 kind=update 帧
    def start(app): ...  # FastAPI lifespan 启动/优雅停止
```

**共享订阅**（`$share/kk-srv/...`）是多实例水平扩容的钩子：每条消息只投递到组内一个实例，无重复写库。单实例部署时它退化为普通订阅，零成本。

### A3. 关键决策与理由

- **在线判定真相源**：Agent 的 LWT + retain 的 `status` 帧。`_on_status(online=False)` → `store.set_online(host, False)`；服务端不再维护超时定时器（4404 逻辑随 WS 一起删除）。Broker 在 1.5×keepalive 内必发 LWT，比手写 `max(3×interval, 30s)` 超时判定更准确。
- **离线命令排队**：Agent 持久会话（clean_session=False）+ 命令 QoS1 → 断线期间命令滞留 Broker 会话队列（上限 MAX_QUEUED=512），重连自动补投。**删除 store.pending_for() 补发路径**，`mark_sent` 语义变为「已发布到 Broker」。
- **心跳无 ACK/无连接归属**：hb QoS0 retain，丢了下一帧就到；指标落库以「最后写入者胜」，幂等。
- **命令结果归属校验（修 P0-3）**：`_on_result` 按主题里的 host 拿到天然归属：`cmd = store.get_command(id)`，`cmd.pod != host` → 丢弃 + `add_audit("mqtt", "result_mismatch")`。
- **鉴权迁移**：WS hello 的 token 校验移到 Broker 层——Mosquitto `password_file` + `acl_file`：每个 Agent 用 `kk-agent-{host}` 用户名 + token 作密码，ACL 限定只能 pub `$SYS` 之外自己的 `kk/v1/{host}/#`、sub 自己的 `/cmd`。服务端凭 mosquitto 已认证身份 + status 帧的 host 字段一致性做二次校验（host 与 client 用户名不匹配即拒）。**这同时修掉了 P1-10（pod 身份可伪造）的服务端一半**。

### A4. controllers 改造

- 删 `agent_ws.py`；`controllers/__init__.py` 去掉注册。
- `containers.py::_container_view` 的 `online` 改读 `store.is_online(pod)`（B5 的列），不再调 hub.is_online。
- `commands.py::create_commands`：`await hub.try_dispatch(row)` 改 `bridge.dispatch_command(row)`（同步 publish，返回即「已交 Broker」）。批量原子性保留「先全量校验目标存在、再逐条下发」。
- `main.py`：`create_app` 装配 `MqttBridge`（FastAPI lifespan），`app.state.hub` → `app.state.bridge`；顺带修 P2-13（shutdown Event 从未 set → lifespan finally 里统一置位）。

### A5. 验收

起本地 Mosquitto（`docker run -it eclipse-mosquitto` 允许匿名）→ `uv run kk-server` → 用 `mosquitto_pub` 模拟 Agent 发 status/hb/result → 断言：容器入库、在线状态正确、命令结果落库、host 伪造的结果被拒并留审计。多实例验证共享订阅不重复写（可在 A 阶段先手工验证，自动测试在 F 阶段补）。

---

## 4. 阶段 B：Store 异步化与存储治理

**目标**：解除同步 SQLite 对事件循环的阻塞（P0-2，性能雪崩根因）；修命令输出不可见（P1-5）与存储只增不减（P1-6）。

**文件**：`server/src/kk_server/models/store.py`（大改）、`controllers/*`（统一 async）。

### B1. aiosqlite + 单写者模型

- `AsyncStore`：一个写连接 + 批量写队列（`asyncio.Queue`）。心跳/结果等高频写攒批：每 200ms 或满 500 条 flush 成一个事务。500 台 × 60s ≈ 8.3 写/s，批后 < 1 事务/s。
- 读走独立连接（WAL 允许并发读）。
- `PRAGMA journal_mode=WAL; busy_timeout=5000; synchronous=NORMAL`。
- REST 路由统一 `async def`，消除 `list_containers` 用 `def` 而 `create_commands` 用 `async def` 的阻塞语义不一致（评审 H1/M8）。
- 保留同步 `Store` 作为过渡壳（测试与 `ensure_admin` 启动路径仍可用），或直接替换——按实现时手感定，方案不锁死。

### B2. 命令结果新帧适配（修 P1-5）

`append_result(frame)` 接收 Agent 新帧 `{"id","seq","total","out_b64","done","rc","timed_out","elapsed_ms","truncated"}`：

- `out_b64` 解码累积；`done` 帧写 `status='done'` + rc/timed_out/elapsed_ms/truncated + finished_at。
- 输出列改存 base64 文本（`out`），消除 utf-8/replace 污染（修 L2，二进制输出不再乱码）。
- `list_commands` 返回 `out_tail`（末 2KB 解码）；新增 `GET /api/commands/{cid}/out` 返回完整输出（支持 `?format=text|base64`）。
- `_outbuf` 内存缓存取消（B4）。

### B3. 存储回收（修 P1-6）

`cleanup()` 增补：hourly 保留 90 天；清理 `status='lost'` 命令（当前 finished_at IS NULL 永不被清）；大表 DELETE 分批（LIMIT 5000 循环）避免长事务；命令 out 超龄清文本保留状态行。

### B4. 落盘化

取消 `_outbuf`（内存态重启即丢 + 1000 条上限丢弃），累积输出直接增量写 `commands.out`（DB 即真相）。

### B5. 容器在线状态列

`containers` 表加 `online INTEGER DEFAULT 0` + `status_ts`，桥接写；`set_online`/`is_online(grace)` 直接读列，替代「每行调 hub.is_online + last_seen 宽限」的组合判断。

### B6. 列表性能（修 P1-7 的服务端半边）

`GET /api/containers?view=summary`：跳过逐行 `json.loads(last_metrics)`，返回 `pod/image/agent_ver/online/age_sec/disk_alert` + 冗余进 containers 表的 cpu/mem 摘要列（record_hb 时顺手 UPDATE）。完整 metrics 视图仅详情页用。ETag 支持作为可选优化，若 summary 视图已达标可不做。

---

## 5. 阶段 C：命令状态机与可靠性加固

**目标**：命令「不石沉大海」，身份可信，更新可信。500 台规模 C1/C2 必做，C3 视合规。

### C1. 命令 ACK 状态机（修 P0-4 残余）

引入 `cmd_ack` 帧（Agent → 服务端，QoS1）：`pending → sent → acked → running → done`。

- Agent 收到命令立即回 `cmd_ack`（executor.submit 前）。
- 桥接收到 ack 更新状态与 acked_at。
- 服务端定时扫描：`sent` 且 `timeout+30s` 无 ack → 置 `timeout`（失败可见，前端不转圈）。
- 与 Broker 离线排队互补：Broker 保证「送达在线/重连的 Agent」，ACK 保证「Agent 真的开始处理」。QoS1 PUBACK 只到 Broker，服务端看不到，所以应用层 ACK 仍有必要。

### C2. Agent 身份与 ACL（修 P1-10 收尾）

- Mosquitto `acl_file`：Agent 用户只能读写自己的 `kk/v1/{host}/#`。
- 服务端 `_on_status` 校验 client 用户名与 host 一致（A3 已做连接层）。
- 可选：`containers` 表登记 `first_seen_client`，同一 token 出现异 host 时审计告警。

### C3. 自更新签名（修 P1-9，可选）

ed25519 私钥签名 + Agent 内置公钥验签（`_verify_signature` 已留 HMAC 钩子，扩展为 ed25519）；灰度按 host 哈希分 10 批；`POST /api/system/agent/rollback` 恢复 `.prev`。**注意**：ed25519 纯标准库不支持，需 `pynacl` 或 PyInstaller hiddenimports 引入——与「Agent 依赖最小化」冲突，暂列可选项，默认保持 HMAC（部署期注入密钥）。

### C4. 命令幂等键（修 P2-17）

下发命令带 `idempotency_key`，Agent 侧记录最近 N 个已执行 key 去重，防 Broker 补发导致重复执行有副作用命令。

### C5. 可观测性（修 P2-18/19）

桥接统计（消息计数/延迟分位/DB 写耗时）→ `GET /api/system/stats`；结构化 JSON 日志；连接失败原因从 `log.debug` 升 `log.warning`。

---

## 6. 阶段 D：前端 Vue3（soybean-admin 底座）

**目标**：替换 361 行无框架单页；解决 P1-7 前端半边（全量轮询）；为后续图表/批量操作能力打地基。

### D1. 选型结论（已调研，2026-09-04）

**选定 soybean-admin**（github.com/soybeanjs/soybean-admin，MIT，~15k stars，活跃维护）。

| 候选 | 结论 | 理由 |
|---|---|---|
| **soybean-admin** | ✅ 首选 | 单包结构（非 monorepo），裁剪不牵动全局；Naive UI 颜值与表格/表单能力；内置 ECharts 封装；无强制 i18n/多租户负担 |
| vue-pure-admin (thin) | 备选 | Element Plus 生态、中文资料多；整体比 soybean 略重 |
| vue-vben-admin | ❌ | turbo monorepo + 多包依赖，对 5 页面场景严重过度，单人维护净负担 |
| Fantastic-admin | ❌ | 社区小、生态弱 |

### D2. 目录与工程结构

```
web/                          # 新建（独立 UV/npm 工程，与 server 分离）
├── package.json              # soybean-admin thin 基础 + 业务依赖
├── src/
│   ├── views/                # 5 个业务页面
│   │   ├── monitor/          # 主机总览（表格 + 在线状态 + 磁盘告警 + 批量选择）
│   │   ├── detail/           # 主机详情（ECharts 指标曲线 + Top 进程 + 登录用户）
│   │   ├── command/          # 命令中心（下发表单 + 历史列表 + 输出查看）
│   │   ├── audit/            # 宸计日志
│   │   └── login/            # 登录（soybean 自带，改接 /api/login）
│   ├── service/api/          # fetch 封装 + 5 个 API 模块
│   └── ...
├── Dockerfile                # node:22 build → nginx 托管 dist
└── .env                      # VITE_API_BASE=http://127.0 完.1:8443
```

- 裁剪原则：删 soybean demo 页/多标签页/主题切换可保留但不动；保留其路由/鉴权/请求层封装。
- **服务端继续托管构建产物**：`uv build` 后将 `web/dist` 拷入 server 包（`KK_WEB_DIR` 指向）；开发时 `VITE_PROXY` 代理到 8443。两种部署形态都支持。
- API 层用 OpenAPI（FastAPI 自动生成的 /openapi.json）生成 TS 类型（`openapi-typescript`），保证前后端字段对齐（阶段 E 顺带做）。

### D3. 五个页面的信息架构

| 页面 | 数据源 | 关键交互 |
|---|---|---|
| 登录 | `POST /api/login` | soybean 自带页，接 Bearer token |
| 主机总览 | `GET /api/containers?view=summary`（10s 轮询） | 500 行虚拟表格（Naive DataTable virtual-scroll）；多选 → 批量下发命令入口 |
| 主机详情 | `GET /api/containers/{pod}` + `/metrics` | ECharts：CPU/内存 24h 曲线（raw→hourly 自动切换）；Top 进程表、登录用户表、磁盘卡片 |
| 命令中心 | `POST /api/commands`、`GET /api/commands` | 下发表单（argv 数组模式 + cmdline 模式双模式）；历史列表 5s 轮询（仅命令中心页激活时）；点行展开 out_tail，全量输出走 `/out` 接口 |
| 审计日志 | `GET /api/audit` | 只读分页表格 |

### D4. 前端轮询策略（修 P1-7 前端半边）

- 路由级数据加载：切走即停轮询（soybean 路由钩子里 clear）。
- 摘要视图 10s、详情图表 30s、命令中心 5s（仅激活时）。
- 500 行表格用虚拟滚动 + 行内 Sparkline（可选）。

### D5. 旧 UI 退役

`server/src/kk_server/web/`（app.js/index.html/style.css）在 web 工程首个版本可用后删除，`KK_WEB_DIR` 指向新构建产物。保留一个提交窗口的双轨期。

### D6. 验收

`pnpm dev` 起前端 + `uv run kk-server` + 真实 Agent：登录 → 总览 500 行性能可接受（首屏 < 1s）→ 详情图表渲染 → 批量下发 `echo kk-ok` 到 3 台 → 命令中心看到 done + 输出。

---

## 7. 阶段 E：协议文档与周边同步

- **`proto/messages.md` → v2**：改写为「MQTT 主题布局 + 帧格式」：`kk/v1/{host}/{status,hb,result,cmd,cmd_ack}`、QoS/retain/LWT 语义、连接返回码说明；保留 upgrade/cfg 帧（cfg 经 cmd 主题下发）。
- **`docs/design.md`**：架构图改为 Broker 居中；Agent 模块表去 ws.py/conn.py 加 transport.py；依赖策略改为「引入依赖 + 单文件打包」。
- **`AGENTS.md`**：目录边界（web/ 新工程、transport.py、psutil）、命令（pnpm dev、pnpm build）、协议 v2 说明、测试数量更新。
- **`README.md`**：快速开始改为「先起 Broker → kk-server → kk-agent（KK_SERVER=mqtt://...）」；仓库结构加 web/。
- **`agent/README.md`、`server/README.md`**：环境变量表同步 MQTT 键。
- **`docs/architecture-review.md`**：各缺陷打钩并指向本方案阶段号。

---

## 8. 阶段 F：测试与 CI 还债（必做，提交前完成）

当前 13 个失败测试是 MQTT 重写的直接欠账，与 A/B 阶段联动：

| 测试 | 处理 |
|---|---|
| `agent/tests/test_ws_framing.py` | 删除（ws.py 已删） |
| `agent/tests/test_collector.py` | 重写：mock psutil 函数（`monkeypatch.setattr(collector.psutil, ...)`），保留「按项采集容错」「未知项忽略」用例；不再伪造 /proc（fake_fs fixture 删除或仅留 executor 用） |
| `agent/tests/test_executor.py` | 修 Runner 构造签名（emit 回调签名）、超时/killpg 用例保持 |
| `agent/tests/test_updater.py` | 修 `_http_get` 签名（新增 timeout/as_bytes/insecure 参数）；`update_url` 空串跳过的语义；形态校验用例保持并补「sys.executable 不被覆盖」 |
| `agent/tests/test_transport.py` | 新增：parse_broker 解析（mqtt://mqtts://user:pass@host:port）、主题构造、status 载荷、will_set 参数 |
| `server/tests/test_integration.py` | 重写：起真实 uvicorn + 本地 Mosquitto（testcontainers 或 CI service 容器）+ 真实 Agent 线程，断言「上线→指标可见→批量下发 shell→结果回传可见」；CLI 命令 argv 数组模式（Windows 兼容） |
| `server/tests/test_bridge.py` | 新增（替代 test_hub.py）：本地 Broker 上验证 status/hb/result 路由、归属校验（A pod 回传 B 命令被拒）、dispatch_command QoS1 |
| `server/tests/test_store.py` | 补：out_tail、lost/hourly 清理、online 列、批量写 |
| `server/tests/test_hub.py` | 删除（hub.py 已删） |
| CI | `uv run pytest agent/tests -q` + `uv run pytest server/tests -q`；集成测试标记 `mqtt` 需要 Broker（本地有 docker 就跑）；`scripts/loadtest.py` 改造后作 nightly（500 连接 ×60s 零误判掉线） |

**本地 Mosquitto 测试**：Windows 开发机用 `docker run -p 1883:1883 eclipse-mosquitto -p 1883`（容器默认配置拒绝匿名，需挂自定义 conf 允许匿名或设 `mosquitto.conf` 的 `allow_anonymous true`）。集成测试跑前检查 1883 端口可达，不可达则 skip（带 marker），保证无 docker 环境单测仍然全绿。

---

## 9. 阶段 G：构建与部署

### G1. docker-compose.yml（新增，一键全栈）

```yaml
services:
  mosquitto:
    image: eclipse-mosquitto:2
    ports: ["1883:1883"]
    volumes:
      - ./deploy/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - mosquitto-data:/mosquitto/data
      - ./deploy/mosquitto/passwordfile:/mosquitto/config/passwordfile:ro
      - ./deploy/mosquitto/aclfile:/mosquitto/config/aclfile:ro
  kk-server:
    build: ./server
    environment:
      KK_MQTT_URL: mqtt://mosquitto:1883
      KK_AGENT_TOKENS: dev-token
    ports: ["8443:8443"]
    depends_on: [mosquitto]
  # web 构建产物由 kk-server 托管（KK_WEB_DIR），或独立 nginx 容器反代
```

`deploy/mosquitto/mosquitto.conf`：`listener 1883`、`allow_anonymous false`、`password_file`、`acl_file`（Agent 限定自身主题、kk-server 拥有 `kk/v1/#` 与 `$share` 订阅权）。ACL 思路（具体占位符语义执行期实测，见 §12 风险）：

```
user kk-server
topic readwrite kk/v1/#
topic read $share/kk-srv/kk/v1/#

# Agent 用户名即主机名（如 user web-01）：
#   只能 publish 自己的 status/hb/result、subscribe 自己的 cmd
user <host>
topic write kk/v1/<host>/status
topic write kk/v1/<host>/hb
topic write kk/v1/<host>/result
topic read  kk/v1/<host>/cmd
```

### G2. server/Dockerfile 重写（修 P2-14）

改用 uv 安装（`COPY pyproject.toml uv.lock → uv sync --frozen --no-dev`），`COPY src ./kk_server` 改为标准包路径 `src/kk_server`；不再 pip 手装依赖（与 uv.lock 脱节问题消除）。多实例时前置 LB，服务端无状态。

### G3. agent PyInstaller

`agent/build/build_binary.sh` 确认 paho-mqtt/psutil hiddenimports；产物体积预计 8–12MB（psutil + paho 增量约 3MB）。`scripts/build.sh` 注入 `KK_SERVER=mqtt://...`。`sys.frozen` 路径下自更新继续可用。

### G4. 500 台容量估算

- 心跳：500 × 60s ≈ 8.3 msg/s，每帧约 2–4KB → Broker 内存队列与 kk-server 写入均无压力（Mosquitto 单实例标称 10 万连接）。
- 命令风暴：批量 500 台 × 4MB 输出属极端场景，受 Agent max_out=4MB + 分块 48KB + QoS1 流控约束；实际运维命令输出多在 KB 级。
- SQLite：WAL + 批量写后，写放大 < 1 事务/s；DB 年增长约 500 台 × 90 天 hourly + 30 天命令 ≈ 几百 MB 量级，单机无压力。

---

## 10. 缺陷映射表（架构评审 → 本方案落点）

| 编号 | 缺陷 | 落点 | 状态 |
|---|---|---|---|
| P0-1 | 自更新先替换后判形态 | updater.apply_manifest 重写 | ✅ 已修（工作区） |
| P0-2 | Store 同步阻塞事件循环 | B1 aiosqlite + 写队列 | 阶段 B |
| P0-3 | 命令回传不校验归属 | A2 `_on_result` 主题归属 + C2 ACL | 阶段 A/C |
| P0-4 | sent 命令断线丢、不收敛 | A2 Broker 离线排队 + C1 ACK 状态机 | 阶段 A/C |
| P1-5 | 命令输出 UI 不可见 | B2 out_tail + /out + D3 命令中心 | 阶段 B/D |
| P1-6 | 存储只增不减 | B3 cleanup 增补 | 阶段 B |
| P1-7 | 前端全量轮询放大 | B6 summary 视图 + D4 轮询策略 | 阶段 B/D |
| P1-8 | 内存 Hub 不可 HA | A2 共享订阅 + 无状态服务端 | 阶段 A |
| P1-9 | 自更新无签名 | C3 ed25519（可选）或 HMAC | 阶段 C |
| P1-10 | Pod 身份可伪造 | A3 Mosquitto ACL + C2 校验 | 阶段 A/C |
| P1-11 | 大输出饿死心跳 | A2 独立 QoS1 topic + Agent 线程池 | ✅ 架构已解（工作区） |
| P1-12 | 未提交重构 + 路径错 | 本次提交 + E 阶段文档同步 | ✅ 本次 |
| P2-13 | 清理线程无 shutdown | A4 lifespan 统一管理 | 阶段 A |
| P2-14 | Dockerfile 与锁文件脱节 | G2 uv 化 | 阶段 G |
| P2-15 | _outbuf 内存态丢失 | B4 落盘化 | 阶段 B |
| P2-16 | 协议无压缩 | 未列入（帧已小于 4KB，暂不必要） | 搁置 |
| P2-17 | 无幂等设计 | C4 幂等键 | 阶段 C |
| P2-18 | 异常被 log.debug 吞 | C5 日志升级 | 阶段 C |
| P2-19 | 无可观测面板 | C5 stats 接口 | 阶段 C |

---

## 11. 执行建议与提交粒度

1. **本次提交**（文档 + 现有工作区改动）：先提交 Agent MQTT 化 + server src-layout 归位 + 本方案 v2（docs 单独一笔）。13 个失败测试留待阶段 F——但**提交信息里明确标注「测试待 F 阶段重写」**，避免误以为绿。
2. **A（桥接）**：架构分水岭，做完即无状态 + Broker 兜底。单实例跑通端到端。
3. **B（Store）**：性能雪崩根因。B 与 D 可并行。
4. **C → D → E → F/G**：C 完成命令可靠性闭环；D 前端换血；E 文档收口；F 测试还债（若 A/B 先行，F 与 A/B 的测试改造可合并做）；G 部署收口。
5. **提交粒度**：按模块分批，`feat/fix/docs/test` 中文前缀。每阶段完成即 `uv run pytest` 后提交。

---

## 12. 风险与未决项

- **Mosquitto ACL 的按用户主题限定**：acl_file 不支持按用户名动态展开主题（`%u` 仅在 pattern 行生效），最稳妥做法是「用户名 = 主机名 + pattern 行 `pattern readwrite kk/v1/%u/#`」或为每台主机生成显式 `user` 段（500 台规模脚本生成即可）。执行期先小规模实测再定。
- **soybean-admin 裁剪深度**：thin 分支与主分支差异需拉取后确认；若 demo 页耦合较深，裁剪工作量可能超预期——备选直接用 pure-admin-thin。**决策点：拉代码后 30 分钟内定，超时即换备选。**
- **Windows 开发机的 Mosquitto 集成测试**：依赖 docker 可用；不可用时 skip。CI 上用 service 容器保证。
- **Agent RSS 目标调整**：psutil + paho 后常驻 RSS 预计 25–35MB（原 < 15MB 目标作废）；vscode-server 容器场景完全可接受，但需在 design.md 里改口径。
- **retain 心跳帧的内存占用**：500 台 retain 心跳驻留 Broker ≈ 500 × 4KB ≈ 2MB，无压力；但 Mosquitto 默认 retain 不持久（需 `persistence true` + `retain_available true` + data 卷）——compose 里已规划。
- **proto v1 → v2 过渡**：直接切换（双端同仓同发），不留兼容层；`4401/4402/4403` close code 语义随 WS 删除，协议文档 v2 改用 MQTT 返回码与 LWT 语义描述。

---

## 13. 一句话结论

Agent 侧「换心」已完成（psutil + paho-mqtt，261 行自研 WS 已删）；收尾重心在服务端与前端：**用 MqttBridge 把连接可靠性与离线命令交给 Mosquitto（消灭内存 Hub、手写补发、心跳超时三件自研包袱），用 aiosqlite 写队列解除事件循环阻塞，用 soybean-admin 换掉 361 行无框架单页**，再补 ACK 状态机与归属校验，最后同步协议文档、测试与 compose 部署。按 A→B→C→D→E→F/G 推进，500 台规模下「指标上报 + 主动心跳 + 批量命令」稳定支撑。
