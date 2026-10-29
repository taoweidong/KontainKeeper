import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch
} from "vue";
import { useRouter } from "vue-router";
import type { PaginationProps } from "@pureadmin/table";
import { message } from "@/utils/message";
import {
  listHosts,
  type HostFilters,
  type HostSummary
} from "@/api/containers";
import { createCommand, listCollectItems } from "@/api/commands";
import { exportHosts } from "@/api/exporting";
import {
  ageText,
  downloadBlob,
  errText,
  fileStamp,
  mbText,
  nowSec,
  numText
} from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";

/**
 * 主机总览页（L1-8，P1-1/P1-2）：数据流与列定义。
 *
 * **数据流从 index.vue 原样搬入，语义一行没改**——筛选/排序下推服务端（QR-S36）、
 * 前端分页（QR-W7：summary 是标量行，瓶颈在节点数不在 JSON）、关键字 300ms 防抖、
 * 在途防重入（QR-W4）、静默轮询失败进页头读数（W5）、行级采集不污染批量勾选（FE-7）。
 *
 * 布局裁决（10-15 实测报告 §5，本页是 P1-1/P1-2 的主战场）：
 * - 根挂 `.kk-main`（列表页范式，与 `.kk-page` 互斥），`.kk-band` 仪表带与筛选条
 *   常驻视口顶部，不再随 500 行表格滚走（R4/R5）；
 * - 表格换 `pure-table[adaptive]` 视口测量，`.kk-fill-table` 的 flex 链在本页退役；
 * - `.kk-batch.kk-sticky-bar`（sticky 的 scrollport 是 el-card 且它自己不滚，
 *   10-15 §3.2 实验 2 证明从未吸住过）→ `.kk-select-banner` 选中横幅，
 *   常驻表格上方——勾选后不必滚到表底就能点批量动作（R6）；
 * - 每列必须有唯一非空 `label`：PureTableBar 按 label 做列显隐/固定/拖拽，
 *   `getKeyList` 静默丢弃缺 label 的列（勾选列与「最近心跳」列原来都没有）。
 */

/** host_type 是 Agent 自报的虚拟化形态；空值 = v3 老 Agent 没上报这一项 */
export const HOST_TYPE_LABEL: Record<string, string> = {
  metal: "物理机",
  vm: "虚拟机",
  container: "容器"
};

/** 离线要把服务端记的原因说出来：updating 是设计内的自更新窗口，不是故障，
 *  运维看到它不该去重启 Agent（B6 的语义此前在 UI 上完全丢失）。
 *  取值集合只有 online / updating / LWT 空，其余一律按「离线」呈现。 */
export const OFFLINE_REASON_LABEL: Record<string, string> = {
  updating: "更新中"
};

/** 计量条填充：钳到 0–100，坏值不画出越界的条。
 *  NaN/Infinity 必须显式挡掉（QR29-W5）：Math.min/max 会透传 NaN，
 *  界面会渲染出字面 NaN，插件写坏指标即可触发。 */
export function fillPct(v: number | null | undefined): number {
  if (v === null || v === undefined || !Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(100, Math.round(v)));
}

/** 心跳新鲜度三格，按绝对秒数判级（默认 60s 上报间隔下 ≈ 0.5 / 2 / 10 个周期）。
 *  三格是判级不是历史波形——列头图例写明阈值，避免误读成时间序列。 */
export function tickState(row: HostSummary): { n: number; cls: string } {
  if (!row.online) return { n: 0, cls: "" };
  const age = row.age_sec ?? 0;
  if (age <= 30) return { n: 3, cls: "" };
  if (age <= 120) return { n: 2, cls: "kk-ticks--mid" };
  if (age <= 600) return { n: 1, cls: "kk-ticks--low" };
  return { n: 0, cls: "" };
}

