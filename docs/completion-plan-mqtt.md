# KontainKeeper 完成方案 v3（MQTT 收尾 + 开源复用 + Vue3 前端）

> 编写日期：2026-09-04（v3）  
> 基线：`main` @ 96cd206（Agent MQTT 化与 server src-layout 归位已提交）  
> 本文件是**执行路线图**，只描述方案，不在此改代码。执行时按阶段推进，每阶段完成即跑对应测试并提交。

**v3 相对 v2 的改动（全部来自对已提交代码的逐行复审）**：

| # | 改动 | 性质 |
|---|---|---|
| 1 | 新增**阶段 0：协议语义纠偏 + Agent 测试修复**前置门禁 | 修 3 处已提交的缺陷 + 把测试安全网提前 |
| 2 | 心跳 `retain=True` 判为缺陷（服务端重启会写入 500 条幽灵心跳） | v2 遗漏，且与 v2 的表述相反 |
| 3 | `_pub` 的 `is_connected()` 预检短路了 paho 离线排队 → 命令结果静默丢失 | v2 遗漏；该缺陷使「Broker 接管可靠性」的承诺失效 |
| 4 | 新增 **A5：打通批量采集命令**（`items` / `use_shell` 服务端字段缺失） | 用户核心需求当前只有 Agent 半边 |
| 5 | **否决 v2 的应用层 ACK 状态机**，改为一条 SQL 超时清扫器 | 减约 160 行自研代码、零协议变更 |
| 6 | **共享订阅默认不启用**；更正 v2「共享订阅为 MQTT5 标准」与 client_id 用法的两处错误判断 | 纠偏 + 去复杂度 |
| 7 | C3 自更新 ed25519 签名、C4 幂等键 → 明确「不做/按需」 | 收缩范围 |
| 8 | 新增**代码账本**（量化「复用开源 → 减少代码」） | 让主旨可验收 |

## 0. 目标与总体思路

**核心诉求**：Agent 上报 Linux 服务器各项指标 + 主动心跳；服务端批量下发「采集数据」与「执行 Linux 命令」两类命令；要求性能与稳定性；**最大化复用开源组件**，减少自研代码；**前端引入 Vue3**，以开源管理项目为底座适配；规模目标 **500 台机器**。

**总体思路**：把「连接可靠性 / 离线排队 / 在线判定」三大难题从自研代码里整体移交给成熟开源组件——传输层用 **MQTT Broker（Mosquitto 2.x）**，采集层用 **psutil**，HTTP 层继续用 **FastAPI**，前端底座用 **soybean-admin**。自研代码收缩到三条薄层：Agent 的「采集-执行-发布」循环、服务端的「桥接-存储-API」管线、前端的 5 个业务页面。

**开源组件复用清单**（每项都替代一段自研代码）：

| 组件 | 角色 | 替代的自研代码 | 版本/协议 |
|---|---|---|---|
| Mosquitto 2.x | MQTT Broker：连接、遗嘱(LWT)、离线队列、按用户 ACL | hub.py 内存连接表 + 手写补发 + 4404 超时判定 + token 校验（约 200 行） | EPL/EDL，生产免费 |
| paho-mqtt 2.x | Agent 与服务端的 MQTT 客户端（重连退避/保活/out-queue） | ws.py + conn.py 自研 RFC6455（261 行，已删） | EPL/EDL |
| psutil 7.x | 指标采集（CPU/内存/磁盘/网络/进程/用户） | 手工 /proc 解析（233 行） | BSD |
| FastAPI + uvicorn | REST API、OpenAPI、静态托管 | （沿用） | MIT |
| **SQLAlchemy 2 Core + async engine** | **一套代码适配 SQLite / PostgreSQL / MySQL**：方言、类型、upsert、连接池、重连 | 手写 sqlite3 封装：SCHEMA 字符串拼接、PRAGMA 建库、threading.Lock 串行化、`_outbuf` 缓存、ALTER 列迁移，**以及整个阶段 B 的异步化** | MIT |
| aiosqlite / asyncpg / aiomysql | 三种库各自的 async driver | （取代自写线程池包装同步 driver 的方案） | MIT / PostgreSQL 许可 / MIT |
| soybean-admin | 前端底座（Vue3 + Naive UI + UnoCSS + 鉴权/路由/请求层） | web/app.js 无框架单页（361 行，已到上限） | MIT |
| ECharts（soybean 内置封装） | 指标图表 | （原先无图表能力，属新增能力零自研成本） | Apache-2.0 |

**一句话架构**：

```
Agent (kk-agent, PyInstaller 单文件)
  │ psutil 采集 ──► paho-mqtt 发布（QoS0 hb / QoS1 result）
  ▼
Mosquitto 2.x（LWT 离线 · retain status · QoS1 cmd 离线队列 · pattern ACL）
  ▼
kk-server (FastAPI + paho 桥接 + aiosqlite 写队列)
  ▼
soybean-admin 前端（REST 轮询 + ECharts）
```

### 0.1 代码账本（把「减少项目代码」变成可验收指标）

| 变更 | 删除的自研代码 | 新增自研代码 | 净变化 |
|---|---|---|---|
| WS 客户端 → paho-mqtt | ws.py 261 + conn.py（已删） | transport.py 216（已加） | −45 |
| /proc 解析 → psutil | 旧 collector 233 | 新 collector 291（含按项采集） | +58（换来跨平台 + 8 个采集项） |
| 内存 Hub → MqttBridge | hub.py 150 + agent_ws.py 8 + 超时/补发逻辑 ~40 | mqtt_bridge.py ~150 | **−98** |
| ACK 状态机 → SQL 清扫器（v3 收敛） | — | ~10（v2 方案需 ~170） | **省 160** |
| 无框架前端 → soybean-admin | web 375（app.js 361 + index + style） | 5 个业务页（组件全部由底座/Naive UI 提供） | 底座部分 ≈ 0 自研 |
| **服务端自研规模** | 现 1617 行 | — | **目标 ≈ 950 行（−40%）** |

**这条账是本方案的主旨校验**：任何一个新增项若不能同时说明「它替代了哪段自研代码」，就不该进方案。


---

## 1. 当前基线（代码审计结论，2026-09-04 实测）

### 1.1 已提交（`main` @ 96cd206）

| 模块 | 文件 | 状态 |
|---|---|---|
| Agent 配置 | `agent/src/kk_agent/config.py` | ✅ PROTO_VER=2、MQTT 键、`agent_bin` 不默认 `sys.executable`（防解释器被覆盖） |
| 指标采集 | `agent/src/kk_agent/collector.py`（291 行） | ✅ psutil 全量重写，含 `collect_items()`（按项采集，供 `kind=collect` 命令） |
| MQTT 传输 | `agent/src/kk_agent/transport.py`（216 行） | ✅ 持久会话、LWT、QoS 分级、`status/hb/result/cmd` 主题——**但含 3.1/3.2 两处语义缺陷** |
| 主循环 | `agent/src/kk_agent/main.py`（176 行） | ✅ 事件驱动 + 一次性工作线程——**调度时钟与状态竞争见 3.3** |
| 命令执行 | `agent/src/kk_agent/executor.py`（150 行） | ✅ argv 直传 + 进程组清理 + 截断；有界线程池 |
| 自更新 | `agent/src/kk_agent/updater.py`（261 行） | ✅ P0-1 已修：先判形态再下载；sha256 + 可选 HMAC |
| WS 删除 | `agent/src/kk_agent/ws.py`、`conn.py` | ✅ 已删除 |
| Server 布局 | `server/src/kk_server/` | ✅ 标准 src-layout 归位（此前是直铺 + hatch sources 重映射） |
| Server 打包 | `server/pyproject.toml` | ✅ 加 paho-mqtt/aiosqlite 依赖；uv build 可用 |

