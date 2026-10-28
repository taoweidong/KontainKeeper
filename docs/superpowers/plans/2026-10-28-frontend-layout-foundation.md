# 前端地基与布局对齐（P0+P1）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `web/` 的 6 个列表页改成 vue-pure-admin 开源 RBAC 页同款结构（`div.main` + 搜索条 + `PureTableBar` + `pure-table[adaptive]`），用 `@pureadmin/table@3.3.0` 自带的视口测量替掉自研 `kk-fill-table` flex 链，并升到上游 v7 线的 minor/patch 依赖与组件实现。

**Architecture:** 三层顺序——先立几何探针（红基线），再动共同上游（依赖 + `ReDialog`/`bar.tsx` + `kk.scss` 地基），最后逐页套用已定形的范式。表格高度不再依赖祖先高度链，`el-card` 的 `overflow:hidden` 因此失去杀伤力，列表页顺带去卡片。

**Tech Stack:** Vue 3.5 + TS 5.9 + Vite 7 + Element Plus 2.14 + `@pureadmin/table` 3.3.0 + Tailwind 4 + pnpm 12。后端零改动（P2/P3 才碰）。

**Spec:** [docs/superpowers/specs/2026-10-28-frontend-upgrade-rbac-design.md](../specs/2026-10-28-frontend-upgrade-rbac-design.md) —— 本计划实现其 §2（P0）与 §3（P1）；§4/§5（RBAC）另立计划。

## Global Constraints

每个任务的隐式要求，逐条来自 spec：

- **只升 minor/patch**：element-plus `^2.14.7`、@pureadmin/utils `^2.6.4`、echarts `^6.1.0`、tailwindcss+@tailwindcss/vite `^4.3.3`、vue `^3.5.44`、vue-tsc `^3.3.13`、sass `^1.105.1`、typescript-eslint `^8.71.1`、eslint-plugin-vue `^10.11.1`。**禁止**碰 vite/vue-router/pinia/typescript/eslint/stylelint/@vueuse/core/code-inspector-plugin/unplugin-icons/vue-types 的大版本（spec §9）。`@pureadmin/table` 已在最新 3.3.0，**不许动**。
- **移植上游 v7 组件时剥掉 i18n**：`transformI18n(...)` → 原表达式，`$t(...)`/`t(...)` → 中文字面量，`import { transformI18n } from "@/plugins/i18n"` 整行删除。本仓没有 i18n 插件，也不许为此新增（spec §9）。
- **`src/plugins/elementPlus.ts` 是显式注册表**（218 个组件）。模板里新用任何一个 `el-xxx` 标签前必须确认它在表里，否则**运行时静默不解析、页面缺控件且 typecheck 不报错**。已确认在表：ElPopconfirm、ElTree、ElTooltip、ElPagination、ElSegmented、ElEmpty、ElDrawer、ElUpload、ElSwitch、ElInputNumber、ElLink。
- **读数族不换**：`kk-num`/`kk-state`/`kk-meter`/`kk-ticks`/`kk-band`/`kk-sync`/`kk-row-alert`/`kk-row-fresh`/`kk-kv`/`kk-sub` 原样进 `cellRenderer`。开源在这些位置用 `el-tag` 色块，**不跟**（spec §3.4）。
- **轮询一律走 `usePolls()`**（`src/utils/kkPoll.ts`），禁止裸 `setInterval`；交互计时器（长按）不套。
- **功能不变量**（P1 只改布局，数据流一行不许动）：`useSeq()` 防迟到回包盖新值、`pollFailed` 页头读数不刷 toast、QR-W16「没读到 ≠ 空」的 `read` 位、批量勾选（FE-7）、`upgradeSkipText`、QR-S36 筛选/排序下推、导出带当前筛选范围、`el-select`/`el-checkbox` 不挂重复 `@change`（FE-8）。任一退化即本计划不通过。
- **几何口径只用真实浏览器**：内置浏览器是隐藏页，截图与 `getBoundingClientRect` 不可信。驱动 `@playwright/cli`（本机全局装的是 `playwright-cli@0.1.13`），1920×1080 与 1366×768 两档，500 台种子数据。
- **element-plus 2.14 的插槽类型坑（Task 3 实测，Tasks 6–10 必撞）**：2.14 把 `#default` 作用域插槽的 `row` 收窄成 `DefaultRow`，与本页真实类型（`HostSummary` / `CommandRow`）不再自动相容。**正确做法是在数据边界收窄**（`row as HostSummary`，且该类型必须已在文件里 import 过），**不要**给插槽写类型标注 `#default="{ row }: { row: HostSummary }`——Task 3 试过，错误从 7 条涨到 12 条，已回退。`as any` / `@ts-ignore` / `@ts-expect-error` / `eslint-disable` 全部禁止。另：vue 3.5.44 收紧了 props 推断，组件 `props` 里需要 `type: [...] as PropType<X>`（上游 `iconifyIconOffline.ts` 就是这么改的）。
- **`el-pagination` 的 `small` 已弃用**（3.0 前移除），会在浏览器控制台打警告：现存三处 `audit/index.vue:196`、`command/components/CommandHistory.vue:446`、`host/monitor/index.vue:689`，正是 Task 6/7/9 各自要重写的文件——**迁移时顺手换成 `size`**，别让它留到 Task 11 被判成新增回归。
- **每一列都必须有唯一、非空的 `label`（Task 4 实测，Tasks 6–10 硬约束）**：`PureTableBar` 的列显隐/固定/拖拽**全部按 `label` 匹配**，而 `getKeyList` 的行为是实测出来的、不是猜的——它**不去重**，并且**静默丢弃没有 `label` 键的列**。后果两条：① 两列同名（例如两个「操作」）时，弹层里出现两行无法区分的条目、共用同一个开关值，且三处 `.find`/`filter[0]` 匹配器只命中**第一列**，第二列永远隐不掉也固定不了；② 没有 `label` 的列（最典型就是裸 `{ type: "selection" }`）会让 `checkColumnList.length < dynamicColumns.length`，而 `rowDrop.onEnd` 是**按下标位置**去对 `dynamicColumns` 的，拖拽排序会移动错列。这两条 typecheck/eslint/build **全都测不出来**，只在运行时出现。
  所以：写 `columns` 时每列给一个页内唯一的 `label`；**勾选列必须显式写 `label`**（开源的做法是 `{ label: "勾选列", type: "selection", fixed: "left", reserveSelection: true }`，照它写），不要留裸 `{ type: "selection" }`。
- **本仓对上游的一处有意偏离**：`bar.tsx` 里 `fixed: true` 归到**左**固定（与 element-plus 一致；上游 `main` 把它当右固定是 bug，依据见 `element-plus/es/components/table/src/store/watcher.mjs` 的 `updateColumns` 与 `@pureadmin/table` 的 `TableColumnFixed` 注释）。**将来任何人重新对齐上游 bar.tsx 时必须保留这条偏离**，改代码前先读 `bar.tsx:237-241` 的注释。
- **`pnpm lint:stylelint` 在本仓是 repo 级 `--fix`，且会静默改坏 CSS——Tasks 6–10 禁止裸跑。** `web/stylelint.config.js:67` 的 `order/order` 把泛型 `"at-rules"` 排在 `"rules"` **之前**，于是 `--fix` 会把 `@media (max-width:1200px){…}` 整块**提到它要覆盖的基础规则上面**——同特异性下后来者胜，媒体查询从此失效。Task 5 实测抓到它把 `CommandWorkbench.vue` 的窄屏折叠改没了（已回退）。要求：**只用 `npx stylelint <自己改过的文件>` 限定范围跑，且必须逐行看它对别人文件做了什么**；绝不允许把 stylelint 顺带重排出的无关 diff 提交进去。这条属仓级既有缺陷（任何人跑 `pnpm lint` 都会中招），本程序内先按规避处理，是否修配置留给收口时向用户提出。
- **几何测量前必须让页面 settle ~2s**（Task 5 实测）：`goto` 之后立刻 eval 会读到过渡中的坐标（整页偏 −30px，`left:180` 而非 210）。读完先核对 `sideCols`/`bandTop` 是否落在稳定值上，再判定。
- **测量环境事实（Task 2 实测校正，后续任务的命令里若与此不符以此处为准）**：dev server 实际端口 **8849**（8848 被本机另一个 vite 占用，别去动它）；后端 8443；playwright 会话名 `-s=layout`，认证态存 `%TEMP%\kk-layout-state.json`，**重开会话会掉回 1280×720，必须重新 `resize`**；种子库里主机名是 `vscode-0000` 这种格式，**没有 `Web-01`**——详情类路由一律用 `#/hosts/detail/vscode-0000`；探针 `scripts/fe_layout_probe.js` 的内容区滚动容器判据是「包着 `.main-content` 的那个 `.el-scrollbar__wrap`」（裸 `.el-scrollbar__wrap` 会命中侧边栏，把整页滚读成菜单滚）。
- **门禁三件套每任务收尾必跑**：`pnpm typecheck`、`pnpm lint:eslint`（`--max-warnings 0`，零豁免）、`pnpm build`。
- **提交**：中文信息 + `feat/fix/perf/refactor/chore/docs` 前缀 + 按模块分批；编号沿用 `FE-x`（10-07 方案）、`QR-x`（质量账本），本计划新增 `L1-x`（布局一期）。
- **工作目录**：前端命令一律在 `E:\GitHub\KontainKeeper\web`，git 命令在仓库根。

