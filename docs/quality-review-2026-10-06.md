# KontainKeeper 全量质量评估报告（2026-10-06）

> 评估日期：2026-10-06　代码基线：`main` @ `8d7f442`（+ 本轮前端改动，见 §6，尚未提交）
> 评估范围：`agent/src/kk_agent/`（2,012 行）、`server/src/kk_server/`（3,515 行）、`agent/tests` + `server/tests`（5,498 行）、**`web/src/`（13,687 行 / 129 文件，首轮纳入）**、`Jenkinsfile` + `scripts/` + `deploy/` + 四份 compose、仓库卫生。
> 方法：四路并行深审（服务端 / Agent / 前端首轮 / 测试-CI-部署）→ 高严重度条目由主线复核（NEW-S1 已做**运行时复现**）→ 全量实测基线复跑 → 与既往三份账本（`quality-review-2026-09-29.md` QR-、`service-optimization-plan.md` S-、`quality-assessment-2026-09-12.md` 目标达成度）逐条对账。
> 交付性质：**评估报告 + 优化方案**（方案见 `docs/remediation-plan-2026-10-06.md`）。新缺陷编号沿用 `QR-` 前缀，本轮子代理产出的条目前缀为 `NEW-S/A/P/W`，对账后在正文里已并入 QR 序（如 NEW-S1 = QR-S25）。

---

## 0. 结论速览

**总体判定：B+（较 09-29 的 B+/A- 略降一档，降的是"门禁可信度"而不是代码品质）。**

代码层面的工程纪律依然是这个仓库最强的资产：三库方言只收口在两处、依赖注入层默认拒绝、回归锁 docstring、轮询统一走 `kkPoll`、构建产物有 CI 漂移门禁。本轮四路深审都独立得出了同一结论——**修复是真实的，且修在了正确的位置**。

但风险重心已经迁移，三件事必须在本周期内处理：

1. **安全红线仍可破（最高优先）**：命令黑名单的结构校验在「旗标前缀 + `-c`」形态下失效。`argv=["sh","-x","-c","wipefs -a /dev/sda"]` 与 `["sh","-xc","wipefs -a /dev/sda"]` 经真实函数复现均为 **PASS（不拦截）**，因为递归入口只认 `rest[0] ∈ ("-c","--command")`；脚本类解释器（`python -c` / `perl -e` / `node -e`）同型失效。（QR-S25，即上轮 QR-P0-2 的残留面）
2. **CI 的"绿灯"信息量不足**：夜测脚本 `scripts/loadtest.py`、`scripts/bench_agent.py` **恒返回退出码 0**（只 print「超标」），`Jenkinsfile` 无 `skipped>0` 与用例数下限门槛且 `junit allowEmptyResults:true`，回滚路径缺 `GIT_REF` 参数（勾了回滚选项仍会部署最新代码 + 旧镜像）。三条叠加使「CI = 质量门禁」只在 Linux + Broker 齐备的理想路径上成立。
3. **前端 13.7k 行首轮纳入即暴露三处**：全仓唯一的 HTML 注入 sink 吃进 **Agent 自报主机名**（`kkConfirm` 的 `dangerouslyUseHTMLString`）；`tsconfig` 的 `strict:false` + eslint 关 `no-explicit-any` 让 87 处类型逃逸无人兜底（"假严格"）；500 台全量主机 JSON 在 5 个页面各自重复拉取、喂给非虚拟 el-table、且全仓零请求竞态防护。

**可测量基线（本轮实测，见 §1）**：340 条用例 → 336 passed / 4 skipped / 0 failed，61.1s；前端 `tsc` + `vue-tsc` 全绿，`vite build` 成功（42s，产物 2.78MB）；`pnpm lint` **失败**（eslint 1 error + 16 文件被 prettier 重排 +512/-159）；Python 侧无 ruff / mypy / pytest-cov。

---

## 1. 实测基线（本轮）

| 指标 | 实测值 | 与既往叙事对比 |
|---|---|---|
| 用例收集数 | **340**（agent 154 + server 186） | AGENTS.md/README 写「290+」、09-29 报告写「310」→ **文档漂移**（已 +30） |
| 本机执行（无 Broker） | **336 passed / 4 skipped / 0 failed**，61.1s | 上轮 306/4/0，141s → QR-T3 夹具探测缓存生效，**耗时降到 43%** |
| 源码 : 测试 | 5,527 : 5,498 行（Python） | 「≈1:1」成立，但**全仓无 pytest-cov、无覆盖率门槛**→ 行码叙事 ≠ 覆盖证据 |
| 前端类型检查 | `tsc --noEmit` + `vue-tsc --noEmit` **通过** | 但 `strict:false`（`tsconfig.json:6-7`）+ `no-explicit-any:"off"`（`eslint.config.js:81`）使绿灯含金量有限 |
| 前端 lint | **失败**：`web/build/cdn.ts:1` `'Plugin' is defined but never used`；prettier 重排 16 文件 | 说明 `pnpm lint` 不在任何门禁（Jenkinsfile 无前端 lint 阶段）。**已修**：cdn.ts 死导入清零、ts 侧归零（aa4945c），⑤ 段新增棘轮门禁（见 QR-W10 / §8.5） |
| 前端构建 | `vite build` 成功 42s / 2.78MB | ⚠ `pnpm build` 脚本在 Windows cmd 下失败（`NODE_OPTIONS=... vite build` 是 POSIX 语法），需 Git Bash。**已修**（3e2b2eb 改直调 vite 入口，cmd 下 `pnpm build` 退出码 0，见 §8.5） |
| 依赖新鲜度 | fastapi 0.141.1（最新 0.142.2）、sqlalchemy 2.0.52（2.1.3）、starlette 1.6.0（1.7.0）、uvicorn 0.52.4（0.54.0） | 无落后 majors、无已知高危；asyncpg/pymysql 走 extra 未装（符合设计） |
| 协议一致性 | 双端 `PROTO_VER = 3` 同步；`KK_ALLOW_SHELL` 契约在 `config.py:89`/`executor.py:195` 落实 | ✅ 无漂移 |
| 本机 Docker | **不可用** | 「Broker 可达时全 passed」这一叙事本轮**无法在开发机验证**（见 §7） |

**仓库卫生**：`.zcode/`（24 文件）、`.codegraph/`、`.workbuddy/`（18 文件）、`skills-lock.json`、`KontainKeeper-项目架构设计方案.pptx`（480KB 二进制）均被 git 跟踪；`include/ lib/ service/` 是 WSL 遗留空目录、未进 `.gitignore`；`kk-server.db*` 已正确忽略（QR-T9 的 git 面已修，磁盘残留仍在）。
> **2026-10-07 落地（`c41b7a0`，整改 2.7）**：`.zcode/`（24）+ `.codegraph/`（1）+ `.workbuddy/memory`（2）+ `skills-lock.json` 共 **28 个文件用 `git rm --cached` 摘出版本库，磁盘副本一份没删**（那是别的工具在用的状态）；`.gitignore` 补 `.zcode/ .codegraph/ .stepcode/ .codeartsdoer/ skills-lock.json .pnpm-store/`。磁盘侧清掉 `include/ service/`（空）、`lib/`（已验证零文件的 WSL venv 空壳）、根 `.pnpm-store/`（0 文件）、根 `kk-server.db*`（12 行的一次误跑，移到 `%TEMP%/kk-workspace-cleanup-2026-10-07` 而非直接删；正式运行库一直在 `data/`）。**pptx 仍留在仓库**：`GET /repos/…/releases` 返回 0 条，「走 Release 资产」的前提不成立。更正两处口径：① `include/ lib/ service/` 原本就不需要进 `.gitignore` —— git 不跟踪空目录，它们从未出现在 `git status` 里，2.7 那条是误诊；② 上文「`.workbuddy/`（18 文件）」是**磁盘**计数，`git ls-tree` 实际入库只有 2 个（`memory/2026-08-30.md`、`memory/2026-09-04.md`），故摘出的 28 个是按版本库条目数的。

---

## 2. 既往账本对账（逐域汇总）

### 2.1 服务端（QR-S1~S24 + 优化方案 S1~S15）

