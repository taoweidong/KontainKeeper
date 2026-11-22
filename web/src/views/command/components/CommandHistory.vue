<script setup lang="ts">
/** 执行历史（命令面板 / 采集面板共用的右栏）：分页 + 搜索 + 批次 + 自适应轮询 + 输出抽屉。

结构对齐 web/src/views/audit/（Task 6 定形的开源列表页范式）：
根 div.kk-main + 搜索条 el-form.kk-search-form + PureTableBar + pure-table[adaptive]，
表格填高交给 adaptive 的视口绝对测量，不再依赖祖先高度链与 el-card 的 overflow:hidden。
数据流与列定义在 ./utils/hook.tsx；对外契约（defineExpose 的 reload / focusBatch）
由 shell/index.vue、collect/index.vue 经 ref 调用，一个字不许改。
*/
import { ref } from "vue";

import type { CommandRow } from "@/api/commands";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { IconCommand, IconDownload, IconRefresh } from "@/utils/kkIcons";
import { kkHeaderCellStyle, tsText } from "@/utils/kk";

import { useCommandHistory } from "./utils/hook";

defineOptions({ name: "CommandHistory" });

const tableRef = ref();
const {
  POLL_KEY,
  rows,
  columns,
  read,
  loading,
  keyword,
  statusFilter,
  batchFilter,
  autoRefresh,
  batches,
  exporting,
  pollFailed,
  pagination,
  out,
  outView,
  hasActive,
  batchStat,
  hasFilter,
  statNum,
  shortBatch,
  rowClass,
  loadCommands,
  focusBatch,
  restartTimer,
  clearPoll,
  showOut,
  downloadOut,
  onExport
} = useCommandHistory();

defineExpose({ reload: () => loadCommands(), focusBatch });
</script>