---

## 文件结构

```
scripts/fe_layout_probe.js            新建｜几何探针（R1–R9 口径），playwright-cli eval 的实参
scripts/seed_layout_data.py           新建｜从 data/seed_analysis.py 收编（data/ 被 gitignore，500 台种子须可重建）
web/package.json                      修改｜9 个包的目标版本（Global Constraints）
web/src/components/ReDialog/          替换｜v7 的 index.vue + type.ts + index.ts（剥 i18n）
web/src/components/RePureTableBar/src/bar.tsx  替换｜v7 版（列固定 + 剥 i18n）
web/src/style/kk.scss                 修改｜+布局范式类 −kk-fill-table −kk-sticky-bar，.kk-side 补方向
web/src/views/audit/utils/hook.tsx    新建｜列定义与分页/弹窗逻辑（范式样板）
web/src/views/audit/{index.vue,form.vue}     改写｜样板页
web/src/views/command/components/{CommandWorkbench,CommandHistory}.vue  改写｜P0-1/P0-2 收口
web/src/views/host/update/            改写｜两张表
web/src/views/host/monitor/           改写｜含 kk-band、批量栏、QR-S36 新列
web/src/views/{welcome,host/detail,system}/  改写｜保留卡片，只清 kk-fill-table 误用与硬编码高度
docs/frontend-layout-analysis-2026-10-15.md  修改｜五条缺陷逐条标落地状态与复测数值
```

---

### Task 1: 收口在途 QR-S36 前端半边（P0）

**Files:**
- Modify（已在工作树，本任务只负责验证并提交）: `web/src/views/host/monitor/index.vue`、`web/src/api/containers.ts`、`web/src/api/exporting.ts`

**Interfaces:**
- Consumes: 无
- Produces: 干净工作树；monitor 页的最终列集合（Task 9 的改造对象以此为准）

- [ ] **Step 1: 确认工作树只有这三个文件**

```bash
cd /e/GitHub/KontainKeeper && git status --porcelain
```
预期：仅 3 行 ` M web/src/...`。出现第四个文件即停——说明有别的会话在写，先问用户，别把别人的半成品卷进提交。

- [ ] **Step 2: 干净树上跑门禁**（判例 QR-W11：工作树污染 → 本地假绿、HEAD 红灯）

```bash
cd web && pnpm typecheck && pnpm lint:eslint && pnpm build
```
预期：三条退出码全 0。任一条红：**先修，不许跳过提交**；修不了就回退本任务并报告，别把红灯带进后续 10 个任务。

- [ ] **Step 3: 后端回归确认下推参数没打坏契约**

WSL 内（`~/kk`，rsync 排除项必须根锚定）：
```bash
KK_IT_MQTT_URL=KK_MQTT_URL=mqtt://127.0.0.1:1883 uv run --no-sync python -m pytest agent/tests server/tests -q --junitxml=/tmp/j.xml
```
预期：**397 passed / 0 skip**（Task 1 实测重新定基线。原写 357 是 QR-S38/协议窗口那批用例入库前的旧数，本仓此后只许涨不许掉）。**别用 `| tail -3` 看汇总**（失败行在进度条之前会被截掉，本仓因此漏看过两轮红灯）——解析 junit 的 `tests/failures/errors/skipped`。注意 **0 skip 只在 WSL 有真 Broker 的跑法下成立**；Windows 侧无 Broker 会显示 4 skip，报告时必须写明走的是哪条路径，不能把 4 skip 说成全绿。

- [ ] **Step 4: 提交**

```bash
git add web/src/views/host/monitor/index.vue web/src/api/containers.ts web/src/api/exporting.ts
git commit -m "feat(web): v4 筛选/排序下推接到总览页（QR-S36 前端半边）"
git status --porcelain   # 预期：空
```

---

### Task 2: 几何探针与种子数据（先把红基线钉住）

**Files:**
- Create: `scripts/fe_layout_probe.js`
- Create: `scripts/seed_layout_data.py`
- Modify: `.gitignore`（只在需要放行 `scripts/` 下产物时）

**Interfaces:**
- Produces: `playwright-cli eval "$(cat scripts/fe_layout_probe.js)"` → 单行 JSON（字段见 Step 1），Task 6–11 全部用它判绿；`python scripts/seed_layout_data.py` → 500 台主机 + 185 命令 + 300 审计 + 40 台账的分析库
- Consumes: 运行中的 dev server（默认 `http://127.0.0.1:8849`）与已登录会话

- [ ] **Step 1: 写探针**

`scripts/fe_layout_probe.js`（整文件，勿改口径——这 11 个顶层字段就是 R1–R9 的测量定义，Tasks 6–11 全部按字段名断言）。**下面这段是已提交版本的形态**：内容区滚动容器的判据在 Step 1b 里补了「会话死掉就返回 `error`」的早退分支，抄这段时必须一起带上，否则静默绿灯的 bug 会被重新引入。测量端用它自己的消费规则：**先判 `.error`，再用 `===` 严格比较取值——`!pageScrolls` 这类真值判断一律禁止**（`undefined` 和 `null` 同样为假，早期版本正是靠这个漏洞把登录页读成绿灯的）。

```js
// 布局几何探针：对当前页跑一遍 R1–R9 口径，返回单行 JSON。
// 用法：playwright-cli -s=layout eval "$(cat scripts/fe_layout_probe.js)"
(() => {
  const q = s => document.querySelector(s);
  const rect = el => (el ? el.getBoundingClientRect() : null);
  // 滚动容器：底座是**内容区** el-scrollbar 的 wrap，不是 window。
  // 裸 `.el-scrollbar__wrap` 会命中侧边栏（DOM 序第一，scrollHeight 恒为菜单高），
  // 把「整页滚不滚」读成侧边栏读数；表格体、设置面板也各有 wrap。
  // 内容区 wrap 的唯一特征：它包着路由页面根 .main-content。
  const wrap =
    [...document.querySelectorAll(".el-scrollbar__wrap")].find(w =>
      w.querySelector(".main-content")
    ) || null;
  const view = wrap ? wrap.querySelector(":scope > .el-scrollbar__view") : null;
  const card = q(".el-card");
  const table = q(".el-table");
  const pager = q(".el-pagination");
  const band = q(".kk-band");
  const cols = [...document.querySelectorAll(".kk-side > .kk-col")].map(el => {
    const r = rect(el);
    return { left: Math.round(r.left), width: Math.round(r.width) };
  });
  const hits = [
    [960, 700],
    [960, 900],
    [960, 1050]
  ].map(([x, y]) => !!document.elementFromPoint(x, y));
  let scrollProbe = null;
  if (wrap) {
    const before = wrap.scrollTop;
    wrap.scrollTop = 600;
    scrollProbe = wrap.scrollTop;
    wrap.scrollTop = before;
  }
  return JSON.stringify({
    path: location.hash || location.pathname,
    pageScrolls: wrap && view ? view.scrollHeight > wrap.clientHeight : null,
    scrollHeights: view && wrap
      ? { scroll: view.scrollHeight, client: wrap.clientHeight }
      : null,
    canScroll: scrollProbe === null ? null : scrollProbe > 0,
    tableRect: rect(table),
    pagerRect: rect(pager),
    bandTop: band ? Math.round(rect(band).top) : null,
    stickyTop: (() => {
      const s = q(".kk-sticky-bar");
      return s ? Math.round(rect(s).top) : null;
    })(),
    sideCols: cols,
    reachableAt: hits,
    cardOverflow: card ? getComputedStyle(card).overflow : null
  });
})();
```

- [ ] **Step 1b: 探针必须能响亮失败**

会话过期或被重定向到登录页时，`wrap` 会是 `null`。此时探针**不许**返回一栈 `null` 字段配上 `path:"#/login"` 以外的默认值——`reachableAt` 在登录页仍是 `[true,true,true]`，后续任务用 `!pageScrolls` 这类真值判断就会把「根本没在目标路由」读成绿灯。要求：`wrap` 为 null 时早退，返回 `{"error":"no content wrap — session dead or layout changed","path":…}`。这**不动那 11 个字段的定义**，只加一条失败路径，因此不违反「勿改口径」。