| 结论 | 条数 | 明细 |
|---|---|---|
| 已真实修复 | **8** | QR-S1（`out_tail` 镜像列 + 迁移登记）、QR-S2（router 级 `Depends`，双层路由拆分正确，未发现漏网端点）、QR-S3（`busy_timeout` 按连接生效；`synchronous` 仍仅首连并注明 aiosqlite 限制）、QR-S4（`idx_hb_ts` + 存量补索引，三库分支收在 `_ensure_schema` 一处）、QR-S7（四导出 + 二进制下载均落审计）、QR-S8（假盐同代价 PBKDF2，但引入 NEW-S2）、P0-2（`-c` 递归，残留 NEW-S1）、P0-1 服务端签名侧 |
| 仍存在（登记未动） | **16** | QR-S5、S6、S9（task 无强引用/无上限）、S10（半：sha/HMAC 进 `to_thread` ✅，整包 64MB 双副本 + 读循环在锁外 ✗）、S11、S12、S13、S14、S15、S16、S17（且每块**多加一次前置 SELECT**）、S18、S19、S20、S21（180 仍三处）、S22、S23、S24 |
| 优化方案 P1 三项 | **未做 2.5** | S1 聚合小时级 N+1 原样（`store.py:868-880`）；S3 PBKDF2 仍在事件循环（`store.py:764-785`）；S2 畸形帧字段只补了 `seq`，`ts`/`interval`/`rc`/`elapsed_ms`/`proto_ver` 五处仍漏 |
| 优化方案 S4 索引 | **机制建好但未登记** | `_table_indexes` 三库补索引机制已落地，`kk_commands.status` 却没进清单 → sweep 仍每 30s 全表扫 |

### 2.2 Agent（QR-A1~A18）

已修 6（P0-1 三层门控无在线绕过路径、A1 有界 LRU 去重、A2 `.kkbak` 回滚 + `exec_error` 回执、A3 加载挪锁外 + 超时隔离、A4 恒返 `({}, None)`、A5 注释澄清、A13 已钳制）；**仍存在 11**：A6（终态帧走同一个满队列）、A7（`stop()` 未 `wait_for_publish`，干净 DISCONNECT 不触发 LWT）、A8（POSIX 进程树回收三处缺陷保留：组长 `wait(3)` 成功即 return、`ProcessLookupError` 直接 return、`_kill_tree()` 后未 `p.wait()` 留僵尸；Windows 侧已与 docstring 一致）、A9（`argv=[]` → `sh -c "[]"`）、A10（清单 HTTP 读取仍无上限）、A11、A12、A14、A15、A16、A17、A18。

> **A18 的风险等级需要上调**：`updater.spawn_apply` 直调 `apply_manifest`，**绕过 `update_disabled` 与 `push_update_allowed` 两道门禁**。当前零调用方所以不是活漏洞，但它是躺在库里的门控绕过原语——被接线即成 RCE。

### 2.3 测试体系（QR-T1~T11）

| ID | 状态 | 关键证据 |
|---|---|---|
| QR-T3 | ✅ 已修 | `test_integration.py:27-57` 进程级探测缓存（实测耗时 141s→61s 印证） |
| QR-T4 | ✅ 已修 | `test_bridge.py:455/469/483`、`test_agent_update.py:181-185` 显式 close |
| QR-T9 | ✅ git 面已修 | `.gitignore:4-6`；磁盘残留仍在 |
| QR-T2 | ⚠ 部分 | `scripts/db_smoke.py` + `Jenkinsfile:497-557` 已建真库 stage，**但只由 TimerTrigger/FORCE_NIGHTLY 触发**，普通 push 不跑；且该 stage 里的 load/bench 永不失败（→ NEW-P1） |
| QR-T1 | ⚠ 部分 | bridge 生命周期/失败路径单测已补（`test_bridge.py:505-566`）；**Jenkinsfile 的 skipped 硬断言仍缺失**（`junit allowEmptyResults:true`） |
| **QR-T5** | ❌ **账本标注与代码不符** | 报告写「已修复」，实际：`filterwarnings=error` ✔，但三份 ini **均无 `--strict-markers` / markers 注册**（grep 零命中），`server/pyproject.toml:40-44` dev 组**仍无 pytest-asyncio** |
| **QR-T7** | ❌ **同上** | `test_bridge.py:233` 的 OR 断言逐字未动；`test_executor.py:90` 仍是 `rc in (126,127) or b"cannot spawn"` |
| QR-T6 | ⚠ 部分 | conftest 死代码已删（现 56 行）；`FakePublish` 仍 3 份复制、`api` 夹具仍 3 份 |
| QR-T8 / T10 / T11 | ❌ 仍存在 | `test_integration.py:374` `retain=True` 无清理；`GET /api/system/updates`、`/api/me`、上传 413、`config` 心跳下限钳制仍无测试；executor 双路覆盖格局未变 |

**诚实性结论**：09-29 账本总体可信，但 T5/T7 两条「已修复」标注不成立。账本作为唯一的状态真相源，这类漂移会让下一轮把精力错配到已闭合项上。

### 2.4 前端（首轮，全新条目 W1~W9）

约定符合度三项全绿：**裸 `setInterval` 为 0**（仅 `utils/kkPoll.ts:53` + 白名单 `directives/longpress`），5 处轮询全部 `usePolls()` 作用域版；`getAsyncRoutes()` 返回 `[]`、6 页静态路由齐全；业务页最大 477 行（≤500 自律线成立，超线的 690/631 行是 pure-admin 底座 `lay-tag`/`lay-setting`）；`Jenkinsfile:258-280` 的 `STRICT_WEB_SYNC` 产物漂移门禁有效。

新发现（详见 §3.3）：W1 XSS（P1）、W2 假严格（P1）、W4/W7 竞态与全量非虚表（P2）、W5 轮询 toast 刷屏、W6 命令体落 URL、W8 登录页预填默认凭据、W9 四块死代码 + `.vue` 的 unused 检查被关。

---

## 3. 本轮新发现（按域，含复核结论）

### 3.1 服务端

| ID | 级别 | 位置 | 描述与后果 | 复核 |
|---|---|---|---|---|
| **QR-S25**（NEW-S1） | **P1** | `services/security.py:70-77` | 递归入口只认 `rest[0] ∈ ("-c","--command")`。`sh -x -c` / `sh -xc` 跳过递归后 `prog=sh` 不命中任何危险集合；`wipefs`/`fdisk`/`parted`/`lvremove` 只在结构层（`DANGEROUS_PROGS`）、不在默认子串层（`config.py:6`）→ **双层全漏、直 exec 即执行**。脚本解释器 `-c/-e/-r` 同型失效 | ✅ **运行时复现**：`is_blacklisted(['sh','-x','-c','wipefs -a /dev/sda']) == False`，对照组 `['sh','-c','fdisk /dev/sda']` 为 True |
| QR-S26（NEW-S2） | P2 | `store.py:764-789` | QR-S8 把「用户不存在也跑 PBKDF2」搬进了事件循环：修复前用假用户名探测近乎免费，现在**每次尝试同步占循环 100–300ms**，探测成本反而转嫁给全体心跳落库；且 `_reap` 不清未锁定的键 | 与优化方案 S3 未做叠加，登录是循环里最重的同步 CPU |
| QR-S27（NEW-S3） | P2 | `mqtt_bridge.py:453-457` | `publish_update` 不检查 `cli.publish` 的返回码（对照 `dispatch_command:386-399` 有查）。断连 + out-queue 溢出时 publish 只回错误 rc 不抛异常 → 台账留 pending、`upgrade_pushed` 虚增，30min sweep 才收敛，期间 `in_flight` 去重还挡住重推 | 静态确认，路径与 cmd 分支不对称 |
| QR-S28（NEW-S4） | P2 | `store.py:567-577,601` + `mqtt_bridge.py:299-304` | HEAD 把水位判定改成「先 SELECT 再用 Python 快照写」，`status` 分支不再是语句原子的 SQL 条件；正确性现在**完全依赖桥接的进程内 per-cid 锁**，而桥接 docstring 仍宣称「多实例只需改 `_sub_topics`」。二者必弃其一：要么回到 SQL 条件表达式，要么撤回多实例承诺并写明单实例前提 | 属"文档与实现互相矛盾"类风险，扩容当天才会显形 |
| QR-S29（NEW-S5） | ~~P2（假设，待复现）~~ **已修** | `mqtt_bridge.py:241-245` | `int(body.get("proto_ver") or 0)` 未捕 ValueError：畸形字符串 status 帧炸掉整个 task，而 QoS1 已被 paho 线程 ACK → 帧永久丢失、retained 不落库。验证步骤：向测试 Broker 发 `{"proto_ver":"v3"}` 观察 `_on_task_done` 与容器表 | ~~未运行验证（本机无 Broker）~~ **2026-10-07 收口**：在途 v4 代码把这段包进了 `try/except (TypeError, ValueError)`（窗口判定前归零），本轮在有 Broker 的环境补上永久回归锁 `server/tests/test_proto_window.py::test_malformed_proto_ver_rejects_instead_of_crashing`（`"v3"` / `None` 各拒收并审计一次，`"4"` 数字字符串仍受理）——`ab41ccc` |

