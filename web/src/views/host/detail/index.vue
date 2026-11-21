<script setup lang="ts">
import { onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { useResizeObserver } from "@vueuse/core";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import {
  IconBack,
  IconCollect,
  IconCommand,
  IconDisk,
  IconDownload,
  IconNet,
  IconProc,
  IconRefresh,
  IconRetry,
  IconTrend,
  IconUpgrade,
  IconUser
} from "@/utils/kkIcons";
import {
  ageText,
  cmdStateClass,
  DISK_ALERT_PCT,
  durText,
  fillPct,
  hostStateClass,
  hostStateText,
  kindText,
  mbText,
  numText,
  pctText,
  statusLabel,
  syncReadout,
  tsText
} from "@/utils/kk";
import { readChartColors, useChartTheme } from "@/utils/kkChart";
import {
  buildDetailOption,
  DETAIL_TABLE_MAX_H,
  detailEmptyText,
  useHostDetail
} from "./utils/hook";

defineOptions({ name: "HostDetail" });

echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer
]);

// 数据流沉 ./utils/hook（QR1121）：错误态分流/记账/纯函数 option 都在那里，可单测
const {
  loading,
  detail,
  hours,
  series,
  pollFailed,
  chartEmpty,
  chartEmptyText,
  hostError,
  hostEmptyText,
  lastLoadedAt,
  disks,
  procs,
  users,
  net,
  labelsList,
  labelsTruncated,
  customEntries,
  descCols,
  exporting,
  upgrading,
  load,
  onExportMetrics,
  onUpgradeOne,
  gotoCommand,
  gotoTrend,
  gotoMonitor
} = useHostDetail();

const chartEl = ref<HTMLDivElement>();
const chart = shallowRef<echarts.ECharts>();
/** 图表配色（QR1121-W11）：运行时读 EP 变量，暗色/主题切换重读重绘 */
const colors = ref(readChartColors());

function renderChart() {
  if (!chartEl.value) return;
  if (!chart.value) chart.value = echarts.init(chartEl.value);
  chart.value.setOption(
    buildDetailOption(series.value, hours.value, colors.value)
  );
}

// flush:post —— 等 v-show 把容器露出来再 init，避免量到 0 宽（ResizeObserver 兜底仍在）
watch(series, renderChart, { flush: "post" });

/** 图表跟随容器宽度：侧边栏折叠、tabs 收放都只改容器尺寸、不触发 window resize，
 *  原来只听 window.addEventListener("resize") 会在这些场景下留一条错位宽度的曲线（FE-15）。 */
useResizeObserver(chartEl, () => chart.value?.resize());

useChartTheme(() => {
  colors.value = readChartColors();
  renderChart();
});

onBeforeUnmount(() => {
  chart.value?.dispose();
  chart.value = undefined;
});
</script>