### 1.2 测试基线（实测）

- `uv run pytest agent/tests -q`：**11 failed / 12 passed** —— `test_ws_framing.py` 因 ws.py 已删而收集失败；`test_collector.py` 5 项全测已不存在的 `/proc` 解析函数；`test_executor.py` 2 项、`test_updater.py` 4 项因签名变化失败。
- `uv run pytest server/tests -q`：**2 failed / 15 passed** —— 两个集成测试仍按旧 WS 协议起 Agent（`KK_SERVER=ws://...`）。其余 15 项（store/hub/黑名单/审计）仍绿。
- 结论：**代码已在 MQTT 上，测试还停在 WS 时代**。v3 把 Agent 侧的修复提前到阶段 0（门禁），服务端侧仍随 A/B 推进（阶段 F）。

### 1.3 服务端现状（尚未动，仍是 WS 架构）

`hub.py`（150 行）仍是内存连接表 + 手写补发；`store.py`（356 行）同步 SQLite；`agent_ws.py` 仍是 WS 入口；`web/app.js`（361 行）无框架单页。缺陷 P0-2/3/4、P1-5/6/7/8/11 全部原样存在（详见 `docs/architecture-review.md`）。

### 1.4 缺陷清单（R1–R5 来自 v3 复审，R6–R10 来自阶段 0 执行期实测）

| # | 发现 | 证据 |
|---|---|---|
| R1 | 心跳 retain 会在服务端重启时回放陈旧 hb，且落库用服务器时间 → 幽灵心跳污染指标 | `transport.py:181`、`store.py:131` |
| R2 | `_pub` 的 `is_connected()` 预检短路了 paho 的 QoS1 out-queue，重连窗口内结果丢失且服务端永停 running | `transport.py:164`、`main.py:49`、`main.py:132` |
| R3 | 调度用挂钟 + 伪随机抖动 + 差分状态无锁 | `main.py:163`、`167`、`60/101` |
| R4 | **`kind=collect` 与 `use_shell` 在服务端完全不可达**（用户核心需求缺口） | `commands.py:15-20`、`hub.py:137-143` vs `main.py:56`、`executor.py:133` |
| R5 | 批量下发是 N 次单查询 + N 次单插入（非阻断，但 500 台一次点击会慢） | `commands.py:46,57` |
| **R6** | **命令结果一条都发不出去**：`emit(kind, cid, res)` 三参数 vs `Runner` 的两参数调用 → TypeError 被线程池 `except: pass` 静默吞掉（旧 WS 队列元组的遗留签名） | `main.py:165`、`executor.py:148` |
| **R7** | 推送式自更新不可达：dispatcher 没有 `kind=update` 分支，落到 unknown kind 回 127；`updater.spawn_apply` 全仓零调用点 | `main.py:117`、`updater.py:259` |
| **R8** | `wait_ready` 不响应停止信号：Broker 不可达时阻塞满 timeout，容器停止会被 SIGKILL 而非优雅退出 | `transport.py:142` |
| **R9** | Windows 无 `killpg/getpgid`：命令超时后 `_kill_tree()` 抛 AttributeError → 子进程杀不掉且泄漏 | `executor.py:45` |
| **R10** | IPv6 字面量地址 `mqtt://[::1]:1883` 解析失败（按 `:` 切分把 host 切空） | `transport.py:44` |
| **R11** | **小时聚合按日历从最早心跳循环到今天**：一条坏时钟的 `ts=0` 心跳即可让循环跑几十万次把服务卡死——`record_hb` 信任帧内 ts 之后，这条路径由理论变为可达 | `store._aggregate_hours` |

> R6 是本轮最严重的一条：它让「服务端下发命令 → 看结果」这条主功能在真实运行中**完全失效**，而 12 个通过的单元测试一个都没抓到——因为 `Runner` 与 `main.run()` 的接法从未被测到。现已由 `test_main.py` 复刻 run() 的接法钉住（并给线程池补上异常日志，见 P2-18）。

**采集耗时实测（Windows 开发机）**：单轮 `collect()` ≈ 2.0s，其中 `proc` 项 2.05s（`process_iter` 逐进程开句柄）、`cpu` 首帧 0.58s，其余各项 <15ms。Linux 上 `/proc` 读取远快于此，且采集在带 busy 闸门的工作线程内、不阻塞 MQTT，60s 间隔下无风险。若将来压到 5s 级间隔或上万级主机，再把 `proc_metrics` 改为复用 `process_iter(attrs=...)` 已缓存的 `p.info`（省掉每进程第二次 `as_dict` 系统调用）。

---

## 2. 方案总览（八个阶段）

| 阶段 | 内容 | 修掉的缺陷 | 状态 |
|---|---|---|---|
| **0** | **协议语义纠偏 + Agent 测试修复（前置门禁）** | R1–R3、R6–R10 + Agent 测试红灯 | ✅ 完成（93 项 + 真实 Broker 10 项） |
| A | 服务端 MQTT 桥接（MqttBridge 替代 Hub）+ **批量采集打通** + 批量性能 | P0-3、P0-4、P1-8、P1-11、R4、R5、R12 | ✅ 完成（40 项 + 集成 3 项 + 真实进程链路） |
| B | Store 异步化 + 存储治理（**多库支持改造顺带完成异步化**） | P0-2、P1-5、P2-15、R11 | 🟡 部分：异步化/B2/B4/B5 完成，B3 回收与 B6 摘要视图未做 |
| C | 可靠性加固（v3 收敛为 4 项、约 40 行） | P0-4 残余、P1-10、P2-18/19 | ⬜ 未开始（清扫器已随 A 落地） |
| D | **前端 Vue3（soybean-admin 底座）** | P1-7、P2-19 | ⬜ 未开始 |
| E | 协议文档与周边同步 | P1-12 | ⬜ 未开始 |
| F | 测试与 CI 还债 | Agent 侧已随阶段 0 完成 | 🟡 部分 |
| G | 构建与部署（docker compose 全栈） | P2-13 | ⬜ 未开始 |

执行顺序：**0 → A → B → C → D → E → F/G**。要点：

- **0 是门禁**，不过绿不进 A（A/B/C 全靠它做回归保护）。
- **C 已收敛到 ~40 行**，与 A 的桥接同批实现最省事（清扫器就挂在 bridge 的 lifespan 任务上）。
- **D 只依赖 B6（summary 视图）与 B2（out_tail）**，这两个接口一旦落地，前端即可与 C/E 并行开工。
- 每阶段单独提交，提交信息标注覆盖的缺陷编号。

---

## 3. 阶段 0：协议语义纠偏 + Agent 测试修复（前置门禁）——✅ 已完成