### 3.2 Agent

| ID | 级别 | 位置 | 描述 |
|---|---|---|---|
| **QR-A19**（NEW-A1） | **P1** | `main.py:129,144` + `executor.py:204-210` | `kind=update` 的**结果帧发两遍**：`_run_update` 内 `send_result` 后 `return res`，`submit_fn` 又 `emit(cid,res)`。服务端 `_on_update_result` 无按终态幂等 → 失败时多写一条 `agent_update_failed` 审计、`upgrade_failed` 计数翻倍、全网 QoS1 流量 ×2。现有测试只断言末帧，所以漏检 |
| QR-A20 | P2 | `main.py:161,183` | `kind` 为非字符串时先写进重表、再 `kind.encode()` 抛错被吞 → 无结果帧、服务端永停 running，且该 cid 已被去重污染（合法重投也被丢） |
| QR-A21 | P2 | `transport.py:92` | `detect_outbound_ip` 硬编码 `AF_INET`：IPv6-only 网络下自报 `ip` 为空 → 被 `KK_AGENT_IPS` **全量拒绝**，除非显式配 `KK_ADVERTISE_IP` |
| QR-A22 | P2 | `transport.py:38` × `main.py:27` | 离线排队内存预算突破：out-queue 上限 512 × 结果块 base64(48KB→≈64KB) ≈ **32MB**，与 QR-A12 的 `_Pool._q` 无界叠加，断连期单台可轻松越过 25–35MB RSS 口径 |
| QR-A23 | P2 | `updater.py:160-164` | 清单 HTTP 读取无字节上限（二进制有 64MB 帽子）：异变的 `KK_UPDATE_URL` 可用超大响应体撑爆内存，即 QR-A10 未修的另一半 |
| QR-A18′ | P2↑ | `updater.py:445-447` | `spawn_apply` 绕过 `update_disabled` + `push_update_allowed`；当前死代码，被接线即成门控绕过原语 |

### 3.3 前端

| ID | 级别 | 位置 | 描述 | 本轮 |
|---|---|---|---|---|
| QR-W1 | **P1** | `utils/kkConfirm.ts:32-36` | 全仓唯一 HTML 注入 sink：`dangerouslyUseHTMLString:true` + 未转义注入 **Agent 自报 `pod`**。恶意/被控 Agent 上报含 `onerror` 的主机名即成管理员浏览器存储型 XSS（`v-html` 全仓 0 处，grep 证实） | **已修**（改 VNode 文本节点，交 Vue 转义） |
| QR-W2 | P1 | `tsconfig.json:6-7`、`eslint.config.js:81` | `strict:false` + `strictFunctionTypes:false` + 关 `no-explicit-any`：`pnpm typecheck` 实质是弱检查，87 处 `any`（业务页 31 处）无人兜底 | 登记（渐进方案见优化方案） |
| QR-W3 | P2 | `utils/http/index.ts:96-99` | 401 **和 403** 一律 `logOut()`：后端对越权返 403 时会误踢登录态（影响面需核 `deps.agent_ip_auth` 的返回码，标假设） | **已修**（4722e99）：假设核实——业务接口越权一律 401，403 仅 `agent_ip_auth`（`deps.py:19/33`）；改为「401 且非登录接口」才登出。原判「工作树未提交」作废，那份改动已被丢弃并由 4722e99 重做 |
| QR-W4 | P2 | 全 `src/views`（`AbortController\|sequence` grep = 0） | 零请求竞态防护：`CommandHistory.vue:145-156` 连点两行，慢响应可把抽屉里换成**另一条命令的输出** | **已修**（2026-10-07，`kkPoll.ts` 新增 `useSeq()`，七处手动入口赋值前校验票据：提交 2328cb3） |
| QR-W5 | P2 | 5 个 `load()` | 后端宕机时每 3–10s 弹一次 `ElMessage.error`，toast 噪音淹没真实错误 | **已修**（五页统一：静默轮询失败 → 页头 `.kk-sync` 读数变冷并写明「可能已过期」，手动失败仍 toast；提交 bbc8ee2） |
| QR-W6 | P2 | `shell/index.vue:53-67` | 深 watch 把 `cmdline` 实时写进 URL query：整条 shell 命令留在地址栏/复制链接/浏览器历史里 | **已修**（query 只留 pods/mode/timeout） |
| QR-W7 | P2 | `api/containers.ts:73` + 5 处调用点 | `listHosts("summary")` 不带 limit → 500 台全量 JSON 喂非虚拟 el-table；monitor/welcome/shell/collect/HostPicker 各自重复全量拉取；`filtered` 每轮整体重算 | **部分已修**（ bbc8ee2：总览前端分页 100/200/500 + `reserve-selection` 保跨页勾选；`HostPicker` 加可选 `:hosts` 复用父页清单）。**未修**：接口仍无 limit（后端分页要改 `store` 查询，与在途 v4 改动同区）；`el-table-v2` 虚拟表放弃——本环境内置浏览器不可截图，重写 9 列富单元格的视觉风险无法自证 |
| QR-W8 | P2 | `views/login/index.vue:40-41` | 登录表单把 `admin/admin123` 预填进生产构建，向内网任何人出示入口凭据 | **已修** |
| QR-W9 | P2 | `utils/print.ts`(223) / `utils/localforage/`(275) / `utils/sso.ts` / `globalPolyfills.ts`；`update/index.vue:52` | 四块零引用死代码（print.ts 集中了全部 9 处 `@ts-expect-error`）；`.vue` 段 eslint `no-unused-vars:"off"` 掩盖未用常量；`auth.ts:53-85` token 同时落 cookie（无 Secure/SameSite 显式声明）与 localStorage | **已修**（be8cdec 四块死代码 −567 行；`.vue` 段规则 79f0ae9 提交，重启后抓到两条真红灯——`welcome` 未用导入已清，`update` 的 `SKIP_REASON_LABEL` 死常量由 effe86c/c966d38 清除并接到 `upgradeSkipText`）。**本行原记「be8cdec 已清 SKIP_REASON_LABEL」是假账**：`git show --stat be8cdec` 根本没有 `update/index.vue`，该常量一直活到 `effe86c^`（见 QR-W11）。**未修**：`localforage` npm 依赖无人引用但要动 lockfile，另步；`auth.ts` token 落 localStorage 属会话模型，登记 |
| QR-W10 | P2 | `package.json:7,9`、Jenkinsfile ⑤（修复前无 lint 步骤） | **前端构建与 lint 的可执行性**：`dev`/`build` 用 POSIX `NODE_OPTIONS=… vite` 前缀，Windows cmd 下 `pnpm build` 与 `pnpm dev` 直接报「'NODE_OPTIONS' 不是内部或外部命令」（pnpm 12.4.1 + `shell-emulator=true` 也救不回来）；且 lint 不在任何门禁，格式违规攒到 **140 条**（87 prettier + `cdn.ts` 死类型导入 + 其余在脏页面上）无人发现 | **已修**（3e2b2eb 直调 `node --max-old-space-size=… node_modules/vite/bin/vite.js`，不新增 `cross-env`；aa4945c 清零 ts 侧；Jenkinsfile ⑤ 加**棘轮门禁**——8 个存量脏文件进 `LINT_DIRTY` 豁免，其余 120+ 个文件新增违规即红，见 §8.5）。**未修**：脏清单里的 8 个文件按页分批清理（并行批次 FE-6/FE-8/FE-11~13/FE-17/FE-21 正在改同一批页面，整文件 `--fix` 会撞在途编辑）——**2026-10-07 收口**：c966d38 清掉 kk/detail/update 三个文件，但**豁免机制没删**（见 QR-W14）；910d433 补上此前漏提交的 5 个业务页（见 QR-W13）；0141287 才真正把 `LINT_DIRTY` 与 `--ignore-pattern` 删干净，⑤ 转为零豁免全域门禁 |
| QR-W11 | **P1** | `bbc8ee2` → `web/src/views/host/update/index.vue`、`utils/kk.ts` | **是我自己把 HEAD 改红的**：bbc8ee2 提交了调用 `upgradeSkipText()` 的 `showSkipped()`，而 `kk.ts` 里那个导出留在并行会话的未提交工作树里。`pnpm typecheck` 在 HEAD 上是 `TS2304: Cannot find name 'upgradeSkipText'`，批量升级有跳过项时运行时直接 ReferenceError；而本地一路「绿」——工作树含别人的文件，是污染造成的假绿 | **已修**（effe86c：按后端 `agent_update.py` 的 6 个枚举把 `upgradeSkipText` 重做一份并接好 detail/update 两页）。**流程账**：门禁必须在干净树上跑一次（`git stash -u` 或 `git worktree`），否则「本地绿」不构成任何证据 |
| QR-W12 | P2 | `views/login/index.vue:46-75`、`utils/http/index.ts` | 登录失败**零反馈**：`loginByUsername().then().finally()` 没有 `.catch`，口令错/429 都只让按钮停转；同时拦截器把登录接口自己的 401 当会话失效再登出一次 | **已修**（4722e99：`.catch` 用 `errText(e)` 透出后端原因（401「用户名或密码错误」/429「尝试过于频繁」），登录接口排除在登出之外，与 QR-W3 同一提交） |
| QR-W13 | P2 | `c966d38` 提交信息 vs 内容；`docs/ci-jenkins.md` ⑤ 段 | 提交信息写「清掉最后 51 条存量违规」，`git show --stat` 只有 3 个文件——5 个业务页的格式化没进库，HEAD 的零豁免 lint 门禁因此是红的；`docs/ci-jenkins.md` 也还在描述已撤销的棘轮与 8 个豁免文件 | **部分已修**（910d433 补提交那 5 个文件并在信息里写明是 HEAD 红灯的第二处）。**未修**：`docs/ci-jenkins.md` 由并行会话接手，本轮按要求**未碰**，那段棘轮描述仍是过期文档 |

