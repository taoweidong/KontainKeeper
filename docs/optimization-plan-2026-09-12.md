# KontainKeeper 优化方案（基于 2026-09-12 质量分析）

> 编写日期：2026-09-12　基线：`main` @ 31768a4（工作区干净）
> 依据：[`docs/quality-assessment-2026-09-12.md`](quality-assessment-2026-09-12.md)
> 性质：**方案文档，评审通过后再动代码**。分两阶段，阶段一交付核心能力，阶段二加固。
> 约定：不引入新第三方依赖（导出用标准库 `csv` + `io`；前端只用 Element Plus + 现有 `kk-*` 工具类），不改表名与列名，不改协议（阶段一零协议变更）。

---

## 0. 方案总览

| 批次 | 项 | 目标 | 改动面 | 依赖 |
|---|---|---|---|---|
| **一** | A1 结果导出 | 补 P0-1：命令/审计/主机/指标四类导出 | 新增 1 控制器 + 1 API 模块 + 1 工具函数；4 个页面加按钮 | 无 |
| **一** | A2 批量发布回路去 N+1 | 补 P1-1：500 台点击零 DB 回查 | `commands.py` 1 处循环重写 | 无 |
| **一** | A3 结果分页 + 批次聚合 | 补 P1-2/P1-3：能看全、能按批次核验 | 1 列登记 + store 2 方法 + 控制器 + 前端页 | A1 复用筛选参数 |
| **一** | A4 上报间隔治理 | 补 P1-4：消除死配置 | `config.py` + `mqtt_bridge._on_hb` + stats | 无 |
| **一** | A5 **Web 前端易用性与布局改造** | 命令快速下发 / 结果查询 / 导出入口 / 消除布局 magic number | 新增 4 个组件 + `kk.scss` 布局基建 + 5 个页面改造 | 复用 A1 导出、A3 分页；与 A1/A3 同批交付体验最佳 |
| **二** | B1 客户端 nice 降权 | 补 P2-1：零代码级隔离 → 优先级隔离 | `entrypoint-wrapper.sh` | 无 |
| **二** | B2 自更新回滚接口 | 补 P2-7：`.prev` 有文件无入口 | 1 端点 + 前端 1 按钮 | 无 |
| **二** | B3 登录限流补 IP 维度 | 补 P2-3 | `auth.py` | 无 |
| **二** | B4 文档一致性修正 | 补 P2-8 | `AGENTS.md` / `main.py` 注释 / `architecture-review.md` | 无 |
| **二** | B5 CI 落地（真库 + Broker + nightly 压测） | 补 P2-5/P2-6 | 新增 `.github/workflows/ci.yml` | 需要仓库启用 Actions |
| 不做 | C1 ed25519 签名、C2 协议压缩、C3 共享订阅 | 既有决策（`architecture-review` §0 已关闭），本次不翻案 | — | — |

**阶段一完成后的验收口径**：在命令中心**一屏之内**完成「选 500 台 → 下发 → 看到该批次进度 → 翻页/按批次筛选看全结果 → 一键导出 CSV 核验成功/失败分布」，且发布动作零 DB 回查、业务页无布局硬编码、无新增 UI 依赖。

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
| 无 magic number | 全仓 `grep "calc(100vh"` 在业务页为 0 命中 |
| 一致性 | 新组件只用 `el-*` + `kk-*`；`pnpm typecheck` 与 `pnpm build` 零错误；无新增依赖（`package.json` diff 为空） |
| 回归 | 总览「批量执行命令/批量采集」跳转后主机预选仍然生效；`?pods=` 契约不变 |
| 导出闭环 | 命令面板导出按钮产出的 CSV 与页面筛选条件一致（行数、状态、批次） |

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

**现状**：仓库**没有 `.github/workflows/`**（已确认），因此「三库真库验证」与「500 台压测」目前无任何自动保障。

新增 `.github/workflows/ci.yml`，三个 job：

| job | 内容 | 触发 |
|---|---|---|
| `test` | `uv sync --all-packages` → `pytest agent/tests server/tests`，配 `eclipse-mosquitto:2` service（1883）→ 断言 **202 passed（含 4 个原 skip 的集成用例）** | push / PR |
| `dialects` | matrix `postgres:16` / `mysql:8` service，跑 `test_dialects.py` 的**真实连接**版本 + `test_store.py`，断言扩列迁移（`_ensure_schema`）在两库上真的成功 | push / PR |
| `nightly` | `scripts/loadtest.py` 500 连接 × 60s，断言心跳零误判掉线、命令成功率 100%；`scripts/bench_agent.py` 断言 RSS `< 40MB` | `schedule: cron` |

> **真库验证的已知风险**（`design.md:222` 已标注）：MySQL 排序规则大小写、PG 标识符小写折叠可能暴露既有代码的隐性问题。**这正是这个 job 的价值**——它可能一次跑出若干真实缺陷，需要预留修复余量，不要指望「加个 CI 文件就绿」。

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
| 10 | `chore(agent): Agent 启动 nice 降权` | B1 |
| 11 | `feat(server): Agent 自更新回滚接口` | B2 |
| 12 | `fix(server): 登录限流补客户端 IP 维度` | B3 |
| 13 | `docs: 修正文档与代码失配项` | B4 |
| 14 | `chore(ci): 落地测试/真库/nightly 压测流水线` | B5 |

每个提交前跑：`.venv/Scripts/python.exe -m pytest agent/tests server/tests -q`；涉及前端时加 `pnpm typecheck`；涉及前端产物时 `pnpm build` 并同步到 `server/src/kk_server/web/`。

> **提交 2 为何独立**：布局基建（`.kk-page` / `.kkPoll` / `downloadBlob`）是提交 3/6/7/8 的共同依赖。独立提交让「布局回归」这类问题可以单点回滚，而不必牵连业务页改动。

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

**回滚**：阶段一每项互相独立、按提交分离，任一项出问题可单独 revert；`batch_id` 列只增不改不删，回滚代码后遗留列无害（`server_default=''`）。
