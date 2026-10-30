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
      class="kk-search-form bg-bg_color w-full pl-8 pr-8 pt-3 overflow-auto"
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
        <!-- 搜索按**当前页**重查，所以绑 onSearch 而不是「归第 1 页再查」：这是原页面的行为，
             brief 也如此。归位由 hook 内部 reloadFirstPage 在另外三条路径上做（关键字防抖 /
             改条数 / 重置），它不导出，模板里既不需要也不该自己拼 currentPage = 1。 -->
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
        <!-- :adaptive-config 上游与 brief 都写 offsetBottom: 108，本页实测 132：108 时
             pagerRect.bottom 在 1920/1366 两档各溢出可视高度 +21px。adaptive 按
             innerHeight - wrapper.top - offsetBottom 定高，抬到 132 后 996<=999、684<=687。
             搜索条一改高就得复测——别照抄上游 108，也别照抄别的页的数。 -->
        <!-- :pagination 上游四页一律写 { ...pagination, size }，这里**必须**直连 reactive：
             @pureadmin/table 对传入的那个对象就地写 currentPage/pageSize，展开成一次性快照
             后写入无人读 → 翻页与条数全废。代价：分页器不吃密度 size（纯观感）。
             同理模板不绑 @page-size-change：条数由 hook 内 watch(pageSize) 统一发起重载，
             两处都绑会打两遍后端（FE-8）。 -->
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