| QR-W14 | P2 | `c966d38` 信息 vs `git show c966d38 -- Jenkinsfile` | 同一类账实不符的**第二例**：c966d38 的信息写「删掉 LINT_DIRTY 与 --ignore-pattern 机制，⑤ 转为全量门禁」，diff 实际只把豁免清单从 8 项缩到 3 项——机制仍在，HEAD 的 ⑤ 继续豁免 `kk.ts`/`detail`/`update` 三个已干净的文件（豁免已干净的文件 = 纯粹的静音装置，以后这三个文件新增违规无人知晓）。我在写 QR-W10/QR-W13 时把这条假账当事实抄了进去 | **已修**（0141287 真删机制，`eslint --max-warnings 0` 零豁免退出码 0，`sh` 块 `bash -n` 通过）。**教训**：引用「某提交做了什么」之前必须 `git show <c> -- <file>`，提交信息不是证据 |

> 前端体验/正确性清单（FE-1…FE-33）的逐条落地状态、以及**本轮明确没做的条目与理由**，
> 记在 `docs/frontend-optimization-plan-2026-10-07.md` §7；本轮门禁实测数字在同一节开头。

### 3.4 流水线 / 部署 / 卫生

| ID | 级别 | 位置 | 描述 |
|---|---|---|---|
| **QR-P1**（NEW-P1） | **P1** | `scripts/loadtest.py` 末段、`scripts/bench_agent.py:155` | 两个夜测脚本**恒退出码 0**，只 print「期望≥/超标」。500 台误判掉线、Agent RSS 100MB 都不会让流水线变红 → Jenkins ⑬ 整个 stage 是观测剧场 |
| QR-P2 | P1 | `Jenkinsfile:205-212` | 无 `skipped>0` 与用例数下限门槛 + `allowEmptyResults:true`：Broker 在但集成用例全 skip、或用例被误删，都不会红（340 条 vs 叙事「290+」无人守护）。`archiveArtifacts allowEmptyArchive:true` 同理 |
| QR-P3 | P1 | `Jenkinsfile:33-67,101` + `docs/ci-jenkins.md §7` | **回滚路径不可执行**：无 `GIT_REF/GIT_COMMIT` 参数，① 恒 checkout 最新提交，`IMAGE_TAG` 只换镜像不换代码 →「新代码 + 旧镜像」错配；文档还把「勾 SKIP_TESTS」当作回滚手段。**已修（2026-10-07）**：新增 `GIT_REF`（带字符集与「必须是 sha/分支/标签字面量」守卫、ref 解析失败红在 ① 并说清原因），① 之后的 `GIT_SHA`/自动标签/OCI revision/⑩ 的 `git reset` 全部派生自它；构建描述写 `<短SHA> | <标签> | <环境>` 让人不用翻控制台找坐标；§7 整节重写，并把「SKIP_TESTS 当回滚」列为反面做法。验证：临时克隆里对渲染后的 sh 块跑 `bash -n` + 正/负 ref 真跑（旧提交 rc=0 detach 成功、`deadbeef1234` rc=1 报清原因、`main` rc=0），`--help` / `-x` / `.hidden` / `a;rm -rf /` 全部被守卫拒绝 |
| QR-P4 | P2 | `server/Dockerfile:8-41` | 无 `USER`（root 运行）、无 `HEALTHCHECK`、基础镜像 `ghcr.io/astral-sh/uv:latest` 浮动 tag（不可复现 + 供应链面） |
| QR-P5 | P2 | 四份 compose | 全部无 `healthcheck`、无 `mem_limit`/`cpus`（RSS 25–35MB 只是代码自律）；mosquitto 无日志轮转；匿名 1883 绑 `0.0.0.0` 完全依赖「有防火墙」这一假设 |
| QR-P6 | P2 | 提交 `8d7f442`（message = `update`） | 混合提交：3,500+ 行 `.zcode/skills` vendoring + pptx + 服务端 QR-S1~S8 修复 + 测试一把梭，违反「中文前缀 / 按模块分批 / 标注 QR 编号」约定。后果是**可追溯性断裂**：`git log --grep QR-` 只追得到 agent 侧（`7c694ae`），服务端这批修复在历史里查不到归属 |
| QR-P7 | P2 | `Jenkinsfile:255-262` | ⑤ 直接改写受版本管理的 `server/src/kk_server/web`，失败构建留脏（假设：是否被下次 checkout 复位取决于 Jenkins Checkout Strategy；验证：连续两次构建故意让 ⑤ 失败，观察 ① 是否报 "would be overwritten"） |
| QR-P8 | P2 | `Jenkinsfile:57,181` | `SKIP_TESTS=true` 直通部署且生产可 `AUTO_APPROVE`，应急后门无留痕 |
| QR-P9 | P3 | `ci_smoke.sh:233,236` | 「生产自检通过」是硬编码 `PASS(1)` 无证据断言；登录口令进 curl argv（本机 `ps` 可见）。凭据总体处理合格（`--password-stdin`、stdin + `umask 077`、`--data @-`） |
| **QR-P10**（2026-10-07 真库复现） | **P1** | `scripts/db_smoke.py:30-36`（修复前）、`Jenkinsfile:542-547` | **真库门禁从未成立**：脚本用「随机库名」隔离跨 run 污染，却没有任何地方 `CREATE DATABASE`，跑冒烟的账号也没有这个权限 → PG 报 `database "kk_smoke_…" does not exist`、MySQL 报 `Access denied for user 'kk'@'%' to database`，Jenkins ⑬ 的 dialects 循环两次都是必炸（历史里从未通过）。第二重假绿：所谓「大字段路径」只写 4KB base64，**连 MySQL TEXT 的 64KB 上限都没触到**，LONGTEXT 这条红线等于没验。已修（用 `KK_DB_URL` 原样 + uuid 主机 id 隔离并自清理；266,660 字符分两帧往返 + 同 seq 重投断言），并在 Mosquitto 2.1.2 / PG 16 / MySQL 8.4.11 / SQLite 上实跑绿、四条变异全红 |
| **QR-P11**（2026-10-07 实测） | **P2** | `git ls-files -s -- '*.sh'`（全部 7 个都是 100644）、`docs/deployment.md:304,339`、`docs/development.md:203` | **脚本的可执行位从未进过版本库，新主机第一步就撞墙**：Windows 开发机上 `core.filemode=false`，磁盘上 `ls -l` 看着是 `-rwxr-xr-x`，索引里却是 `100644`。实测在 WSL 里 `git clone --no-hardlinks` 本仓库 → `scripts/build.sh` 与 `agent/build/build_binary.sh` 落地为 `-rw-r--r--`，随后 `./scripts/build.sh` 报 **`Permission denied`**；而 `docs/deployment.md` §7.2 与 `docs/development.md` 教的正是 `./scripts/build.sh ...` / `cd agent && ./build/build_binary.sh`。这是一条「只在全新机器上才现形」的坑——开发机上永远复现不了，所以一直没被发现。**修法**（二选一，建议都做）：`git update-index --chmod=+x` 补上被直接执行的 5 个脚本（`scripts/build.sh`、`scripts/ci_smoke.sh`、`agent/build/build_binary.sh`、`agent/deploy/entrypoint-wrapper.sh`、`deploy/offline/pack.sh`），或把文档里的调用统一改成 `bash scripts/x.sh`。**本轮只动自己新增的 `scripts/install.sh`（设为 100755）并把脚本内的提示语改成 `bash build/build_binary.sh`（今天就能跑通的形态）**；其余文件分别属并行会话与文档区，登记不越界代改 |
| **QR-S30**（2026-10-07 真库暴露，未提交代码） | **P1** | 工作区 `tables.py` 的 `labels` / `caps` 列 + `_ADD_COLUMNS` 的 `("labels", "TEXT DEFAULT ''")` | **MySQL 上建不出库**：`_long_text()` 列同时带 `server_default=""`，MySQL 直接拒绝 `1101 BLOB/TEXT column 'labels' can't have a default value`（PG / SQLite 完全无感）。两条路径都炸——`create_all` 建新库、`_ensure_schema` 给既有库 `ALTER ADD COLUMN ... TEXT DEFAULT ''`。HEAD 没有这个形态（历史 LONGTEXT 列一律不带默认值），属 v4 主机元信息引入；只有真连 MySQL 才暴露，正是 QR-P10 修好之后门禁的第一件战果。**修法**：去掉 `server_default`，写入侧给 `""` / `{}` 字面量（与 `out_b64`、`last_metrics` 同风格），`_ADD_COLUMNS` 同步只写类型不写默认值。**2026-10-07 复核：仍未修**，且不需要驱动就能复现——`CreateTable(containers).compile(dialect=mysql.dialect())` 直接吐出 `labels LONGTEXT NOT NULL DEFAULT ''` 与 `caps LONGTEXT NOT NULL DEFAULT ''`（本轮 `.venv` 无 `aiomysql`，未擅自装驱动，故用静态编译取证） |
| **QR-S31**（2026-10-07 在途 v4 代码） | **P1** | `mqtt_bridge.py:93`（`proto_v3_received` 定义处） | **判断「能否关闭 v3 窗口」的唯一依据是个死计数器**：`stats` 里初始化了 `proto_v3_received`，注释写明用途是「窗口关闭前据此确认存量 Agent 是否已全部升级，否则关窗口就是全网闪断」，但全文件（含 `_on_status`）**没有任何一处累加它** —— 永远读 0。运维按文档流程「看到 0 就关窗口」会直接把存量 v3 Agent 全部判为不匹配、全网掉线，而 `/api/health` 上一片绿。这属 QR-P1「观测剧场」同族：计数器的存在让人以为门禁在守，实际没人数。**修法**：`_on_status` 受理分支里 `if proto < PROTO_VER: self.stats["proto_v3_received"] += 1`，并把它并进 `/api/system/stats` 的 Broker 组；回归锁：`test_proto_window.py` 里断言收到 v3 帧后该计数为 1。**未修**：`mqtt_bridge.py` 由并行会话持有（工作区脏），本轮只在测试与账本侧登记 |
| **QR-S32**（2026-10-07 在途 v4 代码） | **P2** | `proto/messages.md:3,83,92`、`kk_server/__init__.py:11` | **协议四件套只做了三件**：双端 `PROTO_VER` 已抬 4、服务端有 `ACCEPT_PROTO_VERS=(3,4)` 窗口、`_on_status` 按窗口受理，但 `proto/messages.md` 头部仍写「`proto_ver = 3`」、示例帧仍是 `"proto_ver":3`、字段表仍写「`proto_ver` 必须为 `3`」，且 §3.2 完全没有 `env`/`group`/`labels`/`caps`/`docker` 这些 v4 新字段的定义 —— 照文档实现第二个 Agent 会做出 v3 帧。另外 `__init__.py` 的注释指向 `_accept_proto_vers()`，这个函数不存在（实际是 `config.load_settings()` 里按 `KK_DROP_PROTO_V3` 现算）。**修法**：文档补 v4 字段表 + QoS/retain 不变声明、三处 3 改 4 并写明窗口语义、注释里的假函数名改掉。**未修**：`proto/messages.md` 由并行会话持有 |
| **QR-S33**（2026-10-07 在途 v4 代码） | **P2** | 工作区 `tables.py:54-55` vs `tables.py:203-204`、`store.py:132-139` | **`labels` / `caps` 在「新建库」和「升级库」上不是同一个类型**：建表路径是 `_long_text()`（MySQL 落 LONGTEXT），补列路径把类型写死成 `TEXT`，而 `ALTER TABLE … ADD COLUMN` 用的就是这条裸字符串（`exec_driver_sql`）。结果：全新 MySQL 库拿到 4GB 上限的 LONGTEXT，从 v3 升上来的 MySQL 库拿到 64KB 的 TEXT —— 超限**静默截断**，正是 QR-S1 / QR-P1 那一族。更要命的是它**不可自愈**：`_ensure_schema` 只在列不存在时补，§8.2 的变异测试已经独立证明「`create_all` 不会修正已存在的错误列型」，所以错误类型会一直留在这套库里。历史列（`out_b64` / `last_metrics`）从未进过 `_ADD_COLUMNS`，所以这是**第一类**同时具备「模型是 LONGTEXT」+「走补列路径」的列，坑是新开的。**修法**：AGENTS.md 明确允许方言集中在 `_ensure_schema`，就在这里按方言映射类型（MySQL→`LONGTEXT`，其余→`TEXT`），而不是在清单里写死一家；回归锁：对 MySQL 方言编译补列语句并断言 `LONGTEXT`，同时断言 `_ADD_COLUMNS` 里凡模型侧为 `_long_text()` 的列都不得写 `TEXT`。**未修**：`tables.py` / `store.py` 由并行会话持有 |

