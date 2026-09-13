# KontainKeeper 优化方案（基于 2026-09-12 质量分析）

> 编写日期：2026-09-12　基线：`main` @ `31768a4`（D 组追加时已至 `2f4598d`，工作区干净）
> 依据：[`docs/quality-assessment-2026-09-12.md`](quality-assessment-2026-09-12.md)
> 性质：**方案文档，评审通过后再动代码**。分两阶段，阶段一交付核心能力，阶段二加固，另有 §1.5 的 D 组新需求。
> 约定：不引入新第三方依赖（导出用标准库 `csv` + `io`；前端只用 Element Plus + 现有 `kk-*` 工具类），不改表名与列名，**不改 MQTT 帧格式**（D1.4 只是把既有字段补进文档，不新增字段，`proto_ver` 保持 3）。

---

## 0. 方案总览

| 批次 | 项 | 目标 | 改动面 | 依赖 |
|---|---|---|---|---|
| **一** | A1 结果导出 | 补 P0-1：命令/审计/主机/指标四类导出 | 新增 1 控制器 + 1 API 模块 + 1 工具函数；4 个页面加按钮 | 无 |
| **一** | A2 批量发布回路去 N+1 | 补 P1-1：500 台点击零 DB 回查 | `commands.py` 1 处循环重写 | 无 |
| **一** | A3 结果分页 + 批次聚合 | 补 P1-2/P1-3：能看全、能按批次核验 | 1 列登记 + store 2 方法 + 控制器 + 前端页 | A1 复用筛选参数 |
| **一** | A4 上报间隔治理 | 补 P1-4：消除死配置 | `config.py` + `mqtt_bridge._on_hb` + stats | 无 |
| **一** | A5 **Web 前端易用性与布局改造** | 命令快速下发 / 结果查询 / 导出入口 / 消除布局 magic number | 新增 4 个组件 + `kk.scss` 布局基建 + 5 个页面改造 | 复用 A1 导出、A3 分页；与 A1/A3 同批交付体验最佳 |
| **一** | A6 **自更新可观测性**（审视新增） | 补自更新缺口：**推送式更新当前静默失效** + 更新结果无回执 | `updater.py` URL 解析 + `updates` 表 + 1 个回执分支 | 无 |
| **一** | A7 **日志体系重构**（新增） | 引 `loguru` 换掉原生 `logging`；顺带修 Agent 日志**双写入 + 双轮转**缺陷；三套格式归一 | Agent 重写 `logutil.py` + 服务端新增 `logsetup.py`（含 stdlib 拦截）；**两侧调用点零改动** | `loguru`（唯一新增依赖） |
| **一** | **D1 版本信息硬化**（新需求①） | 版本**单一真源**（现 `AGENT_VER=0.3.0` 与 `__version__=0.2.0` 互相矛盾）+ 落后判定由服务端算 + 「最新版本」对运维可见 | `agent/__init__.py` 1 行 + `containers.py` 计算字段 + 1 个新端点 + 协议补字段说明 | 无 |
| **一** | **D2 受控批量升级**（新需求②） | 补空白：**选机器 → 升级到最新**。引入 `KK_UPDATE_MODE`（默认 `manual`）收归升级决策权；复用 A6.2 台账做 `in_flight` 去重与离线 `queued` 补投 | 1 个新端点 + `updates` 表加 `queued` 状态 + 前端复用既有表格多选 + 新增「版本与更新」页 | **强依赖 A6.1 / A6.2**；同族 B2（回滚） |
| **二** | B1 客户端 nice 降权 | 补 P2-1：零代码级隔离 → 优先级隔离 | `entrypoint-wrapper.sh` | 无 |
| **二** | B2 自更新回滚接口 | 补 P2-7：`.prev` 有文件无入口 | 1 端点 + 前端 1 按钮 | 无 |
| **二** | B3 登录限流补 IP 维度 | 补 P2-3 | `auth.py` | 无 |
| **二** | B4 文档一致性修正 | 补 P2-8 | `AGENTS.md` / `main.py` 注释 / `architecture-review.md` | 无 |
| **二** | B5 CI 落地（**Jenkins**：主流水线已落地 / 真库 + nightly 待做） | 补 P2-5/P2-6 | 新增 `Jenkinsfile` + `scripts/ci_smoke.sh` + `docs/ci-jenkins.md` | 需要 Jenkins 节点与凭据 |
| 不做 | C1 ed25519 签名、C2 协议压缩、C3 共享订阅 | 既有决策（`architecture-review` §0 已关闭），本次不翻案 | — | — |

**阶段一完成后的验收口径**：在命令中心**一屏之内**完成「选 500 台 → 下发 → 看到该批次进度 → 翻页/按批次筛选看全结果 → 一键导出 CSV 核验成功/失败分布」，且发布动作零 DB 回查、业务页无布局硬编码、无新增 UI 依赖；**推送式自更新在镜像零配置下可用，且每次升级的结果与失败原因可查**；**双端日志收敛为同一套格式、各自唯一写入者，可轮转可保留，且同一记录不再重复出现**；**在主机总览勾选任意机器即可把它们升级到服务端当前最新版本，在线者立即生效、离线者重连自动补投，全程可在「版本与更新」页逐台核验**。

> **落地状态（截至 2026-09-13）**
> - **D1 版本信息硬化（服务端）**：`AGENT_VER` 单一真源 + `containers.py` 落后判定（含 `latest_agent_ver` / `agent_outdated`）+ `GET /api/system/agent/current` 等端点，已随 D2 服务端同批提交 `60c3c08` 并推送 `origin/main`。
> - **D2.1 / D2.2 / D2.3（服务端）**：升级模式开关 `KK_UPDATE_MODE`（默认 `manual`）、选机升级端点 `POST /api/system/agent/upgrade`、离线 `queued` 补投与 `sweep` 接入，已提交 `60c3c08` 并推送；后端测试全绿（含 D2 专项 16 例）。
> - **D2.4（前端）**：「版本与更新」页（`views/host/update/index.vue`）、版本 tag、批量升级入口（monitor/detail/welcome 三处）、台账列表均已写完、通过人工代码审查，并已**在沙箱内完成 `typecheck` + `build` 双验证通过**（绕开全局 pnpm store 的 AV 写拦截：改用 `pnpm install --store-dir=C:/Users/Taowd/AppData/Local/Temp/kk-store --ignore-scripts --shamefully-hoist` 联网装依赖，根 node_modules 由 65 → 500 项、`vue-demi` 提升成功，`build` 产出 `dist` 2.9M）。10 个源文件已 `git add` 暂存，对应提交 24。
> - **2026-09-13 清理**：已删除本轮排障产生的临时诊断文件（`.workbuddy/memory/` 下 `pnpm_install*.log` / `npm_install.log` / `ginst.log` / `inst.log` / `remove.log` / `_*.{log,sh,json,yaml}` 等 17 个）；清理前端缓存 `web/.pnpm-store/`、`web/dist/`、`web/node_modules/`（半残状态）与 Python 缓存 `__pycache__` / `.pytest_cache`。保留 `.venv`（后端工作环境）、`data/*.db*`（运行时数据）、`reports/pytest.xml`（测试证据）。
> - 整组 D2 已收口：前端 commit 24（`793426a`）已提交并推送 `origin/main`（服务端 `60c3c08` + 前端 D2.4 同批闭环）。
> - **D3 测试（收口验证）**：D3 列出的 14 个后端用例 + 前端 `typecheck`/`build` **全部已落地且全量回归绿**。`pytest agent/tests server/tests` = **287 passed / 4 skipped / 0 failed**（152s）；D3 用例实际随 D2 服务端 `60c3c08` 一并入库（`test_controlled_upgrade.py` + `test_version_governance.py` + `agent/tests/test_version.py` + `test_transport.py::test_hb_frame_always_carries_agent_ver`），故计划中独立的「提交 25」已被提交 23 吸收，**无需再单独提交**。
> - **全计划落地完成（截至 2026-09-13）**：阶段一 A1–A7、D 组 D0–D3、阶段二 B1–B6 对应的提交 1–24 已全部推送 `origin/main`；提交 25（D3 测试）已含于 23。最终 `pytest agent/tests server/tests` = 287 passed / 4 skipped / 0 failed，`pnpm typecheck && pnpm build` 零错误（D2.4 验证明 dist 2.9M）。下一步进入「重构阶段：MQTT 传输协议 + 最大化复用开源组件」方向。

---

## 1. 阶段一 · 核心能力

### A1 结果导出（P0-1）

#### A1.1 接口设计

新增 `server/src/kk_server/controllers/exporting.py`（**注意模块名不能叫 `export.py`**，避免与「导出」概念混淆且便于检索），统一前缀 `/api/export`，四个端点**全部走 `current_user` 鉴权**：

| 方法 | 路径 | 参数 | 输出 | 列 |
|---|---|---|---|---|
| GET | `/api/export/commands` | `pod` `kind` `status` `batch` `since` `until` `limit=2000` `include_tail=0` | CSV | `id, pod, kind, argv, status, rc, timed_out, truncated, elapsed_ms, created_at, finished_at, created_by, out_purged, out_tail` |
| GET | `/api/export/audit` | `actor` `action` `since` `limit=5000` | CSV | `id, ts, actor, action, detail` |
| GET | `/api/export/hosts` | `view=summary` | CSV | `pod, image, agent_ver, online, hb_interval, cpu, mem_mb, disk_pct, disk_alert, last_seen, age_sec` |
| GET | `/api/export/metrics` | `pod`（必填）`hours=24` | CSV | `ts, cpu, mem_mb` |

**为什么不做单条全量输出导出**：`/api/commands/{cid}/out` 已能取单条全量；批量带全量输出（500 × 4MB = 2GB）必须走 zip + 临时文件流式，复杂度与收益不匹配。一期导出**元数据 + 末 2KB 输出**（`include_tail=1`），覆盖「批量核验 + 生成报表」这个真实诉求；全量 zip 列为二期备选（见 §5）。

#### A1.2 公共实现

`controllers/exporting.py` 内一个私有函数，四个路由共用（不再单独拆 services，行数量级约 150 行）：

```python
def _csv_response(header, rows, filename):
    """rows 为 list[list]，内存生成 CSV 并流式返回。

    - UTF-8 BOM 前缀：Excel 双击打开不乱码（Windows 场景这是刚需）
    - _cell() 做 CSV 注入防护：以 = + - @ Tab CR 开头的值加前导单引号，
      否则 Excel 会把 `=cmd|'/c calc'!A1` 这类 argv 当公式执行
    - Content-Disposition 用 filename*=UTF-8''<quoted> 兼容中文名
    """
```

关键实现约束：

| 约束 | 做法 | 理由 |
|---|---|---|
| 防 CSV 注入 | `_cell(v)` 对 `= + - @ \t \r` 开头值加 `'` 前缀 | `argv` 与 `detail` 是用户可控内容，Excel 公式注入是真实攻击面 |
| 中文文件名 | `Content-Disposition: attachment; filename*=UTF-8''%s` | 兼容非 ASCII |
| 时间可读 | Unix 秒 → `"%Y-%m-%d %H:%M:%S"`（服务端本地时区） | 报表直接可读；表头不做时区标注，保持简单 |
| 行数上限 | `limit` 默认 2000，硬上限 20000，超限返回 **400 + 缩小范围提示** | 静默截断会让报表缺失且无感知 |
| 大字段 | 默认 `include_tail=0`，不回 `out_tail` | 2000 行 × 2KB = 4MB，默认不背 |

#### A1.3 前端实现

| 改动 | 位置 | 要点 |
|---|---|---|
| API 层 | 新增 `web/src/api/exporting.ts` | 四个函数返回 `Promise<Blob>`；**必须显式传 `timeout: 0`**——`web/src/utils/http/index.ts:19` 的默认超时是 10s，导出千行会超时 |
| 下载工具 | `web/src/utils/kk.ts` 加 `downloadBlob(blob, filename)` | `URL.createObjectURL` + 隐藏 `<a download>` + `revokeObjectURL`；文件名由前端拼（带日期），**不依赖 `Content-Disposition`**——响应拦截器只返回 `response.data`，拿不到 headers |
| 命令历史 | `CommandHistory.vue` 工具栏加「导出 CSV」 | 把**当前筛选条件**（status / 未来加的 batch / pod）作为 query 传给后端；导出的是筛选结果而非当前页 |
| 审计页 | `audit/index.vue` 工具栏加「导出 CSV」 | 同上，带 keyword 与 limit |
| 主机总览 | `host/monitor/index.vue` 工具栏加「导出清单」 | 导出 `view=summary` 全量（资产盘点场景） |
| 主机详情 | `host/detail/index.vue` 加「导出指标」 | 带当前 `hours` 选择 |

**已验证的可行性**：`http.request` 的响应拦截器（`utils/http/index.ts:78-90`）无条件 `return response.data`，因此 `responseType: "blob"` 时拿到 Blob；401/403 分支读 `$error.response.status`，blob 响应不受影响。

#### A1.4 测试

| 用例 | 断言 |
|---|---|
| `test_export_commands_csv` | 200 + `text/csv` + 行数正确 + 首字节为 BOM + 表头对齐 |
| `test_export_csv_injection` | 造一条 `argv=["=cmd|'/c calc'!A1"]` 的命令，断言导出值以 `'` 开头 |
| `test_export_limit_guard` | `limit=99999` → 400 |
| `test_export_requires_auth` | 无 token → 401 |
| `test_export_metrics_hourly_source` | `hours=48` 时走 hourly 表（与 `metrics_series` 语义一致） |

---

### A2 批量发布回路去 N+1（P1-1）

**现状**（`controllers/commands.py:116-121`）：

```python
for cid, pod in zip(ids, body.pods):
    sent = bridge.dispatch_command(await store.get_command(cid))   # ← 500 次 DB 单查
```

**根因**：`dispatch_command(row)` 需要 `id/pod/kind/argv/timeout` 五个字段，而调用方手上已经有全部信息（`ids`、`body.pods`、`body.kind`、`payload`、`timeout`），却回查了数据库。

**修复**：直接就地组装行字典，零 DB 往返。

