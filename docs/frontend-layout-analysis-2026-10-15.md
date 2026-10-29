# 前端布局专项分析（2026-10-15，live 实测）

> 指令：全量分析，重点针对布局优化。
> 方法：**真实浏览器（msedge）+ DOM 几何测量**，不是静态读码推断。
> 数据：后端灌入 500 主机 / 185 命令 / 300 审计 / 40 台账（`data/seed_analysis.py`，
> 幂等），SQLite `service/kk-analysis.db`，覆盖 500 台规模的真实行高。

---

## 1. 结论速览

| # | 级别 | 问题 | 一句话结论 |
|---|---|---|---|
| P0-1 | P0 | 命令中心双栏从未生效 | `.kk-side` 计算样式实为 `flex-direction: column`，左右栏上下堆叠、同宽同左边 |
| P0-2 | P0 | 命令中心内容被裁且不可达 | 历史卡 1797px、底边在视口外 1129px；`wrap.scrollTop` 置 600 仍为 0，**表格下半 + 分页器完全点不到** |
| P1-1 | P1 | 高度锚点只落地 1 页（FE-5 未做） | monitor 5469px / audit 6606px / update 2012px / system 1219px 整页滚，仪表带随滚走 |
| P1-2 | P1 | 吸底栏从未吸住 | monitor 滚到 2000px 时 sticky 元素 top=3447（视口外）；根因是 el-card 的 `overflow:hidden`，不是滚动容器 |
| P2-1 | P2 | 「表格填充」范式不统一 | 全仓仅 welcome 一页写法正确；其余 4 页的 `kk-fill-table` 全部落空（`.el-card__body` 是 block） |

**最重要的一条**：10-07 方案文档用静态分析断定「命令中心 `kk-fill-table` flex 生效、内部滚动、页面不滚」，
本轮实测**推翻**了该结论——命令中心既是双栏坍塌的，也是内容不可达的。静态推断在 CSS 层叠 +
flex 链路 + el-card 内部结构这三层上全部失灵，这正是本轮必须做 live 测量的原因。

---

## 2. 实测环境与方法

| 项 | 值 |
|---|---|
| 浏览器 | msedge（playwright-cli 驱动，真实渲染，非隐藏页截图） |
| 分辨率 | 1920×1080 主测；1366×768（运维笔记本）窄屏复测 |
| 后端 | `127.0.0.1:8443`，SQLite，broker 未连（REST 路径不受影响） |
| 前端 | vite dev `127.0.0.1:8848`，msedge 已登录 admin |
| 测量手段 | `page.evaluate`：`getBoundingClientRect` / `scrollHeight` / `clientHeight` / `getComputedStyle` / `elementFromPoint` / 直接写 `scrollTop` 验证可滚性 |
| 工作树 | 含 QR-S36 三个未提交文件（monitor/containers.ts/exporting.ts），分析以当前工作树为准 |

测量口径定义（后文回归复用）：

- `pageScrolls`：滚动容器（el-scrollbar wrap）`scrollHeight > clientHeight`，即整页滚
- `canScroll`：把 `wrap.scrollTop` 置 600 后回读，非 0 为真
- `可达性`：`elementFromPoint(视口中心偏下)` 非 null
- `sticky top`：吸底栏在指定 `scrollTop` 下的 `getBoundingClientRect().top`，≤ 视口高为生效

---

## 3. 各页实测几何（1920×1080）