**为什么提到最前**：v2 把测试还债排在阶段 F，意味着 A/B/C 所有改动都要在「13 个红灯」的基线上推进——等于没有安全网。同时本轮复审代码发现三处**协议语义级缺陷**，它们不会被后续阶段顺带修掉，且其中两条直接违背本方案「把可靠性交给 Broker」的核心承诺。

### 3.1 去掉 hb 的 retain（语义错误 + 数据污染）

`transport.py:181` 心跳以 `retain=True` 发布。Retained 消息会在**每次订阅建立的瞬间**回放给订阅者，于是 kk-server 每次重启/扩容都会立刻收到全部主机的陈旧 hb；而 `store.record_hb` 落库用的是 `now = int(time.time())`（`store.py:131`）而非帧内 `ts` → 500 条幽灵心跳以「刚刚上报」的身份写进 heartbeats 表，污染 24h 曲线并可能误触告警。

- **改**：`publish_hb(..., retain=False)`；retain 只留给 `status`（在线状态确实需要「新订阅者立刻拿到现状」，这是 Broker 换掉自研的真实收益点）。
- 配套：`record_hb` 优先采用帧内 `ts`（缺省回落 now），使重放/补发具备幂等性。
- **收益**：Broker retain 占用从 500×4KB 降到 500×0.2KB；「指标真相在 DB、状态真相在 Broker」这条边界才成立。

### 3.2 不要用 is_connected 短路 QoS1 发布（离线排队被自己废掉了）

`transport.py:164` 的 `_pub` 开头是 `if not self.cli.is_connected(): return False`。paho 的 `publish()` 本身就会把 QoS1/2 消息投入 out-queue、重连后自动重发（未连接时返回 `MQTT_ERR_NO_CONN`，积压受 `max_queued_messages_set(512)` 约束）。这个前置判断**在 Agent 侧把离线排队能力短路了**：

- 重连窗口内的命令结果被直接丢弃；`send_result` 中途 `return False` 后剩余分块全丢（`main.py:49-50`），而 `main.emit` 忽略返回值（`main.py:132-136`）→ 服务端那一行命令**永远停在 running**，与评审 P0-4「石沉大海」同型。
- **改**：QoS1 主题（`result`）不做 is_connected 预检，交给 paho 入队；成功判定放宽为 `rc in (MQTT_ERR_SUCCESS, MQTT_ERR_NO_CONN)`。QoS0 的 `hb` 保留预检（断线时积压陈旧指标无意义）。
- `send_result` 任一分块最终失败时，必须补发一个**失败终态**（`done=true, rc=-3`「结果回传中断」），让服务端可收敛；`emit` 接住返回值并落 warning 日志。

### 3.3 调度时钟与抖动（500 台峰值打散是硬需求）

- `main.py:163` 用 `time.time()` 做调度基准，NTP 回拨会造成心跳停摆或集中补发 → 改 `time.monotonic()`。
- `main.py:167` 抖动为 `interval * (0.9 + 0.2*(now % 1.0))`，依赖调度时刻的小数部分，并不均匀；500 台同时到点正是 Broker/DB 尖峰来源 → 改 `random.uniform(0.9, 1.1)`。
- `state_box["state"]` 被心跳线程（`main.py:101-102`）与 `kind=collect` 命令线程（`main.py:60-61`）并发读-改-写，磁盘/网络差分基线可能错配，产生速率尖刺 → 加 `threading.Lock` 或按来源分键隔离。

### 3.4 Agent 测试修复（把阶段 F 的欠账提前还掉一半）

> 执行期又发现并一并修掉 R6–R10（见 §1.4）：R6 尤其关键——它让「下发命令看结果」这条主功能在真实运行中完全失效，而原先 12 个绿灯测试一个都没抓到，因为 `Runner` 与 `run()` 的接法从未被测到。`test_main.py` 现复刻该接法作回归锁。

| 文件 | 处理 |
|---|---|
| `test_ws_framing.py` | 删除（ws.py / conn.py 已不存在） |
| `test_collector.py` | 重写为 mock `collector.psutil`；保留「按项采集容错」「未知项忽略」；`fake_fs` 与 /proc 伪造逻辑退役（conftest 仅留 executor 需要的部分） |
| `test_executor.py` | 适配 `Runner(emit, max_out, max_workers, allow_shell)` 新签名 |
| `test_updater.py` | 适配 `_http_get(..., timeout, as_bytes, insecure)` 新签名；补「`update_url` 缺省即跳过、不下载不落盘」 |
| `test_transport.py` | **新增**：`parse_broker` 全形态（mqtt/mqtts/user:pass@/IPv6）、主题拼装、QoS 与 retain 参数——作为 3.1/3.2 的回归锁 |

**验收（实测达成）**：

- `uv run pytest agent/tests -q` → **93 passed / 0 failed**（阶段 0 前为 11 failed / 12 passed）。新增 `test_transport.py`(26) 与 `test_main.py`(13) 钉住 QoS/retain/排队/失败终态/dispatcher 全链路。
- `scripts/mqtt_e2e.py`（新增）对**真实 Mosquitto** 跑通 10 项，把「交给 Broker 的可靠性」从单元层断言变成实测事实：retained status 上线、psutil 心跳、**R1 实测 Broker 只 retain status 不 retain hb**、**R6 shell 命令结果真实回传**（这条此前在主功能上是 100% 失效的）、300KB 输出 7 块按 seq 完整重组、`kind=collect` 按项采集回传结构化数据、**强杀进程触发 LWT offline**、**离线期间下发的命令由 Broker 排队、Agent 重连后自动补投并回传结果**。
- 唯一未被真实网络验证的一环是 paho 自身 out-queue 的重连补发（R2 方向：Agent 断线时持有结果）——需要 Broker 在命令执行中途重启，时机难以稳定复现，暂以单元测试在 `publish()` 边界处锁住语义。
- 服务端套件 15 passed / 2 failed，两条失败仍是 WS 时代集成测试（`KK_SERVER=ws://...`），随阶段 A/F 消解，本次未引入回归。

**本地 Broker 环境（开发/测试用）**：Windows 开发机无 docker，改用 WSL 常驻 Mosquitto：
`wsl -d Ubuntu-22.04 -- sudo apt-get install -y mosquitto mosquitto-clients`（systemd 服务随发行版自启，监听 0.0.0.0:1883 允许匿名）。
两个坑：① 发行版空闲被拆网络 → 先 `wsl -d Ubuntu-22.04 -- sleep 600 &` 挂住；② Ubuntu 22.04 仓库版本是 **1.6.9**，不含共享订阅/`pattern %u` 之外的 2.x 特性——够用本套冒烟，但 G1 的 ACL pattern 与生产部署要求必须在 **Mosquitto 2.x**（docker/CI）上另行验证。

---

## 4. 阶段 A：服务端 MQTT 桥接

**目标**：服务端变为「无状态桥接」——连接可靠性、离线命令排队、在线检测全部交给 Broker，服务端只负责「消息路由到 DB / 命令发布到主题」。这是整个架构的分水岭。

**文件**：`server/src/kk_server/services/mqtt_bridge.py`（新建）、`config.py`、`main.py`、`controllers/agent_ws.py`（删除）、`controllers/containers.py`、`controllers/commands.py`。

