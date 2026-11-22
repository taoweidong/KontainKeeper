<script setup lang="ts">
/** 拦截记录（QR1124）：白名单外上报按源 IP 聚合展示。
 *
 * 数据来自服务端 kk_ip_rejects（ip 主键聚合，hits 为节流后 recorded 计数）；
 * ip_rejected 审计并行保留（安全溯源链）。本页 admin 专属（菜单挂 Rbac 子树 +
 * 接口 AdminUser）；operator 经审计页 action=ip_rejected 仍可见拦截事件。
 */
import { ref } from "vue";
import { useIpRejects } from "./utils/hook";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import {
  IconAlert,
  IconDownload,
  IconRefresh,
  IconSearch
} from "@/utils/kkIcons";
import { kkHeaderCellStyle, syncReadout } from "@/utils/kk";

defineOptions({ name: "IpRejects" });

const tableRef = ref();
const {
  POLL_KEY,
  form,
  columns,
  dataList,
  loading,
  read,
  hasFilter,
  exporting,
  pollFailed,
  lastLoadedAt,
  autoRefresh,
  interval,
  pagination,
  onSearch,
  reloadFirstPage,
  onExport,
  resetFields,
  restartTimer,
  clearPoll
} = useIpRejects();
</script>

<template>
  <div class="kk-main">
    <el-form
      :inline="true"
      :model="form"
      class="kk-search-form bg-bg_color w-full pl-4 pr-4 pt-2 overflow-auto"
    >
      <el-form-item label="IP：" prop="ip">
        <el-input
          v-model="form.ip"
          placeholder="按源 IP 模糊过滤"
          clearable
          class="w-45!"
        />
      </el-form-item>
      <el-form-item>
        <el-button
          type="primary"
          :icon="useRenderIcon(IconSearch)"
          @click="reloadFirstPage"
          >搜索</el-button
        >
        <el-button :icon="useRenderIcon(IconRefresh)" @click="resetFields"
          >重置</el-button
        >
      </el-form-item>
      <el-form-item>
        <el-checkbox
          v-model="autoRefresh"
          @change="v => (v ? restartTimer() : clearPoll(POLL_KEY))"
        >
          自动刷新
        </el-checkbox>
      </el-form-item>
      <el-form-item>
        <el-select v-model="interval" class="kk-select-w">
          <el-option label="10 秒" :value="10" />
          <el-option label="30 秒" :value="30" />
          <el-option label="60 秒" :value="60" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <span class="kk-sync" :class="{ 'kk-sync--stale': pollFailed }">
          {{ syncReadout(pollFailed, lastLoadedAt) }}
        </span>
      </el-form-item>
    </el-form>

    <PureTableBar
      title="拦截记录"
      :title-icon="IconAlert"
      :columns="columns"
      :loading="loading"
      :table-ref="tableRef?.getTableRef()"
      @refresh="onSearch"
    >
      <template #buttons>
        <el-button
          type="primary"
          :icon="useRenderIcon(IconDownload)"
          :loading="exporting"
          @click="onExport"
          >导出 CSV</el-button
        >
      </template>
      <template v-slot="{ size, dynamicColumns }">
        <!-- offsetBottom 逐页实测（搜索条含自动刷新控件，1366 可能换行）：
             初值 150，浏览器两档视口复测后定稿。pagination 直连 reactive（FE-8）。 -->
        <pure-table
          ref="tableRef"
          row-key="ip"
          adaptive
          :adaptive-config="{ offsetBottom: 150 }"
          align-whole="center"
          table-layout="auto"
          :loading="loading"
          :size="size"
          :data="dataList"
          :columns="dynamicColumns"
          :pagination="pagination"
          :header-cell-style="kkHeaderCellStyle"
          @page-current-change="() => onSearch()"
        >
          <template #empty>
            <el-empty
              class="kk-empty"
              :image-size="60"
              :description="
                read
                  ? hasFilter
                    ? '该筛选条件下没有匹配记录'
                    : '暂无拦截记录'
                  : '没读到（拦截记录接口不可达）'
              "
            />
          </template>
        </pure-table>
      </template>
    </PureTableBar>
  </div>
</template>