<template>
  <div v-loading="loading" class="kk-page--grow">
    <el-card v-if="detail" shadow="never" class="kk-card">
      <template #header>
        <div class="kk-toolbar">
          <div>
            <b>{{ detail.pod }}</b>
            <!-- 在线三态与总览/升级页同源（QR1120-W10）：updating 方点不丢 -->
            <span class="kk-ml" :class="hostStateClass(detail)">
              {{ hostStateText(detail) }}
            </span>
            <!-- 告警圆点与同页 kk-meter--alert 同语言（QR1122-W4）：
                 不再用红 tag，严重度词汇全站统一 -->
            <span
              v-if="detail.disk_alert"
              class="kk-state kk-state--bad kk-bad kk-ml"
            >
              磁盘告警
            </span>
            <span class="kk-sub kk-ml"
              >最近心跳 {{ ageText(detail.age_sec) }}</span
            >
            <!-- 新鲜度读数统一口径（QR1121-W8）：不再显静态「每 30 秒」，
                 失败变冷 / 已同步 · 时刻，与 monitor 同族 -->
            <span
              class="kk-sync kk-ml"
              :class="{ 'kk-sync--stale': pollFailed }"
            >
              {{ syncReadout(pollFailed, lastLoadedAt) }}
            </span>
          </div>
          <div class="kk-actions">
            <!-- 回总览锚点（QR1121）：与 trend 回详情对称，下钻链路可逆 -->
            <el-button
              link
              type="primary"
              :icon="useRenderIcon(IconBack)"
              @click="gotoMonitor"
              >总览</el-button
            >
            <el-select v-model="hours" style="width: 120px">
              <el-option label="近 6 小时" :value="6" />
              <el-option label="近 24 小时" :value="24" />
              <el-option label="近 7 天" :value="168" />
            </el-select>
            <el-button :icon="useRenderIcon(IconRefresh)" @click="load()"
              >刷新</el-button
            >
            <el-button :icon="useRenderIcon(IconCommand)" @click="gotoCommand"
              >在此主机执行命令</el-button
            >
            <el-button :icon="useRenderIcon(IconTrend)" @click="gotoTrend"
              >使用趋势</el-button
            >
            <!-- 仅当本机落后于最新版本时显示升级按钮：避免「点了却显示已是最新」的无效路径 -->
            <el-button
              v-if="detail.agent_outdated"
              type="warning"
              :icon="useRenderIcon(IconUpgrade)"
              :loading="upgrading"
              @click="onUpgradeOne"
            >
              升级到 {{ detail.latest_agent_ver }}
            </el-button>
            <el-button
              type="primary"
              :icon="useRenderIcon(IconDownload)"
              :loading="exporting"
              @click="onExportMetrics"
            >
              导出指标
            </el-button>
          </div>
        </div>
      </template>

      <el-descriptions :column="descCols" border class="kk-desc">
        <el-descriptions-item label="镜像">{{
          detail.image || "-"
        }}</el-descriptions-item>
        <el-descriptions-item label="Agent 版本">{{
          detail.agent_ver || "-"
        }}</el-descriptions-item>
        <el-descriptions-item label="上报间隔"
          >{{ detail.hb_interval }} 秒</el-descriptions-item
        >
        <el-descriptions-item label="系统">
          {{ detail.metrics?.os || detail.metrics?.kernel || "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="CPU">
          {{ numText(detail.metrics?.cpu) }}% ·
          {{ detail.metrics?.cpu_cores ?? "-" }} 核 · load
          {{ detail.metrics?.load ?? "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="内存">
          {{ mbText(detail.metrics?.mem_mb) }} /
          {{ mbText(detail.metrics?.mem_total_mb) }} （{{
            numText(detail.metrics?.mem_pct, 0)
          }}%）
        </el-descriptions-item>
        <el-descriptions-item label="磁盘 IO">
          读 {{ numText(detail.metrics?.disk_read_mb, 2) }} MB/s · 写
          {{ numText(detail.metrics?.disk_write_mb, 2) }} MB/s
        </el-descriptions-item>
        <el-descriptions-item label="运行时长">
          <!-- 区间语义用 durText（QR1120-W9）：ageText 的「3 天前」说的是时刻不是时长 -->
          {{ durText(detail.metrics?.uptime_sec) }}
        </el-descriptions-item>
        <!-- v4 建模字段补齐（QR1121）：API 早有、页面此前未露 -->
        <el-descriptions-item label="首次发现">
          <span class="kk-num">{{ tsText(detail.first_seen) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="最近上报">
          <span class="kk-num">{{ tsText(detail.last_seen) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="内存上限">
          {{
            detail.mem_limit === null
              ? "未自定义"
              : pctText(detail.mem_limit, 0)
          }}
        </el-descriptions-item>
        <el-descriptions-item label="磁盘上限">
          {{
            detail.disk_limit === null
              ? `未自定义（全局 ${DISK_ALERT_PCT}%）`
              : pctText(detail.disk_limit, 0)
          }}
        </el-descriptions-item>
        <el-descriptions-item
          v-if="labelsList.length || labelsTruncated"
          label="标签"
          :span="descCols"
        >
          <el-tag
            v-for="[k, v] in labelsList"
            :key="k"
            size="small"
            type="info"
            effect="plain"
            class="kk-label-tag"
            :title="`${k}: ${v}`"
          >
            {{ k }}: {{ v }}
          </el-tag>
          <span v-if="labelsTruncated" class="kk-sub"
            >超限标签已被服务端丢弃</span
          >
        </el-descriptions-item>
      </el-descriptions>

      <!-- 自定义采集（QR1121）：有键才渲染，kk-kv 流式与审计明细同族 -->
      <template v-if="customEntries.length">
        <h4 class="kk-h4 kk-mt-lg">
          <el-icon><component :is="useRenderIcon(IconCollect)" /></el-icon
          >自定义采集
        </h4>
        <div>
          <span v-for="[k, v] in customEntries" :key="k" class="kk-kv">
            <b>{{ k }}</b
            >:
            {{ typeof v === "object" ? JSON.stringify(v) : String(v) }}
          </span>
        </div>
      </template>

      <!-- 空数据占位（QR1120-W17）：与趋势页 trend-empty 同族；
           图表容器用 v-show 保活，echarts 实例与 ResizeObserver 不重建 -->
      <div v-if="chartEmpty" class="kk-chart-empty">
        {{ chartEmptyText }}
      </div>
      <div v-show="!chartEmpty" ref="chartEl" class="kk-chart" />

      <!-- 与总览页同族的响应式栅格（:xs/:lg）：1366 笔记本窄屏不再挤压 -->
      <el-row :gutter="16" class="kk-mt-lg">
        <el-col :xs="24" :lg="12">
          <h4 class="kk-h4">
            <el-icon><component :is="useRenderIcon(IconDisk)" /></el-icon>磁盘
          </h4>
          <el-table :data="disks" size="small" :max-height="DETAIL_TABLE_MAX_H">
            <template #empty>{{ detailEmptyText("磁盘") }}</template>
            <el-table-column prop="mount" label="挂载点" min-width="120" />
            <el-table-column label="已用 / 总量" min-width="160">
              <template #default="{ row }">
                {{ mbText(row.used_mb) }} / {{ mbText(row.total_mb) }}
              </template>
            </el-table-column>
            <el-table-column label="使用率" min-width="160">
              <template #default="{ row }">
                <!-- 计量语言与 monitor 统一（QR1121）：kk-meter 替代 el-progress。
                     告警判级两源不同是刻意的：detail 逐挂载点 pct ≥ 阈值，
                     monitor 用服务端汇总的 disk_alert（多挂载点取最大）。 -->
                <span
                  class="kk-meter"
                  :class="{ 'kk-meter--alert': row.pct >= DISK_ALERT_PCT }"
                >
                  <span class="kk-meter__bar">
                    <i :style="{ width: fillPct(row.pct) + '%' }" />
                  </span>
                  <span class="kk-meter__num">{{ pctText(row.pct, 0) }}</span>
                </span>
              </template>
            </el-table-column>
          </el-table>
        </el-col>
        <el-col :xs="24" :lg="12">
          <h4 class="kk-h4">
            <el-icon><component :is="useRenderIcon(IconNet)" /></el-icon
            >网卡速率（MB/s）
          </h4>
          <el-table :data="net" size="small" :max-height="DETAIL_TABLE_MAX_H">
            <template #empty>{{ detailEmptyText("网卡") }}</template>
            <el-table-column prop="nic" label="网卡" min-width="120" />
            <el-table-column label="发送" min-width="90">
              <template #default="{ row }">{{
                numText(row.sent_mb, 3)
              }}</template>
            </el-table-column>
            <el-table-column label="接收" min-width="90">
              <template #default="{ row }">{{
                numText(row.recv_mb, 3)
              }}</template>
            </el-table-column>
            <el-table-column label="pps（发/收）" min-width="120">
              <template #default="{ row }">
                {{ numText(row.sent_pps, 0) }} / {{ numText(row.recv_pps, 0) }}
              </template>
            </el-table-column>
          </el-table>
        </el-col>
      </el-row>

      <el-row :gutter="16" class="kk-mt-lg">
        <el-col :xs="24" :lg="14">
          <h4 class="kk-h4">
            <el-icon><component :is="useRenderIcon(IconProc)" /></el-icon>Top
            进程（按 CPU）
          </h4>
          <el-table :data="procs" size="small" :max-height="DETAIL_TABLE_MAX_H">
            <template #empty>{{ detailEmptyText("进程") }}</template>
            <el-table-column prop="pid" label="PID" width="80" />
            <el-table-column prop="name" label="进程" min-width="140" />
            <el-table-column prop="user" label="用户" min-width="110" />
            <el-table-column label="CPU %" width="90">
              <template #default="{ row }">{{ numText(row.cpu) }}</template>
            </el-table-column>
            <el-table-column label="内存" width="100">
              <template #default="{ row }">{{ mbText(row.mem_mb) }}</template>
            </el-table-column>
          </el-table>
        </el-col>
        <el-col :xs="24" :lg="10">
          <h4 class="kk-h4">
            <el-icon><component :is="useRenderIcon(IconUser)" /></el-icon
            >登录用户
          </h4>
          <el-table :data="users" size="small" :max-height="DETAIL_TABLE_MAX_H">
            <template #empty>{{ detailEmptyText("登录用户") }}</template>
            <el-table-column prop="name" label="用户" width="110" />
            <el-table-column prop="terminal" label="终端" width="90" />
            <el-table-column prop="host" label="来源" min-width="120" />
            <el-table-column label="登录时间" min-width="150">
              <template #default="{ row }">
                <span class="kk-num">{{ tsText(row.started) }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-col>
      </el-row>

      <h4 class="kk-h4 kk-mt-lg">
        <el-icon><component :is="useRenderIcon(IconCommand)" /></el-icon
        >最近命令
      </h4>
      <el-table
        :data="detail.commands"
        size="small"
        :max-height="DETAIL_TABLE_MAX_H"
      >
        <!-- 命令表随详情 payload 到达：读到了才有这张表，空就是真没有 -->
        <template #empty>还没有命令记录</template>
        <el-table-column prop="id" label="ID" width="120" />
        <el-table-column label="类型" width="100">
          <template #default="{ row }">{{ kindText(row.kind) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <!-- 圆点四档与命令历史/更新台账同语言（QR1121-W2） -->
            <span :class="cmdStateClass(row.status)">{{
              statusLabel(row.status)
            }}</span>
          </template>
        </el-table-column>
        <el-table-column
          label="输出（末 2KB）"
          min-width="240"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            {{
              row.out_purged ? "（输出已按保留策略清理）" : row.out_tail || "-"
            }}
          </template>
        </el-table-column>
        <el-table-column label="创建" width="160">
          <template #default="{ row }">
            <span class="kk-num">{{ tsText(row.created_at) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 全失败态（QR1121-W9）：404 与不可达分流——接口挂了不许谎报「主机不存在」；
         过期指示与重试在详情缺失时同样可见（原实现横幅藏在 v-if=detail 的 header 里） -->
    <el-card v-else shadow="never" class="kk-card">
      <div v-if="pollFailed" class="kk-sync kk-sync--stale kk-mb">
        刷新失败，数据可能已过期
      </div>
      <el-empty :description="hostEmptyText">
        <el-button
          v-if="hostError === 'unreachable'"
          :icon="useRenderIcon(IconRetry)"
          @click="load()"
          >重试</el-button
        >
      </el-empty>
    </el-card>
  </div>
</template>

<!-- 通用类（kk-toolbar/kk-desc/kk-chart/kk-h4 等）统一在 style/kk.scss -->

<style scoped lang="scss">
/* 图表空数据占位（QR1120-W17）：高度与 .kk-chart 同口径，切换时不跳布局 */
.kk-chart-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 300px;
  margin: var(--kk-space-lg) 0 0;
  font-size: 13px;
  color: var(--el-text-color-placeholder);
}

/* 标签 tag 间距（QR1121）：四档尺度，窄屏换行不贴边。
   长 k:v 溢出保护（QR1122）：max-width + 内容 ellipsis，title 已给全值 */
.kk-label-tag {
  max-width: 100%;
  margin: 0 var(--kk-space-sm) var(--kk-space-sm) 0;

  :deep(.el-tag__content) {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

/* 窄屏堆叠时两栏小节的纵向缝（QR1122-W5）：gutter 只管横向 */
.kk-page--grow > .el-row {
  row-gap: var(--kk-space-lg);
}
</style>
