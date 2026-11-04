# KontainKeeper 全量质量评估报告与分阶段优化方案(2026-11-03)

评估范围:`agent/src/kk_agent`(2355 行)、`server/src/kk_server`(4971 行)、`web/src` 业务代码(约 1.08 万行)。
方法:四路并行深审(后端/Agent/前端/测试覆盖)+ 关键发现逐条人工交叉验证(所有 P1/P2 均已核实到行)。
基线(改动前实测):后端 pytest **491 条,0 失败/0 错误/恰好 5 条 Broker 集成 skipped**;前端 typecheck 通过,**vitest 177 条全过**。

优先级口径:P0=安全漏洞或数据损坏;P1=明确 bug 或生产事故风险;P2=质量债务(可靠性/安全加固/性能);P3=风格与低风险清理。

---

## 一、评估报告

### 1. 代码结构与分层

**总体:高于平均水准,纪律性强。** 服务端 MVC 边界干净:controllers 零 SQL、全部经 store 门面;三库方言差异确实收口在 `store.py` 的 `_upsert/_ensure_schema/_pg_tune/_delete_batched` 汇聚点;Agent 端 config/transport/collector/executor/updater 职责清晰,几乎每个非常量旁都有带缺陷编号(QR-xx)的决策注释;前端页面只剩模板,数据流收进 `utils/hook.tsx`,轮询统一走 `kkPoll`(逐一 grep 验证无绕过者)。

债务(均为 P3):
- `mqtt_bridge.py` 单文件 602 行承担帧路由/白名单/升级编排/sweep 四类职责,`_on_status` 混入升级联动,规模再涨应拆。
- `commands.py:137-138` controller 手工拼「伪 row dict」喂 `bridge.dispatch_command`,字段契约靠约定不靠类型。
- `web/src/api/routes.ts:39` 菜单树装配全程 `any`;`HostPicker.vue:50` 表格实例裸 `any`(仓内已有 `UpdateTableApi` 精确类型可参照)。
- 死代码:`_store_hosts.upsert_container`、`_store_commands.mark_sent`(单条版)、`is_online`、`default_admin_exists` 生产无调用点,仅测试引用,与契约注释矛盾。

### 2. 命名与可读性

无超 80 行函数、无超 4 层嵌套(双端核过);魔法数治理是强项。择要:
- [P3] `_store_rbac.py:235` `count_enabled_admins` 返回 0/1 而非计数,语义名不副实(调用方恰用 `<=1` 才没出 bug)。
- [P3] `helpers.py:27` 单行三元 + 续行,运算符优先级易误读。
- [P3] `config.py:115` 与 `:20` 两套布尔解析约定并存(默认真 vs 默认假)。
- [P3] `plugin_loader.py:19` 缓存条目裸元组 + 下标访问。

### 3. 重复与冗余

- [P3] 负 offset 防御三写:`containers.py:126`、`commands.py:170`、`audit.py:24`。
- [P3] `_download_url` 两份:`mqtt_bridge.py:479` 与 `agent_update.py:178`。
- [P3] 「json.loads + 失败回落」四份同构(containers/_store_maintenance/commands/exporting),回落语义各不相同,收敛需谨慎。
- [P3] 前端:主机在线状态映射两份(monitor/update hook)、`DEFAULT_ITEMS` 两份、平表→树三份(routes/role 页/menu 页,排序细节不同)。
- [P3] Agent:`collector.py:27` 与 `hostinfo.py:25` 的 `_safe` 逐字重复;`updater.py:445` 同一二进制哈希两遍(可辩护但未注明刻意)。

### 4. 错误与异常处理

整体好:`_on_message` 有兜底壳并计数、janitor 区分 CancelledError、PBKDF2/大文件 IO 挪 `to_thread`、Agent 线程异常均有日志出口、tmp 半成品三处收口。择要:
- **[P1] `plugin_loader.py:141` + `main.py:213-215`** — 插件返回不可 JSON 序列化对象(如 `set()`/`datetime`)时,`json.dumps` 在 `transport.publish_hb` 内才抛,`work()` 的 `except Exception` 整体吞掉:**一个坏插件即让该主机心跳永久停摆**,且 quarantine 机制(只隔离 collect 抛异常/卡死)够不着序列化边界,与模块自述「单插件失败绝不影响主循环」直接矛盾。
- [P3] `mqtt_bridge.py:249` 任务异常只记 repr 不带栈,落库失败排障丢 traceback。
- [P3] `mqtt_bridge.py:134-136` `stop()` 先 `loop_stop()` 再 `disconnect()`,DISCONNECT 包永远发不出去。
- [P3] `collector.py:276` 采集项异常无日志静默 `continue`,项持续失败时运维只见面板空白。
- [P3] `agent/main.py:197` 非 str `kind` 使回执路径崩溃,命令挂到超时清扫(无 result 帧)。