### A1. 桥接配置

```
KK_MQTT_URL=mqtt://broker:1883        # mqtts:// 启用 TLS
KK_MQTT_PREFIX=kk/v1                  # 与 Agent 的 KK_TOPIC_PREFIX 一致
KK_MQTT_CLIENT_ID=kk-server-1         # 实例唯一（见 A3「client_id 唯一性」）；默认单机即 kk-server
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
    # paho Client(MQTTv311, CallbackAPIVersion.VERSION2, clean_session=False)
    #   —— 与 Agent 同协议版本；共享订阅在 broker 侧生效，与客户端协议版本无关
    # 订阅（默认普通订阅，共享订阅作为扩容钩子，见 A3）：
    #   kk/v1/+/status   QoS1     kk/v1/+/hb   QoS0     kk/v1/+/result   QoS1
    def _sub_topics(self) -> list: ...          # 唯一需要改的地方：加 $share/{group}/ 前缀即扩容
    def _on_status(self, host, payload): ...    # upsert_container + set_online
    def _on_hb(self, host, payload): ...        # record_hb（阶段 B 前经 to_thread）
    def _on_result(self, host, payload): ...    # 归属校验 + append_result
    def dispatch_command(self, row) -> bool:    # publish QoS1 到 kk/v1/{pod}/cmd
    def push_upgrade(self, host, manifest): ... # 版本落后时发 kind=update 帧
    def sweep_timeouts(self): ...               # C1 的命令超时清扫（复用 lifespan 周期任务）
    def start(app): ...                         # FastAPI lifespan 启动/优雅停止
```

### A3. 关键决策与理由

- **在线判定真相源**：Agent 的 LWT + retain 的 `status` 帧。`_on_status(online=False)` → `store.set_online(host, False)`；服务端不再维护超时定时器（4404 逻辑随 WS 一起删除）。Broker 在 1.5×keepalive 内必发 LWT，比手写 `max(3×interval, 30s)` 超时判定更准确。
- **离线命令排队**：Agent 持久会话（clean_session=False）+ 命令 QoS1 → 断线期间命令滞留 Broker 会话队列，重连自动补投。**删除 `store.pending_for()` 补发路径**，`mark_sent` 语义变为「已发布到 Broker」。
- **心跳不 retain**（阶段 0 的 3.1）：hb QoS0、无 retain，丢了下一帧就到；指标真相在 DB，Broker 只做搬运。
- **命令结果归属校验（修 P0-3）**：`_on_result` 按主题里的 host 拿到天然归属：`cmd = store.get_command(id)`，`cmd.pod != host` → 丢弃 + `add_audit("mqtt", "result_mismatch")`。
- **共享订阅默认不启用（v3 更正）**：v2 把 `$share/kk-srv/...` 写进默认实现并称其为「MQTT5 标准」，两点需更正——① 共享订阅的语义在 MQTT 5.0 规范中有描述（§4.8.2），但**是否实现属于服务端选择**：Mosquitto 2.x、EMQX、HiveMQ 均已实现，且对 3.1.1 客户端同样生效（本质是 topic 名约定，与客户端协议版本无关）——所以既不能当作「升级到 v5 自然就有」，也不能假定所有 broker 都有；② **500 台单实例根本用不到**，它解决的是多消费端分摊分发压力，而本方案的瓶颈在 SQLite 写入（已由 B1 批量化解决），不在订阅分发。故默认普通订阅，只把主题拼装收敛到 `_sub_topics()` 一个方法作为扩容钩子。
- **client_id 唯一性（真实隐患）**：MQTT 中 client_id 即会话标识，**两个实例共用同一 client_id 会被 Broker 判为重复连接而互踢**（表现为交替掉线）。因此多实例时 client_id 必须逐实例唯一（`kk-server-1/2/...` 或注入 `KK_HOST`），共享组名 `$share/{group}/` 才是各组实例共用的部分。单机部署沿用 `kk-server` 即可。
- **鉴权迁移**：WS hello 的 token 校验移到 Broker 层——Mosquitto `password_file` + `acl_file`：每个 Agent 用主机名作用户名、token 作密码，ACL 限定只能读写自身 `kk/v1/{host}/#`（详见 G1）。服务端凭「已认证身份 ↔ status 帧 host 一致」做二次校验，不一致即拒并审计。**这同时修掉 P1-10（pod 身份可伪造）的服务端一半**。

### A4. controllers 改造

- 删 `agent_ws.py`；`controllers/__init__.py` 去掉注册。
- `containers.py::_container_view` 的 `online` 改读 `store.is_online(pod)`（B5 的列），不再调 hub.is_online。
- `commands.py::create_commands`：`await hub.try_dispatch(row)` 改 `bridge.dispatch_command(row)`（同步 publish，返回即「已交 Broker」）。批量原子性保留「先全量校验目标存在、再逐条下发」的语义，但校验与写入按 A6 批量化。
- `main.py`：`create_app` 装配 `MqttBridge`（FastAPI lifespan），`app.state.hub` → `app.state.bridge`；顺带修 P2-13（shutdown Event 从未 set → lifespan finally 里统一置位）。

### A5. 打通「批量采集数据」命令（核心需求缺口，阻断级）

**审计结论**：用户诉求里「服务端批量下发**采集数据**的命令」目前**只有 Agent 半边**，是接口断点而非待优化项——

| 端 | 现状 | 证据 |
|---|---|---|
| Agent | 读 `cmd["items"]` 走 `collect_items()`；读 `cmd["use_shell"]` 允许管道 | `main.py:56-63`、`executor.py:133` |
| Server | `CommandBody` 无 `items`、无 `use_shell` 字段；派发载荷只有 `{t,id,kind,argv,timeout}` | `commands.py:15-20`、`hub.py:137-143` |

结果：`kind=collect` 永远收不到 items（进 `rc=2 缺少 items` 分支）、`KK_ALLOW_SHELL` 开启的管道模式无法从服务端触发、Agent 的 8 个采集项（cpu/mem/disk/disk_io/net/proc/user/sys）在管理界面上完全不可用。

改法（与桥接同批落地）：

1. `CommandBody` 增 `items: Optional[List[str]]` 与 `use_shell: bool = False`；`kind` 放开 `shell | collect | plugin_reload | update`。
2. `kind=collect` 时服务端校验 `items ⊆ COLLECT_ITEMS`（服务端持一份白名单常量，与 Agent `collector.ITEM_NAMES` 对齐并纳入协议文档，避免双端漂移）；`argv` 允许空。
3. `bridge.dispatch_command(row)` 载荷补 `items` / `use_shell`，字段名与 Agent 严格一致（阶段 E 写进 proto v2）。
4. 新增 `GET /api/collect/items`：返回可采集项与中文标签，供前端出「指标项 × 主机」勾选面板（D3 命令中心）。
5. 命令表 `argv` 列对 collect 存 `{"items":[...]}` 结构（保持 JSON 列不变形），列表视图渲染成可读的「采集 cpu, mem」。

### A6. 批量下发的性能改法（500 台一次点击）

