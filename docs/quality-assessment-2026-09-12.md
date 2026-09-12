# KontainKeeper 项目质量分析报告（目标达成度专项）

> 分析日期：2026-09-12　代码基线：`main` @ d6a0ab0（工作区干净）
> 分析范围：`agent/src/kk_agent/`（1517 行）、`server/src/kk_server/`（1310 行）、`web/src/`（5 个业务页）、`deploy/`、`proto/messages.md`
> 方法：全量源码走查 + 关键路径实测 + 测试基线复跑 + 与既定设计文档交叉比对
> 交付性质：**分析报告，未改动任何代码**。缺口修复方向见 §3。

---

## 0. 结论速览

**总体判断：7 项验收目标，5 项完全达成，1 项基本达成但缺硬隔离，1 项未达成。**

| # | 目标 | 达成度 | 关键证据 | 缺口 |
|---|---|---|---|---|
| 1 | 客户端定时上报数据 | ✅ **达成** | `main.py:208-221` 单调钟调度 + ±10% 抖动 + busy 重入闸门；8 项 psutil 采集；实测心跳帧 1.72KB | 服务端无法强制收敛上报间隔（`KK_ENFORCED_INTERVAL` 悬空） |
| 2 | 服务端支持下发命令 | ✅ **达成** | `POST /api/commands` 三类 kind；QoS1 下发；黑名单 + 全量审计；端到端集成用例覆盖 | — |
| 3 | 服务端支持批量下发命令、回收结果 | ✅ **达成**（规模缺口） | `pods` 数组 + 单事务批量建行；result 48KB 分块 / 4MB 封顶；seq 水位幂等去重；`asyncio.Lock` 保序；SQL 超时清扫器收敛 | 发布回路仍是 N+1 单查；结果列表无分页，500 台批量后只能看 100 条 |
| 4 | **支持结果导出** | ❌ **未达成** | 全仓零导出接口、零导出按钮 | 见 §3 P0-1 |
| 5 | 内网安全执行 | ✅ **达成**（边界明确） | `KK_AGENT_IPS` 白名单 + 生产自检；结构化黑名单；结果归属校验；自更新 sha256/HMAC；审计全覆盖 | 白名单基于自报 IP（内网内可伪造）；管理端 TLS 需前置 |
| 6 | 客户端资源消耗与限制、不影响已有服务 | ⚠️ **基本达成** | 有界线程池、读取侧输出封顶、插件超时隔离、进程组回收、离线队列溢出回 `rc=-3` | **无 OS 级硬隔离**（无 nice / cgroup / RLIMIT），靠代码自律而非内核约束 |
| 7 | 服务端数据存储与 Web 展示 | ✅ **达成** | 三库 async（SQLite/PG/MySQL）；分层保留 2d/90d/30d/7d；5 个业务页 + ECharts | PG/MySQL 未连真实库验证 |

**工程质量基线（本次实测）**

| 指标 | 实测值 | 判定 |
|---|---|---|
| 后端用例收集数 | 202（agent 96 + server 106） | — |
| 本机执行结果（无 Broker） | **198 passed, 4 skipped**（158s） | ✅ 与 `AGENTS.md` 基线一致 |
| 心跳帧尺寸（本机真实采集） | **1760 字节 / 1.72KB**（3 磁盘、5 进程） | ✅ 优于设计承诺的 2–4KB |
| 单轮 `collect()` 耗时（Windows） | 2.5s（`proc` 项双采样 0.3s + 逐进程句柄为主） | ✅ 60s 间隔下占空比 4%；Linux 远快于此 |
| 缺陷账本闭环率 | P0/P1/R1–R13 **全部闭环**，仅 `P2-14` 未开始 | ✅ |

---

## 1. 评估方法与证据来源