- [ ] **Step 2: 收编种子脚本**（`data/` 被 gitignore，500 台数据必须能从版本库重建，否则 R 系列口径下个会话无法复现）

```bash
cd /e/GitHub/KontainKeeper
cp data/seed_analysis.py scripts/seed_layout_data.py
python - <<'PY'
import re, pathlib
p = pathlib.Path("scripts/seed_layout_data.py")
s = p.read_text(encoding="utf-8")
s = s.replace("布局实测灌数脚本（一次性，勿入库）", "布局回归灌数脚本（R1–R9 口径的 500 台种子）")
p.write_text(s, encoding="utf-8")
PY
git add scripts/seed_layout_data.py && python scripts/seed_layout_data.py
```
预期：脚本自带的灌数输出，主机数 500。若它写死了 `data/service/kk-analysis.db` 路径，保持不动（分析库，`service/` 也在 gitignore 内）。

- [ ] **Step 3: 起服务并登录一次**

```bash
# 终端 A：后端（用分析库）
KK_DB_PATH=service/kk-analysis.db .venv/Scripts/python.exe -m kk_server
# 终端 B：前端
cd web && pnpm dev
# 浏览器会话由 playwright-cli 持有，人工登录一次即可（session 名 layout）
playwright-cli -s=layout open http://127.0.0.1:8849
playwright-cli -s=layout resize 1920 1080
playwright-cli -s=layout snapshot   # 取 ref 后 fill 用户名 admin / 口令 admin123 并点击登录
```
预期：落到 `/welcome`。**登录态复用同一个 `-s=layout` 会话**，后续任务不再重登。

- [ ] **Step 4: 跑红基线并留档**

依次 `goto` 六个页面并 eval：
```bash
for h in "#/hosts/monitor" "#/hosts/update" "#/audit/index" "#/command/shell" "#/command/collect" "#/hosts/detail/vscode-0000"; do
  playwright-cli -s=layout goto "http://127.0.0.1:8849/$h" >/dev/null
  printf '%s ' "$h"; playwright-cli -s=layout eval "$(cat scripts/fe_layout_probe.js)"
done
```
预期红灯基线（Task 2 实测校正后的判据）：`pageScrolls:true`；命令中心 `canScroll:false` 且 `pagerRect.bottom` 远超 `scrollHeights.client`；`sideCols` 两项 `left` 相同（实测 210/1710）；`stickyTop` 远大于视口（实测 5447）；`cardOverflow:"hidden"` 各处可见。

**`reachableAt` 不是 R2 的判据**——三个采样点 (960, 700/900/1050) 在 1920×1080 下恒在视口内，坏页面也读 `[true,true,true]`；而 10-15 文档 R2 原文的 `elementFromPoint(960,1500)` 在 1080 高的视口里**物理上不可能非 null**（超出视口的坐标一律返回 null），那条绿标准永远无法达成。R2 的实际红判据是 `canScroll:false` + `pagerRect.bottom` 落在 `scrollHeights.client` 之外，两者已在同一份 JSON 里。**不许为了让 `reachableAt` 有意义而改它的采样点**——改语义会让 Tasks 6–11 的复测失去可比性；改成从表格底边推导坐标等于换了一道尺子。

把这批 JSON 原样贴进提交信息正文——**没有红基线就没有「修好了」的证据**（verification-before-completion）。

- [ ] **Step 5: 提交**

```bash
git add scripts/fe_layout_probe.js
git commit -m "test(fe): 布局几何探针 R1–R9 口径脚本化（L1-1）"
```

---

### Task 3: 依赖 bump + 移植 v7 ReDialog

**Files:**
- Modify: `web/package.json:50-130`
- Modify: `web/src/components/ReDialog/index.vue`, `web/src/components/ReDialog/type.ts`, `web/src/components/ReDialog/index.ts`

**Interfaces:**
- Produces: `addDialog(options: DialogOptions)`、`ReDialog`（全局注册于 `src/App.vue`）、`DialogOptions` 类型——Task 6–10 的表单弹窗与 Task P3 的 RBAC 弹窗都调它
- Consumes: 无

> **顺序红线**：依赖升级与 ReDialog 移植**必须在同一个提交里落地**。6.3.0 的 release note 明确 ReDialog/ReDrawer/message 是「适配 `el-dialog`/`el-drawer`/`ElMessage` 的 API 变化」——先升 element-plus 会让所有弹窗处于已知坏态，任何中间提交都是红灯。

- [ ] **Step 1: 改版本号**（只动 Global Constraints 点名的 9 个，逐字符对照，不顺手升别的）

`web/package.json` 里把
`"element-plus": "^2.11.5"` → `"^2.14.7"`；`"@pureadmin/utils": "^2.6.2"` → `"^2.6.4"`；`"echarts": "^6.0.0"` → `"^6.1.0"`；`"vue": "^3.5.22"` → `"^3.5.44"`；`"vue-tsc": "^3.1.2"` → `"^3.3.13"`；`"sass": "^1.93.2"` → `"^1.105.1"`；`"typescript-eslint": "^8.46.2"` → `"^8.71.1"`；`"eslint-plugin-vue": "^10.5.1"` → `"^10.11.1"`；`"@tailwindcss/vite"` 与 `"tailwindcss"` 的 `"^4.1.16"` → `"^4.3.3"`。
**核对未被动过**：`"vite": "^7.1.12"`、`"vue-router": "^4.6.3"`、`"pinia": "^3.0.3"`、`"typescript": "^5.9.3"`、`"eslint": "^9.38.0"`、`"@vueuse/core": "^14.0.0"`、`"@pureadmin/table": "^3.3.0"`。

- [ ] **Step 2: 装并确认解析版本**

```bash
cd web && pnpm install && pnpm list element-plus @pureadmin/table @pureadmin/utils vue vite tailwindcss
```
预期：element-plus 2.14.7、@pureadmin/table **3.3.0**（若被间接抬到别的版本立即停——adaptive 的行为以 3.3.0 为准）、vite 仍是 7.x。

- [ ] **Step 3: 拉上游 v7 的 ReDialog 三个文件**

```bash
mkdir -p /tmp/v7/ReDialog
for f in index.vue type.ts index.ts; do
  curl -sS --fail -o /tmp/v7/ReDialog/$f \
    "https://raw.githubusercontent.com/pure-admin/vue-pure-admin/main/src/components/ReDialog/$f"
done
diff -u web/src/components/ReDialog/index.vue /tmp/v7/ReDialog/index.vue | head -80
```
预期：diff 落在 `el-dialog` 的 prop 名、`close`/`closed` 回调签名、`ElMessage` 调用形式三处附近。`--fail` 保证 404 不会写出空文件把本地覆盖成垃圾。

- [ ] **Step 4: 整体替换 + 剥 i18n**

```bash
cp /tmp/v7/ReDialog/{index.vue,type.ts,index.ts} web/src/components/ReDialog/
cd web/src/components/ReDialog
grep -n "transformI18n\|plugins/i18n\|\$t(\|[^a-zA-Z]t(" *.vue *.ts
```
按 grep 结果逐处改：删除 `import { transformI18n } from "@/plugins/i18n";`；`transformI18n(x)` → `x`；`$t("中文.key")` → 对应中文字面量（本仓弹窗文案：`"提示"`、`"取消"`、`"确定"`）。改完再 grep 一次，预期**零命中**。
另外核对 `index.ts` 的导出集合仍是 `{ ReDialog, dialogStore, addDialog, closeDialog, updateDialog, closeAllDialog }` 且 `DialogOptions` 等类型仍导出——上游若删了某个导出，**保留本地的旧签名**并记为 `L1-2` 待议，别连锁改调用方。

- [ ] **Step 5: 门禁 + 弹窗冒烟**

```bash
cd web && pnpm typecheck && pnpm lint:eslint && pnpm build
```
预期全 0。然后真实浏览器走一遍现存弹窗（详情页的输出查看抽屉/对话框仍走 ReDialog 的那几条路径），确认能开能关、`beforeSure` 校验生效、关闭后焦点归还表格。

- [ ] **Step 6: 提交**

```bash
git add web/package.json web/pnpm-lock.yaml web/src/components/ReDialog
git commit -m "chore(web): 追平上游 v7 线 minor 依赖并移植 ReDialog（L1-2）

element-plus 2.11.5→2.14.7、@pureadmin/utils→2.6.4、echarts→6.1.0、
vue→3.5.44、vue-tsc→3.3.13、sass→1.105.1、tailwind→4.3.3。
majors（vite8/router5/pinia4/TS6/eslint10）按设计文档 §9 留给下一期。
ReDialog 整体替换为 v7 实现并剥掉 i18n——与依赖升级同提交，
中间态不会留下「新 element-plus + 旧 ReDialog」的红灯。"
```

---

### Task 4: 移植 v7 RePureTableBar（列固定 + 剥 i18n）

