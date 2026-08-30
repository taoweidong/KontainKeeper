# KontainKeeper 架构分析报告

> 分析日期：2026-08-30
> 范围：`agent/`（kk-agent）、`server/`（kk-server）、`proto/`、`tests/`、`scripts/`
> 依据文档：`README.md`、`docs/design.md`、`proto/messages.md`、`AGENTS.md`

> **结构说明（2026-08-30 重构后）**：本报告基于重构前的平铺模块结构撰写（如 `agent/kk-agent/kk_config.py`、`agent_main.run()`、`kk_ws` 等）。重构后 `agent` 已成为独立 Python 项目，源码包为 `agent/kk_agent/`（`config.py`/`main.py`/`ws.py`/`conn.py`/`collector.py`/`executor.py`/`plugin_loader.py`/`logutil.py`），并通过 PyInstaller 编译为单文件二进制直接嵌入容器。功能与协议完全等价，文中问题定位与优化建议仍然成立。

---

## 1. 项目意图识别

**一句话定位**：在 K8S 集群的 vscode-server 容器 IDE 中，提供一条**绕过 K8S 集群能力、不触碰宿主机、对用户无感知**的直连管理与数据采集通道。

**核心设计决策（已从代码确认与文档一致）**：
- **反向通道**：容器内的 Agent 主动出站 WebSocket 长连服务端（唯一通道），规避"容器无入站、不能动宿主机、不能用 K8S Service/Ingress"的约束。
- **资源纪律**：Agent 强制纯 Python 标准库（无 psutil/asyncio），单线程 `select` 事件循环，工作（采集/命令/插件）在一次性 daemon 线程跑，结果经 `queue.Queue` 回主线程发帧——保证 **socket 只被主线程触碰**。
- **身份与鉴权**：镜像构建期注入 `KK_TOKEN`；服务端校验 `token + proto_ver`，连接按 `pod(hostname)` 关联历史。
- **安全护栏**：命令走 argv 数组直传 `exec`（不经过 shell 拼接），服务端统一黑名单 + 全量审计；WSS。
- **部署无损**：`entrypoint-wrapper.sh` 后台拉起 Agent（崩溃 5s 监督重启）+ `exec` 原 vscode-server 启动命令。

**意图达成度**：核心意图已被代码忠实实现，且工程质量高于同类脚本——尤其是 Agent 的资源约束与"socket 线程安全"处理得相当到位。

---

## 2. 架构分层总览

