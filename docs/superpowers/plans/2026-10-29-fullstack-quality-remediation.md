# KontainKeeper 全栈质量优化 · 第二轮承接计划（2026-10-30）

> 依据：`docs/quality-review-2026-10-29.md`（46 条 `QR29-*`）与 `docs/remediation-plan-2026-10-29.md`（第一轮 6 阶段，**已于 10-29 07:11 全部实施完毕并登记**）。
> 本文只规划**第一轮之后仍存在的缺口**，不重复登记已完成条目。
> 当前真实基线（10-30 02:08 实测）：后端 `pytest agent/tests server/tests` → **444 passed / 5 skipped / 0 failed（61.1s，无 Broker）**；前端 `pnpm test` → **25 passed（2 个 spec：`utils/kk.spec.ts`、`utils/kkPoll.spec.ts`）**；`pnpm typecheck` 绿。
> 已逐条复核确认为「已修」的条目（不再动）：S1 `asyncio.to_thread`、S2/S3 `_bump` + `_stats_lock` + `bridge_errors`、S5 `set_online` 的 ±窗口钳制、S9 `raw_days*86400`、S16 死代码清零、W1 `logoutApi`、A1 `agent/tests/test_hostinfo.py`、`server/tests/test_qr29_remediation.py`（S2/S3/S7/S8 等）。

## 本轮的三件真事

1. **前端测试仍然只有 25 条**，覆盖 2 个 utils 文件；`api/`、`utils/http`、`utils/auth`、`store/`、`views/*/utils/hook.tsx` 全部零覆盖——用户要求的「补全前端全部单元测试」主体尚未落地。
2. **门禁依然只是文档**：`git config core.hooksPath` 实测未设，`.git/hooks` 无生效钩子，`web/.husky/pre-commit`（lint-staged）与 commit-msg（commitlint）是死文件。AGENTS.md 写着「必须跑前后端单测才允许提交」，但没有任何机制保证，且 `pnpm test` 之外无覆盖率入口。
3. **第一轮明确「本轮不做」的结构性条目**：契约收敛（评估 §5 九处字段偏差 + 后端零 `response_model`）、重复与冗余（§6.2 八组 + S17）、`store.py` 1142 行拆分、Agent 更新流式下载（A4）。

## 需人工裁决，本轮不做（保持现状并已被测试锁住）

| 项 | 现状 | 为什么不能自动决 |
|---|---|---|
| 安全覆盖面 | `curl \| sh`/`systemctl`/`truncate`/`find -delete`/`kill 1` 放行，且已由 `test_security` 矩阵固化 | 内网运维灵活 vs 红线扩张，是产品口径 |
| 页面隐藏时轮询 | 全仓无 `visibilitychange` | 取决于用户是否常驻总览页 |
| v4 字段呈现（`caps`/`labels`/`custom`/`docker_*`） | 上报 + API 回吐，UI 不消费；服务端 `caps` 门禁未写 | 要不要做 UI 是产品决策 |
| A5「升上去就砖」启动健康门禁 | 无自愈回滚 | 新增机制，非缺陷修复 |

---

## 阶段 A · 让门禁从文档变成可执行

**目标**：AGENTS.md 那条规范不再依赖自觉；一次提交能在数十秒内拿到「前后端都绿」的机械证据。

**涉及**：`web/.husky/pre-commit`、`git config core.hooksPath`、`web/package.json`（`test:coverage`）、`scripts/`（新增快测入口脚本）、`AGENTS.md`（补「快测口径」段）。

**步骤**
- [ ] A.1 `git config core.hooksPath web/.husky`，用一次故意的空提交/干跑验证钩子真的执行
- [ ] A.2 `pre-commit` 改为：后端 `-q -x` 快跑（排除 Broker 级）+ 前端 `typecheck && test`；全量仍交 Jenkins
- [ ] A.3 `pnpm test:coverage` 接 `@vitest/coverage-v8`（本轮已装），产出 `web/coverage/` 并 gitignore
- [ ] A.4 验收：`pnpm test` 25 条仍绿；钩子在人为制造红灯时确实拦住提交；`git config core.hooksPath` 有值
- [ ] A.5 回滚边界：`git config --unset core.hooksPath` + 还原 `pre-commit` 一个文件，零生产码影响

**风险**：钩子挡住并发会话的提交（本仓有多个写入者）——故快测必须是**秒级**且失败信息明确；若过慢则退回「只跑前端 + 后端 smoke」。

## 阶段 B · 前端测试补全（本轮最大面）

**目标**：核心业务逻辑、接口输入输出、异常分支、边界条件全覆盖；不测像素。

**涉及**：新 `web/src/api/__tests__/`、`web/src/utils/__tests__/`（http/auth/message）、`web/src/store/__tests__/`、`web/src/views/audit/utils/hook.spec.ts`；被测对象 `web/src/api/*.ts`(6)、`utils/http/*`、`utils/auth.ts`、`store/modules/{user,permission}.ts`、`views/audit/utils/hook.tsx`。