### 5. 边界与空值

时间戳钳制、`_tri` 三态布尔、LIKE autoescape、`fillPct` 挡 NaN、QR-W16「没读到≠0」贯穿七页——边界处理是本仓强项。实质问题:
- **[P2] `_store_hosts.py:230`** `record_hb` 的 `int(msg.get("interval") or 60)` 未设防:坏值 ValueError 使**整帧心跳丢失**;且无上界,Agent 自报 `interval=2e9` 后僵尸主机永判在线(宽限 `max(3*interval,180)`)。同函数 ts/pcts 都有 try,唯此漏网。
- **[P2] `_store_hosts.py:203-206`** `set_online` 未对 `status_reason/agent_ver/image` 走 `_clip`(同函数 os_name/kernel 等都裁了):MySQL 严格模式超长 reason 报 1406,**该主机 status 整帧落库失败→永远显示离线**;PG/SQLite 静默存超长——三库行为分叉,正是仓库明令禁止的形态。
- [P3] `commands.py:155`/`_store_commands.py:136` 负 limit 产生错误切片(`min(limit,100)` 无下限)。
- [P3] `commands.py:104-128` 批量命令 pods 不去重不 strip(升级路径 `agent_update.py:294` 已做),重复 pod 建重复命令。
- [P3] 前端 trend:`171-172` **30s 静默轮询无条件回填 `memInput/diskInput`,冲掉用户编辑中的告警上限**;且缺 `watch(pod)`,路由参数变化不重取数据。
- [P3] Agent:`collector.py:123/139` 速率差分用 wall clock,时钟回拨单周期速率归零(有钳负,危害有限);`config.py:56` 之外的首次更新检查无抖动。

### 6. 接口契约一致性

列表分页统一(items/total/offset/limit);响应无 Pydantic `response_model` 是已知取舍(出口收口函数)。择要:
- [P3] 写操作 `ok` 字段有无凭各页发挥(users_create 有、create_commands 无)。
- **[P2] `auth.py:137-138,162`** 用户行缺失(并发删除窗口)时 roles fail-open 默认 `"admin"`;实际权限判定走 `require_admin` 实时查库故无提权,但契约应 fail-closed。
- [P3] 前端 `rbac.ts:56` username 直接拼 URL path,特殊字符无编码。

### 7. 安全隐患

- **[P1] `controllers/agent_update.py:33`** — `router = APIRouter(prefix="/api/system", dependencies=[Depends(current_user)])`:上传新二进制(POST /agent)、回滚(POST /agent/rollback)、**全网批量升级**(POST /agent/upgrade)只需任意登录会话。operator 角色即可对全网 500 台分发执行任意代码——这是事实上的 RCE 入口。模块 docstring 自述「上传需管理员会话」,实现与文档矛盾;`deps.py:41` 现成的 `AdminUser` 未用。RBAC「权限只控菜单」的范围裁决覆盖不了二进制分发这类特权操作。
- **[P1] `services/security.py:101-108`** — `_check_tokens` 跳过 wrapper 前缀后**默认下一 token 即程序名**,不含 wrapper 自身参数:`env FOO=1 rm -r -f /usr`(prog=`foo=1`)、`sudo -u root dd if=...`(prog=`-u`)、`timeout 10 rm -rf /`(prog=`10`)全部放行,子串兜底层也不命中。黑名单是 AGENTS.md 明文红线「不能绕过」,此为现实可用的绕过。
- [P2] `_store_rbac.py:213-229` 管理员**重置密码不吊销既有会话**(仅禁用才吊销):密码泄露后重置,攻击者 12h token 仍有效。
- [P2] `mqtt_bridge.py:118-121` mqtts:// 未配 CA 时静默 `CERT_NONE`(加密但无认证),零日志。
- [P2] `mqtt_bridge.py:414-416` `update_result_mismatch` 审计未走 `_audit_throttled`:该路径由 Broker 侧匿名发布者可控(重放 up- 帧),持续灌库;同类 ip_rejected/host_mismatch 均已限速,唯此漏网。
- [P2] `mqtt_bridge.py:251-263` 节流按 key 计,key(host/ip)可被攻击者无限轮换,聚合写入率不受约束;ip 串进 key/日志无长度裁剪。
- [P3] `/api/health` 无鉴权暴露全量统计与在线台数;登录限流是进程内字典(多 worker 失效)。
- [P3] Agent 侧:`executor.py:54` 子进程全量继承环境变量(`KK_MQTT_PASSWORD` 进子进程 environ);`transport.py:358` 入站帧无大小上限(Broker 侧 `message_size_limit` 兜)。
- [P3] 前端:`auth.ts:53` token 存非 httpOnly Cookie(pure-admin 底座继承项,无 HTML 注入 sink 故残余风险低);`command/shell/index.vue:62` `...route.query` 展开使 `?cmdline=` 永久残留,与「命令体不落 URL」自述矛盾。
- **正面**:SQL 注入面干净(排序白名单/参数化/LIKE autoescape)、CSV 注入有 `_cell`、自更新防御纵深(形态校验→size→sha→HMAC 常量时间→原子替换→execv 回滚)堪称范本、命令输出 `<pre>{{ }}</pre>` 文本插值无 XSS、弹窗刻意避 `dangerouslyUseHTMLString`。

