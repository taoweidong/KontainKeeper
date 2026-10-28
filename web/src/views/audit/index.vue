<script setup lang="ts">
import { ref } from "vue";
import { useAuditLog } from "./utils/hook";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import SearchIcon from "~icons/ri/search-line";
import RefreshIcon from "~icons/ep/refresh";
import DownloadIcon from "~icons/ep/download";

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
          :icon="useRenderIcon(SearchIcon)"
          @click="onSearch"
          >搜索</el-button
        >
        <el-button :icon="useRenderIcon(RefreshIcon)" @click="resetFields"
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
          :icon="useRenderIcon(DownloadIcon)"
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
          :adaptive-config="{ offsetBottom: 132 }"
          align-whole="center"
          table-layout="auto"
          :loading="loading"
          :size="size"
          :data="dataList"
          :columns="dynamicColumns"
          :pagination="pagination"
          :header-cell-style="{
            background: 'var(--el-fill-color-light)',
            color: 'var(--el-text-color-primary)'
          }"
          @selection-change="onSelectionChange"
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