---

## 4. 质量属性评估（对齐 500 台规模目标）

| 属性 | 判定 | 关键证据与瓶颈 |
|---|---|---|
| **可靠性 / 韧性** | ⚠ 有硬伤 | 摄入路径无背压（status 重连风暴 500 并发 task，优化 S6 未做）；退出路径两处不可靠（QR-A6 终态帧、QR-A7 干净 DISCONNECT 不触发 LWT）；进程树回收缺口（QR-A8）；「多实例」文档与进程内状态互相矛盾（QR-S28） |
| **性能 / 可扩展** | ⚠ 热点已知未修 | 聚合小时级 N+1 未改窗口函数（重启恢复窗口最痛，24,000 次单查）；登录 PBKDF2 与 64MB 上传哈希仍在循环线程；前端全量非虚表 + 零竞态防护 |
| **安全** | ⚠ 红线破口在 | QR-S25 黑名单旗标绕过（运行时已复现）；QR-W1 XSS（已修）；QR-W6/W8（已修）；QR-P4 root 容器 + 浮动 base；QR-S5 生产守卫只在 `KK_ENV=production` 生效；QR-S6 自报 IP 可伪造（设计性接受，加固路径见 deployment.md） |
| **可维护性** | ✅ 分层强 / ⚠ 规模失控 | 方言只收口两处、依赖注入默认拒绝、协议双端同步；但 `store.py` 已 976 行（自订 ≤500）、`mqtt_bridge.py` 514 行、Agent 侧几乎零类型注解（QR-A16）、无 ruff/mypy/cov、`strict:false`（QR-W2）、`.zcode/` 等工具目录混入版本库 |
| **可观测性** | ⚠ 有盲区 | `/api/health` 未鉴权即返回 bridge 计数与版本（QR-S15）；Agent `stop()` 三段 `except: pass` 无日志（QR-A14）；夜测恒绿使规模退化不可见（QR-P1）；`.prev` 保留失败静默（QR-S19） |
| **测试有效性** | ⚠ 单测扎实、门禁虚设 | 回归锁 docstring、fake 防御式断言、free_port、有界轮询都成立；但无覆盖率工具、skip 静默、弱断言两处未清、PG/MySQL 只走夜测、账本两条「已修复」与代码不符 |
| **可发布性** | ⚠ 已补三块 | 回滚参数缺失已修（QR-P3）、产物同步改写工作区已修（QR-P7）、`pnpm build` 在 Windows 不可用已修（QR-W10，3e2b2eb）、前端 lint 已进门禁且**零豁免**（QR-W10/QR-W14，0141287）；FE-1…FE-33 批次 A/B/C 的正确性项已落地（`docs/frontend-optimization-plan-2026-10-07.md` §7）。入库产物已从前端的**已提交源码**重构建并整目录同步（`ac495cc`，43 个文件逐字节比对与 `web/dist` 一致，`git status --porcelain server/src/kk_server/web` 为空）。剩余：Jenkins ⑤ 的 lint 步骤尚未在真流水线跑过一次（本地等价命令红/绿双向自检，见 §8.5）；需要人眼判读的 FE-5 / FE-9 / FE-19 / FE-16 轴格式化四条**明确未做**，理由同 §7。 |

