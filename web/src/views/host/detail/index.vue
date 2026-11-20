<script setup lang="ts">
import {
  computed,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef,
  watch
} from "vue";
import { useRoute, useRouter } from "vue-router";
import { useResizeObserver, useWindowSize } from "@vueuse/core";
import { ElMessage } from "element-plus";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

import { getHost, getHostMetrics, type HostDetail } from "@/api/containers";
import { upgradeHosts } from "@/api/agent";
import { exportMetrics } from "@/api/exporting";
import {
  ageText,
  DISK_ALERT_PCT,
  downloadBlob,
  durText,
  errText,
  fileStamp,
  fillPct,
  hostStateClass,
  hostStateText,
  mbText,
  numText,
  statusLabel,
  statusType,
  tsText,
  upgradeSkipText
} from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";

defineOptions({ name: "HostDetail" });

echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer
]);

const route = useRoute();
const router = useRouter();
const pod = computed(() => String(route.params.pod || ""));
// 作用域版 setPoll：卸载时只清本组件注册的 key
const { setPoll } = usePolls();
/** 本页的 load 有两个入口（30s 轮询 + 升级/手动刷新），kkPoll 的在途防重入只护定时器那路：
 *  手动请求撞上在途轮询时，谁后回包谁就写界面 —— useSeq 让迟到的那一路整条丢弃（QR-W4）。 */
const beginLoad = useSeq();
const beginMetrics = useSeq();

const loading = ref(false);
const detail = ref<HostDetail | null>(null);
const hours = ref(24);
const chartEl = ref<HTMLDivElement>();
const chart = shallowRef<echarts.ECharts>();

const disks = computed(() => {
  const d = (detail.value?.metrics?.disks || {}) as Record<string, any>;
  return Object.entries(d).map(([mount, v]) => ({ mount, ...(v as object) }));
});
const procs = computed(() => (detail.value?.metrics?.procs_top || []) as any[]);
const users = computed(() => (detail.value?.metrics?.users || []) as any[]);
const net = computed(() => {
  const n = (detail.value?.metrics?.net || {}) as Record<string, any>;
  return Object.entries(n).map(([nic, v]) => ({ nic, ...(v as object) }));
});

/** 描述列表列数随窗口宽度收敛：≥1200px 四列，否则两列（el-descriptions 不支持断点） */
const { width: winWidth } = useWindowSize();
const descCols = computed(() => (winWidth.value >= 1200 ? 4 : 2));

function renderChart(
  series: Array<{ ts: number; cpu: number | null; mem_mb: number | null }>
) {
  if (!chartEl.value) return;
  if (!chart.value) chart.value = echarts.init(chartEl.value);
  // x 轴刻度随窗口变化：跨天窗口（近 7 天）必须带「月-日」，否则一屏全是重复的钟点
  const fmt: Intl.DateTimeFormatOptions =
    hours.value > 24
      ? {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false
        }
      : {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false
        };
  const labelFmt = new Intl.DateTimeFormat("zh-CN", fmt);
  // toLocaleString 每次都新建一个 DateTimeFormat，24h 窗口 1440 点就是 1440 次；
  // 格式化器建一次、逐点复用，输出的文案与原来逐字相同（FE-16）。
  const times = series.map(p => labelFmt.format(new Date(p.ts * 1000)));
  chart.value.setOption({
    tooltip: {
      trigger: "axis",
      // 双轴单位不同（CPU % / 内存 MB）：valueFormatter 拿不到 series 上下文，
      // 用 formatter 逐条按 seriesName 配单位（QR1120-W16）；null 点显 - 不画 0
      formatter: (params: any) => {
        const list = Array.isArray(params) ? params : [params];
        if (!list.length) return "";
        const lines = list.map((p: any) => {
          const unit = p.seriesName === "CPU %" ? "%" : " MB";
          const v =
            p.value === null || p.value === undefined
              ? "-"
              : numText(Number(p.value), 1) + unit;
          return `${p.marker}${p.seriesName}: ${v}`;
        });
        return [list[0].axisValueLabel ?? list[0].name, ...lines].join("<br/>");
      }
    },
    legend: { data: ["CPU %", "内存 MB"], right: 0 },
    grid: { left: 56, right: 60, top: 36, bottom: 36 },
    xAxis: { type: "category", data: times, boundaryGap: false },
    yAxis: [
      { type: "value", name: "CPU %", max: 100, min: 0 },
      { type: "value", name: "MB" }
    ],
    series: [
      {
        name: "CPU %",
        type: "line",
        showSymbol: false,
        smooth: true,
        areaStyle: { opacity: 0.15 },
        data: series.map(p => p.cpu)
      },
      {
        name: "内存 MB",
        type: "line",
        yAxisIndex: 1,
        showSymbol: false,
        smooth: true,
        data: series.map(p => p.mem_mb)
      }
    ]
  });
}