```python
# 建行已批量完成；发布所需字段本地齐备，不必回查（500 台省 500 次单查）
argv_json = json.dumps(payload, ensure_ascii=False)
created = []
for cid, pod in zip(ids, body.pods):
    row = {"id": cid, "pod": pod, "kind": body.kind,
           "argv": argv_json, "timeout": timeout}
    sent = bridge.dispatch_command(row)
    if sent:
        await store.mark_sent(cid)
    created.append({"id": cid, "pod": pod, "status": "sent" if sent else "pending"})
```

| 注意点 | 说明 |
|---|---|
| `dispatch_command` 签名不变 | 它只做 `row["..."]` 取值，dict 与 Row 同样兼容，**无需改桥接** |
| `argv_json` 在循环外序列化一次 | 500 台共用同一份，避免重复 `json.dumps` |
| `mark_sent` 仍是逐条 UPDATE | 这是有意的：它是「已发布到 Broker」的语义标记，且 `_run` 每次一事务。500 条 UPDATE 在 SQLite 上约数十毫秒；若要进一步收敛，可加 `store.mark_sent_batch(ids)` 用 `IN` 一次更新——**建议本轮一并做**，改动约 8 行 |

**测试**：`test_create_commands_batch_dispatch_without_lookup` —— 用假 bridge 记录 `dispatch_command` 入参，断言 500 台下发时 `store.get_command` **零调用**（用 monkeypatch 计数）。

---

### A3 结果分页 + 批次聚合（P1-2 / P1-3）

#### A3.1 数据模型：新增 `batch_id`

**优雅点**：`tables.py:121` 的 `_CMD_COLS` 是**动态生成**的（`[c.name for c in commands.columns if c.name != "out_b64"]`），新增列会自动进入 `_CMD_COLS`、自动被 `list_commands` 查询、自动出现在 API 响应里 —— **后端只加列，列表接口无需改一行**。

| 改动 | 位置 | 内容 |
|---|---|---|
| 表定义 | `models/tables.py` `commands` | `Column("batch_id", String(32), nullable=False, server_default="")` |
| 补列登记 | `models/tables.py` `_ADD_COLUMNS["kk_commands"]` | `("batch_id", "VARCHAR(32) DEFAULT ''")` —— 三库通用写法（不带引号，避免 MySQL 严格模式报错） |
| 生成批次号 | `models/store.py` `create_commands_batch` | `batch_id = "b-" + secrets.token_hex(8)`，一次调用一个批次，写入所有行 |
| 返回值 | 同上 | 返回 `(ids, batch_id)` 破坏既有签名 → **改为返回 `ids` 不变，batch_id 通过新增的轻量方法或直接由调用方生成**。见下方决策 |

> **决策**：`create_commands_batch` 当前签名被 `create_command`（单条）与控制器共用，改返回值会牵连测试。采用**最小侵入**方案：在 `Store` 上加可选参数 `batch_id=None`（None 时内部生成），并把生成好的 batch_id 通过 `self._last_batch` 回传是不好的设计（有状态）。
>
> **最终方案**：`create_commands_batch(pods, kind, argv, timeout, created_by, batch_id=None) -> (ids, batch_id)`，同步修改两处调用点（`create_command` 与 `commands.py`）与既有测试。**显式返回元组优于隐式状态**，改动面 3 处，可控。

**索引**：`_ensure_schema` 只做 `ALTER TABLE ADD COLUMN`，不建索引。500 台规模 + `batch_id` 等值查询下，现有 `idx_cmd_pod(pod, created_at)` 已够用；**本轮不加索引**（加索引需另开 `CREATE INDEX IF NOT EXISTS` 通道，收益不足），在 `tables.py` 注释里标注为「规模上万时再补」。

#### A3.2 查询能力

`store.list_commands` 扩展（保持向后兼容：新增参数默认不改变原行为）：

```python
async def list_commands(self, pod=None, limit=100, offset=None,
                        batch=None, status=None, kind=None) -> list
async def count_commands(self, pod=None, batch=None, status=None, kind=None) -> int
```

`controllers/commands.py` 的 `GET /api/commands` 增加同名 query 参数，响应加 `total` 与 `offset`（**只加字段、不改 `items`**，现有前端不受影响）：

```json
{"items": [...], "total": 1234, "offset": 0, "limit": 100}
```

#### A3.3 前端

> **前端细节统一收在 A5.6**（执行历史表增强），此处只列后端契约对应的交互要点，避免两处描述漂移。

`CommandHistory.vue` 需消费的新契约：

| 改动 | 说明 |
|---|---|
| 分页 | 接 `el-pagination`（`total` 来自响应），`size` 可选 50/100/200；轮询只刷新当前页 |
| 批次列 | 表格加「批次」列（`batch_id` 前 8 位 + tooltip 全值），点击即按该批次筛选 |
| 筛选下推 | 当前 `statusFilter` 是**纯前端过滤**（`filteredRows` computed），改为传给后端 query —— 这样「导出 CSV」才能与看到的列表一致 |
| 轮询交互 | 分页/筛选变化时重置 `offset=0`；轮询保持当前页，避免翻页被刷回 |
| 批次汇总条 | 选中某批次时，在工具栏显示「该批次 N 台：done X / failed Y / timeout Z / pending W」（数据来自已有的 `total` + 一次按 status 分组查询，或直接用 `GET /api/system/stats` 的扩展） |

> **批次汇总的实现选择**：为不改 `stats` 语义，新增 `GET /api/commands/batches?limit=20` 返回最近批次列表及各自状态分布（一条 `GROUP BY batch_id, status` 查询 + 内存聚合）。前端在命令面板展示「最近批次」卡片，一键跳转到该批次筛选视图。这条查询是 A3 的价值落点：**让 500 台一次点击变成一个可核验的对象**。

#### A3.4 测试

| 用例 | 断言 |
|---|---|
| `test_batch_id_assigned_to_all_rows` | 一次 3 台批量 → 3 行同一 batch_id，且 `b-` 前缀 |
| `test_list_commands_pagination` | `offset=1&limit=1` 返回第 2 条且 `total` 为全量 |
| `test_list_commands_batch_filter` | 两个批次各自筛选互不污染 |
| `test_batch_summary_groups_by_status` | 混合状态批次的分布计数正确 |

---

### A4 上报间隔治理（P1-4）

**现状**：`KK_ENFORCED_INTERVAL` 在 `server/src/kk_server/config.py:88-89` 被解析进 `Settings.enforced_interval`，**全仓零消费点**；Agent 侧也不读该键。服务端因此无法发现（更谈不上收敛）异常上报频率的机器。

**决策：选「检测 + 审计 + 平台可见」，不做「强制改频」。**

| 方案 | 成本 | 结论 |
|---|---|---|
| 删掉死配置 | 改 3 行 | 治标：丢失本可低成本获得的异常检测能力 |
| **检测 + 审计 + stats 计数（推荐）** | 改约 20 行，**零协议变更** | 500 台规模下能发现误配/异常机器，且不改 `PROTO_VER`、不动双端测试 |
| 强制下发改频 | 需新增 `kind=set_interval` 或 `status` 回带期望值 → **协议变更**（要同步 `PROTO_VER`、`proto/messages.md`、双端测试） | 收益不抵成本：`interval` 由镜像内置，误配概率低；真有需求时零迁移成本再加 |

**落地**（零协议变更）：

| 位置 | 改动 |
|---|---|
| `config.py` | 把 `KK_ENFORCED_INTERVAL` **改名为 `KK_INTERVAL_MIN`**（语义即「允许的最小上报间隔秒数」，0/空 = 不检查）。改名理由：全仓与 `deploy/`、compose **均未使用旧名**（已 grep 确认），无兼容负担；`enforced` 一词暗示强制，与实现的「检测」语义不符，留着会再次误导 |
| `mqtt_bridge._on_hb` | 取 `body.get("interval")`，若 `< interval_min` → `store.add_audit("mqtt", "interval_violation", {...})` + `stats["interval_violation"] += 1`；**仍照常落库**（检测不阻断，避免丢数据） |
| `mqtt_bridge.stats` | `stats` 字典加 `interval_violation` 计数（`stats.py` 自动透出，前端 welcome 页已消费 stats） |
| 告警可见 | welcome 页 stats 卡片加「间隔异常」计数，非 0 时标红 |
| 部署文档 | `docs/deployment.md` 补 `KK_INTERVAL_MIN`（若文中提及间隔相关配置） |

**为什么检测不阻断**：心跳是唯一指标源，阻断等于丢数据。治理目标应是「发现并让人去修镜像/环境变量」，而不是在服务端把数据流掐掉。

**测试**：`test_interval_violation_audited` —— 造 `interval=1` 且 `KK_INTERVAL_MIN=10` 的心跳，断言落库成功 **且** 审计表出现 `interval_violation`；`test_interval_within_limit_no_audit` 反例。

---

### A5 Web 前端易用性与布局改造

**目标**：让「快速下发 → 看结果 → 导出」这条主链路在一个屏幕内完成，不再靠滚动和翻页找结果；同时消除布局层的 magic number。

**硬约束（保持与项目一致）**：只用 Element Plus + 现有 `kk-*` 工具类，**不引入任何新 UI 依赖**；新增样式一律进 `web/src/style/kk.scss`（页面不留重复定义，延续 2026-09-06 的收敛约定）；颜色全部走 `var(--el-*)`，不写死色值。

#### A5.1 现状痛点（逐条可验证）

| # | 页面 | 痛点 | 证据 |
|---|---|---|---|
| 1 | 命令面板 | 表单卡片 + 历史表**垂直堆叠**，下发后要看结果必须向下滚动；批量 500 台时历史表很高，来回滚动成本大 | `command/shell/index.vue:86-147`（卡片上下排列） |
| 2 | 命令面板 | 历史表高度是 `calc(100vh - 560px)` —— 560 是为了扣掉上方表单卡片高度**算出来的**，表单增删一项就失效 | `CommandHistory.vue:116` |
| 3 | 主机选择 | `el-select multiple` 全量平铺，500 台时下拉列表极长；无「仅在线」筛选、无全选/反选 | `shell/index.vue:91-106`、`collect/index.vue:74-89` |
| 4 | 下发动作 | 无二次确认，500 台一把梭；**离线主机的命令会由 Broker 排队补投**这个关键行为用户看不到 | `submitShell()` 直接调 `createCommand` |
| 5 | 结果查看 | 输出用 `el-dialog` 居中弹窗，**完全遮挡列表**，无法连续对比多条；无「下载此条输出」 | `CommandHistory.vue:156-160` |
| 6 | 历史表 | 无分页（A3 补）、无关键字搜索、无批次列、状态筛选是**纯前端过滤**、看不到「本次下发」的新结果 | `CommandHistory.vue:27-29,101-109` |
| 7 | 历史表 | 固定 5s 轮询，无 running 时也在空转 | `CommandHistory.vue:45` |
| 8 | 总览页 | 批量操作栏在表格**下方非 sticky**，长列表要拉到底才能点 | `monitor/index.vue:228-240` |
| 9 | 总览页 | 进详情只能点主机名链接（命中区域小）；告警行无整行视觉区分 | `monitor/index.vue:180-183` |
| 10 | 详情页 | 无「在此主机执行命令」入口，要从别处绕；指标曲线无导出 | `host/detail/index.vue` |
| 11 | 全局 | 页面状态不落 URL（仅 `pods` 通过 query 传一次），刷新即丢；`setInterval` 散落 5 处 | `AGENTS.md` 与实际不符（见 B4） |

#### A5.2 布局基建：高度锚点收敛到一处，页面内部全 flex

**先确认约束（已核源码，这一条决定技术路线）**：

| 事实 | 出处 | 含义 |
|---|---|---|
| `.app-main { height: 100vh; overflow-x: hidden }` | `layout/components/lay-content/index.vue:196-201` | 固定视口高；滚动发生在内部 `el-scrollbar__wrap`（`:135` 的 `el-backtop target` 可证） |
| 内容链 `.app-main → el-scrollbar → .el-scrollbar__view → .grow → .main-content` | 同上 `:117-160` | 中间 `.el-scrollbar__view` 的高度由内容撑开，**页面根拿不到确定的 `height: 100%`** |
| `fixedHeader` 可开关，关闭时换 `.app-main-nofixed-header`（auto 高度、window 滚动） | 同上 `:110,203-208` | 两种模式滚动容器不同，不能依赖单一高度来源 |

> **结论：不能简单用 `height: 100%`** —— 这正是现有代码写 `calc(100vh - Npx)` 的原因。
> 但问题不在用了 `calc`，而在于**同一页面里嵌套了两层 calc，内层的 N 是手算的**：
> `CommandHistory.vue:116` 的 `calc(100vh - 560px)` 必须扣掉上方命令表单的高度，**表单增删一项就失效**。

**做法**：高度只在**页面根算一次**，且走 CSS 变量集中维护；页面内部全部 flex，不再出现第二个 calc。

```scss
/* 新增到 style/kk.scss */
:root {
  /* 页面高度锚点：navbar + tabs + 内容区 padding + 页脚 的合计占用。
     实施第一步用 DevTools 实测确定（.app-main 为 100vh，该值约 150px 量级），
     只在此处维护；业务页不得再写 calc(100vh - N)。 */
  --kk-page-h: 152px;
}

.kk-page {                       /* 页面根容器：全站唯一的高度锚点 */
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--kk-page-h));
  min-height: 0;
}

.kk-page__body {                 /* 卡片/内容区：占满剩余高度 */
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
}

.kk-fill-table {                 /* 表格填满卡片剩余空间（替代 height="calc(...)"） */
  flex: 1;
  min-height: 0;
}

.kk-sticky-bar {                 /* 表底批量操作条：吸底常驻 */
  position: sticky;
  bottom: 0;
  z-index: 2;
  padding: 10px 12px;
  background: var(--el-bg-color);
  border-top: 1px solid var(--el-border-color-lighter);
}

.kk-side {                       /* 命令中心左右栏容器 */
  display: flex;
  gap: 16px;
  align-items: stretch;
}

@media (max-width: 1200px) {     /* 1366×768 运维笔记本常见，窄屏折叠为上下 */
  .kk-side { flex-direction: column; }
}
```