**Files:**
- Modify: `web/src/components/RePureTableBar/src/bar.tsx`

**Interfaces:**
- Consumes: element-plus ≥2.14（Task 3）
- Produces: `PureTableBar`（`src/components/RePureTableBar/index.ts` 的 `withInstall` 导出名不变）。契约固定为：props `title`/`tableRef`/`columns`/`isExpandAll`/`tableKey`；emits `refresh`/`fullscreen`；slots `title`、`buttons`、**默认作用域插槽回传 `{ size, dynamicColumns }`**。Task 6–10 的每页都依赖 `dynamicColumns`——列显隐与固定由 bar 持有，页面必须把它传给 `pure-table :columns`，**不是**直接传 `columns`。

- [ ] **Step 1: 确认 `index.ts` 不用改**

```bash
cd web && curl -sS --fail "https://raw.githubusercontent.com/pure-admin/vue-pure-admin/main/src/components/RePureTableBar/index.ts" | diff - src/components/RePureTableBar/index.ts
```
预期：无差异或仅注释差异 → `index.ts` 保持。若上游多导出了东西（如 `ReVxeTableBar`），**不采纳**：本仓不引 vxe-table。

- [ ] **Step 2: 替换 bar.tsx**

```bash
curl -sS --fail -o web/src/components/RePureTableBar/src/bar.tsx \
  "https://raw.githubusercontent.com/pure-admin/vue-pure-admin/main/src/components/RePureTableBar/src/bar.tsx"
```

- [ ] **Step 3: 剥 i18n 并补齐它引入的新依赖**

```bash
cd web/src/components/RePureTableBar/src
grep -n "transformI18n\|plugins/i18n\|\$t(\|PinAngle\|pin-angle\|handleToggleColumnFixed" bar.tsx
```
- i18n 同 Task 3 Step 4 处理，改完预期零命中。
- 列固定用到的图标：本仓 `@iconify/json` 已在依赖里，`bi/pin-angle`、`bi/pin-angle-fill` 属 `bi` collection，无需新增包。若报图标缺失，改用已注册的 `ep/*` 等价图标并在提交信息注明，别引 `unplugin-icons` 新集合。
- 上游若 `import` 了本仓不存在的模块（除 i18n 外），逐个 `grep -rn` 定位后**优先删该功能分支**而非新建模块——本轮要的是四枚工具按钮与列固定，不是上游全部能力。

- [ ] **Step 4: 门禁**

```bash
cd web && pnpm typecheck && pnpm lint:eslint && pnpm build
```
预期全 0。（此任务尚无调用方，绿即可；真验证在 Task 6。）

- [ ] **Step 5: 提交**

```bash
git add web/src/components/RePureTableBar/src/bar.tsx
git commit -m "feat(web): 移植 v7 的 PureTableBar（列固定 + 剥 i18n）（L1-3）"
```

---

### Task 5: `kk.scss` 布局地基

**Files:**
- Modify: `web/src/style/kk.scss:45-89`（`--kk-page-h` 注释、`.kk-page*`、`.kk-fill-table`、`.kk-sticky-bar`、`.kk-side`）
- Modify: `web/src/style/kk.scss` 末尾追加范式类

**Interfaces:**
- Produces: 类名 `.kk-main`、`.kk-search-form`、`.kk-select-banner`、`.reset-margin`（Task 6–10 的模板直接用）。`.kk-fill-table` 与 `.kk-sticky-bar` **本任务不删**——6 页还挂着，删了会让中间提交出现无样式类名；Task 10 收尾时随最后一次引用一起删。

- [ ] **Step 1: 补 P0-1 的一行**（两个类共挂同一元素时，方向不该依赖「另一类恰好没声明」）

`web/src/style/kk.scss:78` 的 `.kk-side` 块内加 `flex-direction: row;`，放在 `display: flex;` 之后。媒体查询（L85-89）里的 `column` 覆盖**不动**。

- [ ] **Step 2: 给 `.kk-page` 的注释补上适用面**

`--kk-page-h` 的地基在 P1 后**只服务非列表页**（详情/首页/系统统计）。把 L9-18 那段注释的最后一句改为：「列表页改用 `@pureadmin/table` 的 `adaptive` 视口测量，不再依赖本锚点；锚点保留给多卡堆叠的详情/首页/统计页。」

- [ ] **Step 3: 追加范式类**

在 `kk.scss` 的「布局基建」小节末尾插入（值取自开源 `views/system/user/index.vue` 的 scoped 样式与 `bar.tsx` 的容器类，一次定义六页复用）：

```scss
/* ---- 列表页范式（对齐 vue-pure-admin v7 的 views/system/{user,role,menu}）----
 * 开源那四页根节点是 div.main、卡片观感由 PureTableBar 的 bg-bg_color 容器伪成，
 * 所以 el-card 的 overflow:hidden 与 block body 从来不是它们的问题——本仓 10-15
 * 实测的 P0-2/P1-1/P2-1 三条同源缺陷在这套结构里不存在。表格填高交给 @pureadmin/table
 * 的 adaptive（视口绝对测量），不要再挂 .kk-fill-table。
 */
.kk-main {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

/* 搜索条：与开源逐字同构（bg-bg_color 是它的卡片替身）。
   注意：本文件是全局样式，**不能**用 :deep()——那是 SFC scoped 的专属语法，
   在全局 scss 里会原样编译成无效选择器、静默不生效（10-15 的「安慰剂类名」同族坑）。 */
.kk-search-form .el-form-item {
  margin-bottom: 12px;
}

/* 选中横幅：取代 .kk-sticky-bar（sticky 的 scrollport 是 el-card，而它自己不滚，
   10-15 §3.2 实验 2 已证明放开 overflow 才能吸住、而 overflow 承担圆角裁剪） */
.kk-select-banner {
  display: flex;
  gap: 12px;
  align-items: center;
  height: 46px;
  padding-left: 16px;
  margin-bottom: 8px;
  background: var(--el-fill-color-light);
}

/* 行内按钮组去左边距（Element Plus 相邻按钮默认 12px，列宽会撑爆） */
.reset-margin {
  margin-left: 0;
}
```
**不要**写 `.main-content { margin: 24px 24px 0 !important }` 这类每页覆盖（Task 6 原样板里有，已删）。理由：`.main-content` 是 `lay-content` 渲染的**页面组件的祖先**元素，子组件的 scoped 样式和 `:deep()` 都只作用于自身渲染的后代，够不到祖先——这行在全仓会**静默不生效**，正是要避免的安慰剂 CSS。开源那条能生效是因为它的写法与本仓不同，照搬前先验证。页底留白差异由 `adaptive` 的 `offsetBottom` 实测吸收（验收口径是「分页器在视口内」，不是「margin 等于 24px 0」）。

- [ ] **Step 4: 门禁 + 单页目视**

```bash
cd web && pnpm lint:stylelint && pnpm typecheck && pnpm build
```
预期全 0。新类此时仍无调用方，所以**除 R1 外**的基线应当原样复现——`sideCols` 是**唯一会合理转绿的指标**（Step 1 就是修它的：两栏 `left` 从相同变为不同、宽比≈34%:66%）。其余任何一条在 Task 5 就变绿都说明测错了对象，回 Task 2 复核探针，别往下走。把 Task 5 之后的 `sideCols` 读数记进提交信息，作为 P0-1 的第一个证据。

- [ ] **Step 5: 提交**

```bash
git add web/src/style/kk.scss
git commit -m "fix(web): .kk-side 补 flex-direction:row 修双栏坍塌，并落列表页范式类（P0-1/L1-4）

.kk-page__body 声明 column、.kk-side 不声明方向，两个类共挂同一元素时
计算值恒为 column——A5.3 的「左执行/右结果」自落地以来从未横向排列过。
同批新增 .kk-main/.kk-search-form/.kk-select-banner/.reset-margin，
.kk-fill-table 与 .kk-sticky-bar 留待 6 页迁完后随最后一条引用删除。"
```

---

### Task 6: 审计页样板改造（范式在此定形）

**Files:**
- Create: `web/src/views/audit/utils/hook.tsx`
- Modify: `web/src/views/audit/index.vue`（整文件重写）

> 本任务**不建 `utils/types.ts`**：审计页是只读页，没有弹窗表单，`FormItemProps` 之类没有消费方——写一份没人 import 的类型就是 10-15 文档点名的「安慰剂文件」。P3 的三张 RBAC 页有表单，那时再建。

**Interfaces:**
- Consumes: `PureTableBar`（Task 4）、`addDialog`（Task 3）、`.kk-main`/`.kk-search-form`/`.reset-margin`（Task 5）、`@/api/audit` 的 `listAudit({limit,offset,keyword})`/`parseDetail`、`@/utils/kk` 的 `tsText`/`errText`/`downloadBlob`/`fileStamp`、`@/utils/kkPoll` 的 `useSeq`
- Produces: `useAuditLog()` 返回 `{ formRef, form, columns, dataList, loading, pagination, onSearch, resetFields, onExport, exporting, handleSelectionChange }`；`audit/index.vue` 的模板结构是 Task 7–10 的复制源