<template>
  <div class="kk-main">
    <el-form
      :inline="true"
      class="kk-search-form bg-bg_color w-full pl-4 pr-4 pt-2 overflow-auto"
    >
      <el-form-item label="关键字：">
        <el-input
          v-model="keyword"
          placeholder="搜索主机 / 命令 / ID"
          clearable
          class="w-45!"
        />
      </el-form-item>
      <el-form-item label="状态：">
        <el-select
          v-model="statusFilter"
          clearable
          placeholder="全部"
          class="w-32!"
        >
          <el-option label="待下发" value="pending" />
          <!-- queued 是 v3 离线队列档（statusLabel 早有词，筛选下拉此前漏档） -->
          <el-option label="已排队" value="queued" />
          <el-option label="已下发" value="sent" />
          <el-option label="执行中" value="running" />
          <el-option label="已完成" value="done" />
          <el-option label="失败" value="failed" />
          <el-option label="超时" value="timeout" />
          <el-option label="结果丢失" value="lost" />
        </el-select>
      </el-form-item>
      <el-form-item label="批次：">
        <el-select
          v-model="batchFilter"
          clearable
          placeholder="全部"
          class="w-36!"
        >
          <el-option
            v-for="b in batches"
            :key="b.batch_id"
            :label="shortBatch(b.batch_id) + '（' + b.total + '）'"
            :value="b.batch_id"
          />
        </el-select>
      </el-form-item>
      <el-form-item>
        <!-- 勾掉自动刷新必须真的停表：clearPoll(POLL_KEY) 走的是 usePolls 的作用域句柄，
             不是把间隔改成 0；重新勾上交给 restartTimer 按 hasActive 重建 3s / 10s 节奏。 -->
        <el-checkbox
          v-model="autoRefresh"
          @change="v => (v ? restartTimer() : clearPoll(POLL_KEY))"
        >
          自动刷新
        </el-checkbox>
      </el-form-item>
    </el-form>

    <!-- :loading 接真实请求态（QR1120-W27）：不传时 bar 内部仍是固定 500ms 假转圈，
         传了则 spin 跟随 loading，慢请求转到底、快请求立刻停。 -->
    <PureTableBar
      title="执行历史"
      :columns="columns"
      :loading="loading"
      :table-ref="tableRef?.getTableRef()"
      @refresh="loadCommands()"
    >
      <template #title>
        <div class="flex min-w-0 items-center gap-2">
          <b class="shrink-0">
            <!-- #title 槽不走 bar 的 titleIcon prop（QR1123-W3），槽内自接 -->
            <el-icon class="kk-type-icon"
              ><component :is="useRenderIcon(IconCommand)" /></el-icon
            >执行历史
          </b>
          <span v-if="batchStat" class="kk-sub truncate">
            该批次 {{ batchStat.total }} 台： 完成 {{ batchStat.done || 0 }} ·
            失败 {{ batchStat.failed || 0 }} · 超时
            {{ batchStat.timeout || 0 }} · 未终态
            {{
              statNum(batchStat.pending) +
              statNum(batchStat.sent) +
              statNum(batchStat.running)
            }}
          </span>
          <!-- 刷新失败进这条读数（W5 / QR1120-W22：静默与手动同一口径）；静默那路
               不弹 toast——3~10s 一轮的刷屏会把真错误淹掉。
               正常态按当前档位说实话（QR1120-W30）：全终态时实际是 10s 一轮，
               恒显「有未终态命令时 3 秒一轮」会让用户按 3s 的预期等结果。 -->
          <span
            v-if="autoRefresh"
            class="kk-sync shrink-0"
            :class="{ 'kk-sync--stale': pollFailed }"
          >
            {{
              pollFailed
                ? "刷新失败，列表可能已过期"
                : hasActive
                  ? "有未终态命令：3 秒一轮"
                  : "全部已终态：10 秒一轮"
            }}
          </span>
        </div>
      </template>
      <template #buttons>
        <el-button
          :loading="loading"
          :icon="useRenderIcon(IconRefresh)"
          @click="loadCommands()"
          >刷新</el-button
        >
        <el-button
          type="primary"
          :icon="useRenderIcon(IconDownload)"
          :loading="exporting"
          @click="onExport"
          >导出 CSV</el-button
        >
      </template>
      <template v-slot="{ size, dynamicColumns }">
        <!-- :adaptive-config 的 offsetBottom=140 是本页实测值，两个数都不许照抄
             （上游 108 / 审计页 132 都不满足本页的第二条尺子）。本页有两条约束：
             ① adaptive 定高 = innerHeight - tableWrapper.top - offsetBottom，分页器底边
                = innerHeight - offsetBottom + 16(上边距) + 32(分页高)，内容区可视高
                = innerHeight - 81 ⇒ offsetBottom ≥ 129（108 时 1366×768 实测溢出 +21px）；
             ② 外层 .kk-page 是 calc(100vh - var(--kk-page-h)) 的固定高盒（审计页没有这条），
                表格越过它整页又开始滚 ⇒ ≥ 119。
     取 140 = 129 的下界再留 11px 余量。实测（同一份 500 台种子库、pageSize 50）：
     1920×1080 pager.bottom 988 ≤ client 999、1366×768 pager.bottom 676 ≤ 687、
     1100×800（媒体查询折叠为上下）pager.bottom 708 ≤ 719，三档 pageScrolls 均 false、
     表体内部滚动（内容 3190 / 可视 779·467·126）。搜索条或表头读数一改高就复测。
     2026-10-31 复核：--kk-page-h 152→112 后约束②放宽到 ≥79（129 仍是最紧的一条，
     140 不动）；右栏 .kk-main 挂 overflow:auto、各横带 flex-shrink:0（kk.scss /
     CommandWorkbench），窗口偏矮时内容超出改走栏内滚动，不再把搜索条压扁
     （1920×937 曾实测表单只剩 12px 缝）。 -->
        <!-- :pagination 上游四页一律写 { ...pagination, size }，这里**必须**直连 reactive：
             @pureadmin/table 对传入的那个对象就地写 currentPage/pageSize，展开成一次性快照
             后写入无人读 → 翻页与条数全废。代价：分页器不吃密度 size（纯观感）。
             同理模板不绑 @page-size-change：条数由 hook 内 watch(pagination.pageSize) 统一
             发起重载，两处都绑会打两遍后端（FE-8）。 -->
        <pure-table
          ref="tableRef"
          row-key="id"
          adaptive
          :adaptive-config="{ offsetBottom: 140 }"
          align-whole="center"
          table-layout="auto"
          :loading="loading"
          :size="size"
          :data="rows"
          :columns="dynamicColumns"
          :row-class-name="rowClass"
          :pagination="pagination"
          :header-cell-style="kkHeaderCellStyle"
          @row-click="showOut"
          @page-current-change="() => loadCommands()"
        >
          <template #empty>
            <el-empty
              class="kk-empty"
              :image-size="60"
              :description="
                read
                  ? pagination.total
                    ? '本页无数据'
                    : hasFilter
                      ? '该筛选条件下没有匹配命令'
                      : '还没有命令记录'
                  : '没读到（命令接口不可达）'
              "
            />
          </template>
          <template #operation="{ row }">
            <!-- reset-margin 按 brief 与上游同构挂着：本页操作列当前只有一个按钮，
                 EP 的 `.el-button + .el-button` 还没落下那 12px，所以它今天**不生效**
                 （生效边界见 kk.scss 该规则的注释）；加第二个按钮时它就接手。 -->
            <el-button
              class="reset-margin"
              link
              type="primary"
              :size="size"
              @click.stop="showOut(row as CommandRow)"
              >查看</el-button
            >
          </template>
        </pure-table>
      </template>
    </PureTableBar>

    <!-- 宽度与 RBAC 弹窗同族（QR1121）：min(92vw, 680px)——极窄屏 92vw 保底，
         宽屏不再按 42% 无限放宽（EP addUnit 对字符串原样透传，原生支持 min()）。 -->
    <el-drawer v-model="out.visible" :title="out.title" size="min(92vw, 680px)">
      <div class="kk-out-wrap">
        <!-- 元信息行（QR1121-W7）：下发者/收发时间/截断/分片来自列表行快照，
             不另发请求；API 早有这些字段，此前抽屉只给标题与输出。 -->
        <div v-if="out.row" class="kk-out-meta">
          <span class="kk-kv"
            ><b>下发者</b>: {{ out.row.created_by || "-" }}</span
          >
          <span class="kk-kv"
            ><b>创建</b>:
            <span class="kk-num">{{ tsText(out.row.created_at) }}</span></span
          >
          <span class="kk-kv"
            ><b>下发</b>:
            <span class="kk-num">{{ tsText(out.row.sent_at) }}</span></span
          >
          <span class="kk-kv"
            ><b>完成</b>:
            <span class="kk-num">{{ tsText(out.row.finished_at) }}</span></span
          >
          <span class="kk-kv"
            ><b>截断</b>: {{ out.row.truncated ? "是" : "否" }}</span
          >
          <span class="kk-kv"
            ><b>分片</b>:
            <span class="kk-num">{{ out.row.out_chunks }}</span></span
          >
        </div>
        <!-- 大输出切片渲染（QR1120-W24）：超 256K 字符只把头 128K + 尾 64K 交给 DOM，
             中段折叠成一条引导（完整内容走下方「下载此条输出」，复用既有在途守卫）。
             不超阈值时 outView 为 null，照旧单个 pre 全文渲染。 -->
        <el-scrollbar class="kk-out-scroll">
          <div v-loading="out.loading" class="kk-out-body">
            <pre class="kk-out">{{ outView ? outView.head : out.text }}</pre>
            <template v-if="outView">
              <div class="kk-out-gap">
                … 已省略中段
                {{ outView.omitted }} 字符，点下方「下载此条输出」查看完整内容 …
              </div>
              <pre class="kk-out">{{ outView.tail }}</pre>
            </template>
          </div>
        </el-scrollbar>
      </div>
      <template #footer>
        <div class="kk-toolbar">
          <span class="kk-sub">点击列表其他行可连续查看，列表不会被遮挡</span>
          <el-button
            type="primary"
            :icon="useRenderIcon(IconDownload)"
            @click="downloadOut"
            >下载此条输出</el-button
          >
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
/* 元信息行 + 输出滚动区的 flex 列（QR1121）：元信息 flex-shrink:0 不被压缩，
   滚动区吃剩余高度（el-drawer__body 是确定高，flex:1 可解算） */
.kk-out-wrap {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.kk-out-meta {
  flex-shrink: 0;
  padding-bottom: var(--kk-space);
  margin-bottom: var(--kk-space);
  line-height: 1.8;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.kk-out-scroll {
  flex: 1;
  min-height: 0;
}

/* v-loading 从 pre 挪到包裹层（QR1120-W24 切片后有两个 pre）：
   给最小高度，空输出/加载中 spinner 才有落点 */
.kk-out-body {
  min-height: 120px;
}

/* 中段省略引导条：与输出正文区分开，弱底色 + 居中。
   间距收口四档（QR1121）：原 8px 10px / 6px 不在尺度内 */
.kk-out-gap {
  padding: var(--kk-space);
  margin: var(--kk-space-sm) 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: center;
  background: var(--el-fill-color);
  border-radius: 4px;
}
</style>
