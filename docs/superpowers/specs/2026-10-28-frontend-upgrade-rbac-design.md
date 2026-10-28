# 前端升级 + RBAC 设计（2026-10-28）

> 指令：按 [vue-pure-admin](https://github.com/pure-admin/vue-pure-admin) 最新代码更新与优化 `web/`，
> 组件库优先用开源最新；保持功能与页面流程正确；**RBAC 页面布局参考开源并对齐样式**。
> 决策来源：本文件由 brainstorming 逐节批准后落稿，用户的四次选择均已写死在正文里。

---

## 0. 结论速览

| # | 事实 | 对本轮的影响 |
|---|---|---|
| F1 | `pure-admin-thin` 上游**至今仍是 6.2.0**，本仓 `web/package.json` 与 `thin@main` 逐行相同（仅 `dev`/`build` 脚本为 Windows 换成 `node --max-old-space-size`） | **「升级 thin 底座」这件事不存在**：没有更新的目标版本。可移动的是 `vue-pure-admin` v7.0.0 线 |
| F2 | 上游在动的是 **vue-pure-admin v7.0.0**（2026-04-07），`compare/v6.2.0...v7.0.0` 改了 190 个文件（`src/views` 93 / `src/layout` 26 / `src/components` 13） | 本轮是**选择性手工移植**，不是换底座；majors 波次单独排期（§9 明确不做） |
| F3 | **`@pureadmin/table` 上游最新就是 3.3.0，本仓已在该版本** | 表格填高有官方机制可用，**零依赖改动**即可修掉 10-15 实测的 P0-2 / P1-1 / P2-1（§3.2） |
| F4 | 开源 RBAC 四页在 `src/views/system/{user,role,menu,dept}`，根节点是 `div.main` **不是 `el-card`**，卡片观感由 `PureTableBar` 的 `bg-bg_color` 容器伪成 | 本仓表格「填不满也滚不动、吸底栏吸不住」的根因（el-card `overflow:hidden` + block body）在开源结构里根本不存在 |
| F5 | 开源菜单 API 返回**一维数组**（`id` + `parentId`），前端 `handleTree` 组树；`menu_type` 有 0菜单/1iframe/2外链/3按钮 四态，`auths` 是行上的字符串权限标识 | `kk_menus` 照此建模（§4.1）；「按钮」就是树上的叶子行，不另建权限点表 |
| F6 | 前端 `addAsyncRoutes` 用 `import.meta.glob("/src/views/**/*.{vue,tsx}")` 的键做 **`includes()` 子串匹配**解析 `component` 字符串，且 `findIndex` 取**首个**命中 | **录错一个组件路径 → 静默白屏**。更要命的是 glob 里含 `form.vue` 与 `utils/hook.tsx`：`component:"system/user"` 首个命中的是 `/src/views/system/user/form.vue`，会把弹窗当页面渲染。所以 `component` **必须写到文件名一级**（`system/user/index`），并由用例断言**唯一命中**（§4.4，红线） |
| F7 | 后端只有单管理员：`kk_admins(username,salt,pw_hash,created)` + `kk_sessions`，`KK_ADMIN_USER/PASS` 首次播种且改 env 重启即轮换口令；`CurrentUser` 依赖做默认拒绝 | RBAC 是在这套之上加身份维度，**不动**口令与会话机制（不做 refresh-token） |
| F8 | `models/store.py` 已 **1142 行**，超本仓「Python 文件 ≤500 行」约束 | RBAC 新代码进独立模块（§4.3）；拆 store.py 本轮不做（§9） |

**分期**：`P0 收口在途 → P1 前端地基（依赖+组件移植+布局范式）→ P2 RBAC 后端 → P3 RBAC 前端+动态菜单`，四期串行，每期可独立回归与提交。

---

## 1. 架构与边界

```
P1  web/src/components/RePureTableBar（移植 v7：列固定，剥 i18n）
    web/src/components/ReDialog（移植 v7：适配新版 el-dialog）
    web/src/style/kk.scss（读数族保留；kk-fill-table/kk-sticky-bar 退役）
    web/src/views/<6 个列表页>（div.main + search-form + PureTableBar + pure-table[adaptive]）
        │ 依赖：@pureadmin/table 3.3.0 的 adaptive 视口测量
        ▼
P2  server/src/kk_server/models/rbac_{tables,seed,store}.py  ← 新代码不进 store.py
        ▼
    controllers/rbac.py（CRUD）+ controllers/deps.py(require_perm 工厂)
        ▼
P3  web/src/api/rbac.ts + web/src/views/system/{user,role,menu}/ + getAsyncRoutes() 真接口
```

**边界**：

- Agent 通道（MQTT + `agent_router` 的 IP 白名单）**完全不进 RBAC**——Agent 没有「用户」概念，`/api/agent/*` 那条自更新路径沿用 `KK_AGENT_IPS` 真实源 IP 校验（v3 决策，本轮不改）。
- 命令黑名单（`KK_CMD_BLACKLIST`）与审计（`store.add_audit`）是**内容**防线，RBAC 是**身份/接口**防线，两层互不替代、互不绕过。
- 前端 `v-auth` 只负责隐藏按钮，**不是安全边界**；后端 `require_perm` 才是（用户明确选择「后端逐接口强制」）。

---

## 2. P0 · 收口在途改动

`web/src/views/host/monitor/index.vue`（+289 行）、`web/src/api/containers.ts`、`web/src/api/exporting.ts` 未提交，属 QR-S36（筛选/排序下推）前端半边。monitor 又是布局改造的核心页，两批改动混在同一未提交文件里会导致回归无法归因。

**动作**：`pnpm typecheck` + `eslint --max-warnings 0` + `pnpm build` 确认完整可用 → 按仓规提交（中文信息、`feat(web)` 前缀、标注 QR-S36）→ P1 从干净树起步。

**门禁必须在干净树上跑**（判例 QR-W11：工作树被未提交文件污染 → 本地假绿、HEAD 红灯）。

---

## 3. P1 · 前端地基

### 3.1 依赖升级清单（只 minor/patch）

| 包 | 当前 | 目标 | 依据 |
|---|---|---|---|
| element-plus | ^2.11.5 | ^2.14.7 | 上游 v7 同线；仍在 2.x，无 3.x 破坏面 |
| @pureadmin/utils | ^2.6.2 | ^2.6.4 | 同上 |
| echarts | ^6.0.0 | ^6.1.0 | 同上 |
| tailwindcss / @tailwindcss/vite | ^4.1.16 | ^4.3.3 | 同上；**仍是 v4，无需 v3→v4 配置迁移** |
| vue | ^3.5.22 | ^3.5.44 | patch |
| vue-tsc | ^3.1.2 | ^3.3.13 | 与 TS 5.9 兼容 |
| sass | ^1.93.2 | ^1.105.1 | 上游 v7 |
| typescript-eslint / eslint-plugin-vue | ^8.46.2 / ^10.5.1 | ^8.71.1 / ^10.11.1 | 配套 eslint 9 的 minor，**不跳 eslint 10** |
| @pureadmin/table | ^3.3.0 | 不动 | 上游最新即 3.3.0 |

**顺序约束**：ReDialog 的 v7 移植**必须早于** element-plus 升 2.14.7——6.3.0 那条 release note 明确是 ReDialog/ReDrawer/message 对 `el-dialog`/`el-drawer`/`ElMessage` 的 API 适配，先升依赖会把弹窗全打歪。

### 3.2 表格填高：换成 `adaptive`

`@pureadmin/table@3.3.0` 的机制（源码 `packages/components/table/index.tsx:299`）：

```js
const setAdaptive = async () => {
  await nextTick();
  const tableWrapper = getTableDoms().tableWrapper;
  const offsetBottom = unref(adaptiveConfig).offsetBottom ?? 96;
  tableWrapper.style.height = `${
    window.innerHeight - tableWrapper.getBoundingClientRect().top - offsetBottom
  }px`;
};
```

`onMounted` 跑一次，`window resize` 按 `adaptiveConfig.timeout ?? 60` 防抖重算；除非 `fixHeader:false`，还会把表头 `position:sticky` 钉住。**关键：视口绝对测量，不依赖祖先高度链**，所以 el-card 的 `overflow:hidden` 与 `.el-card__body` 是 block 这两条都不再是障碍。

`el-table` 与 `el-pagination`（`.pure-pagination`）是同一个被定高 wrapper 里的兄弟，所以 `offsetBottom` 实际是「分页条 + 搜索条 + chrome」的预留——这就是开源四页里 user/role 用 **108**、menu/dept 用 **45** 的原因。**本仓取值以实测为准**（§3.5 的 R 系列口径），不许照抄。

退役：`.kk-fill-table`（6 页挂名全部落空）、`.kk-sticky-bar`（吸不住）。

### 3.3 页面结构规范（对齐开源）

```
div.main                              ← 页面根（列表页不再是 el-card）
├─ el-form.search-form[bg-bg_color w-full pl-8 pt-3 overflow-auto]   ← 搜索条
│    .search-form :deep(.el-form-item){ margin-bottom:12px }
├─ PureTableBar(:columns="columns" @refresh="onSearch")
│    ├─ #buttons                        ← 左标题由 bar 渲染，右按钮走这个 slot
│    └─ v-slot="{ size, dynamicColumns }"
│         ├─ 选中横幅（替换 kk-sticky-bar）
│         │    class="bg-(--el-fill-color-light) w-full h-11.5 mb-2 pl-4 flex items-center"
│         ├─ pure-table(row-key align-whole="center" table-layout="auto" :size="size"
│         │            adaptive :adaptiveConfig="{ offsetBottom: N }"
│         │            :columns="dynamicColumns" :pagination="{ ...pagination, size }"
│         │            :header-cell-style="{ background:'var(--el-fill-color-light)',
│         │                                   color:'var(--el-text-color-primary)' }")
│         │    └─ template #operation    ← 行内操作；破坏性动作用 el-popconfirm
│         └─ （批量操作按钮走 bar 的 #buttons，不放表尾）
└─ .main-content{ margin: 24px 24px 0 !important }   ← 每页 scoped 覆盖
```

列定义放 `utils/hook.tsx` 的 `TableColumnList`，`cellRenderer` 用 TSX；新增/编辑一律 `addDialog()`（ReDialog），**不写手搓 `el-dialog`**。行内按钮 `class="reset-margin" link type="primary" :size="size"`。响应式用 `deviceDetection()`，弹窗宽度 `w-[60vw]!` / `width:"40%"`、`draggable`、`fullscreenIcon`、`closeOnClickModal:false`。

### 3.4 本仓设计语言的保留与移植件

- **保留**（`kk.scss` 的读数族进 `cellRenderer`）：`kk-num`（等宽 tabular-nums）、`kk-state` 四档状态点、`kk-meter` 计量条、`kk-ticks` 心跳刻度、`kk-band` 机队仪表带、`kk-sync` 轮询读数、`kk-row-alert`/`kk-row-fresh` 行态。开源在这些位置用 `el-tag` 色块——**不换**，我们的四档状态更克制且是既定品牌。
- **移植** `vue-pure-admin` v7 的 `RePureTableBar/src/bar.tsx`：带上列固定（`bi/pin-angle` 图标 + `handleToggleColumnFixed`），**剥掉全部 `$t()`/`transformI18n` 调用**（本仓无 i18n 插件；为移植引 `@intlify/unplugin-vue-i18n` + `locales/` 不值当）。
- **移植** v7 的 `ReDialog`（含 `type.ts`）与 `message` 适配。不移植 `ReDrawer`（本轮没有抽屉场景；详情页现有抽屉沿用）。
- `kk-side` 补 `flex-direction: row`（P0-1，一行；两个类共挂同一元素时方向不该依赖「另一类恰好没声明」）。
- `--kk-page-h` / `.kk-page` 只留给**非列表页**（详情、welcome、系统统计）——这些页是多卡堆叠，flex 链本来就是对的。
- detail 页 5 处硬编码 `max-height`（220/220/260/260/240）：本页保留卡片，表格改 `:height` 绑 `--kk-drawer-h` 同族的变量或沿用 flex，**不引入 adaptive**（一页五表同屏，视口测量会互相抢高度）。`.kk-chart{height:300px}` 沿用已有 `useResizeObserver`。

### 3.5 P1 验收（复用 10-15 文档的 R1–R9）

| 编号 | 指标 | 修前实测 | 修后期望 |
|---|---|---|---|
| R1 | 命令中心两栏几何 | 同 left 210 / 同宽 1710 | left 不同，宽比 ≈34%:66% |
| R2 | 命令历史可达性 | `elementFromPoint(960,1500)=null` | 非 null；分页器在视口内可点 |
| R3 | 命令中心容器 | `wrap.scrollTop=600` 回读 0 | 表内滚由 adaptive wrapper 承担，页面不整页滚 |
| R4 | monitor 整页滚 | 5469px | `pageScrolls=false` |
| R5 | 仪表带常驻 | scrollTop=2000 时 top=-1900 | top≈0 |
| R6 | 批量栏可达 | sticky top=3447 | 常驻可见（选中横幅） |
| R7 | audit/update/system | 6606 / 2012 / 1219px | 均 `pageScrolls=false` |
| R8 | detail 窄屏 1366×768 | 932 vs 687 | 无截断 |
| R9 | 改其余页时命令中心几何不变 | — | R1 复测通过 |

**功能不变量**（P1 只改布局，不许动数据流）：7 个页面的轮询（`usePolls`）、`pollFailed` 页头读数、`kk-fill` 语义、批量勾选（FE-7）、`upgradeSkipText`、筛选/排序下推（QR-S36）、导出、命令输出 `useSeq` 防错配——逐项过一遍，任一退化即本轮不通过。

---

## 4. P2 · RBAC 后端

### 4.1 数据模型

新表由 `MD.create_all` 建，**不需要**登记 `_ADD_COLUMNS`；只有 `kk_admins` 补列要登记。MySQL 红线照旧：主键定长 `String(n)`、大字段 LONGTEXT。

| 表 | 列 |
|---|---|
| `kk_admins`（补列） | `is_super SmallInt default 0`、`status SmallInt default 1`、`nickname String(64) default ''`、`updated BigInteger default 0` |
| `kk_roles` | `id Integer PK autoinc`、`code String(64) uniq`、`name String(64)`、`remark String(255) default ''`、`status SmallInt default 1`、`created BigInteger` |
| `kk_user_roles` | `username String(64)`、`role_id Integer`，PK `(username, role_id)` |
| `kk_menus` | `id Integer PK autoinc`、`parent_id Integer default 0`、`menu_type SmallInt`、`title String(64)`、`name String(64)`、`path String(191)`、`component String(191) default ''`、`redirect String(191) default ''`、`icon String(64) default ''`、`auths String(191) default ''`、`rank Integer default 0`、`show_link SmallInt default 1`、`keep_alive SmallInt default 0`、`status SmallInt default 1`、`created BigInteger` |
| `kk_role_menus` | `role_id Integer`、`menu_id Integer`，PK `(role_id, menu_id)` |

`kk_menus` 存**一维数组**（`id`+`parent_id`），与开源 `getMenuList` 的契约一致；组树在前端 `handleTree`。`menu_type`：`0`菜单/`3`按钮（`1`iframe/`2`外链本仓用不到，枚举留着但不播种）。

**表名不改**：`kk_admins` 就是用户表（仓规「不改数据库表名与列名」，同 `containers`/`pod` 判例）；接口层叫 `/api/rbac/users`，产品术语叫「用户」。

### 4.2 权限语义

- 用户 `permissions` = 其所有角色所授菜单行的 `auths` 并集（非空者）；`is_super=1` → `["*:*:*"]`。
- 用户 `roles` = 角色 `code` 数组。
- 菜单可见性 = 所授 `menu_type=0` 且 `status=1` 的行；`menu_type=3` 不进 `/api/routes`。
- 权限判定：`has(perm) = "*:*:*" ∈ perms or perm ∈ perms`。

### 4.3 代码位置（守 ≤500 行）

```
models/rbac_tables.py   四张新表 + _ADD_COLUMNS 登记 + 建表元数据          ~90 行
models/rbac_seed.py     菜单/角色种子 + 权限码常量（唯一权威来源）        ~150 行
models/rbac_store.py    class RbacMixin：CRUD + 权限解析 + 组树查询        ~300 行
models/store.py         class Store(RbacMixin)（只改 class 那一行）        不动
controllers/rbac.py     /api/rbac/users|roles|menus 的 REST                ~250 行
controllers/deps.py     require_perm 工厂（接在 CurrentUser 之后）         +~40 行
```

`RbacMixin` 复用 `Store` 的 `_one/_run/_upsert`，所以 `Store(RbacMixin)` 是最小改动的做法（不新增 `app.state.rbac` 接线）。

### 4.4 播种

- **一次性机械抽取**现有 `web/src/router/modules/kk.ts` 的 7 条路由（4 个一级 + 6 个子级 + detail 的 `show_link:false`）写成 `rbac_seed.py` 的常量，外加「系统管理」一级 `/system` 下的三张新页。服务端读不了 TS，所以种子的权威在服务端；`kk.ts` 之后退役为参照。
- **`component` 一律写到文件名一级**：`host/monitor/index`、`system/user/index`、`system/stats/index`（依据 F6——只写 `system/user` 会先命中 `form.vue`）。
- **幂等**：只补缺失行（按 `name` 判在），**不覆盖**后台改过的字段；因此升级后运维手工改的菜单不会被刷回出厂值。
- 种子角色（权限码精确到枚举起，不留「之类」的模糊）：
  - `operator` = `host:view`、`command:view`、`collect:view`、`audit:view`、`command:dispatch`、`agent:publish`、`agent:rollback`；**不得** `agent:upload`，不得任何 `user:*`/`role:*`/`menu:*`。
  - `viewer` = `host:view`、`command:view`、`audit:view`、`system:view`；不得任何写码。菜单只给主机总览/详情/审计/系统统计——**不给命令中心**（点了也不能用，露出来只是误导）。
  - `admin`（`is_super=1`）不参与授权表，短路得 `*:*:*`。
- **红线用例** `test_rbac_seed.py`：断言每个 `menu_type=0` 行的 `component` 在 glob 键里**有且只有一个**命中（不止「命中」——两个命中会渲染成弹窗页）。这条用例是「录错一个组件路径就静默白屏」的唯一廉价防线。

### 4.5 接口契约

响应形状沿用前端既有约定 `{success, data}`（`/api/login`、`/api/routes`）与 `{code, message, data}`（`/api/rbac/*`，对齐开源 `api/system.ts`，`code===0` 为成功）。

| 方法 | 路径 | 权限码 | 说明 |
|---|---|---|---|
| POST | `/api/login` | — | **响应新增** `roles: string[]`、`permissions: string[]`；`token`/`username` 字段不变 |
| GET | `/api/routes` | 登录即可 | 按当前用户过滤后的菜单树（`RouteComponent` 形状，`component` 为字符串） |
| GET | `/api/me` | 登录即可 | **响应新增** `roles`、`permissions`（刷新页面用，前端现在只回 username） |
| GET/POST/PUT/DELETE | `/api/rbac/users` | `user:view` / `user:add` / `user:edit` / `user:delete` | 列表分页；建用户带初始口令；改昵称/状态 |
| POST | `/api/rbac/users/reset_pwd` | `user:reset_pwd` | 管理员重置他人口令（走既有 `_pwdf` 盐+PBKDF2） |
| POST | `/api/rbac/users/assign_roles` | `user:assign_role` | 全量覆盖式授权 |
| GET/POST/PUT/DELETE | `/api/rbac/roles` | `role:view/add/edit/delete` | 删角色受「仍绑定用户」保护 |
| POST | `/api/rbac/roles/assign_menus` | `role:assign_menu` | 全量覆盖式授权（菜单 id 列表） |
| GET/POST/PUT/DELETE | `/api/rbac/menus` | `menu:view/add/edit/delete` | 一维数组出，前端组树；删菜单级联子行并清 `kk_role_menus` |

**既有 20 个接口逐个挂码**（用户选择「逐接口强制」，不是只挑高危）：

- `host:view` → `GET /api/containers`、`/api/containers/{pod}`、`/api/containers/{pod}/metrics`
- `command:view` → `GET /api/commands`、`/api/commands/batches`、`/api/commands/{cid}`、`/api/commands/{cid}/out`
- `collect:view` → `GET /api/collect/items`
- `audit:view` → `GET /api/audit`
- `system:view` → `GET /api/system/stats`
- `agent:view` → `GET /api/updates`、`/api/agent/current`
- 导出复用同域读码（同一批数据的另一种编码，另立码只增歧义不增防护）：`/api/export/hosts`→`host:view`，`/api/export/commands`、`/api/export/metrics`→`command:view`，`/api/export/audit`→`audit:view`
- `command:dispatch` → `POST /api/commands`（命令与采集**共用**此端点，按 body 拆码很脆；采集面板的可见性由菜单控制，够用了）
- `agent:upload` / `agent:rollback` / `agent:publish` → `POST /api/agent`、`/api/agent/rollback`、`/api/agent/upgrade`
- **不挂码**：`GET /api/health`（容器健康检查匿名探针）、`/api/login`、`/api/logout`

`require_perm(code)` 实现要点：解析 session→username→**一条 join SQL** 取该用户全部 `auths`（避免逐行 N 次往返——本仓已知的静默坑）→ `*:*:*` 短路 → 不中则 `HTTPException(403)` **并** `store.add_audit(actor,"perm_denied",{perm, path, ip})`。每请求一次查询，与 `current_user` 同量级，不上缓存（要缓存就得管失效，500 台规模不值得）。

### 4.6 兼容窗口与红线

- 老库升级：`create_all` 建 4 张新表 + `_ensure_schema` 给 `kk_admins` 补 4 列；`admin` 得 `is_super=1` → 全菜单，**存量部署升级后不会看不见菜单**。
- **把自己锁在门外的兜底**：`is_super=1` 的用户若拿到空菜单树，后端回落成「全部启用菜单可见」。
- 删除保护：不能删自己（409 `self_delete`）、不能删超管（409 `super_protected`）、超管角色不可改不可删、不能删仍绑定用户的角色（409 `role_in_use`）。
- 口令强度沿用既有门禁（`KK_LOG`/`production` 弱口令拒绝启动那条）；新建用户的初始口令过同一校验。
- 审计：登录/登出/命令下发的 `actor` 语义不变（原先只有 admin，现在多用户）；`perm_denied` 新增 action 需进审计页的 action 映射（未映射走既有兜底文案，不许渲染成空白）。

---

## 5. P3 · RBAC 前端 + 动态菜单

### 5.1 目录

```
web/src/api/rbac.ts                       三套 CRUD + 类型（对齐开源 api/system.ts 形状）
web/src/views/system/user/{index.vue,form.vue,utils/{hook.tsx,types.ts,rule.ts}}
web/src/views/system/role/{index.vue,form.vue,utils/{hook.tsx,types.ts,enums.ts}}
web/src/views/system/menu/{index.vue,form.vue,utils/{hook.tsx,types.ts,enums.ts}}
web/src/views/system/stats/index.vue      ← 现有「系统统计」从 views/system/index.vue 挪入
```

腾位与树形：一级 `/system` 的标题从「系统统计」改成**「系统管理」**（仍 `rank:4`，仍是最后一个入口），children 变成四条叶子 `stats`/`user`/`role`/`menu`；`redirect` 改指 `/system/stats`，并给旧路径 `/system/index` 留 **alias**（老书签可用）——这条与命令面板 `/command/index` alias 的既有做法一致。**路由与目录同名**（`/system/user` ↔ `views/system/user/index.vue`），刻意不另起 `/rbac` 前缀：路径与组件目录分叉是 F6 那类「子串命中错文件」事故的温床。

三张页**必须**用 §3.3 定的同一套结构（search-form + PureTableBar + pure-table[adaptive] + hook.tsx 列 + addDialog）。菜单页是树表：`row-key="id"` + `:tree-props`，一维数据由前端 `handleTree` 组。角色页授权 = `el-tree` 勾选 + `getCheckKeys`，与开源 role 页同构。

### 5.2 动态菜单接线

- `web/src/api/routes.ts`：`getAsyncRoutes()` 从「返回空数组的桩」改回真接口 `GET /api/routes`，删掉那段解释桩的注释。
- `web/src/router/modules/kk.ts`：**删除**（业务菜单全部后台下发）；`home.ts`（welcome）/`remaining.ts`/`error` 保持静态。
- `web/src/store/modules/user.ts` + `utils/auth.ts`：`roles`/`permissions` 来自真登录响应，不再是 `["admin"]`/`["*:*:*"]` 硬编码。
- **`/api/routes` 失败不许静默白屏**：`initRouter` 区分「未登录 401 → 跳登录」（既有）与「已登录但接口错 → 错误态 + 重试按钮」，沿用 FE-6 的页头读数风格，不刷 toast。
- `getConfig().CachingAsyncRoutes` 保持关闭（开了是 localStorage 缓存，菜单编辑后会读到过期值）。
- 文档同步：`AGENTS.md` 里「菜单**完全静态**（`getAsyncRoutes()` 返回 `[]`）」那条**必须改**——这是本轮唯一一处推翻既有文档决策，不改会让下一个会话按旧约定把动态路由又桩掉。

### 5.3 按钮级

写操作按钮包 `v-auth="'command:dispatch'"`（指令在 `src/directives/auth`，已在仓内，只是从没被真数据喂过）。隐藏不是防线，后端仍逐接口强制。

---

## 6. 测试与验证口径

**不用 SQLite/typecheck 代替真实环境**（用户既定偏好）。

| 层 | 口径 |
|---|---|
| 后端回归 | WSL 内 rsync 后 `KK_IT_MQTT_URL=mqtt://127.0.0.1:1883 KK_MQTT_URL=mqtt://127.0.0.1:1883 uv run --no-sync python -m pytest agent/tests server/tests`（两个变量**分开各自赋值**——写成 `A=B=v` 会让 `KK_IT_MQTT_URL` 的值变成字符串 `KK_MQTT_URL=mqtt://…`，集成用例静默 skip 而汇总仍是绿的）；基线 **397 passed / 0 skip 不许掉**（2026-10-28 Task 1 实测重定；此前记的 357 是 QR-S38/协议窗口用例入库前的旧数。0 skip 只在 WSL 真 Broker 路径成立，Windows 无 Broker 是 4 skip，报告须写明走的哪条）。排除项必须根锚定（`--exclude /.venv --exclude /web/node_modules …`），判例：`--exclude tests` 曾把两轮新用例全排掉，绿色来自旧文件 |
| 三库 | SQLite 实测；**真实 PG16（wslc，宿主 5433）跑一遍 RBAC 建表/补列/CRUD/授权**；MySQL 仍只做方言编译级静态校验，并**在结论里如实写「未连过真 MySQL」** |
| RBAC 强制 | 逐接口 403/200 两侧用例；`*:*:*` 短路；空菜单超管兜底；四条删除保护；seed↔views 存在性 |
| 前端门禁 | `pnpm typecheck`、`eslint --max-warnings 0 "{src,mock,build}/**"` 零豁免、`pnpm build`、产物整目录同步 `server/src/kk_server/web/` 并逐文件 sha256 核对、`git status --porcelain` 为空 |
| 布局 | **Playwright + 真实 Chrome**（内置浏览器是隐藏页，几何不可信），1920×1080 与 1366×768 两档，500 台种子数据（`data/seed_analysis.py` 口径），逐项过 R1–R9 |
| RBAC 流程 | 真实浏览器走「建用户→授角色→以该用户登录→看不见的菜单敲 URL 进不去 **且** 直接敲接口也 403」正反两条 |

---

## 7. 多 agent 编排

| 期 | 串行地基 | 可并行单元 | 派发边界 |
|---|---|---|---|
| P1 | 依赖 bump、`bar.tsx`/ReDialog 移植、`kk.scss` 收口（**主线单人**，是所有页的共同上游） | monitor / audit / update / shell+collect / welcome / detail 六个改造单元 | 每个 agent **只准碰 `src/views/<自己的页>/**`**；shared 文件（`kk.scss`、`components/`、`api/`）由主线持有。**不用 worktree**——六路合并 `kk.scss` 必然冲突，靠文件互斥而非合并来避免 |
| P2 | `rbac_tables.py` + `rbac_seed.py`（先行） | ① `rbac_store.py` ② `require_perm` + 控制器接线 ③ 测试 | ②③ 依赖 ①；① 与 ③ 的用例骨架可并行 |
| P3 | 动态路由接线（回主线） | user / role / menu 三页 + stats 腾位 | 三页目录互不相交；`api/rbac.ts` 主线先定稿 |

**回线纪律**：每个 agent 回来我先 `git diff --stat` 核对**只动了授权范围内的文件**，再跑门禁，再抽查 diff 的实际内容（agent 的自述是意图，不是事实），最后按仓规提交。失败一轮不重试同一 prompt——先判根因。

---

## 8. 提交约定

中文信息 + `feat/fix/perf/refactor/chore/docs` 前缀 + 按模块分批；编号沿用 `FE-x`（延续 10-07 方案）、`QR-x`（质量账本）并新增 `RBAC-x`。每期结束补一条落地状态表进本文件（判例：提交信息不是证据，引用前 `git show <c> -- <file>`）。

---

## 9. 本轮明确不做

| 项 | 理由 |
|---|---|
| vite 7→8（Rolldown/Oxc）、vue-router 4→5、pinia 3→4、TS 5.9→6、eslint 9→10、stylelint 16→17、@vueuse 14→15、code-inspector 1→2、unplugin-icons 22→24、vue-types 6→7 | 用户选择「只升 minor/patch + 移植 v7 组件」；majors 波次要重写 flat config、把守卫 `next()` 改 `return`、`better-tailwindcss` 类序规则会对所有 .vue 产生 auto-fix 大 diff，与本轮布局改造同时做则回归无法归因。**下一期单独做** |
| 部门管理（`dept`） | 本产品没有部门概念（只有主机 `env`/`group`），做了就是一张空表 |
| i18n（`@intlify/unplugin-vue-i18n` + `locales/`） | 移植 `bar.tsx` 时剥掉 `$t()` 即可；为四枚图标 tooltip 引整个 i18n 子系统不值当 |
| refresh-token | 后端从来没有；前端 `refreshTokenApi` 保持显式抛错 + 拦截器清 token 跳登录的既有语义 |
| 拆 `models/store.py`（1142 行） | 超本仓 ≤500 行约束是**既有债务**，与本轮目标正交。机械拆 1142 行热代码混进这轮，出了问题分不清是升级引入、重构引入还是 RBAC 引入。RBAC 新代码全部进独立模块，先保证增量合规 |
| FE-19 构建瘦身（`manualChunks`）、FE-22 `http.request` 包装、FE-24 `dayjs`、FE-33 审计 actor/action 筛选 | 10-07 方案里已判过「未做/收益边际」，本轮不翻案 |

---

## 10. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| `adaptive` 与 `el-card` 共存时 sticky 表头行为异常 | 中 | 列表页按 §3.3 去掉 el-card（用户已批准）；detail 页明确不用 adaptive |
| seed 的 `component` 子串命中多个文件 → 解析到错页面 | 中 | 用例断言**唯一命中**（不止「命中」） |
| 逐接口挂码后，某条既有前端调用路径漏码 → 线上 403 | 高 | 前端全量扫 `src/api/*.ts` 与挂码表逐条对照；集成用例覆盖每个端点至少一次 200 |
| 动态菜单后端故障 → 全站无入口 | 高 | §5.2 错误态而非白屏；超管空菜单回落全量可见 |
| element-plus 2.14 与未移植的 ReDialog 时序错 | 中 | §3.1 顺序约束：先移植后升依赖 |
| 六页并行 agent 越界改 shared 文件 | 中 | 回线 `git diff --stat` 白名单核对 |

---

## 11. 与既有文档的接续

- `docs/frontend-layout-analysis-2026-10-15.md`：P0-1/P0-2/P1-1/P1-2/P2-1 五条缺陷在 §3 一次收口，R1–R9 直接复用为 P1 验收；本方案把它的「flex 链修复」路线换成「上游 sanctioned 的 `adaptive` 路线」，理由是同一缺陷用官方机制修，不需要自研 `--kk-page-h` 全站铺开。
- `docs/frontend-optimization-plan-2026-10-07.md`：FE-5（高度锚点全站）以 §3.2 的形式落地并改为不依赖锚点；FE-19/22/24/33 维持未做判定（§9）。
- `docs/v4-evolution-plan-hosts-docker.md`：QR-S36 的服务端半边已合入（`e8333a8`/`0e8e8d3`），P0 收口的是前端半边，两者不冲突。
- 需更新：`AGENTS.md`（菜单完全静态那条）、`docs/completion-plan-mqtt.md`（登记 RBAC 分期）。