- `commands.py:46` 的目标存在性校验是 **N 次 `get_container` 查询** → 改一次 `SELECT pod FROM containers WHERE pod IN (...)`（500 参数在 SQLite 变量上限内，超量分片）。
- `create_command` 循环逐条 INSERT → 单事务批量 INSERT（配合 B1 写队列）。
- 发布仍逐条 publish（每主机一主题），保留「按主机可见、可单独重放、可按行查状态」的运维语义。**不引入广播主题**：`kk/v1/batch/{group}/cmd` 虽省 500 次发布，却要新增主机分组维护、结果按主机关联、部分失败可见性一整套协议面——与「减少代码」相悖，且 500 次 QoS1 发布对 Mosquitto（万级 msg/s）毫无压力。**这是本方案主动拒绝的一次「优化」。**

### A7. 验收

起本地 Mosquitto（`docker run` + 挂允许匿名的 conf）→ `uv run kk-server` → `mosquitto_pub` 模拟 Agent 发 status/hb/result → 断言：主机入库、在线状态正确、命令结果落库、host 伪造的结果被拒并留审计。再用 `POST /api/commands {kind:"collect", items:["cpu","net"], pods:[...]}` 走真实 Agent 线程，断言按项采集结果回传可见。

---

## 5. 阶段 B：Store 异步化与存储治理 —— 部分完成

> **状态更新（2026-09-05）**：本阶段的 **B1（异步化）已由「多数据库支持」改造一并完成**——
> store.py 改为 SQLAlchemy 2 Core + async engine（aiosqlite/asyncpg/aiomysql），REST 路由全部
> `async def` + `await`，因此 P0-2「同步 SQLite 阻塞事件循环」已消除，不需要再单独做一遍。
> **未做**：B3 的 hourly 90 天回收、命令 out 超龄清理（B3 的 lost 盖章已做）、B6 的
> `?view=summary` 摘要视图。这三项是纯粹的存储治理，随 D 阶段前端接口需求一起做即可。

**目标**：解除同步 SQLite 对事件循环的阻塞（P0-2，性能雪崩根因）；修命令输出不可见（P1-5）与存储只增不减（P1-6）。

### B0. 多数据库支持（2026-09-05 新增，已完成）

`KK_DB_URL` 三选一，缺省回落 `KK_DB_PATH` 的 SQLite，既有部署零改动可升级：

```
KK_DB_URL=sqlite:///data/kk.db
KK_DB_URL=postgresql://kk:pw@pg.internal:5432/kontainkeeper     # 自动补 +asyncpg
KK_DB_URL=mysql://kk:pw@mysql.internal:3306/kontainkeeper       # 自动补 +aiomysql，需 charset=utf8mb4
```

驱动按需装：`uv sync --extra postgres` / `--extra mysql`（默认只装 SQLite 路径）。

设计上必须记住的四条跨库事实（都由 `test_dialects.py` 静态把关）：

| 差异 | 处理 |
|---|---|
| upsert 三种写法 | `Store._upsert()` 一处方言分支（全库唯一分支）；MySQL 的 do-nothing 是 `INSERT IGNORE`，SQLAlchemy 2.0 **没有** `.ignore()`，必须 `prefix_with("IGNORE")` |
| MySQL TEXT 只有 64KB | 命令输出 base64 最大约 5.6MB → `out_b64` 等大字段用 `LONGTEXT` variant，否则静默截断 |
| MySQL 主键必须定长 | 所有主键用 `String(n)`，kv 表早期是 TEXT 主键（建表即失败） |
| `GREATEST` vs `max` | 在线宽限阈值改用 `CASE WHEN`，三库通用 |

**验证边界（重要）**：SQLite 走完整实测（40 项单测 + 3 项真实 Broker 集成 + 真实进程链路）；
**PostgreSQL / MySQL 只做了 DDL 与语句的跨方言编译校验，没有连过真实库**——上线前必须补一轮
真库跑测（CI 用 docker service，或本机 WSL `apt install postgresql mariadb-server`）。已知需
现场核对的点：MySQL 排序规则大小写敏感性（主机名 `Web-01` 与 `web-01` 是否同键）、PG 的标识符
小写折叠、以及三种库对 `Text` 主键/索引长度的具体限制。

**文件**：`server/src/kk_server/models/store.py`（大改）、`controllers/*`（统一 async）。

### B1. 异步化（已随 B0 完成，无需手写写队列）

- 原方案打算自己写 `asyncio.Queue` 攒批（每 200ms 或满 500 条 flush 一个事务）。实施时判定**不必做**：500 台 × 60s ≈ 8.3 写/s，SQLAlchemy async engine 的连接池 + WAL 下每条短事务都是亚毫秒级，手写批处理队列属于为不存在的瓶颈加复杂度。若将来间隔压到秒级或上万台，再引入攒批不迟——届时也只是在 Store 外加一层，不动调用方。
- 读走连接池独立连接（WAL 允许并发读）；`PRAGMA journal_mode=WAL / busy_timeout=5000 / synchronous=NORMAL` 仅对 SQLite 施加。
- REST 路由已全部 `async def` + `await`，消除原 `list_containers` 用 `def`、`create_commands` 用 `async def` 的阻塞语义不一致（评审 H1/M8）。
- 列表接口的在线状态改为**一次查回在线集合**（`online_set()`）：若逐行 `is_online`，500 台就是 500 次往返，比原来的内存查表更差。

### B2. 命令结果新帧适配（修 P1-5）——已随阶段 A 完成

`append_result(frame)` 接收 Agent 新帧 `{"id","seq","total","out_b64","done","rc","timed_out","elapsed_ms","truncated"}`：

- `out_b64` 解码累积；`done` 帧写 `status='done'` + rc/timed_out/elapsed_ms/truncated + finished_at。
- 输出列改存 base64 文本（`out`），消除 utf-8/replace 污染（修 L2，二进制输出不再乱码）。
- `list_commands` 返回 `out_tail`（末 2KB 解码）；新增 `GET /api/commands/{cid}/out` 返回完整输出（支持 `?format=text|base64`）。
- 失败终态收敛（配合 3.2）：收到 `rc=-3`「结果回传中断」帧时置 `status='failed'`，保留已收到的部分输出并置 `truncated=1`——**任何路径都不允许把命令留在 running**。
- `_outbuf` 内存缓存取消（B4）。

### B3. 存储回收（修 P1-6）—— 部分完成

已完成：`lost`/`timeout` 命令现在都会盖 `finished_at`，因此能被 30 天窗口回收（早先是 NULL 永不清理）。
**未完成**：hourly 90 天后 DELETE；大表分批 DELETE（`LIMIT 5000` 循环）避免长事务；命令 `out_b64` 超龄清文本保留状态行。

### B4. 落盘化 —— 已随阶段 A 完成

取消 `_outbuf`（内存态重启即丢 + 1000 条上限丢弃），累积输出直接增量写 `commands.out`（DB 即真相）。

### B5. 在线状态列 —— 已随阶段 A 完成

`containers` 表加 `online INTEGER DEFAULT 0` + `status_ts`，桥接写；`set_online`/`is_online(grace)` 直接读列，替代「每行调 hub.is_online + last_seen 宽限」的组合判断。

### B6. 列表性能（修 P1-7 的服务端半边）—— 未完成

