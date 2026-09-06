# KontainKeeper 架构评审与优化方案

> 分析日期：2026-09-04　代码基线：`main` @ 1c0d0c5（含暂存区未提交的 MVC 重构）
> 范围：`agent/src/kk_agent/`（1234 行）、`server/src/`（1452 行）、`proto/messages.md`、`docs/design.md`、部署脚本
> 本文为 **2026-08-30 首版评审的更新版**：首版发现的问题多数已修复，本次重点补录新发现的缺陷。

---

## 0. 缺陷处置总表（2026-09-06 更新）

> 本表是**最新状态**，覆盖本文 §3 的 P0–P2、v3 复审的 R1–R5、以及执行期实测新增的 R6–R13。
> 处置方案与阶段划分见 [`docs/completion-plan-mqtt.md`](completion-plan-mqtt.md)。
> §3 各表的「证据」列仍指向**发现当时**的代码位置，未同步刷新——以本表状态为准。

| 编号 | 缺陷 | 状态 | 落点 |
|---|---|---|---|
| **P0-1** | 自更新先替换后判形态 | ✅ 已修 | `updater.apply_manifest` 重写（66e54fc） |
| **P0-2** | Store 同步阻塞事件循环 | ✅ 已修 | SQLAlchemy 2 Core + async engine（阶段 B0/B1） |
| **P0-3** | 命令回传不校验归属 | ✅ 已修 | MQTT 主题天然归属 + `_on_result` 校验（阶段 A） |
| **P0-4** | sent 命令断线丢、不收敛 | ✅ 已修 | 结果不再短路 + 失败终态 `rc=-3` + SQL 超时清扫器（阶段 0/C1） |
| **P1-5** | 命令输出 UI 不可见 | ✅ 已修 | `out_tail` + `/out` + 命令中心页（阶段 B2/D） |
| **P1-6** | 存储只增不减 | ✅ 已修 | `cleanup()` 重写：分批删 / hourly 90 天 / 输出 7 天清（阶段 B3） |
| **P1-7** | 前端全量轮询放大 | ✅ 已修 | `?view=summary` 摘要列 + 路由级轮询（阶段 B6/D4） |
| **P1-8** | 内存 Hub 不可 HA | ✅ 已修 | MqttBridge 无状态桥接，`hub.py` 已删（阶段 A） |
| **P1-9** | 自更新无发布者签名 | 🔶 决策关闭 | 保持 sha256 + 可选 HMAC；ed25519 需 pynacl 且边际收益有限（C3） |
| **P1-10** | Pod 身份可伪造 | ✅ 已修 | Broker 鉴权（用户名=主机名）+ `status` 帧一致性校验（阶段 A/C2） |
| **P1-11** | 大输出饿死心跳 | ✅ 已修 | 结果走独立 QoS1 主题 + 线程池发布（66e54fc） |
| **P1-12** | 未提交重构 + 路径错 | ✅ 已修 | 66e54fc / fa6ba9f；路径已随本次文档同步统一 |
| **P2-13** | 清理线程无 shutdown | ✅ 已修 | lifespan 统一管理（阶段 A4） |
| **P2-14** | Dockerfile 与锁文件脱节 | ⬜ 未开始 | G2：改为 `uv sync --frozen --no-dev`（阶段 G） |
| **P2-15** | `_outbuf` 内存态丢失 | ✅ 已修 | base64 在 SQL 侧拼接累加，无进程缓存 |
| **P2-16** | 协议无压缩 | 🔶 决策关闭 | 不做（帧已 2–4KB，压缩收益不抵复杂度） |
| **P2-17** | 无幂等设计 | 🔶 降级按需 | 写操作已被黑名单在源头拦截（C4） |
| **P2-18** | 异常被 `log.debug` 吞 | ✅ 已修 | 连接失败升 `log.warning`、入队失败可见（阶段 C5） |
| **P2-19** | 无可观测面板 | ✅ 已修 | `GET /api/system/stats`（阶段 C5） |
| **R1** | hb retain → 重启回放幽灵心跳 | ✅ 已修 | 去 retain + `record_hb` 用帧内 ts（阶段 0） |
| **R2** | `is_connected` 预检短路离线排队 | ✅ 已修 | 交给 paho out-queue + 失败终态（阶段 0） |
| **R3** | 挂钟调度 + 伪随机抖动 + 差分无锁 | ✅ 已修 | `monotonic` + `random.uniform` + 按来源分键（阶段 0） |
| **R4** | `collect`/`use_shell` 服务端不可达（需求缺口） | ✅ 已修 | 字段打通 + `/api/collect/items` + 采集面板（阶段 A5/D3） |
| **R5** | 批量下发 N 次查询 + N 次插入 | ✅ 已修 | 单查询 `IN` 校验 + 批量 INSERT（阶段 A6） |
| **R6** | `emit` 签名不匹配 → 结果一条都发不出 | ✅ 已修 | `emit(cid, res)` 对齐 + 线程池异常不再静默（阶段 0） |
| **R7** | 推送式自更新不可达（缺 update 分支） | ✅ 已修 | 补 `kind=update` → `spawn_apply`（阶段 0） |
| **R8** | `wait_ready` 卡住停止信号 | ✅ 已修 | stop 感知的就绪等待（阶段 0） |
| **R9** | Windows 无 killpg → 超时命令杀不掉 | ✅ 已修 | 分平台杀进程树（Windows 走 taskkill /T） |
| **R10** | IPv6 broker 地址解析失败 | ✅ 已修 | 括号形态单独切分（阶段 0） |
| **R11** | 坏时钟心跳让小时聚合循环几十万次 | ✅ 已修 | 聚合窗口夹在保留期内 + 帧 ts 越界回落 |
| **R12** | 自更新 token 吊销校验在迁移中丢失 | ✅ 已修 | `agent_token_auth` 补 `is_token_revoked`（阶段 A；后注：token 体系已随协议 v3 整体移除，改为 `KK_AGENT_IPS` IP 白名单） |
| **R13** | result 分块并发乱序丢块（E2E 实测 200KB 输出仅落库 2-4 块且无标记） | ✅ 已修 | `_on_result` 按 cmd id 加 `asyncio.Lock` 串行化，终态帧后回收锁（2026-09-06） |