**步骤**
- [ ] B.1 `utils/http`：token 注入、401 去重（W7 的新语义）、403/5xx、网络错误、`errText` 路径、请求方法/参数拼装（mock axios）
- [ ] B.2 `api/*`：每端点 URL/方法/入参与返回适配（mock `http.request`），含 `getLogin` 的 pure-admin 形状适配与 `logoutApi` 自吞错误
- [ ] B.3 `utils/auth.ts`：cookie/localStorage 读写、`getToken` 过期语义、`resetRouter` 交互面
- [ ] B.4 `store/modules/user.ts`：登录成功/失败、登出（本地清理与 fire-and-forget 吊销的次序不变量）、`permission.ts` 静态菜单
- [ ] B.5 `views/audit/utils/hook.tsx`：分页单一状态源、`read` 三态、搜索/重置/导出、迟到包丢弃（jsdom + fake timers）
- [ ] B.6 稳定与隔离验收：`pnpm test` 全绿 + 随机顺序复跑仍绿（`--sequence.shuffle`）+ 连续两遍计数一致
- [ ] B.7 回滚边界：纯新增 spec 与 `vitest.config.ts` 的 include 扩到 `*.spec.tsx`；任一 spec 暴露生产缺陷 → 单独编号提交修复，不混在测试里

## 阶段 C · 后端剩余测试缺口

**目标**：把第一轮没登记的分支补上，并把「只 compile 不执行」的方言测试升级成真执行。

**涉及**：`server/tests/test_agent_update.py`、`test_api.py`（或新 `test_auth_me.py`）、`test_dialects.py`、`test_store.py`。

**步骤**
- [ ] C.1 `upload_agent` 的 413 与 version 校验分支（`agent_update.py:54,65`）
- [ ] C.2 `/api/me` 与 `/api/logout` 的会话吊销语义（配合 W1 的跨端闭环）
- [ ] C.3 `test_dialects` 在 SQLite 上真执行（含超长 detail 截断、负 OFFSET、`INSERT IGNORE` 语义），PG/MySQL 保持 compile 级
- [ ] C.4 S9 的长宕机追赶窗口若无锁则补一条（先确认 `test_qr29_remediation` 是否已覆盖）
- [ ] C.5 验收：全量绿，用例数从 444 上升；每条新用例自带 tmp_path/monkeypatch 隔离
- [ ] C.6 回滚边界：纯新增测试；暴露缺陷则按 QR29 编号单独提交

## 阶段 D · 跨端契约收敛与漂移防护

**目标**：评估 §5 的九处字段偏差不只修一次，而是装上机制；这是「接口契约一致性」维度的根因修复。

**涉及**：`server/src/kk_server/controllers/{containers,audit,agent_update,auth}.py`（新增 Pydantic 响应模型）、新 `server/tests/test_contract.py`、`web/src/api/*.ts`。

**步骤**
- [ ] D.1 4 个热点端点加 `response_model`（先确认过滤掉的字段前端确实不读）
- [ ] D.2 契约快照测试：`/openapi.json` 的字段集与前端 `api/*.ts` 声明清单对账，§5 的表钉成用例
- [ ] D.3 `hourly.last_metrics` 零消费确认后砍写入（行为变更，独立 commit）
- [ ] D.4 `commands.argv` 形状双端统一
- [ ] D.5 验收：契约测试绿；`pnpm typecheck` 绿；全量后端绿
- [ ] D.6 回滚边界：逐端点独立 commit（响应模型会剔除额外字段，风险最高的是「前端隐式依赖未声明字段」）

## 阶段 E · 重复与冗余收敛（零行为变更）

**步骤**
- [ ] E.1 服务端 S17：`180` 三处、`min(max(limit,…))` 三处、400 分片四处、同构分批删两处 → 收进 `models/helpers.py`
- [ ] E.2 前端常量单点：`argvText()`、`nowSec()`、间隔档位、磁盘阈值口径（W14/W16 的常量面，不动 views 结构）
- [ ] E.3 Agent A7/A8/A10：差分公式与结果帧构造抽 helper、超时魔法数命名（含 A11 死代码：`_in_backoff`、`len(buf)>max_out`、`topic_prefix` 校验）
- [ ] E.4 验收：三套测试全绿 + 逐条 diff 自检「无行为变更」；每条独立 commit 便于单条 revert

## 阶段 F · Agent 更新流式下载（A4）

**目标**：`download_binary` 不再整包驻留内存，压住更新期 RSS 峰值（AGENTS.md 的 25–35MB 口径）。

**涉及**：`agent/src/kk_agent/updater.py`、`agent/tests/test_updater.py`。

**步骤**：流式写 tmp + 边写边算 sha256，封顶语义与 `verify_and_replace` 校验链一字不变；验收=既有 `test_updater` 全绿 + 新增「峰值内存不随包体线性」断言；回滚=单 commit revert。

## 阶段 G · `store.py` 结构性拆分（1142 行 → 内聚模块）

**目标**：按 auth/audit/commands/metrics/hosts/maintenance 六域搬动，`Store` 保持门面，**调用点零改动**；方言收敛点（`_upsert`/`_ensure_schema`/`_column_ddl`）留在单一基座，不散回各模块（AGENTS.md 硬约束）。

**步骤**：G.1 建包与门面骨架，纯 re-export → 全量绿（证明搬动无损）；G.2 按 auth→audit→commands→metrics→hosts→maintenance 逐域搬，每搬一域跑一次全量；G.3 验收 = 全量绿 **且** `git diff --stat` 显示 `controllers/`、`services/` 零改动；G.4 回滚 = 整段 revert 即回到单体。混进任何行为变更即视为违规，拆回阶段 C。

---

## 顺序与验证节奏

```
A（门禁，秒级）→ B（前端测试）→ C（后端测试）→ D（契约）→ E（重复收敛）→ F（A4）→ G（拆分）
```

每阶段收口必须给出：改动文件清单、测试计数变化（444 → ?，25 → ?）、是否属语义修正。
每阶段独立可验证、独立可 revert，阶段之间只有顺序依赖，无半成品耦合。
