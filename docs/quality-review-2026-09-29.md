# KontainKeeper 全量质量审视报告（2026-09-29）

> 审视日期：2026-09-29　代码基线：`main` @ ff38793
> 审视范围：`agent/src/kk_agent/`（约 2.3k 行）、`server/src/kk_server/`（约 2.9k 行）、`agent/tests/` + `server/tests/`（约 5.0k 行）
> 方法：三路独立深审（服务端 FastAPI 规范与 async 正确性 / Agent 线程模型与更新安全 / 测试套件完整度）+ 高严重度条目逐条人工核验（file:line 全部确认）+ 全量测试复跑。
> 交付性质：**缺陷账本 + 修复记录**。缺陷编号使用 `QR-` 前缀（Quality Review），与 `completion-plan-mqtt.md`（P0-x/P1-x/R-x）及 2026-09-12 质量分析（P0/P1/P2）的历史编号不冲突。状态列随修复进度更新。

---

## 0. 结论速览

**总体判定：B+/A- 档工程水准。** 架构分层纪律强、async 正确性总体优秀、测试文化成熟（回归锁 docstring、手写 fake 防御式断言、有界轮询）。风险集中在四处：

1. **一条默认开启的远程代码执行链**（QR-P0-1）：匿名 Broker + HMAC 默认关 + 推送更新不受 `KK_UPDATE_DISABLED` 门控；
2. **黑名单可被 `sh -c` 包装绕过**（QR-P0-2）；
3. **服务端数据层三处「规模叙事下的真实破绽」**：列表路径拖大字段、SQLite PRAGMA 只配首连、聚合查询索引失配（QR-S1~S3）；
4. **测试盲区**：MQTT 运行面与 PG/MySQL 真库执行几乎无覆盖（QR-T1/T2）。

**测试基线（本次实测）**：310 条，306 passed / 4 skipped（无 Broker 的集成用例）/ 0 failed，141s。源码:测试 ≈ 1:1（5.2k : 5.0k 行）。

---

## 1. 缺陷账本

### 2.1 P0

| ID | 位置 | 描述 | 状态 |
|---|---|---|---|
| QR-P0-1 | `agent/src/kk_agent/main.py:146-149`、`config.py:78/82`、`updater.py:215-226` | **推送更新不受 `KK_UPDATE_DISABLED` 门控 + 默认接受未签名清单**。`KK_UPDATE_DISABLED=1` 只挡轮询路径（`main.py:248`、`updater.py:369`），`kind=update` 命令直通 `runner.submit_fn` 触发「下载+execv」；且 `KK_UPDATE_REQUIRE_SIG` 缺省 False、未配 HMAC key 时 `_verify_signature` 直接放行。v3 默认匿名 Broker 下，任何能向 `kk/v1/{host}/cmd` 发消息的客户端（无需经过服务端 API）都能让 Agent 替换二进制并 execv——sha256 只是完整性校验不是认证手段。运维显式关掉更新后 RCE 入口仍敞开，超出「内网可信」设计性接受的范围。**修复方向**：dispatch 层 update 命令尊重 `KK_UPDATE_DISABLED`；MQTT 推送路径默认要求签名（配 key 或显式 `KK_UPDATE_ALLOW_UNSIGNED=1` 才接受未签名清单），HTTP 轮询路径语义不变。 | 已修复 |
| QR-P0-2 | `server/src/kk_server/services/security.py:33-34,100` | **命令黑名单可被 shell 包装绕过**。WRAPPERS 集合不含 `sh/bash/dash/zsh`，`{"kind":"shell","argv":["sh","-c","rm -r -f /usr"],"use_shell":false}` 走 `_check_tokens(argv)` 时 prog=sh 不命中任何危险集合；参数拆写（`rm -r -f`）也躲过默认子串兜底。结构校验对「shell + `-c` 载荷」整体失效。**修复方向**：shell 类程序的 `-c` 载荷按 shell 语义递归校验。 | 已修复 |

### 2.2 P1 — 服务端

