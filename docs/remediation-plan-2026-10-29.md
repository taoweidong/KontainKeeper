# 分阶段优化方案（2026-10-29）

> 依据：`docs/quality-review-2026-10-29.md`（含同日「复核记录」一节，所有实施条目均经第二会话逐条读码确认）。
> 原则：**每阶段独立可验证、可单独回滚**；不合并提交；每阶段结束跑全量回归（后端 pytest / 前端 typecheck+test），
> 绿了才进下一阶段。阶段内每一步都要求「功能行为与优化前一致且正确」——行为修正型改动必须伴随锁定新语义的回归测试。
> 提交规范：中文提交信息、`fix/feat/test/docs/chore` 前缀、按阶段分批提交，信息里标注缺陷编号（QR29-xx）。
>
> 基线：`main @ fafd497`，后端 397 passed / 5 skipped（58s，无 Broker），前端 typecheck 绿、0 个测试文件。

## 总览

| 阶段 | 主题 | 缺陷 | 风险 | 预计规模 |
|---|---|---|---|---|
| 1 | P1 缺陷修复（正确性红线） | S1 S2 W1 W2 W3 | 低（每处 ≤30 行） | 服务端 2 文件 + 前端 3 文件 |
| 2 | P2 缺陷修复（服务端批次） | S3 S4 S5 S7 S8 S9 S12 S13 S14 S15 S16 S18 | 中（有语义修正，逐条带回归测试） | 服务端 6 文件 |
| 3 | P2 缺陷修复（前端批次） | W4 W5 W7 W8 W9 W11 W12 | 低-中 | 前端 8 文件 |
| 4 | 后端测试补全（评审 A 类 + 缺口） | A1 A2 A3 + S 类新语义 | 低（纯新增测试） | agent/tests + server/tests 新增/扩充 |
| 5 | 前端测试基建与核心单测 | W 类工具层 + 门禁 | 低（纯新增） | web/package.json + vitest 配置 + 2~3 个 spec |
| 6 | 协作规范固化 | 门禁不存在（§6.4） | 无 | AGENTS.md |

后期阶段（本轮不做，登记备选）：`store.py` 按内聚拆分（评审 §6.1，1,142 行）、
前端 6 页轮询/导出/空态范式下沉（§6.2 的 8 组重复）、Agent 更新流式下载（A4）与
启动健康门禁（A5）、v4 字段（caps/labels/custom）UI 呈现（§5，产品裁决）。

---

## 阶段 1：P1 缺陷修复（正确性红线）

**目标**：消除「能让全绿服务端变瘫」「用户可见的恒错显示」「抽屉永久卡死」「登出不吊销」四个 P1。

| # | 文件 | 改动 | 行为契约 |
|---|---|---|---|
| S1 | `server/src/kk_server/models/store.py` | `verify_admin` 的 PBKDF2（310k 迭代，含不存在用户的时序拉平与 legacy 升级两条路径）挪进 `asyncio.to_thread` | 登录成功/失败/legacy 升级三路返回值不变；事件循环不再被同步哈希阻塞 |
| S2 | `server/src/kk_server/services/mqtt_bridge.py` | `_on_message` 全路径 try/except Exception 兜底（解析圈外还包住 stats 与 `_dispatch`）；`call_soon_threadsafe` 的 RuntimeError（循环已关）单独吞掉并计数 | 正常帧路由不变；paho 网络线程内任何异常不再外溢；新增 `stats["bridge_errors"]` 计数 |
| W1 | `web/src/api/user.ts`、`web/src/store/modules/user.ts` | 新增 `logoutApi()`（POST /api/logout）；`logOut()` 先尽力吊销服务端会话（catch 吞错，不阻塞本地清理与跳转） | 本地登出行为（清 token/路由/tags）一字不变；后端会话同步失效 |
| W2 | `web/src/views/host/detail/index.vue` | `metrics?.sys?.os` → `metrics?.os`、`metrics?.sys?.uptime_sec` → `metrics?.uptime_sec`（与 `collector.py` 平铺形状对齐） | 「系统」「运行时长」两行在真机上开始显示真值 |
| W3 | `web/src/views/command/components/utils/hook.tsx` | `showOut` 的 `out_purged` 分支显式复位 `out.loading = false` | 点已清理行不再让在途请求的 spinner 永久挂起 |

