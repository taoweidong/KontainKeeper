# KontainKeeper 优化方案（2026-10-06）

> 输入：`docs/quality-review-2026-10-06.md`（本轮评估）+ `docs/quality-review-2026-09-29.md`（QR 账本）+ `docs/service-optimization-plan.md`（S 系列）。
> 编排原则：先修**能让其它判断失真的东西**（门禁、红线、双发），再修**规模退化**（N+1、背压、RSS 预算），最后做**工程卫生**。每条带验收口径。
> 编号：沿用 QR-；本轮新条目已在评估报告 §3 定稿，这里只做排序与批次。

---

## 批次 0 · 红线与门禁（1–2 天，本周内）

**这三件不做，后面所有「全绿」都没有意义。**

### 0.1 黑名单旗标绕过（QR-S25，P0）

- 位置：`server/src/kk_server/services/security.py:70-77`
- 现状（已运行时复现）：`["sh","-x","-c","wipefs -a /dev/sda"]`、`["sh","-xc","wipefs -a /dev/sda"]`、`["python","-c",...]` 均不拦截；`["sh","-c","fdisk /dev/sda"]` 拦截。
- 改法（三处，都在 `_check_tokens` 内，不分叉到新文件）：
  1. 跳过前导旗标后再定位脚本载荷：`while rest and rest[0].startswith("-"): ...`，组合旗标（`-xc`）按「尾字母含 `c`」判定；
  2. `_SHELLS` 之外补一组**脚本解释器**（`python`/`python3`/`perl`/`ruby`/`node`，参数 `-c/-e`）进同一递归；
  3. 把 `DANGEROUS_PROGS` 的程序名同时并入子串层，逐 segment 扫描（结构层与子串层互为冗余，不再各管一半）。
- 验收：`server/tests/test_security.py` 新增矩阵用例——旗标前缀 / 组合旗标 / 四个解释器 / `env`+`sh` 双层包装 / 递归深度超限；**并把这三条复现串作为回归锁 docstring 写进用例**；`scripts/mqtt_e2e.py` 增一条「绕过形态被拒」的 Broker 侧断言。

### 0.2 让流水线重新有牙齿（QR-P1 + QR-P2，P0）

- `scripts/loadtest.py`、`scripts/bench_agent.py`：任一指标超标即 `sys.exit(1)`；打印保留，但结论进退出码。
- `Jenkinsfile:205-212`：解析 `reports/pytest.xml`，硬断言 `failures==0 and errors==0`，且 `skipped==0`（显式 `SKIP_TESTS` 时 `skipped<=4`）、`tests>=330`（用例数下限，防误删）。
- `allowEmptyResults` / `allowEmptyArchive` 改为 `false`（缺报告 = 红灯）。
- 验收：故意制造一次「集成用例全 skip」与一次「loadtest 超标」，两次构建都必须红；在 `docs/ci-jenkins.md` 的排障节补「静默绿灯」反例清单。

### 0.3 前端已落地的四处（本轮完成，待提交）

QR-W1（XSS sink）、QR-W6（命令体不落 URL）、QR-W8（默认凭据预填）、QR-W5 总览页部分 + 设计基座（见 §4）。
- 待办：提交（拆 2 个 commit：`fix(web): 安全面收敛` + `feat(web): 总览页读数族与设计基座`），并同步 `web/dist` → `server/src/kk_server/web`，让 ⑤ 的 `STRICT_WEB_SYNC` 门禁去验漂移。

---

## 批次 1 · 正确性与规模（下个迭代，约 1 周）

### 1.1 `kind=update` 结果帧双发（QR-A19，P1）

`_run_update` 自己 `send_result` 后 `return res`，`submit_fn` 又 `emit` 一遍 → 服务端多写审计、`upgrade_failed` 翻倍、QoS1 流量 ×2。
改法：`_run_update` 只返回结果，发送权归一（或走「不 emit」的直投路径，二选一但只留一条）。
验收：`agent/tests/test_main.py` 把断言从「末帧正确」改为「**update 命令的 emit 计数 == 1**」，并对 shell/collect 各加一条同样的计数断言，防止其它 kind 复现。

### 1.2 事件循环上的同步 CPU（优化 S3 + QR-S26 + QR-S29）

