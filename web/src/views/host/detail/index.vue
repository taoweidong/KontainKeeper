<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
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
import { ageText, mbText, numText, statusLabel, statusType, tsText } from "@/utils/kk";

defineOptions({ name: "HostDetail" });

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

const route = useRoute();
const pod = computed(() => String(route.params.pod || ""));

const loading = ref(false);
const detail = ref<HostDetail | null>(null);
const hours = ref(24);
const chartEl = ref<HTMLDivElement>();
const chart = shallowRef<echarts.ECharts>();
let timer: ReturnType<typeof setInterval> | null = null;

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

function renderChart(series: Array<{ ts: number; cpu: number | null; mem_mb: number | null }>) {
  if (!chartEl.value) return;
  if (!chart.value) chart.value = echarts.init(chartEl.value);
  const times = series.map(p =>
    new Date(p.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false })
  );
  chart.value.setOption({
    tooltip: { trigger: "axis" },
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

async function loadMetrics() {
  try {
    const data = await getHostMetrics(pod.value, hours.value);
    renderChart(data.series);
  } catch (e: any) {
    ElMessage.error("加载指标序列失败：" + (e?.message ?? e));
  }
}

async function load() {
  if (!pod.value) return;
  loading.value = true;
  try {
    detail.value = await getHost(pod.value);
  } catch (e: any) {
    ElMessage.error("加载主机详情失败：" + (e?.message ?? e));
  } finally {
    loading.value = false;
  }
  await loadMetrics();
}

function onResize() {
  chart.value?.resize();
}

watch(hours, () => loadMetrics());

onMounted(async () => {
  await load();
  timer = setInterval(load, 30000); // 详情与图表 30s：曲线不需要秒级新鲜度
  window.addEventListener("resize", onResize);
});

onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
  timer = null;
  window.removeEventListener("resize", onResize);
  chart.value?.dispose();
  chart.value = undefined;
});
</script>

<template>
  <div v-loading="loading">
    <el-card v-if="detail" shadow="never" class="kk-card">
      <template #header>
        <div class="kk-toolbar">
          <div>
            <b>{{ detail.pod }}</b>
            <el-tag
              class="kk-ml"
              size="small"
              :type="detail.online ? 'success' : 'info'"
            >
              {{ detail.online ? "在线" : "离线" }}
            </el-tag>
            <el-tag v-if="detail.disk_alert" class="kk-ml" size="small" type="danger">
              磁盘告警
            </el-tag>
            <span class="kk-sub kk-ml">最近心跳 {{ ageText(detail.age_sec) }}</span>
          </div>
          <div class="kk-actions">
            <el-select v-model="hours" style="width: 120px">
              <el-option label="近 6 小时" :value="6" />
              <el-option label="近 24 小时" :value="24" />
              <el-option label="近 7 天" :value="168" />
            </el-select>
            <el-button @click="load">刷新</el-button>
          </div>
        </div>
      </template>

      <el-descriptions :column="4" border class="kk-desc">
        <el-descriptions-item label="镜像">{{ detail.image || "-" }}</el-descriptions-item>
        <el-descriptions-item label="Agent 版本">{{ detail.agent_ver || "-" }}</el-descriptions-item>
        <el-descriptions-item label="上报间隔">{{ detail.hb_interval }} 秒</el-descriptions-item>
        <el-descriptions-item label="系统">
          {{ detail.metrics?.sys?.os || detail.metrics?.kernel || "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="CPU">
          {{ numText(detail.metrics?.cpu) }}% · {{ detail.metrics?.cpu_cores ?? "-" }} 核 ·
          load {{ detail.metrics?.load || "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="内存">
          {{ mbText(detail.metrics?.mem_mb) }} / {{ mbText(detail.metrics?.mem_total_mb) }}
          （{{ numText(detail.metrics?.mem_pct, 0) }}%）
        </el-descriptions-item>
        <el-descriptions-item label="磁盘 IO">
          读 {{ numText(detail.metrics?.disk_read_mb, 2) }} MB/s ·
          写 {{ numText(detail.metrics?.disk_write_mb, 2) }} MB/s
        </el-descriptions-item>
        <el-descriptions-item label="运行时长">
          {{ detail.metrics?.sys?.uptime_sec ? ageText(detail.metrics.sys.uptime_sec) : "-" }}
        </el-descriptions-item>
      </el-descriptions>

      <div ref="chartEl" class="kk-chart" />

      <el-row :gutter="16">
        <el-col :span="12">
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
                  :percentage="Math.min(100, Math.round(row.pct || 0))"
                  :status="row.pct >= 85 ? 'exception' : undefined"
                />
              </template>
            </el-table-column>
          </el-table>
        </el-col>
        <el-col :span="12">
          <h4 class="kk-h4">网卡速率（MB/s）</h4>
          <el-table :data="net" size="small" max-height="220">
            <el-table-column prop="nic" label="网卡" min-width="120" />
            <el-table-column label="发送" min-width="90">
              <template #default="{ row }">{{ numText(row.sent_mb, 3) }}</template>
            </el-table-column>
            <el-table-column label="接收" min-width="90">
              <template #default="{ row }">{{ numText(row.recv_mb, 3) }}</template>
            </el-table-column>
            <el-table-column label="pps（发/收）" min-width="120">
              <template #default="{ row }">
                {{ numText(row.sent_pps, 0) }} / {{ numText(row.recv_pps, 0) }}
              </template>
            </el-table-column>
          </el-table>
        </el-col>
      </el-row>

      <el-row :gutter="16" class="kk-mt">
        <el-col :span="14">
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
        <el-col :span="10">
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

      <h4 class="kk-h4 kk-mt">最近命令</h4>
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
        <el-table-column label="输出（末 2KB）" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.out_purged ? "（输出已按保留策略清理）" : row.out_tail || "-" }}
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

<style scoped>
.kk-card {
  margin: 16px;
}
.kk-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}
.kk-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.kk-ml {
  margin-left: 10px;
}
.kk-sub {
  font-size: 12px;
  color: #909399;
}
.kk-chart {
  height: 300px;
  margin: 16px 0;
}
.kk-desc {
  margin-bottom: 8px;
}
.kk-h4 {
  margin: 12px 0 8px;
  font-size: 14px;
  color: #303133;
}
.kk-mt {
  margin-top: 8px;
}
</style>
