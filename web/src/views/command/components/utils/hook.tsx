import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch
} from "vue";
import type { PaginationProps } from "@pureadmin/table";
import { message } from "@/utils/message";
import {
  getCommandOut,
  listBatches,
  listCommands,
  type BatchSummary,
  type CommandRow
} from "@/api/commands";
import { exportCommands } from "@/api/exporting";
import {
  downloadBlob,
  elapsedText,
  errText,
  fileStamp,
  statusLabel,
  statusType,
  tsText
} from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";

/**
 * 执行历史（命令面板 / 采集面板共用）的数据流与列定义。
 *
 * 改造要点（A5.6 / A3 前端，语义一条没改）：
 * - 筛选全部下推后端（分页 / 状态 / 关键字 / 批次），否则导出与所见不一致
 * - 轮询自适应：有未终态命令时 3s，否则 10s；不再无条件 5s 空转
 * - 输出改右侧抽屉：居中弹窗会完全遮挡列表，无法连续对比多条
 * - 本次下发的行加左侧色条：500 台里一眼找到刚才发的那批
 *
 * 分页只有一个状态源（Task 6 的裁决，本页沿用）：`pagination` 的 `pageSize` /
 * `currentPage`，`offset` 由 `(currentPage - 1) * pageSize` 推导，不再单独持有
 * 原页面的 `pageSize`/`offset` 两个 ref——两处都能改必然漂移。
 */

/** 与后端 `status` 字段同名的状态词汇表，只用于「未终态」判定 */
const ACTIVE_STATUS = ["pending", "sent", "running"];

/**
 * 状态列的严重度判级：**逐档照搬 `@/utils/kk` 的 `statusType` 返回值**（五档 el-tag
 * type 映射到本仓四档圆点），语义一条不许丢——这是 L1-5 评审揪出的那类回归：
 * `.kk-state::before` 默认圆点是 `var(--kk-live)`（绿 = 在线/健康），只改文字色的
 * `.kk-bad` 不动圆点，于是 `failed`/`timeout`/`lost` 会顶着绿色健康点说「一切正常」。
 *
 * | statusType | 状态值 | 圆点 |
 * |---|---|---|
 * | `danger` | failed / timeout / lost | 红点 + 红字（`--bad` 上色圆点，`kk-bad` 上色文字） |
 * | `warning` | sent | 琥珀点（已下发等结果） |
 * | `primary` | running | 琥珀点（与 sent 同档，见下） |
 * | `success` | done | 默认实心绿点（返回基类，不加变体） |
 * | `info` | pending / queued / 未知 | 空心点（未开始，不是错误也不是健康） |
 *
 * `primary`（running）与 `warning`（sent）并进同一档琥珀点，是本仓状态语言的取舍：
 * `kk.scss` 的四档词汇（绿/红/琥珀/空心）里没有蓝色圆点，而 `running` 与 `sent` 的
 * 严重度本来就相同——都是「未终态、等结果」，也正是 `hasActive` 与 3s 快轮询要一起
 * 捞的那一档；两者的**文字**仍由 `statusLabel` 分别给出「执行中 / 已下发」， finer 区分
 * 没有丢。另两条禁区：`kk-warn` 在本仓专指「Agent 版本落后于待分发版本」，`kk-state--updating`
 * 是方点、承载 B6「更新中」语义——都不许挪用去表达命令严重度。
 */
const statusStateClass = (s: string): string => {
  switch (statusType(s)) {
    case "danger":
      return "kk-state kk-state--bad kk-bad";
    case "warning":
    case "primary":
      return "kk-state kk-state--warn";
    case "success":
      return "kk-state";
    default:
      return "kk-state kk-state--idle";
  }
};