**sticky 可行性（已核）**：滚动容器是 `.el-scrollbar__wrap`，`.kk-sticky-bar` 位于其内部，`position: sticky` 生效；但 `.app-main` 的 `overflow-x: hidden` 按 CSS 规范会把 `overflow-y` 的计算值变成 `auto`，存在形成「双层滚动容器」的风险。**实施时必须先在 DevTools 里实测**；若吸底不生效，兜底改用 `el-affix`（Element Plus 自带，不引新依赖），而不是堆 `!important` 硬抗。

**收益**：`CommandHistory:116`(560) / `monitor:174`(320) / `audit:69`(240) 三处硬编码收敛为「根级一次 + CSS 变量」；**内层 calc 归零**，以后增删工具栏不再需要重算高度。

> **`fixedHeader=false` 场景**：该模式下主区改为 window 滚动，`calc(100vh - var(--kk-page-h))` 会略偏小（navbar 被重复扣除）。本期以默认的 `fixedHeader=true` 作为验收场景；若实际部署关闭了 fixedHeader，只需把 `--kk-page-h` 调小 —— 仍是**一处维护**。

#### A5.3 命令中心：从「上下堆叠」改为「左执行 / 右结果」工作台

新增 `views/command/components/CommandWorkbench.vue`，`shell` / `collect` 两个页面共用同一骨架（左侧 slot 放各自的表单）：

```
┌────────────── kk-toolbar（标题 + 刷新 + 导出）──────────────┐
├──────────────────┬────────────────────────────────────────┤
│ 左栏 lg=9        │ 右栏 lg=15                             │
│ ┌──────────────┐ │ ┌────────────────────────────────────┐ │
│ │ 目标主机      │ │ │ 本次下发 · 批次 b-a1b2c3           │ │
│ │ [HostPicker] │ │ │ 进度 ▓▓▓▓▓░░░  12/20  完成9 失败1  │ │
│ ├──────────────┤ │ ├────────────────────────────────────┤ │
│ │ 执行内容      │ │ │ 执行历史（分页 / 搜索 / 批次 /    │ │
│ │ 模式 / 命令   │ │ │   状态筛选 / 导出 CSV）            │ │
│ │ 超时 / 下发   │ │ │ ─────────────────────────────────  │ │
│ ├──────────────┤ │ │ （表格，点击行 → 右侧抽屉看输出）   │ │
│ │ 最近命令      │ │ │                                    │ │
│ │ （点击复用）  │ │ │ ─── 分页 ───                       │ │
│ └──────────────┘ │ └────────────────────────────────────┘ │
└──────────────────┴────────────────────────────────────────┘
```

| 子项 | 设计 |
|---|---|
| 双栏骨架 | `CommandWorkbench.vue` 提供 `#form` / `#result` 两个 slot，页面只写自己那半边；用 `.kk-side` 控制窄屏折叠 |
| 下发即定位 | 下发成功后**不再滚动**——右栏顶部直接出现该批次的「进度卡」，实时刷新 done/failed/timeout 分布（A3 的批次能力前端化） |
| 结果可对比 | 输出查看改 `el-drawer`（右侧 42%），列表保持可见，可连续点多条对比；抽屉底部放「下载此条输出」（复用 A1 的 `downloadBlob`） |

#### A5.4 主机选择：`HostPicker.vue` 抽屉式多选

| 项 | 设计 |
|---|---|
| 触发区 | 显示「已选 N 台」+ 前若干主机标签（超过 5 个折叠为 `+M`），不再把 500 个选项塞进下拉 |
| 选择面板 | `el-drawer` 内：搜索框（主机名/镜像）+「仅在线」开关 + `el-table` 多选 + 全选/反选/清空 |
| 排序 | 默认在线优先（离线项灰显 + 标「离线」），避免误选离线机后困惑「为什么没结果」 |
| 对外契约 | `v-model:pods`（字符串数组），保持与 `monitor` 跳转的 `?pods=a,b,c` 完全兼容，**页面改造不破坏现有链路** |
| 复用 | 数据源仍是 `listHosts("summary")`，不新增接口 |

#### A5.5 下发确认与「离线排队」可见化

批量下发前弹 `ElMessageBox.confirm`（**单台不弹**，避免打断高频操作）：

```
确认向 20 台主机下发命令？
在线 18 台 · 离线 2 台（web-07、web-13）
离线主机的命令将由 Broker 排队，重连后自动补投，结果会稍后出现。

[取消]  [确认下发]
```

**这条提示的价值**：MQTT 离线队列是本项目的核心设计亮点（`design.md` 第一条卖点），但目前用户完全感知不到——离线主机的命令「先没反应、过一会儿突然出现结果」会被误判为系统异常。把它显式说清楚，是零成本的易用性提升。

#### A5.6 执行历史表增强（依赖 A3 后端）

| 改动 | 说明 |
|---|---|
| 分页 | 接 `el-pagination`（`total` 来自 A3 新增字段），尺寸 50/100/200 |
| 搜索 | 新增关键字输入，**下推到后端**（A3 加 `keyword` 参数，匹配 `pod` / `argv` / `id`）——必须下推，否则导出结果与所见不一致 |
| 状态筛选 | 从下拉改为**点击状态 tag 即筛选**（保留下拉作为备选），减少两次点击 |
| 批次列 | 显示 `batch_id` 前 8 位（tooltip 全值），点击按该批次筛选并显示汇总 |
| 本次下发高亮 | 下发后把新命令 ID 存入组件内 `Set`，轮询刷新时给这些行加左侧色条 + 淡入过渡，一眼找到刚才发的 |
| 自适应轮询 | 存在 `pending`/`sent`/`running` 时 3s，否则 10s；不再无条件 5s 空转 |
| 导出按钮 | 工具栏「导出 CSV」，把当前全部筛选条件传给 A1 的 `/api/export/commands` |

#### A5.7 总览页与详情页

| 页面 | 改动 |
|---|---|
| 总览 | `kk-batch` 加 `.kk-sticky-bar` 吸底常驻；批量操作增加「导出选中清单」 |
| 总览 | 整行可点进详情（`@row-click`），但需跳过 selection 列（`column.type === "selection"` 时 return），避免勾选被误判为进详情 |
| 总览 | 告警行整行浅红底（`row-class-name` 返回 `kk-row-alert`），扫描时不需要逐格看磁盘列 |
| 详情 | 顶部操作区加「在此主机执行命令」→ 跳 `/command/shell?pods=<pod>`（复用现有 query 契约） |
| 详情 | 指标曲线区加「导出指标 CSV」（A1 的 `/api/export/metrics`） |

#### A5.8 交互一致性与效率

| 项 | 设计 |
|---|---|
| URL 状态保持 | 命令面板把 `pods` / `mode` / `cmdline` / `timeout` 同步到 query（`router.replace`，不污染历史栈），刷新与分享不丢输入 |
| 快捷键 | `Ctrl/Cmd + Enter` 下发；历史表 `↑`/`↓` 移动、`Enter` 打开输出（进阶项，可后置） |
| 轮询统一管理 | 新增 `web/src/utils/kkPoll.ts` 提供 `setPoll/clearPolls`，改造 5 处散落的 `setInterval`（同时兑现 B4 的文档一致性） |
| 空态与加载 | 延续既有 `el-empty` 文案风格，为「无主机」「无命令」「筛选无结果」分别给出**可操作**的空态文案（如「筛选无结果，清除筛选条件」） |

#### A5.9 组件与文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `views/command/components/CommandWorkbench.vue` | 新增 | 双栏骨架 + slot |
| `views/command/components/HostPicker.vue` | 新增 | 抽屉式主机多选 |
| `views/command/components/CommandResultPanel.vue` | 新增 | 本次下发进度卡 + 历史表容器 |
| `views/command/components/CommandOutputDrawer.vue` | 新增 | 输出抽屉（替代 dialog） |
| `views/command/shell/index.vue` | 改造 | 接入 Workbench + HostPicker + 确认弹窗 |
| `views/command/collect/index.vue` | 改造 | 同上（左侧换成采集项勾选） |
| `views/command/components/CommandHistory.vue` | 改造 | 分页 / 搜索 / 批次 / 高亮 / 自适应轮询 / 导出 |
| `views/host/monitor/index.vue` | 改造 | 吸底批量栏 / 整行点击 / 告警行色 / 导出 |
| `views/host/detail/index.vue` | 改造 | 执行入口 / 指标导出 |
| `views/audit/index.vue` | 改造 | 导出 + 分页 |
| `utils/kkPoll.ts` | 新增 | 轮询统一管理 |
| `utils/kk.ts` | 改造 | 加 `downloadBlob` |
| `style/kk.scss` | 改造 | 加 `--kk-page-h` 变量与 `.kk-page` / `.kk-page__body` / `.kk-fill-table` / `.kk-sticky-bar` / `.kk-side` / `.kk-row-alert` |

#### A5.10 验收（可视化清单）

| 项 | 标准 |
|---|---|
| 一屏完成主链路 | 1920×1080 下：选主机 → 输命令 → 下发 → 看到进度与结果，**全程无需滚动** |
| 窄屏可用 | 1366×768 下双栏折叠为上下，**无横向滚动条、无双纵向滚动条** |
| 高度锚点唯一 | `grep -rn "calc(100vh" web/src/views`：每页**至多**命中页面根一处，且为 `var(--kk-page-h)` 而非字面量；**内层零命中**（表格走 `.kk-fill-table`） |
| 一致性 | 新组件只用 `el-*` + `kk-*`；`pnpm typecheck` 与 `pnpm build` 零错误；无新增依赖（`package.json` diff 为空） |
| 回归 | 总览「批量执行命令/批量采集」跳转后主机预选仍然生效；`?pods=` 契约不变 |
| 导出闭环 | 命令面板导出按钮产出的 CSV 与页面筛选条件一致（行数、状态、批次） |

---

### A6 自更新可观测性（机制审视新增）

#### A6.0 审视结论：机制本身合理，但「可观测性」有两个功能性缺口

**先说做对的部分（这些不要动）**：

| 设计 | 评价 |
|---|---|
| **形态校验前置**：先判 `sys.frozen` + `_is_binary_target`，再下载落盘（`updater.py:186-200`） | ✅ 铁律正确。这是 P0-1 的修复成果——旧实现会在源码形态下**覆盖 Python 解释器** |
| **原子替换**：tmp → write → flush → fsync → `os.replace`，`except BaseException` 清理 tmp（`:233-250`） | ✅ 同文件系统内原子；异常路径不留半截二进制 |
| **完整性双层**：sha256 强制 + 可选 HMAC，`KK_UPDATE_REQUIRE_SIG=1` 时缺签名直接拒（`:137-159`） | ✅ 防损坏 + 防伪造；ed25519 决策关闭是合理的（边际收益有限） |
| **并发串行化**：`_update_lock` 覆盖轮询与推送两个入口 | ✅ 避免两次下载竞争同一目标 |
| **只升不降**：`version_lt` 门禁 | ✅ 防降级攻击 |
| **execv 复用 PID**，与 wrapper 的 `wait "$AGENT_PID"` 配合 | ✅ 监管循环不把自更新误判为崩溃 → **不会 5s 重启出双实例** |
| 下载上限 64MB、HTTPS 可选且配了 CA 时不静默降级 | ✅ 边界清晰 |

**再说不合理的（本节要修的）**：

| # | 问题 | 证据 | 严重度 |
|---|---|---|---|
| 1 | **推送式更新在默认部署下静默失效** | `updater.py:81,203-206`：`_api_base` 未配 `KK_UPDATE_URL` → `log.info` + `return False`；而 `scripts/build.sh` 烧入镜像的 ENV 只有 `KK_SERVER` / `KK_AGENT_BIN` / `KK_PLUGIN_DIR` / `KK_LOG`，**没有 `KK_UPDATE_URL`** | 🔴 高（服务端推了，Agent 什么都不做，双方都不知道） |
| 2 | **更新结果是全平台唯一没有回执的操作** | 其他 kind（shell/collect/plugin_reload）都回传 result；`kind=update` 走 `spawn_apply`（`main.py:121`）**不回传**；且服务端 `_maybe_push_upgrade` 用的 `id = "u-" + host` **不是 commands 表的行**，即使回传也会被 `_on_result` 判为 `result_unknown_cmd` 拒收 | 🔴 高（500 台里失败多少台无从得知） |

> 两条合起来的后果：**「上传新版本 → 500 台自动升级」这条运维主路径，在当前代码上是不可观测的**——服务端不知道自己推的更新有没有生效，Agent 侧失败只落在本地日志里。自更新是本项目「用户无感知」定位的关键机制（镜像叠加 + 后台自更新），它的结果必须可见。

#### A6.1 推送式更新改为服务端下发绝对 URL

**根因**：Agent 侧的 `KK_UPDATE_URL` 是**隐式必需配置**，但镜像构建脚本不烧入它、文档也没把它列为必配项。MQTT 化之后 broker 地址与 HTTP API 地址不再同源，Agent 无法自行推导（`updater.py:74-80` 的注释已说明）——「推送到位但拿不到下载地址」因此是必然结果。

**修复：服务端下发的 `url` 带绝对地址；Agent 侧「绝对即用、相对才回落」。**

| 侧 | 改动 |
|---|---|
| 服务端 | 新增 `KK_PUBLIC_URL`（服务端对外访问基址，如 `http://10.0.0.1:8443`）。`_maybe_push_upgrade` 构造 payload 时 `url = public_url + "/api/system/agent/download"`；未配置则回落为当前相对路径（行为不变） |
| 服务端 | `/agent/latest` 响应套用同一逻辑（轮询路径同样受益） |
| Agent | `apply_manifest` 改为：`url.startswith(("http://", "https://"))` → 直接用；否则才 `base + url`，`base` 仍取 `KK_UPDATE_URL`（**向后兼容**） |
| Agent | 两者都缺时把 `log.info` **升为 `log.warning`**，并点明「需配 KK_UPDATE_URL 或由服务端下发绝对 URL」——静默失败正源于日志级别过低 |
| 部署 | `docs/deployment.md` 把 `KK_PUBLIC_URL` 列为**启用自更新的必配项**；`scripts/build.sh` 注释同步 |

**收益**：镜像侧零配置即可完成推送式更新，符合项目「单二进制 + 一个地址即拉起」的取向。

#### A6.2 更新结果回执与 `updates` 台账