| ID | 位置 | 描述 | 状态 |
|---|---|---|---|
| QR-S1 | `server/src/kk_server/models/store.py:420` | **`list_commands` 无条件拖 `out_b64` 大字段**。`_CMD_COLS`（`tables.py:148`）刻意排除 `out_b64` 后又被无条件加回；单行最大 ~5.6MB（LONGTEXT），列表 500 行、导出 2000 行最坏 GB 级 DB→应用传输；`tail=False` 时取回即丢。与 `commands.py:147` 文档声明的「列表只给 out_tail」矛盾。**修复方向**：落 `out_tail` 列（写入时维护，走 `tables._ADD_COLUMNS` 迁移），列表/导出只查小列。 | 已修复 |
| QR-S2 | `server/src/kk_server/controllers/deps.py:7-13` + 全部 controllers | **鉴权不走框架依赖注入**。全仓零 `Depends`/`Annotated`，20+ 端点手写 `await current_user(request)`；新端点漏写一行即裸奔，靠人工纪律而非框架保证；OpenAPI 无 security scheme、无 tags。**修复方向**：router 级 `dependencies=[Depends(...)]` 收口（默认拒绝），需要用户身份的端点用 `Annotated` 别名取值。 | 已修复 |
| QR-S3 | `server/src/kk_server/models/store.py:51-55` | **SQLite PRAGMA 只对 setup 首条连接生效**。`busy_timeout=5000`/`synchronous=NORMAL` 是每连接设置，挂在 `engine.begin()` 的那一条连接上；池化新建连接全部回落 `busy_timeout=0`，并发写下 `database is locked` 防护实际失效（WAL 是持久的，这两个不是）。**修复方向**：`busy_timeout` 走 `connect_args={"timeout": 5}`；`synchronous` 尝试挂 pool connect 事件。 | 已修复 |
| QR-S4 | `server/src/kk_server/models/store.py:792-800`、`tables.py:53` | **小时聚合查询无法命中索引**。`_aggregate_hours` 只按 `ts` 过滤，唯一索引前导列是 `pod`（`idx_hb_pod_ts(pod, ts)`）→ 每个小时桶全表扫描，回补窗口最长 3 天 = 72 次全扫（500 台 × 60s 心跳 ≈ 144 万行/表），发生在 janitor 清理趟。**修复方向**：给小时表补 `ts` 单列索引，经 `_ensure_schema` 幂等创建。 | 已修复 |
| QR-S5 | `server/src/kk_server/config.py:99-103` | **生产放行守卫仅在 `KK_ENV=production` 时生效**。不设该变量的生产部署静默退化为「白名单空 = 全放行」。属设计取舍（环境变量约定），但值得在部署文档显著位置警示。 | 登记 |
| QR-S6 | `server/src/kk_server/services/mqtt_bridge.py:179` | MQTT 侧白名单基于 Agent 自报 `ip`，控制了 Broker 权限即可伪造。v3 设计已声明「内网可信前提」，REST 侧 `deps.agent_ip_auth` 用真实源 IP 是对的；此项为架构性已知取舍，加固路径见 `docs/deployment.md`（Broker 鉴权）。 | 登记（设计取舍） |
| QR-S7 | `server/src/kk_server/controllers/exporting.py`（四个导出端点）、`agent_update.py:330` | 导出/二进制下载（8-12MB 资产外带）不留审计，批量数据外泄不可追溯；命令创建/上传/回滚/升级/登录均有审计。**修复方向**：导出与下载端点补 `store.add_audit`。 | 已修复 |
| QR-S8 | `server/src/kk_server/models/store.py:711-712` | `verify_admin` 用户不存在时立即返回 False，存在时才跑 310k 轮 PBKDF2（百毫秒级差）→ 用户名枚举时序侧信道。登录限流（`auth.py:51-91`）缓解但不消除。**修复方向**：用户不存在时对假哈希跑一次同代价校验。 | 已修复 |

### 2.3 P1 — Agent