> 这页先做：它只有 5 列、无批量勾选、无图表，是验证 `adaptive` + `PureTableBar` + 分页三件套是否真能咬合的**最小完整用例**。范式在这里定形，后面五页照抄。

- [ ] **Step 1: 先定「条数」的单一状态源（本任务唯一的判断题，已裁决）**

原页面用 `limit`/`offset` 两个 ref 表达分页；迁移后 `pure-table` 的 `pagination` 自带 `pageSize`/`currentPage`。**两处都能改 = 必然漂移**，所以本计划裁决（Task 8、9 沿用同一条）：

- **`pagination` 是唯一来源**。删除 `form.limit`，搜索条里**不放**条数下拉——条数交给分页器的 `sizes`（`pageSizes: [100, 200, 500]`），与开源一致。
- `offset` 由 `(currentPage - 1) * pageSize` 推导，不再单独持有。
- `watch(pagination.pageSize)` 继续承担「条数变了重置到第 1 页并重载」，`@page-size-change`/`@page-current-change` 里**只调 `onSearch()`，不再赋值** `currentPage`/`pageSize`（pure-table 已改过，重复赋值打两遍后端——FE-8 同族）。
- `read` 位（QR-W16「没读到 ≠ 暂无审计记录」）随 hook 一起导出，模板的 `#empty` 继续用它。

下面 Step 2/3 的代码即按这条裁决书写。

- [ ] **Step 2: `utils/hook.tsx` 写列定义与数据流**

```tsx
import { onBeforeUnmount, reactive, ref, onMounted, watch } from "vue";
import type { PaginationProps } from "@pureadmin/table";
import { message } from "@/utils/message";
import { listAudit, parseDetail, type AuditRow } from "@/api/audit";
import { exportAudit } from "@/api/exporting";
import { downloadBlob, errText, fileStamp, tsText } from "@/utils/kk";
import { useSeq } from "@/utils/kkPoll";

/** 动作色档：沿用原页面的判级，一处都不改语义（QR-W16 只关心「没读到」，
 *  这里关心「读到了算哪档」）。 */
const actionType = (a: string) => {
  if (a.includes("fail") || a.includes("blocked") || a.includes("rejected"))
    return "danger";
  if (a.includes("mismatch") || a.includes("timeout")) return "warning";
  if (a.includes("ok") || a.includes("create") || a.includes("restore"))
    return "success";
  return "info";
};

export function useAuditLog() {
  const form = reactive({ keyword: "" });
  const loading = ref(false);
  const exporting = ref(false);
  const dataList = ref<AuditRow[]>([]);
  /** 读到过真值没有：「暂无审计记录」是强断言（等于说没人操作过），
   *  读不到时必须与「确实为空」分开（QR-W16 红线）。 */
  const read = ref(false);
  const pagination = reactive<PaginationProps>({
    total: 0,
    pageSize: 200,
    currentPage: 1,
    pageSizes: [100, 200, 500],
    background: true
  });

  const columns: TableColumnList = [
    { label: "#", prop: "id", width: 80 },
    {
      label: "时间",
      width: 170,
      cellRenderer: ({ row }) => <span class="kk-num">{tsText(row.ts)}</span>
    },
    { label: "操作者", prop: "actor", width: 140 },
    {
      label: "动作",
      width: 180,
      cellRenderer: ({ row }) => (
        <span class={`kk-state ${row.action.includes("fail") ? "kk-bad" : ""}`}>
          {row.action}
        </span>
      )
    },
    {
      label: "明细",
      minWidth: 320,
      align: "left",
      cellRenderer: ({ row }) => (
        <>
          {Object.entries(parseDetail(row.detail)).map(([k, v]) => (
            <span key={k} class="kk-kv">
              <b>{k}</b>:{" "}
              {typeof v === "object" ? JSON.stringify(v) : String(v)}
            </span>
          ))}
          {!row.detail && <span class="kk-sub">-</span>}
        </>
      )
    }
  ];

  const beginLoad = useSeq();

  async function onSearch() {
    const isLatest = beginLoad();
    loading.value = true;
    try {
      // 关键字下推后端（store._audit_filters），与导出共用同一套语义：所见即所得
      const data = await listAudit({
        limit: pagination.pageSize,
        offset: (pagination.currentPage - 1) * pagination.pageSize,
        keyword: form.keyword.trim() || undefined
      });
      if (!isLatest()) return;
      dataList.value = data.items;
      pagination.total = data.total;
      read.value = true;
    } catch (e: any) {
      if (!isLatest()) return;
      message("加载审计日志失败：" + errText(e), { type: "error" });
    } finally {
      if (isLatest()) loading.value = false;
    }
  }

  /** 关键字 300ms 防抖：输入停顿才打后端（原页行为保持） */
  let kwTimer: ReturnType<typeof setTimeout> | null = null;
  watch(
    () => form.keyword,
    () => {
      if (kwTimer) clearTimeout(kwTimer);
      kwTimer = setTimeout(() => {
        pagination.currentPage = 1;
        onSearch();
      }, 300);
    }
  );
  onBeforeUnmount(() => kwTimer && clearTimeout(kwTimer));

  // 条数变化只赋值，重载由下面这一处统一发起（FE-8：两处都调会打两遍后端）
  watch(
    () => pagination.pageSize,
    () => {
      pagination.currentPage = 1;
      onSearch();
    }
  );

  async function onExport() {
    exporting.value = true;
    try {
      const blob = await exportAudit({
        keyword: form.keyword.trim() || undefined,
        limit: pagination.pageSize
      });
      downloadBlob(blob, `审计日志_${fileStamp()}.csv`);
    } catch (e: any) {
      message("导出失败：" + errText(e), { type: "error" });
    } finally {
      exporting.value = false;
    }
  }

  function resetFields(formEl?: any) {
    if (!formEl) return;
    formEl.resetFields();
    pagination.currentPage = 1;
    onSearch();
  }

  onMounted(onSearch);

  return {
    form,
    columns,
    dataList,
    loading,
    read,
    exporting,
    pagination,
    onSearch,
    onExport,
    resetFields
  };
}
```
`message` 来自 `@/utils/message`（thin 自带），不再直调 `ElMessage`——静默轮询不刷屏的既有约定靠它保证。

- [ ] **Step 3: `index.vue` 重写**

```vue
<script setup lang="ts">
import { useAuditLog } from "./utils/hook";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { ref } from "vue";

defineOptions({ name: "AuditLog" });

const tableRef = ref();
const {
  form,
  columns,
  dataList,
  loading,
  read,
  exporting,
  pagination,
  onSearch,
  onExport,
  resetFields
} = useAuditLog();

function onSelectionChange(rows: any[]) {
  // 审计页无批量操作：保留回调仅为 PureTableBar 的 tableRef 契约稳定
  void rows;
}
</script>

<template>
  <div class="kk-main">
    <el-form
      :inline="true"
      :model="form"
      class="kk-search-form bg-bg_color w-full pl-8 pt-3 overflow-auto"
    >
      <el-form-item label="关键字：" prop="keyword">
        <el-input
          v-model="form.keyword"
          placeholder="按操作者 / 动作 / 明细过滤"
          clearable
          class="w-45!"
        />
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          :icon="useRenderIcon('ri/search-line')"
          @click="onSearch"
          >搜索</el-button
        >
        <el-button :icon="useRenderIcon('ep/refresh')" @click="resetFields"
          >重置</el-button
        >
      </el-form-item>
    </el-form>

    <PureTableBar
      title="审计日志"
      :columns="columns"
      :table-ref="tableRef?.getTableRef()"
      @refresh="onSearch"
    >
      <template #buttons>
        <el-button
          type="primary"
          :icon="useRenderIcon('ep/download')"
          :loading="exporting"
          @click="onExport"
          >导出 CSV</el-button
        >
      </template>
      <template v-slot="{ size, dynamicColumns }">
        <pure-table
          ref="tableRef"
          row-key="id"
          adaptive
          :adaptiveConfig="{ offsetBottom: 108 }"
          align-whole="center"
          table-layout="auto"
          :loading="loading"
          :size="size"
          :data="dataList"
          :columns="dynamicColumns"
          :pagination="{ ...pagination, size }"
          :header-cell-style="{
            background: 'var(--el-fill-color-light)',
            color: 'var(--el-text-color-primary)'
          }"
          @selection-change="onSelectionChange"
          @page-size-change="() => onSearch()"
          @page-current-change="() => onSearch()"
        >
          <template #empty>
            <el-empty
              class="min-h-200"
              :image-size="60"
              :description="
                read
                  ? form.keyword.trim()
                    ? '该筛选条件下没有匹配记录'
                    : '暂无审计记录'
                  : '没读到（审计接口不可达）'
              "
            />
          </template>
        </pure-table>
      </template>
    </PureTableBar>
  </div>
</template>
```