**剩余未闭环**：P2-14（Dockerfile uv 化，阶段 G）。
**已知的验证边界**：PostgreSQL / MySQL 只做了 DDL 与语句的跨方言编译校验，未连过真实库；
集成测试在无 Broker 环境下 skip，真实 MQTT 链路需在 CI 用 docker service 验证。

---

## 1. 首版问题处置情况（8-30 → 9-04）

| 首版编号 | 问题 | 状态 | 依据 |
|---|---|---|---|
| H1 | 阻塞式 SQLite 跑在 async 事件循环上 | ❌ **未修（仍是最核心瓶颈）** | `store.py:103-115`、`hub.py:116` |
| H2 | `entrypoint-wrapper.sh` 词分割导致原 entrypoint 失效 | ✅ 已修 | 改为 Docker 原生 ENTRYPOINT 数组透传（`7abcbef`） |
| H3 | TLS 未终结、token 明文风险 | ✅ 已修 | README 增补 TLS 硬约束章节 |
| M1 | WS 握手用子串弱校验 `"101" in ...` | ✅ 已修 | `ws.py:183-185` 改为精确匹配状态码 |
| M2 | 黑名单纯子串匹配可绕过 | ✅ 已修 | `security.py` 升级为「程序名 + 高危参数」结构化规则 |
| M3 | 无连接级 keepalive / 半开检测 | ⚠️ 部分 | 服务端有 4404 心跳超时，Agent 仍不主动 ping |
| M4 | 单进程内存态 Hub，无法 HA | ❌ 未修 | `hub.py:19` |
| M5 | 登录无速率限制 | ❌ 未修 | `auth.py:16-23` |
| M6 | 批量下发非原子，留孤儿 pending | ✅ 已修 | `commands.py:45-48` 先全量校验再下发 |
| M7 | `metrics_series` 24h 边界接缝 | ❌ 未修 | `store.py:148-159` |
| M8 | 列表接口无缓存/分页，逐行 `json.loads` | ❌ 未修 | `containers.py:48` |
| L2 | 二进制命令输出被 `utf-8/replace` 污染 | ❌ 未修 | `store.py:197` |
| L3 | `_aggregate_hours` 锁下碎查询 | ❌ 未修 | `store.py:313-342` |

