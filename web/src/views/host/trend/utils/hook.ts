import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useResizeObserver } from "@vueuse/core";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import {
  GridComponent,
  MarkLineComponent,
  TooltipComponent
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsType } from "echarts/core";
import { message } from "@/utils/message";
import {
  getHostMetrics,
  setHostLimits,
  type MetricPoint,
  type MetricsResult
} from "@/api/containers";
import { errText } from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";

/**
 * 单机使用趋势页（内存 / 磁盘百分比 + 告警上限）。
 *
 * 两条曲线都是**百分比**口径：mem_pct 是采集器原值，disk_pct 是多挂载点最大值
 * （哪块盘快满看哪块，语义与总览页 disk_alert 一致）；序列形状 raw/hourly 双路
 * 统一，null = 该点没数据（老 Agent / 采集被关），ECharts 画断线而不是 0。
 *
 * 上限语义在服务端算好下发（D1.3 判例）：metrics 响应带 disk_limit（有效值，
 * 单机覆盖 ?? 全局 85）与 mem_limit（单机覆盖原值，null = 不画线）；
 * 保存走 PUT /containers/{pod}/limits，清除（null）回落全局口径。
 */

// echarts 注册只取本页用到的；与 detail 页各管各的 use 列表（无全局单例负担）
echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  MarkLineComponent,
  CanvasRenderer
]);

/** 时间窗档位：6h/24h 走原始帧，7d 自动切小时聚合（后端 metrics_series 语义） */
export const HOUR_OPTIONS = [
  { label: "近 6 小时", value: 6 },
  { label: "近 24 小时", value: 24 },
  { label: "近 7 天", value: 168 }
];

/** 上限取值校验：null = 清除（合法）；否则必须落在 1–100 的整数区间。
 *  返回 null 表示合法，返回字符串就是给输入框下面那行红字的文案。 */
export function limitError(v: number | null): string | null {
  if (v === null || v === undefined) return null;
  if (!Number.isInteger(v) || v < 1 || v > 100)
    return "上限需为 1–100 的整数（百分比）";
  return null;
}

/** 是否越限：任一侧没数据都不算越限（没读到不是告警，QR-W16 口径） */
export function isBreach(
  pct: number | null | undefined,
  limit: number | null | undefined
): boolean {
  if (
    pct === null ||
    pct === undefined ||
    limit === null ||
    limit === undefined
  )
    return false;
  return pct >= limit;
}

/** 图表 option 组装（纯函数，可测）。
 *  - 两个 series 共用一条 0–100 的 y 轴（百分比口径的直白好处）；
 *  - 上限线用 markLine：EP danger 默认色 #f56c6c——echarts 不解析 CSS 变量，
 *    写字面量并注明来源，改主题时这里要跟着 kk.scss 的 --kk-alert 对一遍；
 *  - x 轴文案由调用方注入的 fmt 做一次性格式化（FE-16：热路径不新建 Intl）。 */
export function buildTrendOption(
  points: MetricPoint[],
  opts: {
    key: "mem_pct" | "disk_pct";
    lineName: string;
    limit: number | null;
    fmt: (ts: number) => string;
  }
): echarts.EChartsCoreOption {
  const data = points.map(p => p[opts.key]);
  return {
    tooltip: { trigger: "axis" },
    grid: { left: 44, right: 20, top: 30, bottom: 32 },
    xAxis: {
      type: "category",
      data: points.map(p => opts.fmt(p.ts)),
      boundaryGap: false
    },
    yAxis: {
      type: "value",
      min: 0,
      max: 100,
      axisLabel: { formatter: "{value}%" }
    },
    series: [
      {
        name: opts.lineName,
        type: "line",
        showSymbol: false,
        smooth: true,
        areaStyle: { opacity: 0.15 },
        connectNulls: false,
        data,
        ...(opts.limit === null
          ? {}
          : {
              markLine: {
                symbol: "none",
                silent: true,
                lineStyle: { color: "#f56c6c", type: "dashed" },
                label: {
                  formatter: "告警上限 {c}%",
                  position: "insideEndTop",
                  color: "#f56c6c"
                },
                data: [{ yAxis: opts.limit }]
              }
            })
      }
    ]
  };
}