| 手段 | 具体做法 | 产出 |
|---|---|---|
| 源码走查 | agent / server / web 全量通读，逐项对齐需求 | 每条结论附 `文件:行号` |
| 测试基线复跑 | `.venv/Scripts/python.exe -m pytest agent/tests server/tests -q` | 198 passed / 4 skipped |
| 关键路径实测 | 在本机真实执行 `collector.collect()`，度量帧尺寸与耗时 | 1.72KB / 2.5s |
| 交叉比对 | `AGENTS.md`、`docs/design.md`、`proto/messages.md`(v3)、`docs/architecture-review.md` §0 缺陷总表 | 识别文档与代码失配项 |
| 缺口验证 | 对「结果导出」「分页」「强制间隔」等诉求做全仓 grep 反证 | 确认缺失而非遗漏走查 |

---

## 2. 逐项目标评估

### 2.1 客户端定时上报数据 —— ✅ 达成

**实现要点**

| 维度 | 实现 | 位置 |
|---|---|---|
| 调度 | 单线程事件循环，`time.monotonic()` 单调钟（防 NTP 回拨停摆/集中补发） | `main.py:211-215` |
| 打散 | `interval × random.uniform(0.9, 1.1)` 真随机抖动，500 台峰值解耦 | `main.py:215` |
| 重入 | `busy` 事件闸门，上轮未完成不叠加 | `main.py:131-133` |
| 采集 | 8 项独立函数（cpu/mem/disk/disk_io/net/proc/user/sys），单项异常返回 None 不拖垮整帧 | `collector.py:237-266` |
| 裁剪 | `KK_HB_ITEMS` 可精简采集项（千进程主机可去掉 `proc`） | `config.py:53` |
| 传输 | paho 后台网络线程；`hb` QoS0 且**不 retain**（避免服务端重启回放幽灵心跳） | `transport.py:238-250` |
| 容错 | 单指标类型错误记 NULL，不让整帧丢弃 | `store.py:184-192` |

**证据**：实测帧 1760 字节，含 `cpu_per_core`、`disks`(3)、`net`、`procs_top`(5)、`users`、`sys` 全量字段。设计文档承诺 2–4KB，实测更小。

**遗留**：服务端 `Settings.enforced_interval`（`config.py:20/88-89`）被解析后**全仓零消费点**，Agent 侧也不读该键。含义是：服务端目前**无法强制收敛** Agent 的上报频率——若某台机器被误配为 `KK_INTERVAL=1`，服务端只能被动承受其上报压力（500 台规模下是真实风险）。

---

### 2.2 服务端支持下发命令 —— ✅ 达成

| 能力 | 实现 | 位置 |
|---|---|---|
| 下发接口 | `POST /api/commands`，`pods` 数组 + `kind` 三类 | `commands.py:85-125` |
| 三类命令 | `shell`（argv 直传 / cmdline 经 sh -c）、`collect`（按项采集，白名单校验）、`plugin_reload` | `commands.py:43-82` |
| 传输 | `kk/v1/{host}/cmd` QoS1，离线由 Broker 持久会话排队补投 | `mqtt_bridge.py:247-280` |
| 安全 | 结构化黑名单（程序名 + 高危参数，`use_shell` 形态先切分再校验）+ 审计 `command_create`/`command_blocked` | `commands.py:107-124` |
| 前端 | 命令面板：目标主机多选、cmdline/argv 双形态、超时配置 | `views/command/shell/index.vue` |
| 验证 | 集成测试覆盖「真实 Agent + 真实 Broker + 真实 uvicorn」的命令回传链路 | `server/tests/test_integration.py` |

**评价**：这是需求缺口 R4（`collect`/`use_shell` 服务端不可达）修复后的完整闭环，链路可从 `Agent → Broker → DB → 前端` 全程走通。

---

### 2.3 批量下发命令 + 回收结果 —— ✅ 达成，但存在规模化缺口

**做对的部分**