---

## 2. 做对了什么（不要改这些）

| 决策 | 为什么值得保留 |
|---|---|
| 容器主动出站 WebSocket | 「不动宿主机、不用 kube-api」硬约束下唯一可行解，选型无可替代 |
| Agent 纯标准库 + 单文件二进制 | 免去在用户镜像里塞 Python 运行时，是「用户无感知」的前提 |
| `proto/messages.md` 契约先行 | 双端版本联动、close code 语义明确，协议演进有锚点 |
| Agent 单线程 select + 一次性工作线程 | socket 只由主线程触碰，从根上消灭并发 bug，也压住 RSS |
| 命令 argv 直传 exec + 服务端黑名单 | 不经 shell 拼接，绕过整类注入问题 |
| `collector.py` 全量经 `fs_root` 注入 | 采集逻辑可在 Windows 上用伪造 /proc 测试，罕见的可测性设计 |

---

## 3. 新发现的架构问题

### 🔴 P0 — 阻断性缺陷

| # | 问题 | 证据 | 后果 |
|---|---|---|---|
| **1** | **自更新先替换、后判断形态**：`verify_and_replace()` 在 `_is_binary_target()` 之前执行 | `updater.py:195-200` + `config.py:37`（`agent_bin` 缺省 = `sys.executable`） | 源码形态运行（含 README 推荐的 `uv run kk-agent`）且未设 `KK_AGENT_BIN` 时，下载的二进制会**直接覆盖容器里的 Python 解释器**。测试全部显式传了 `agent_bin`（`test_updater.py:87,101`），**该路径零覆盖** |
| **2** | **Store 同步阻塞事件循环**（H1 未修） | `store.py:103-115`；`hub.py:116`；`commands.py:24`（`async def` 内 N 次同步查询） | 心跳写入串行化并卡住整个 loop。1000 容器时，`hb_timeout = max(3×interval, 30)` 被我们自己制造的延迟触发 → **大规模误判 4404 掉线**。`list_containers` 用 `def`（线程池）而 `create_commands` 用 `async def`，阻塞语义还不一致 |
| **3** | **命令回传不校验归属**：`append_result(msg)` 只按 `id` 匹配，不校验结果是否来自该命令所属 pod 的连接 | `hub.py:117-118` → `store.py:189` | 任一持合法 token 的 Agent 可回传**其他 pod 的命令结果**，污染命令表与审计链 |
| **4** | **命令在 `sent` 状态断线即丢，且永不收敛** | `store.py:181-183`（补发只查 pending）；`hub.py:94-96`；`store.py:352-354`（1h 才标 lost） | Agent 收到命令后崩溃/重启 → 永久停在 `sent`，前端一直转圈，无超时提示、无重试、无失败反馈 |

### 🟠 P1 — 重要风险