三条必须照办的细节（都是本仓踩过的位置）：
1. **条数只有一个来源**（Step 1 已裁决）：模板里没有条数下拉，`form` 里也没有 `limit`，只有 `pagination.pageSize`。原页面的 `limit`/`offset` 两个 ref 全部由 `pagination` 取代。
2. `@page-size-change`/`@page-current-change` 里 **不要再改 `pagination.currentPage`**，pure-table 已改过，重复赋值会打两遍后端（FE-8 同族坑）。
3. `#empty` 若 pure-table 不透传（3.3.0 支持转发，但以实测为准），退路是 `:pagination="false"` 外加 el-table 的 `empty-text`；**不许**为了绕过而把「没读到」断言弱化成默认「暂无数据」——那是 QR-W16 的回归。

**本任务不需要 `<style>` 块**：`.el-form-item` 的 12px 下边距由 Task 5 的全局 `.kk-search-form` 提供；`.main-content` 的 margin 覆盖已按 Task 5 的裁决删除（子组件够不到祖先，写了也静默无效）。若模板里确实出现本页独有的样式，才加 `scoped` 块。

- [ ] **Step 4: 跑门禁**

```bash
cd web && pnpm typecheck && pnpm lint:eslint && pnpm build
```
预期全 0。typecheck 若报 `TableColumnList` 未定义，说明 `types/global.d.ts` 没被纳入 `tsconfig.include`——不要改成显式 import 绕过，先确认为何样板页之外的其他页能用到它。

- [ ] **Step 5: 几何复测（此页应转绿）**

```bash
playwright-cli -s=layout goto http://127.0.0.1:8849/#/audit/index
playwright-cli -s=layout eval "$(cat scripts/fe_layout_probe.js)"
```
预期：`pageScrolls:false`、`pagerRect.bottom <= scrollHeights.client`（**不用 `reachableAt` 判据**，它在 1920 下恒真）、`tableRect.height` 在 600~900 之间，且测量前先 settle ~2s。然后 **1366×768 复测**（`resize 1366 768`）：分页器仍须在视口内——这是 `offsetBottom: 108` 的取值依据，若被裁就调大该值并在提交信息里写下实测数值与调整原因（不许照抄开源的 108）。

- [ ] **Step 6: 功能回归（人眼 + 探针之外）**

真实浏览器逐条过：搜索防抖一次只打一发请求（看 Network）、重置回第 1 页、翻页数据对得上、导出 CSV 行数与页面 `total` 一致、后端宕掉时 `description` 显示「没读到」而不是「暂无审计记录」、列设置里取消一列再勾回、密度切换、全屏按钮进出、刷新按钮重取。

- [ ] **Step 7: 提交**

```bash
git add web/src/views/audit
git commit -m "refactor(web): 审计页改开源列表页范式，表格填高换 adaptive（P0-2/P1-1/P2-1 样板/L1-5）"
```

---

### Task 7: 命令中心（双栏 + 执行历史）

**Files:**
- Modify: `web/src/views/command/components/CommandWorkbench.vue`
- Create: `web/src/views/command/components/utils/hook.tsx`
- Modify: `web/src/views/command/components/CommandHistory.vue`

**Interfaces:**
- Consumes: Task 6 定形的 `PureTableBar` + `pure-table[adaptive]` 模板；`CommandHistory` 现有的 `loadCommands`/`loadBatches`/`showOut`/`focusBatch`/`rowClass`/`restartTimer`/`POLL_KEY`/`defineExpose({ reload, focusBatch })`
- Produces: `CommandHistory` 对外契约**不变**（`defineExpose({ reload: () => loadCommands(), focusBatch })`），因为 shell/collect 两页的 ref 调用依赖它

- [ ] **Step 1: 保住不变量清单**

先 `git log --oneline -1 -- web/src/views/command` 与 `grep -n "useSeq\|pollFailed\|kk-sync\|autoRefresh\|usePolls\|clearPoll\|restartTimer" web/src/views/command/components/CommandHistory.vue`，把命中的行号抄进本次提交信息。**`defineExpose` 那一行一个字不许改**。

- [ ] **Step 2: `CommandWorkbench.vue` 保留 `.kk-page`+`.kk-side`，只补链路**

本页是双栏容器不是列表页：根仍是 `.kk-page`（Task 5 已修 `flex-direction`），左栏卡片保留 `el-card`（表单需要卡片观感）。右栏改为不再要求子元素撑高：把 `.kk-col--result` 的 `flex: 1; min-width: 0;` 保留，**删掉** `.kk-fill :deep(.el-card__body)` 这条只在左栏生效的补丁所暗示的「右栏也要填满」预期——右栏现在由 `adaptive` 自己定高。改动仅限 `template` 的类名与 `style scoped`，不动 slot 约定（`#form`/`#result`）。

- [ ] **Step 3: `CommandHistory.vue` 迁移**

按 Task 6 的结构整页替换：根 `el-card.kk-card.kk-page__body` → `div.kk-main`；`#header` 的 `kk-toolbar` → 搜索条 `el-form.kk-search-form`（关键字 `keyword`、状态 `statusFilter`、批次 `batchFilter`、自动刷新 `autoRefresh` 复选、刷新/导出按钮进 `PureTableBar` 的 `#buttons`）；表头标题里的 `batchStat` 与 `kk-sync` 读数放进 `#title` 插槽（`PureTableBar` 有 `title` slot）；`el-table` + 11 个 `el-table-column` → `columns` 数组（进 `utils/hook.tsx`）+ `pure-table adaptive`。

列迁移对照（`prop`/`label`/`width` 逐条照搬，`cellRenderer` 用原模板内容改写）：

| 原列 | 新列写法要点 |
|---|---|
| ID `width=120` | `{ label:"ID", prop:"id", width:120 }`，`cellRenderer` 包 `class="kk-num"` |
| 主机 | `{ label:"主机", prop:"pod", minWidth:150 }`，**必须 `align:"left"`**（主机名长，居中会让扫读基准线漂移） |
| 批次 | `cellRenderer` 用 `<el-tooltip>` 包 `<el-link onClick={e=>{e.stopPropagation(); batchFilter.value=row.batch_id}}>` —— 原代码的 `@click.stop` 不能丢，否则会触发整行 `row-click` 弹输出 |
| 类型 | `{ label:"类型", prop:"kind", width:90 }` |
| 命令/采集项 | `{ minWidth:200, showOverflowTooltip:true, cellRenderer:()=>argvPreview(row.argv) }` |
| 状态 | `cellRenderer` 用 `<span class="kk-state">` 家族（**不用 `el-tag`**，Global Constraints），保留 `@click.stop` |
| rc / 耗时 / 输出 / 创建 | `width:70/90/180/160`，输出列 `showOverflowTooltip`，`rc===null → "-"` 的判空保留 |
| 操作 `fixed="right"` | `{ fixed:"right", width:90, slot:"operation" }` + 模板 `<template #operation="{ row, size }">` 里 `class="reset-margin" link type="primary" :size="size"` |

- [ ] **Step 4: `row-click` 与 selection**

`pure-table` 透传 `@row-click`，保留「点行看输出」。整行点击仍是鼠标专属（FE-17 的既定取舍），不动。

- [ ] **Step 5: 门禁 + 几何 + 功能**

```bash
cd web && pnpm typecheck && pnpm lint:eslint && pnpm build
playwright-cli -s=layout goto http://127.0.0.1:8849/#/command/shell
playwright-cli -s=layout eval "$(cat scripts/fe_layout_probe.js)"
```
预期（**控制基线已随 Task 5 的 `.kk-side` 修复更新，不再是 `167bb84` 的 shell 读数**）：
- **R1 现在是回归检查而非待修项**——Task 5 已把双栏修好（实测 `sideCols` `[{210,581},{807,1113}]`，34.3:65.7）。本页迁移后两栏必须**仍是**两个不同 `left`；若变回相同，说明迁移把 `.kk-side`/`.kk-page__body` 的共挂结构改坏了。
- **R2/R3 才是本页要转绿的**：迁移前 shell 页 `pageScrolls` 为 2128/999、`pagerRect.bottom` 约 1856（仍在视口外）、`canScroll:false`；迁移后要求 `pageScrolls:false`、`pagerRect.bottom <= scrollHeights.client`、表内可滚。**`reachableAt` 不作为判据**（1920 下恒 `[true,true,true]`，见 Global Constraints）。
- 1100×800 下两栏须堆叠（`.kk-side` 的媒体查询本意），这条 Task 5 已实测，本页迁移后不得失效。
功能必测：下发一批命令后新批次行可见、批次统计读数随轮询更新、勾掉「自动刷新」后定时器真停（`clearPoll`）、导出范围与筛选一致、点行开输出框、连点两行不出现输出错配（`useSeq`）。