在线判定已改成一次查询（`online_set()`）；但 `?view=summary` 摘要视图**仍未做**，
列表接口目前还逐行 `json.loads(last_metrics)`，是 D 阶段前端要用的接口，随 D 一起补。

`GET /api/containers?view=summary`：跳过逐行 `json.loads(last_metrics)`，返回 `pod/image/agent_ver/online/age_sec/disk_alert` + 冗余进 containers 表的 cpu/mem 摘要列（record_hb 时顺手 UPDATE）。完整 metrics 视图仅详情页用。ETag 支持作为可选优化，若 summary 视图已达标可不做。

---

## 6. 阶段 C：可靠性加固（v3 已大幅收敛）

**目标**：命令「不石沉大海」、身份可信。**v3 复审把本阶段从 5 项砍到 4 项，代码增量从约 200 行压到约 40 行**——理由见 C1。

### C1. 命令超时清扫器（替代 v2 的应用层 ACK 状态机）

v2 计划新增 `cmd_ack` 帧 + `pending → sent → acked → running → done` 五态。**复审后否决**：Broker 的 QoS1 + 持久会话已经保证「送达在线或重连的 Agent」，应用层再补一层 ACK 是**把刚交给开源组件的可靠性又自研一遍**，代价是多一个帧类型、多两列状态、多一段迁移逻辑与对应的双端测试——与本项目「复用开源、减少代码」的主旨直接相悖。

改为**不加帧 + 一条 SQL 清扫器**：

- 状态仅 `pending → sent → done | failed | timeout`；`running` 由 `_on_result` 收到首块输出时顺带置位（Agent 已在输出 = 已在执行，无需额外帧）。
- 清扫器（挂在 A2 `sweep_timeouts()`，复用 lifespan 周期任务）：
  ```sql
  UPDATE commands SET status='timeout', finished_at=?
   WHERE status IN ('pending','sent')
     AND COALESCE(sent_at, created_at) < ? - (timeout + 30);
  ```
  （用 `COALESCE(sent_at, created_at)` 而非只判 `sent_at`，否则 publish 失败停在 `pending` 的行带着 NULL sent_at 永不被扫——正是评审 P1-6 里 `lost` 永不清理的同一个坑。）
- 效果达成同一个验收点「失败可见、前端不转圈」，代码增量 ≈ 10 行，**零协议变更**。
- 唯一丢掉的语义是「区分 Agent 未收到 vs 收到未执行」——500 台运维场景不需要这个分辨率。协议文档预留 `cmd_ack` 主题位但**不实现**，未来确有需求时零迁移成本启用。
- 配合阶段 0 的 3.2（结果发布不再被短路 + 失败终态 `rc=-3`），P0-4 的两个残余分支（断线丢、不收敛）都 closed。

### C2. Agent 身份与 ACL（修 P1-10 收尾）

- Mosquitto `acl_file` 的 `pattern` 行按用户名展开：`pattern readwrite kk/v1/%u/#`，用户名即主机名（详见 G1）。
- 服务端 `_on_status` 校验「已认证用户名 == 帧内 host」，不一致即拒 + 审计（A3 已述）。
- 成本几乎为零：**不需要**在应用层再做 token↔host 绑定表，Broker 已把边界划清。

### C3. 自更新签名（修 P1-9，延后）

保持现状：sha256 + 可选 HMAC（`KK_UPDATE_HMAC_KEY` + `KK_UPDATE_REQUIRE_SIG`，部署期注入密钥即可用）。ed25519 需引入 `pynacl`（纯标准库不支持），换来的边际安全增益有限——HMAC 已能防「未持密钥者伪造更新」，而服务端被完全攻陷时攻击者同样能篡改清单里的 sha256，签名方案在此场景下并不更优。**决策：不做**，仅在灰度发布与 `POST /api/system/agent/rollback`（恢复 `.prev`）需要时再议。

### C4. 命令幂等（修 P2-17，降级为按需）

QoS1 是 at-least-once，Broker 补发确实可能重复执行。但：破坏性命令已被服务端黑名单（`security.py`）拦在源头，只读采集类命令重复执行无害。**默认不做**；若将来开放写操作类命令，再在 Agent 侧加「最近 N 个 cmd id 环形去重」（≈15 行），无需协议字段。

### C5. 可观测性（修 P2-18/19）

桥接统计（消息计数/延迟分位/DB 写耗时）→ `GET /api/system/stats`；结构化 JSON 日志；连接失败原因从 `log.debug` 升 `log.warning`。

---

## 7. 阶段 D：前端 Vue3（soybean-admin 底座）

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
│   │   ├── audit/            # 审计日志
│   │   └── login/            # 登录（soybean 自带，改接 /api/login）
│   ├── service/api/          # fetch 封装 + 5 个 API 模块
│   └── ...
├── Dockerfile                # node:22 build → nginx 托管 dist
└── .env                      # VITE_API_BASE=http://127.0.0.1:8443
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
| 命令中心 | `POST /api/commands`、`GET /api/commands`、`GET /api/collect/items` | **两种下发模式并列**：① 采集面板——勾选指标项（cpu/mem/disk/disk_io/net/proc/user/sys）× 多选主机 → 一次批量 collect；② 命令面板——argv 数组模式 / cmdline 模式（含 `use_shell` 开关，受 Agent `KK_ALLOW_SHELL` 约束）。历史列表 5s 轮询（仅本页激活），点行展开 out_tail，全量输出走 `/out` |
| 审计日志 | `GET /api/audit` | 只读分页表格 |

> 「采集面板」是用户核心需求「批量下发采集数据的命令」的界面落点，依赖 A5 的服务端字段打通。

### D4. 前端轮询策略（修 P1-7 前端半边）

- 路由级数据加载：切走即停轮询（soybean 路由钩子里 clear）。
- 摘要视图 10s、详情图表 30s、命令中心 5s（仅激活时）。
- 500 行表格用虚拟滚动 + 行内 Sparkline（可选）。

### D5. 旧 UI 退役

`server/src/kk_server/web/`（app.js/index.html/style.css）在 web 工程首个版本可用后删除，`KK_WEB_DIR` 指向新构建产物。保留一个提交窗口的双轨期。

### D6. 验收

`pnpm dev` 起前端 + `uv run kk-server` + 真实 Agent：登录 → 总览 500 行性能可接受（首屏 < 1s）→ 详情图表渲染 → **采集面板勾选 cpu+net 批量下发 3 台，看到结构化采集结果** → 命令面板下发 `echo kk-ok`，看到 done + 输出。

---

## 8. 阶段 E：协议文档与周边同步