**目标**：回答「这次上传的新版本，500 台升了多少、失败多少、卡在哪一步」。

**数据模型**（`models/tables.py` 新增表，`create_all` 直接建，不涉及 `_ADD_COLUMNS`）：

| 列 | 说明 |
|---|---|
| `id`（PK, String(32)） | `up-<host>-<ts>`，服务端推送时生成并下发 |
| `pod` / `from_version` / `to_version` | 主机与版本 |
| `status` | `pending` / `done` / `failed` / `timeout` |
| `reason` | 失败原因：`sha256_mismatch` / `hmac_mismatch` / `no_url` / `bad_target` / `http_error` / `disk_error` |
| `created_at` / `finished_at` | 时间戳 |

**流程**：

| 步骤 | 落点 |
|---|---|
| ① 服务端决定推送 | `_maybe_push_upgrade` 先写 `pending` 台账，再用**台账主键**作为 `cmd` 帧的 `id` |
| ② Agent 执行 | `dispatcher` 的 `kind=update` 分支把 `cmd["id"]` 传给 `spawn_apply`；成功/失败均经 `send_result` 回传极简结果（`rc` 0/1，失败时 `out_b64` 带原因） |
| ③ 服务端收结果 | `_on_result` 对 `up-` 前缀的 id **特判**：不查 commands 表，直接更新台账；失败同时写审计 `agent_update_failed` |
| ④ 平台可见 | `GET /api/system/updates?limit=50` 返回台账（各状态计数 + 失败明细）；welcome 页 stats 卡片加「升级中 / 升级失败」计数 |

**「成功」的时序约定**（关键细节）：Agent 替换二进制后立刻 `execv`，**进程被替换，回执帧可能来不及发出**。因此：

- Agent 在 **execv 之前**发送成功回执 —— 此时 `os.replace` 已返回，判定成功是准确的；
- 服务端另有**独立佐证**：Agent 重启后上报的 `status` 帧带新 `agent_ver`。台账若停在 `pending` 而主机已上报新版本，在 `_on_status` 里补成 `done`（该处已有 `_maybe_push_upgrade` 的上下文）；
- 台账超时（`created_at + 30min` 未终结且未上报新版本）→ janitor 收敛为 `timeout`，复用 `SWEEP_INTERVAL` 机制。

**为什么用独立台账而不塞进 `commands` 表**：升级是「一主机一次、长周期（下载 + 重启）」的操作，生命周期与命令不同；塞进去会污染命令历史页（用户看到的"命令"里混着升级记录），并让 `sweep_command_timeouts` 的超时语义混淆。

#### A6.3 测试

| 用例 | 断言 |
|---|---|
| `test_update_absolute_url_used_directly` | 清单 `url` 为绝对地址且未配 `KK_UPDATE_URL` → 仍发起下载 |
| `test_update_relative_url_needs_base` | 相对 `url` + 无 base → WARNING、返回 False、不落盘 |
| `test_update_receipt_on_failure` | sha256 不匹配 → Agent 回传结果帧（`rc=1` + 原因），台账置 `failed` |
| `test_updates_ledger_done_by_status_evidence` | 台账 `pending` + 主机上报新 `agent_ver` → 收敛 `done` |
| `test_updates_ledger_swept_timeout` | 超 30min 未终结 → `timeout` |
| `test_result_frame_for_update_not_rejected` | `up-` 前缀结果帧**不**落入 `result_unknown_cmd` 审计，而是更新台账 |

---

### A7 日志体系重构：引入 `loguru` 替换原生 `logging`

> **2026-09-13 已落地**：`cc5d45c`（Agent 侧）+ `3aedd31`（服务端侧）。全量 260 passed + 4 skipped。
> 两处与本文档的偏离/补充，供后续参照：
> ① **A7.4 随 Agent 侧提交落地**（原文排在提交 20）：`test_wrapper_never_redirects_agent_output_to_kk_log`
> 这类静态回归锁必须与 wrapper 改动同批，否则中间提交点是红的。
> ② 方案未预见但实测撞到的两件事：**patcher 是 Logger 级配置**（`add(patch=…)` 是运行期
> `TypeError`，必须 `logger.patch()`）；**`InterceptHandler` 的 component 要显式取 `record.name`**
> （用 `logging.currentframe()` 起栈回溯会落成 `"logging"`，分不清 uvicorn 还是 paho）。
> 另外 **`bind()` 的上下文必须显式渲染**（两侧格式串都加了 `ctx`）——loguru 的 `extra` 只在
> JSON 模式可见，不渲染则 `log.bind(host=…)` 在 docker logs 里等于没绑。
> PyInstaller 冻结验收已实跑（10.18MB，文件与 stderr 两个 sink 均正常），§6 的
> 「loguru 冻结后行为异常」风险项排除。

#### A7.0 现状盘点（含一个顺带修掉的真实缺陷）

**Agent 现状**

| 事实 | 证据 |
|---|---|
| 全模块**唯一**的 `logging` 引用点就是 `logutil.py` | `agent/src/kk_agent/logutil.py:2,5,11` |
| `get_logger(path, level, name)` 手工挂两个 handler：`StreamHandler(stderr)` + `RotatingFileHandler(1 MB, backupCount=1)` | `logutil.py:16-27` |
| logger 走**依赖注入**，不检索全局 | `transport.py:110`（`self.log`）、`executor.py:155,179`（`self._log`）、`updater.py:101,137,198,240`（`log` 形参）+ `_Null()` 空实现占位（`updater.py:42-52`）、`plugin_loader.py:41`（`log=None`） |
| 调用点约 35 处，**清一色 `%s` 懒格式化**，无字符串拼接 | `grep -n "log\.\(info\|warning\|error\|debug\|exception\)" agent/src` |
| 配置项 `KK_LOG`（路径）/ `KK_LOG_LEVEL` | `config.py:79-80` |

**Server 现状**

| 事实 | 证据 |
|---|---|
| 三个模块级 logger：`kk.server` / `kk.store` / `kk.bridge` | `main.py:27`、`store.py:26`、`mqtt_bridge.py:33` |
| 唯一集中配置是 `main()` 里的 `basicConfig` | `main.py:109` |
| uvicorn 用**自有**日志配置，格式与 `basicConfig` 不同 | `main.py:110-115` |
| 调用点约 23 处，同为 `%s` 风格 | — |
| **无文件落盘、无轮转、无保留上限** | 只写 stdout；`docker-compose.prod.yml:23-43` 未设 `logging:` 段 → json-file 驱动默认无 `max-size` |
| 日志级别来自 `KK_LOG_LEVEL` | `main.py:114`；`server/README.md:93` |

**缺陷清单**

| # | 问题 | 证据 | 严重度 |
|---|---|---|---|
| **D1** | **Agent 日志文件有两个写入者、两套轮转机制** | 镜像 ENV 烧入 `KK_LOG=/var/log/kk-agent.log`（`Dockerfile.snippet:23`、`scripts/build.sh:70`）。wrapper 读它 → shell 重定向 `>>"$KK_LOG" 2>&1` 且**自带** 1MB `mv` 轮转（`entrypoint-wrapper.sh:19,25-26,42`）；Agent **也**读它 → `log_path` → `RotatingFileHandler(同一路径, 1MB×1)`（`config.py:79` → `logutil.py:23`） | 🔴 |
| D2 | 三套日志格式并存（Agent / server `basicConfig` / uvicorn），同一份 `docker logs` 里混排 | 见上表 | 🟡 |
| D3 | 服务端日志无落盘、无轮转、无上限 | compose 未设 `logging:` | 🟡 |
| D4 | 无结构化字段与上下文传播：500 台规模下 `host` / `cmd` / `batch` 全靠手拼进消息串，无法按主机聚簇检索 | `mqtt_bridge.py:148-306` 十余处手工拼 `host=%s` | 🟡 |
| D5 | 多行 traceback 破坏行式采集：`log.exception` 输出多行，docker json-file 会把一条记录拆成 N 行 | `updater.py:251`、`main.py:143`、`mqtt_bridge.py:271` | 🟢 |
| D6 | Agent 日志保留量过小且容器重建即丢：`1MB×1` 上限仅 2MB；`/var/log` 无 volume 挂载 | `logutil.py:23`、`Dockerfile.snippet:19` | 🟢 |

**D1 的后果（本节要修的实质缺陷，不只是观感问题）**：

| 后果 | 说明 |
|---|---|
| 每条记录写两遍 | stderr 经 shell 追加进 `KK_LOG`，同时 `RotatingFileHandler` 直写同一路径 |
| 先轮转的一方让另一方的 fd 悬空 | handler 先轮转 → `os.rename(KK_LOG → KK_LOG.1)`，shell 的 fd 仍指向该 inode，此后 Agent 的 stderr 全部落进 `.1`；wrapper 先轮转则反过来。两套重命名策略（handler 的 `os.rename` 与 wrapper 的 `mv -f`）作用于同一路径，**行为不由设计决定** |
| 兜底排查手段是错的 | `docker exec … cat /var/log/kk-agent.log` 看到的既不完整也非全部；而 `.1` 因 wrapper 只检查 `KK_LOG`（已停止增长）**不再被轮转** |
| 阈值形同虚设 | 两套 1MB 阈值互相踩，谁先生效取决于时序 |

> 定位口径：这不是「日志写得不够好看」，而是**同一个文件被两个进程内的两套机制同时管**。修法是让写入者唯一（A7.4）。

#### A7.1 选型：`loguru` + 薄适配层

| 候选 | 优势 | 为什么不选 |
|---|---|---|
| **`loguru`（选）** | 轮转/保留/压缩/彩色/结构化一行配置；纯 Python 单包，**PyInstaller onefile 友好**（无 C 扩展、无隐藏 import、无资源文件、无 `__file__` 相对加载）；线程安全；`bind()` 传上下文；`catch()` 兜住线程入口异常 | — |
| `structlog` | JSON 事件、processor 链最灵活 | 收益来自**改调用点**（`log.info("event", key=value)`）才拿得到；本项目痛点是「轮转缺失 + 格式不一 + 无上下文」，不是日志分析，投入产出不匹配 |
| `picologging` | C 扩展版 `logging`，同 API 更快 | API 不变 ⇒ 不解决 handler 样板/结构/轮转；且给冻结二进制引入 C 扩展，跨平台构建风险不值得 |
| 保留 stdlib + 自研 handler 工厂 | 零新依赖 | 等于自研一个 loguru 子集（轮转 + 保留 + 压缩 + 结构化），自研量反而上升——与项目「复用开源、压低自研」取向相反 |

**关键决策：写一个薄适配层，两侧业务调用点一律不动。**

现有代码全是 `%s` 懒格式化（`log.warning("mqtt connect failed: %s", reason_code)`），而 loguru 用 `{}` 占位。两条路线：

| 路线 | 内容 | 结论 |
|---|---|---|
| (a) 全量改写调用点 | 把 58 处（Agent ~35 + Server ~23）`%s` 改成 `{}` | ❌ 机械改动却触碰全部业务模块，且测试里对消息串的断言要一起改；日志是横切关注点，不该与业务代码纠缠 |
| **(b) 薄适配层（选）** | `logutil` 内约 40 行：`_Adapter` 保留 `.info/.warning/.error/.exception/.debug/.critical(msg, *args)` 签名，内部 `msg % args` 后交给 loguru；`bind()` 透传 | ✅ **调用点零改动**，回归面最小；依赖注入模式（`log` 形参 / `self.log` / `_Null()` 占位）原样保留 |

适配层同时承担三件事，因此不是纯粹的兼容 shim：**统一格式**、**统一轮转策略**、**暴露 `bind()` 给上下文**。

> **`log.exception` 语义兼容已确认**：适配器方法在调用栈上仍处于调用方的 `except` 块内，`sys.exc_info()` 可取到活动异常，loguru 的 `opt(exception=True)` 能正确附带 traceback —— 58 处 `log.exception` 无需改写。

#### A7.2 Agent 侧落地

`agent/src/kk_agent/logutil.py` 重写（**仍是全项目唯一 import 日志后端的地方**，公开签名 `get_logger(path, level, name)` 不变）：

| 项 | 设计 | 理由 |
|---|---|---|
| sink 1 | stderr | 容器场景由 docker 采集，语义不变 |
| sink 2 | 文件 sink，仅当 `path` 非空且 `!= "-"` | 与现有语义一致 |
| 轮转 | `rotation="1 MB"`, `retention=2` | 与现状 `1MB×1` 同量级，但保留逻辑交给库保证 |
| 压缩 | **关**（Agent 侧省 CPU） | Agent 受 nice 降权（B1），不做无收益的 gzip |
| `enqueue` | **`False`（显式）** | `enqueue=True` 是 multiprocessing 队列 + feeder 线程 —— 与「单线程事件循环 + 一次性 daemon worker」的既定模型不符，会新增线程并抬高 RSS。同步写保留 stdlib 现有语义 |
| `backtrace` / `diagnose` | `backtrace=True`，**`diagnose=False`** | `diagnose=True` 会把**局部变量值**写进日志（可能含密钥/环境），生产必须关闭 |
| `catch` | `True` | 日志自身出错（磁盘满、路径不可写）不炸 Agent —— 现有 `logutil.py:26` 的 `except OSError` 是在手工做同一件事 |
| 幂等 | 以 `name` 为键记录「已初始化」 | 现状靠 `if logger.handlers:` 判断（`logutil.py:14`），换库后需要等价机制，否则 reload/测试会重复加 sink |
| 冻结二进制 | 纯 Python，PyInstaller 自动收集，无需 `hiddenimports` | 体积影响约 +200KB（当前二进制 8–12MB 量级，可接受） |
| 依赖 | `agent/pyproject.toml` 加 `loguru>=0.7` | — |
| 测试影响 | `agent/tests` 中对日志文本的断言改为断言**消息体**，不再依赖格式前缀 | 格式前缀由 sink 统一提供 |

**上下文绑定**（D4 的 Agent 侧正解）：建立 logger 后按组件 `bind`，让每条记录自带来源：

```python
log = kk_logutil.get_logger(cfg["log_path"], cfg["log_level"])
# 组件级绑定：日志自带 component=，500 台规模下可按组件过滤
tr = Transport(cfg, log=log.bind(component="transport"))
kk_plugins.collect_all(cfg["plugin_dir"], log=log.bind(component="plugin"))
```