- [ ] **Step 6: 提交**

```bash
git add web/src/views/command
git commit -m "fix(web): 命令中心双栏生效且执行历史填满视口（P0-1/P0-2 收口/L1-6）

10-15 实测：历史卡 1797px 底边在视口外 1129px、wrap.scrollTop 写 600 回读 0、
elementFromPoint(960,1500)=null——分页器与表格下半从未能点到。
换 adaptive 视口测量后不再依赖祖先高度链，el-card 的 overflow:hidden 失去杀伤力。"
```

---

### Task 8: 版本与更新页

**Files:**
- Create: `web/src/views/host/update/utils/hook.tsx`
- Modify: `web/src/views/host/update/index.vue`
- Modify: `web/src/views/host/update/form.vue`（如已有上传表单；无则新建）

**Interfaces:**
- Consumes: Task 6 范式；`@/api/agent` 的清单/上传/回滚/推送；`upgradeSkipText`（`@/utils/kk`）
- Produces: 两张表（台账表 + 主机版本表）各自的 `columns`，同页共存时 `adaptive` 的 offsetBottom **必须分别取值**

- [ ] **Step 1: 结构决策（写进提交信息）**

本页原有两张表 + `kk-sticky-bar`。两张表同屏都开 `adaptive` 会互相抢高度（视口测量按各自 `getBoundingClientRect().top` 算，前一张撑开后一张就溢出）。**采用**：台账表（上方、行数少）保留自然高度并 `:max-height="320"`；主机版本表（下方、主表）用 `adaptive` + 实测 offsetBottom。**不要**两张都 adaptive——这是本页与开源单页结构的必要偏离，理由写进注释。

- [ ] **Step 2: 迁移**

按 Task 6 范式改主表；`kk-fill-table` 两处全部去掉；`.kk-sticky-bar` 改 `.kk-select-banner`。上传/回滚/推送按钮进 `PureTableBar` 的 `#buttons`；「跳过原因」继续用 `upgradeSkipText`（不许在前端重抄枚举——QR-W11 判例：抄一份枚举就让 HEAD 红了一轮）。

- [ ] **Step 2b: 首次真实调用 addDialog 的验证责任**

**这是 `addDialog` 在本仓的第一次真实调用**（Task 3 查明此前零消费者，`beforeSure`/焦点归还从未被浏览器验证过，且 v7 移植净增的 12 个 `DialogProps` 只是类型可见、从未渲染）。本任务的弹窗必须**至少实际用到其中一个新增 prop** 并在几何复测时确认渲染行为与类型声明一致——不要把类型面当成已验证。同时这也是 `el-dialog` 在 element-plus 2.14 下的首次真开真关：确认能开能关、`beforeSure` 校验生效、关闭后焦点归还表格，并把这几项写进报告，补上 Task 3 留下的验证缺口。

- [ ] **Step 3: 门禁 + R7 + 功能**

```bash
cd web && pnpm typecheck && pnpm lint:eslint && pnpm build && cd ..
playwright-cli -s=layout goto http://127.0.0.1:8849/#/hosts/update
playwright-cli -s=layout eval "$(cat scripts/fe_layout_probe.js)"
```
预期 `pageScrolls:false`（R7）。功能必测：上传新版本、跳过原因可见、回滚二次确认、推送后轮询状态推进、±20% 抖动轮询的 `pollFailed` 页头读数。

- [ ] **Step 4: 提交**

```bash
git add web/src/views/host/update
git commit -m "refactor(web): 版本与更新页改列表范式（P2-1/L1-7）"
```

---

### Task 9: 主机总览页

**Files:**
- Create: `web/src/views/host/monitor/utils/hook.tsx`
- Modify: `web/src/views/host/monitor/index.vue`
- Modify: `web/src/views/host/monitor/components/*`（若有抽屉子组件）

**Interfaces:**
- Consumes: Task 1 落地后的 monitor 页（QR-S36 的筛选/排序下推参数、`HostFilters` 契约）、`kk-band` 仪表带、`usePolls`
- Produces: 全站最重要的勾选批量路径（`kk-select-banner` 取代 `kk-sticky-bar`）

- [ ] **Step 1: 重新读文件，别照抄本计划里的行号**

Task 1 刚改过这页（+289 行）。先 `Read web/src/views/host/monitor/index.vue` 全文，按**当前**内容定位，行号一律以实际读到的为准。

- [ ] **Step 2: 分层迁移**

`.kk-band` 仪表带是本页「整屏最重要的判断」，必须**常驻视口顶部**（R5）。结构：
```
div.kk-main
├─ div.kk-band           ← 常驻，不参与 adaptive 计算
├─ el-form.kk-search-form ← 分组/主机类型/仅 Docker 异常/仅告警/关键字/排序
└─ PureTableBar → pure-table(adaptive, offsetBottom=实测)
```
`offsetBottom` 从 108 起调，按实测（band 高 + 搜索条高 + 分页器高 + chrome）逐步加大，两档分辨率下分页器都必须在视口内。取到的值写进 `:adaptiveConfig` 并在注释里标「实测：1920×1080 / 1366×768 两档分页器均可见」。

- [ ] **Step 3: 批量栏**

`el-table` 的 selection 列进 `columns`（**必须写 `label`**：`{ label: "勾选列", type: "selection", fixed: "left", reserveSelection: true }`——裸 `{ type: "selection" }` 会被 `getKeyList` 静默丢弃，导致弹层条目与列下标错位、拖拽排序移错列，这是 Global Constraints 里点名的一条）。`reserveSelection` 保证翻页不丢勾选（FE-7「显示 10 台实发 1 台」的同类风险）。选中横幅用 `.kk-select-banner`，批量下发/批量采集按钮放里面；横幅位置在 `PureTableBar` 默认插槽内、`pure-table` 之上（与开源选中横幅同位）。

- [ ] **Step 4: 迁移列**

按现有列逐条进 `columns`，`kk-meter`（磁盘）、`kk-ticks`（心跳新鲜度）、`kk-state`（四档在线态）、`kk-warn`（版本落后）继续用现有类名与现有判级函数（`statusType`/`statusLabel`/`ageText` 等来自 `@/utils/kk`），**语义一行不改**。`kk-row-alert`/`kk-row-fresh` 的行类走 `:row-class-name="rowClass"`，`rowClass` 原样搬。

- [ ] **Step 5: 门禁 + R4/R5/R6 + 500 台**

```bash
cd web && pnpm typecheck && pnpm lint:eslint && pnpm build && cd ..
playwright-cli -s=layout goto http://127.0.0.1:8849/#/hosts/monitor
playwright-cli -s=layout eval "$(cat scripts/fe_layout_probe.js)"
```
预期：`pageScrolls:false`（R4）、`bandTop≈0`（R5）、`stickyTop:null` 且横幅在视口内（R6）。
功能必测：500 台分页正确、筛选/排序下推生效（Network 里能看到 query）、勾选跨页保留、批量下发与单行采集互不污染（FE-7）、仅告警/仅 Docker 异常三态布尔不被当成真值（QR-S36 点名的雷：`"false"` 在 Python 里是真值）。

**Task 1 评审带出的隐性红线（必须在本任务处理）**：`0238b9b` 之后，`online`/`alerts`/`outdated` 三个读数是在**返回的 `items` 上求和**（`server/src/kk_server/controllers/containers.py:135-138`），而 `total` 是服务端**按筛选集全量** count（`:124`）。当前不传 `limit/offset`，两者一致，读数诚实。**本任务一旦启用真后端分页，这三个数会静默退化成「本页读数」，却仍与全集 `total` 并排显示**——aria-label 与离线比例条跟着一起说谎。所以本任务的验收必须二选一：把三个读数改为 filtered-set 聚合（要动后端，属 P1 之外的新增），或明确把它们标成「本页」。**若走后者，文案与 `kk-band` 的标签必须同时改**，不许留一个看起来像全集的数字。

- [ ] **Step 6: 提交**

```bash
git add web/src/views/host/monitor
git commit -m "refactor(web): 总览页仪表带常驻 + 表格内滚，批量栏不再靠 sticky（P1-1/P1-2/L1-8）"
```

---

### Task 10: 首页、主机详情、系统统计（保留卡片，清误用）

**Files:**
- Modify: `web/src/views/welcome/index.vue`
- Modify: `web/src/views/host/detail/index.vue`
- Modify: `web/src/views/system/index.vue`
- Modify: `web/src/style/kk.scss`（**本任务删掉 `.kk-fill-table` 与 `.kk-sticky-bar`**——此时全仓引用应为零）