export function useCommandHistory() {
  const POLL_KEY = "command-history";

  const rows = ref<CommandRow[]>([]);
  /** 读到过真值没有：「还没有命令记录」在接口挂掉时是一句假话（QR-W16）。
   *  total 是**筛选后**的总数，所以空表还要再分「筛选无匹配」与「真的没有记录」。 */
  const read = ref(false);
  const loading = ref(false);
  const statusFilter = ref("");
  const keyword = ref("");
  const batchFilter = ref("");
  const autoRefresh = ref(true);
  /** 本次（或最近一次）下发的命令 id：给这些行加左侧色条 */
  const fresh = ref<Set<string>>(new Set());
  const batches = ref<BatchSummary[]>([]);
  const exporting = ref(false);
  /** 静默轮询失败：状态进表头读数，不再每 3~10s 弹一次 toast（W5） */
  const pollFailed = ref(false);

  const pagination = reactive<PaginationProps>({
    total: 0,
    pageSize: 50,
    currentPage: 1,
    pageSizes: [50, 100, 200],
    background: true
  });

  const out = reactive({
    visible: false,
    title: "",
    text: "",
    loading: false,
    id: ""
  });

  // 作用域版 setPoll/clearPoll：卸载只清本组件注册的 key，不再误清其他页面的轮询
  const { setPoll, clearPoll } = usePolls();

  // 手动触发的请求不走轮询那条在途防抖：列表与抽屉输出各一路「最新一次」票据（QR-W4）。
  // 没有它，连点两行时先点的慢响应会后到，把标题为 B 的抽屉填上 A 的输出。
  const beginLoad = useSeq();
  const beginOut = useSeq();

  async function loadCommands(silent = false) {
    const isLatest = beginLoad();
    if (!silent) loading.value = true;
    try {
      const data = await listCommands({
        status: statusFilter.value || undefined,
        keyword: keyword.value.trim() || undefined,
        batch: batchFilter.value || undefined,
        limit: pagination.pageSize,
        offset: (pagination.currentPage - 1) * pagination.pageSize
      });
      if (!isLatest()) return;
      rows.value = data.items;
      pagination.total = data.total;
      read.value = true;
      pollFailed.value = false;
    } catch (e: any) {
      if (!isLatest()) return;
      // 自适应轮询最密到 3s 一轮：失败刷 toast 会把噪音做成刷屏，状态改由表头读数承担（W5）
      pollFailed.value = silent;
      if (!silent)
        message("加载命令历史失败：" + errText(e), { type: "error" });
    } finally {
      // 只有最新一路负责收 spinner：迟到包不该把仍在途的请求的加载态关掉
      if (isLatest()) loading.value = false;
    }
  }

  async function loadBatches() {
    try {
      batches.value = (await listBatches(20)).items;
    } catch {
      batches.value = [];
    }
  }

  /** 是否有未终态命令：决定 3s / 10s 两档节奏 */
  const hasActive = computed(() =>
    rows.value.some(r => ACTIVE_STATUS.includes(r.status))
  );

  /** 自适应轮询：有未终态命令时 3s，否则 10s */
  function restartTimer() {
    if (!autoRefresh.value) return clearPoll(POLL_KEY);
    setPoll(POLL_KEY, () => loadCommands(true), hasActive.value ? 3000 : 10000);
  }

  // 只按「未终态 ↔ 全终态」的翻转重建定时器。挂到 rows 上等于每次回包都
  // clearPoll + 新建 setInterval：间隔变成「设定值 + 请求耗时」越跑越慢，
  // 而且是在自己的回调里拆掉自己（FE-11）。
  watch(hasActive, restartTimer);

  /**
   * 回到第 1 页并重载一次。`el-pagination` 只在**用户点击**时 emit `current-change`，
   * 程序改 `pagination.currentPage` 不触发 emit，所以筛选/批次/条数这几条路径必须自己
   * 发起重载；反过来用户点页码时 pure-table 已把 `currentPage` 改进这个 reactive，
   * 模板的 `@page-current-change` 只调 `loadCommands()`、不再赋值（FE-8 同族）。
   */
  function reloadFirstPage() {
    pagination.currentPage = 1;
    loadCommands();
  }

  /** 筛选（状态 / 批次）与每页条数变了都归位重载，并让节奏立刻跟上（原页行为保持） */
  watch([statusFilter, batchFilter], () => {
    reloadFirstPage();
    restartTimer();
  });
  watch(
    () => pagination.pageSize,
    () => {
      reloadFirstPage();
      restartTimer();
    }
  );

  /** 关键字输入防抖：避免每敲一个字就打一次后端 */
  let kwTimer: ReturnType<typeof setTimeout> | null = null;
  watch(keyword, () => {
    if (kwTimer) clearTimeout(kwTimer);
    kwTimer = setTimeout(() => {
      kwTimer = null;
      reloadFirstPage();
    }, 300);
  });
  // 卸载时清掉待触发的防抖：否则切页后 300ms 内这一个 timer 仍会打一次后端
  // （usePolls 只登记 setInterval，setTimeout 不在它的管辖里）
  onBeforeUnmount(() => {
    if (kwTimer !== null) clearTimeout(kwTimer);
    kwTimer = null;
  });

  /** 当前批次的状态分布：选中批次时表头直接给出「N 台：done X / failed Y」 */
  const batchStat = computed(() => {
    if (!batchFilter.value) return null;
    const b = batches.value.find(x => x.batch_id === batchFilter.value);
    return b || null;
  });

  /** 批次分布值是 string|number 联合类型，这里统一成数字再累加 */
  function statNum(v: string | number | undefined): number {
    return Number(v || 0);
  }

  /** 三个筛选任一处生效：空表要说的是「这个条件下没匹配」，不是「从来没下过命令」 */
  const hasFilter = computed(
    () => !!(statusFilter.value || keyword.value.trim() || batchFilter.value)
  );

  function shortBatch(id?: string): string {
    return id ? id.slice(0, 8) : "-";
  }

  /** 批次筛选链接：点批次号把列表收窄到这一批。
   *  必须 stopPropagation——否则同一次点击会冒到整行 row-click，把输出抽屉也弹开。 */
  function focusBatchFilter(batchId?: string) {
    if (!batchId) return;
    batchFilter.value = batchId;
  }

  /** 父页下发成功后调用：定位到该批次并高亮新命令（对外契约，shell/collect 经 ref 调用） */
  function focusBatch(batchId: string, ids: string[] = []) {
    fresh.value = new Set(ids);
    batchFilter.value = batchId;
    pagination.currentPage = 1;
    loadCommands();
    loadBatches();
    restartTimer();
  }

  async function showOut(row: CommandRow) {
    const isLatest = beginOut();
    out.title = `${row.id} · ${row.pod}`;
    out.id = row.id;
    out.text = "";
    out.visible = true;
    if (row.out_purged) {
      out.text = "（输出已按保留策略清理，仅保留状态行）";
      return;
    }
    out.loading = true;
    try {
      const text = await getCommandOut(row.id);
      if (!isLatest()) return;
      out.text = text || "（无输出）";
    } catch (e: any) {
      if (!isLatest()) return;
      out.text = "读取输出失败：" + errText(e);
    } finally {
      if (isLatest()) out.loading = false;
    }
  }

  /** 下载单条完整输出：复用导出工具，不新增依赖 */
  async function downloadOut() {
    try {
      const text = await getCommandOut(out.id);
      downloadBlob(
        new Blob([text], { type: "text/plain;charset=utf-8" }),
        `${out.id}.txt`
      );
    } catch (e: any) {
      message("下载输出失败：" + errText(e), { type: "error" });
    }
  }

  async function onExport() {
    exporting.value = true;
    try {
      // 导出带上当前关键字 + 状态 + 批次：所见即所得，与列表同一条筛选语义
      const blob = await exportCommands({
        status: statusFilter.value || undefined,
        batch: batchFilter.value || undefined,
        keyword: keyword.value.trim() || undefined,
        include_tail: 1
      });
      downloadBlob(blob, `命令历史_${fileStamp()}.csv`);
    } catch (e: any) {
      message("导出失败：" + errText(e), { type: "error" });
    } finally {
      exporting.value = false;
    }
  }

  function argvPreview(argv: CommandRow["argv"]): string {
    if (!argv) return "-";
    // 列表接口不解析 argv：字符串（命令原文或 JSON 文本）直接展示，不再 JSON 化
    if (typeof argv === "string") return argv;
    if (Array.isArray(argv)) return argv.join(" ");
    if (typeof argv === "object") {
      const o = argv as Record<string, any>;
      if (o.items) return "items: " + (o.items as string[]).join(", ");
      return JSON.stringify(argv);
    }
    return String(argv);
  }

  /** 新下发的行加左侧色条：一眼找到刚才发的那批 */
  function rowClass({ row }: { row: CommandRow }): string {
    return fresh.value.has(row.id) ? "kk-row-fresh" : "";
  }

  /**
   * 每列都有唯一且非空的 `label`：`PureTableBar` 按 `label` 匹配列做显隐 / 固定 /
   * 拖拽，`getKeyList` 不去重也会静默丢弃缺 `label` 的列——重名会让第二列无法隐藏，
   * 缺 label 会让浮层勾选与 `dynamicColumns` 下标错位（typecheck / eslint 抓不到）。
   * 原 el-table 的 11 条列逐条照搬（prop / label / width 一字易），渲染器换成 cellRenderer。
   */
  const columns: TableColumnList = [
    {
      label: "ID",
      prop: "id",
      width: 120,
      cellRenderer: ({ row }) => <span class="kk-num">{row.id}</span>
    },
    // 主机名长且是扫读基准线，必须左对齐（居中会让每行的起点漂移）
    { label: "主机", prop: "pod", minWidth: 150, align: "left" },
    {
      label: "批次",
      width: 100,
      cellRenderer: ({ row }) =>
        row.batch_id ? (
          <el-tooltip content={row.batch_id} placement="top">
            <el-link
              type="primary"
              onClick={(e: MouseEvent) => {
                e.stopPropagation();
                focusBatchFilter(row.batch_id);
              }}
            >
              {shortBatch(row.batch_id)}
            </el-link>
          </el-tooltip>
        ) : (
          <span class="kk-sub">-</span>
        )
    },
    { label: "类型", prop: "kind", width: 90 },
    {
      label: "命令 / 采集项",
      minWidth: 200,
      showOverflowTooltip: true,
      cellRenderer: ({ row }) => argvPreview(row.argv)
    },
    {
      label: "状态",
      width: 100,
      cellRenderer: ({ row }) => (
        <span
          class={statusStateClass(row.status)}
          onClick={(e: MouseEvent) => e.stopPropagation()}
        >
          {statusLabel(row.status)}
        </span>
      )
    },
    {
      label: "rc",
      width: 70,
      cellRenderer: ({ row }) => (
        <span class="kk-num">{row.rc === null ? "-" : row.rc}</span>
      )
    },
    {
      label: "耗时",
      width: 90,
      cellRenderer: ({ row }) => (
        <span class="kk-num">{elapsedText(row.elapsed_ms)}</span>
      )
    },
    {
      label: "输出",
      minWidth: 180,
      showOverflowTooltip: true,
      cellRenderer: ({ row }) =>
        row.out_purged ? "（已清理）" : row.out_tail || "-"
    },
    {
      label: "创建",
      width: 160,
      cellRenderer: ({ row }) => (
        <span class="kk-num">{tsText(row.created_at)}</span>
      )
    },
    { label: "操作", width: 90, fixed: "right", slot: "operation" }
  ];

  onMounted(async () => {
    await Promise.all([loadCommands(), loadBatches()]);
    restartTimer();
  });

  return {
    /** 下面三个是「自动刷新」复选框那条 `@change` 要用的句柄，模板里逐字保留原写法 */
    POLL_KEY,
    clearPoll,
    restartTimer,
    rows,
    columns,
    read,
    loading,
    keyword,
    statusFilter,
    batchFilter,
    autoRefresh,
    batches,
    exporting,
    pollFailed,
    pagination,
    out,
    batchStat,
    hasFilter,
    statNum,
    shortBatch,
    rowClass,
    loadCommands,
    focusBatch,
    showOut,
    downloadOut,
    onExport
  };
}
