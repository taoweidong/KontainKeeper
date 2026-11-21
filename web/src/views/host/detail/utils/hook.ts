import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useWindowSize } from "@vueuse/core";
import { ElMessage } from "element-plus";

import {
  getHost,
  getHostMetrics,
  type HostDetail,
  type MetricPoint
} from "@/api/containers";
import { upgradeHosts } from "@/api/agent";
import { exportMetrics } from "@/api/exporting";
import {
  downloadBlob,
  errText,
  fileStamp,
  nowSec,
  numText,
  upgradeSkipText
} from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";
import { DEFAULT_CHART_COLORS, type ChartColors } from "@/utils/kkChart";

/** 主机详情页的数据流（QR1121 从 index.vue 沉底，语义保持）。
 *
 *  沉底动机：① 全失败态的 404/不可达分流是语义修复（QR1121-W9），需要
 *  可单测的落点；② buildDetailOption 抽成纯函数后与 trend 的 buildTrendOption
 *  同族，双轴单位/跨天刻度/colors 注入都能直接驱动断言。
 *  图表实例生命周期（init/resize/dispose）仍留在 index.vue——它依赖模板 ref。
 */

/** 子表最大高度收口（QR1121）：原 220/260/240 三值并存，统一 260。
 *  磁盘/网卡表略增高，与进程/用户表同档，窄屏折叠后节奏一致。 */
export const DETAIL_TABLE_MAX_H = 260;

/** 子表空态文案（QR1121-W10）：指标类子表的数据来自「最近一帧」，
 *  空 ≠ 接口失败——详情读到了才有这些表，空就是该帧没带这项采集。 */
export function detailEmptyText(label: string): string {
  return `该帧没有${label}数据，等一帧心跳再回来看`;
}

export type DetailSeriesPoint = Pick<MetricPoint, "ts" | "cpu" | "mem_mb">;

/** 双轴折线 option 组装（QR1121-W11，纯函数可单测）：
 *  原 index.vue renderChart 的 option 整体搬入，语义逐字保持——
 *  双轴单位不同（CPU % / 内存 MB），tooltip 按 seriesName 配单位（QR1120-W16），
 *  null 点显 - 不画 0；跨天窗口（>24h）刻度带「月-日」。
 *  colors 可选注入（默认亮色字面量）：暗色/主题切换由调用方重读重绘。 */