**Interfaces:**
- Consumes: `.kk-page`/`.kk-page__body` 锚点（这三页是多卡堆叠，flex 链本来就是对的）
- Produces: 全仓 `kk-fill-table`/`kk-sticky-bar` 引用清零

- [ ] **Step 1: 先证明清零，再删类**

```bash
cd web && grep -rn "kk-fill-table\|kk-sticky-bar" src/
```
预期：只剩 `src/style/kk.scss` 自身的定义。若还有页面命中，**说明 Task 6–9 有页没迁完**，回去补，不许先删类。

- [ ] **Step 2: welcome**

保留 `.welcome{padding:0}` 与「`height="100%"` + `:deep(.el-card__body){display:flex}`」这条仓内唯一正确范式（10-15 §P2-1 认定它是对的），只补根节点高度锚点：外层加 `.kk-page`（本页根无锚点是 10-15 记的窄屏 910 vs 687 的成因）。

- [ ] **Step 3: detail**

5 处硬编码 `max-height`（220/220/260/260/240）→ 统一绑到 `--kk-drawer-h` 同族的 flex 布局；`.kk-chart{height:300px}` 沿用现有 `useResizeObserver`，**不引 adaptive**（一页五表同屏，视口测量互相抢高度）。本页保留 `el-card`——它是多卡堆叠的详情页，不是列表页。

- [ ] **Step 4: system**

根节点加 `.kk-page` + `.kk-page__body`；4 处 `el-table` 里属于「卡内剩余空间」语义的挂 `.kk-page__body` 的 flex 链，小表格（计数器分组）保留自然高度。`pageScrolls` 允许为 `true`（读数页本就长）——**别为了指标好看把排障面板压成一屏**，那会让「什么值该担心」的说明文字被折掉。R7 对本页的验收改成：1080p 下无横向滚动、抽屉内表格不截断。

- [ ] **Step 5: 删类 + 门禁 + R8**

从 `kk.scss` 删除 `.kk-fill-table`（L61-65）与 `.kk-sticky-bar`（L67-75）两个块及其注释。
```bash
cd web && pnpm typecheck && pnpm lint:eslint && pnpm build
playwright-cli -s=layout resize 1366 768
playwright-cli -s=layout goto http://127.0.0.1:8849/#/hosts/detail/vscode-0000
playwright-cli -s=layout eval "$(cat scripts/fe_layout_probe.js)"
```
预期 R8：detail 在 768 高视口下五表无截断、无横向滚动。

- [ ] **Step 6: 提交**

```bash
git add web/src/views web/src/style/kk.scss
git commit -m "refactor(web): 首页/详情/统计页收进统一锚点，退役 kk-fill-table 与 kk-sticky-bar（P2-1 收口/L1-9）"
```

---

### Task 11: 全量绿灯、产物入库与文档收口

**Files:**
- Modify: `docs/frontend-layout-analysis-2026-10-15.md`（§4/§5 每条缺陷标注落地状态 + 复测数值）
- Modify: `docs/frontend-optimization-plan-2026-10-07.md`（§7 的 FE-5 状态从「未做」改为已修，附新机制说明）
- Modify: `server/src/kk_server/web/**`（构建产物整目录替换）
- Create: `docs/superpowers/specs/2026-10-28-frontend-upgrade-rbac-design.md` 的 §3.5 补一行实测值

**Interfaces:**
- Consumes: Task 1–10 全部完成
- Produces: RBAC 计划（P2/P3）开工的干净基线

- [ ] **Step 1: 两档分辨率全量复测**

```bash
for res in "1920 1080" "1366 768"; do
  set -- $res; playwright-cli -s=layout resize $1 $2
  for h in "#/hosts/monitor" "#/hosts/update" "#/audit/index" "#/command/shell" "#/command/collect" "#/welcome" "#/hosts/detail/vscode-0000"; do
    playwright-cli -s=layout goto "http://127.0.0.1:8849/$h" >/dev/null
    printf '%s %sx%s ' "$h" "$1" "$2"
    playwright-cli -s=layout eval "$(cat scripts/fe_layout_probe.js)"
  done
done
```
预期：R1–R9 全绿（system 页按 Task 10 Step 4 的修订口径）。**逐个数值贴进文档，不写「已验证通过」**——本仓判例：文档写「flex 生效」而 live 实测把它推翻了两次。

- [ ] **Step 2: 后端全量回归（确认前端改动没牵动协议）**

```bash
cd /e/GitHub/KontainKeeper && .venv/Scripts/python.exe -m pytest agent/tests server/tests -q --junitxml=/tmp/j.xml
```
预期：**397 passed / 0 skip**（Task 1 重新定的基线；Broker 不可达时允许 4 skip，但必须在结论里如实写明是哪种情况——0 skip 只在 WSL 真 Broker 路径下成立）。

- [ ] **Step 3: 产物整目录同步入库**

```bash
cd web && pnpm build
rm -rf ../server/src/kk_server/web/static ../server/src/kk_server/web/index.html \
       ../server/src/kk_server/web/*.js ../server/src/kk_server/web/*.css ../server/src/kk_server/web/*.svg ../server/src/kk_server/web/*.ico 2>/dev/null
cp -r dist/. ../server/src/kk_server/web/
cd .. && git status --porcelain server/src/kk_server/web | head
```
预期：只有构建产物的增删改，**且提交前必须逐文件核对两侧 sha256 汇总一致**：
```bash
find web/dist -type f -exec sha256sum {} \; | sed 's#web/dist/##' | sort > /tmp/a.txt
find server/src/kk_server/web -type f -exec sha256sum {} \; | sed 's#server/src/kk_server/web/##' | sort > /tmp/b.txt
diff /tmp/a.txt /tmp/b.txt && echo SYNCED
```
（`platform-config.json`/`favicon.ico`/`logo.svg` 两边都应有；FE-18 记过 `logo.svg` 仍是 pure-admin 图形，属设计活，本轮不动，别在同步时误删。）

- [ ] **Step 4: 文档收口**

`docs/frontend-layout-analysis-2026-10-15.md` 的 §4 五条（P0-1/P0-2/P1-1/P1-2/P2-1）各加一行「落地：`<commit>`，复测值：`<JSON 关键字段>`」；§5 开头声明路线变更——**原计划的「flex 链全站化」已被「@pureadmin/table adaptive」取代**，理由是同一缺陷用上游 sanctioned 机制修，不需要自研 `--kk-page-h` 铺开。`docs/frontend-optimization-plan-2026-10-07.md` §7 的 FE-5 从「未做」改「已修（机制换为 adaptive）」。

- [ ] **Step 5: 提交**

```bash
git add docs/ server/src/kk_server/web
git commit -m "docs(fe): P1 布局一期收口——R1–R9 全绿复测值入库，产物同步（L1-10）

原方案「--kk-page-h 全站铺开 + kk-fill-table flex 链」改为 @pureadmin/table
3.3.0 自带的 adaptive 视口测量：同一缺陷用上游 sanctioned 机制修，
少一套自研高度锚点要六页同步维护的负担。"
```

- [ ] **Step 6: 宣布 P1 完成，转 RBAC 计划**

P1 到此为可独立交付状态。P2（RBAC 后端）另写计划——它的表/seed/`require_perm` 不依赖 P1 产物，但 P3 的三张页**必须**照 P1 定形的范式（Task 6 样板）写，所以 RBAC 前端计划只能在 P1 落地后动笔。

---

## 自审结论（已就地修正）

1. **Spec 覆盖**：spec §2（P0）→ Task 1；spec §3.1（依赖）→ Task 3；§3.2（adaptive 换机制）→ Task 5–10；§3.3（页面结构规范）→ Task 6 定形、7–9 套用；§3.4（读数族保留 / bar.tsx 移植 / kk-side 一行 / detail 不用 adaptive / 不移植 ReDrawer）→ Task 4、5、10；§3.5（R1–R9 验收）→ Task 2 钉基线、每页复测、Task 11 全量。**spec §4/§5（RBAC）不在本计划**，已在 Task 11 Step 6 显式交接。
2. **占位符**：无 TBD/TODO；两处「以实测为准」的 `offsetBottom` 取值都给了起点（108）、判据（两档分辨率分页器可见）与记录要求（写进提交信息），属测量而非空缺。
3. **类型一致性**：`PureTableBar` 导出名（Task 1/4/6 一致）、默认插槽 `{ size, dynamicColumns }`（Task 4 契约 → Task 6/7/8/9 模板一致）、`useAuditLog()` 返回集合（Task 6 Step 2 定义 → Step 3 解构一致）、`defineExpose({ reload, focusBatch })`（Task 7 Step 1 锁死）已互相对齐。Task 6 Step 2 的 `watch`/`onBeforeUnmount` import 缺失已在同块内显式补齐说明。