| 维度 | 实现 | 位置 |
|---|---|---|
| 批量建行 | 单事务 + `executemany` 参数列表，500 台一次插入 | `store.py:307-317` |
| 存在性校验 | 一次 `IN` 查询（400 分片避开变量上限）取代 N 次单查 | `store.py:231-242` |
| 前置校验 | 先全量校验再下发，避免「部分已下发、部分 404」的状态撕裂 | `commands.py:98-100` |
| 结果分块 | 48KB/块，总量 4MB 封顶并置 `truncated` | `main.py:26,36-63` |
| 落库 | base64 在 SQL 侧拼接累加（无进程内缓存、重启不丢） | `store.py:348-386` |
| 幂等 | `last_seq` 水位去重，QoS1 重投不导致输出翻倍 | `store.py:369-374` |
| 保序 | 按命令 ID 加 `asyncio.Lock`，修复并发乱序丢块（R13） | `mqtt_bridge.py:220-241` |
| 归属校验 | A 主机回传 B 主机的结果被丢弃并审计 | `mqtt_bridge.py:229-234` |
| 终态收敛 | 分块失败补发 `rc=-3` 失败终态 + SQL 超时清扫器（30s 周期） | `main.py:29-33`、`store.py:388-400` |
| 归属可见 | `result_mismatch` / `result_unknown_cmd` 落审计 | `mqtt_bridge.py:227-233` |

**缺口（影响 500 台规模下的可用性）**

| 编号 | 问题 | 证据 | 后果 |
|---|---|---|---|
| G1 | **发布回路仍是 N+1 单查** | `commands.py:116-121`：`for cid, pod in zip(...)` 内 `await store.get_command(cid)` 逐条单查 | 建行已批量化，但发布仍是 500 次 DB 往返 + 500 次 publish。SQLite 本地约 0.5s 内可接受；若换 PG/MySQL 走网络，单次 500 台点击会阻塞事件循环 1s 以上 |
| G2 | **结果列表无分页** | 后端 `list_commands(pod, limit)` 无 `offset`（`store.py:322-329`）；前端固定 `limit: 100`（`CommandHistory.vue:35`） | 500 台批量下发后历史页**只能看到最近 100 条**，无法追溯其余 400 台的结果 |
| G3 | **无「按下发批次聚合」视图** | 无 batch/batch_id 概念，`create_commands_batch` 不落批次标识（`store.py:307-317`） | 批量下发后想整体核验「500 台里成功多少、失败多少」，只能靠状态筛选后人工计数，无法按一次操作聚合检索 |

---

### 2.4 支持结果导出 —— ❌ 未达成

**反证过程**：对 `export` / `csv` / `xlsx` / `download` / `导出` 做全仓 grep（排除构建产物），命中项全部无关：

| 命中 | 实际含义 |
|---|---|
| `agent_update.py:95` `/agent/download` | Agent 二进制自更新下载，与命令结果无关 |
| `main.py:36` 注释「审计导出」 | **注释与实现不符**：`audit.py` 仅有 `GET /api/audit` 列表接口，无导出 |
| 前端 `export const` | 均为 ES 模块语法，非导出功能 |

**现状**：唯一的输出获取通道是 `GET /api/commands/{cid}/out`（`commands.py:150-161`），返回单条命令的纯文本 / base64。该接口需要 `Authorization: Bearer` 头，浏览器直接访问会 401，无法作为「下载」使用。

**结论**：命令结果、采集结果、审计日志、主机指标四类数据**均无导出能力**，且无批量导出。这是本次评估中唯一完全未达成的目标项。

---

### 2.5 内网安全执行 —— ✅ 达成（边界需明确）

**已落地的防线**