| ID | 位置 | 描述 | 状态 |
|---|---|---|---|
| QR-A1 | `agent/src/kk_agent/main.py:140-141`、`transport.py:133` | **命令无去重**。clean_session=False + QoS1 是至少一次投递，PUBACK 竞态窗口内 Broker 重发会导致同一命令重复执行（shell 副作用不可幂等）。**修复方向**：dispatch 层加有界 LRU 去重，重复 cid 丢弃并告警日志。 | 已修复 |
| QR-A2 | `agent/src/kk_agent/updater.py:347-359` | **execv 无失败回退**。新二进制落盘成功后若 execv 失败（架构不符、ENOEXEC），进程继续跑旧代码但磁盘已是新二进制，下次重启即变砖，无备份恢复；且异常沿 `apply_manifest_receipt` 逃逸，命令退避账本不记账。**修复方向**：替换前保留旧二进制备份，execv 失败时恢复备份并走正常失败回执。 | 已修复 |
| QR-A3 | `agent/src/kk_agent/plugin_loader.py:62-75` | **插件加载锁无超时**。`spec.loader.exec_module` 在 `_lock` 内执行且无超时，插件 import 阶段卡死会让锁永久被占 → 心跳停摆（busy 永置位），且 8 个池线程与 shell 命令共用，全部饿死。现有 quarantine 只覆盖 `collect()` 超时不覆盖加载阶段。**修复方向**：加载挪出锁外 + 加载线程带超时，超时隔离进 quarantine。 | 已修复 |
| QR-A4 | `agent/src/kk_agent/collector.py:102-103` vs `:216` | **disk_io 返回契约破裂**。`disk_io_counters()` 返回 None 时 `return {}`，正常路径返回二元组，`_item_disk_io` 解包必抛后又被 `collect_items` 裸 `except` 吞掉（`:262-263`）——相关平台（Windows 性能计数器不可用等）disk_io 永久静默缺失。测试全绿但生产必现。**修复方向**：异常路径返回 `({}, None)` 同构二元组。 | 已修复 |
| QR-A5 | `agent/src/kk_agent/main.py:124-128`、`updater.py:352-359` | 更新**成功**路径 `_apply_manifest` 以 execv 结束永不返回，`send_result` 只在失败路径执行——成功更新的 `cid` 永远拿不到 `done=true` 结果帧。经核对，服务端设计的成功终态是「版本到达判定」（`store.finish_updates_reaching`，Agent 重启上线后由 janitor 判定成功），失败才走结果帧；配合 QR-A2 的 execv 失败回退（失败必回执），语义闭环成立。**结论：设计如此，补注释说明即可，不改协议。** | 已修复（注释澄清） |
| QR-A6 | `agent/src/kk_agent/main.py:61-62` | 分块 publish 失败（队列满）时补发的 `_fail_frame` 走同一个满队列，同样失败 → 终态帧也送不出去，只剩服务端超时兜底。防线在核心场景（断网积压溢出）失效。**修复方向**：终态帧失败时丢最老排队帧强插，或至少日志告警（当前静默）。 | 登记 |
| QR-A7 | `agent/src/kk_agent/transport.py:211-217` | `stop()` 中 `publish_status(False)` 后立刻 `disconnect()` 未 `wait_for_publish`，离线状态帧与 DISCONNECT 包竞争可能未出网；干净 DISCONNECT 不触发 LWT，服务端可能看不到下线。 | 登记 |
| QR-A8 | `agent/src/kk_agent/executor.py:62-70,128-132` | 进程树回收缺口：SIGTERM 后组长 `wait(3)` 成功即 return，忽略 TERM 的孙进程无 SIGKILL 兜底；`os.getpgid` 抛 ProcessLookupError 也直接 return；通用异常分支 `_kill_tree()` 后未 `p.wait()` 留僵尸。Windows 路径只有 `taskkill /F` 无体面终止窗口（与 docstring 不符）。 | 登记 |
| QR-A9 | `agent/src/kk_agent/executor.py:195-197` | `use_shell=true` 且 `argv` 为空列表时 `argv` 落到 `str(argv)` 即字符串 `"[]"` 被当 shell 命令执行，产生难排查的 127。 | 登记 |
| QR-A10 | `agent/src/kk_agent/updater.py:161-163,340,345` | 清单读取无大小上限（二进制有 64MB 帽子，清单没有）；清单缺 sha256 时跳过检查、失败原因被误标 `hmac_mismatch`，台账归因失真。 | 登记 |

### 2.4 P1 — 测试体系

| ID | 位置 | 描述 | 状态 |
|---|---|---|---|
| QR-T1 | `server/tests/`（mqtt_bridge 对应用例） | **MQTT 运行面几乎零单测**：bridge 的 start/stop/`_on_connect`/`_on_disconnect`/TLS 分支、发布异常→台账 failed（FakePublish 永不抛）、`_on_update_result` 跨主机回执、`_dispatch` loop=None 拒帧，只靠 4 条可被静默 skip 的集成用例；CI 里 skip 了也不红（`Jenkinsfile:575` 仅人工看日志）。**修复方向**：补 bridge 生命周期/失败路径单测；流水线对 skipped>0 设硬断言。 | 部分（补 3 类单测；Jenkinsfile 留待流水线验证） |
| QR-T2 | `server/tests/test_dialects.py:1-9` | **PG/MySQL 只有编译级校验**（"编译得出合法 SQL"），无任何真库执行测试，CI 无该 stage。三库支持中两个库换上当天是裸奔。**修复方向**：CI 增加一次 PG/MySQL 容器 smoke（可选 stage）。 | 登记 |
| QR-T3 | `server/tests/test_integration.py:41-44` | 无 Broker 时集成夹具每用例空转最多 ~30s（function 级 ×4 ≈ 2 分钟纯等待）才 skip；应做模块级一次性探测缓存。**修复方向**：探测结果缓存到模块级。 | 已修复 |
| QR-T4 | `server/tests/test_agent_update.py:156-168`、`test_bridge.py:441-463` | store 生命周期脆弱点：TestClient 退出（lifespan 已 `store.close()`）后继续用 `app.state.store`；`_mk_bridge` 两个用例从不 close store（泄漏 aiosqlite 连接）。 | 登记 |
| QR-T5 | 根/agent/server 三份 `pyproject.toml` | pytest 配置欠账：无 `--strict-markers`、无 `filterwarnings=error`；三份 ini 漂移（server 的 dev 组无 pytest-asyncio 但写了 `asyncio_mode="auto"`，脱离 workspace 单跑会静默失效）。**修复方向**：根配置补 `filterwarnings` 与 marker 注册；server dev 组补 pytest-asyncio。 | 已修复 |
| QR-T6 | `server/tests/conftest.py:24-54` | 死代码：`make_fake_fs`/`fake_fs` 夹具（采集器改 psutil 后无引用）；`FakePublish` 与 `api` 夹具三处逐字复制。 | 登记 |