/** 静默轮询失败：状态进页头读数，不再每 30s 刷一次 toast（W5）。
 *  详情页即使离线主机也常开（挂着看曲线），所以这条读数必须说清「图上是旧数据」。 */
const pollFailed = ref(false);

/** 图表空数据态（QR1120-W17）：序列为空时给占位文案，不画一副空坐标系。
 *  初始为 true——首帧指标到达前同样没有可画的数据。失败时不动它：
 *  保留上一批曲线是 pollFailed 的既定语义。 */
const chartEmpty = ref(true);

/** 占位文案分两种（QR-W16 红线）：从没读到过 ≠ 读到了但窗口里没点 */
const chartEmptyText = computed(() =>
  pollFailed.value && chartEmpty.value
    ? "没读到（指标接口不可达）"
    : "该窗口无数据，等一帧心跳再回来看"
);

/** silent=true 供轮询复用：曲线失败不刷 toast，交页头读数 */
async function loadMetrics(silent = false) {
  const isLatest = beginMetrics();
  try {
    const data = await getHostMetrics(pod.value, hours.value);
    if (!isLatest()) return;
    chartEmpty.value = data.series.length === 0;
    renderChart(data.series);
    pollFailed.value = false;
  } catch (e: any) {
    if (!isLatest()) return;
    // 手动刷新失败同样要变冷（QR-W17，与 system/welcome 口径一致）——
    // 这台主机挂着看曲线，绿色「已同步」会把旧数据谎报成新鲜读数。
    pollFailed.value = true;
    if (!silent) ElMessage.error("加载指标序列失败：" + errText(e));
  }
}

/** silent=true 供轮询复用：详情与曲线原位更新，不闪整页 loading */
async function load(silent = false) {
  if (!pod.value) return;
  const isLatest = beginLoad();
  if (!silent) loading.value = true;
  let hostFailed = false;
  try {
    const data = await getHost(pod.value);
    if (!isLatest()) return;
    detail.value = data;
  } catch (e: any) {
    if (!isLatest()) return;
    hostFailed = true;
    if (!silent) ElMessage.error("加载主机详情失败：" + errText(e));
  } finally {
    if (isLatest()) loading.value = false;
  }
  await loadMetrics(silent);
  // getHost 失败而曲线成功时页头也要变冷（QR1120-W11，welcome 的 OR 口径）：
  // 读数说的是整页新鲜度，详情卡是旧数据就不能继续显绿色「自动刷新」。
  if (hostFailed) pollFailed.value = true;
}

/** 图表跟随容器宽度：侧边栏折叠、tabs 收放都只改容器尺寸、不触发 window resize，
 *  原来只听 window.addEventListener("resize") 会在这些场景下留一条错位宽度的曲线（FE-15）。 */
useResizeObserver(chartEl, () => chart.value?.resize());

const exporting = ref(false);
const upgrading = ref(false);

/** 导出当前时间窗口的指标曲线（与图上 hours 一致，>24h 走服务端小时聚合表）。 */
async function onExportMetrics() {
  exporting.value = true;
  try {
    downloadBlob(
      await exportMetrics(pod.value, hours.value),
      `指标_${pod.value}_${hours.value}h_${fileStamp()}.csv`
    );
  } catch (e: any) {
    ElMessage.error("导出失败：" + errText(e));
  } finally {
    exporting.value = false;
  }
}

/** 单机升级：直接复用选机升级端点，单台与批量走同一条路，UI 语义一致（D2.4）。 */
async function onUpgradeOne() {
  if (!detail.value) return;
  upgrading.value = true;
  try {
    const r = await upgradeHosts([detail.value.pod]);
    if (r.accepted.length) {
      const a = r.accepted[0];
      ElMessage.success(a.queued ? `已排队，主机上线后自动补投` : `已下发`);
    } else if (r.skipped.length) {
      // 跳过原因要说人话：只报「已跳过」运维会以为是失败并反复重试
      ElMessage.warning(upgradeSkipText(r.skipped[0].reason));
    }
    await load();
  } catch (e: any) {
    ElMessage.error("升级失败：" + errText(e));
  } finally {
    upgrading.value = false;
  }
}

watch(hours, () => loadMetrics());

/** 同页换主机（从别的入口改 route.params.pod）不会重建组件，
 *  这里显式重载——原来只是碰巧靠底座 lay-content 的 :key="fullPath" 强制重建才没出事，
 *  那份运气不属于本页（FE-14）。 */