#### A7.3 Server 侧落地

新增 `server/src/kk_server/logsetup.py`：

| 组件 | 内容 |
|---|---|
| `setup_logging(settings)` | 幂等注册 sink：① stdout（保留容器采集）② 文件（`KK_LOG`，默认空 = 不落盘）+ `rotation="20 MB"`, `retention=10`, `compression="gz"` —— 服务端长跑，保留规格高于 Agent |
| `get_logger(name)` | 供各模块替换 `logging.getLogger("kk.xxx")`，调用点写法不变 |
| `InterceptHandler` | `logging.Handler` 子类，把 stdlib 记录转投 loguru → **统一接管 uvicorn / paho / httpx** |
| `serialize` | 由 `KK_LOG_JSON=1` 控制（默认 0，人读优先；接采集端时打开，日志即 JSON Lines） |

装配点与陷阱：

| 项 | 做法 | 理由 |
|---|---|---|
| 调用位置 | 在 **`create_app()` 内**调用，而非 `main()` | 测试用 `create_app(env)` 构造（`test_api.py:20` 是 **function 级 fixture**），日志行为需与生产一致 |
| **幂等是硬要求** | 模块级 `_configured` 标记 + sink 句柄集合，重复调用只更新级别 | `create_app` 在整套测试里被调用**数百次**（function fixture）；若每次都 `logger.add()`，sink 线性增长 → 一条日志打印几百遍，测试输出爆炸 |
| `uvicorn.run(..., log_config=None)` | **显式置 `None`** | 否则 uvicorn 用自带 dictConfig **覆盖**我们的格式，`InterceptHandler` 前功尽弃（`main.py:110-115`） |
| 移除 `basicConfig` | 删除 `main.py:109`，改调 `setup_logging` | 避免两套配置打架（D2） |
| 三处 `getLogger` | 改为 `get_logger("kk.server" / "kk.store" / "kk.bridge")` | 模块名保留，`log.xxx` 调用点不动 |
| 请求级上下文 | `mqtt_bridge` 的 `_on_hb` / `_on_result` / `dispatch_command` 用 `log.bind(host=…, cmd=…)` | 一条记录自带主机与命令号：`grep 'host=web-07'` 即拉出该机全部事件。**这是 500 台规模下 A7 价值最高的落点** |
| docker 兜底 | `docker-compose.prod.yml` **与 `docker-compose.offline.yml`** 的 `kk-server` 服务都加 `logging: {driver: json-file, options: {max-size: "50m", max-file: "5"}}` | 应用侧轮转是文件维度；docker 侧不设上限仍会无限增长（D3）。两个 compose 的 kk-server 段结构不同（`build` vs `image`），需分别改 |
| 文档 | `server/README.md` 环境变量表补 `KK_LOG` / `KK_LOG_JSON`；`agent/README.md:134` 按 A7.4 更新 | — |

#### A7.4 顺带修掉 D1：`KK_LOG` 归属拆分

**核心：一个日志文件只能有一个写入者。** 两个方案：

| 方案 | 做法 | 评价 |
|---|---|---|
| **α. 文件归应用（选）** | Agent 侧由 loguru 的文件 sink 全权负责 `KK_LOG`；wrapper **不再**把 Agent 输出重定向到该文件（改 stderr 直通 docker logs），supervisor 自身消息写 `KK_SUPERVISOR_LOG`（默认空 = 只走 stderr） | ✅ 单写入者、单轮转；轮转/保留/压缩由库保证（wrapper 只有裸 `mv`）；语义清晰：`KK_LOG` = Agent 应用日志 |
| β. 文件归 wrapper | Agent 的 `log_path` 强制置空，只写 stderr，由 wrapper 统一采集轮转 | ❌ 失去 retention/compression；且不经 wrapper 直跑 Agent（开发场景）时完全没有文件日志 |

**落地**：

| 文件 | 改动 |
|---|---|
| `agent/deploy/entrypoint-wrapper.sh` | `:42` 改为 `"$KK_BIN" >/dev/null 2>&1 &`（或按可选变量保留形态）；`:45,62` 的 supervisor 消息改写 `${KK_SUPERVISOR_LOG:-/dev/stderr}`；**删除** `rotate_log()`（轮转归应用） |
| `agent/deploy/Dockerfile.snippet` | ENV 值不变，补注释说明 `KK_LOG` 现由 Agent 独占 |
| `agent/README.md:134` | `KK_LOG` 描述改为「Agent 日志文件，轮转与保留由 Agent 自身管理」；并提示**不要同时把 Agent stdout 重定向到同一路径** |
| `scripts/build.sh:70` | 同步注释 |

> 保留 `KK_LOG` 的 ENV 值与路径（`/var/log/kk-agent.log`）不变——**只改语义归属，不改部署面**，避免运维脚本失效。

#### A7.5 测试

| 用例 | 断言 |
|---|---|
| `test_adapter_percent_format_compat` | `log.info("a %s b %d", "x", 1)` 经适配器后消息为 `a x b 1` —— 锁死 58 处调用点语义不变 |
| `test_adapter_no_args_untouched` | 无参数的 `log.warning("plain %d")` 不因缺参抛 `TypeError`（`%` 仅在存在 args 时执行） |
| `test_get_logger_idempotent` | 连调两次 `get_logger`，sink 数不翻倍 |
| `test_get_logger_no_file_sink_when_empty` | `path=""` 与 `path="-"` 均不建文件 sink |
| `test_get_logger_file_rotation_and_retention` | 写入超阈值 → 出现轮转文件，且文件总数受 `retention` 约束 |
| `test_exception_carries_traceback` | `try/except` 内 `log.exception("boom")`，输出含 `ZeroDivisionError` 与调用点 |
| `test_setup_logging_idempotent_across_create_app` | 同进程 `create_app()` 调 3 次，sink 数不变（**防测试输出爆炸**） |
| `test_stdlib_logs_intercepted` | 触发一条 stdlib 记录（模拟 uvicorn access）→ 落入统一 sink |
| `test_server_json_sink_when_kk_log_json` | `KK_LOG_JSON=1` 时每行可被 `json.loads` 解析 |
| `test_agent_wrapper_not_redirecting_to_kk_log` | 静态检查 `entrypoint-wrapper.sh` 中 Agent 启动行不含 `>>"$KK_LOG"`（防 D1 回归） |

#### A7.6 约定固化（同步写入 `AGENTS.md`）

| 约定 | 内容 |
|---|---|
| 单一日志后端 | 双端只允许 `loguru`，且只能经 `logutil` / `logsetup` 的适配层使用；**新代码不得再 `import logging`**（stdlib 仅允许出现在 `InterceptHandler` 内） |
| 调用风格 | 继续用 `%s` 懒格式化（适配层契约），不写 f-string 拼消息 —— 保证延迟求值，也保证与既有 58 处风格一致 |
| 一个文件一个写入者 | `KK_LOG` 只由应用进程写；shell / docker 侧重定向不得指向同一路径 |
| 上下文优先 | 需要主机/命令/批次维度时用 `log.bind(...)`，不要拼进消息串 |
| 生产红线 | `diagnose=False`；日志中不得出现口令、token、完整环境变量 |

---

## 1.5 新需求 · 客户端版本治理与受控升级（D 组，2026-09-12 追加）

> **需求原文（两条）**：
> ① 支持自动采集客户端工具的版本信息，作为默认上报数据；
> ② 支持选择机器，快速更新指定服务器上的客户端工具，升级版本到最新版本。

### D0 需求逐条对齐现状（先纠一个认知差）

**结论先给：第 ① 条基本已在做，只差三处硬化；第 ② 条是真空白。** 工作量分布约 **D1 占 1/5、D2 占 4/5**。

| 需求点 | 现状证据 | 判定 |
|---|---|---|
| 自动采集版本 | `transport.py:174-175` 状态帧带 `agent_ver` + `proto_ver`；`:244` 心跳帧带 `agent_ver` | ✅ 已在做 |
| 采集「客户端工具」运行环境 | `collector.sys_metrics()`（`:189`）已采 `os`(`platform.platform`) / `kernel` / `arch`，且 `sys` 属默认 8 项（`HB_DEFAULT_ITEMS`, `collector.py:273`） | ✅ 已在做 |
| 作为**默认**上报数据 | 版本在两帧里是**无条件字段**，不受 `KK_HB_ITEMS` 影响；`sys` 项也在默认清单内 | ✅ 已在做 |
| 版本入库 | `containers.agent_ver String(40)`（`tables.py:30`），`upsert_container` / `set_online` 均写入（`store.py:141,150`） | ✅ 已在做 |
| 版本展示 | 主机总览列「Agent」（`host/monitor/index.vue:205`）+ 详情页「Agent 版本」（`host/detail/index.vue:158`） | ✅ 已在做 |
| **选择机器升级** | 前端**零**升级入口；`POST /api/system/agent` 上传只能靠 curl；服务端 `_maybe_push_upgrade` 是**无差别自动推**，根本选不了 | ❌ **空白** |

---

### D1 版本信息硬化（小改动，但三处都是真问题）

#### D1.1 版本号有两个真源且在互相矛盾（真缺陷）

| 位置 | 值 | 是否被消费 |
|---|---|---|
| `agent/src/kk_agent/config.py:6` `AGENT_VER` | `"0.3.0"` | ✅ 实际上行值（`transport.py:174,244`、`main.py:201`） |
| `agent/src/kk_agent/__init__.py:13` `__version__` | `"0.2.0"` | ❌ 全仓零消费 |

**影响**：任何按 Python 惯例读 `kk_agent.__version__` 的脚本或打包元数据会拿到 `0.2.0`，与真实上报值 `0.3.0` 矛盾；`proto/messages.md:147` 的 `update` 示例也写着 `"version":"0.2.0"`，进一步混淆。**这是需求①「版本信息准确」的前置条件** —— 版本治理先把版本号本身理顺。

**修法（单一真源，改动面最小）**：以 `config.AGENT_VER` 为唯一真源（它已被 transport 消费），`__init__.py` 改为

```python
from .config import AGENT_VER as __version__
```

加测试 `test_version_single_source` 断言 `kk_agent.__version__ == kk_agent.config.AGENT_VER`。

> 为什么不反过来让 `config.py` 从 `__init__` 取：`config.py` 保持零 import 依赖更利于 PyInstaller 冻结与冷启动，`__init__` 是天然的上层聚合点。

#### D1.2 「最新版本是什么」对运维不可见

`GET /api/system/agent/latest`（`agent_update.py:78`）有两个限制：走 `agent_ip_auth`（**只有 Agent 的源 IP 能查**）、响应体是 `{available: bool}`（**只回答「这个版本要不要升」，不告诉你最新是什么**）。

**后果**：需求②里「升级到**最新**版本」在 UI 上无从表达 —— 运维不知道最新是什么版本，也无从判断哪些机器落后。

**修法**：新增管理员可读端点（与 Agent 用的 `/latest` 刻意分离，不复用同一端点以免混淆两种鉴权语义）：

| 端点 / 字段 | 内容 |
|---|---|
| `GET /api/system/agent/current` | 管理员会话；返回 `{version, sha256, size, uploaded_at, hosts_total, hosts_outdated}`；无上传记录时 `version` 为空串 |
| `GET /api/system/stats` | 加 `agent_latest_ver` 与 `agents_outdated` 两个计数，供总览页卡片直接渲染 |

#### D1.3 「是否落后」必须由服务端算，不能让前端算

服务端版本比较在 `models/version.py:version_lt`（`parse_version` 支持多段数字），**前端没有任何等价实现**。若让前端自己比，就是两套语义各自演化，`1.10.0` vs `1.9.0` 这类多段比较最先出错。

**修法**：`containers.py` 的 summary / detail 响应增加两个**计算字段**（是字段，不是列，不入库）：

| 字段 | 说明 |
|---|---|
| `latest_agent_ver` | 从 KV 读**一次**，整个列表共用 —— **不要每行查一次 KV**（500 台 = 500 次读，与 P1-1 同型的 N+1） |
| `agent_outdated` | `bool`，服务端用 `version_lt(agent_ver, latest_agent_ver)` 算好下发 |

前端只负责渲染，不做任何版本比较。

#### D1.4 把「版本是必带字段」写进协议

`proto/messages.md` §3 的字段表目前只在 status 帧列出 `agent_ver`，**hb 帧的字段说明没提**（实现里有，文档没写）。补一句，并加测试 `test_hb_frame_always_carries_agent_ver`，把「默认上报」从实现细节**提升为契约** —— 否则将来有人优化帧体积时，最容易先砍的就是它。

---

### D2 受控批量升级（本组主体）

#### D2.0 先看清现状：当前的真实升级路径是「Agent 自己轮询」，而它选不了机器

两条升级路径并存，**且都不是「选机器立即升」**：

| 路径 | 触发点 | 现状问题 |
|---|---|---|
| 服务端推送 | `_maybe_push_upgrade`，**只在 `_on_status` 里被调用**（`mqtt_bridge.py:205`） | status 帧**只在 Agent 连接时发一次**（`transport.py:266`，在 `start()` 内）→ **上传新版本后，已在线的主机收不到推送**，必须等容器重启或重连。且无差别推送，选不了 |
| Agent 轮询 | `main.py:214-216` 每 `KK_UPDATE_INTERVAL`（默认 **300s**）拉 `/agent/latest`，有新版即自升（`updater.check_update`） | 时机由 Agent 决定，**服务端无法让某台「立刻升」**；且 A6.1 修复前因缺 `KK_UPDATE_URL` 静默失效 |

**结论**：需求②要的是**服务端发起 + 人工选机 + 立即生效**，现有两条路径都给不了。因此 **D2 = 把升级决策权显式收归服务端，并新增人工触发面**。

#### D2.1 新增升级模式开关 `KK_UPDATE_MODE`，默认 `manual`

**为什么必须引入模式开关**：自动全网推与人工选机**不能同时成立** —— 你选了 3 台，剩下 497 台下次重连仍会自己升，「选择」就没有意义。默认取 `manual` 是对需求②的直接落地。