**验收标准**：`pytest agent/tests server/tests -q` 全绿（S1/S2 各带一条回归测试，随阶段 2 的
测试批次一起落，本阶段先保证既有 397 条不红）；`pnpm typecheck` 绿。
**回滚边界**：以上 5 文件的改动各自独立，任一文件出问题单独 `git checkout -- <file>`。

---

## 阶段 2：P2 缺陷修复（服务端批次，逐条带回归测试）

**目标**：修掉「组合态失效」与「静默丢帧」两类 P2；每条修复配一条锁定新语义的测试。

| # | 文件 | 改动 | 新语义（=测试断言） |
|---|---|---|---|
| S3 | `mqtt_bridge.py` | `stats` 读改写收敛到 `self._stats_lock` 保护的 `_bump()` | 并发 bump 不丢计数（多线程压测断言总数） |
| S4 | `mqtt_bridge.py` | status/hb 帧校验 `body["host"]` 与 topic 主机一致（字段缺失放行兼容存量，不一致按白名单外同型拒收 + 节流审计） | 伪装他机 id 的 status/hb 被拒并审计；缺 host 的旧帧照常受理 |
| S5 | `models/store.py` | `set_online` 对 `ts` 套用 `record_hb` 同款钳制（now−86400 ≤ ts ≤ now+300，越界回落服务器时间） | 时钟超前的 status 帧不再把 `status_ts` 写进未来；在线判定与 stale 兜底恢复生效 |
| S7 | `controllers/auth.py` | `_LOGIN_FAILS` 配套记录最近失败时间，`_reap` 同时回收两条字典的过期键 | 未达阈值的失败计数在 TTL 后被回收，字典不无界增长 |
| S8 | `controllers/auth.py` | 锁定期内的 `login_locked` 审计按 60s 节流（对齐桥接 `AUDIT_THROTTLE` 语义，key=ukey/ikey） | 锁定期连打 N 次只落 1 条审计 |
| S9 | `models/store.py` | `_aggregate_hours` 追赶窗口 `(raw_days+1)*3600` → `raw_days*86400`，与清理侧同一保留口径 | 长宕机（>raw_days+1 小时）后心跳不丢聚合；既有 `test_cleanup_aggregates_and_prunes` 语义不变 |
| S12 | `controllers/containers.py` | `view=full` 且未显式给 limit 时封顶 5000（summary 维持不限——导出走 MAX_ROWS=20000 上限） | full 视图无 LIMIT 的自伤面关闭；显式 limit 行为不变 |
| S13 | `models/tables.py`、`models/store.py` | `commands.argv`/`audit.detail` 建表升级 `_long_text()`（新库生效）；`add_audit` 对 detail 做入库前截断（8KB 封顶 + 截断标记） | 超长审计 detail 不再 1406（发布后 500 的撕裂态消失）；既有小 detail 逐字不变 |
| S14 | `mqtt_bridge.py` | `publish_update` 检查 `info.rc`（对齐 `dispatch_command`），入队失败置台账 failed + 计数 | 队列满/未连接时升级不再静默丢、`upgrade_pushed` 不再虚高 |
| S15 | `models/store.py` | `_host_filters` 的 `online` 筛选对齐 grace 语义（CASE 表达式三库通用） | `?online=true/false` 与列表渲染、导出三处同口径 |
| S16 | `controllers/commands.py`、`models/store.py`、`controllers/exporting.py` | 删除死代码：`MAX_ITEMS`、`Store.touch`、store 未用 import；`export_hosts` 移除从不生效的 `view` 参数（前端恒传 summary，已核实无消费方） | 死代码清零；导出接口不再暗示一个不存在的 full 形态 |
| S18 | `models/store.py` | 命令/审计 keyword LIKE 补 `autoescape`（对齐主机侧既有写法，含 64 字符截断） | 搜 `100%` 语义为字面量；`_`/`%` 不再当通配符 |