### 2.5 P2 — 服务端（登记不展开，均带 file:line）

| ID | 位置 | 描述 |
|---|---|---|
| QR-S9 | `services/mqtt_bridge.py:199` | fire-and-forget task 未持强引用（官方建议存集合防 GC）；突发帧时并发 task 数无上界，每个都从默认 5+10 连接池抢连接。 |
| QR-S10 | `controllers/agent_update.py:94` | 64MB 二进制的 sha256 在事件循环线程计算（~100-200ms 停摆）；整包读进内存在并发上传下内存放大。应并入 `_write` 一起 `to_thread`。 |
| QR-S11 | `services/mqtt_bridge.py:79-86,180-184` | stats dict 跨线程读写无锁（paho 线程 vs 事件循环线程），read-modify-write 丢计数（对计数器良性，属未定义行为）。 |
| QR-S12 | `controllers/auth.py:19,58,68` | async 路径用 `threading.Lock`（临界区无 await 不死锁，但与 `agent_update.py:38-39` 已论证的 asyncio.Lock 标准不一致）。 |
| QR-S13 | `controllers/agent_update.py:260-327` | `upgrade_hosts` 对 hosts 数量无上限，10k 台即 20k 次顺序 await 单请求长占资源。 |
| QR-S14 | `controllers/agent_update.py:50-51` | 上传版本号只校验首字符是否数字，脏版本号直入台账并下发。 |
| QR-S15 | `controllers/health.py:9-19` | `/api/health` 未鉴权泄露版本号/proto 版本/在线数/bridge 计数（信息泄露面小但非零）。 |
| QR-S16 | `models/store.py:833-840` | 清理逻辑中 hourly 与 sessions 两条 DELETE 未分批，与 `_delete_batched`「分批防锁表」哲学不一致。 |
| QR-S17 | `models/store.py:518-526` | `append_result` SQL 内列级拼接写放大：每追加 48KB 块重写整列，O(n²) 挪到 DB 层（已声明取舍）。 |
| QR-S18 | `models/store.py:621-631,801-813` | 少量 N+1 残留（finish_updates_reaching 逐行独立事务；聚合时每 pod 两次往返），量级有界非热点。 |
| QR-S19 | `controllers/agent_update.py:79-80` | 保留 .prev 失败 `except OSError: pass` 且无日志——回滚能力静默失效。 |
| QR-S20 | `models/store.py:212,165` | MQTT 帧字段类型信任不一致：`int(msg.get("interval"))` 未捕 ValueError，炸掉整个处理 task 后静默丢帧；同函数对 ts 却有 try/except。 |
| QR-S21 | `controllers/containers.py:13` vs `tables.py:149` | `ONLINE_GRACE = 180` 双处定义，漂移隐患。 |
| QR-S22 | `controllers/commands.py:181` | 控制器越层引用 models 层私有符号 `_b64_tail`。 |
| QR-S23 | `main.py:54-65`（server） | lifespan 启动半途失败不回收资源：`store.setup()` 成功后若后续步骤抛异常，`store.close()` 不执行。 |
| QR-S24 | `controllers/*.py` 各处 | `limit/offset` 手工钳制在 4 处各写一份，无公共 Query 依赖；路径操作全部裸 dict 返回无 response_model（OpenAPI 无 schema、响应无校验）；router 无 tags。 |

### 2.6 P2 — Agent（登记不展开）