### 8. 性能瓶颈

热路径工程到位(summary 视图、out_tail 镜像列、mark_sent_batch、shards 分片、PG 部分索引/BRIN/autovacuum 调优、`Intl` 一次建复用)。遗留:
- [P2] `_store_commands.py:121-128` `batch_summary` 每次请求**全表 GROUP BY**(无 WHERE),命令表自述「500 台 × 每日数千条、百万行级」,而 `tables.py:146` 注释「上万级再补索引」已脱节——命令中心页每轮轮询都全扫。**本轮不改 schema**(索引补齐涉及存量库迁移,单独决策),修复相邻负 limit 边界,此账单列。
- [P3] `_store_metrics.py:61-89` 聚合追账逐小时×逐主机单行 upsert 单事务,停机 2 天 ≈ 2.4 万次串行事务。
- [P3] `agent_update.py:314-333` 批量升级逐台串行 dispatch,500 台 = 500 次往返。
- [P3] `mqtt_bridge.py:510` uid 用秒级时间戳,同秒同主机并发升级主键冲突(一次性 500/任务异常)。
- [P3] `mqtt_bridge.py:592` `_result_locks` 有 sweep 回收(防线在),但该分支零测试。
- [P3] 前端 `host/update` 每 10s 轮询全量 `clearSelection`+500 次 `toggleRowSelection` 回填(纯浪费,行为无错)。

### 9. 测试覆盖现状

后端 491 条(agent 198 + server 293)、前端 177 条(17 个 spec 文件),全绿。质量抽查结论优:
- **无**依赖真实 Broker/外网/真实时钟/执行顺序的用例(test_updater 的"真实 HTTP"是 loopback 自服务,test_bridge 用 FakeClient+tmp SQLite);
- 日志断言均断言消息体、不依赖格式前缀(符合 A7 红线);kkPoll.spec 的 fake timers 写法可作范本。

缺口(Top 10,均为「核心分支只在需 Broker 的集成用例里被触达」或纯函数零覆盖):
1. `controllers/commands.py:43-125` POST /api/commands 全部校验分支(400/404/503)与 **API 边界黑名单拒绝+command_blocked 审计**只有需 Broker 的集成用例守着——无 Broker 日常开发时安全红线的 API 层裸奔。★
2. `web/src/api/routes.ts:27-53` `buildMenuTree`(fail-closed 契约)零覆盖。★
3. `_store_rbac.py:128-141` `allowed_menus` 空授权=有意清空的 fail-closed 契约 + `:90-95` seed 回填分支。★
4. `useMonitor` 的 queryParams 布尔转换(`?online=false` 筛反雷)与 collectTargets 快照(修过的真 bug 无回归锁)。
5. MySQL 运行时路径(`_key_batches`/`INSERT IGNORE`)只有 compile 断言(需 MySQL CI,本轮以既有 compile 锁保底)。
6. `_store_commands.append_result` 的 seq=None/坏 seq 幂等水位分支。★
7. bridge sweep 驱逐 `_result_locks` + update_result_mismatch 审计分支。
8. `exporting.py:55` `_limit` 边界参数化(0/负/20001)。
9. Agent:`collector.cpu_metrics` getloadavg None 分支(Windows 真机每帧都走、测试永远走不到)★;`executor.submit` timeout 非数字回落。
10. `web/src/api/rbac.ts` 全量(唯一无 spec 的 api 层文件)。

(★=协议字段生产者/安全红线级,按仓库规范必须补单测)

### 10. 总体结论