| # | 问题 | 证据 | 后果 |
|---|---|---|---|
| **5** | **命令输出在 UI 上永远看不见**：`_CMD_COLS` 不含 `out`，前端 `renderCmd` 却读 `c.out` | `store.py:85` vs `web/app.js:271` | 列表接口不返回 `out`，详情页也走 `list_commands`，**整个管理界面看不到任何命令回显** |
| **6** | **存储只增不减**：`hourly` 表从不清；`commands` 只删 `finished_at IS NOT NULL`，而 `lost` 命令该字段为 NULL | `store.py:348-355` | `lost` 命令带着最大 4MB 的 `out` 永久留存；`hourly` 每行含完整 JSON。1000 容器 90 天 → 数 GB 无回收路径 |
| **7** | **前端全量轮询放大**：容器列表每 5s 拉全量，服务端逐行 `json.loads(last_metrics)` | `web/app.js:177,262,334`；`containers.py:48,25` | 1000 容器 × 5s = 每秒 200 次全 JSON 反序列化 + 全表扫描，无 ETag / 分页 / 字段裁剪。**服务端先于 Agent 到瓶颈** |
| **8** | **Hub 连接表是进程内存 dict**，无任何多实例抽象 | `hub.py:19,84` | `--workers > 1` 或扩容时命令路由到无连接的进程 → 永久 `pending`。文档称「单进程即可」，但代码与 Dockerfile 均未强制 |
| **9** | **自更新无发布者签名**：仅用服务端下发的 sha256 校验 | `updater.py:146-174` | 只能防传输损坏，防不了服务端被攻陷 → **一键在所有容器内 RCE**。无灰度、无金丝雀、无回滚 API（`.prev` 存在但无接口） |
| **10** | **Pod 身份可伪造**：`pod` 由 Agent 自报，同名新连接会抢占并踢掉旧连接 | `config.py:30`；`hub.py:57,78-84`（4403） | 拿到 token 者可伪装任意 pod 并顶掉真实连接，劫持指标与命令通道 |
| **11** | **大输出命令饿死心跳**：4MB 输出分 85 帧在主线程同步发送，期间不读服务端帧 | `main.py:84-101`；`ws.py:200-214` | 心跳推迟 → 服务端 4404 关连接 → 结果发不完。输出越大越易触发 |
| **12** | **未提交重构 + 三套路径描述** | `git status`（MVC 重构仍在暂存区）；AGENTS.md 写 `server/src/kk_server/`、README 写 `server/kk-server/`、实际是 `server/src/` | `src` 直铺 + hatch sources 重映射是脆弱方案（最近两个提交都在修打包）；文档多处错路径 |

### 🟡 P2 — 技术债

13. 清理线程无 shutdown：`app.state.shutdown` 存了 Event 但无人 `.set()`（`main.py:51-61`）
14. `server/Dockerfile` 用 `pip` 手装依赖并手工 `COPY src ./kk_server`，与 pyproject/uv.lock 脱节
15. `_outbuf` 内存态，进程重启丢失；超 1000 条丢弃最旧累积输出，DB 的 `out` 就此截断且无标记（`store.py:199-210`）
16. 协议无压缩：心跳帧含 procs_top/users/disks，实测远超文档承诺的 2KB
17. 无幂等设计：重连补发可能重复执行有副作用的 shell 命令
18. Hub 异常被 `log.debug` 吞掉，生产排障看不到连接失败原因（`hub.py:124`）
19. 无 `/metrics`、无结构化日志、无连接数/命令成功率可观测面板
20. `scripts/loadtest.py`、`bench_agent.py` 未纳入 CI，M4「1000 连接压测」无回归保障

---

## 4. 优化方案

### 阶段一 · 止血（1–2 天，不动架构）

| 动作 | 改动 | 验证 |
|---|---|---|
| **A1** 修复自更新顺序 | `apply_manifest` 改为**先判形态再下载**：非 frozen 二进制形态直接 `return False` 并告警，绝不落盘。用 `sys.frozen` 判断，不用文件名启发式 | 新增测试：`agent_bin` 缺省时调用，断言 `sys.executable` **未被修改** |
| **A2** 补命令输出 | `list_commands` 增加 `out_tail`（末 2KB）；新增 `GET /api/commands/{cid}/out` 返回完整输出 | 集成测试断言控制台能拿到 `kk-ok` |
| **A3** 命令结果归属校验 | `hub.agent_endpoint` 处理 `cmd_result` 时校验 `get_command(id).pod == 当前连接 pod`，不匹配则丢弃并审计 | 新增测试：A 连接回传 B 的命令 id |
| **A4** 存储回收 | `cleanup` 增补：`hourly` 保留 90 天；清理 `status='lost'` 命令；大表 DELETE 分批（`LIMIT 5000` 循环） | 单测：构造 2 天前 `lost` 命令，断言被清理 |
| **A5** 固化单进程约束 | 启动时检测 worker 数，非单实例则告警；README/Dockerfile 写明不支持多 worker | 人工验证 |
| **A6** 修正文档路径并落地重构 | 统一为 `server/src/`（包名 `kk_server`）；提交暂存的 MVC 重构 | `grep -r "kk_server/"` 无错路径残留 |

### 阶段二 · 加固（1–2 周）