| ID | 位置 | 描述 |
|---|---|---|
| QR-A11 | `main.py:166-167` | StateBox 锁只保护单次 get/put，「读基线→差分→写回」整体不原子，跨类并发仍可丢失更新（速率窗口错乱）。 |
| QR-A12 | `executor.py:212,154` | `_Pool._q` 无界队列，批量长命令可无限积压；插件/采集与 shell 命令共用池，慢采集挤占命令吞吐。 |
| QR-A13 | `collector.py:110-113` | 磁盘 IO 差分无 `max(0, ...)` 钳制（net 侧有），计数器重置/回绕报负速率。 |
| QR-A14 | `transport.py:213-223`、`main.py:121-122` | `stop()` 连续三段 `except Exception: pass` 连日志都不记，故障不可观测。 |
| QR-A15 | `executor.py:64,76,125,136`、`transport.py:258` | 魔法数字硬编码（wait 3/5/2/10s、wait_for_publish 1.0s），与已有命名常量风格不统一。 |
| QR-A16 | agent 全目录 | 函数签名几乎零类型注解（transport/executor/main 均无），与 server 侧差距明显。 |
| QR-A17 | `main.py:236` | 冷启动首拍 update 检查（next_update=0.0）未加抖动，500 台同相打服务端。 |
| QR-A18 | `updater.py:386-388` | `spawn_apply` 全仓库零调用方，死代码。 |

### 2.7 P2 — 测试与仓库卫生

| ID | 位置 | 描述 |
|---|---|---|
| QR-T7 | `server/tests/test_bridge.py:233`、`agent/tests/test_executor.py:90` | 弱断言两处：OR 逻辑 + 子串匹配，两分支任一成立即过。 |
| QR-T8 | `server/tests/test_integration.py:93` | 模拟 Agent 以 `retain=True` 发 status，随机前缀下的 retained 帧永久留在共享开发 Broker 上，无清理步骤。 |
| QR-T9 | 仓库根目录 | 残留 `kk-server.db`/`kk-server-e2e*.db`（来自手工 E2E 脚本而非 pytest），且未进 .gitignore。 |
| QR-T10 | 覆盖盲区（汇总） | agent：transport `start/_on_message/_on_disconnect` 零单测、main 调度循环与信号注册、config 钳制解析；server：`GET /api/system/updates` 全仓无测试、上传 413/400 分支、`/api/me`、commands 错误路径、auth `_reap` TTL。 |
| QR-T11 | 平台覆盖 | executor `_kill_tree` POSIX/Windows 双路与 updater chmod posix 分支各覆盖一半，Windows 独跑永远测不到 killpg（依赖 CI Linux 补齐，需知晓）。 |

---

## 3. 正面发现（保持项）

- **分层与方言纪律**：models 层是全仓唯一接触方言之处，控制器零泄漏；分批删全按主键 IN、批量 IN 分片 400/批；CASE 回避 GREATEST 等细节成熟。
- **async 纪律**：该 `to_thread` 的都 `to_thread`（上传落盘、镜像导出）；paho 线程→事件循环走 `call_soon_threadsafe`；janitor task 持引用且 cancel 后 await 回收；未发现真正阻塞事件循环的调用。
- **测试文化**：回归锁 docstring 指向真实故障；FakePsutil 未预期调用即抛 AssertionError；时间回拨用 SQL 而非 sleep；所有轮询有界；loguru/updater/auth 全局状态清理纪律完整；`asyncio_mode="auto"` 用法正确。
- **安全亮点**：`secrets.compare_digest` 恒时比较、PBKDF2 310k 轮带旧迭代数原地升级、CSV 注入防护、双维度登录限流、默认口令仅非 production 放行、主机名正则防 MQTT 通配注入。
- **QoS 分级语义正确**：心跳 QoS0 断线即弃、命令/结果 QoS1 离线入队由持久会话接管——本项目最核心的正确决策；`announce_update` 等 PUBACK、验签先于落盘、读取侧输出封顶等关键点都在位。

---

## 4. 修复记录

本轮（2026-09-29）按优先级落地：**QR-P0-1、QR-P0-2、QR-S1、QR-S2、QR-S3、QR-S4、QR-S7、QR-S8、QR-A1、QR-A2、QR-A3、QR-A4、QR-A5（注释澄清）、QR-T3、QR-T5、QR-T1（部分：补 bridge 失败路径单测）**。其余登记为后续账本，修复提交信息中标注对应 QR 编号。

修复后的回归基线见各修复提交的测试结果；`QR-T2`（PG/MySQL 真库 smoke）与 `QR-T1` 的 Jenkinsfile 硬断言需要流水线环境验证，留待 CI 侧落地。