export function useHostTrend() {
  const route = useRoute();
  const POLL_KEY = "host-trend";
  /** pod 来自路由参数；详情页/总览跳转都带真实 pod，参数缺失是异常态 */
  const pod = computed(() => String(route.params.pod || ""));

  const loading = ref(false);
  const metrics = ref<MetricsResult | null>(null);
  const hours = ref(24);
  /** 静默轮询失败：状态进页头读数，不刷 toast（W5，全站统一口径） */
  const pollFailed = ref(false);
  /** 读到过真值没有：与「这条主机确实没有序列数据」分开说（QR-W16） */
  const read = ref(false);

  /** 上限编辑态：表单初始值来自 metrics 的单机覆盖原值（null = 未自定义） */
  const memInput = ref<number | null>(null);
  const diskInput = ref<number | null>(null);
  const saving = ref(false);

  const { setPoll } = usePolls();
  const beginLoad = useSeq();

  /** 空序列有两种成因：没读到（接口挂了）vs 读到了但确实没有点（新主机还没攒出数据） */
  const emptyText = computed(() => {
    if (read.value) return "这个时间窗内没有序列数据，等一帧心跳再回来看";
    return "没读到（指标接口不可达）";
  });

  async function load(silent = false) {
    if (!pod.value) return;
    const isLatest = beginLoad();
    if (!silent) loading.value = true;
    try {
      const data = await getHostMetrics(pod.value, hours.value);
      if (!isLatest()) return;
      metrics.value = data;
      read.value = true;
      pollFailed.value = false;
      memInput.value = data.mem_limit;
      diskInput.value = data.disk_limit_raw;
    } catch (e: any) {
      if (!isLatest()) return;
      pollFailed.value = silent;
      if (!silent)
        message("加载趋势数据失败：" + errText(e), { type: "error" });
    } finally {
      if (isLatest()) loading.value = false;
    }
  }

  function restartTimer() {
    setPoll(POLL_KEY, () => load(true), 30 * 1000);
  }

  watch(hours, () => load());

  onMounted(async () => {
    await load();
    restartTimer();
  });

  /** 保存上限：本地先校验（1–100），通过后 PUT；null = 清除自定义。
   *  成功后整页重载一次——上限线与页头读数必须当场对上，不等 30s 轮询。 */
  async function saveLimits() {
    if (!pod.value) return;
    const errs = [limitError(memInput.value), limitError(diskInput.value)];
    if (errs.some(Boolean)) {
      message(errs.find(Boolean) as string, { type: "warning" });
      return;
    }
    saving.value = true;
    try {
      await setHostLimits(pod.value, {
        mem_limit: memInput.value,
        disk_limit: diskInput.value
      });
      message("告警上限已更新", { type: "success" });
      await load();
    } catch (e: any) {
      message("保存失败：" + errText(e), { type: "error" });
    } finally {
      saving.value = false;
    }
  }

  return {
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
    load,
    restartTimer
  };
}

/** 挂一个趋势图实例到容器上：ResizeObserver 自适应宽度（FE-15 的既定机制），
 *  卸载时 dispose。返回 setter 供模板 ref 使用。 */
export function useTrendChart(
  getOption: () => echarts.EChartsCoreOption | null
) {
  const chart = ref<EChartsType | null>(null);
  const el = ref<HTMLElement | null>(null);

  function render() {
    if (!el.value) return;
    if (!chart.value) chart.value = echarts.init(el.value);
    const opt = getOption();
    if (opt) chart.value.setOption(opt, true);
  }

  useResizeObserver(el, () => chart.value?.resize());

  onMounted(render);
  // 数据/上限变化后由调用方显式调 render()；这里只兜挂载与尺寸
  onBeforeUnmount(() => {
    chart.value?.dispose();
    chart.value = null;
  });

  return { chart, el, render };
}