| 层 | 机制 | 位置 |
|---|---|---|
| 接入管控 | `KK_AGENT_IPS` 白名单（IP/CIDR 混合），`_on_message` 单一入口校验 status/hb/result 三类帧，名单外拒收并审计 `ip_rejected` | `mqtt_bridge.py:156-162` |
| 生产自检 | `KK_ENV=production` 时：默认口令 `admin123` 或空白名单 → **拒绝启动** | `config.py:95-104` |
| 协议一致性 | `status` 帧 `proto_ver` 不匹配拒收并审计 | `mqtt_bridge.py:191-198` |
| 命令准入 | 结构化黑名单（程序名 + 高危参数），`use_shell` 形态先按 shell 语义切分再校验（修 P0-1） | `commands.py:104-112` |
| 命令归属 | 结果帧校验 `cmd.pod == 上报 host`，跨主机回传丢弃 + 审计 | `mqtt_bridge.py:229-234` |
| 自更新完整性 | sha256 强制校验 + 可选 HMAC-SHA256；下载按**真实 TCP 源 IP** 校验白名单；先判形态后下载（防覆盖解释器，修 P0-1） | `updater.py:186-220`、`agent_update.py:78-97` |
| 管理端鉴权 | 会话 token（12h）；登录失败限流（5 次 / 300s 锁定 + 审计） | `auth.py:15-63` |
| 审计 | `command_create` / `command_blocked` / `ip_rejected` / `proto_mismatch` / `result_mismatch` / `login_*` / `agent_upload` 全落库 | `store.add_audit` 各调用点 |
| 部署 | Broker 匿名 + `persistence` 持久化；生产 compose 强制从 `.env` 注入口令与白名单 | `deploy/mosquitto/mosquitto.conf`、`docker-compose.prod.yml` |

**必须明确的边界（非缺陷，是设计取舍）**

1. **白名单基于 Agent 自报 IP**。MQTT 经 Broker 中转拿不到发布者真实 TCP 源 IP，因此 `kk/v1/{host}/hb` 的 `ip` 字段是自报值——**内网内可伪造**。文档已在 `proto/messages.md` §2.1 与 `design.md` §7 诚实标注，唯一不可伪造的边界是网络层限制 1883 端口可达范围。若威胁模型包含「内网横向移动」，此防线不足。
2. **命令执行无白名单，只有黑名单**。前端 cmdline 恒 `use_shell=true`，整条命令经 `sh -c`。黑名单（默认 6 条规则）是「减害」而非「隔离」，绕过路径客观存在（如变量拼接、十六进制转义）。`KK_ALLOW_SHELL=0` 可彻底关闭 shell 形态，但需手动配置。
3. **命令以 Agent 进程身份运行**，无 chroot / 无 seccomp / 无独立 uid 降权。
4. **管理端无 TLS 强制**。生产需前置 TLS 终结（文档已注明为硬约束），代码层不拦截明文 HTTP。
5. **登录限流只按用户名、内存态**（`auth.py:15-16`），重启清零，无 IP 维度——`architecture-review.md` B7 建议的「IP + 用户名滑动窗口」未完全落地。

---

### 2.6 客户端资源消耗与限制、不影响已有服务 —— ⚠️ 基本达成，缺硬隔离

**已做对的部分（这一层做得相当扎实）**

| 措施 | 实现 | 位置 | 保护的资源 |
|---|---|---|---|
| 线程模型 | 主循环单线程；MQTT socket 只由 paho 网络线程触碰；采集/命令在 daemon 线程 | `main.py:1-10` | 避免并发 bug，压住 RSS |
| 线程有界 | 固定 `max_workers=8`（上限 64）daemon 线程池，队列阻塞，**不会因批量命令无界建线程** | `executor.py:146-169` | 线程数 / 调度开销 |
| 采集重入 | `busy` 闸门，上轮采集未完成不叠加 | `main.py:131` | 峰值 CPU |
| 输出封顶 | **读取侧**封顶 `max_out=4MB`，达上限后继续排水但不保留——旧实现 `communicate()` 全量缓冲会让 `cat /dev/zero` 撑爆 Agent（资源评审 P1） | `executor.py:87-112` | 常驻内存 |
| 子进程回收 | `start_new_session` 独立进程组；POSIX `killpg` 先 TERM 后 KILL，Windows `taskkill /F /T`；超时清理整棵进程树 | `executor.py:39,49-85` | 防孤儿进程 |
| 插件隔离 | `collect()` 超时 5s（`KK_PLUGIN_TIMEOUT`）即 quarantine 到 mtime 变化，防逐心跳泄漏执行线程（资源评审 P2） | `plugin_loader.py:19-79` | 线程 / 心跳可用性 |
| 离线队列 | `max_queued=512`，溢出回 `rc=-3` 失败终态而非静默丢弃 | `transport.py:38,152`、`main.py:61-62` | Agent 内存 |
| 日志 | 1MB 轮转一份 | `entrypoint-wrapper.sh:rotate_log` | 磁盘 |
| 残留清理 | 启动时清 60 分钟前的 PyInstaller `_MEI*` 残留（被 SIGKILL 时不自动清） | `entrypoint-wrapper.sh:cleanup_stale_mei` | 磁盘 / inode |
| 行为不变 | `kk-entrypoint` 后台监管 + 前台 `"$@"` 透传原镜像入口，**容器生命周期 = 原 IDE 生命周期**，用户启动行为不变 | `entrypoint-wrapper.sh` | 已有服务可用性 |
| 崩溃自愈 | 监管循环崩溃 5s 拉起；信号转发避免 Agent 被 SIGKILL 丢下线帧 | `entrypoint-wrapper.sh:supervise/forward_signal` | 可用性 |

