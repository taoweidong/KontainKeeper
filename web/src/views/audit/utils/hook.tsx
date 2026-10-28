import { onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import type { PaginationProps } from "@pureadmin/table";
import { message } from "@/utils/message";
import { listAudit, parseDetail, type AuditRow } from "@/api/audit";
import { exportAudit } from "@/api/exporting";
import { downloadBlob, errText, fileStamp, tsText } from "@/utils/kk";
import { useSeq } from "@/utils/kkPoll";

/**
 * 动作列的严重度判级（原页面的 `actionType` 四档，一处语义不改，只是将返回值从
 * el-tag 的 type 换成本仓状态语言的类名）。
 *
 * 为什么必须保留分级：`.kk-state::before` 的圆点默认是 `var(--kk-live)`（绿 = 在线/健康），
 * 而 `.kk-bad` 只改**文字**颜色、不动圆点。审计页是追溯凭证，若只按「含不含 fail」上色，
 * `login_locked` / `ip_rejected` / `proto_mismatch` / `cmd timeout` 这些事件会顶着一枚
 * 绿色健康点显示「一切正常」——严重度在迁移中丢失（L1-5 评审揪出的真实回归）。
 *
 * 四档（顺序即优先级，bad 先判）：
 * - `fail|blocked|rejected|locked|violation` → 红点 + 红字
 * - `mismatch|timeout` → 琥珀点
 * - `ok|create|restore` → 默认实心绿点（返回基类，不加变体）
 * - 其余（logout/export/agent_upload…）→ 空心点，中性事件不是错误也不是健康
 *
 * 不许复用 `.kk-state--updating`：那是方点、承载 B6「更新中」语义，不是告警色。
 */
const actionStateClass = (a: string) => {
  if (
    a.includes("fail") ||
    a.includes("blocked") ||
    a.includes("rejected") ||
    a.includes("locked") ||
    a.includes("violation")
  )
    return "kk-state kk-state--bad kk-bad";
  if (a.includes("mismatch") || a.includes("timeout"))
    return "kk-state kk-state--warn";
  if (a.includes("ok") || a.includes("create") || a.includes("restore"))
    return "kk-state";
  return "kk-state kk-state--idle";
};

/**
 * 审计日志页的数据流与列定义。
 *
 * 分页只有一个状态源：`pagination`（`pageSize` / `currentPage`）。搜索条里没有条数
 * 下拉，条数交给分页器的 `sizes`；`offset` 由 `(currentPage - 1) * pageSize` 推导，
 * 不再单独持有原页面的 `limit`/`offset` 两个 ref（Task 6 裁决）。
 */
export function useAuditLog() {
  const form = reactive({ keyword: "" });
  const loading = ref(false);
  const exporting = ref(false);
  const dataList = ref<AuditRow[]>([]);
  /** 读到过真值没有：「暂无审计记录」是强断言（等于说没人操作过），
   *  读不到时必须与「确实为空」分开（QR-W16 红线）。 */
  const read = ref(false);
  const pagination = reactive<PaginationProps>({
    total: 0,
    pageSize: 200,
    currentPage: 1,
    pageSizes: [100, 200, 500],
    background: true
  });

  /**
   * 每列都有唯一且非空的 `label`：`PureTableBar` 按 `label` 匹配列做显隐 / 固定 /
   * 拖拽，`getKeyList` 不去重也会静默丢弃缺 `label` 的列——重名会让第二列无法隐藏，
   * 缺 label 会让浮层勾选与 `dynamicColumns` 下标错位（typecheck / eslint 抓不到）。
   */
  const columns: TableColumnList = [
    { label: "#", prop: "id", width: 80 },
    {
      label: "时间",
      width: 170,
      cellRenderer: ({ row }) => <span class="kk-num">{tsText(row.ts)}</span>
    },
    { label: "操作者", prop: "actor", width: 140 },
    {
      label: "动作",
      width: 180,
      cellRenderer: ({ row }) => (
        <span class={actionStateClass(row.action)}>{row.action}</span>
      )
    },
    {
      label: "明细",
      minWidth: 320,
      align: "left",
      cellRenderer: ({ row }) => (
        <>
          {Object.entries(parseDetail(row.detail)).map(([k, v]) => (
            <span key={k} class="kk-kv">
              <b>{k}</b>:{" "}
              {typeof v === "object" ? JSON.stringify(v) : String(v)}
            </span>
          ))}
          {!row.detail && <span class="kk-sub">-</span>}
        </>
      )
    }
  ];

  const beginLoad = useSeq();

  async function onSearch() {
    const isLatest = beginLoad();
    loading.value = true;
    try {
      // 关键字下推后端（store._audit_filters），与导出共用同一套语义：所见即所得
      const data = await listAudit({
        limit: pagination.pageSize,
        offset: (pagination.currentPage - 1) * pagination.pageSize,
        keyword: form.keyword.trim() || undefined
      });
      if (!isLatest()) return;
      dataList.value = data.items;
      pagination.total = data.total;
      read.value = true;
    } catch (e: any) {
      if (!isLatest()) return;
      message("加载审计日志失败：" + errText(e), { type: "error" });
    } finally {
      if (isLatest()) loading.value = false;
    }
  }

  /**
   * 回到第 1 页并重载一次。`el-pagination` 只在**用户点击**时 emit `current-change`，
   * 程序改 `pagination.currentPage` 不触发 emit，所以这里必须自己 `onSearch()`；
   * 反过来用户点页码时 pure-table 已把 `currentPage` 改进这个 reactive，`@page-current-change`
   * 只调 `onSearch()`、不再赋值（FE-8 同族：既归位又靠 emit 才会各打一发）。
   */
  function reloadFirstPage() {
    pagination.currentPage = 1;
    onSearch();
  }

  /** 关键字 300ms 防抖：输入停顿才打后端（原页行为保持） */
  let kwTimer: ReturnType<typeof setTimeout> | null = null;
  watch(
    () => form.keyword,
    () => {
      if (kwTimer) clearTimeout(kwTimer);
      kwTimer = setTimeout(reloadFirstPage, 300);
    }
  );
  onBeforeUnmount(() => {
    if (kwTimer) clearTimeout(kwTimer);
  });

  /**
   * 条数是唯一来源：pure-table 直接改这个 reactive `pagination`，`pageSize` 一变就
   * 归位重载一次。模板里**不绑** `@page-size-change`——那会在这里之外多打一遍后端。
   */
  watch(() => pagination.pageSize, reloadFirstPage);

  async function onExport() {
    exporting.value = true;
    try {
      // 导出带上当前关键字与条数：审计是追溯凭证，导出范围必须与页面所见一致
      const blob = await exportAudit({
        keyword: form.keyword.trim() || undefined,
        limit: pagination.pageSize
      });
      downloadBlob(blob, `审计日志_${fileStamp()}.csv`);
    } catch (e: any) {
      message("导出失败：" + errText(e), { type: "error" });
    } finally {
      exporting.value = false;
    }
  }

  /**
   * 重置：清空关键字并回到第 1 页。关键字若从有值变空，交给防抖那一路统一重载
   * （避免与归位 emit 叠加成两发）；若本就为空、防抖不会触发，则直接归位重载。
   */
  function resetFields() {
    if (form.keyword !== "") {
      form.keyword = "";
    } else {
      if (kwTimer) clearTimeout(kwTimer);
      reloadFirstPage();
    }
  }

  onMounted(onSearch);

  return {
    form,
    columns,
    dataList,
    loading,
    read,
    exporting,
    pagination,
    onSearch,
    onExport,
    resetFields
  };
}