| 页面 | 根节点 | pageScrolls | 滚动高度 / 视口 | 卡片与表格 | 关键读数 |
|---|---|---|---|---|---|
| command/shell、command/collect | `.kk-page`（CommandWorkbench） | —（见下） | grow `scrollHeight` 2128 vs `clientHeight` 970 | 历史卡高 **1797px**，底边 y=**2209**（视口 1080） | `wrap.scrollTop=600` 回读 **0**（不可滚）；`elementFromPoint(960,1500)=null`；`.kk-side` 计算 `flex-direction: column` |
| host/monitor（总览） | `<div>`（无锚点） | ✅ | **5469px** / 1080 | 表 100 行/页（前端分页） | 滚到 2000 时 `.kk-band` top=**-1900**（仪表带滚走）；sticky 栏 top=**3447** |
| audit | `<el-card>` | ✅ | **6606px** / 1080 | limit 默认 200 | `.kk-page__body` 在 card 内，父级 block，flex 链断 |
| host/update | `<div v-loading>` | ✅ | **2012px** / 1080 | 两张堆叠 kk-card | 表1 同挂 `kk-fill-table` + `kk-sticky-bar`，两个机制都失效 |
| system | — | ✅ | 1219px / 1080 | — | 整页滚 |
| welcome | `.welcome{padding:0}` | 窄屏复测 ✅ | 窄屏 910 vs 687 | 左卡 742 / 表 639 | 唯一 `height="100%"` 正确范式，但根无高度锚点 |
| host/detail | — | 窄屏复测 ✅ | 窄屏 932 vs 687 | card 903 | 5 张表硬编码 `max-height`（220/220/260/260/240） |

### 3.1 命令中心：双栏坍塌的逐节点证据

`CommandWorkbench.vue:16` 是 `<div class="kk-side kk-page__body">`，两个类同时挂在同一元素：

| 选择器 | 声明处 | `flex-direction` |
|---|---|---|
| `.kk-page__body` | kk.scss:57 | `column` |
| `.kk-side` | kk.scss:78-82 | **未声明**（仅媒体查询里覆盖为 column） |

同特异性下只有一条 `flex-direction` 声明生效 → 计算值 `column`。实测两栏
`left` 同为 210、宽度同为 1710（即等宽上下堆叠）。**A5.3 设计的「左执行 / 右结果」
自落地以来从未真正横向排列过**，此前所有文档都按静态结构描述它是双栏。

### 3.2 吸底栏归因实验（两轮）

| 实验 | 操作 | sticky `top` | 结论 |
|---|---|---|---|
| 基线 | monitor 滚到 scrollTop=2000 | 3447 | 视口外，未吸住 |
| 实验 1 | `view.style.overflow='visible'` | 3447 | **无效**（排除 el-scrollbar view 的 overflow:hidden） |
| 实验 2 | `card.style.overflow='visible'` | **715** | **生效** → 真因是 el-card 默认 `overflow:hidden` |

机理：sticky 的 scrollport 是**最近的滚动祖先**；el-card 自己是 `overflow:hidden`
（非 `visible`）且自身不滚，于是 card 成为 scrollport，sticky 永远等不到滚动事件。
放开 card 的 overflow 后 sticky 立刻贴到底部 715px 处。这也说明「保留 sticky、
放开 overflow」在技术上是可行解——但 el-card 的 overflow:hidden 承担圆角裁剪，
放开有副作用（见 §5 P1-2 方案比选）。

---

## 4. 问题清单

> **落地状态（2026-10-30 复测收口）**：P0-1 `e029263`、P0-2 `2776d77`、P1-1/P1-2 `8df7b18`、
> P2-1 `64f5e0b`。复测口径从「--kk-page-h flex 链全站化」改为「@pureadmin/table adaptive
> 视口测量」（路线变更声明见 §5）；复测值（playwright-cli 真实浏览器，探针
> scripts/fe_layout_probe.js）：
> - monitor 1920×1080：pageScrolls **false**（修前 5469/999 整页滚）、pager.bottom 938≤999、
>   bandTop 93 常驻、stickyTop **null**（kk-sticky-bar 已退役）；
> - monitor 1366×768：pageScrolls false、pager.bottom 626≤687；
> - welcome/detail/system 接入 .kk-page--grow 后 1920 档 pageScrolls:false、
>   1366×768 档内容超出照常滚（修订口径）且五页 hOverflow 全 false；
> - kk-fill-table / kk-sticky-bar / kk-batch / kk-pager 四类引用清零后随 `64f5e0b` 退役。