**验收标准**：上述每条的新增测试 + 既有 397 条全绿；`test_dialects`（compile 级）确认三库 SQL 仍可编译。
**回滚边界**：每条缺陷一个独立 hunk + 一条独立测试；按缺陷逐条 revert，互不牵连。
S12/S13/S15/S18 是**语义修正**（旧行为本身是缺陷），测试锁定新语义时在测试 docstring 里写明 QR29 编号。

---

## 阶段 3：P2 缺陷修复（前端批次）

**目标**：修掉前端「可复现的死态 / 谎报 / NaN 直通」，全部为局部改动，不动页面布局与交互范式（避免与并行会话的 Task 8/9 WIP 冲突——`host/update/*` 与 `api/agent.ts` 本阶段**不碰**）。

| # | 文件 | 改动 | 行为契约 |
|---|---|---|---|
| W4 | `views/command/components/HostPicker.vue`、`shell/index.vue`、`collect/index.vue` | 新增可选 prop `hostsRead`（父页清单读到过没有）；`listRead` 优先取它，缺省回落现行为 | 真空机队显示「没有匹配的主机」而非「接口不可达」；父页加载失败仍显示「没读到」 |
| W5 | `utils/kk.ts`（`numText`）、`views/host/monitor/index.vue`（`fillPct`） | 非有限数（NaN/Inf）按「没读到」处理：`numText` 回 `-`，`fillPct` 回 0 | 插件写坏指标不再渲染 `NaN`；正常值显示一字不变 |
| W7 | `utils/http/index.ts` | 401 触发的登出去重（1s 窗口内只执行一次）+ 一条「登录已过期」提示 | 登录接口的 401 仍不触发（isNoTokenUrl 语义不变）；6 路轮询同时过期只登出一次 |
| W8 | `views/command/components/utils/hook.tsx` | `POLL_KEY` 加实例唯一后缀（模块级自增序号） | 对外契约不变（仍导出 POLL_KEY）；两页 CommandHistory 共存不再互掐轮询 |
| W9 | `views/command/shell/index.vue` | watch 收窄到真正落 URL 的三个字段（pods/mode/timeout），去掉 `{...shellForm}` + deep | 命令框输入不再每键触发 `router.replace`；URL 同步行为不变 |
| W11 | `utils/kk.ts`（`errText`） | FastAPI 422 的数组型 `detail` 取 `msg` 拼接；对象型降级 JSON.stringify | 校验错误 toast 可读；普通 detail 文案逐字不变 |
| W12 | `views/command/collect/index.vue`、`components/HostPicker.vue`、`views/welcome/index.vue` | 三处内联 `e?.message ?? e` 换 `errText` | 错误文案口径统一（后端 detail 中文文案开始透出） |

**验收标准**：`pnpm typecheck` 绿；W5/W11 的工具函数行为由阶段 5 的 vitest 用例锁定。
**回滚边界**：每个文件独立可 revert；W4 涉及三个文件作为一个原子单元回滚。

---

## 阶段 4：后端测试补全（评审 A 类 + 服务端缺口）

**目标**：把「协议字段来自未测代码」「加固路径无回归锁」「异常逃逸零覆盖」三类缺口补齐；
全部单测**无 Broker 依赖、无相互依赖、可任意顺序重复执行**（tmp_path/monkeypatch 隔离，沿用仓库既有夹具风格）。

