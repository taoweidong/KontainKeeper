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
import { listIpRejects, type IpRejectRow } from "@/api/ipwhitelist";
import { exportIpRejects } from "@/api/exporting";
import { downloadBlob, errText, fileStamp, nowSec, tsText } from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";

/** 帧类型中文（QR1124）：与桥接 _on_ip_rejected 的 kind 推断同口径
 *  （status 带 online 键 / result 带 id 键 / 其余 hb）。未知原样回显。 */
const KIND_LABEL: Record<string, string> = {
  status: "状态帧",
  hb: "心跳帧",
  result: "结果帧"
};
export const rejectKindText = (k: string): string => KIND_LABEL[k] || k || "-";

/**
 * 拦截记录页的数据流与列定义（QR1124，照 audit 骨架）。
 *
 * 记录按源 IP 聚合（服务端 kk_ip_rejects，ip 主键）：一行一个 IP，hits 为节流后
 * 的 recorded 计数。分页唯一状态源 pagination；ip 模糊筛选下推（与导出同语义）。
 * 自动刷新走 usePolls（作用域清理 + 在途防重入），失败变冷不刷 toast（syncReadout）。
 */
export function useIpRejects() {
  const POLL_KEY = "ip-rejects";
  const form = reactive({ ip: "" });
  const loading = ref(false);
  const exporting = ref(false);
  const dataList = ref<IpRejectRow[]>([]);
  /** 读到过真值没有（QR-W16）：接口挂掉 ≠ 「暂无拦截」 */
  const read = ref(false);
  const pollFailed = ref(false);
  const lastLoadedAt = ref(0);
  const autoRefresh = ref(true);
  const interval = ref(30);
  const pagination = reactive<PaginationProps>({
    total: 0,
    pageSize: 100,
    currentPage: 1,
    pageSizes: [50, 100, 200],
    background: true
  });

  /** 每列唯一非空 label（PureTableBar 按 label 匹配显隐/拖拽）。
   *  被拒语义红点红字（kk-state--bad kk-bad）：拦截次数是告警读数。 */
  const columns: TableColumnList = [
    {
      label: "IP",
      prop: "ip",
      minWidth: 160,
      align: "left",
      cellRenderer: ({ row }) => <span class="kk-num">{row.ip || "-"}</span>
    },
    {
      label: "拦截次数",
      width: 100,
      cellRenderer: ({ row }) => <span class="kk-num kk-bad">{row.hits}</span>
    },
    { label: "最近主机", prop: "pod", minWidth: 140, align: "left" },
    {
      label: "帧类型",
      width: 100,
      cellRenderer: ({ row }) => rejectKindText(row.kind)
    },
    {
      label: "首次拦截",
      width: 170,
      cellRenderer: ({ row }) => (
        <span class="kk-num">{tsText(row.first_seen)}</span>
      )
    },
    {
      label: "最近拦截",
      width: 170,
      cellRenderer: ({ row }) => (
        <span class="kk-num">{tsText(row.last_seen)}</span>
      )
    }
  ];

  const hasFilter = computed(() => !!form.ip.trim());
  const beginLoad = useSeq();
  const { setPoll, clearPoll } = usePolls();

  async function onSearch(silent = false) {
    const isLatest = beginLoad();
    if (!silent) loading.value = true;
    try {
      const data = await listIpRejects({
        limit: pagination.pageSize,
        offset: (pagination.currentPage - 1) * pagination.pageSize,
        ip: form.ip.trim() || undefined
      });
      if (!isLatest()) return;
      dataList.value = data.items;
      pagination.total = data.total;
      read.value = true;
      pollFailed.value = false;
      lastLoadedAt.value = nowSec();
    } catch (e: any) {
      if (!isLatest()) return;
      pollFailed.value = true;
      if (!silent)
        message("加载拦截记录失败：" + errText(e), { type: "error" });
    } finally {
      if (isLatest()) loading.value = false;
    }
  }

  function reloadFirstPage() {
    pagination.currentPage = 1;
    onSearch();
  }

  /** ip 关键字 300ms 防抖归位（照 audit 关键字口径） */
  let kwTimer: ReturnType<typeof setTimeout> | null = null;
  watch(
    () => form.ip,
    () => {
      if (kwTimer) clearTimeout(kwTimer);
      kwTimer = setTimeout(reloadFirstPage, 300);
    }
  );
  onBeforeUnmount(() => {
    if (kwTimer) clearTimeout(kwTimer);
  });

  watch(() => pagination.pageSize, reloadFirstPage);

  function restartTimer() {
    if (!autoRefresh.value) return clearPoll(POLL_KEY);
    setPoll(POLL_KEY, () => onSearch(true), interval.value * 1000);
  }
  watch(interval, restartTimer);

  async function onExport() {
    exporting.value = true;
    try {
      const blob = await exportIpRejects({
        ip: form.ip.trim() || undefined,
        limit: pagination.pageSize
      });
      downloadBlob(blob, `拦截记录_${fileStamp()}.csv`);
    } catch (e: any) {
      message("导出失败：" + errText(e), { type: "error" });
    } finally {
      exporting.value = false;
    }
  }

  /** 重置：ip 有值 → 清空交防抖统一重载；本就空 → 直接归位（任何路径单发） */
  function resetFields() {
    if (kwTimer) {
      clearTimeout(kwTimer);
      kwTimer = null;
    }
    if (form.ip !== "") {
      form.ip = "";
    } else {
      reloadFirstPage();
    }
  }

  onMounted(async () => {
    await onSearch();
    restartTimer();
  });

  return {
    POLL_KEY,
    form,
    columns,
    dataList,
    loading,
    read,
    hasFilter,
    exporting,
    pollFailed,
    lastLoadedAt,
    autoRefresh,
    interval,
    pagination,
    onSearch,
    reloadFirstPage,
    onExport,
    resetFields,
    restartTimer,
    clearPoll
  };
}