**实测数据**

| 项 | 值 | 说明 |
|---|---|---|
| 心跳帧 | 1760 字节 | 60s 一次 → 约 0.03 KB/s 上行 |
| 单轮 `collect()` | 2.5s（Windows 开发机） | `proc` 项双采样 0.3s + 逐进程句柄为主要成本；目标环境 Linux 的 `/proc` 读取远快于此 |
| 常驻线程 | 约 10（主 1 + paho 网络 1 + 线程池 8，池在首条命令时创建） | 空闲时线程池阻塞在队列 |
| 常驻 RSS | 文档口径 25–35MB（`design.md:75`） | **本次未实测**：`scripts/bench_agent.py` 需真实 Broker，本机 1883 无 Mosquitto |

**缺口：无 OS 级硬约束**

| 缺口 | 现状 | 风险 |
|---|---|---|
| 无 CPU/IO 优先级控制 | 未调用 `nice` / `ionice`；采集进程与用户 IDE **同优先级**竞争 | 单轮采集 2.5s（Linux 上虽更短）在 CPU 争抢场景下会与 IDE 抢时间片；无硬性上限 |
| 无内存硬上限 | 无 `RLIMIT_AS` / cgroup memory limit；4MB 输出封顶是**代码级**约束 | 并发 8 条命令各持 4MB 输出 + psutil 枚举开销，理论峰值可达数十 MB；靠代码自律而非内核兜底 |
| 与已有服务共享 cgroup | Agent 与 IDE 同容器同 cgroup，若 IDE 侧设有 CPU/memory limit，Agent 受同一配额，**无法独立核算** | 资源归因困难；IDE 侧限流会连带压制 Agent 心跳 |
| 首次部署无连接抖动 | 心跳有 ±10% 抖动，但**上线连接**无抖动 | 500 台同批启动时存在连接风暴（Broker 侧可承受，但无制度化缓解） |
| 无负载自适应 | 采集固定周期，无「系统高负载时降频」逻辑（如读 `/proc/loadavg` 退避） | 高负载机器上仍按 60s 固定开销采集 |

**结论**：在「不影响已有服务」这个诉求上，**行为层面已做到**（入口透传、生命周期绑定、开销以 MB/秒计），但**资源层面是软约束**——所有上限都写在 Python 代码里，没有内核级隔离。对常规 IDE 容器（CPU 充裕、IDE 本身占大头）不成问题；对资源紧张的容器，缺少兜底。

---

### 2.7 服务端数据存储与 Web 展示 —— ✅ 达成

**存储**

| 维度 | 实现 | 位置 |
|---|---|---|
| 多库适配 | SQLAlchemy 2 Core + async engine，`KK_DB_URL` 三选一；方言差异**只收在 `_upsert` / `_ensure_schema` 两处** | `store.py:113-138,53-79` |
| 热表优化 | SQLite WAL + `busy_timeout=5000` + `synchronous=NORMAL` | `store.py:45-48` |
| 摘要列 | `containers` 冗余 `cpu/mem_mb/disk_pct`，`?view=summary` 不解析 `last_metrics` | `store.py:194-203` |
| 分层保留 | 原始心跳 **2 天** → 小时聚合 **90 天** → 命令状态行 **30 天** → 命令输出**单独 7 天**清文本留状态行 | `store.py:528-554` |
| 大表回收 | 按主键分批删（三库通用，避开 `DELETE...LIMIT` 方言差异与长事务锁表） | `store.py:556-571` |
| 无迁移成本升级 | `_ADD_COLUMNS` + `_ensure_schema` 自动 ALTER（SQLite 查 PRAGMA，其余查 `information_schema`） | `store.py:53-79` |
| 时钟防污染 | 帧内 ts 明显越界回落服务器时间；`_aggregate_hours` 窗口夹在保留期内（防坏时钟拖死循环，R11） | `store.py:176-177,486-526` |
| 容量 | 500 台 ≈ 几百 MB/年（`design.md:201`） | 单机无压力 |

