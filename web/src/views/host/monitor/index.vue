<script setup lang="ts">
import { ref } from "vue";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import DownloadIcon from "~icons/ep/download";
import { nOr, tsText } from "@/utils/kk";
import { useMonitor } from "./utils/hook";

defineOptions({ name: "HostMonitor" });

const tableRef = ref();

const {
  loading,
  total,
  online,
  alerts,
  outdated,
  read,
  lastLoadedAt,
  pollFailed,
  interval,
  keyword,
  onlyOnline,
  onlyAlert,
  hostType,
  protoVer,
  isFiltered,
  clearFilters,
  emptyText,
  paged,
  pagination,
  load,
  exporting,
  onExport,
  selection,
  onSelectionChange,
  openCollect,
  openCommandCenter,
  gotoUpgrade,
  onRowClick,
  rowClass,
  onSortChange,
  dialog,
  collectItems,
  collectTargets,
  submitCollect,
  columns
} = useMonitor();
</script>

<template>
  <div class="kk-main">
    <!-- 机队仪表带：整屏最重要的判断，常驻视口顶部（R4/R5）——
         旧结构挂在 el-card #header 里随 500 行表格滚走（10-15 实测 5469px 整页滚）。
         刷新入口只留 PureTableBar 那一枚（Task 7 评审口径），这里的动作只剩间隔档位。 -->
    <div class="bg-bg_color w-full pl-8 pr-8 pt-3 pb-3">
      <div class="kk-band">
        <div class="kk-band__fleet">
          <!-- 首次加载失败时一个读数都没有：条与四个格子一律不渲染 0（QR-W16）。
               0 台主机是肯定语句，运维会当成「 fleet 空了」去做下一步动作。 -->
          <span
            v-if="read"
            class="kk-band__strip"
            role="img"
            :aria-label="`在线 ${online} 台，离线 ${Math.max(0, total - online)} 台`"
          >
            <i class="kk-band__seg" :style="{ flex: String(online) }" />
            <i
              class="kk-band__seg kk-band__seg--offline"
              :style="{ flex: String(Math.max(0, total - online)) }"
            />
          </span>
          <span class="kk-band__readout"
            ><b>{{ nOr(read, total) }}</b
            >台主机</span
          >
          <el-tooltip
            v-if="isFiltered"
            content="筛选条件生效中：以上读数只统计筛后的子集，「导出清单」与之一致"
            placement="bottom"
          >
            <el-tag size="small" type="info">已筛选</el-tag>
          </el-tooltip>
          <span class="kk-band__readout"
            ><b>{{ nOr(read, online) }}</b
            >在线</span
          >
          <span class="kk-band__readout kk-band__readout--alert">
            <b>{{ nOr(read, alerts) }}</b
            >磁盘告警
          </span>
          <span class="kk-band__readout kk-band__readout--stale">
            <b>{{ nOr(read, outdated) }}</b
            >待升级
          </span>
          <el-tooltip
            content="自动刷新最近一次成功的时间；失败时表格保持上一批数据"
            placement="bottom"
          >
            <span class="kk-sync" :class="{ 'kk-sync--stale': pollFailed }">
              {{
                pollFailed
                  ? "自动刷新失败，读数可能已过期"
                  : lastLoadedAt
                    ? `已同步 ${tsText(lastLoadedAt)}`
                    : "尚未同步"
              }}
            </span>
          </el-tooltip>
        </div>
        <div class="kk-actions">
          <el-select v-model="interval" style="width: 120px">
            <el-option label="5 秒" :value="5" />
            <el-option label="10 秒" :value="10" />
            <el-option label="30 秒" :value="30" />
            <el-option label="不自动刷新" :value="0" />
          </el-select>
        </div>
      </div>
    </div>

    <!-- 筛选条：条件全部下推服务端（QR-S36），列表与「导出清单」读同一份参数。
         类型 / 协议 来自 v4 元信息，只有服务端能筛——
         关键字同时匹配主机名、镜像、系统与分组。
         mt-2：与上方仪表带之间露一条 8px 灰缝（页面背景），与 audit/update 页
         「白块 → 灰缝 → 白块」的上下节奏一致，不再白贴白连成一张大白板。 -->
    <el-form
      :inline="true"
      class="kk-search-form bg-bg_color w-full mt-2 pl-8 pr-8 pt-3 overflow-auto"
    >
      <el-form-item label="关键字：">
        <el-input
          v-model="keyword"
          placeholder="主机名 / 镜像 / 系统 / 分组"
          clearable
          class="w-45!"
        />
      </el-form-item>
      <el-form-item label="类型：">
        <el-select
          v-model="hostType"
          placeholder="全部"
          clearable
          style="width: 110px"
        >
          <el-option label="物理机" value="metal" />
          <el-option label="虚拟机" value="vm" />
          <el-option label="容器" value="container" />
        </el-select>
      </el-form-item>
      <el-form-item label="协议：">
        <el-tooltip
          content="按上报协议筛：筛 v3 是关兼容窗口（KK_DROP_PROTO_V3）前的例行检查"
          placement="bottom"
        >
          <el-select
            v-model="protoVer"
            placeholder="全部"
            clearable
            style="width: 104px"
          >
            <el-option label="v4" value="4" />
            <el-option label="v3" value="3" />
          </el-select>
        </el-tooltip>
      </el-form-item>
      <el-form-item>
        <el-checkbox v-model="onlyOnline">仅在线</el-checkbox>
        <el-checkbox v-model="onlyAlert">仅告警</el-checkbox>
      </el-form-item>
      <el-form-item v-if="isFiltered">
        <el-button link type="primary" @click="clearFilters">
          清空筛选
        </el-button>
      </el-form-item>
    </el-form>

    <!-- 选中横幅：取代 .kk-sticky-bar（sticky 的 scrollport 是 el-card 且它自己不滚，
         10-15 §3.2 实验 2 证明从未吸住过）。常驻表格上方：勾选后不必滚到表底就能点
         批量动作（R6）。始终渲染而不是 v-if——时隐时现会改表格顶边、
         让 adaptive 量到陈旧高度（update 页同一条裁决）。 -->
    <div class="kk-select-banner">
      <span class="kk-sub">已选 {{ selection.length }} 台</span>
      <el-button
        type="primary"
        :disabled="!selection.length"
        @click="openCollect"
      >
        批量采集
      </el-button>
      <el-button :disabled="!selection.length" @click="openCommandCenter">
        批量执行命令
      </el-button>
      <!-- 升级按钮只对选中的落后主机开放：
           - 全是最新版本时灰着，避免「点了却跳过全部」造成的体验割裂；
           - 有落后时直接跳到「版本与更新」页并在 query 里带上已选主机，
             进入页面后 selection 已被预填好。 -->
      <el-button
        type="warning"
        :disabled="!selection.length || !selection.some(h => h.agent_outdated)"
        @click="gotoUpgrade"
      >
        批量升级
      </el-button>
      <!-- 原来这里还有一个「导出选中清单」：它调的是全量导出，跟勾选无关。
           按勾选导出需要服务端的 pods 契约（500 台放不进 query），登记在 QR-S38；
           名义不符的入口先摘掉，导出走 bar 的「导出清单」。 -->
    </div>

    <PureTableBar
      title="主机总览"
      :columns="columns"
      :table-ref="tableRef?.getTableRef()"
      @refresh="load()"
    >
      <template #buttons>
        <el-button
          type="primary"
          :icon="useRenderIcon(DownloadIcon)"
          :loading="exporting"
          @click="onExport"
          >导出清单</el-button
        >
      </template>
      <template v-slot="{ size, dynamicColumns }">
        <!-- :pagination 必须直连 reactive，不能写 { ...pagination, size }：
             @pureadmin/table 对传入对象**就地**写 currentPage/pageSize，展开成
             一次性快照后写入无人读 → 翻页全废。本页是前端分页：pure-table 写的
             currentPage 由 paged 这个 computed 消费（切片即翻页，不打后端），
             所以模板也不绑 @page-* —— 换条数/翻页没有任何东西需要重新请求。
             勾选跨页由 row-key=pod + 勾选列 reserveSelection 保住。 -->
        <!-- offsetBottom 是本页实测值（1920×1080 与 1366×768 两档复测，
             分页器底边均在视口内；上方仪表带/筛选条一改高就得复测）。 -->
        <pure-table
          ref="tableRef"
          row-key="pod"
          adaptive
          :adaptive-config="{ offsetBottom: 190 }"
          align-whole="center"
          table-layout="auto"
          :loading="loading"
          :size="size"
          :data="paged"
          :columns="dynamicColumns"
          :pagination="pagination"
          :row-class-name="rowClass"
          :default-sort="{ prop: 'last_seen', order: 'descending' }"
          :header-cell-style="{
            background: 'var(--el-fill-color-light)',
            color: 'var(--el-text-color-primary)'
          }"
          @selection-change="onSelectionChange"
          @row-click="onRowClick"
          @sort-change="onSortChange"
        >
          <template #empty>
            <el-empty :description="emptyText" />
          </template>
        </pure-table>
      </template>
    </PureTableBar>

    <el-dialog
      v-model="dialog.visible"
      :title="collectTargets.length > 1 ? '批量采集指标' : '采集指标'"
      width="420px"
    >
      <el-checkbox-group v-model="dialog.items">
        <el-checkbox v-for="it in collectItems" :key="it" :value="it">
          {{ it }}
        </el-checkbox>
      </el-checkbox-group>
      <p class="kk-sub">
        将对
        {{ collectTargets.length }} 台主机下发采集命令，结果在「命令中心」查看。
      </p>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="dialog.submitting"
          @click="submitCollect"
        >
          下发
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<!-- 通用类（kk-band/kk-state/kk-meter/kk-ticks/kk-select-banner 等）统一在 style/kk.scss -->