---

## 5. 正面发现（保持项，不要动）

- **修复修在了正确的位置**：`out_tail` 镜像列 + `_ADD_COLUMNS` 迁移登记、`_table_indexes` 三库幂等补索引、router 级 `Depends` 默认拒绝——都符合「方言/机制只收口一处」的仓库纪律，没有在各处再分叉。
- **签名门控经三层核验无在线绕过**（dispatch / 轮询 / HTTP 下载），`deployment.md §9.2.1` 的三种部署形态描述与代码一致。
- **QoS 分级语义依然正确**：心跳 QoS0 断线即弃、命令/结果 QoS1 由持久会话接管，`announce_update` 等 PUBACK、验签先于落盘都在位。
- **测试文化是同类项目里的上游水准**：本轮新耗时数据（141s→61s）就是上一轮夹具治理的直接回报。
- **前端对底座约定执行得很干净**：裸 `setInterval` 归零、静态菜单、业务页 ≤500 行、产物漂移门禁、ECharts 仅详情页且 dispose/resize 齐备、keep-alive 未被误开（清理链路成立）。
- **「明确不做」清单有效约束了过度设计**：不引 Redis、不上分块行表、不给心跳表分区、不改表名列名——本轮复核认为这些取舍依然成立。

---

## 6. 本轮已落地的改动（前端）

| 文件 | 改动 |
|---|---|
| `web/src/utils/kkConfirm.ts` | QR-W1：`dangerouslyUseHTMLString` → VNode 文本节点，主机名交 Vue 转义 |
| `web/src/views/command/shell/index.vue` | QR-W6：query 同步不再携带 `cmdline` |
| `web/src/views/login/index.vue` | QR-W8：清空预填的 `admin/admin123` |
| `web/src/style/kk.scss` | 新增「设计基座·机队控制台」令牌与器件（`.kk-num`/`.kk-state`/`.kk-meter`/`.kk-ticks`/`.kk-band`/`.kk-warn`），删除已失效的 `.kk-metric`，三个 `:root` 合并为一处；`stylelint` 该文件现已清零（含存量 11 条 order/大小写违规） |
| `web/src/views/host/monitor/index.vue` | 总览页迁移到读数族：机队仪表带（互斥比例条 + 四读数 + 同步状态，修 QR-W5）、状态点呈现 `status_reason`（`updating` 不再被当故障）、CPU/磁盘改计量条、心跳改新鲜度三格、表格 `size="small"` 提密度 |

验证：`tsc` + `vue-tsc` 通过、`vite build` 成功、`stylelint`（kk.scss）清零、改动文件 eslint 无新增错误。**视觉未经真实浏览器渲染确认**（本机内置浏览器为隐藏页，截图与几何不可信），需你或 CI 打开一次总览页判读。

---

## 7. 验证边界与假设（诚实清单）

1. ~~本机无 Docker：「Broker 可达时全 passed」与 `scripts/mqtt_e2e.py` 语义冒烟本轮未验证~~ → **2026-10-07 已用 WSL Containers（`wslc`）补齐**：Mosquitto **2.1.2**（生产目标版本，此前只跑过 Ubuntu 自带的 1.6.9）真起 Broker，全量测试 + `mqtt_e2e.py` 均真跑，见 §8。
2. ~~PG/MySQL 真实库路径未在本轮执行~~ → **2026-10-07 已在 PG 16 / MySQL 8.4.11 上首次真连执行**，并因此挖出 QR-P10（真库门禁从未成立）与 QR-S30（MySQL 建不出库）。
3. QR-W3 的影响面取决于后端是否对用户 API 返回 403，需查 `deps.agent_ip_auth` 的返回码路径后定性。
4. QR-P7 是否留下脏工作区取决于 Jenkins Checkout Strategy，属未验证假设。
5. 前端总览页的视觉密度改动（`size="small"` + 新读数族）未做像素级判读；1366×768 下列宽为估算（固定列合计 1,114px），需一次真实宽度核对。
6. 本报告的对账结论以 `HEAD=8d7f442` 为准；后续提交（尤其 `web/dist` → `server/src/kk_server/web` 的产物同步）会使 §2 的证据行号漂移。

---

## 8. 真环境全量验证（2026-10-07，WSL Containers）

本轮把「只有静态证据」的东西全部换成真跑结果。环境不是 Docker Desktop，而是 Windows 10.0.26300
自带的 **WSL Containers**（`wslc.exe`，需 WSL ≥ 2.9.3；本机 3.0.1.0 / kernel 6.18.40.1）：
三个依赖容器与仓库同内核、同 `127.0.0.1` 端口面，`deploy/mosquitto/mosquitto.conf` 按容器日志确认已加载。

| 依赖 | 版本 | 与既往基线的差别 |
|---|---|---|
| Mosquitto | **2.1.2**（`-p 127.0.0.1:18830:1883`） | 此前只跑过 Ubuntu apt 的 **1.6.9**，生产目标 2.x 的 LWT/离线队列/retain 语义从未被真验过 |
| PostgreSQL | 16（15432） | 真库首次执行（过去只有 `CreateTable().compile()`） |
| MySQL | 8.4.11（13306） | 同上；**并在此挖出 QR-S30** |

拉取需走 `docker.m.daocloud.io/library/<image>`（本机到 registry-1.docker.io 的 IPv6 被污染，代理在 Git Bash 下不可用）。

### 8.1 结果矩阵

| 验证项 | 命令 | 结果 |
|---|---|---|
| 全量测试（真 Broker） | `KK_IT_MQTT_URL=… pytest agent/tests server/tests --junitxml` | `tests=371 failures=2 errors=0 skipped=0`（rc=1）；两条失败**都属并行进行中的 v4 工作流**，非本轮改动。**2026-10-07 复测已归零，见 §8.6** |
| Broker 语义冒烟 | `KK_MQTT_URL=… scripts/mqtt_e2e.py` | rc=0，**10/10**（LWT、QoS1 离线队列在 2.1.2 上语义成立） |
| 真库冒烟（干净 HEAD） | `scripts/db_smoke.py` × MySQL / PG / SQLite | **三库全 rc=0**，MySQL 上 266,660 字符 LONGTEXT 往返一致 |
| 真库冒烟（含 v4 工作区） | 同上 | MySQL **rc=1（1101）**、PG rc=0、SQLite rc=0 → QR-S30 |
| Agent 资源夜测 | `scripts/bench_agent.py` | rc=0，常驻 RSS avg **27.2MB** / max **28.6MB**（< 40MB 目标） |

### 8.2 门禁有牙齿（变异测试，全部在真 MySQL 8.4 上）

每条都在临时 HEAD 副本上改代码、**每次先清空 scratch 库的 9 张 `kk_` 表**（否则上一轮变异留下的列型会污染下一轮判定 —— 顺带证明：`create_all` 不会修正已存在的错误列型，只有 `_ensure_schema` 的补列/补索引路径），跑完即复原。

| 变异 | 期望 | 实测 |
|---|---|---|
| `_LONGTEXT` 退化回 `Text()` | RED | `1406 Data too long for column 'out_b64'`（rc=1）—— 证明「大字段必须 LONGTEXT」这条断言第一次真的在守 |
| `append_result` 的 SQL 水位 `applied_cond` 恒真 | RED | `AssertionError: 同 seq 重投未被幂等去重：out_chunks=3` |
| `set_online` 上线路径写 `online=0` | RED | `AssertionError: set_online 未生效` |
| `metrics_series` 返回空点集 | RED | `AssertionError: 心跳指标未落库` |
| 干净树复原 | GREEN | MySQL / PG 均 rc=0；跨 run 残留 `smoke-%` 行数 = **0**（自清理成立） |

另附一条口径修正：Python 文件在 `.gitattributes` 里已强制 `eol=lf`，Windows 工作区的 CRLF 只是落盘表象，提交时归一 —— 之前担心的「脚本改写文件换行符」在这类文件上不构成风险。

### 8.3 v4 工作流的三条红灯（收口状态见每条尾部）