**Web 展示**

| 页面 | 能力 |
|---|---|
| 主机总览 | 10s 轮询（可选 5/10/30/关闭）、关键字过滤、仅在线/仅告警、多选、CPU 进度条、磁盘告警着色、批量采集 / 批量执行命令入口 |
| 主机详情 | ECharts CPU/内存曲线（6h / 24h / 7d，>24h 自动走 hourly 聚合表）、磁盘表、网卡表、Top 进程、登录用户、该机命令历史 |
| 命令面板 | 主机多选 + cmdline/argv 双形态 + 超时；下发后历史立即刷新 |
| 采集面板 | 采集项勾选（8 项白名单）+ 批量下发 |
| 执行历史（共用组件） | 5s 静默轮询、状态筛选（7 态）、输出末 2KB 预览 + 全量弹窗、`out_purged` 提示 |
| 审计日志 | 200/100/500 条切换、关键字过滤、动作类型着色 |
| 监测面板 | `GET /api/system/stats`：Broker 连通性、链路计数（hb/result/cmd_published/cmd_failed/rejected）、各表行数、命令状态分布 |
| 首页 | `getStats` + `getHealth` 消费 |

**遗留**

1. `out_tail`（末 2KB）随列表返回，5s 轮询 × 100 条 ≈ 200KB/次的响应体，500 台场景下可优化为「列表不带输出，点开再取」。
2. PostgreSQL / MySQL **只做了 DDL 与语句的跨方言编译校验，未连过真实库**（`design.md:222`、`architecture-review.md:51` 已诚实标注）。上线前需补真库跑测。
3. `list_commands` / `list_audit` 均无 `offset`，前端只能「条数切换」不能翻页。

---

## 3. 缺口清单与修复方向

### P0 — 阻断验收

| 编号 | 缺口 | 根因 | 修复方向（依赖感知） |
|---|---|---|---|
| **P0-1** | **结果导出完全缺失** | 需求项在设计文档与完成方案中**从未被登记**，`design.md`「前端五个业务页」与 `completion-plan` 阶段 D 的页面清单均无导出项——属需求追溯断链，非实现遗漏 | ① 后端加 `GET /api/commands/export?format=csv&pod=&status=&since=`（返回 `StreamingResponse` + `Content-Disposition`），复用 `list_commands` 的筛选条件；② 批量导出按 `cmd_id` 集合走 `IN` 查询，**输出字段导出走 `/out` 逐条**或提供「仅状态行」轻量模式（避免一次拉 500×4MB）；③ 前端在「执行历史」工具栏加「导出 CSV」按钮，`window.open` 不可用（需 Bearer），改用 `http.request` 拿 blob + `URL.createObjectURL` 落盘；④ 审计与指标序列同法各加一个导出端点 |

### P1 — 影响 500 台规模的可用性

