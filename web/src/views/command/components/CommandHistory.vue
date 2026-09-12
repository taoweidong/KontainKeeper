<script setup lang="ts">
/** 执行历史（采集面板 / 命令面板共用）：分页 + 搜索 + 批次 + 自适应轮询 + 输出抽屉。

改造要点（A5.6 / A3 前端）：
- 筛选全部下推后端（分页 / 状态 / 关键字 / 批次），否则导出与所见不一致
- 轮询自适应：有未终态命令时 3s，否则 10s；不再无条件 5s 空转
- 输出改右侧抽屉：原 el-dialog 居中弹窗会完全遮挡列表，无法连续对比多条
- 本次下发的行加左侧色条：500 台里一眼找到刚才发的那批
*/
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import {
  getCommandOut,
  listBatches,
  listCommands,
  type BatchSummary,
  type CommandRow
} from "@/api/commands";
import { exportCommands } from "@/api/exporting";
import { downloadBlob, fileStamp, numText, statusLabel, statusType, tsText } from "@/utils/kk";
import { clearPolls, setPoll, usePolls } from "@/utils/kkPoll";

defineOptions({ name: "CommandHistory" });

const POLL_KEY = "command-history";

const rows = ref<CommandRow[]>([]);
const total = ref(0);
const loading = ref(false);
const statusFilter = ref("");
const keyword = ref("");
const batchFilter = ref("");
const pageSize = ref(50);
const offset = ref(0);
const autoRefresh = ref(true);
/** 本次（或最近一次）下发的命令 id：给这些行加左侧色条 */
const fresh = ref<Set<string>>(new Set());
const batches = ref<BatchSummary[]>([]);
const exporting = ref(false);

const out = reactive({ visible: false, title: "", text: "", loading: false, id: "" });

usePolls();

function errText(e: any): string {
  return e?.response?.data?.detail ?? e?.message ?? String(e);
}

async function loadCommands(silent = false) {
  if (!silent) loading.value = true;
  try {
    const data = await listCommands({
      status: statusFilter.value || undefined,
      keyword: keyword.value.trim() || undefined,
      batch: batchFilter.value || undefined,
      limit: pageSize.value,
      offset: offset.value
    });
    rows.value = data.items;
    total.value = data.total;
  } catch (e: any) {
    ElMessage.error("加载命令历史失败：" + (e?.message ?? e));
  } finally {
    loading.value = false;
  }
}

async function loadBatches() {
  try {
    batches.value = (await listBatches(20)).items;
  } catch {
    batches.value = [];
  }
}

/** 自适应轮询：有未终态命令时 3s，否则 10s */
function restartTimer() {
  if (!autoRefresh.value) return clearPolls();
  const hasActive = rows.value.some(r =>
    ["pending", "sent", "running"].includes(r.status)
  );
  setPoll(POLL_KEY, () => loadCommands(true), hasActive ? 3000 : 10000);
}

watch([rows], restartTimer);
watch([statusFilter, batchFilter, pageSize], () => {
  offset.value = 0;
  loadCommands();
  restartTimer();
});

/** 关键字输入防抖：避免每敲一个字就打一次后端 */
let kwTimer: ReturnType<typeof setTimeout> | null = null;
watch(keyword, () => {
  if (kwTimer) clearTimeout(kwTimer);
  kwTimer = setTimeout(() => {
    offset.value = 0;
    loadCommands();
  }, 300);
});

/** 当前批次的状态分布：选中批次时工具栏直接给出「N 台：done X / failed Y」 */
const batchStat = computed(() => {
  if (!batchFilter.value) return null;
  const b = batches.value.find(x => x.batch_id === batchFilter.value);
  return b || null;
});

/** 批次分布值是 string|number 联合类型，这里统一成数字再累加 */
function statNum(v: string | number | undefined): number {
  return Number(v || 0);
}

/** el-pagination 用 1 起始的页码，后端要的是 offset —— 换算只在这里做一次 */
const pageNo = computed({
  get: () => Math.floor(offset.value / pageSize.value) + 1,
  set: (v: number) => {
    offset.value = (v - 1) * pageSize.value;
  }
});

function onPageChange(p: number) {
  offset.value = (p - 1) * pageSize.value;
  loadCommands();
  restartTimer();
}

/** 每页条数变化由上面的 watch 统一重置 offset 并重载，这里只赋值 */
function onSizeChange(s: number) {
  pageSize.value = s;
}

function shortBatch(id?: string): string {
  return id ? id.slice(0, 8) : "-";
}

/** 父页下发成功后调用：定位到该批次并高亮新命令 */
function focusBatch(batchId: string, ids: string[] = []) {
  fresh.value = new Set(ids);
  batchFilter.value = batchId;
  offset.value = 0;
  loadCommands();
  loadBatches();
  restartTimer();
}

async function showOut(row: CommandRow) {
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
    out.text = (await getCommandOut(row.id)) || "（无输出）";
  } catch (e: any) {
    out.text = "读取输出失败：" + errText(e);
  } finally {
    out.loading = false;
  }
}

/** 下载单条完整输出：复用导出工具，不新增依赖 */
async function downloadOut() {
  try {
    const text = await getCommandOut(out.id);
    downloadBlob(new Blob([text], { type: "text/plain;charset=utf-8" }), `${out.id}.txt`);
  } catch (e: any) {
    ElMessage.error("下载输出失败：" + errText(e));
  }
}