- **`proto/messages.md` → v2**：改写为「MQTT 主题布局 + 帧格式」：`kk/v1/{host}/{status,hb,result,cmd}`（`cmd_ack` 仅登记主题位、标注未实现）；**明确 QoS 与 retain 语义**——`status` retain+LWT、`hb` QoS0 **无 retain**、`result`/`cmd` QoS1 无 retain；`cmd` 帧字段补 `items` 与 `use_shell`，并列出 8 个采集项白名单；close code 章节改为 MQTT 连接返回码与鉴权失败语义。
- **`docs/design.md`**：架构图改为 Broker 居中；Agent 模块表去 ws.py/conn.py 加 transport.py；依赖策略改为「引入依赖 + 单文件打包」；**RSS 口径从 <15MB 改为 25–35MB**；场景描述从「vscode-server 容器」扩为「Linux 主机（含容器）」。
- **`AGENTS.md`**：目录边界（web/ 新工程、transport.py、psutil）、命令（pnpm dev、pnpm build）、协议 v2 说明、测试数量更新；删除「Agent 必须纯标准库」这条已失效约束。
- **`README.md`**：快速开始改为「先起 Broker → kk-server → kk-agent（KK_SERVER=mqtt://...）」；仓库结构加 web/。
- **`agent/README.md`、`server/README.md`**：环境变量表同步 MQTT 键。
- **`docs/architecture-review.md`**：各缺陷打钩并指向本方案阶段号（含 R1–R5）。
- **术语统一**：产品定位已从「vscode-server 容器」扩为「Linux 主机」，界面/文档/字段说明统一用「主机 / host」；但**不改数据库表名与列名**（`containers` / `pod` 保持原样，改名的迁移成本换不到任何功能收益，属典型过度重构）。仅在 `models/store.py` 顶部加一行注释说明 `pod` 列即主机标识。

---

## 9. 阶段 F：服务端测试与 CI（Agent 侧已随阶段 0 完成）

v2 把双端测试都堆在本阶段，v3 拆掉 Agent 半边（提前到门禁），本阶段只剩服务端与 CI：

| 测试 | 处理 |
|---|---|
| `server/tests/test_integration.py` | 重写：真实 uvicorn + 本地 Mosquitto + 真实 Agent 线程，断言「上线 → 指标可见 → **批量 collect** → 批量 shell → 结果回传可见 → 断开后 LWT 置离线」；命令一律 argv 数组模式（Windows 空格路径兼容，见 AGENTS.md 陷阱） |
| `server/tests/test_bridge.py` | 新增（替代 `test_hub.py`）：status/hb/result 路由、归属校验（A 主机回传 B 的命令被拒 + 留审计）、`dispatch_command` 载荷含 items/use_shell、超时清扫器 |
| `server/tests/test_store.py` | 补：`out_tail`、lost/timeout 命令清理、hourly 90 天回收、`online` 列、批量写队列 |
| `server/tests/test_hub.py` | 删除（hub.py 随阶段 A 删除） |
| CI | `uv run pytest agent/tests -q` + `uv run pytest server/tests -q` 双绿为门禁；需 Broker 的集成测试打 `mqtt` marker，端口不可达即 skip（保证无 docker 环境仍全绿）；`scripts/loadtest.py` 改造为 MQTT 版并纳入 nightly（500 连接 ×60s 零误判掉线 + 命令成功率 100%） |

**本地 Mosquitto 测试环境**：`docker run -p 1883:1883 -v ./deploy/mosquitto/test.conf:/mosquitto/config/mosquitto.conf eclipse-mosquitto:2`（测试用 conf 开 `allow_anonymous true`，生产 conf 关）。

---

## 10. 阶段 G：构建与部署

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

`deploy/mosquitto/mosquitto.conf`：`listener 1883`、`allow_anonymous false`、`password_file`、`acl_file`。ACL 用 **pattern 行按用户名展开主题**（用户名即主机名，一台主机一个用户），这样 500 台不需要 500 段显式 `user` 配置：

```
# 服务端：全主题读写（默认普通订阅；未来多实例再补 $share 权限）
user kk-server
topic readwrite kk/v1/#

# Agent：只能读写自己的主题（%u = 认证用户名 = 主机名）
pattern readwrite kk/v1/%u/#
```

> `pattern` 行的 `%u` 展开语义与优先级需在 G 阶段用真实 Mosquitto 2.x 实测确认（见「风险与未决项」）。退路：脚本按主机清单生成显式 `user` 段。密码文件由 `mosquitto_passwd -c` 按清单生成。

### G2. server/Dockerfile 重写（修 P2-14）

改用 uv 安装（`COPY pyproject.toml uv.lock → uv sync --frozen --no-dev`），`COPY src ./kk_server` 改为标准包路径 `src/kk_server`；不再 pip 手装依赖（与 uv.lock 脱节问题消除）。多实例时前置 LB，服务端无状态。

### G3. agent PyInstaller

`agent/build/build_binary.sh` 确认 paho-mqtt/psutil hiddenimports；产物体积预计 8–12MB（psutil + paho 增量约 3MB）。`scripts/build.sh` 注入 `KK_SERVER=mqtt://...`。`sys.frozen` 路径下自更新继续可用。

### G4. 500 台容量估算

- 心跳：500 × 60s ≈ 8.3 msg/s，每帧约 2–4KB → Broker 与 kk-server 写入均无压力（Mosquitto 承载万级连接属常规用法，500 台远未触及边界）。
- 命令风暴：批量 500 台 × 4MB 输出属极端场景，受 Agent `max_out=4MB` + 48KB 分块约束。**R2 修复后出现新边界**：结果不再被丢弃而是入 paho out-queue，而 `MAX_QUEUED=512` 在 4MB 输出（≈86 块）下只容得约 6 条命令积压，断线期间超出部分会被静默淘汰。对策：`MAX_QUEUED` 提为可配置，且 `send_result` 感知入队失败（`rc=MQTT_ERR_QUEUE_SIZE`）→ 回 `rc=−3` 失败终态（与 3.2 同一收敛路径）。**否则只是把「立即丢」变成「静默丢」——这是本阶段必须一起做的。**
- SQLite：WAL + 批量写后，写放大 < 1 事务/s；DB 年增约 500 台 × 90 天 hourly + 30 天命令 ≈ 几百 MB 量级，单机无压力。

---

## 11. 缺陷映射表（评审 P0/P1/P2 + v3 新增 R1–R5 → 方案落点）