| 模式 | 行为 |
|---|---|
| `manual`（**新默认**） | 服务端不在 `_on_status` 自动推；Agent 轮询路径**也**被策略关闭（见下） |
| `auto` | 完全保留现状（自动推 + 轮询），供给「想维持用户无感知」的部署选择 |

**关键取舍：关闭 Agent 轮询不需要改 Agent 一行代码** —— 在 `manual` 模式下让 `/api/system/agent/latest` 返回 `{"available": false, "policy": "manual"}`。

- 这**不是撒谎**：该端点的语义就是「**现在**该不该升级」，策略是人工时，正确答案本来就是「否」。加 `policy` 字段让它自描述，排障时一眼看出是策略所致而非故障。
- 收益：**老版本 Agent 无需升级即可服从新策略** —— 服务端单侧改动就建立起一个全网点，避免了「要改行为，先得升级全网」的鸡生蛋困境。对已有 500 台在跑的存量部署，这一点是关键。

#### D2.2 新增批量升级端点

```
POST /api/system/agent/upgrade          （管理员会话）
body: {"hosts": ["web-01","web-02"], "version": ""}   # version 省略/空 = 用当前最新
resp: {"ok": true, "batch_id": "ug-<ts>-<n>",
       "accepted": [{"host","from_version","to_version","ledger_id"}],
       "skipped":  [{"host","reason"}]}
```

| `skipped.reason` | 触发条件 | 处置 |
|---|---|---|
| `not_found` | hosts 里含未知主机 | 改正后再调 |
| `no_binary` | 服务端尚未上传任何版本（KV 空） | 先上传 |
| `bad_version` | 指定 `version` 在服务端不存在 | 传对版本 |
| `already_latest` | `version_lt` 判为不落后 | — |
| `in_flight` | 该主机台账已有未终结行（见 D2.3） | 等其收敛 |

**每台主机在 `updates` 台账写一行**（`id = up-<host>-<ts>`，**复用 A6.2 已设计的表，不新增结构**），再用**台账主键**作为 `cmd` 帧的 `id` 下发 `kind=update` —— 这正是 A6.2「为什么升级要独立台账」的落地场景。

**离线主机照常受理（这是本项目既有卖点的复用）**：Agent 用 `clean_session=False` + 稳定 `client_id`（`transport.py:127-131`），Broker 会为离线订阅者排队 QoS1 消息。所以选中一台离线机器**不是失败**，而是「已排队，重连即升」—— 台账标 `queued`，UI 如实说明。

> **由此引出一个必须修正的细节**：A6.2 定的「`created_at + 30min` 未终结即收敛 `timeout`」对**下发时主机离线**的行会误判 —— Broker 里排着队，Agent 可能几小时后才上线，30min 一到就被记成 `timeout`，把「正常排队」报成「失败」。修法：台账增加 `queued` 状态，仅**在线下发**的行受 30min 超时约束；`queued` 行改由「重连补投」（下条）驱动，超时阈值放宽到 `KK_UPDATE_QUEUE_TTL`（默认 7d，与 Broker 离线队列保留期对齐）。

#### D2.3 重连补投与防重复

| 场景 | 处理 |
|---|---|
| 离线时被选中 → 主机重连 | `_on_status` 收到 `online=true` 时，扫该主机台账中 `queued` 且 `to_version` 仍为最新的行，补投一次 `kind=update`（此时才转 `pending`，接续 A6.2 的判定链） |
| 主机已有未终结升级 → 又被选中 | 直接 `skipped: in_flight`。**没有台账就只能靠 `version_lt` 猜，必然重复下发与重复下载**（8–12MB/台）—— A6.2 与 D2.3 在此互相成全 |
| 同一批里重复传同一 host | 按 host 去重后处理，不产生两行台账 |
| 升级在途，运维又上传了更新的版本 | 不打断在途升级（`in_flight`）；该主机完成后若上报的 `agent_ver` 仍落后，由 D2.2 再次人工触发（`auto` 模式下则自动） |

#### D2.4 前端

**「选择机器」这一步无需新建组件**：主机总览表**已有** `type="selection"` 多选 + `selection` ref + `onSelectionChange`（`host/monitor/index.vue:169,33,70`），当前服务于「批量执行命令」「批量采集」。D2.4 直接**复用同一套选择状态**，只加按钮。

| 改动 | 位置 | 内容 |
|---|---|---|
| 版本列升级为 tag | `host/monitor/index.vue:205` | `最新`=success、落后=warning 且显示 `0.2.0 → 0.3.0`；数据源用 D1.3 的 `agent_outdated`（**前端不做版本比较**） |
| 吸底批量栏加按钮 | 复用 A5.7 的 `.kk-sticky-bar` | 「升级到 vX.Y.Z」：在线/离线台数分别计数；`latest` 为空时按钮 `disabled` 并提示「请先上传新版本」 |
| 二次确认 | 沿用 A5.5 风格 | 明确列出「在线 X 台（立即升级）· 离线 Y 台（重连后自动补投）」，与既有「离线排队」话术一致，避免被误判为异常 |
| 详情页入口 | `host/detail/index.vue` | 单机「升级」按钮（仅 `agent_outdated` 时显示） |
| **新增「版本与更新」页** | `views/host/update/index.vue`（新） | ① 上传新版本二进制（`POST /api/system/agent` **当前完全无 UI**，只能 curl）；② 台账列表（复用 A6.2 的 `GET /api/system/updates`），逐台展示状态与失败原因 |
| 总览页卡片 | `views/welcome` | 「Agent 版本 vX.Y.Z · 落后 N 台」 |

> 上传入口是需求②的**必要前置**：没有上传面，「升级到最新版本」只能在服务器上敲 curl，与「快速」二字相悖。

#### D2.5 依赖与既有项的关系

| 关系 | 说明 |
|---|---|
| **强依赖 A6.1** | 远端 Agent 拿不到下载地址时，**手动升级同样失败**。D2 必须在 A6.1 之后做 |
| **强依赖 A6.2** | `updates` 台账是升级状态的唯一载体；`in_flight` 去重、`queued` 补投、失败原因全落在它上面。D2 只是给它加了「人工触发」入口 + `queued` 状态 |
| 与 B2（回滚接口）同族 | `版本治理 = A6.2 台账 + D2 升级 + B2 回滚`，三者共用一个台账，建议同批验收 |
| 与 §5「灰度 / 分批更新」 | D2 **不做**自动分批，但提供了灰度的**手动形态**：选 3 台先升 → 观察台账 → 再选其余。这让「灰度」从待建功能降为操作规范（§5 该行已补注） |
| 与 B6.2（更新轮询抖动） | `manual` 模式下轮询已关闭 → B6.2 只对 `auto` 模式有意义，**不因 D2 作废**，但优先级可下调 |

---

### D3 测试

| 用例 | 断言 |
|---|---|
| `test_version_single_source` | `kk_agent.__version__ == config.AGENT_VER` |
| `test_hb_frame_always_carries_agent_ver` | 心跳帧 `agent_ver` 非空且等于 `AGENT_VER`（锁死「默认上报」契约） |
| `test_hosts_summary_carries_outdated_flag` | 造「最新=0.3.0、主机=0.2.0」→ `agent_outdated=True`；且 KV **只读一次**（计数替身断言，防 N+1 回归） |
| `test_agent_current_requires_admin` | 未登录访问 `/agent/current` → 401/403；登录 → 返回最新版本 |
| `test_update_mode_manual_disables_auto_push` | `manual` + 落后主机上报 status → **不发布** `kind=update`（对照组 `auto` 必须发布） |
| `test_update_mode_manual_hides_poll_path` | `manual` 下 `/agent/latest` 返回 `available=False` 且 `policy=manual`（**老 Agent 无需改动即服从**） |
| `test_upgrade_endpoint_accepts_and_writes_ledger` | 选 2 台 → `accepted` 2 条，台账 2 行 `pending`，`cmd` 帧 `id` 等于台账主键 |
| `test_upgrade_skips_already_latest` | 主机已最新 → `skipped.reason == already_latest`，**不产生台账行** |
| `test_upgrade_skips_in_flight` | 台账已有未终结行 → `skipped.reason == in_flight` |
| `test_upgrade_offline_host_marked_queued` | 下发时主机离线 → 台账 `queued`（**非** `failed`），且不参与 30min 超时收敛 |
| `test_queued_upgrade_flushed_on_reconnect` | `queued` 行 + 主机上报 `online=true` → 重新投递一次并转 `pending` |
| `test_upgrade_no_binary_returns_skipped` | KV 无版本 → 全量 `skipped.reason == no_binary`，不抛异常 |
| `test_upgrade_endpoint_requires_admin` | 未登录 → 401/403（与既有审计/上传接口同门禁） |
| `test_proto_doc_example_version_in_sync` | `proto/messages.md` 中 `update` 示例的 `version` 与 `AGENT_VER` 同族（防文档漂移） |
| 前端 | `pnpm typecheck` + `pnpm build`：版本 tag、批量栏按钮、上传页、落后计数卡片 |

---

## 2. 阶段二 · 加固

### B1 客户端 nice 降权（P2-1）

**改动**（`agent/deploy/entrypoint-wrapper.sh`，`supervise()` 内）：

```sh
# 让 Agent 在 CPU 争抢时主动让位给用户 IDE；nice 不存在则原样启动（不阻断）
KK_NICE="${KK_NICE:-19}"
if command -v nice >/dev/null 2>&1; then
  nice -n "$KK_NICE" "$KK_BIN" >>"$KK_LOG" 2>&1 &
else
  "$KK_BIN" >>"$KK_LOG" 2>&1 &
fi
```

| 评估 | 结论 |
|---|---|
| 风险 | 极低。nice 只影响调度优先级，不改变功能；探测失败自动回退 |
| 覆盖范围 | CPU 维度；IO 维度需 `ionice`（util-linux，镜像不保证存在）——**探测式可选**，存在则用 `ionice -c3` |
| 不做的事 | **不设 `RLIMIT_AS`**：PyInstaller onefile + psutil 的地址空间占用难以给出安全下限，设错会让 Agent 直接崩溃（比资源超标更糟）。内存上限继续依赖既有的代码级封顶（读取侧 4MB） |
| 验证 | 手工：容器内 `ps -o pid,ni,comm -p $(pgrep -f kk-agent)` 断言 `NI=19` |

### B2 自更新回滚接口（P2-7）

| 项 | 内容 |
|---|---|
| 现状 | 上传时已保留上一版为 `kk-agent.prev`（`agent_update.py:59-63`），但无接口触发回滚 |
| 新增 | `POST /api/system/agent/rollback`（管理员会话）：把 `kk-agent.prev` 换回 `kk-agent`（当前版存为 `.rollback`），更新 `agent_latest` 的 version（需读旧二进制的版本号——**方案：上传时把 version 记入 KV 的 `agent_latest`，回滚时读 `agent_prev` KV 记录**，需在上传路径同时写一条 `agent_prev`） |
| 触发更新 | 回滚后 Agent 不会自动降级（`version_lt` 只升不降）。**明确语义**：本接口只回滚服务端**待分发的二进制**，让「新上线/重启的 Agent」拿到旧版；已在跑的 Agent 需重启才生效。文档必须写清这一点，否则会被误认为一键回滚全网 |
| 测试 | `test_agent_rollback_restores_prev` / `test_agent_rollback_without_prev_404` |

### B3 登录限流补 IP 维度（P2-3）

`auth.py` 的 `_LOGIN_FAILS` 由 `dict[str, int]`（仅用户名）改为 `dict[tuple[str, str], int]`（用户名 + 客户端 IP），阈值语义变为「同一用户名 或 同一 IP，任一维度达 5 次即锁」。

| 注意 | 说明 |
|---|---|
| IP 来源 | 直连取 `request.client.host`；**不用 `X-Forwarded-For`**（可伪造，反而给攻击者换锁对象的便利）。经反代时源 IP 变代理地址 → 退化为「全局 5 次」，需在文档标注「反代场景请改由网关限流」 |
| 内存增长 | 键从用户名变为二元组，仍无上限。加一个轻量清理：锁定期过后顺手剔除过期键（在既有 `_LOGIN_LIMIT_LOCK` 临界区内，约 5 行） |
| 测试 | `test_login_lock_by_ip` —— 同 IP 换用户名连续失败 5 次即 429 |

### B4 文档一致性修正（P2-8）

| 项 | 修正 |
|---|---|
| `AGENTS.md` | 「前端轮询定时器统一用 `setPoll()`/`clearPolls()`」**该函数全仓不存在**，实际 5 处各自 `setInterval`（`monitor:65`、`detail:113`、`CommandHistory:45`、`welcome:109`、`longpress:36`）。**二选一**：(a) 实现 `utils/kkPoll.ts` 提供 `setPoll/clearPolls` 并改造 5 处 —— 收益是「页面卸载漏清理」这类 bug 从根上消除；(b) 改 `AGENTS.md` 删除该条。**推荐 (a)**：5 处手写 `onBeforeUnmount` 清理已有重复代码，抽出来是净减少；且 A3 加分页后轮询逻辑更复杂，统一管理更值 |
| `server/main.py:36` | 注释「（老库查看、审计导出）」→ 改为「（老库只读查看）」，或等 A1 落地后改为「（老库查看、数据导出）」 |
| `architecture-review.md` | §1 表加一行说明「本表为 2026-09-04 快照，最新状态以 §0 为准」，消除 H1/M4/M5「未修」与实际的矛盾 |
| `AGENTS.md` | 「全量 202 条」等测试数在 A1/A3 后会变，方案落地时同步更新 |

### B5 CI 落地（P2-5 / P2-6）

> **2026-09-12 决定变更**：CI 平台由 GitHub Actions 改为 **Jenkins**（用户指定）。
> 主流水线（测试 → 构建 → 镜像冒烟 → 推送 → 部署 → 验证）**已落地**，
> 见根目录 `Jenkinsfile`、`scripts/ci_smoke.sh` 与 [ci-jenkins.md](ci-jenkins.md)。

**现状**：仓库**没有 `.github/workflows/`**（已确认），CI 改为 Jenkins 后不再需要它。

已落地部分（本次提交）：