这是一套质量显著高于平均水准的代码:分层纪律、缺陷编号注释、边界口径(QR-W16/W5)、防御纵深(自更新链路)都体现了真实踩坑后的固化。两个 P1 都集中在**组合边界与鉴权裁决**而非模块内部:特权操作的鉴权收口、黑名单结构层的 wrapper 参数盲区;P2 集中在「帧级容错缺口导致整帧丢失」(interval/status_reason/插件序列化三处同型)与前端跨页口径漂移。修复均是小切口、可独立验证的。

**质量最高**:Agent `updater.py`/`transport.py`、服务端 `_store_hosts.py`/`_store_maintenance.py`、前端 `utils/kkPoll.ts`/`api/containers.ts`。
**最需改进**:`controllers/agent_update.py`(特权+性能+契约问题密度最高)、`services/mqtt_bridge.py`(五处问题全在攻击者/故障可达路径)、`models/_store_commands.py`(生产规模头号慢查询候选)、`views/host/trend/utils/hook.ts`(编辑态被轮询冲掉)。

---

## 二、分阶段优化方案

原则:每阶段独立提交、独立可验证、可用 `git revert <该阶段提交>` 整体回滚;每阶段附带的回归测试随修复同批提交(仓库规范:语义修复必须随附锁定新语义的回归测试)。每阶段完成后跑对应侧门禁,最后全量 `bash scripts/quality_gate.sh`。

### 阶段 1:服务端安全收口(修 2×P1)
- **文件**:`server/src/kk_server/controllers/agent_update.py`、`server/src/kk_server/services/security.py`、`server/tests/test_agent_update.py`、`server/tests/test_security.py`
- **改动**:
  1. 上传/回滚/批量升级三个写端点改挂 `AdminUser`(deps.py 现成依赖);`GET /agent/current` 保持登录会话可读(operator 的「版本与更新」菜单页要展示当前版本与落后数,只读无风险)。同步修正 docstring 与实际一致。
  2. `_check_tokens` 的 wrapper 前缀跳过加固:跳过 `-` 开头选项、`KEY=VALUE` 赋值、纯数字/时长 token,以及已知取值旗标(`-u/-g/-n/-p/--user` 等)后的值 token——`env FOO=1 rm ...`、`sudo -u root dd ...`、`timeout 10 rm -rf /` 全部落网,良性 wrapper 命令(`sudo -u postgres pg_dump`、`nice -n 10 make`、`env A=1 python app.py`)不受影响。
- **行为变化**:operator 调用三个写端点从 200 变 403(预期安全修复);黑名单对包装参数形态从漏判变拦截。
- **验收**:新增回归用例(operator 403/admin 200;3 个绕过载荷被拦、4 个良性载荷放行)全过;既有 491 条不红(skipped 仍为 5)。
- **回滚边界**:revert 本阶段提交即恢复原鉴权与原黑名单语义。

### 阶段 2:Agent 端可靠性(修 P1 心跳停摆 + P3 鲁棒性)
- **文件**:`agent/src/kk_agent/plugin_loader.py`、`agent/src/kk_agent/transport.py`、`agent/src/kk_agent/main.py`;`agent/tests/test_plugins.py`、`agent/tests/test_transport.py`、`agent/tests/test_main.py`
- **改动**:
  1. `collect_all` 对每个插件输出做 `json.dumps` 试序列化(模块 docstring 本就约定「输出必须是 JSON 可序列化对象」),失败按 collect 失败同款处理:记 warning、丢弃该插件输出、继续其它插件——隔离承诺在序列化边界补齐。
  2. `parse_broker` 坏端口抛 `TransportError`(与缺主机名/内嵌凭据同通道),不再裸 ValueError。
  3. `dispatch` 对非 str `kind` 走 `str()` 归一,保证未知 kind 恒有 127 回执帧。
- **行为变化**:坏插件从「整帧心跳永久丢失」变「丢该插件输出+告警日志」;坏端口配置从裸 traceback 变干净报错。
- **验收**:新增用例(set() 插件不杀心跳、其余插件照常上报;坏端口 TransportError;非 str kind 有回执)全过,agent 侧全量绿。
- **回滚边界**:revert 本阶段提交。

