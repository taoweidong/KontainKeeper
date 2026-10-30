<script setup lang="ts">
import { computed, watch } from "vue";
import { useRouter } from "vue-router";
import {
  HOUR_OPTIONS,
  isBreach,
  buildTrendOption,
  useHostTrend,
  useTrendChart
} from "./utils/hook";

defineOptions({ name: "HostTrend" });

const router = useRouter();

const {
  pod,
  hours,
  metrics,
  loading,
  pollFailed,
  read,
  emptyText,
  memInput,
  diskInput,
  saving,
  saveLimits,
  load
} = useHostTrend();

/** 时间轴文案：6h/24h 精确到分，7d 精确到小时——格式化器建一次复用（FE-16） */
const fmt = computed(() => {
  const parts: Intl.DateTimeFormatOptions =
    hours.value > 24
      ? { month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false }
      : { hour: "2-digit", minute: "2-digit", hour12: false };
  const f = new Intl.DateTimeFormat("zh-CN", parts);
  return (ts: number) => f.format(new Date(ts * 1000));
});

const series = computed(() => metrics.value?.series ?? []);
const memLimit = computed(() => metrics.value?.mem_limit ?? null);
const diskLimit = computed(() => metrics.value?.disk_limit ?? null);

/** 页头读数取序列最新一个非空点（末点可能是 null：老 Agent 的帧） */
const latest = computed(() => {
  for (let i = series.value.length - 1; i >= 0; i--) {
    const p = series.value[i];
    if (p.mem_pct !== null || p.disk_pct !== null) return p;
  }
  return null;
});

const memChart = useTrendChart(() =>
  series.value.length
    ? buildTrendOption(series.value, {
        key: "mem_pct",
        lineName: "内存使用率",
        limit: memLimit.value,
        fmt: fmt.value
      })
    : null
);
const diskChart = useTrendChart(() =>
  series.value.length
    ? buildTrendOption(series.value, {
        key: "disk_pct",
        lineName: "磁盘使用率（多挂载点最大）",
        limit: diskLimit.value,
        fmt: fmt.value
      })
    : null
);

/** 数据/上限/时间窗变化后重画两图：computed 依赖变化不会自动触发 setOption，
 *  读数与图表是两条渲染链，这里显式桥接（引用都是整体替换，浅监听足够） */
watch([series, memLimit, diskLimit, fmt], () => {
  memChart.render();
  diskChart.render();
});

/** 回详情页（趋势页是详情的下钻，来路锚点放页头） */
function gotoDetail() {
  router.push({ name: "HostDetail", params: { pod: pod.value } });
}
</script>

<template>
  <div class="kk-page">
    <div class="kk-page__body">
      <!-- 页头：主机锚点 + 最新读数（越限给红点）+ 上限编辑 + 时间窗。
           上限编辑框的初始值是单机覆盖原值（未自定义 = 空），
           磁盘的画线口径（覆盖 ?? 全局 85）由服务端算好放进 metrics 响应 -->
      <div class="bg-bg_color w-full pl-8 pr-8 pt-3 pb-3">
        <div class="kk-toolbar">
          <div class="kk-band">
            <el-button link type="primary" @click="gotoDetail">
              {{ pod || "未知主机" }}
            </el-button>
            <span class="kk-band__readout">
              <b>{{ latest?.mem_pct ?? "—" }}</b>
              内存 %
              <span
                v-if="isBreach(latest?.mem_pct, memLimit)"
                class="kk-state kk-state--bad kk-bad"
                >越限</span
              >
            </span>
            <span class="kk-band__readout">
              <b>{{ latest?.disk_pct ?? "—" }}</b>
              磁盘 %
              <span
                v-if="isBreach(latest?.disk_pct, diskLimit)"
                class="kk-state kk-state--bad kk-bad"
                >越限</span
              >
            </span>
            <el-tooltip
              content="自动刷新最近一次成功的时间；失败时图表保持上一批数据"
              placement="bottom"
            >
              <span class="kk-sync" :class="{ 'kk-sync--stale': pollFailed }">
                {{
                  pollFailed
                    ? "自动刷新失败，图表可能已过期"
                    : "每 30 秒自动刷新"
                }}
              </span>
            </el-tooltip>
          </div>
          <div class="kk-actions">
            <span class="kk-sub">内存上限</span>
            <el-input-number
              v-model="memInput"
              :min="1"
              :max="100"
              :controls="false"
              placeholder="未设置"
              style="width: 76px"
            />
            <span class="kk-sub">磁盘上限</span>
            <el-input-number
              v-model="diskInput"
              :min="1"
              :max="100"
              :controls="false"
              placeholder="全局 85"
              style="width: 76px"
            />
            <el-button type="primary" :loading="saving" @click="saveLimits">
              保存上限
            </el-button>
            <el-select v-model="hours" style="width: 120px">
              <el-option
                v-for="o in HOUR_OPTIONS"
                :key="o.value"
                :label="o.label"
                :value="o.value"
              />
            </el-select>
            <el-button :loading="loading" @click="load()">刷新</el-button>
          </div>
        </div>
      </div>

      <!-- 双图：内存 | 磁盘各占一半，≤1200px 折上下（运维笔记本）。
           图表高度交给 ResizeObserver 自适应（FE-15 既定机制），
           容器只负责给出确定高度（flex:1 + min-height:0）。
           空态区分「没读到」与「确实没有点」（QR-W16） -->
      <div class="trend-grid">
        <div class="bg-bg_color trend-card">
          <div class="kk-h4">内存使用率 %</div>
          <div v-if="!read || !series.length" class="trend-empty">
            {{ emptyText }}
          </div>
          <div
            v-show="read && series.length"
            :ref="memChart.el"
            class="trend-chart"
          />
        </div>
        <div class="bg-bg_color trend-card">
          <div class="kk-h4">磁盘使用率 %（多挂载点取最大）</div>
          <div v-if="!read || !series.length" class="trend-empty">
            {{ emptyText }}
          </div>
          <div
            v-show="read && series.length"
            :ref="diskChart.el"
            class="trend-chart"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
/* 双图栅格：宽屏并排各半，窄屏折上下 */
.trend-grid {
  display: grid;
  flex: 1;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  min-height: 0;
  margin-top: 12px;
}

.trend-card {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 12px 12px 4px;
}

.trend-chart {
  flex: 1;
  min-height: 0;
}

.trend-empty {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: var(--el-text-color-placeholder);
}

@media (width <= 1200px) {
  .trend-grid {
    grid-template-columns: 1fr;
  }
}
</style>