| 编号 | 缺陷 | 落点 | 状态 |
|---|---|---|---|
| P0-1 | 自更新先替换后判形态 | updater.apply_manifest 重写 | ✅ 已修（66e54fc） |
| P0-2 | Store 同步阻塞事件循环 | SQLAlchemy async engine（B0/B1） | ✅ 已修 |
| P0-3 | 命令回传不校验归属 | A2/A3 `_on_result` 主题归属 + C2 ACL | 阶段 A/C |
| P0-4 | sent 命令断线丢、不收敛 | 3.2 结果不再短路 + C1 超时清扫器 | 阶段 0/C |
| P1-5 | 命令输出 UI 不可见 | B2 out_tail + /out + D3 命令中心 | 阶段 B/D |
| P1-6 | 存储只增不减 | B3 cleanup 增补 | 阶段 B |
| P1-7 | 前端全量轮询放大 | B6 summary 视图 + D4 轮询策略 | 阶段 B/D |
| P1-8 | 内存 Hub 不可 HA | A2/A3 无状态桥接（共享订阅降为扩容钩子） | ✅ 已修（阶段 A） |
| P1-9 | 自更新无签名 | C3：**保持 HMAC，ed25519 不做** | 决策关闭 |
| P1-10 | Pod 身份可伪造 | A3 Broker 鉴权 + C2 pattern ACL | 阶段 A/C |
| P1-11 | 大输出饿死心跳 | 独立 QoS1 主题 + 线程池发布 | ✅ 架构已解（66e54fc） |
| P1-12 | 未提交重构 + 路径错 | 66e54fc / fa6ba9f + E 阶段文档 | ✅ 已修 |
| P2-13 | 清理线程无 shutdown | A4 lifespan 统一管理 | 阶段 A |
| P2-14 | Dockerfile 与锁文件脱节 | G2 uv 化 | 阶段 G |
| P2-15 | _outbuf 内存态丢失 | base64 在 SQL 侧拼接累加，无进程缓存 | ✅ 已修 |
| P2-16 | 协议无压缩 | 不做（帧已 <4KB） | 决策关闭 |
| P2-17 | 无幂等设计 | C4 按需（写操作已被黑名单在源头拦截） | 决策降级 |
| P2-18 | 异常被 log.debug 吞 | C5 日志升级 | 阶段 C |
| P2-19 | 无可观测面板 | C5 stats 接口 | 阶段 C |
| **R1** | **hb retain → 服务端重启回放幽灵心跳** | 3.1 去 retain + record_hb 用帧内 ts | 阶段 0 |
| **R2** | **is_connected 预检短路离线排队 → 结果静默丢失** | 3.2 交给 paho out-queue + 失败终态 rc=−3 | 阶段 0 |
| **R3** | **挂钟调度 + 伪随机抖动 + 差分状态无锁** | 3.3 monotonic + random + Lock | 阶段 0 |
| **R4** | **collect / use_shell 服务端不可达（需求缺口）** | A5 字段打通 + /api/collect/items + D3 采集面板 | 阶段 A/D |
| **R5** | **批量下发 N 次查询 + N 次插入** | A6 单查询校验 + 批量 INSERT | 阶段 A |
| **R11** | **`_aggregate_hours` 按日历从最早心跳循环到今天**：一条坏时钟的 `ts=0` 心跳即可让循环跑几十万次把服务卡死（`record_hb` 信任帧内 ts 后该路径变为可达） | 窗口夹在保留期内 + 帧时间戳越界回落服务器时间 | ✅ 已修 |
| **R6** | **emit 签名不匹配 → 命令结果一条都发不出去** | `emit(cid, res)` 对齐 + 线程池异常不再静默 | ✅ 已修 |
| **R7** | 推送式自更新不可达（dispatcher 缺 update 分支） | 补 `kind=update` → `spawn_apply` | ✅ 已修 |
| **R8** | `wait_ready` 卡住停止信号 | stop 感分的就绪等待（实测停止延迟 0.25s） | ✅ 已修 |
| **R9** | Windows 无 killpg → 超时命令杀不掉且泄漏 | 分平台杀进程树（Windows 走 taskkill /T） | ✅ 已修 |
| **R10** | IPv6 broker 地址解析失败 | 括号形态单独切分 | ✅ 已修 |
| **R11** | 坏时钟心跳可让小时聚合循环几十万次、卡死服务 | 聚合窗口夹在保留期内 + 帧 ts 越界回落 | ✅ 已修 |
| **R12** | 自更新接口的 token 吊销校验在 WS→MQTT 迁移中丢失 | `agent_token_auth` 补 `is_token_revoked` | ✅ 已修 |

---

## 12. 执行建议与提交粒度

1. **阶段 0 单独提交**（`fix(agent)` 协议语义 + `test(agent)` 重写）：**先修安全网，再动架构**——这是 v3 最重要的顺序调整。双端 `uv run pytest` 全绿后才进 A。
2. **A（桥接 + A5 采集打通 + A6 批量性能）**：架构分水岭。做完即「无状态 + Broker 兜底」，且用户核心需求「批量下发采集命令」首次端到端可用。**A5 不要拆到后面**——只做桥接等于把需求缺口一起搬进新架构。
3. **B（Store）**：性能雪崩根因。B2/B6 落地即解锁 D。
4. **C**：收敛后仅约 40 行，清扫器本就长在 bridge 的 lifespan 上，建议与 A 同批实现。
5. **D（前端）**：用户可见价值最高的一阶段；soybean 工程骨架单独提交，5 个业务页按页提交。
6. **E / F / G** 收尾。每阶段完成即跑测试后提交，`feat/fix/docs/test/chore` 中文前缀，提交信息标注覆盖的缺陷编号（P0-x / P1-x / R-x）。

---

## 13. 风险与未决项

- **R2 修复引入的新边界**（必须先想清楚再改）：结果不再立即丢弃，转而依赖 paho out-queue；`MAX_QUEUED=512` 在大输出断线时会溢出并被静默淘汰。**修复必须与「入队失败 → 回 `rc=−3` 失败终态」同批落地**（见 G4），否则只是把「立即丢」变成「静默丢」。
- **Mosquitto ACL pattern 的 `%u` 展开**：`pattern readwrite kk/v1/%u/#` 依赖「认证用户名 = 主机名」约定，其在 `acl_file` 中的展开与优先级需真实 Mosquitto 2.x 实测。退路：脚本按主机清单生成显式 `user` 段（500 台完全可管理）。
- **soybean-admin 裁剪深度**：thin 分支与主分支差异需拉取后确认；若 demo 页耦合较深、裁剪工作量超预期，备选 pure-admin-thin。**决策点：拉代码后 30 分钟内定，超时即换备选。**
- **Windows 开发机的 Mosquitto 集成测试**：依赖 docker；不可用时用 `mqtt` marker skip，CI 用 service 容器保证。
- **Agent RSS 口径变更**：psutil + paho 后常驻 RSS 预计 25–35MB（原 <15MB 作废）。Linux 主机场景可接受，但 design.md / AGENTS.md 的「纯标准库」卖点必须同步改掉（阶段 E），否则文档与代码互相打脸。
- **retain 持久化**：`status` retain 需 `persistence true` + data 卷，否则 Broker 重启后在线状态需等一轮心跳/LWT 才恢复（compose 已规划 data 卷）。
- **proto v1 → v2 过渡**：直接切换（双端同仓同发），不留兼容层；`4401/4402/4403/4404` close code 语义随 WS 删除，v2 文档改用 MQTT 连接返回码 + LWT 描述。
- **已提交但已知红灯**：`66e54fc` 提交时 Agent 测试 11 项失败（提交信息已标注）。阶段 0 的第一件事就是清零，不要在其上叠加其他 Agent 改动。

---

## 14. 一句话结论

Agent 侧「换心」已完成（psutil + paho-mqtt，261 行自研 WS 已删），但复审暴露出**三处协议语义缺陷（R1/R2/R3）与一处需求缺口（R4：批量采集在服务端根本不可达）**，因此 v3 把它们提为**阶段 0 门禁 + A5**，并把 v2 里我设计过重的四项（应用层 ACK 状态机、共享订阅、ed25519 签名、命令幂等键）**主动否决或降级，换回约 200 行不写的自研代码**。收尾主线不变：**用 MqttBridge 把连接可靠性与离线排队交给 Mosquitto，用 aiosqlite 写队列解除事件循环阻塞，用 soybean-admin 换掉 361 行无框架单页**——服务端自研规模从 1617 行收敛到约 950 行。按 0→A→B→C→D→E→F/G 推进，500 台规模下「指标上报 + 主动心跳 + 批量采集与命令」稳定支撑。