/** 在线/离线状态：空心点 = 离线（含自更新窗口的方点变体），实心 = 在线 */
export function stateClass(row: HostSummary): string {
  if (row.online) return "kk-state";
  return row.status_reason === "updating"
    ? "kk-state kk-state--updating"
    : "kk-state kk-state--idle";
}

export function stateText(row: HostSummary): string {
  if (row.online) return "在线";
  return OFFLINE_REASON_LABEL[row.status_reason || ""] || "离线";
}

export function useMonitor() {
  const router = useRouter();

  const loading = ref(false);
  const rows = ref<HostSummary[]>([]);
  /** 服务端按同一套条件算出的台数：仪表带读它（筛选生效时是筛后子集的诚实读数）。
   *  分页器的 total 不用它——本页是前端分页，total 跟清单长度走。 */
  const total = ref(0);
  const online = ref(0);
  const alerts = ref(0);
  const outdated = ref(0);
  const keyword = ref("");
  const onlyOnline = ref(false);
  const onlyAlert = ref(false);
  const hostType = ref("");
  const protoVer = ref("");
  /** 表头排序状态下推到服务端（`order` 空 = 用该列默认方向） */
  const sortKey = ref("last_seen");
  const sortDir = ref("");
  /** 最近一次加载成功的时间（秒级时间戳），0 = 尚未加载 */
  const lastLoadedAt = ref(0);
  /** 静默轮询失败：不刷 toast，改由表头的同步读数如实说明（W5） */
  const pollFailed = ref(false);
  /** 是否读到过至少一次真值：和「服务端报了 0 台」是两件事（QR-W16） */
  const read = computed(() => lastLoadedAt.value > 0);
  /** 轮询间隔（秒），0 = 停。总览是唯一常驻轮询的页面，10s 足够且不给服务端放大压力 */
  const interval = ref(10);

  const collectItems = ref<string[]>([]);
  const dialog = reactive({
    visible: false,
    items: [] as string[],
    submitting: false
  });
  const selection = ref<HostSummary[]>([]);
  /** 采集弹窗的目标主机快照：行级「采集」只下发这一台，不能反过来改写表格的
   *  批量勾选（旧写法 `selection.value = [row]` 只改了内存里的 ref，表头复选框
   *  与行勾选的视觉状态由 el-table 自己持有，于是界面显示 10 台、实际发 1 台）。 */
  const collectTargets = ref<HostSummary[]>([]);

  // 作用域版 setPoll：卸载时只清本页注册的 key（此前漏调 usePolls，切页后 host-monitor 轮询不会停）
  const { setPoll } = usePolls();
  /** 10s 轮询与「刷新」按钮是两个入口，在途防重入只护定时器那路：
   *  手动刷新撞上轮询时迟到的回包会把刚才的数据盖回去，还会提前熄灭 loading（QR-W4）。 */
  const beginLoad = useSeq();

  /** 与服务端同源的一份筛选条件：仪表带读数、分页与「导出清单」都读它（QR-S36）。
   *  布尔位显式转字符串——直接传 boolean 会造出 `?online=false` 这种「看着在筛、
   *  其实筛的是反的」的请求，服务端 `_tri()` 只认明确写法。
   *
   *  `docker_unhealthy` 有参数但页面不开放：内置 Agent 目前只声明 shell 能力，
   *  status 帧里没有 docker 摘要（见 QR-S38），勾上它永远得到空表——
   *  「查不出」被读成「没有问题」，比没有这个筛选项更糟。 */
  function queryParams(): HostFilters {
    return {
      keyword: keyword.value.trim(),
      host_type: hostType.value || "",
      proto_ver: protoVer.value || "",
      online: onlyOnline.value ? "true" : "",
      alert: onlyAlert.value ? "true" : "",
      sort: sortKey.value,
      order: sortDir.value
    };
  }

  /** 筛选生效时仪表带只统计筛后的子集，标签必须说实话：
   *  「8 台主机」被当成机队规模读，就会做出「机队空了」的下一步动作（QR-W16 同族）。
   *  用真值判断而不是 `!== ""`：el-select 清空时给的是 undefined，两种都算「没筛」。 */
  const isFiltered = computed(
    () =>
      keyword.value.trim() !== "" ||
      !!hostType.value ||
      !!protoVer.value ||
      onlyOnline.value ||
      onlyAlert.value
  );

  /**
   * 分页——单一状态源（Task 6 裁决，Task 8/9 沿用）。**前端**分页：500 台规模
   * （QR-W7）一次全量渲染会把 500 行 × 富单元格（仪表条 / 心跳刻度 / 链接）铺进
   * DOM，还要每 10s 随轮询整体重渲染——分页把渲染规模压到一页。summary 视图是
   * 标量行，500 台的 JSON 不是瓶颈，改后端分页只是换一种取法，代价换不到额外收益。
   * 跨页勾选由 row-key + reserve-selection 保住，「分几页勾完再批量下发」的老路径不断。
   */
  const pagination = reactive<PaginationProps>({
    total: 0,
    pageSize: 100,
    currentPage: 1,
    pageSizes: [100, 200, 500],
    background: true
  });

  const paged = computed(() => {
    const start = (pagination.currentPage - 1) * pagination.pageSize;
    return rows.value.slice(start, start + pagination.pageSize);
  });

  /** 换筛选条件回到第一页，否则会停在筛选后不存在的页上显示空表。
   *  条件即请求：勾选与下拉一改就查服务端（关键字另有防抖）。 */
  watch([onlyOnline, onlyAlert, hostType, protoVer], () => {
    pagination.currentPage = 1;
    load();
  });

  /** 关键字防抖：边打字边打服务端等于自己 DoS 自己，静默 300ms 后发一枪。
   *  这是**交互**计时器不是轮询，所以不走 usePolls；但卸载一定要清——
   *  否则切走页面还会补发一次请求。 */
  let kwTimer: ReturnType<typeof setTimeout> | null = null;
  watch(keyword, () => {
    pagination.currentPage = 1;
    if (kwTimer !== null) clearTimeout(kwTimer);
    kwTimer = setTimeout(() => {
      kwTimer = null;
      load(true);
    }, 300);
  });

  onBeforeUnmount(() => {
    if (kwTimer !== null) clearTimeout(kwTimer);
  });

  /** 轮询让行数变少时夹住当前页，避免出现一张空的尾页 */
  function clampPage() {
    const maxPage = Math.max(
      1,
      Math.ceil(rows.value.length / pagination.pageSize)
    );
    if (pagination.currentPage > maxPage) pagination.currentPage = maxPage;
  }

  /** 表头排序下推服务端；取消排序回到服务端默认（最近心跳降序）。
   *  可排序的列只有服务端 `_SORTS` 白名单里那五个，排不动的列不要挂 sortable。 */
  function onSortChange({
    prop,
    order
  }: {
    prop?: string;
    order?: string | null;
  }) {
    sortKey.value = prop || "last_seen";
    sortDir.value =
      order === "ascending" ? "asc" : order === "descending" ? "desc" : "";
    pagination.currentPage = 1;
    load();
  }

  function clearFilters() {
    keyword.value = "";
    hostType.value = "";
    protoVer.value = "";
    onlyOnline.value = false;
    onlyAlert.value = false;
  }

  /** 空表有两种成因，混为一谈会把人引向错误的下一步：筛选过严 vs 根本没主机上报 */
  const emptyText = computed(() => {
    if (rows.value.length) return "本页没有行，换一页看看";
    return isFiltered.value
      ? "没有符合当前筛选条件的主机，放宽条件试试"
      : "还没有主机上报，确认 Agent 与 Broker 已连通";
  });

  /** silent=true 供轮询复用：表格数据原位更新，不闪整页 loading，失败也不刷 toast */
  async function load(silent = false) {
    const isLatest = beginLoad();
    if (!silent) loading.value = true;
    try {
      const data = await listHosts("summary", queryParams());
      if (!isLatest()) return;
      rows.value = data.items;
      total.value = data.total;
      online.value = data.online;
      alerts.value = data.alerts;
      outdated.value = data.outdated;
      lastLoadedAt.value = nowSec();
      pollFailed.value = false;
      // 前端分页的 total 跟清单长度走（与筛选全集一致）；行数变少后当前页可能越界
      pagination.total = rows.value.length;
      clampPage();
    } catch (e: any) {
      if (!isLatest()) return;
      // 自动轮询失败只在表头的同步读数上如实说明：后端宕机时每 10s 弹一次
      // message 会把真正的错误淹成噪音（W5）。
      pollFailed.value = silent;
      if (!silent)
        message("加载主机列表失败：" + errText(e), { type: "error" });
    } finally {
      if (isLatest()) loading.value = false;
    }
  }

  function restartTimer() {
    // 0 = 不自动刷新；setPoll 对同名 key 是覆盖，不会叠加定时器
    setPoll("host-monitor", () => load(true), interval.value * 1000);
  }

  watch(interval, restartTimer);

  function onSelectionChange(val: HostSummary[]) {
    selection.value = val;
  }

  const exporting = ref(false);

  /** 导出主机清单（资产盘点场景）：条件与页面同源（QR-S36）——页面上筛完 2 台、
   *  导出 50 台的报表没有意义，还会让人以为筛选只影响显示。 */
  async function onExport() {
    exporting.value = true;
    try {
      downloadBlob(
        await exportHosts(queryParams()),
        `主机清单${isFiltered.value ? "_筛选后" : ""}_${fileStamp()}.csv`
      );
    } catch (e: any) {
      message("导出失败：" + errText(e), { type: "error" });
    } finally {
      exporting.value = false;
    }
  }

  /** 采集项懒加载：连点两次「批量采集」会并发两路，迟到那路的结果与错误都不该再动界面（QR-W4）。 */
  const beginCollectItems = useSeq();

  async function ensureCollectItems(): Promise<boolean> {
    if (collectItems.value.length) return true;
    const isLatest = beginCollectItems();
    try {
      const items = (await listCollectItems()).items;
      if (!isLatest()) return false;
      collectItems.value = items;
      return true;
    } catch (e: any) {
      if (!isLatest()) return false;
      message("加载采集项失败：" + errText(e), { type: "error" });
      // 采集项拿不到就不开弹窗，避免勾选区空白
      return false;
    }
  }

  /** 默认预勾的三项：只在后端确实支持时才预勾，不硬塞采集项清单 */
  const DEFAULT_ITEMS = ["cpu", "mem", "disk"];

  /** 打开采集弹窗。目标由调用方给定：批量=当前勾选，行级=这一台。 */
  async function openCollectDialog(targets: HostSummary[]) {
    if (!(await ensureCollectItems())) return;
    collectTargets.value = targets;
    dialog.items = DEFAULT_ITEMS.filter(i => collectItems.value.includes(i));
    dialog.visible = true;
  }

  async function openCollect() {
    if (!selection.value.length) {
      message("请先在表格里勾选主机", { type: "warning" });
      return;
    }
    await openCollectDialog(selection.value);
  }

  /** 行级「采集」：只针对这一台，不改写用户在表格里已有的批量勾选 */
  function collectOne(row: HostSummary) {
    return openCollectDialog([row]);
  }

  async function submitCollect() {
    if (!dialog.items.length) {
      message("至少勾选一个采集项", { type: "warning" });
      return;
    }
    if (!collectTargets.value.length) {
      message("没有目标主机", { type: "warning" });
      return;
    }
    dialog.submitting = true;
    const pods = collectTargets.value.map(r => r.pod);
    try {
      const res = await createCommand({
        pods,
        kind: "collect",
        items: dialog.items
      });
      message(`已下发 ${res.items.length} 条采集命令`, { type: "success" });
      dialog.visible = false;
      // 与「批量命令 / 批量升级」同一套 ?pods= 契约：落到采集页要带回刚下发的
      // 主机，否则运维得在采集页把同一批机器重新勾一遍（FE-3）。
      router.push({ name: "CommandCollect", query: { pods: pods.join(",") } });
    } catch (e: any) {
      message("下发失败：" + errText(e), { type: "error" });
    } finally {
      dialog.submitting = false;
    }
  }

  function openCommandCenter() {
    if (!selection.value.length) {
      message("请先在表格里勾选主机", { type: "warning" });
      return;
    }
    router.push({
      name: "CommandShell",
      query: { pods: selection.value.map(r => r.pod).join(",") }
    });
  }

  function gotoDetail(pod: string) {
    router.push({ name: "HostDetail", params: { pod } });
  }

  /** 跳到「版本与更新」页并把已选主机带过去（D2.4：批量升级是跨页动作）。 */
  function gotoUpgrade() {
    if (!selection.value.length) return;
    router.push({
      name: "HostUpdate",
      query: { pods: selection.value.map(r => r.pod).join(",") }
    });
  }

  /** 整行可点进详情（原只能点主机名链接，命中区域太小）。
   *  必须跳过 selection 列，否则勾选会被误判为进详情。 */
  function onRowClick(row: HostSummary, column: any) {
    if (column?.type === "selection") return;
    gotoDetail(row.pod);
  }

  /** 告警行整行浅红底：扫描时不必逐格看磁盘列 */
  function rowClass({ row }: { row: HostSummary }): string {
    return row.disk_alert ? "kk-row-alert" : "";
  }

  /**
   * 列定义。每列都有唯一且非空的 `label`：`PureTableBar` 按 `label` 匹配列做
   * 显隐 / 固定 / 拖拽，`getKeyList` **静默丢弃缺 `label` 的列**——迁移前的
   * 勾选列与「最近心跳」列都没有 label，列设置浮层与列下标是错位的。
   * 对齐规则（Task 7 评审口径）：主机名 / 系统 / Agent 版本这类长度不可预测的
   * 文本列左对齐，居中只留给状态点、数字与操作。
   */
  const columns: TableColumnList = [
    {
      label: "勾选列",
      type: "selection",
      width: 46,
      align: "center",
      /** row-key=pod 下的跨页勾选保留：分几页勾完再批量下发的老路径不断 */
      reserveSelection: true
    },
    // 主机名是扫读基准线，左对齐（居中会让每行起点漂移）
    {
      label: "主机",
      prop: "pod",
      sortable: "custom",
      minWidth: 180,
      align: "left",
      cellRenderer: ({ row }) => (
        <>
          <el-link
            type="primary"
            class="kk-num"
            onClick={(e: MouseEvent) => {
              e.stopPropagation();
              gotoDetail(row.pod);
            }}
          >
            {row.pod}
          </el-link>
          <div class="kk-sub">{row.image || "-"}</div>
        </>
      )
    },
    {
      label: "状态",
      width: 92,
      cellRenderer: ({ row }) => (
        <span class={stateClass(row)}>{stateText(row)}</span>
      )
    },
    {
      // v4 元信息：类型带分组。容器列没上——内置 Agent 不发 docker 摘要，
      // 恒为 0 的一列只会教运维「这一列没数据就是没问题」（QR-S38）。
      label: "类型",
      width: 108,
      cellRenderer: ({ row }) => (
        <>
          <span>{HOST_TYPE_LABEL[row.host_type] || "-"}</span>
          {row.group_name ? <div class="kk-sub">{row.group_name}</div> : null}
        </>
      )
    },
    {
      label: "系统 / 地址",
      minWidth: 132,
      align: "left",
      cellRenderer: ({ row }) => (
        <>
          <div>{row.os_name || "-"}</div>
          <div class="kk-sub kk-num">{row.ip || "-"}</div>
        </>
      )
    },
    {
      label: "CPU",
      prop: "cpu",
      sortable: "custom",
      width: 132,
      cellRenderer: ({ row }) => (
        <span class="kk-meter">
          <span class="kk-meter__bar">
            <i style={{ width: fillPct(row.cpu) + "%" }} />
          </span>
          <span class="kk-meter__num">{numText(row.cpu)}</span>
        </span>
      )
    },
    {
      label: "内存",
      prop: "mem_mb",
      sortable: "custom",
      width: 110,
      align: "right",
      cellRenderer: ({ row }) => (
        <span class="kk-num">{mbText(row.mem_mb)}</span>
      )
    },
    {
      label: "磁盘",
      prop: "disk_pct",
      sortable: "custom",
      width: 132,
      cellRenderer: ({ row }) => (
        // 告警态由条本身变红承担，不再套一层 tag：一列只有一种强调方式
        <span class={["kk-meter", { "kk-meter--alert": row.disk_alert }]}>
          <span class="kk-meter__bar">
            <i style={{ width: fillPct(row.disk_pct) + "%" }} />
          </span>
          <span class="kk-meter__num">{numText(row.disk_pct, 0)}</span>
        </span>
      )
    },
    {
      label: "Agent",
      width: 150,
      align: "left",
      cellRenderer: ({ row }) => (
        <>
          {/* 落后于待分发版本时用「待升级」同色——一眼看出哪些要升（D2.4） */}
          <span class={["kk-num", { "kk-warn": row.agent_outdated }]}>
            {row.agent_ver || "-"}
          </span>
          {/* 只标 v3：兼容窗口是要关掉的，剩下的旧协议主机就是拦路的那几台 */}
          {row.proto_ver === 3 ? (
            <el-tooltip
              content="这台仍以 v3 上报：设 KK_DROP_PROTO_V3 关掉兼容窗口前，先把它升上来"
              placement="bottom"
            >
              <el-tag class="kk-ml" size="small" type="warning">
                v3
              </el-tag>
            </el-tooltip>
          ) : null}
        </>
      )
    },
    {
      label: "最近心跳",
      prop: "last_seen",
      sortable: "custom",
      width: 158,
      headerRenderer: () => (
        <el-tooltip
          content="心跳新鲜度分级：满格 ≤30s，两格 ≤2 分钟，一格 ≤10 分钟，空格更久"
          placement="bottom"
        >
          <span>最近心跳</span>
        </el-tooltip>
      ),
      cellRenderer: ({ row }) => {
        // 一次判级两处用：此前模板每行调 tickState 两次、每次新建对象（FE-10 尾巴）
        const tick = tickState(row);
        return (
          <>
            <span class={["kk-ticks", tick.cls]} aria-hidden="true">
              {[1, 2, 3].map(n => (
                <i key={n} class={n <= tick.n ? "on" : ""} />
              ))}
            </span>
            <span class="kk-num kk-ml">{ageText(row.age_sec)}</span>
          </>
        );
      }
    },
    {
      label: "操作",
      fixed: "right",
      width: 150,
      cellRenderer: ({ row }) => (
        <>
          <el-button
            link
            type="primary"
            class="reset-margin"
            onClick={(e: MouseEvent) => {
              e.stopPropagation();
              gotoDetail(row.pod);
            }}
          >
            详情
          </el-button>
          <el-button
            link
            type="primary"
            onClick={(e: MouseEvent) => {
              e.stopPropagation();
              collectOne(row);
            }}
          >
            采集
          </el-button>
        </>
      )
    }
  ];

  onMounted(async () => {
    await load();
    restartTimer();
  });

  return {
    loading,
    total,
    online,
    alerts,
    outdated,
    read,
    lastLoadedAt,
    pollFailed,
    interval,
    restartTimer,
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
  };
}