| 缺口 | 文件 | 覆盖内容 |
|---|---|---|
| A1 | `agent/tests/test_hostinfo.py`（新增） | `_clip`/`_safe`/`_in_container`（cgroup 标记、dockerenv、K8S env 三信号）/`_container_runtime`/`_virt`（cpuinfo flags monkeypatch）/`_pretty_name`（os-release 解析、缺文件回落）/`_ips`（psutil monkeypatch、去环回、8 上限）/`host_env` 聚合形状 |
| A2 | `agent/tests/test_main.py`（扩充） | 信号处理（SIGTERM/SIGINT → stop 置位）与 `run()` 的退出路径：publish_status(wait=True) 在退出时被调、异常退出也走 finally |
| A3 | `agent/tests/test_transport.py`（扩充） | `mqtt_username` 鉴权分支、mqtts 无 CA 的 `CERT_NONE`+`tls_insecure_set`、`_on_message` 非 JSON/非 dict 忽略分支（不外溢） |
| §1 矩阵 | `server/tests/test_security.py`（扩充） | 把复核矩阵固化为回归：`sh -x -c`、`python -c`、`dd` BLOCK；`curl \| sh`、`truncate`、`find -delete`、`systemctl`、`kill` PASS（**锁定现状口径**，产品裁决前防止意外漂移） |
| 阶段 2 新语义 | `server/tests/test_store.py`、`test_bridge.py`、`test_api.py`、`test_auth`（新增或并入 test_api） | S3/S4/S5/S7/S8/S9/S12/S13/S14/S15/S18 各一条（随阶段 2 落，此处只登记） |

**验收标准**：全量 `pytest agent/tests server/tests -q` 通过且总数显著增长；新测试不含 sleep 依赖真实时钟
（用注入 now/monkeypatch time）；连续跑两遍结果一致。
**回滚边界**：纯新增文件/用例，revert 不影响任何生产代码。

---

## 阶段 5：前端测试基建与核心单测

**目标**：终结「前端 0 测试」；先覆盖**纯逻辑层**（工具函数与轮询管理），组件级测试登记为后续阶段（需要 jsdom + Element Plus stub，收益/成本比低）。

| 项 | 文件 | 内容 |
|---|---|---|
| 基建 | `web/package.json`（devDeps：`vitest`、`@vue/test-utils`、`jsdom`；scripts：`"test": "vitest run"`）+ `web/vitest.config.ts` | node 环境为主（纯函数），DOM 用例按文件用 `// @vitest-environment jsdom` 注解 |
| 单测 | `web/src/utils/kk.spec.ts` | `tsText/ageText/durText/mbText/numText（含 NaN→"-"，W5）/nOr/errText（detail 字符串、422 数组、message 兜底，W11）/statusType/statusLabel/upgradeSkipText/fileStamp` |
| 单测 | `web/src/utils/kkPoll.spec.ts` | `setPoll` 覆盖同名 key 不叠加、`interval<=0` 只清不建、`usePolls` 作用域只清本作用域 key、`useSeq` 迟到包丢弃（fake timers） |

**验收标准**：`pnpm test` 绿、`pnpm typecheck` 绿；测试不依赖网络与真实定时器（fake timers）。
**回滚边界**：纯新增 + package.json devDeps 一行级改动；`pnpm install` 可完整复原。

---

## 阶段 6：协作规范固化（AGENTS.md）

**目标**：把「测试门禁」写进 `AGENTS.md`，成为后续所有 AI 交互的硬约束。

新增「协作规范（AI 交互测试门禁）」一节，内容：
1. 每次 AI 交互涉及代码改动时，**提交前必须**跑：`.venv/Scripts/python.exe -m pytest agent/tests server/tests -q --junitxml=<tmp>/results.xml`（解析 tests/failures/errors/skipped 计数，别用 `| tail` 截断）与 `cd web && pnpm typecheck && pnpm test`；
2. **全部通过才允许提交**；任何一条红，先定位修复再重跑，禁止「跳过/搁置/注释测试」式提交；
3. 后端有 Broker 时 397+ 条全 passed、无 Broker 时允许恰好 5 条集成用例 skipped——skip 数超过 5 视为异常，须排查；
4. 汇总以 junitxml 解析为准（失败行在进度条之前，`tail` 会截掉——本仓库已两次踩坑）。

