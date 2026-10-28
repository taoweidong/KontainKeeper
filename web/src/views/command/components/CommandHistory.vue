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
import DownloadIcon from "~icons/ep/download";
import RefreshIcon from "~icons/ep/refresh";

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
      class="kk-search-form bg-bg_color w-full pl-8 pt-3 overflow-auto"
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

    <PureTableBar
      title="执行历史"
      :columns="columns"
      :table-ref="tableRef?.getTableRef()"
      @refresh="loadCommands()"
    >
      <template #title>
        <div class="flex min-w-0 items-center gap-2">
          <b class="shrink-0">执行历史</b>
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
          <!-- 静默轮询失败进这条读数、不弹 toast（W5）：3~10s 一轮的刷屏会把真错误淹掉。 -->
          <span
            v-if="autoRefresh"
            class="kk-sync shrink-0"
            :class="{ 'kk-sync--stale': pollFailed }"
          >
            {{
              pollFailed
                ? "自动刷新失败，列表可能已过期"
                : "有未终态命令时 3 秒一轮"
            }}
          </span>
        </div>
      </template>
      <template #buttons>
        <el-button
          :loading="loading"
          :icon="useRenderIcon(RefreshIcon)"
          @click="loadCommands()"
          >刷新</el-button
        >
        <el-button
          type="primary"
          :icon="useRenderIcon(DownloadIcon)"
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
             表体内部滚动（内容 3190 / 可视 779·467·126）。搜索条或表头读数一改高就复测。 -->
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
          :header-cell-style="{
            background: 'var(--el-fill-color-light)',
            color: 'var(--el-text-color-primary)'
          }"
          @row-click="showOut"
          @page-current-change="() => loadCommands()"
        >
          <template #empty>
            <el-empty
              class="min-h-200"
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

    <el-drawer v-model="out.visible" :title="out.title" size="42%">
      <el-scrollbar class="kk-out-scroll">
        <pre v-loading="out.loading" class="kk-out">{{ out.text }}</pre>
      </el-scrollbar>
      <template #footer>
        <div class="kk-toolbar">
          <span class="kk-sub">点击列表其他行可连续查看，列表不会被遮挡</span>
          <el-button type="primary" @click="downloadOut"
            >下载此条输出</el-button
          >
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.kk-out-scroll {
  height: 100%;
}
</style>