### P0-1 命令中心双栏坍塌

- **现象**：shell / collect 两页的执行表单与执行历史上下堆叠且等宽。
- **证据**：§3.1 表格；实测两栏 left/width 相同。
- **根因**：`kk.scss` 中 `.kk-page__body`（L57）与 `.kk-side`（L78）的层叠冲突——
  后者缺 `flex-direction` 声明，前者的 `column` 无人覆盖。
- **影响**：A5.3「一屏完成主链路」的设计目标完全落空；500 台时左右互抢纵向空间。

### P0-2 命令中心内容被裁且不可达

- **现象**：历史表下半部分与分页器无法通过任何滚动到达。
- **证据**：历史卡 1797px、底边 y=2209；`wrap.scrollTop` 写 600 回读 0；
  `elementFromPoint(960,1500)=null`；grow `scrollHeight` 2128 vs `clientHeight` 970。
- **根因**（三层叠加，缺一不可）：
  1. `.kk-page` 定高根（`calc(100vh - 152px)`）+ lay-content `view-style` 的
     `overflow:hidden`（lay-content:129）→ 溢出被裁、wrap 不滚；
  2. `CommandHistory.vue:281` 的 `<el-card class="kk-card kk-page__body">` 里，
     `.el-card__body` 是 Element Plus 默认的 **block**，`kk-fill-table` 的
     `flex:1; min-height:0` 作用在 block 子级上完全无效（CommandWorkbench 左栏
     `.kk-fill :deep(.el-card__body)` 修了这条链，右栏没有）；
  3. `el-table`（CommandHistory:356）**没有 `height` prop** —— Element Plus 表格
     不设 height 时高度随内容、自身不滚。**仅靠 `kk-fill-table` 不够，必须配
     `height="100%"`**，这是本轮实测澄清的关键机理（welcome 页正是两者都有）。
- **影响**：分页器不可达 = 无法翻页；新下发的批次若落在表格下半，看不到结果。
  这是全站唯一「功能不可用」级布局缺陷，故列 P0。

### P1-1 高度锚点只落地一页（FE-5 未做）

- **现象**：除命令中心外 6 页全部整页滚。
- **证据**：§3 表格 `pageScrolls` 列。
- **根因**：`--kk-page-h` 地基（kk.scss:22）只有 CommandWorkbench 用了 `.kk-page`；
  monitor/audit/update/system/detail/welcome 的根节点都不是 `.kk-page`。
  monitor 页 scoped 注释里还写着「业务页不得再写 calc(100vh - N)」，
  但本页自己就没有锚点——**禁令是对空令**。
- **影响**：`.kk-band` 仪表带（500 台健康比例，总览页最重要的判断）随整页滚走；
  各页 `calc(100vh - N)` 手算值一旦 navbar/tabs 改尺寸就漂。

### P1-2 吸底栏从未吸住

- **现象**：monitor 的批量操作栏（`.kk-batch.kk-sticky-bar`）要到几千 px 外才出现。
- **证据**：§3.2 实验表。
- **根因**：el-card `overflow:hidden` 成为 sticky 的 scrollport 且自身不滚
  （§3.2 实验 2 直接证实）。
- **影响**：500 台勾选后必须滚到表格底才能点「批量下发」，与 kk.scss:67 注释
  宣称的「不用拉到底才能点」相反。

### P2-1 填充范式不统一

- **现象**：`kk-fill-table` 在 4 个页面挂名但全部不生效；detail 页走硬编码。
- **证据**：
  - welcome：`height="100%"` + `:deep(.el-card__body){display:flex;…}` —— 仓内唯一正确范式；
  - CommandHistory：card 根 flex 但 card__body 是 block，且表无 height；
  - audit：`.kk-page__body` 包在 `<el-card>` 内部（block 父级），双无效；
  - update：两张表挂 `kk-fill-table`，同样断在 card__body；
  - detail：5 张表 `max-height` 硬编码 220/220/260/260/240 + `.kk-chart{height:300px}`，
    窗口一矮就截断，图表靠 `useResizeObserver` 单独自适应——两套机制并存。