**验收标准**：AGENTS.md 更新且与实际命令一致（本会话收口时全量跑过一遍）。
**回滚边界**：文档改动，单独提交可独立 revert。

---

## 顺序与验证节奏

```
阶段1 → pytest(397绿) + pnpm typecheck
阶段2 → pytest(397+新增 全绿)
阶段3 → pnpm typecheck（行为验证依赖阶段5的测试与人工点检）
阶段4 → pytest 全绿 ×2 遍
阶段5 → pnpm test + pnpm typecheck
阶段6 → AGENTS.md 落笔，全量复跑一次作为收口证据
```

每阶段收口在对话里给出：改动文件清单、测试计数变化、是否触发语义修正（需要人工知悉的 4 条：
S12 full 视图封顶、S13 审计截断、S15 筛选口径、W4 空态文案）。

---

## 实施记录（2026-10-29 第二会话收口）

六个阶段全部按上述顺序实施完毕，每阶段独立验证、全绿后才进下一阶段：

| 阶段 | 状态 | 验证证据 |
|---|---|---|
| 1 | ✅ | 既有 397 条保持绿；`pnpm typecheck` 绿 |
| 2 | ✅ | 新增 `server/tests/test_qr29_remediation.py` 17 条；全量 **449 passed / 5 skipped / 0 failed**（397→449） |
| 3 | ✅ | `pnpm typecheck` 绿 |
| 4 | ✅ | 新增 `agent/tests/test_hostinfo.py`（24 条）+ test_transport 鉴权/TLS/畸形帧 7 条 + test_main 信号/退出 3 条 + test_security 矩阵固化 2 条；全量 449 绿 |
| 5 | ✅ | vitest 3.2.7 基建（`web/vitest.config.ts` + `pnpm test`）；`kk.spec.ts` + `kkPoll.spec.ts` 共 25 条全绿 |
| 6 | ✅ | AGENTS.md 新增「协作规范（AI 交互测试门禁）」一节 |

过程中被既有回归锁当场拦下的一次：新增 `stats["bridge_errors"]` 计数器后，
`test_proto_window.test_live_counter_has_frontend_reading_slot` 立即红灯——它要求桥接累加的每个
计数器必须在系统统计页有读数位。已按锁的意图补齐（`web/src/views/system/index.vue` 增加
「桥接异常」读数），并把该锁的静态扫描正则同步到 `_bump()` 新写法。门禁机制有效的直接证据。

**需要人工知悉的语义修正（旧行为本身是缺陷，但外显行为有变化）**：

1. S12：`/api/containers?view=full` 不带 limit 时从「不限」封顶为 5000；
2. S13：审计 detail 超 8KB 截断落库（原为整条 INSERT 失败 500）；新库 `commands.argv`/`audit.detail` 升 LONGTEXT；
3. S15：`?online=` 筛选从裸 online 列改为与渲染一致的宽限口径（筛出的行 = 页面上看到的行）；
4. W4：HostPicker 空机队显示「没有匹配的主机」而非「主机清单不可达」（父页新增 `hostsRead` prop）；
5. S7：登录失败计数新增 300s 冷却回收——爆破防护强度不变（每 5 分钟 5 次），但历史计数不再永久累积。

**登记未做（原方案「后期阶段」）**：store.py 拆分、前端 6 页范式下沉、A4 流式下载、A5 启动健康门禁、
v4 字段 UI 呈现、安全覆盖面口径（§7-3）、页面隐藏轮询（§7-4）——均为产品/设计裁决项，见评估报告 §7。