| 编号 | 缺口 | 证据 | 修复方向 |
|---|---|---|---|
| **P1-1** | 批量下发发布回路是 N+1 单查 | `commands.py:118` `await store.get_command(cid)` 在 500 次循环内 | 建行时直接由 `ids + pods + payload` 组装发布帧（`dispatch_command` 只需 `id/pod/kind/argv/timeout`，无需回查）；或加 `store.get_commands_batch(ids)` 一次 `IN` 取回 |
| **P1-2** | 命令历史无分页，批量结果看不全 | 后端无 `offset`（`store.py:322`）；前端固定 `limit:100`（`CommandHistory.vue:35`） | 后端加 `offset` + `total`；前端接 `el-pagination`；配合 P1-3 的批次筛选 |
| **P1-3** | 无批量下发批次概念，无法聚合核验 | `commands` 表无 `batch_id` 列（`tables.py`） | 加 `batch_id` 列（登记 `_ADD_COLUMNS`，`_ensure_schema` 自动 ALTER，零手工迁移）；`create_commands_batch` 生成一个 batch_id 落所有行；新增 `GET /api/commands?batch=` 与前端按批次查看/导出 |
| **P1-4** | `KK_ENFORCED_INTERVAL` 悬空，服务端无法收敛上报频率 | `config.py:88-89` 解析后无消费点 | 二选一：**A**（推荐）删掉该死配置，避免误导；**B** 落地为：`status` 帧回带期望 interval，或新增 `kind=set_interval` 命令在下行帧下发，Agent 更新 `cfg["interval"]`。选 B 需同步 `proto/messages.md` 与双端 `PROTO_VER`（现为 3） |

### P2 — 加固与一致性

| 编号 | 缺口 | 说明 |
|---|---|---|
| P2-1 | 客户端无 OS 级资源隔离 | 建议在 `entrypoint-wrapper.sh` 拉起 Agent 时加 `nice -n 19`（或 `ionice -c3`，若镜像内有 util-linux）；这是**零代码**改动、对 IDE 侧只可能更有利。更强约束需 `RLIMIT_AS`（Agent 内 `resource.setrlimit`，POSIX 专属，Windows 需跳过） |
| P2-2 | 白名单基于自报 IP | 属设计取舍，不可在应用层根治。缓解：文档与部署清单中把「防火墙限制 1883 可达范围」列为**必做项**而非建议项 |
| P2-3 | 登录限流只按用户名、内存态 | 补 IP 维度（`_LOGIN_FAILS` 改双键）并用 `X-Forwarded-For` 时需谨慎（可伪造，仅作辅助） |
| P2-4 | 管理端无 TLS 强制 | 代码层可加：`KK_ENV=production` 时检测到明文 HTTP 请求头（`x-forwarded-proto != https`）即告警日志 |
| P2-5 | PG/MySQL 未连真实库 | 三库代码路径已写但零真库验证，属最大隐性风险。CI 加 `postgres:16` / `mysql:8` service 跑 `test_dialects.py` 的真实连接版本 |
| P2-6 | 压测工具未纳入 CI | `scripts/loadtest.py` / `bench_agent.py` 存在但无回归保障，「500 台零误判掉线」「RSS 达标」无自动验证。建议 nightly job |
| P2-7 | Agent 自更新无回滚接口 | 服务端保留 `.prev`（`agent_update.py:59-63`）但无 `POST /api/system/agent/rollback`；出问题只能手工重传旧包 |
| P2-8 | 文档与代码失配 | ① `AGENTS.md` 规定「前端轮询统一用 `setPoll()`/`clearPolls()`」，**该函数全仓不存在**，实际 5 处各自 `setInterval`（`monitor:65`、`detail:113`、`CommandHistory:45`、`welcome:109`、`longpress:36`）——要么补工具函数要么改文档；② `main.py:36` 注释「审计导出」与实现不符；③ `architecture-review.md` §1 表未随 §0 刷新（H1/M4/M5 标「未修」但实际已修） |

---

## 4. 风险与验证边界