| 动作 | 要点 |
|---|---|
| **B1 命令状态机** | 引入 `cmd_ack` 帧：`pending → sent → acked → running → done`。重连补发 `sent` 但未 `acked` 的命令；超 `timeout+30s` 未 ack 自动置 `timeout`。彻底消灭「石沉大海」 |
| **B2 解除事件循环阻塞** | ① Store 操作包 `run_in_threadpool`（改动最小，先止血）；② 或迁移 `aiosqlite` + 写队列（治本）。同时加 `PRAGMA busy_timeout`，REST 路由统一 `def`（线程池）语义 |
| **B3 前端增量化** | 容器列表加 `?view=summary`（跳过 `json.loads(last_metrics)`）；摘要字段冗余进 `containers` 表列；加 ETag + `If-None-Match`；轮询按页面活跃度退避 |
| **B4 自更新可信化** | 上传端 ed25519 签名，Agent 内置公钥验签；灰度（按 pod 名哈希分 10 批）；新增 `POST /api/system/agent/rollback` 恢复 `.prev` |
| **B5 身份绑定** | token 与 pod 命名前缀绑定（构建期注入时锁定），拒绝不匹配的 hello 并审计告警 |
| **B6 发送背压** | 大输出分帧改为可中断：每帧后回主循环 `select` 一次优先保心跳；或服务端对正在回传结果的连接放宽 4404 |
| **B7 安全加固** | 登录失败按 IP + 用户名滑动窗口限流（5 次/5 分钟）；审计补充来源 IP |

### 阶段三 · 演进（1–2 月，按规模触发）

| 动作 | 触发条件与要点 |
|---|---|
| **C1 Hub 连接注册表抽象** | 需要多实例时。定义 `ConnRegistry` 接口，内存实现不变，Redis 实现用 pub/sub 转发命令帧 |
| **C2 指标分层存储** | 热数据内存环形缓冲（1h）／温数据 SQLite 明细（24h）／冷数据 `hourly`（90d），或评估 VictoriaMetrics |
| **C3 协议 v2** | permessage-deflate 压缩；帧级 HMAC；命令幂等键防重复执行；`proto_ver` 双版本共存过渡 |
| **C4 前端工程化** | 引入构建 + 由 OpenAPI 生成 TS 类型。当前单文件 361 行已接近可维护上限 |
| **C5 可观测性** | 暴露 `/metrics`（连接数、心跳延迟分位、命令成功率、DB 写入耗时）、结构化 JSON 日志 |

---

## 5. 优先级决策依据

- **先修 1（自更新）**：唯一可能造成**不可逆数据破坏**的缺陷，修复成本 < 20 行。
- **先修 2（同步 IO）**：唯一会让系统在**设计目标规模**（1000 容器）下自我雪崩的缺陷。
- **先修 3、4**：安全与功能正确性，直接影响可信度。
- **B 阶段看规模**：目标为百级容器时，阶段二可只做 B1/B2/B3；B4/B5 视合规要求决定。
- **C 阶段按需**：在明确要扩容到多实例或万级容器前，不要提前引入 Redis 与协议 v2。

---

## 6. 建议的最小验证闭环

```
新增测试（阶段一）
├─ test_updater: agent_bin 缺省时 sys.executable 不被覆盖      ← 防回归（当前零覆盖）
├─ test_hub: A 连接回传 B 的命令 id 被拒                        ← 安全
├─ test_store: lost 命令与过期 hourly 被清理                     ← 存储
└─ test_integration: 控制台能看到命令输出                        ← 功能

纳入 CI
└─ scripts/loadtest.py 作为 nightly，断言 1000 连接下心跳零 4404
```

---

## 7. 结论

骨架是对的，血肉撑不住生产规模。反向长连接、纯标准库 Agent、协议契约先行、双端独立单测——这四件事做对了，是这个项目最值钱的部分。

当前有 **4 个阻断性缺陷**（其中自更新顺序问题能直接摧毁容器内的 Python 解释器），且在「1000 容器」这个设计目标上，**服务端的同步 IO 模型与轮询式前端会先于 Agent 资源瓶颈崩塌**——这一点值得特别注意，因为项目的全部资源优化都投在了 Agent 侧。

按阶段一清单止血（约 1–2 天）后，系统可安全支撑百级容器；完成阶段二后可稳定支撑千级。