- **影响**：每页一套表格高度逻辑，改一处漏三处；`kk-fill-table` 成为「看起来
  统一、实际各页各自为政」的安慰剂类名。

---

## 5. 修复方案（按优先级）

> **路线变更（2026-10-28 实施时裁决）**：原计划的「--kk-page-h flex 链全站化」已被
> 「@pureadmin/table adaptive 视口测量」取代——同一批缺陷用上游 sanctioned 机制修，
> 不需要自研高度锚点六页同步维护；el-card 的 overflow:hidden 在该机制下失去杀伤力。
> 以下各条按原样保留作为决策依据，落地形态见 §4 顶部的落地状态。
> 遵循项目约定：本方案评审通过后再动代码。每条都标注了改动文件与回归口径（§6）。

### P0-1 `.kk-side` 层叠修复（1 行）

- **改法**：`kk.scss` 的 `.kk-side` 显式补 `flex-direction: row;`。
  两个类共挂同一元素时方向不再依赖声明顺序与「另一类恰好没声明」的巧合；
  媒体查询里的 `column` 覆盖保持不变。
- **备选**：CommandWorkbench 去掉 `kk-page__body`、把 `flex:1; min-height:0`
  移进 `.kk-side`。效果等价，但会让「共挂两类」的写法继续存在，不推荐。
- **回归**：命令中心两栏 `left` 不同、宽度比约 34%:66%；≤1200px 时折叠为上下。

### P0-2 CommandHistory 填充链修复（3 处）

1. `CommandWorkbench.vue`：把左栏的 `.kk-fill :deep(.el-card__body){…}` 范式
   **提到全局 kk.scss**（如 `.kk-card__fill` 或直接强化 `.kk-page__body > .el-card`），
   右栏复用——左右两栏现在一条链修了一条没修，本身就是分裂源。
2. `CommandHistory.vue:281`：el-card 加深 flex 链（复用上一步的全局范式）。
3. `CommandHistory.vue:356`：el-table 补 `height="100%"`（**与 `kk-fill-table`
   同时存在才有效**，缺一即退化为「撑高卡片」）。
- **回归**：历史卡高度 ≤ 视口；`wrap.scrollTop` 可写（卡内滚由 el-table 承担）；
  分页器在视口内可点；下发后新批次行可见。

### P1-1 FE-5 高度锚点全站化（承接 10-07 方案 B1，本轮补实测依据）

- monitor / audit / update / system / detail / welcome 六个页面根统一
  `.kk-page` + `.kk-page__body`；`el-card` 一律接 §5 P0-2 的 flex 链；
  表格一律 `kk-fill-table` + `height="100%"`。
- audit 现有的卡内 `.kk-page__body`（audit:142）拆掉，并入统一结构。
- detail 的 5 处硬编码 `max-height` 与 `.kk-chart{height:300px}` 收敛为
  flex + `height="100%"`，图表沿用已有 `useResizeObserver`。
- **回归**：6 页 `pageScrolls=false`；表格在卡内滚；`.kk-band` 常驻视口顶部；
  1080p 与 768p 两档都不出现截断。

### P1-2 吸底栏改 flex 末段（推荐）而非抢救 sticky

| 方案 | 做法 | 评价 |
|---|---|---|
| A（推荐） | 删 `kk-sticky-bar`，批量栏作为卡片 flex 末段常驻 | P1-1 落地后卡片本就是视口高、卡内滚由 el-table 承担，栏天然在表尾可见；无 hack |
| B | 保留 sticky，放开 el-card `overflow:hidden` | 实验证明能吸住，但 overflow:hidden 承担圆角裁剪，放开后表格圆角/阴影可能溢出卡片，副作用面不可控 |

- **回归**：勾选行后批量栏无需滚动即可见；卡片圆角与表格边界无视觉破损。