watch(pod, () => load());

/** 在此主机执行命令：复用既有的 ?pods= 契约，不再从别处绕 */
function gotoCommand() {
  router.push({ name: "CommandShell", query: { pods: pod.value } });
}

/** 单机使用趋势（内存/磁盘百分比 + 告警上限）：详情的下钻页 */
function gotoTrend() {
  router.push({ name: "HostTrend", params: { pod: pod.value } });
}

onMounted(async () => {
  await load();
  // 详情与图表 30s：曲线不需要秒级新鲜度
  setPoll("host-detail", () => load(true), 30000);
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
            <el-tag
              v-if="detail.disk_alert"
              class="kk-ml"
              size="small"
              type="danger"
            >
              磁盘告警
            </el-tag>
            <span class="kk-sub kk-ml"
              >最近心跳 {{ ageText(detail.age_sec) }}</span
            >
            <span
              class="kk-sync kk-ml"
              :class="{ 'kk-sync--stale': pollFailed }"
            >
              {{
                pollFailed
                  ? "自动刷新失败，读数可能已过期"
                  : "每 30 秒自动刷新"
              }}
            </span>
          </div>
          <div class="kk-actions">
            <el-select v-model="hours" style="width: 120px">
              <el-option label="近 6 小时" :value="6" />
              <el-option label="近 24 小时" :value="24" />
              <el-option label="近 7 天" :value="168" />
            </el-select>
            <el-button @click="load()">刷新</el-button>
            <el-button @click="gotoCommand">在此主机执行命令</el-button>
            <el-button @click="gotoTrend">使用趋势</el-button>
            <!-- 仅当本机落后于最新版本时显示升级按钮：避免「点了却显示已是最新」的无效路径 -->
            <el-button
              v-if="detail.agent_outdated"
              type="warning"
              :loading="upgrading"
              @click="onUpgradeOne"
            >
              升级到 {{ detail.latest_agent_ver }}
            </el-button>
            <el-button
              type="primary"
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
      </el-descriptions>

      <!-- 空数据占位（QR1120-W17）：与趋势页 trend-empty 同族；
           图表容器用 v-show 保活，echarts 实例与 ResizeObserver 不重建 -->
      <div v-if="chartEmpty" class="kk-chart-empty">
        {{ chartEmptyText }}
      </div>
      <div v-show="!chartEmpty" ref="chartEl" class="kk-chart" />

      <!-- 与总览页同族的响应式栅格（:xs/:lg）：1366 笔记本窄屏不再挤压 -->
      <el-row :gutter="16" class="kk-mt-lg">
        <el-col :xs="24" :lg="12">
          <h4 class="kk-h4">磁盘</h4>
          <el-table :data="disks" size="small" max-height="220">
            <el-table-column prop="mount" label="挂载点" min-width="120" />
            <el-table-column label="已用 / 总量" min-width="160">
              <template #default="{ row }">
                {{ mbText(row.used_mb) }} / {{ mbText(row.total_mb) }}
              </template>
            </el-table-column>
            <el-table-column label="使用率" min-width="160">
              <template #default="{ row }">
                <el-progress
                  :percentage="fillPct(row.pct)"
                  :status="row.pct >= DISK_ALERT_PCT ? 'exception' : undefined"
                />
              </template>
            </el-table-column>
          </el-table>
        </el-col>
        <el-col :xs="24" :lg="12">
          <h4 class="kk-h4">网卡速率（MB/s）</h4>
          <el-table :data="net" size="small" max-height="220">
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
          <h4 class="kk-h4">Top 进程（按 CPU）</h4>
          <el-table :data="procs" size="small" max-height="260">
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
          <h4 class="kk-h4">登录用户</h4>
          <el-table :data="users" size="small" max-height="260">
            <el-table-column prop="name" label="用户" width="110" />
            <el-table-column prop="terminal" label="终端" width="90" />
            <el-table-column prop="host" label="来源" min-width="120" />
            <el-table-column label="登录时间" min-width="150">
              <template #default="{ row }">{{ tsText(row.started) }}</template>
            </el-table-column>
          </el-table>
        </el-col>
      </el-row>

      <h4 class="kk-h4 kk-mt-lg">最近命令</h4>
      <el-table :data="detail.commands" size="small" max-height="240">
        <el-table-column prop="id" label="ID" width="120" />
        <el-table-column prop="kind" label="类型" width="100" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">
              {{ statusLabel(row.status) }}
            </el-tag>
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
          <template #default="{ row }">{{ tsText(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-else shadow="never" class="kk-card">
      <el-empty description="主机不存在或尚未上报" />
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
</style>