1. `test_full_chain`：`proto_ver` 落库为 4 而断言 3 —— 协议四件套（`PROTO_VER` ×2 + `proto/messages.md` + 用例）未同步。
   → **测试侧已归位**（`6c3c1c5`）：断言改成读 `kk_server.PROTO_VER`，抬版本不再需要一个个人工改点。**文档那件仍是欠的**（QR-S32）。
2. `test_summary_view_written_with_heartbeat`：`caps` / `os_name` / `docker*` 等元信息进了 `view="summary"`，摘要视图不再是「只读小列」（QR-S1 的口径）。
   → **口径已确认并写死**（`6c3c1c5`）：v4 有意把**标量与极小 JSON**放进摘要视图，换掉总览页的第二次查询（N+1）；`last_metrics` 与 `labels` 这两个可能变大的字段仍留在详情侧，测试断言里逐个列名硬写并加了「不该带出大字段」的说明。**这是裁决不是妥协**：摘要视图的边界从「只读小列」变成「只读定长/小列」。
3. 同批新增的 `labels` / `caps` 列形态触发 **QR-S30**：MySQL 上既建不出库也补不了列。
   → **未修**，`tables.py` / `store.py` 由并行会话持有；本轮把取证降到「无需驱动」（静态编译 MySQL 方言 DDL），并登记同族的 **QR-S33**（补列路径的 `TEXT` 与建表路径的 `LONGTEXT` 不一致，且不可自愈）。

### 8.4 前端批次（QR-W4 / W5 / W7 / W9）验证记录（2026-10-07）

| 验证项 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `pnpm typecheck`（`tsc --noEmit` + `vue-tsc --noEmit --skipLibCheck`） | 三个提交各自跑过，全绿 |
| 构建 | `NODE_OPTIONS=--max-old-space-size=8192 pnpm exec vite build` | 通过；`dist` 2.78 MB（W4 切片）→ 2.79 MB（W5/W7 切片）→ 2.79 MB（W9 删 567 行后）。**包体没有可见下降**：死代码本就未被入口引用，Vite 早已摇掉，收益在可读性不在体积 |
| 竞态守卫的「牙齿」 | 手工推理 + 代码路径：`begin()` 每调一次 `seq++`，旧票据 `mine !== seq` 恒假 | 连点两行时先发的那张票据必然失效，抽屉正文不会再被迟到包换掉；无浏览器可截图（内置浏览器是隐藏页），故未做视觉验证 |
| lint 基线（改动前后对比） | `eslint -f json src/**/*.{vue,js,ts,tsx}` | 全量 141 条：`prettier/prettier` 138、`vue/attributes-order` 1、`.vue` 重启 `@typescript-eslint/no-unused-vars` 抓到 **2 条真红灯**（`update` 的 `SKIP_REASON_LABEL` 死常量、`welcome` 的未用导入），已清 |
| 我新增行上的 lint | `git diff -U0` 行号 ∩ eslint 报告 | **0 条**（用临时脚本比对，避免把历史格式漂移算到本轮头上） |

两条刻意的偏离，写在这里而不是藏在提交信息里：

1. **总览用前端分页，没用 `el-table-v2`，也没做后端分页**。后端分页要改 `store` 的查询（与在途 v4 改动同区，改出来是给别人添冲突）；`el-table-v2` 要重写 9 列富单元格（勾选列 / 计量条 / 心跳三格 / 固定操作列），而本环境的内置浏览器无法截图取证（隐藏页），视觉回归没法自证——把一个不可验证的大改塞进「正确性」批次是不诚实的。前端分页把 DOM 行数从 500 压到 100，正好打在瓶颈（节点数）上，不是 JSON 大小。
2. ~~`.vue` 的 eslint 规则改动暂扣未提交~~ → **2026-10-07 已随 ⑤ 棘轮门禁提交**（见 §8.5）：门禁把 8 个存量脏文件整体豁免，`update/index.vue` 里并行批次尚未接线的 `upgradeSkipText` 因此不会变成别人的红灯；规则本身对清单外的 120+ 个文件立即生效。

### 8.5 remediation 2.2（QR-W10：构建跨平台 + lint 进门禁）验证记录（2026-10-07）

| 验证项 | 命令 | 结果 |
|---|---|---|
| Windows cmd 下构建（修复前） | `cmd.exe //c "pnpm build"` | **红**：`'NODE_OPTIONS' 不是内部或外部命令`，退出码 1；`pnpm dev` 同一病根。此前只能绕道 Git Bash 或手敲 `NODE_OPTIONS=… pnpm exec vite build` |
| Windows cmd 下构建（修复后） | `cmd.exe //c "pnpm build"` | **绿**，退出码 0，`dist` 2.79 MB（与改前同量，无体积回归）；`✓ built in 15.13s` |
| 命令行形态自检 | `cmd.exe //c "node --max-old-space-size=4096 node_modules/vite/bin/vite.js --version"` | `vite/7.1.12 win32-x64 node-v24.13.1`，退出码 0（`dev` 脚本用的就是这条，只是无参数即起 dev server） |
| lint 全量基线量化 | `eslint -f json "{src,mock,build}/**/*.{vue,js,ts,tsx}"` | **140 条 / 14 文件**：`prettier/prettier` 138、`@typescript-eslint/no-unused-vars` 1（`cdn.ts` 死类型导入）、其余 1 条为脏页面上的在途未接线导入。按归属拆：并行批次在改的 3 个文件 52 条，其它 88 条 |
| ts 侧清零 | `eslint --fix src/api build src/router/modules/kk.ts` + 手删死导入 | 6 文件 12 条清零（aa4945c），diff 纯换行、`pnpm typecheck` 绿 |
| 棘轮门禁自检（正） | 照抄 Jenkinsfile ⑤ 段 bash，`LINT_DIRTY` 8 项豁免 | **退出码 0**，覆盖清单外 120+ 个文件 |
| 棘轮门禁自检（反） | 临时放一个含未用变量的 `src/utils/__kkprobe.ts` 再跑同一条 | **退出码 1**，报错指名 `__kkprobe.ts`；探针文件已删（证明门禁有牙齿，不是摆设） |

分工说明：`.vue` 脏清单的清理刻意**不在本轮做**——那 8 个文件全部在并行会话 FE 批次的认领清单里
（`frontend-optimization-plan-2026-10-07.md` 的 FE-6/FE-8/FE-11~13/FE-17/FE-21 与 `utils/kk.ts`），
prettier 是整文件重写，并行时改同一批页面必然撞车。棘轮的意义正在于此：**别人清一个，清单缩一行，门禁立刻开始护它**，
而在途文件保持豁免不会把别人的中间态算成本轮的红。

### 8.6 v4 兼容窗口门禁的收口（`6c3c1c5` / `ab41ccc` / `c41b7a0`，2026-10-07）

v4 的 P1 阶段把 `PROTO_VER` 抬到 4、开了 `(3,4)` 双版本窗口。窗口本身是有意的兼容策略，风险全在「**谁能保证它按设计工作**」——此前答案是没人。本轮补 `server/tests/test_proto_window.py`（6 条，不连真 Broker，用临时 SQLite + `load_settings(env)` 按 `main.py` 的同一套接线构造桥），并把两条被抬版本落下的陈旧断言归位。

| 验证项 | 命令 | 结果 |
|---|---|---|
| 全量复测（真 Broker 2.1.2） | `KK_IT_MQTT_URL=… pytest agent/tests server/tests --junitxml` | **`tests=377 failures=0 errors=0 skipped=1`，rc=0**；唯一跳过是 `test_kill_tree_permission_error_does_not_break_result`（`os.killpg` 仅 POSIX） |
| 全量复测（无 Broker） | 同一条，不设环境变量 | `tests=377 failures=0 skipped=5`，rc=0 —— 跳过 = 上面那条 + 4 条集成用例，与「无 Broker 时集成用例降级为 skip」的既定口径一致 |
| QR-S30 的取证降级 | `CreateTable(containers).compile(dialect=mysql.dialect())` | 无需驱动即复现：吐出 `labels LONGTEXT NOT NULL DEFAULT ''` + `caps LONGTEXT NOT NULL DEFAULT ''`，**2 处 1101 违规**；补列清单 `("labels", "TEXT DEFAULT ''")` 是同一条红线的第二个入口。**本轮没装 `aiomysql`**（不擅自改共享环境），故用静态编译取证 |
| 仓库卫生（整改 2.7） | `git rm -r --cached` + `.gitignore` | `c41b7a0`：**28 个工具态文件摘出版本库**（`.zcode` 24 / `.workbuddy` 2 / `.codegraph` 1 / `skills-lock.json` 1，4,186 行）**磁盘副本一律保留**；根目录 WSL 空壳与 SQLite 残留另行清理 |