```
┌─ 管理端 ────────────────────────────────────────────────┐
│ FastAPI(app.state: store / hub / cmd_blacklist)          │
│  ├─ REST /api/*（containers/commands/audit/auth/tokens）  │
│  ├─ WS   /ws/agent  ← Hub.agent_endpoint（async）         │
│  └─ Static /  → web/ 单页管理界面（hash 路由 + 轮询）     │
│ Store(SQLite WAL + 全局 threading.Lock)                   │
└───────────────▲ 出站 WebSocket（容器→服务端）──────────────┘
┌─ 容器内 Agent（纯 stdlib，常驻 RSS <15MB）──────────────┐
│ agent_main.run()：select 事件循环 + 队列回传             │
│  kk_ws(自研 RFC6455) / kk_conn / kk_config               │
│  kk_collector(/proc) / kk_executor(subprocess)           │
│  kk_plugins(热加载) / kk_logutil(1MB 轮转)               │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 架构优点（先肯定）

1. **反向隧道思路正确且优雅**：在不依赖 K8S 的前提下实现了"管理面可达容器"，约束与方案自洽。
2. **Agent 资源纪律到位**：纯 stdlib、`select` 单线程、工作线程结果经队列回主线程发帧——`socket` 永不跨线程，避免了最经典的并发 Bug。
3. **协议契约清晰**：`proto/messages.md` 定义了帧类型、字段来源、关闭码语义（4401/4402/4403）、分块回传与离线补发，并有 `proto_ver` 版本协商。
4. **测试质量高**：`test_integration.py` 用真实 uvicorn + 真实 Agent 主循环 + 伪造 `/proc`（`conftest.make_fake_fs`）做端到端验证，且等待条件同时检查 `metrics.mem_mb 非空`（符合 AGENTS.md 约定）；压测 `loadtest.py`、资源基线 `bench_agent.py` 齐备。
5. **安全基础扎实**：`verify_admin` 用 `secrets.compare_digest`（常数时间）；命令 argv 直传 `exec` 天然避免 shell 注入；黑名单 + 审计 + 二次确认链条完整。
6. **部署无损**：`entrypoint-wrapper.sh` + 监督循环设计合理，用户态 vscode-server 启动行为不变。

---

## 4. 架构问题与风险（按严重度排序）

### 🔴 高（影响规模化 / 功能正确性 / 安全部署）

**H1. 阻塞式 SQLite 跑在 async 事件循环上（核心扩展性瓶颈）**
- `store` 的全部方法都是**同步**的，且共用一把全局 `threading.Lock`；`hub.agent_endpoint`（async）与所有 API 处理器**直接在事件循环里调用** `record_hb / append_result / 各种查询`，每次都 `acquire lock → 写 → commit`。
- 后果：单进程单事件循环下，任何一次 DB 提交都会**阻塞整个 loop**；所有连接串行化。
- 更尖锐的是 `store.cleanup()` → `_aggregate_hours()`（每 300s 跑）对**每个过去小时 × 每个 pod** 做多次小查询（`self._query` 反复拿锁/放锁），在规模下（M4 目标 1000 连接）会造成明显的延迟尖刺，且聚合期间与事件循环争锁。
- 影响：直接威胁文档中 M4「1000 连接压测通过」的目标。

**H2. `entrypoint-wrapper.sh` 原 entrypoint 透传存在 Bug**
- 脚本里 `exec $KK_ORIG_ENTRYPOINT` 是**未加引号的变量展开 + 词分割**：`KK_ORIG_ENTRYPOINT` 是 `docker image inspect` 拿到的 JSON 串，如 `["/usr/local/bin/code-server","--flag"]`。`exec` 后得到的是字面量 `[/usr/local/bin/code-server",` 等碎片，**首个 token 不是可执行文件**，原 entrypoint 直接失效。
- 仅当基础镜像 ENTRYPOINT 为 `null`（很多镜像把启动逻辑放在 CMD）时走 `exec "$@"` 才正常。凡是**显式定义了 ENTRYPOINT 的基础镜像，叠加后容器启动会失败**。
- 配套缺陷：`scripts/build.sh` 生成 `CMD ${ORIG_CMD}`，当 `ORIG_CMD` 为 JSON `null` 时得到非法 `CMD null`，`docker build` 报错。

**H3. TLS 未终结且未强制（安全部署陷阱）**
- `server/Dockerfile` 直接 `python -m kk_server`（KK_PORT=8443），**无任何 TLS/证书配置**；`main.py` 的 uvicorn 也未启用 ssl。
- 而文档要求 Agent 用 `wss://`——这意味着生产必须前置一个 TLS 终止的反代（nginx/LB），否则 `wss://` 连不上；若有人图省事改用 `ws://`，**hello 中的 token 将以明文传输**。
- README/部署章节未把"必须 TLS 终止、禁止 `ws://` 直连暴露"作为硬约束写明。

### 🟠 中

**M1. Agent 自研 WebSocket 实现（~260 行 RFC6455）**
- 正确性/安全/维护风险集中点：手搓握手、掩码、分片、ping/pong、close。不支持扩展/压缩；握手校验用 `if "101" not in hlines[0]`（子串弱校验，HTTP 1010 之类也会蒙混）。
- 这是为"纯 stdlib + 低内存"做的刻意取舍（可接受），但需要配套更强的协议模糊测试与明确边界说明。

**M2. 命令黑名单是子串匹配，可被绕过/误伤**
- `commands.py` 把 argv 转小写拼接后做 `任意子串 in joined`。`rm -rf  /`（双空格）、`r\m -rf /`、`echo rm -rf /` 等既能绕过也能误伤合法命令。
- 本质上这是"护栏"而非"强安全"（真安全来自仅管理员可下发 + 审计）。建议在文档中明确其护栏性质，并升级为结构化规则（argv[0] 精确/前缀匹配 + 危险参数）。

**M3. 缺少连接级 keepalive / 半开连接检测**
- Agent 从不主动发 WS ping，也不设置 TCP keepalive；服务端也不发 WS ping 探活。`conns[pod]` 的存活完全依赖下一次业务心跳。
- 风险：防火墙/代理静默丢包后，服务端 `conns` 可能保留"半开"条目，`try_dispatch` 会向死 socket 发送，要等发送失败才清理。

**M4. 单进程内存态 Hub，无法水平扩展 / HA**
- `Hub.conns`、`hub.tokens` 都在单进程内存里。设计上假设单实例（文档已写"单进程即可管理数千长连接"），但这也意味着无法多副本部署，断服即全盲。
- SQLite 单文件同样绑定单实例。若未来要求 HA，需要引入外部状态（Redis/PG + 共享连接注册表）。

**M5. 登录无速率限制 / 会话不滑动**
- `/api/login` 失败仅写审计，无失败锁、无限流，存在暴力破解风险。会话固定 12h 不刷新。

**M6. 批量命令下发非原子，可能留下孤儿 pending**
- `create_commands` 循环里：对每个 pod 先 `get_container` 校验，再 `create_command` + `try_dispatch`。若第 2 个 pod 不存在（404），前序 pod 已创建/已下发的命令不会被回滚，返回 404 时留下部分已派发的命令。

**M7. `metrics_series` 跨 24h 边界数据接缝**
- `hours<=24` 走 `heartbeats` 原始表，`>24` 走 `hourly` 聚合表。两者拼接点在"当前未结束的小时"，可能漏掉或重复最近一小时（且 `hourly` 仅在每 300s 清理时聚合，存在延迟）。

**M8. 列表接口无缓存/分页**
- `_container_view` 每次请求都 `json.loads(last_metrics)`（1000 pod = 1000 次解析）；`/api/containers` 全量返回，无分页。

### 🟡 低

- **L1 文档与代码漂移**：design.md §3.3 写 `python3 -OO /opt/kk-agent/__main__.py`，实际 wrapper 跑的是 `agent_main.py`；`entrypoint-wrapper.sh` 里的 `KK_ORIG_ENTRYPOINT` 分支未在 design.md 体现。
- **L2 二进制命令输出损坏**：`append_result` 把 base64 输出按 `utf-8 / replace` 解码再存文本，二进制结果会被替换字符污染。
- **L3 聚合查询过碎**：`_aggregate_hours` 在全局锁下做 `O(小时数 × pod 数)` 的小查询，规模下放大锁竞争。
- **L4 `KK_ORIG_ENTRYPOINT` 经 ENV 传 JSON 字符串**：容器内含空格/特殊字符的 entrypoint 解析脆弱。

---

## 5. 优化建议（按优先级）

### P0（必须修，否则卡规模化/上线）
1. **DB 写入移出事件循环**
   - 方案 A（推荐）：引入 `aiosqlite`，把 `Store` 改成 async，所有调用 `await`。
   - 方案 B（改动最小）：心跳/命令结果写入走**独立 writer 线程 + 内存队列**（fire-and-forget），事件循环只 enqueue；API 读走 `run_in_threadpool`。
   - 同时把 `_aggregate_hours` 改为**游标批处理 + 限流分片**，避免在清理线程里长时间持锁。
2. **修复 entrypoint 透传**
   - `build.sh` 对 `null` ENTRYPOINT/CMD 做兜底（ENTRYPOINT 为空则不改写、CMD 为空则省略 `CMD`）。
   - `entrypoint-wrapper.sh` 用 `python -c` 或 `jq` 把 JSON entrypoint 解析为 argv 数组再 `exec "$entrypoint_arr[@]"`；或干脆把原 entrypoint 透传成一个可执行 wrapper，避免词分割。
3. **强制 TLS 终止**
   - 服务端部署文档明确"必须前置 TLS 反代，禁止 `ws://` 直连"；可选地，Agent 端拒绝 `ws://`（仅允许 `wss://`），并在 `main.py` 增加"若 `KK_REQUIRE_TLS` 且未走反代则告警"。

### P1（强烈建议）
4. **加连接级探活**：Agent 周期发 WS ping（或开 TCP keepalive）；服务端定时 WS ping 并清理无响应 `conns`。
5. **黑名单升级**：改为结构化规则（argv[0] + 危险参数白/黑名单），并在 UI/文档标注"仅为护栏，强安全靠鉴权+审计"。
6. **登录加固**：失败计数 + 临时锁/限流；会话支持滑动过期。
7. **批量命令原子化**：先校验所有 pod 存在、再统一创建（用事务或预校验列表），失败整体回滚。

### P2（打磨）
8. **可扩展性准备**：连接注册表外置（为 HA 留口）；指标查询加缓存层；`/containers` 加分页；修 `metrics_series` 边界接缝。
9. **Agent WS 健壮性**：对 `kk_ws` 补协议模糊测试（畸形帧/超大帧/分片边界）；二进制输出改为 base64 透传不解码。
10. **文档与代码对齐**：修正 design.md 的 `entrypoint-wrapper` 描述，补全 TLS 部署约束与黑名单语义说明。

---

## 6. 结论

KontainKeeper 的**架构思路与意图高度自洽**，在"不依赖 K8S、用户无感知、低资源"这三条硬约束下给出了优雅的反向通道方案，代码工程质量（尤其 Agent 的资源纪律、测试覆盖）明显优于同类脚本。

当前**最关键的三个工程风险**集中在：**(H1) 阻塞式 SQLite 跑在 async 事件循环上的扩展瓶颈、(H2) entrypoint 透传词分割 bug、(H3) TLS 未终结/未强制的部署安全陷阱**。这三项不解决，会分别卡住规模化压测目标、让部分基础镜像叠加失败、以及在公网暴露时泄露 token。

优化路径清晰：**先把 DB 访问移出事件循环并修复 entrypoint/TLS 三处 P0，再补探活、黑名单、登录限流等 P1 加固**，即可在保持现有简洁架构的前提下支撑到千级容器规模。