### 阶段 3:服务端数据健壮性与桥接加固(修 6×P2)
- **文件**:`server/src/kk_server/models/_store_hosts.py`、`_store_rbac.py`、`server/src/kk_server/services/mqtt_bridge.py`、`server/src/kk_server/controllers/auth.py`、`commands.py`、`_store_commands.py`;对应测试文件
- **改动**:
  1. `record_hb` 的 interval:try/except 回落 60 + `clamp(1,3600)`(坏值丢整帧→回落;自报 2e9 的僵尸在线击穿→钳制)。
  2. `set_online` 对 `status_reason/agent_ver/image` 补 `_clip`(列宽对齐 tables.py),消除 MySQL 1406 整帧失败与三库分叉。
  3. `_store_rbac.update_user`:重置密码与禁用同款吊销该用户全部会话。
  4. `auth.py` 两处 roles fail-open `"admin"` → fail-closed(空串/空表)。
  5. `mqtt_bridge`:`update_result_mismatch` 审计走 `_audit_throttled`;mqtts 无 CA 打 warning;任务异常日志带栈;`stop()` 先 disconnect 后 loop_stop;升级 uid 加 `secrets.token_hex` 防同秒主键冲突。
  6. 批量命令 pods 去重 strip(对齐升级路径);`batch_summary` 的 limit `clamp(1,100)`。
- **行为变化**:均为缺陷修复;`stop()` 顺序、uid 格式若有测试断言旧值,按新语义同步改锁并在 docstring 注明依据。
- **验收**:每项修复随附回归用例;server 侧全量绿(skipped=5)。
- **回滚边界**:revert 本阶段提交(单提交或按 3a-3f 拆分提交,均可独立 revert)。

### 阶段 4:前端一致性(修 3×P2)
- **文件**:`web/src/views/host/monitor/utils/hook.tsx`、`host/detail/index.vue`、`host/update/utils/hook.tsx`、`host/trend/utils/hook.ts`、`command/components/utils/hook.tsx`;对应 spec
- **改动**:
  1. monitor/detail/update/trend 四页 `pollFailed.value = silent` → 失败恒 `true`(对齐 system/welcome 既定口径:手动刷新失败同样变冷——数据确实过期了),注释标注 QR 编号。
  2. trend:静默轮询不再回填 `memInput/diskInput`(只在非静默加载同步);补 `watch(pod)` 切主机重取。
  3. `downloadOut` 加在途守卫,连点不重复下载。
- **行为变化**:手动刷新失败后页头显示「已过期」而不是绿色「已同步」(预期修复);trend 输入框不再被 30s 轮询打回;切主机立即重取数据。
- **验收**:`pnpm typecheck` + `pnpm test` 绿;trend/monitor hook spec 新增用例锁新口径。
- **回滚边界**:revert 本阶段提交。

### 阶段 5:冗余收敛与死代码清理(纯等价重构)
- **文件**:`server` 各死导入文件、`models/helpers.py`、三个 controller;`web/src/api/commands.ts`、monitor/update hook、`utils/kk.ts`
- **改动**:删 5 处死导入(逐一 grep 确认无引用);`offset_or_none` 助手收口三处负 offset 防御;`AGENT_DOWNLOAD_PATH` 常量收口 `_download_url` 两份;前端 `DEFAULT_COLLECT_ITEMS` 与主机状态映射收口到单一来源(逐字段等价才搬,菜单树三份因排序细节不同**不动**,列账)。
- **验收**:双端全量测试绿,无行为变化;死代码不再被测试引用的(如 `mark_sent`)保留测试原样。
- **回滚边界**:revert 本阶段提交。

### 阶段 6:测试补全(覆盖缺口 Top 10 中未随修复覆盖的部分)
- **文件**:`server/tests/test_api.py`、`test_rbac.py`、`test_store.py`、`test_bridge.py`、`test_export.py`;`web/src/api/routes.spec.ts`、`api/rbac.spec.ts`、`views/host/monitor/utils/hook.spec.ts`
- **新增用例**:POST /api/commands 校验分支 9 例(含 API 边界黑名单+审计)★;`allowed_menus` 空授权 fail-closed + seed 回填 2 例★;`append_result` seq 异常 2 例★;bridge sweep 驱逐与 argv 损坏回落 2 例;`exporting._limit` 边界参数化;`collector.cpu_metrics` getloadavg None 分支★;`buildMenuTree` 纯函数 5 例★;`rbac.ts` URL 契约 4 例;`useMonitor` queryParams/collectTargets 3 例。
- **验收**:双端全量绿,后端 skipped 恰好 5;新用例无相互依赖、不依赖 Broker/网络/时钟(ASGI Transport / FakePsutil / mock http)。
- **回滚边界**:纯增测试,revert 即移除,无运行时影响。

### 明确不做(列账,需单独决策)
- `batch_summary` 全表聚合的索引/schema 变更(涉及存量库迁移);
- MySQL 运行时测试矩阵(需 MySQL CI 节点);
- `mqtt_bridge.py` 升级编排拆模块;菜单树三份收敛(排序细节不同,强行统一会改 UI 行为);
- collector 差分换单调钟(现有测试以 wall-clock 构造状态,切换有回归面,收益低);
- token Cookie 属性(pure-admin 底座继承项)。