### P2-1 收敛唯一填充范式

固化为一条写进 kk.scss 注释的规范，后续新页照此办理：

```
页面根 .kk-page → 卡片 .kk-page__body（el-card__body 经 :deep 改 flex column）
→ 表格 .kk-fill-table + height="100%"
```

`kk-fill-table` 保留类名（6 页已在用），但必须在文档与注释中写明「**必须配
height prop，单独使用不生效**」，避免再被当成自洽方案。

---

## 6. 回归口径（修复后逐项过）

| 编号 | 指标 | 修前实测 | 修后期望 | 测量方法 |
|---|---|---|---|---|
| R1 | 命令中心两栏几何 | 同 left 210 / 同宽 1710 | left 不同，宽比 34%:66% | `getBoundingClientRect` |
| R2 | 命令历史卡可达性 | `elementFromPoint(960,1500)=null` | 非 null；分页器在视口内 | `elementFromPoint` |
| R3 | 命令中心容器可滚性 | `wrap.scrollTop=600` 回读 0 | 卡内滚由 el-table 承担，页面不整页滚 | 写 `scrollTop` 回读 |
| R4 | monitor 整页滚动高度 | 5469px | `pageScrolls=false` | `scrollHeight vs clientHeight` |
| R5 | 仪表带常驻 | scrollTop=2000 时 top=-1900 | top ≈ 0（视口顶部） | `getBoundingClientRect` |
| R6 | 批量栏可达 | sticky top=3447 | 常驻可见（flex 末段） | `getBoundingClientRect` |
| R7 | audit / update / system 整页滚 | 6606 / 2012 / 1219px | 均 `pageScrolls=false` | 同上 |
| R8 | detail 窄屏 | 1366×768 下 932 vs 687 | `pageScrolls=false`，5 表无截断 | 两档分辨率各测一次 |
| R9 | 命令中心不被带偏 | 已是正确双栏（修复后） | 修复其余页时命令中心几何不变 | R1 复测 |

> 旧文档曾记录「内置浏览器为隐藏页，截图与几何不可信」。本轮全程使用
> playwright-cli + msedge 真实浏览器几何测量，结论可复现；修复后按同一脚本复测即可。

---

## 7. 与 10-07 方案文档的勘误对照

| 10-07 文档结论 | 本轮实测 | 处置 |
|---|---|---|
| 「命令中心 `kk-fill-table` flex 生效，内部滚动，页面不滚」（L144） | **错**。双栏坍塌 + 内容被裁不可达 | 以本文 P0-1/P0-2 为准 |
| 「`.kk-side` 窄屏折叠为上下」 | 代码意图如此，但宽屏也因层叠 Bug 变成上下 | 修 P0-1 后媒体查询才恢复本意 |
| FE-5「monitor 吸底栏改为 flex 末段（不需要 sticky）」 | 方向正确，且 §3.2 实验给出更强依据（sticky 在该结构下无解） | 按方案 A 执行 |
| FE-5 未做、待 6 页 × 3 分辨率人眼判 | 未做状态不变；本轮已提供几何判据替代纯人眼 | §6 表格作为验收 |
| 「`.kk-card` 从未定义（仅标记）」 | 仍如此（kk.scss 只有 `.kk-card + .kk-card` 间距规则） | 建议 P0-2 改造时把 flex 链落到 `.kk-card` 上，顺带补定义 |

---

## 8. 边界与不在本轮范围

- QR-S36（筛选/排序下推 + 前端分页）在途未提交，本文几何数据基于含该改动的
  工作树；QR-S36 合并不影响本文任何结论（它改数据来源，不改布局链路）。
- 本文只做分析与方案，**不改代码**；修复待本方案评审通过后按 §5 优先级执行。
- `service/kk-analysis.db`、`data/seed_analysis.py`、`web/vite.analysis.*.log`、
  `.playwright-cli/`、`login.yaml` 为本次分析临时产物，不入库。