`verify_admin` / `ensure_admin` 的 `_pwdf` 包 `asyncio.to_thread`（QR-S8 引入的反作用一并消除）；`upload_agent` 的 sha/HMAC 已在 `to_thread`，把整包 `bytearray` + `bytes(data)` 的双副本改成流式分块哈希。
验收：并发登录 × 心跳注入的 P99 对比记录（改造前后各一次），并新增「坏帧」负路径用例（`interval="abc"`、`proto_ver="v3"`、`ts="yesterday"`）断言帧不落空、task 不炸。

### 1.3 聚合小时级 N+1 → 窗口函数（优化 S1）

`store.py:868-880` 逐 pod 一查一 upsert 改 `ROW_NUMBER() OVER (PARTITION BY pod ORDER BY ts DESC)` 一次取完 + 按小时批量 upsert，语句数 ~4.8 万 → 每小时 3 条。方言差异继续只走 `Store._upsert` / `_ensure_schema`。
验收：新增「500 主机 × 2 小时」规模用例，用 `engine` 事件统计 statement 数并断言下降一个数量级；三库方言测试覆盖窗口函数路径（MySQL 需 8.0+，在 `docs/deployment.md` 明确写出版本下限）。

### 1.4 摄入背压与进程内状态（优化 S5/S6 + QR-S9/S11/S28）

- `agent_latest` 加 10–30s TTL 缓存（顺带消掉双读）；`_spawn` 前过 `asyncio.Semaphore(16–32)`；
- fire-and-forget task 持强引用（存 set + `add_done_callback` 丢弃）；
- `_result_locks` 先无锁预查命令存在性再建锁，并设容量上限；`_LOGIN_FAILS` 改 `(count,last_ts)` 并让 `_reap` 清无新增的键；
- stats 计数改 `threading.Lock` 或 `collections.Counter` 单点写；
- **QR-S28 语义裁决**：要么把 `status` 分支写回 SQL 条件表达式（保留多实例承诺），要么撤回 docstring 的多实例说法并写明单实例前提。二者必选其一，不能并存。
验收：单测模拟 500 条 retained status 重放，断言并发峰值 ≤ N 且全部处理完成；被选中的那条语义路径有对应回归用例。

### 1.5 Agent 资源预算与退出路径（QR-A22 / A12 / A6 / A7 / A8 / A23）

- **RSS 预算显式化**：`max_queued × CHUNK` 必须落进 25–35MB 口径（当前 512×≈64KB≈32MB 明显越界），命令结果优先于心跳出队；`_Pool._q` 设上界并对溢出回 `rc=-3`；
- `stop()`：`publish_status(False)` 后 `wait_for_publish(1.0)` 再 `disconnect()`，三段 `except: pass` 全部补 warning（QR-A14）；
- 终态帧失败时丢最老排队帧强插，且必须打日志（QR-A6）；
- POSIX 进程树回收补齐 SIGKILL 兜底、`ProcessLookupError` 分支、`_kill_tree()` 后 `p.wait()`（QR-A8）；
- 清单读取加 1MB 流式上限（QR-A23）；`spawn_apply` 直接删除或挂同一门禁（QR-A18′）。
验收：`agent/tests` 对每条都有负路径（队列满、断开、无权限 kill、超大清单）；`scripts/bench_agent.py` 的 RSS 断言在批次 0.2 之后能真的红。

> 落地修正（实施时发现的方案自身偏差，按证据改法）：① 计划里的「命令结果优先于心跳出队」无对象可做——
> 心跳本就是 QoS0 且断线时直接跳过入队，out-queue 里只有 status 与 result 两类，因此改成的杠杆是
> **上限本身**（512→128，配回归用例锁 ≈8MB 预算）；② 「丢最老排队帧强插终态」换成**终态走 QoS0 直发**：
> 强插要动 paho 私有队列结构，QoS0 不入队、天然绕开积压，代价（终态可能丢一次）由服务端 30min 超时清扫兜底。
> 已在 `proto/messages.md` §1.1/§3.3 写清「服务端不得假设 result 恒为 QoS1」，帧结构不变故不升 `proto_ver`。

### 1.6 回滚可执行（QR-P3，P1）