export function buildDetailOption(
  series: DetailSeriesPoint[],
  hours: number,
  colors: ChartColors = DEFAULT_CHART_COLORS
) {
  // x 轴刻度随窗口变化：跨天窗口（近 7 天）必须带「月-日」，否则一屏全是重复的钟点
  const fmt: Intl.DateTimeFormatOptions =
    hours > 24
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
  return {
    tooltip: {
      trigger: "axis",
      backgroundColor: colors.tooltipBg,
      borderColor: colors.tooltipBorder,
      textStyle: { color: colors.text },
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
    legend: {
      data: ["CPU %", "内存 MB"],
      right: 0,
      textStyle: { color: colors.text }
    },
    grid: { left: 56, right: 60, top: 36, bottom: 36 },
    xAxis: {
      type: "category",
      data: times,
      boundaryGap: false,
      axisLabel: { color: colors.text },
      axisLine: { lineStyle: { color: colors.axisLine } }
    },
    yAxis: [
      {
        type: "value",
        name: "CPU %",
        max: 100,
        min: 0,
        nameTextStyle: { color: colors.text },
        axisLabel: { color: colors.text },
        splitLine: { lineStyle: { color: colors.splitLine } }
      },
      {
        type: "value",
        name: "MB",
        nameTextStyle: { color: colors.text },
        axisLabel: { color: colors.text },
        splitLine: { lineStyle: { color: colors.splitLine } }
      }
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
  };
}

export function useHostDetail() {
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
  /** 最新一批曲线点：index.vue watch 它驱动 renderChart（实例生命周期在模板侧） */
  const series = ref<DetailSeriesPoint[]>([]);

  /** 静默轮询失败：状态进页头读数，不再每 30s 刷一次 toast（W5）。
   *  详情页即使离线主机也常开（挂着看曲线），所以这条读数必须说清「图上是旧数据」。 */
  const pollFailed = ref(false);

  /** 主机详情读记账（QR1121-W9，修 QR-W16 红线违反）：
   *  原实现 getHost 失败时 detail 保持 null，页面渲染「主机不存在或尚未上报」——
   *  把接口不可达谎报成不存在。现在按 HTTP 状态分流：**只有 404 才许说不存在**，
   *  其余错误说「没读到（主机接口不可达）」并给重试。 */
  const hostRead = ref(false);
  const hostError = ref<"" | "notfound" | "unreachable">("");
  /** 最近一次 getHost 成功时刻：页头新鲜度读数走 syncReadout 统一口径（QR1121-W8） */
  const lastLoadedAt = ref(0);

  const hostEmptyText = computed(() => {
    if (hostError.value === "notfound") return "主机不存在或尚未上报";
    if (hostError.value === "unreachable") return "没读到（主机接口不可达）";
    return "加载中…";
  });

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

  const disks = computed(() => {
    const d = (detail.value?.metrics?.disks || {}) as Record<string, any>;
    return Object.entries(d).map(([mount, v]) => ({ mount, ...(v as object) }));
  });
  const procs = computed(
    () => (detail.value?.metrics?.procs_top || []) as any[]
  );
  const users = computed(() => (detail.value?.metrics?.users || []) as any[]);
  const net = computed(() => {
    const n = (detail.value?.metrics?.net || {}) as Record<string, any>;
    return Object.entries(n).map(([nic, v]) => ({ nic, ...(v as object) }));
  });

  /** v4 标签展示（QR1121）：_truncated 是服务端「超限整对丢弃」的标记键，
   *  不是用户标签，滤掉后单独尾注说明。 */
  const labelsList = computed(() =>
    Object.entries(detail.value?.labels || {}).filter(
      ([k]) => k !== "_truncated"
    )
  );
  const labelsTruncated = computed(
    () => detail.value?.labels?._truncated === true
  );
  /** 自定义采集（QR1121）：有键才渲染小节，空对象不占版面 */
  const customEntries = computed(() =>
    Object.entries(detail.value?.custom || {})
  );

  /** 描述列表列数随窗口宽度收敛（QR1121 增 1 列档）：
   *  ≥1200px 四列、≥768px 两列、更窄一列（el-descriptions 不支持断点） */
  const { width: winWidth } = useWindowSize();
  const descCols = computed(() =>
    winWidth.value >= 1200 ? 4 : winWidth.value >= 768 ? 2 : 1
  );

  /** silent=true 供轮询复用：曲线失败不刷 toast，交页头读数 */
  async function loadMetrics(silent = false) {
    const isLatest = beginMetrics();
    try {
      const data = await getHostMetrics(pod.value, hours.value);
      if (!isLatest()) return;
      series.value = data.series;
      chartEmpty.value = data.series.length === 0;
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
      hostRead.value = true;
      hostError.value = "";
      lastLoadedAt.value = nowSec();
    } catch (e: any) {
      if (!isLatest()) return;
      hostFailed = true;
      // QR1121-W9：404 与不可达分流，不许把网络错误谎报成「主机不存在」
      hostError.value =
        e?.response?.status === 404 ? "notfound" : "unreachable";
      if (!silent) ElMessage.error("加载主机详情失败：" + errText(e));
    } finally {
      if (isLatest()) loading.value = false;
    }
    await loadMetrics(silent);
    // getHost 失败而曲线成功时页头也要变冷（QR1120-W11，welcome 的 OR 口径）：
    // 读数说的是整页新鲜度，详情卡是旧数据就不能继续显绿色「自动刷新」。
    if (hostFailed) pollFailed.value = true;
  }

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

  /** 回总览锚点（QR1121）：与 trend 回详情的锚点对称，下钻链路可逆 */
  function gotoMonitor() {
    router.push({ name: "HostMonitor" });
  }

  onMounted(async () => {
    await load();
    // 详情与图表 30s：曲线不需要秒级新鲜度
    setPoll("host-detail", () => load(true), 30000);
  });

  return {
    pod,
    loading,
    detail,
    hours,
    series,
    pollFailed,
    chartEmpty,
    chartEmptyText,
    hostRead,
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
    loadMetrics,
    onExportMetrics,
    onUpgradeOne,
    gotoCommand,
    gotoTrend,
    gotoMonitor
  };
}