| 类别 | 项 | 影响 | 建议 |
|---|---|---|---|
| 未验证 | PostgreSQL / MySQL 真实连接 | 换库后可能因排序规则、标识符大小写折叠、类型差异出问题 | 上线前必须补真库跑测 |
| 未验证 | Agent 常驻 RSS | 文档 25–35MB 为**预期值**，本次未实测 | 在目标 Linux 环境跑 `scripts/bench_agent.py`（需真实 Broker） |
| 未验证 | 500 台规模端到端 | 全部容量结论来自设计估算，无实测 | 用 `scripts/loadtest.py` 跑 500 连接 × 60s，断言心跳零误判、命令成功率 100% |
| 测试盲区 | 4 个集成用例在本机 skip | 真实 MQTT 链路无 CI 保障 | CI 用 `eclipse-mosquitto:2` service 容器 |
| 单点 | Mosquitto 是新增关键依赖 | Broker 故障 = 全平台失联（服务端无状态但无 Broker 即无数据） | 生产需 Broker 持久化（已配）+ 进程监控 + 重启告警 |
| 协议 | 心跳 QoS0 丢帧 | 指标曲线可能缺点（设计上接受） | 无需处理，但要向使用方说明曲线点非严格等间隔 |
| 存储 | 命令输出 7 天清除 | 超期后 `/out` 返回空字符串 + `out_purged=1`；**导出功能需在保留窗口内使用**，且 P0-1 修复时应明确「导出不受保留策略保护」的语义 | 导出按钮上标注数据保留窗口 |

---

## 5. 结论

**骨架和主链路是扎实的。** 本次评估中，202 个用例 198 passed（4 个集成用例按设计 skip），P0/P1/R1–R13 缺陷账本**全部闭环**，仅 `P2-14`（Dockerfile uv 化）未开始。MQTT 化重构达到了预期目标：连接可靠性、离线排队、在线判定三件事整体移交 Broker，服务端退化为无状态桥，代码量下降且缺陷密度收敛。客户端资源策略（读取侧封顶、线程有界、插件隔离、队列溢出有终态）做得比多数同类项目认真。

**但需求侧有一处硬缺口、两处规模化软肋：**

1. **结果导出完全未实现**——这是明确写在验收目标里的功能，且在设计文档与完成方案的页面清单中从未登记，属需求追溯断链。修复成本不高（一个流式导出端点 + 一个前端按钮），但必须补。
2. **批量下发的「最后一公里」未打磨**：建行批量化了，发布回路仍逐条回查；结果列表无分页无批次，500 台一次点击后**看不到全貌、也导不出来**。「支持批量下发并回收结果」在功能上成立，在规模上不成立。
3. **客户端资源是软约束**：所有上限都在 Python 代码里，没有内核级隔离，也没有强制上报频率的手段（`KK_ENFORCED_INTERVAL` 是死配置）。

**行动优先级**：先补 P0-1（导出）+ P1-1（发布回路）——这两项直接决定「批量下发 + 结果导出」这个核心场景能否真正交付；随后 P1-2/P1-3（分页 + 批次）把规模可用性补齐；P2 项可作为下一轮加固，其中 **P2-5（真库验证）与 P2-6（压测进 CI）** 是从「能跑」到「敢上生产」之间最重要的两块拼图。

---

## 附录：核心代码位置索引

| 关注点 | 入口 |
|---|---|
| 心跳调度与抖动 | `agent/src/kk_agent/main.py:208-221` |
| 采集项与按需采集 | `agent/src/kk_agent/collector.py:237-287` |
| MQTT 主题与 QoS/retain 语义 | `proto/messages.md` §1.1、`agent/src/kk_agent/transport.py:222-257` |
| 批量下发与校验 | `server/src/kk_server/controllers/commands.py:85-125` |
| 批量建行 | `server/src/kk_server/models/store.py:307-317` |
| 结果分块落库与幂等 | `server/src/kk_server/models/store.py:348-386` |
| 结果归属校验与保序 | `server/src/kk_server/services/mqtt_bridge.py:216-241` |
| 白名单校验单入口 | `server/src/kk_server/services/mqtt_bridge.py:156-162` |
| 超时收敛与僵尸在线 | `server/src/kk_server/services/mqtt_bridge.py:298-307` |
| 存储回收与分层保留 | `server/src/kk_server/models/store.py:528-589` |
| Agent 资源策略 | `agent/src/kk_agent/executor.py:87-112`、`agent/src/kk_agent/plugin_loader.py:19-79` |
| 入口透传与监管 | `agent/deploy/entrypoint-wrapper.sh` |
| 生产部署 | `docker-compose.prod.yml`、`deploy/mosquitto/mosquitto.conf` |