`Jenkinsfile` 加 `GIT_REF`（默认 `main`，回滚时填历史 tag/commit）参数并在 ① 使用；部署日志与审计里记录实际部署的 commit SHA；`docs/ci-jenkins.md §7` 的回滚步骤重写为「填 GIT_REF + 不勾 SKIP_TESTS」。
验收：连续两次构建，第二次填旧 GIT_REF，断言部署的是旧代码（比对 `/api/health` 返回的版本或构建号）。

### 1.7 前端正确性三连（QR-W4 / W7 / W9）

- 请求竞态：`kkPoll` 之外补一个 `useSeq()`（或直接 `AbortController`）用于抽屉/详情类一次性请求，回包校验后才赋值；`CommandHistory` 的 `kwTimer` 补卸载 `clearTimeout`；
- 500 台规模：总览走后端分页或 `el-table-v2` 虚拟表；`HostPicker` 复用页面已加载的主机列表而不是再全量拉一次（`shell` 页当前其实已经拉过）；其余 4 页的轮询失败一并改为「页头状态」而非 toast（补完 QR-W5）；
- 删除 `utils/print.ts`/`localforage/`/`sso.ts`/`globalPolyfills.ts` 四块死代码，`.vue` 段把 `no-unused-vars` 开回来。
验收：`pnpm typecheck` + `pnpm build` 通过；「500 行主机 + 连点两行历史」的手工用例不再有覆盖现象；JS bundle 首屏包体下降有数字记录。

---

## 批次 2 · 工程门禁与可维护性（排期做）

| # | 内容 | 备注 |
|---|---|---|
| 2.1 | Python 侧接入 `ruff`（lint+format，零配置起步）与 `mypy`（先 `--ignore-missing-imports` 宽松跑通 `store`/`services`），挂进 Jenkinsfile 测试阶段 | 优化方案 S15，仍欠 |
| 2.2 | 前端 `pnpm lint` 进门禁，并先把它变绿（`build/cdn.ts` 的未用 `Plugin`）；`pnpm build` 脚本改跨平台（`cross-env` 或 `npm-run-all`），当前 Windows cmd 下直接失败 | 本轮实测 |
| 2.3 | `store.py`（976 行）按「心跳/命令/更新/清理」拆成 `store_*.py`  mixin 或独立仓储，保持方言只在一处的纪律 | 自订 ≤500 行线 |
| 2.4 | 常量与私有符号收口：`ONLINE_GRACE` 三处（QR-S21）、控制器越层引用 `_b64_tail`（QR-S22）、`_int()` 规整畸形帧字段（优化 S2 余下五处）、`append_result` 的 `out_tail` 与 SELECT 前置合并为单语句（QR-S17） |  |
| 2.5 | 账本诚实性：把 QR-T5 / QR-T7 的「已修复」改回「部分/未修」，并补 `--strict-markers` + markers 注册 + server dev 组 `pytest-asyncio` | 防下一轮误配精力 |
| 2.6 | 部署面加固：`server/Dockerfile` 非 root `USER` + `HEALTHCHECK` + pin uv 镜像 digest；compose 补 `healthcheck`/`mem_limit`；`.env.example` 补 `KK_MQTT_USERNAME/PASSWORD`；`ci_smoke.sh` 删硬编码 `PASS(1)`、口令移出 argv | QR-P4/P5/P9 |
| 2.7 | 仓库卫生：`.zcode/`、`.codegraph/`、`.workbuddy/`、`skills-lock.json` 从版本库移出或 ignore；pptx 走 Release 资产而非仓库；`.gitignore` 补 `include/ lib/ service/`；清理根目录 `kk-server.db*` 磁盘残留 | QR-P6 |
| 2.8 | 提交规范回补：本轮之后按「中文前缀 + 按模块 + 标注 QR 编号」执行；服务端那批修复的可追溯性用一条 `docs:` 提交映射「commit → QR 编号」补回 | QR-P6 |

---

## 3. 前端设计基座（本轮已落地部分 + 后续）

### 3.1 主题定位（brief）

KontainKeeper 不是「数据分析仪表盘」，是**内网机队控制台**：使用者是平台/运维工程师，在 1366×768 笔记本上长时间扫描 500 行，主要动作是「判断哪台有问题 → 下发命令/采集 → 事后追溯」。视觉语言因此取自控制舱仪表而非 SaaS 卡片：**读数等宽对齐、状态只有四档、层级靠密度与 hairline，不靠圆角/阴影/渐变**。