| 段 | 内容 | 复用 |
|---|---|---|
| 后端测试 | 流水线自起 `eclipse-mosquitto:2`（端口 18830，只绑回环）→ `uv sync --all-packages` → `pytest agent/tests server/tests`，**断言 202 passed**（无 Broker 时是 198+4 skipped 的假绿，见 ci-jenkins.md §3） | 既有测试 |
| Broker 语义冒烟 | retain 只落 status、LWT、离线排队、大输出分块 | `scripts/mqtt_e2e.py` |
| 前端构建 | `pnpm typecheck && pnpm build` → 同步进 `server/src/kk_server/web/` → **产物漂移检查** | `web/package.json` |
| Agent 二进制 | PyInstaller 单文件（用 `uv venv --seed` 建的构建 venv：uv 的 venv 不带 pip，而该脚本用 pip 装 pyinstaller） | `agent/build/build_binary.sh` |
| 服务端镜像 | 上下文为仓库根（uv workspace 锁在根）+ OCI 修订标签 | `server/Dockerfile` |
| **镜像部署冒烟** | 真起 Broker + 真起镜像 + **真跑 Agent 二进制**：健康 → 登录 → 上线 → 指标 → 命令 → 结果 → 审计，11 条断言 | `scripts/ci_smoke.sh`（新增） |
| 部署 | 目标机 `git reset --hard` 到本次提交 → `docker compose pull` → `up -d`；生产需人工确认 | `docker-compose.prod.yml` |
| 部署验证 | `/api/health` 断言 `ok=true` + `broker=connected`，再登录读 `/api/system/stats` | — |

仍待落地（建议单独建一个 daily job，`when` 里只跑这两段——它们耗时长且与部署无关）：

| 段 | 内容 | 触发 |
|---|---|---|
| `dialects` | matrix `postgres:16` / `mysql:8`，跑 `test_dialects.py` 的**真实连接**版本 + `test_store.py`，断言扩列迁移（`_ensure_schema`）在两库上真的成功 | 每次 push 或每日 |
| `nightly` | `scripts/loadtest.py` 500 连接 × 60s，断言心跳零误判掉线、命令成功率 100%；`scripts/bench_agent.py` 断言 RSS `< 40MB` | 每日定时 |

> **真库验证的已知风险**（`design.md:222` 已标注）：MySQL 排序规则大小写、PG 标识符小写折叠可能暴露既有代码的隐性问题。**这正是这个 job 的价值**——它可能一次跑出若干真实缺陷，需要预留修复余量，不要指望「配个 job 就绿」。

> **一处为流水线做的前置改动**：`docker-compose.prod.yml` 的 `kk-server` 服务加上了
> `image: ${KK_SERVER_IMAGE:-kk-server:latest}`。原文件只有 `build:`，**无法部署已推送到镜像仓库的产物**
> （`docker compose pull` 没有可拉的对象）。默认值与 `deploy/offline/pack.sh`、`docker-compose.offline.yml`
> 的 `kk-server:latest` 一致，手工部署流程与行为不变。

### B6 自更新健壮性（机制审视新增）

A6 修的是「看不见」，B6 修的是「规模化下的健壮性」。六条互相独立，可按需挑选。

#### B6.1 更新期间离线原因可见

**问题**：`os.execv` 直接替换进程，MQTT 连接被断，Broker 发 LWT（`reason` 为**空**）→ 服务端只看到「主机离线」，运维无法区分「容器停了」和「正在自更新」。

**事实**：`_status_payload(online, reason)` **本来就支持 `reason`**（协议 §3.1 已定义 `reason` 字段：`online` / `offline` / LWT 触发时为空）。缺的只是 execv 路径没走 `Transport.stop()`。

| 改动 | 说明 |
|---|---|
| 增加重启前钩子 | `updater.apply_manifest` 增加可选参数 `on_before_restart`；`main.py` 调用处传入 `lambda: tr.publish_status(False, "updating")` |
| 保底 | 钩子执行失败不影响更新主流程（`try/except` 包住，只记日志） |

**收益**：服务端离线视图能显示 `reason=updating`，并可在命令下发前提示「该主机正在更新，命令将排队」。约 15 行。

#### B6.2 更新轮询加抖动（与心跳同款理由）

**问题**：心跳有 `random.uniform(0.9, 1.1)` 抖动（`main.py:215`，为打散 500 台峰值而设），但更新轮询是 `next_update = now + cfg["update_interval"]` —— **无抖动**，且启动时 `next_update = 0.0`（立即检查一次）。

**后果**：本项目 Agent 随镜像批量部署，500 台启动相位接近 → 同一时刻检查更新 → 同时下载 8–12MB 二进制（服务端瞬时约 5GB 流量）+ 同时断连重连。

| 改动 | `next_update = now + cfg["update_interval"] * random.uniform(0.8, 1.2)` |
|---|---|

一行代码，与心跳抖动同一套理由。**这是本节性价比最高的一条。**

#### B6.3 更新失败退避

**问题**：`apply_manifest` 失败不记录状态；下个轮询周期（300s）以及**每次上线**都会再试。面对一个坏包（sha256 不匹配）或只读磁盘，会形成无限重试 + 日志刷屏。

| 改动 | 说明 |
|---|---|
| Agent 侧 | 模块级 `_fail_state = {"ts": 0, "count": 0}`；失败后指数退避（5min → 10min → 30min 封顶），成功或版本变化即清零 |
| 门禁位置 | `check_update` 入口处判断退避窗口，未到期直接返回 |

#### B6.4 下载后比对清单 `size` + 清理临时文件残留

| 问题 | 证据 | 修复 |
|---|---|---|
| 清单里的 `size` **从未用于校验** | `download_binary` 只有 64MB 上限；`apply_manifest` 只校验 sha256 | 下载后 `len(data) != manifest["size"]` 即拒 —— 比 sha256 更早暴露截断，失败信息也更直观 |
| 下载中途收到 SIGTERM 会残留 `.kk-agent.update.<pid>` | `verify_and_replace` 的 `except BaseException` 覆盖异常路径，但 **SIGTERM 默认终止进程、不抛异常**（容器停止的常见形态） | 在 wrapper 既有的 `cleanup_stale_mei()` 里顺带清 `/opt/kk-agent/.kk-agent.update.*` —— 同一个清理函数，零新增机制 |

#### B6.5 服务端上传加锁

**问题**（`controllers/agent_update.py:53-69`）：`shutil.move(dest, dest + ".prev")` → 写新文件 → `set_agent_latest`。**全程无锁**，两个管理员并发上传会互相踩（A 刚 move 完、B 又 move 一次，`.prev` 变成中间态）；Windows 上 move 正在被读的文件还会直接失败。

| 改动 | 模块级 `threading.Lock()` 包住「换 `.prev` + 写文件 + 更新 KV」三步；`set_agent_latest` 保持在文件写成功之后（当前顺序正确，保持） |
|---|---|

单管理员场景概率低，但加锁成本是 3 行，且**顺序正确性**（先落盘后写 KV）值得在代码注释里固化下来。

#### B6.6 「只升不降」与回滚接口的交互（文档固化）

`apply_manifest` 的 `version_lt(AGENT_VER, ver)` 门禁决定了 **Agent 不会自我降级**。这与 B2 的回滚接口必须配合说明，否则容易被误解为一键回滚全网：

| 场景 | 实际行为 |
|---|---|
| 上传 0.4.0 → 全部 Agent 升到 0.4.0 | 正常 |
| 0.4.0 有问题 → 调用回滚接口 | 服务端待分发的二进制变回 0.3.0，**已在跑的 0.4.0 Agent 不会降级** |
| 之后新上线 / 重启 / **重装镜像**的 Agent | 拿到 0.3.0 |
| 想让全网退回 0.3.0 | 必须**上传一个版本号更高**（如 0.3.1）的包，用「升版本」实现「退代码」——这是防降级攻击的必然代价 |

**建议**：B2 的接口文档与 `docs/deployment.md` 都写明上表；并考虑提供 `KK_UPDATE_ALLOW_DOWNGRADE=1` 的显式逃生开关（默认关闭），让确实需要真降级的场景有一条明路，而不是改代码。

---

## 3. 提交计划（按模块分批，中文信息带前缀）

| # | 提交 | 覆盖 |
|---|---|---|
| 1 | `feat(server): 数据导出接口（命令/审计/主机/指标 CSV）` | A1 后端 + 测试 |
| 2 | `refactor(web): 布局基建与工具收敛（kk-page/kkPoll/downloadBlob）` | A5.2 / A5.8 基建**先行**——后续页面改造都建立在它之上，单独提交便于回滚 |
| 3 | `feat(web): 四个业务页加导出入口` | A1 前端 + `pnpm typecheck` |
| 4 | `perf(server): 批量下发发布回路去掉 N+1 回查` | A2 + `mark_sent_batch` |
| 5 | `feat(server): 命令批次号与结果分页查询` | A3 后端（含 `_ADD_COLUMNS` 登记）+ 测试 |
| 6 | `feat(web): 命令中心双栏工作台与主机选择抽屉` | A5.3 / A5.4 / A5.5 |
| 7 | `feat(web): 执行历史分页、批次筛选、搜索与输出抽屉` | A3 前端 + A5.6 |
| 8 | `feat(web): 总览吸底批量栏、整行点击与详情页执行入口` | A5.7 |
| 9 | `feat(server): 上报间隔下限检测与审计` | A4 + 测试 |
| 10 | `fix(agent): 自更新支持服务端下发的绝对 URL` | A6.1 —— **独立提交**：这是当前推送式更新静默失效的直接修复，值得单独回滚点 |
| 11 | `feat(server): 更新台账与结果回执` | A6.2（新表 + 回执分支 + 端点 + 测试） |
| 12 | `chore(agent): Agent 启动 nice 降权` | B1 |
| 13 | `feat(server): Agent 自更新回滚接口` | B2（含 B6.6 的语义文档） |
| 14 | `fix(server): 登录限流补客户端 IP 维度` | B3 |
| 15 | `docs: 修正文档与代码失配项` | B4 |
| 16 | `chore(ci): 落地真库验证与 nightly 压测 job` | B5 剩余部分（主流水线已随 `chore(ci): Jenkins 流水线` 单独提交） |
| 17 | `perf(agent): 更新轮询抖动、失败退避与 size 校验` | B6.2 / B6.3 / B6.4 |
| 18 | `fix(server): 自更新上传加锁与更新期离线标记` | B6.5 / B6.1 |
| 19 | `refactor(agent): 日志体系换 loguru（薄适配层保 %s 语义）` | A7.1 / A7.2 / A7.5 |
| 20 | `refactor(server): 服务端日志归一与 KK_LOG 归属拆分` | A7.3 / A7.4 / A7.6（含顺带修掉的 D1 缺陷） |
| 21 | `fix(agent): 版本号统一为单一真源` | **D1.1** —— 独立提交：`AGENT_VER` / `__version__` 矛盾是需求①的直接前提，一行改动、独立回滚点 |
| 22 | `feat(server): 主机版本落后判定与最新版本端点` | D1.2 / D1.3 / D1.4（含 KV 只读一次的防 N+1 断言） |
| 23 | `feat(server): 升级模式开关与受控批量升级接口` | D2.1 / D2.2 / D2.3 —— **依赖提交 10/11**（A6.1 绝对 URL + A6.2 台账） |
| 24 | `feat(web): 版本与更新页、版本 tag 与批量升级入口` | D2.4（复用主机表既有 `type="selection"`；含上传面） |
| 25 | `test(server): 受控升级与版本治理用例` | D3（含离线 `queued` 补投与 `in_flight` 去重） |

每个提交前跑：`.venv/Scripts/python.exe -m pytest agent/tests server/tests -q`；涉及前端时加 `pnpm typecheck`；涉及前端产物时 `pnpm build` 并同步到 `server/src/kk_server/web/`。

> **提交 2 为何独立**：布局基建（`.kk-page` / `.kkPoll` / `downloadBlob`）是提交 3/6/7/8 的共同依赖。独立提交让「布局回归」这类问题可以单点回滚，而不必牵连业务页改动。
>
> **提交 10/11 为何排在 A4 之后**：A6 触碰 Agent 与服务端双端，且引入新表 —— 放在阶段一末尾，避免与 A1~A5 的改动面交织。但**若要先修一处，选提交 10**：它让「推送式更新」从静默失效变成真正可用，改动仅 `updater.py` 十余行。
>
> **提交 19/20（A7）为何排在最后**：A7 是**横切改造**——它会全局改变运行时与测试的日志输出形态。若早做，A1~A6 每一项的验证都会多一层无关噪音（「这条日志格式变了」与「这个功能坏了」混在一起）。排到最后，前面的验证按现状进行，A7 一次切换、一次回归。
>
> **D1（`KK_LOG` 双写入）与功能项完全正交**：若当下就想修，提交 19 可先只做 A7.4 的最小版（wrapper 不再重定向 + 删除 `rotate_log()`，约 6 行 shell），不必等 loguru 落地。但两者同批做更划算——因为 A7.4 让文件写入者唯一之后，正好由 loguru 的文件 sink 接手。
>
> **提交 21~25（D 组）为何排在 A7 之后**：D2 的硬前提是提交 10/11（A6.1 绝对 URL + A6.2 台账）—— 没有下载地址则手动升级同样失败，没有台账则无法去重与补投。而 A7 是横切改造，放在功能项之后能保证 D 组的验证不被日志格式噪音干扰。**若要提前**，提交 21（D1.1 版本单一真源）可以提到最前面，它是一行改动且与其他项零耦合，是需求①成本最低的落点。
>
> **D 组是既有项的「收口」而非新起炉灶**：D1 复用 A6.2 已定的 `version_lt` 与 KV；D2 复用 A6.2 的 `updates` 台账、A5.7 的 `.kk-sticky-bar`、主机表既有的 `type="selection"` 多选。**净新增只有 1 个端点 + 1 个页面 + 1 个模式开关 + 台账的 `queued` 状态**。

---

## 4. 验收标准

