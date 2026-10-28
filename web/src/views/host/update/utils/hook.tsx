import { computed, h, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { ElMessageBox } from "element-plus";
import type { PaginationProps } from "@pureadmin/table";
import { message } from "@/utils/message";
import { listHosts, type HostSummary } from "@/api/containers";
import {
  getAgentCurrent,
  listUpdates,
  upgradeHosts,
  type AgentCurrent,
  type UpdateRow
} from "@/api/agent";
import {
  ageText,
  errText,
  statusLabel,
  statusType,
  tsText,
  upgradeSkipText
} from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";

/**
 * 版本与更新页（D2.4）的数据流与两张表的列定义（Task 8，P2-1 / L1-7）。
 *
 * 结构裁决（写进提交信息，勿当遗留硬编码删）：本页**两张表同屏**，台账表在上、
 * 主机版本主表在下。两张都开 `adaptive` 会互相抢高度——它按
 * `innerHeight - wrapper.getBoundingClientRect().top - offsetBottom` 定高，
 * 前一张撑开后一张就溢出（R7 页 scrolls）。所以**台账表（行数少）不套 bar、
 * 不开 adaptive**，保留自然高度并 `max-height: 320` 封顶；主表套 PureTableBar
 * 走 adaptive，offsetBottom 是本页实测值（见 index.vue 模板注释）。
 *
 * 改造要点（范式逐字对齐 audit / command 两页，语义一条没改）：
 * - 主表改 `pure-table[adaptive]` + 列定义进 `columns`；勾选列补 `label: "勾选列"`
 *   （getKeyList 会静默丢弃无 label 的列，列设置浮层与 dynamicColumns 错位）
 * - 500 台落后主机不再一次铺进 DOM：`pagination` 是**前端分页**（summary 视图是
 *   标量行，瓶颈在节点数不在 JSON，与总览页 QR-W7 同一条理由）；
 *   `row-key + reserve-selection` 保住跨页勾选，「分几页勾完再批量下发」的老路径不断
 * - `.kk-sticky-bar` → `.kk-select-banner`，并按 kk.scss 的配对说明**摘掉 `.kk-batch`**
 *   （两条同挂会把 12px(顶)+8px(底) 叠在一个元素上、间距翻倍）
 * - 严重度迁移：原 `el-tag :type` 的分档逐档映射到 `.kk-state` 圆点词汇，一条不折叠
 */

/**
 * 主机状态列的严重度分档（原 `el-tag :type="row.online ? 'success' : 'info'"`）：
 * - 在线 → `.kk-state` 默认实心绿点（圆点的默认语义就是「在线/健康」，逐字对上 success）
 * - 离线且在自更新窗口（`status_reason === "updating"`，B6）→ `.kk-state--updating`
 *   方点 + 「更新中」——本页是全站唯一名正言顺用它的地方：Agent 更新**真的**在进行，
 *   把它并进「离线」会让运维把「正在升级」当「掉线」去重试
 * - 其余离线 → `.kk-state--idle` 空心点，中性「不在」，不是错误（保持 info 档的中性）
 * 三档都比色块更可扫读；`kk-warn` 不在这里出现——它专指「版本号落后于待分发版本」，
 * 那个语义给「当前版本」列的文字色用（见下）。
 */
const hostStateClass = (row: HostSummary): string => {
  if (row.online) return "kk-state";
  if (row.status_reason === "updating") return "kk-state kk-state--updating";
  return "kk-state kk-state--idle";
};

const hostStateText = (row: HostSummary): string => {
  if (row.online) return "在线";
  if (row.status_reason === "updating") return "更新中";
  return "离线";
};

/**
 * 台账状态列的严重度分档：**逐档照搬 `@/utils/kk` 的 `statusType` 返回值**
 * （与 command/components/utils/hook.tsx 同一张映射表，L1-5 评审锁死的口径）：
 *
 * | statusType | 台账状态值 | 圆点 |
 * |---|---|---|
 * | `danger` | failed / timeout | 红点 + 红字 |
 * | `warning` / `primary` | （台账词表今天不产生 sent/running，映射保留防后端扩档） | 琥珀点 |
 * | `success` | done | 默认实心绿点（返回基类，不加变体） |
 * | `info` | pending / queued / 未知 | 空心点 |
 *
 * 不许折叠成「绿/红」两档：`queued`（已排队，重连即升）与 `pending`、`failed` 的
 * 运维动作完全不同，圆点必须分得开。
 */
const ledgerStateClass = (s: string): string => {
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

/**
 * 表格实例（按结构收窄；pure-table 暴露 getTableRef 拿内层 el-table）。
 * `setAdaptive` 是 @pureadmin/table 挂在 pure-table 上的重测入口：
 * adaptive 只在**挂载与窗口 resize** 时量一次 `innerHeight - tableWrapper.top - offsetBottom`，
 * 而本页主表的 top 取决于台账的**实际渲染高度**（自然高度、数据回来才定形）——
 * 挂载那次量到的是「空台账」的旧值（1920×1080 实测 stale 高 549、应为 321，
 * pager 底边 1224 越出视口、R7 亮红灯）。数据落位后显式重测一次，
 * 是「两张表同屏 + 上方表随数据变高」这个结构对 adaptive 的必要补偿。
 */
type UpdateTableApi = {
  getTableRef: () => {
    clearSelection: () => void;
    toggleRowSelection: (row: HostSummary, selected?: boolean) => void;
  };
  setAdaptive: () => void | Promise<void>;
};

export function useHostUpdate() {
  const route = useRoute();
  const POLL_KEY = "host-update";

  const loading = ref(false);
  const current = ref<AgentCurrent | null>(null);
  const outdated = ref<HostSummary[]>([]);
  const updates = ref<UpdateRow[]>([]);
  const interval = ref(10);
  const selection = ref<HostSummary[]>([]);
  const upgrading = ref(false);
  /** 静默轮询失败：状态进页头读数，不再刷 toast（W5） */
  const pollFailed = ref(false);

  const tableRef = ref<UpdateTableApi>();

  /** ?pods= 预填只应用一次；之后仅当 query 本身变化（watch）才重设，轮询刷新保留用户勾选 */
  let podsPrefilled = false;

  const hasLatest = computed(() => !!current.value?.version);

  /** 读到过真值没有：与「服务端说没有落后主机」是两件事（QR-W16） */
  const read = ref(false);
  /** 升级台账是独立一路（load 内自带 catch），它不能连带别的路一起谎称「暂无记录」 */
  const ledgerRead = ref(false);

  /**
   * 「落后 N / M 台」三态分开说：没读到 / 读到了但还没有待分发版本 / 有版本。
   * 前两态渲染 0 是假话——服务端注释写明「没上传过版本时 0 表示『无从定义』，
   * 不是『全都不落后』」。hosts_total 不受此限：它始终是真读数，0 就该显示 0。
   */
  const fleetInfo = computed(() => {
    const c = current.value;
    if (!read.value) {
      return { version: "—", outdated: "—", total: "—", cls: "" };
    }
    const known = !!c?.version;
    return {
      version: c?.version || "（未上传）",
      outdated: known ? (c?.hosts_outdated ?? 0) : "—",
      total: c?.hosts_total ?? "—",
      cls: known ? (c?.hosts_outdated ? "kk-bad" : "kk-ok") : ""
    };
  });

  /**
   * 主表分页——只有一个状态源（Task 6 裁决，本页沿用）。
   * **前端**分页：`listHosts("summary")` 一次带回全部标量行，瓶颈在 DOM 节点数
   * （500 行 × 富单元格，还要每 10s 随轮询整体重渲染，QR-W7 同一条理由），
   * 不在 JSON。所以 `total` 直接取清单长度、页内切片是 computed，翻页**不打后端**。
   */
  const pagination = reactive<PaginationProps>({
    total: 0,
    pageSize: 50,
    currentPage: 1,
    pageSizes: [50, 100, 200],
    background: true
  });

  const pagedOutdated = computed(() => {
    const start = (pagination.currentPage - 1) * pagination.pageSize;
    return outdated.value.slice(start, start + pagination.pageSize);
  });

  /** 数据变少后当前页可能越界（升级成功的行会离开清单）：归位到最后一页而不是空表 */
  function clampPage() {
    const maxPage = Math.max(
      1,
      Math.ceil(outdated.value.length / pagination.pageSize)
    );
    if (pagination.currentPage > maxPage) pagination.currentPage = maxPage;
  }

  /**
   * 主表列（勾选 / 主机 / 当前版本 / 目标版本 / 状态 / 最近心跳）。
   * 每列都有唯一且非空的 `label`：`PureTableBar` 按 `label` 匹配列做显隐 / 固定 /
   * 拖拽，`getKeyList` 静默丢弃缺 `label` 的列（Task 8 Step 0.5 实测预检的第一条）。
   * 同页另一张台账表的「主机 / 状态」与这里同名**不冲突**——唯一性只需在同一个
   * bar 内成立，而台账表不套 bar（Step 1 裁决）。
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
    { label: "主机", prop: "pod", minWidth: 180, align: "left" },
    {
      label: "当前版本",
      width: 120,
      // kk-warn 的正主语义就是「Agent 版本号落后于待分发版本」——本页整张表都是
      // 落后主机，版本列文字给它上色是**本义**，不是严重度挪用（挪用它的是命令页）
      cellRenderer: ({ row }) => (
        <span class="kk-warn kk-num">{row.agent_ver || "-"}</span>
      )
    },
    {
      label: "目标版本",
      width: 130,
      cellRenderer: () => (
        <span class="kk-num">→ {current.value?.version || "-"}</span>
      )
    },
    {
      label: "状态",
      width: 100,
      cellRenderer: ({ row }) => (
        <span class={hostStateClass(row)}>{hostStateText(row)}</span>
      )
    },
    {
      label: "最近心跳",
      width: 130,
      cellRenderer: ({ row }) => (
        <span class="kk-num">{ageText(row.age_sec)}</span>
      )
    }
  ];

  /**
   * 台账列（台账 ID / 主机 / 从 → 到 / 状态 / 原因 / 时间）。
   * 走**自然高度**的 pure-table（max-height 320 封顶、不套 bar、不开 adaptive）——
   * Step 1 的两表裁决；列定义也收进 hook 与主表同构，index.vue 只留模板。
   */
  const ledgerColumns: TableColumnList = [
    {
      label: "台账 ID",
      prop: "id",
      width: 170,
      showOverflowTooltip: true,
      align: "left",
      cellRenderer: ({ row }) => <span class="kk-num">{row.id}</span>
    },
    { label: "主机", prop: "pod", minWidth: 120, align: "left" },
    {
      label: "从 → 到",
      minWidth: 200,
      cellRenderer: ({ row }) => (
        <span class="kk-num">
          {row.from_version || "-"} → {row.to_version}
        </span>
      )
    },
    {
      label: "状态",
      width: 100,
      cellRenderer: ({ row }) => (
        <span class={ledgerStateClass(row.status)}>
          {statusLabel(row.status)}
        </span>
      )
    },
    {
      label: "原因",
      minWidth: 180,
      showOverflowTooltip: true,
      align: "left",
      cellRenderer: ({ row }) => row.reason || "-"
    },
    {
      label: "时间",
      width: 170,
      cellRenderer: ({ row }) => (
        <span class="kk-num">{tsText(row.created_at)}</span>
      )
    }
  ];

  // 作用域版 setPoll：卸载时只清本页注册的 key
  const { setPoll } = usePolls();
  /** 轮询与「升级后 reload」是两个入口，在途防重入只护定时器那路：
   *  迟到的回包既会把旧勾选模型盖回来，也会提前熄灭 loading（QR-W4）。 */
  const beginLoad = useSeq();

  /** silent=true 供轮询复用：勾选态与数据原位更新，不闪整页 loading */
  async function load(silent = false) {
    const isLatest = beginLoad();
    if (!silent) loading.value = true;
    try {
      let missLedger = false;
      const [cur, hosts, upds] = await Promise.all([
        getAgentCurrent(),
        listHosts("summary"),
        listUpdates(50).catch(() => {
          missLedger = true;
          return { items: [] as UpdateRow[], summary: {}, limit: 50 };
        })
      ]);
      if (!isLatest()) return;
      pollFailed.value = false;
      current.value = cur;
      outdated.value = hosts.items.filter(h => h.agent_outdated);
      updates.value = upds.items;
      read.value = true;
      ledgerRead.value = !missLedger;
      // 前端分页的 total 跟着清单长度走；升完功的行离开清单后当前页可能越界
      pagination.total = outdated.value.length;
      clampPage();
      // 轮询刷新保留现有勾选：先把模型对齐到新数据（丢掉已不再落后的主机），再回填表格；
      // 「?pods= 预填」只在首次加载与 query 本身变化（watch）时应用，不随轮询重设
      reconcileSelection();
      if (!podsPrefilled) {
        podsPrefilled = true;
        prefillFromQuery();
      } else {
        applySelection();
      }
    } catch (e: any) {
      if (!isLatest()) return;
      pollFailed.value = silent;
      if (!silent)
        message("加载升级信息失败：" + errText(e), { type: "error" });
    } finally {
      if (isLatest()) {
        loading.value = false;
        // 台账自然高度随数据定形后重测 adaptive（理由见 UpdateTableApi 注释）。
        // silent 轮询也要：台账新行会把主表继续往下推。
        tableRef.value?.setAdaptive?.();
      }
    }
  }

  /**
   * 用户勾选 / 表头全选 → 模型。两个事件的第一参数都是**表格内部的全量勾选集合**：
   * 开了 reserve-selection 后它含其他页的勾选项（按 row-key 认人），跨页勾选不会
   * 被页内集合覆盖。这里仍与模型合并一次「不在当前页」的条目——防的是表头全选
   * 语义只作用于本页数据、而 reserve 未生效的路径把跨页勾选冲掉：模型的并集
   * 与事件的差集合起来，两头都不丢。
   */
  function onSelect(rows: HostSummary[]) {
    const pagePods = new Set(pagedOutdated.value.map(h => h.pod));
    const offPage = selection.value.filter(
      h => !pagePods.has(h.pod) && !rows.some(r => r.pod === h.pod)
    );
    selection.value = [...rows, ...offPage];
  }

  /** 数据刷新后把模型对齐到新行对象：丢掉已不再落后的主机，展示与提交都不引用过期数据 */
  function reconcileSelection() {
    const pods = new Set(selection.value.map(h => h.pod));
    selection.value = outdated.value.filter(h => pods.has(h.pod));
  }

  /**
   * 换数据后按模型回填表格：清空（连 reserve 的跨页残留一起清）再逐台勾回。
   * 勾选对象取**全量清单**而非当前页——不在当前页的行由 reserve-selection
   * 按 row-key 记住，翻过去时自动带勾；「已选 N 台」读数始终以模型为准，
   * 不依赖表格能不能表达某一页。
   */
  function applySelection() {
    const table = tableRef.value?.getTableRef?.();
    if (!table) return;
    table.clearSelection();
    const pods = new Set(selection.value.map(h => h.pod));
    for (const h of outdated.value) {
      if (pods.has(h.pod)) table.toggleRowSelection(h, true);
    }
  }

  /** 跨页带来的 ?pods= 预填（从主机总览「批量升级」跳转），只认当前落后清单里的主机 */
  function prefillFromQuery() {
    const qpods = String(route.query.pods || "")
      .split(",")
      .map(s => s.trim())
      .filter(Boolean);
    if (!qpods.length) return;
    selection.value = outdated.value.filter(h => qpods.includes(h.pod));
    applySelection();
  }

  /**
   * 换条数是纯前端重切片：归位第 1 页即可，不打后端；已勾选的行按 row-key 由
   * reserve-selection 跨页保勾，翻页同样不用重挂表格。
   */
  watch(
    () => pagination.pageSize,
    () => {
      pagination.currentPage = 1;
    }
  );

  /** 仅当 ?pods= 本身变化时才重设勾选；轮询 load() 不触发这里，用户手改的勾选不被冲掉 */
  watch(
    () => route.query.pods,
    () => prefillFromQuery()
  );

  /**
   * 被跳过的台数超过这个值就只列前若干条，其余按原因聚合报数。
   * 500 台批量升级若被跳过几十台，只给一个数字运维无法判断该不该重试
   * （`no_binary` 该去上传、`already_latest` 重试也没用）。
   */
  const SKIP_DETAIL_MAX = 10;

  /** 升级后逐条列出跳过的主机与原因（QR-W9 重做）。
   *  只用 VNode 文本节点走 Vue 转义：主机名由 Agent 自报，不可信。
   *  跳过原因的取词**只**走 upgradeSkipText——枚举真相在 `controllers/agent_update.py`，
   *  前端重抄一份就是 QR-W11 判例（抄枚举让 HEAD 红了一轮）。 */
  async function showSkipped(
    skipped: Array<{ host: string; reason: string }>
  ): Promise<void> {
    if (!skipped.length) return;
    const shown = skipped.slice(0, SKIP_DETAIL_MAX);
    const rest = skipped.slice(SKIP_DETAIL_MAX);
    const lines = shown.map(s =>
      h("p", `${s.host} —— ${upgradeSkipText(s.reason)}`)
    );
    if (rest.length) {
      // 被跳过的主机不会进「升级台账」（后端只为 accepted 建账），
      // 所以这里只能自己把剩余台数按原因归并，不能把人指去查不到的地方。
      const byReason = new Map<string, number>();
      for (const s of rest) {
        byReason.set(s.reason, (byReason.get(s.reason) ?? 0) + 1);
      }
      const agg = [...byReason.entries()]
        .map(([r, n]) => `${upgradeSkipText(r)} ${n} 台`)
        .join("、");
      lines.push(h("p", `另有 ${rest.length} 台：${agg}`));
    }
    try {
      await ElMessageBox.alert(
        h("div", { class: "kk-confirm" }, lines),
        `跳过 ${skipped.length} 台`,
        {
          type: "warning",
          confirmButtonText: "知道了",
          customClass: "kk-skip-box"
        }
      );
    } catch {
      /* 用户点掉弹窗即可，不影响主流程 */
    }
  }

  async function onUpgrade() {
    if (!selection.value.length) {
      message("请先勾选要升级的主机", { type: "warning" });
      return;
    }
    const version = current.value?.version || "";
    if (!version) {
      message("服务端没有待分发版本，先去上传", { type: "warning" });
      return;
    }
    const offlineCount = selection.value.filter(h => !h.online).length;
    let warn = `将把 ${selection.value.length} 台主机升到 ${version}`;
    if (offlineCount) {
      // 离线不算失败，但用户必须看到这一点 —— 升上去要等主机重连
      warn += `，其中 ${offlineCount} 台离线，会标记为 queued 等主机上线后补投`;
    }
    warn += "。确认执行？";
    try {
      await ElMessageBox.confirm(warn, "批量升级", {
        type: "warning",
        confirmButtonText: "确认升级"
      });
    } catch {
      return;
    }
    upgrading.value = true;
    try {
      const r = await upgradeHosts(selection.value.map(h => h.pod));
      const ok = r.accepted.length;
      const skip = r.skipped.length;
      const q = r.accepted.filter(a => a.queued).length;
      const lines = [`已受理 ${ok} 台`];
      if (q) lines.push(`其中 ${q} 台离线，会在重连时自动补投`);
      if (skip) lines.push(`跳过 ${skip} 台（见下方明细）`);
      message(lines.join("，"), { type: "success" });
      selection.value = [];
      // 先刷数据再弹明细：让台账与弹窗内容对得上，而不是反过来
      await load();
      await showSkipped(r.skipped);
    } catch (e: any) {
      message("升级失败：" + errText(e), { type: "error" });
    } finally {
      upgrading.value = false;
    }
  }

  /**
   * 选「所有落后主机」按钮的语义：是「待升级的潜在目标」，不是「会全网推」
   * —— 还是要确认后再真正下发，避免误点把 500 台全推下去。
   * 模型装满后 applySelection 逐台 toggle：不在当前页的行也进表格的勾选集合
   * （勾选态按 row-key 渲染，翻过去自然带勾），「已选 N 台」是全量读数。
   */
  function selectAllOutdated() {
    selection.value = [...outdated.value];
    applySelection(); // 模型与表格勾选态同步
    message(
      `已选 ${selection.value.length} 台落后主机；「批量升级」确认后再下发`,
      { type: "info" }
    );
  }

  function clearSelection() {
    selection.value = [];
    tableRef.value?.getTableRef?.().clearSelection(); // 模型与表格勾选态同步（含跨页残留）
  }

  /** 离线台数：横幅的「含离线主机」提示与升级确认共用一个口径 */
  const offlineSelected = computed(
    () => selection.value.filter(h => !h.online).length
  );

  /**
   * 换间隔只改一处真相：与总览页同构（watch(interval) 重建定时器），
   * 不再把 setPoll 写在模板的 @change 里做类型强转（FE-27）。
   */
  function restartTimer() {
    setPoll(POLL_KEY, () => load(true), interval.value * 1000);
  }

  watch(interval, restartTimer);

  onMounted(async () => {
    await load();
    restartTimer();
  });

  return {
    POLL_KEY,
    loading,
    current,
    outdated,
    pagedOutdated,
    updates,
    interval,
    selection,
    offlineSelected,
    upgrading,
    pollFailed,
    read,
    ledgerRead,
    hasLatest,
    fleetInfo,
    tableRef,
    columns,
    ledgerColumns,
    pagination,
    load,
    onSelect,
    onUpgrade,
    showSkipped,
    selectAllOutdated,
    clearSelection
  };
}