async function onExport() {
  exporting.value = true;
  try {
    const blob = await exportCommands({
      status: statusFilter.value || undefined,
      batch: batchFilter.value || undefined,
      keyword: keyword.value.trim() || undefined,
      include_tail: 1
    });
    downloadBlob(blob, `命令历史_${fileStamp()}.csv`);
  } catch (e: any) {
    ElMessage.error("导出失败：" + errText(e));
  } finally {
    exporting.value = false;
  }
}

function argvPreview(argv: CommandRow["argv"]): string {
  if (!argv) return "-";
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

onMounted(async () => {
  await Promise.all([loadCommands(), loadBatches()]);
  restartTimer();
});

defineExpose({ reload: () => loadCommands(), focusBatch });
</script>

<template>
  <el-card shadow="never" class="kk-card kk-page__body">
    <template #header>
      <div class="kk-toolbar">
        <span>
          <b>执行历史</b>
          <span v-if="batchStat" class="kk-sub kk-ml">
            该批次 {{ batchStat.total }} 台：
            完成 {{ batchStat.done || 0 }} · 失败 {{ batchStat.failed || 0 }} ·
            超时 {{ batchStat.timeout || 0 }} · 未终态
            {{
              statNum(batchStat.pending) +
              statNum(batchStat.sent) +
              statNum(batchStat.running)
            }}
          </span>
        </span>
        <div class="kk-actions">
          <el-input
            v-model="keyword"
            placeholder="搜索主机 / 命令 / ID"
            clearable
            style="width: 200px"
          />
          <el-select v-model="statusFilter" clearable placeholder="状态" style="width: 130px">
            <el-option label="待下发" value="pending" />
            <el-option label="已下发" value="sent" />
            <el-option label="执行中" value="running" />
            <el-option label="已完成" value="done" />
            <el-option label="失败" value="failed" />
            <el-option label="超时" value="timeout" />
            <el-option label="结果丢失" value="lost" />
          </el-select>
          <el-select v-model="batchFilter" clearable placeholder="批次" style="width: 150px">
            <el-option
              v-for="b in batches"
              :key="b.batch_id"
              :label="shortBatch(b.batch_id) + '（' + b.total + '）'"
              :value="b.batch_id"
            />
          </el-select>
          <el-checkbox
            v-model="autoRefresh"
            @change="v => (v ? restartTimer() : clearPolls())"
          >
            自动刷新
          </el-checkbox>
          <el-button :loading="loading" @click="loadCommands()">刷新</el-button>
          <el-button type="primary" :loading="exporting" @click="onExport">导出 CSV</el-button>
        </div>
      </div>
    </template>

    <el-table
      v-loading="loading"
      :data="rows"
      :row-class-name="rowClass"
      size="small"
      class="kk-fill-table"
      @row-click="showOut"
    >
      <el-table-column prop="id" label="ID" width="120" />
      <el-table-column prop="pod" label="主机" min-width="150" />
      <el-table-column label="批次" width="100">
        <template #default="{ row }">
          <el-tooltip v-if="row.batch_id" :content="row.batch_id" placement="top">
            <el-link type="primary" @click.stop="batchFilter = row.batch_id">
              {{ shortBatch(row.batch_id) }}
            </el-link>
          </el-tooltip>
          <span v-else class="kk-sub">-</span>
        </template>
      </el-table-column>
      <el-table-column prop="kind" label="类型" width="90" />
      <el-table-column label="命令 / 采集项" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">{{ argvPreview(row.argv) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small" @click.stop>
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="rc" width="70">
        <template #default="{ row }">{{ row.rc === null ? "-" : row.rc }}</template>
      </el-table-column>
      <el-table-column label="耗时" width="90">
        <template #default="{ row }">
          {{ row.elapsed_ms === null ? "-" : numText(row.elapsed_ms / 1000, 2) + " s" }}
        </template>
      </el-table-column>
      <el-table-column label="输出" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.out_purged ? "（已清理）" : row.out_tail || "-" }}
        </template>
      </el-table-column>
      <el-table-column label="创建" width="160">
        <template #default="{ row }">{{ tsText(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="showOut(row)">查看</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty :description="total ? '本页无数据' : '还没有命令记录'" />
      </template>
    </el-table>

    <div class="kk-pager">
      <el-pagination
        v-model:current-page="pageNo"
        :page-size="pageSize"
        :total="total"
        :page-sizes="[50, 100, 200]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        small
        @size-change="onSizeChange"
        @current-change="onPageChange"
      />
    </div>

    <el-drawer v-model="out.visible" :title="out.title" size="42%">
      <el-scrollbar class="kk-out-scroll">
        <pre v-loading="out.loading" class="kk-out">{{ out.text }}</pre>
      </el-scrollbar>
      <template #footer>
        <div class="kk-toolbar">
          <span class="kk-sub">点击列表其他行可连续查看，列表不会被遮挡</span>
          <el-button type="primary" @click="downloadOut">下载此条输出</el-button>
        </div>
      </template>
    </el-drawer>
  </el-card>
</template>

<style scoped>
.kk-pager {
  display: flex;
  justify-content: flex-end;
  padding-top: 10px;
}

.kk-out-scroll {
  height: 100%;
}
</style>
