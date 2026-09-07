<script setup lang="ts">
/** 执行历史（采集面板 / 命令面板共用）：状态过滤 + 5s 静默轮询 + 输出查看。
 *
 * 原 command/index.vue 两个 tab 共用一个历史卡片，拆成两个路由页后
 * 抽成组件复用；父页下发命令成功后调 reload() 立即刷新。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";

import {
  getCommandOut,
  listCommands,
  type CommandRow
} from "@/api/commands";
import { numText, statusLabel, statusType, tsText } from "@/utils/kk";

defineOptions({ name: "CommandHistory" });

const rows = ref<CommandRow[]>([]);
const loading = ref(false);
const statusFilter = ref("");
const autoRefresh = ref(true);
let timer: ReturnType<typeof setInterval> | null = null;

const outDialog = reactive({ visible: false, title: "", text: "", loading: false });

const filteredRows = computed(() =>
  statusFilter.value ? rows.value.filter(r => r.status === statusFilter.value) : rows.value
);

/** silent=true 用于 5s 轮询：不闪按钮 loading；手动刷新走默认带 loading */
async function loadCommands(silent = false) {
  if (!silent) loading.value = true;
  try {
    rows.value = (await listCommands({ limit: 100 })).items;
  } catch (e: any) {
    ElMessage.error("加载命令历史失败：" + (e?.message ?? e));
  } finally {
    loading.value = false;
  }
}

function restartTimer() {
  if (timer) clearInterval(timer);
  timer = autoRefresh.value ? setInterval(() => loadCommands(true), 5000) : null;
}

function errText(e: any): string {
  return e?.response?.data?.detail ?? e?.message ?? String(e);
}

async function showOut(row: CommandRow) {
  outDialog.title = `${row.id} · ${row.pod}`;
  outDialog.text = "";
  outDialog.visible = true;
  if (row.out_purged) {
    outDialog.text = "（输出已按保留策略清理，仅保留状态行）";
    return;
  }
  outDialog.loading = true;
  try {
    outDialog.text = (await getCommandOut(row.id)) || "（无输出）";
  } catch (e: any) {
    outDialog.text = "读取输出失败：" + errText(e);
  } finally {
    outDialog.loading = false;
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

onMounted(async () => {
  await loadCommands();
  restartTimer();
});

onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
  timer = null;
});

/** 父页下发命令成功后调用：立即刷新一次（带 loading 反馈） */
defineExpose({ reload: () => loadCommands() });
</script>

<template>
  <el-card shadow="never" class="kk-card">
    <template #header>
      <div class="kk-toolbar">
        <span><b>执行历史</b></span>
        <div class="kk-actions">
          <el-select v-model="statusFilter" clearable placeholder="状态" style="width: 130px">
            <el-option label="待下发" value="pending" />
            <el-option label="已下发" value="sent" />
            <el-option label="执行中" value="running" />
            <el-option label="已完成" value="done" />
            <el-option label="失败" value="failed" />
            <el-option label="超时" value="timeout" />
            <el-option label="结果丢失" value="lost" />
          </el-select>
          <el-checkbox v-model="autoRefresh" @change="restartTimer">5 秒自动刷新</el-checkbox>
          <el-button :loading="loading" @click="loadCommands()">刷新</el-button>
        </div>
      </div>
    </template>

    <el-table :data="filteredRows" size="small" height="calc(100vh - 560px)">
      <el-table-column prop="id" label="ID" width="120" />
      <el-table-column prop="pod" label="主机" min-width="160" />
      <el-table-column prop="kind" label="类型" width="90" />
      <el-table-column label="命令 / 采集项" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ argvPreview(row.argv) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small">
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
      <el-table-column label="输出" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.out_purged ? "（已清理）" : row.out_tail || "-" }}
        </template>
      </el-table-column>
      <el-table-column label="创建" width="160">
        <template #default="{ row }">{{ tsText(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="showOut(row)">查看</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="还没有命令记录" />
      </template>
    </el-table>

    <el-dialog v-model="outDialog.visible" :title="outDialog.title" width="720px">
      <el-scrollbar max-height="440px">
        <pre v-loading="outDialog.loading" class="kk-out">{{ outDialog.text }}</pre>
      </el-scrollbar>
    </el-dialog>
  </el-card>
</template>