### 3.2 令牌（全部派生自 Element Plus 变量，不破 light/dark 与 8 套主题预设）

- **Color**：`--kk-live`（在线=success）· `--kk-alert`（磁盘告警=danger）· `--kk-stale`（待升级=warning）· `--kk-idle`（离线=冷灰 disabled）。**规则：饱和色只留给告警与待升级**，所以它们真的刺眼；离线不是错误，用空心冷灰。文本/边框/填充一律沿用 Element 变量，深色主题自动成立。
- **Type**：UI 正文继续用现有本机栈（Helvetica Neue / PingFang SC / 微软雅黑），**读数角色**新增一条本机等宽栈 `--kk-font-num`（ui-monospace / Menlo / Consolas），配合 `font-variant-numeric: tabular-nums`。离线部署（`docker-compose.offline.yml`）与 `VITE_CDN=false` 决定了**不能引外部字体**，个性靠度量而非下载字体。字号 12/13/14/16/20 窄尺度，标题 `letter-spacing:-0.01em`。
- **Layout**：`kk-band`（机队仪表带，页头一次扫描）→ `kk-table size=small`（密度优先）→ `kk-sticky-bar`（吸底批量动作，已有）。分栏仍走 `kk-side`，窄屏折叠。
- **Principles**：① 读数是主角，数字列右对齐 + 等宽；② 一个记忆点（机队仪表带 + 心跳新鲜度三格），其余安静；③ 结构件必须承载信息——仪表带的比例段**只编码互斥类别（在线/离线）**，可重叠子集（磁盘告警、版本落后）用读数表达，否则画出的是一张说谎的图；④ 非触发动效清零（本轮删掉了仪表带的载入揭示动画，只保留数据变化的宽度过渡）。

### 3.3 对通用套路的自查（做了什么取舍）

- 不用 cream+serif+terracotta、不用黑底+酸绿、不用报纸 hairline 三栏、不用「同款圆角卡 + 软阴影 + 渐变」的 SaaS kit——本方案反而在**削减**圆角与 tag 色块（状态改圆点、磁盘告警改条身变红）。
- 「等宽字体」在这里是功能选择而非装饰套路：500×8 个数字单元格需要逐位对齐，这是该领域的通用语言（top / Grafana 表格同源）；使用范围**限定在数字/主机名/版本/时间戳/命令输出**，不用于小标签。
- 克制点：删掉装饰性载入动画、删掉与文字重复的 `el-progress + %` 双写、删掉已失效的 `.kk-metric`。

### 3.4 后续（未做，随批次 1.7）

其余 5 页迁移到读数族（`update` 页已有 `kk-stat` 自动继承等宽数字）；命令输出面板的终端感（真等宽、行号、失败行左边条）与「空态即指引」文案；审计页时间列的 `kk-num`；`welcome` 首页用机队仪表带替代卡片堆叠；键盘可达性（整行可点但不可 Tab，目前靠主机名列的 `el-link` 兜底）。

---

## 4. 建议时序与依赖

```
批次 0（本周，3 项）：0.1 黑名单 → 0.2 门禁 → 0.3 前端提交+产物同步
批次 1（下迭代）：1.1 双发 → 1.5 Agent 预算 → 1.2 事件循环 → 1.4 背压/语义裁决 → 1.3 N+1 → 1.6 回滚 → 1.7 前端正确性
批次 2（穿插做）：2.2 lint 门禁最先（它能让后面每一批自动受益），2.1 ruff/mypy，2.3 store 拆分放最后（会与 1.2/1.3/1.4/2.4 冲突，必须等它们合入）
```

每批独立提交、`fix:/perf:/chore:` 前缀 + QR 编号；批次收口口径统一为：`pytest agent/tests server/tests` 全绿（Broker 可达）、`scripts/mqtt_e2e.py` 通过、`pnpm typecheck && pnpm build` 通过、Jenkins 一次完整流水线红→绿可复现。

## 5. 依然「明确不做」

不引 Redis/外置缓存、不把 `append_result` 重写成分块行表（S14 触发条件未到）、不给心跳表上分区或时序库、不改 `containers`/`pod` 表名列名、不把 6 个业务页重写为 `el-table-v2` 全家桶（只在总览按需引入）、不自研应用层 ACK 状态机（Broker QoS1 + 持久会话已覆盖，`cmd_ack` 主题位仍空置备用）。