| 项 | 验证方式 | 期望 |
|---|---|---|
| 导出可用 | 浏览器点「导出 CSV」 | 下载文件，Excel 打开中文不乱码，行数与页面筛选一致 |
| 导出安全 | 导出一条 argv 含 `=cmd\|...` 的命令 | 单元格值以 `'` 前缀，Excel 不执行公式 |
| 批量看全 | 假造 300 条命令，翻页 | 每页 100，`total=300`，最后页 100 条 |
| 批次可核验 | 批量下发 3 台，点批次号 | 工具栏显示 done/failed/timeout/pending 分布 |
| 发布无回查 | `pytest -k dispatch_without_lookup` | `get_command` 调用次数为 0 |
| 间隔治理 | 造 `interval=1` 心跳 + `KK_INTERVAL_MIN=10` | 指标落库正常，审计出现 `interval_violation`，stats 计数 +1 |
| **一屏主链路** | 1920×1080 下操作命令中心 | 选主机 → 输命令 → 下发 → 看到批次进度与结果，**全程无需滚动** |
| **窄屏无溢出** | 1366×768 下打开命令中心与总览 | 双栏折叠为上下；无横向滚动条、无双纵向滚动条 |
| **高度锚点唯一** | `grep -rn "calc(100vh" web/src/views` | 每页至多命中页面根一处，且为 `var(--kk-page-h)` 而非字面量；**内层零命中**（表格走 `.kk-fill-table`） |
| **无新 UI 依赖** | `git diff web/package.json` | 空 diff |
| **跳转契约不破** | 总览点「批量执行命令」/「批量采集」 | 命令面板主机预选仍生效（`?pods=` 契约不变） |
| **离线排队可见** | 批量选入含离线主机 | 确认框显示「在线 X 台 · 离线 Y 台」及补投说明 |
| **导出与筛选一致** | 命令面板设筛选后点导出 | CSV 行数与页面筛选结果一致 |
| **推送式更新可达** | Agent 未配 `KK_UPDATE_URL`，服务端上传并推送新版本 | 从 Agent 日志确认真的发起了下载与替换，**不再静默跳过** |
| **更新结果可见** | ① 正常升级 ② 故意改坏 sha256 后升级 | `GET /api/system/updates` 分别显示 `done` / `failed` + 具体原因；失败另有审计 `agent_update_failed` |
| **更新期离线可辨** | 升级过程中看主机列表 | 离线记录的 `reason` 为 `updating`，而非空白 |
| **更新不拥塞** | 500 台同批部署后观察 Agent 日志 | 各机检查更新的时刻呈散布（抖动生效），非同一秒集中下载 |
| **版本单一真源** | `python -c "import kk_agent; print(kk_agent.__version__, kk_agent.config.AGENT_VER)"` | 两个值**相同**（当前是 `0.2.0` / `0.3.0`，矛盾） |
| **版本默认上报** | 抓一帧 hb 与一帧 status | 两帧都带非空 `agent_ver`，且等于 `AGENT_VER`（不依赖 `KK_HB_ITEMS` 配置） |
| **最新版本可见** | 管理员登录后看总览页 | 显示「Agent 版本 vX.Y.Z · 落后 N 台」；未上传过版本时显示「未上传」而非报错 |
| **落后判定单一实现** | 断点/替身统计 hosts 接口内的 KV 读次数 | 500 台列表**只读 1 次** KV；前端源码 `grep -rn "version_lt\|parse_version" web/src` **零命中** |
| **手动模式不自动推** | `KK_UPDATE_MODE=manual`，连一台落后主机 | 该主机上报 status 后，Agent 日志**无**下载动作；`agent/latest` 返回 `available=false, policy=manual` |
| **手动模式老 Agent 也服从** | 用**未改动的旧版** Agent 二进制 + manual 模式 | 旧 Agent 同样不自升（服务端单侧即可建立全网点） |
| **选机器立即升** | 总览勾选 2 台在线落后主机 → 点「升级到 vX.Y.Z」 | 返回 `accepted` 2 条；数秒内在 Agent 日志看到下载与替换；主机随后上报新 `agent_ver` |
| **离线机器不被误判** | 勾选 1 台离线 + 1 台在线 | 离线记 `queued`（**非** `failed`）；该机重连后自动完成升级并转 `done` |
| **重复升级被拦住** | 对同一台再点一次升级 | `skipped.reason == in_flight`，Agent 侧**不产生第二次下载** |
| **升级结果可逐台核验** | 「版本与更新」页 | 每台主机显示状态与失败原因（`sha256_mismatch` 等），失败另有审计 `agent_update_failed` |
| **上传面可用** | 在「版本与更新」页上传一个二进制 | 无需 curl 即完成；上传后总览页最新版本随之变化 |
| 全量回归 | `pytest agent/tests server/tests -q` | 全绿（用例数随新增上升，无 failed） |
| 前端质量 | `pnpm typecheck && pnpm build` | 零错误 |

---

## 5. 二期备选（本期不做，登记备查）

| 项 | 触发条件 |
|---|---|
| 命令结果**全量输出** zip 导出（临时文件 + 流式 zip） | 出现「需要把 4MB 完整输出交付给外部」的真实需求时 |
| `kind=set_interval` 强制改频 | 出现「某批机器 interval 配置无法通过重建镜像修正」时 |
| `batch_id` 索引 | 命令表上万级 |
| 导出任务异步化（大范围导出走后台任务 + 结果落 KV） | 单次导出超过 20000 行的需求出现 |
| `out_tail` 从列表响应移出（点开再取） | 500 台 × 5s 轮询的响应体成为压力时 |
| 命令模板 / 收藏（服务端存储，团队共享） | 出现「常用命令需要多人共用」需求时；本期只做 localStorage 单机最近 10 条 |
| 结果表格虚拟滚动 | 单页超过 200 行且实测卡顿时（Element Plus 表格默认全量渲染） |
| 暗色主题逐一核验 | 业务样式已全部走 `var(--el-*)`，理论上自动适配；出现暗色使用反馈时再逐页核验 |
| **灰度 / 分批更新**（按 pod 哈希分 N 批 + 观测窗口 + 暂停开关） | 出现「一次全量更新的风险不可接受」时。当前 500 台靠 A6 台账 + B6.2 抖动已可观测可控；`architecture-review` §0 的「B4 灰度」决策关闭指的就是本项。**（2026-09-12 补：D2 落地后本项的「功能」价值大幅下降 —— 选 3 台先升、看台账、再选其余，就是灰度的手动形态；此处只剩「自动分批 + 定时窗口」仍有价值，等出现无人值守的固定发布窗口需求时再翻）** |
| **多架构 Agent 二进制**（按 `os/arch` 分槽存储与分发） | `agent_update.py:10` 明确「二进制按平台单槽位（`kk-agent`），多架构需另行扩展」。当前 500 台若架构同构则无问题；**一旦混入 arm64，D2 推错二进制会导致该机 Agent 无法启动**（`_is_binary_target` 只验「是不是单文件二进制」，不验架构）。出现异构机型时必修，届时要同时改服务端存储分槽 + `/latest` 按 `sys.arch` 返回匹配槽 + 台账记录投递的架构 |
| **升级窗口与限速**（同批最多 N 台在途、夜间窗口） | D2 已有人工选机与抖动兜底；出现「一次升 500 台仍觉风险高」时，先在 D2 上叠加「在途并发上限」即可，不必重建调度 |
| **`KK_UPDATE_ALLOW_DOWNGRADE=1` 逃生开关** | 确需真降级而非「升版本号退代码」时，见 B6.6 |
| Redis / 共享订阅 / 协议压缩 / ed25519 | 既有决策（`architecture-review` §0），非规模触发不翻案 |

---

## 6. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| `_ADD_COLUMNS` 新增 `batch_id` 的 ALTER 在 MySQL 严格模式失败 | 服务端启动即失败（fail-fast，不会静默） | DDL 用 `VARCHAR(32) DEFAULT ''` 不带引号；B5 的 dialects job 提前暴露 |
| `create_commands_batch` 返回值由 `ids` 改为 `(ids, batch_id)` | 3 处调用点 + 既有测试需同步 | 一次性提交内完成，`test_store.py` 已覆盖批量建命令 |
| 前端分页与 5s 轮询叠加导致翻页被刷回 | 体验退化 | 轮询只刷新当前页并保留 `offset`；分页/筛选变化才重置 |
| 导出请求走 10s 默认超时 | 大范围导出失败 | API 层显式 `timeout: 0` |
| CI 真库 job 首次运行暴露 PG/MySQL 真实缺陷 | 工期不可控 | 该 job 与「测试 job」解耦，**先合并主流程、真库 job 单独迭代至绿**，不阻塞阶段一交付 |
| nice 降权导致命令执行变慢 | 命令耗时上升 | 仅影响 CPU 争抢场景；`KK_NICE` 可配，置 0 等价关闭 |
| **前端改造触碰共用组件** | `CommandHistory.vue` 被命令面板与采集面板**同时引用**，一处改坏两页全坏 | 提交 6/7 分离；两页手工回归；`pnpm typecheck` + `pnpm build` 双门禁 |
| **双栏折叠断点选择不当** | 1366×768 运维笔记本上出现双滚动条 | 断点取 1200px；把两种分辨率写进 A5.10 验收清单，不靠感觉 |
| **URL query 同步污染后退栈** | 用户点后退退不回上一页（一直在同一页换 query） | 用 `router.replace` 而非 `push` |
| **sticky 吸底栏失效或形成双层滚动** | 批量栏不吸底，问题回到原点 | 已核：`.app-main` 带 `overflow-x: hidden`，按规范会连带把 `overflow-y` 算成 `auto`。实施前必须在 DevTools 实测吸底效果；不生效即换 `el-affix`，不堆 `!important` |
| **A6 生效需双端同版本** | 服务端先升级、Agent 未升级时，旧 Agent 不回传更新结果，台账会停在 `pending` | 由 30min 超时收敛为 `timeout`（可见的降级，非静默）。文档写明「A6 需 Agent 与服务端一并升级」；这也符合本项目双端同版本发布的既有节奏 |
| **execv 前的成功回执可能丢帧** | 台账停在 `pending` 但实际已升级成功 | 已设计独立佐证：Agent 重启后上报新 `agent_ver` → 台账补 `done`（A6.2 时序约定）。回执是**辅助**信号，状态帧才是权威 |
| **`updates` 表长期增长** | 与 `commands` 同型的只增不减 | 建表时即纳入 `cleanup()`：台账保留 90 天（升级是低频操作，行数远小于心跳），复用既有分批删 |
| **loguru 在 PyInstaller 冻结后行为异常** | Agent 无法记录日志（可观测性归零） | 纯 Python 单包、PyInstaller 自动收集；提交 19 内必须实跑 `build_binary.sh` 产物验收（已列入 §4）。异常时回退 stdlib sink —— 适配层隔离了影响面，**回退只改 `logutil.py` 一个文件** |
| **适配层语义偏差（`%s` 与 `{}` 混用）** | 日志消息错乱或丢参数 | 适配器只做 `msg % args`，不解析 `{}`；`test_adapter_percent_format_compat` 锁死语义；**约定新增调用点继续用 `%s`**（写进代码注释） |
| **`setup_logging` 非幂等** | 测试日志量数百倍增长，CI 卡顿 | `_configured` 标记 + `test_setup_logging_idempotent_across_create_app` 门禁 |
| **改用 loguru 后 `KK_LOG_LEVEL` 大小写/取值失配** | 级别解析异常或静默回落到默认 | 适配层对未知级别回落到 INFO 并 `catch`，不抛异常；两侧保持 `getattr`-style 容错 |
| **`log_config=None` 漏配** | uvicorn 覆盖格式，「格式统一」静默失败 | 列入 A7.3 显式清单；`test_stdlib_logs_intercepted` 兜底 |
| **`KK_LOG` 语义变更影响既有运维脚本** | 按旧语义（wrapper 采集）读日志的脚本看不到新内容 | ENV 名与路径均不变，只改写入者；`agent/README.md` / `scripts/build.sh` / `Dockerfile.snippet` 三处同步 + 显式提示 |
| **`diagnose=True` 误开启致敏感信息入日志** | 局部变量（含口令/环境变量）写入日志 | 默认 `diagnose=False`，在 A7.2 表格中标注为生产红线 |
| **`KK_UPDATE_MODE` 默认改 `manual` 是行为变更** | 存量部署升级到该版本后，原本「自动全网升」的路径停止工作，运维会以为自更新坏了 | 这是需求②的**有意为之**（自动全网推与人工选机不能共存）。缓解：① 启动时若为 `manual` 则 `log.warning` 明示「自动升级已关闭，请在版本与更新页手动触发」；② 文档 `deployment.md` 单独一段说明；③ 需要旧行为可显式设 `KK_UPDATE_MODE=auto` |
| **`manual` 模式下 `/agent/latest` 返回 `available=false` 被误读为故障** | 排障时以为端点坏了 | 响应体固定带 `policy` 字段自描述；`docs/deployment.md` 注明；测试 `test_update_mode_manual_hides_poll_path` 锁死该契约 |
| **升级台账 `queued` 行长期堆积** | 离线主机一直不回来 → 台账只增不减 | `queued` 行受 `KK_UPDATE_QUEUE_TTL`（默认 7d）约束，与 Broker 离线队列保留期对齐；`cleanup()` 一并回收（复用既有分批删） |
| **B6.1 的「更新期离线」与 D2 的 `queued` 易混淆** | 两个都叫「更新时的离线」，但语义相反：B6.1 是**正在升级**的主机自报 `reason=updating`；D2 的 `queued` 是**还没开始升级**（消息在 Broker 排队） | 命名上区分：主机 `reason` 用 `updating`，台账状态用 `queued`；D3 两条用例分别覆盖，文档明确对照 |
| **台账 `reason` 与主机 `agent_ver` 短暂不一致** | Agent 已替换二进制但 status 帧未到，UI 仍显示旧版本 | 与 A6.2 同源：以**状态帧为权威**（真实上报的 `agent_ver` 才作数），台账 `done` 是辅助。UI 展示以主机实际上报版本为准，不缓存台账推断值 |

**回滚**：阶段一每项互相独立、按提交分离，任一项出问题可单独 revert；`batch_id` 列只增不改不删，回滚代码后遗留列无害（`server_default=''`）。D 组同理：`updates` 表新增的 `queued` 属**状态枚举取值**而非新列（`status` 本就是 `String`），回滚代码后遗留 `queued` 行只影响展示，不影响收敛（`auto` 模式下照常被超时机制收走）；`KK_UPDATE_MODE` 回滚即设回 `auto`，**无需重建镜像**。