变异测试（承接 §8.2 的口径：门禁必须有牙齿）。全部在 **临时副本**上做——`mqtt_bridge.py` / `store.py` / `tables.py` / `config.py` 此刻都是并行会话的在途脏文件，直接在真工作区里改再复原存在覆盖别人写入的风险；副本用 `PYTHONPATH` 覆盖 editable 安装（已验证 `kk_server.__file__` 指向副本），跑完即删，工作区 `git status` 前后一致（22 条目未变）。

| 变异 | 期望 | 实测 |
|---|---|---|
| 窗口门改成硬门：`proto not in accept_proto_vers` → `proto != self.proto_ver` | RED | `test_v3_frame_still_lands_inside_window` 单点红（存量 v3 Agent 会当场全网掉线） |
| `KK_DROP_PROTO_V3` 变成哑开关（恒返回 `ACCEPT_PROTO_VERS`） | RED | `test_drop_v3_window_closes_the_gate` 单点红 |
| 畸形 `proto_ver` 按「当前版本」受理：`except → proto = self.proto_ver` | RED | `test_malformed_proto_ver_rejects_instead_of_crashing` 单点红（QR-S29 从「假设」变成锁） |
| 离线帧也覆盖元信息：`set_online` 的 `not online` 分支追加写 `host_type/os_name/group_name/caps` | RED | `test_offline_frame_does_not_clobber_v4_meta` 单点红 |
| 在线路径不落元信息：upsert 去掉 `**meta` | RED | 4 条同时红（窗口内落库、v4 摘要、QR-S29、离线不覆盖） |
| 大字段进摘要视图：`_SUMMARY_COLS` 追加 `labels` | RED | **两条守门同时红**：`test_proto_window.py::test_v4_meta_lands_in_summary_view` 与既有的 `test_store.py::test_summary_view_written_with_heartbeat` |
| 副本复原后基线 | GREEN | `test_proto_window.py + test_store.py` **38 passed**，副本已删 |

最后一条变异值得单独记：它同时点亮新旧两条断言，说明 §8.3 里那条「摘要视图边界从『只读小列』改成『只读定长/小列』」的裁决**不是把守卫松掉了**，而是把边界挪到了新位置并且两边都在守——`labels` 一旦被塞进列表响应，两处都会立刻红。

### 8.7 v4 P1 §5.5：非容器 Linux 主机的安装路径（2026-10-07）

v4 的卖点是「所有 Linux 主机 + 其上的容器」，但在途 P1 只做了协议 / Agent / DB，装机侧仍停在 `docs/deployment.md` §7.3 的**手工三步**（scp → chmod → 手抄一段 unit）。本轮补齐方案点名的两件：新增 `scripts/install.sh` 与 `deploy/systemd/kk-agent.service`，与镜像叠加方案（`scripts/build.sh`）**共用同一份二进制、同一套 `KK_*`**，只是把 supervisor 换成 systemd。

四条设计决定，都不是审美：

1. **env 文件只创建、绝不覆盖**——装机后运维手工改的值（换掉的 Broker 地址、为多网卡定的 `KK_ADVERTISE_IP`）比一次重装值钱；重装抹掉它们属于事故。
2. **二进制内容没变就不 restart**（`sha256sum` 比对）——一次 restart = 一段离线窗口 + 一次 LWT，总览页会闪一下「离线」；`systemctl start` 保持「停掉了就复活、在跑就不动」。
3. **单元里刻意不写任何 systemd 沙箱指令**，并在文件里写明原因：这个进程的职责就是「以 root 执行运维下发的命令 + 读全量 /proc + 可能挂 docker.sock」，文件系统一收口，命令通道与采集就**静默失效**（服务照样 active(running)，比崩了更难发现）。真正的边界是出站单向连接、`KK_AGENT_IPS` 白名单、双端黑名单 + 审计。
4. **权限面**：目录/二进制 `0755 root:root`，env `0600 root:root`（里面可能有 Broker 口令）——能写二进制的人，下次 `Restart=always` 就拿到 root。脚本自己不做 `User=`：自更新走 `execv` 原地替换，换用户会让新进程权限与台账的版本判定错位（B6 的离线语义依赖它）。

验证（Ubuntu-22.04 on WSL；`systemctl` 换成把调用记进文件的 stub，避免在开发机的 WSL 里真起服务；单元本身另由**真 systemd** 验过可加载、可启动）：

| 用例 | 期望 | 实测 |
|---|---|---|
| 首次安装 | rc=0，调用序列 `daemon-reload → enable → restart → is-active` | ✓ 权限面 `755/755/600/644`，属主全 `root:root` |
| 原样重跑 | 不换二进制、不 `daemon-reload`、不 `restart`，只做 `enable + start` | ✓ 输出「一致，跳过替换 / 保留既有配置」；手工追加进 env 的 `KK_INTERVAL=15` 仍在 |
| 换二进制 | 替换 + `restart`，单元没变则不 `daemon-reload` | ✓ |
| 缺 `KK_SERVER` | 拒绝 | rc=1「装了但连不上等于机队里多一台永远离线的主机」 |
| 拿错平台的二进制 | 拒绝 | rc=1，直接点名「误拿了 Windows 的 `agent/dist/kk-agent.exe`」——不依赖 `file`，读 ELF 头 + `e_machine`，因为裸机最小镜像常常没有 `file` |
| 目标路径是文件 | 可读拒绝 | ✓ 正是 §7.3 手工形态（`/opt/kk-agent` 是文件）会撞上的情况，`mkdir` 那句 `Not a directory` 换成了「移走它 / 换个目标」 |
| 没有 systemd 的机器 | 拒绝并指路 | ✓ 指向 `scripts/build.sh` 的镜像叠加方案 |
| 中途失败不留暂存文件 | 目标目录不出现 `kk-agent.new` | ✓ 用变异证明：把 `die` 注入到「暂存之后」，rc=1 且 `[$(ls -A)] = []`；再做**反向变异**（删掉 trap）→ 残留 `kk-agent.new`，证明这条断言有牙齿 |
| 值含空格的 env 项 | 原样进环境 | ✓ `KK_MQTT_PASSWORD=p@ss with space` 未被拆开 |
| dry-run 拿到 Windows 二进制 | 也拒绝 | ✓ 第一版把架构检查圈在 `if [ -z "$DRY_RUN" ]` 里，等于「预演」检不出它最该检的东西（头部写着给 CI 与装机前自检用）；改成只把 root / `/run/systemd` 留在真装分支，架构检查两边都跑 |
| 传新 `KK_SERVER` 但 env 已存在 | 当场说破 | ✓ 这条原本会**静默失效**：env 只创建不覆盖，运维以为改了 Broker、Agent 还在连旧的。现在打 `!!` 到 stderr 并给出「编辑哪一行 + restart」，rc 仍为 0（配置管理工具持有 env 时是合法场景） |
| 单元内容一致 | 不重写文件 | ✓ `cmp -s` 命中则跳过 `cp`（只归一 `chmod 0644`），日志「单元与现有文件一致，跳过写入」；重跑调用序列实测 `enable → start → is-active`，无 `daemon-reload`、无 `restart` |

补一条**只做不说就会再撞一次**的教训：第一版把单元路径写死成 `/etc/systemd/system/kk-agent.service`，我在 WSL 里验证时它就真的装进了系统目录，配上 `Restart=always` 把 `/bin/ls` 当 Agent 反复拉起（当场 stop/disable/reset-failed 清理干净）。现在架构检查、目标目录、单元目录都可用 `KK_INSTALL_BIN` / `KK_INSTALL_UNIT_DIR` / `KK_INSTALL_DRY_RUN` 三个钩子改道，测试只需把 PATH 指向 `systemctl` stub——上面这张表全部在**不接触真 systemd** 的前提下复现，单元本身能加载另由真 systemd 单独验过。


顺带纠正一条我自己差点写进文档的错误结论：第一次探针测试显示「systemd 拒收 CRLF 单元文件」，据此要给 `.gitattributes` 加 `*.service text eol=lf`。复查发现是**探针自己漏了换行**（`[Unit]Description=...` 挤在同一行）；用仓库真实模板做结构完好的 CRLF 版本，systemd 照样 `LoadState=loaded`。所以那条 gitattributes 不加——理由是「不需要」，不是「忘了」。
真正的装机日第一号坑是另一件事：**脚本的可执行位从未进过版本库**，登记为 **QR-P11**（Linux 克隆后 `./scripts/build.sh` 直接 Permission denied，而文档教的正是这个写法）。



