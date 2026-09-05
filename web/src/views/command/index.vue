<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";

import {
  createCommand,
  getCommandOut,
  listCollectItems,
  listCommands,
  type CommandRow
} from "@/api/commands";
import { listHosts, type HostSummary } from "@/api/containers";
import { numText, statusLabel, statusType, tsText } from "@/utils/kk";

defineOptions({ name: "CommandCenter" });

const route = useRoute();

const hosts = ref<HostSummary[]>([]);
const items = ref<string[]>([]);
const rows = ref<CommandRow[]>([]);
const loading = ref(false);
const submitting = ref(false);
const tab = ref("collect");
const statusFilter = ref("");
const autoRefresh = ref(true);
let timer: ReturnType<typeof setInterval> | null = null;

const collectForm = reactive({ pods: [] as string[], items: ["cpu", "mem", "disk"] as string[] });
const shellForm = reactive({
  pods: [] as string[],
  mode: "cmdline" as "cmdline" | "argv",
  cmdline: "",
  argvText: "",
  useShell: false,
  timeout: 30
});

const outDialog = reactive({ visible: false, title: "", text: "", loading: false });

const filteredRows = computed(() =>
  statusFilter.value ? rows.value.filter(r => r.status === statusFilter.value) : rows.value
);

async function loadHosts() {
  try {
    hosts.value = (await listHosts("summary")).items;
  } catch (e: any) {
    ElMessage.error("加载主机列表失败：" + (e?.message ?? e));
  }
}

async function loadItems() {
  try {
    items.value = (await listCollectItems()).items;
  } catch {
    items.value = ["cpu", "mem", "disk", "disk_io", "net", "proc", "user", "sys"];
  }
}

async function loadCommands() {
  try {
    rows.value = (await listCommands({ limit: 100 })).items;
  } catch (e: any) {
    ElMessage.error("加载命令历史失败：" + (e?.message ?? e));
  }
}

function restartTimer() {
  if (timer) clearInterval(timer);
  timer = autoRefresh.value ? setInterval(loadCommands, 5000) : null;
}

function errText(e: any): string {
  return e?.response?.data?.detail ?? e?.message ?? String(e);
}

async function submitCollect() {
  if (!collectForm.pods.length) return ElMessage.warning("请选择主机");
  if (!collectForm.items.length) return ElMessage.warning("请勾选采集项");
  submitting.value = true;
  try {
    const res = await createCommand({
      pods: collectForm.pods,
      kind: "collect",
      items: collectForm.items
    });
    ElMessage.success(`已下发 ${res.items.length} 条采集命令`);
    await loadCommands();
  } catch (e: any) {
    ElMessage.error("下发失败：" + errText(e));
  } finally {
    submitting.value = false;
  }
}

async function submitShell() {
  if (!shellForm.pods.length) return ElMessage.warning("请选择主机");
  submitting.value = true;
  try {
    const body: Record<string, any> = {
      pods: shellForm.pods,
      kind: "shell",
      timeout: shellForm.timeout,
      use_shell: shellForm.useShell
    };
    if (shellForm.mode === "argv") {
      const argv = shellForm.argvText
        .split("\n")
        .map(s => s.trim())
        .filter(Boolean);
      if (!argv.length) return ElMessage.warning("argv 模式至少填一行参数");
      body.argv = argv;
    } else {
      if (!shellForm.cmdline.trim()) return ElMessage.warning("请填写命令行");
      body.cmdline = shellForm.cmdline.trim();
    }
    const res = await createCommand(body as any);
    ElMessage.success(`已下发 ${res.items.length} 条命令`);
    await loadCommands();
  } catch (e: any) {
    ElMessage.error("下发失败：" + errText(e));
  } finally {
    submitting.value = false;
  }
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
  const preset = String(route.query.pods || "");
  if (preset) {
    const pods = preset.split(",").filter(Boolean);
    collectForm.pods = pods;
    shellForm.pods = pods;
  }
  await Promise.all([loadHosts(), loadItems(), loadCommands()]);
  restartTimer();
});

onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
  timer = null;
});
</script>

<template>
  <div>
    <el-card shadow="never" class="kk-card">
      <template #header>
        <span><b>命令中心</b></span>
      </template>

      <el-tabs v-model="tab">
        <el-tab-pane label="采集面板" name="collect">
          <el-form label-width="90px">
            <el-form-item label="目标主机">
              <el-select
                v-model="collectForm.pods"
                multiple
                filterable
                collapse-tags
                collapse-tags-tooltip
                placeholder="选择主机（可多选）"
                style="width: 100%"
              >
                <el-option
                  v-for="h in hosts"
                  :key="h.pod"
                  :label="h.online ? h.pod : h.pod + '（离线）'"
                  :value="h.pod"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="采集项">
              <el-checkbox-group v-model="collectForm.items">
                <el-checkbox v-for="it in items" :key="it" :label="it" :value="it">
                  {{ it }}
                </el-checkbox>
              </el-checkbox-group>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="submitting" @click="submitCollect">
                下发采集（{{ collectForm.pods.length }} 台）
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="命令面板" name="shell">
          <el-form label-width="90px">
            <el-form-item label="目标主机">
              <el-select
                v-model="shellForm.pods"
                multiple
                filterable
                collapse-tags
                collapse-tags-tooltip
                placeholder="选择主机（可多选）"
                style="width: 100%"
              >
                <el-option
                  v-for="h in hosts"
                  :key="h.pod"
                  :label="h.online ? h.pod : h.pod + '（离线）'"
                  :value="h.pod"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="输入方式">
              <el-radio-group v-model="shellForm.mode">
                <el-radio value="cmdline">命令行</el-radio>
                <el-radio value="argv">argv 数组（推荐，不会被空格拆坏）</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="shellForm.mode === 'cmdline'" label="命令行">
              <el-input
                v-model="shellForm.cmdline"
                placeholder="例如：ls -lh /var/log"
                @keyup.enter="submitShell"
              />
            </el-form-item>
            <el-form-item v-else label="argv">
              <el-input
                v-model="shellForm.argvText"
                type="textarea"
                :rows="3"
                placeholder="每行一个参数，例如：&#10;/bin/ls&#10;-lh&#10;/var/log"
              />
            </el-form-item>
            <el-form-item label="超时">
              <el-input-number v-model="shellForm.timeout" :min="1" :max="600" /> 秒
            </el-form-item>
            <el-form-item label="经 shell">
              <el-switch v-model="shellForm.useShell" />
              <span class="kk-sub kk-ml">
                开启后原样交给 sh -c（可用管道/重定向），与 Agent 的 KK_ALLOW_SHELL 同时打开才生效
              </span>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="submitting" @click="submitShell">
                下发命令（{{ shellForm.pods.length }} 台）
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </el-card>

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
            <el-button :loading="loading" @click="loadCommands">刷新</el-button>
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
    </el-card>

    <el-dialog v-model="outDialog.visible" :title="outDialog.title" width="720px">
      <el-scrollbar max-height="440px">
        <pre v-loading="outDialog.loading" class="kk-out">{{ outDialog.text }}</pre>
      </el-scrollbar>
    </el-dialog>
  </div>
</template>

<style scoped>
.kk-card {
  margin: 16px;
}
.kk-toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}
.kk-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.kk-ml {
  margin-left: 10px;
}
.kk-sub {
  font-size: 12px;
  color: #909399;
}
.kk-out {
  margin: 0;
  padding: 10px;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  background: #f5f7fa;
  border-radius: 4px;
}
</style>
