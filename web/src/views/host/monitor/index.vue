<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { listHosts, type HostSummary } from "@/api/containers";
import { createCommand, listCollectItems } from "@/api/commands";
import { ageText, mbText, numText, tsText } from "@/utils/kk";

defineOptions({ name: "HostMonitor" });

const router = useRouter();

const loading = ref(false);
const rows = ref<HostSummary[]>([]);
const online = ref(0);
const alerts = ref(0);
const keyword = ref("");
const onlyOnline = ref(false);
const onlyAlert = ref(false);
/** 轮询间隔（秒），0 = 停。总览是唯一常驻轮询的页面，10s 足够且不给服务端放大压力 */
const interval = ref(10);
let timer: ReturnType<typeof setInterval> | null = null;

const collectItems = ref<string[]>([]);
const dialog = reactive({
  visible: false,
  items: [] as string[],
  submitting: false
});
const selection = ref<HostSummary[]>([]);

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  return rows.value.filter(r => {
    if (onlyOnline.value && !r.online) return false;
    if (onlyAlert.value && !r.disk_alert) return false;
    if (kw && !r.pod.toLowerCase().includes(kw) && !(r.image || "").toLowerCase().includes(kw))
      return false;
    return true;
  });
});

async function load() {
  loading.value = true;
  try {
    const data = await listHosts("summary");
    rows.value = data.items;
    online.value = data.online;
    alerts.value = data.alerts;
  } catch (e: any) {
    ElMessage.error("加载主机列表失败：" + (e?.message ?? e));
  } finally {
    loading.value = false;
  }
}

function restartTimer() {
  if (timer) clearInterval(timer);
  timer = null;
  if (interval.value > 0) {
    timer = setInterval(load, interval.value * 1000);
  }
}

function onSelectionChange(val: HostSummary[]) {
  selection.value = val;
}

async function openCollect() {
  if (!selection.value.length) {
    ElMessage.warning("请先在表格里勾选主机");
    return;
  }
  if (!collectItems.value.length) {
    collectItems.value = (await listCollectItems()).items;
  }
  dialog.items = ["cpu", "mem", "disk"];
  dialog.visible = true;
}

async function submitCollect() {
  if (!dialog.items.length) {
    ElMessage.warning("至少勾选一个采集项");
    return;
  }
  dialog.submitting = true;
  try {
    const res = await createCommand({
      pods: selection.value.map(r => r.pod),
      kind: "collect",
      items: dialog.items
    });
    ElMessage.success(`已下发 ${res.items.length} 条采集命令`);
    dialog.visible = false;
    router.push({ name: "CommandCenter" });
  } catch (e: any) {
    ElMessage.error("下发失败：" + (e?.response?.data?.detail ?? e?.message ?? e));
  } finally {
    dialog.submitting = false;
  }
}

function openCommandCenter() {
  if (!selection.value.length) {
    ElMessage.warning("请先在表格里勾选主机");
    return;
  }
  router.push({
    name: "CommandCenter",
    query: { pods: selection.value.map(r => r.pod).join(",") }
  });
}

function gotoDetail(pod: string) {
  router.push({ name: "HostDetail", params: { pod } });
}

onMounted(async () => {
  await load();
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
        <div class="kk-toolbar">
          <div class="kk-stat">
            <span>主机 <b>{{ rows.length }}</b></span>
            <span>在线 <b class="kk-ok">{{ online }}</b></span>
            <span>磁盘告警 <b class="kk-bad">{{ alerts }}</b></span>
          </div>
          <div class="kk-actions">
            <el-input
              v-model="keyword"
              placeholder="按主机名 / 镜像过滤"
              clearable
              style="width: 200px"
            />
            <el-checkbox v-model="onlyOnline">仅在线</el-checkbox>
            <el-checkbox v-model="onlyAlert">仅告警</el-checkbox>
            <el-select v-model="interval" style="width: 120px" @change="restartTimer">
              <el-option label="5 秒" :value="5" />
              <el-option label="10 秒" :value="10" />
              <el-option label="30 秒" :value="30" />
              <el-option label="不自动刷新" :value="0" />
            </el-select>
            <el-button :loading="loading" @click="load">刷新</el-button>
          </div>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="filtered"
        height="calc(100vh - 320px)"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="46" />
        <el-table-column label="主机" min-width="200">
          <template #default="{ row }">
            <el-link type="primary" @click="gotoDetail(row.pod)">{{ row.pod }}</el-link>
            <div class="kk-sub">{{ row.image || "-" }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.online ? 'success' : 'info'" size="small">
              {{ row.online ? "在线" : "离线" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="CPU" width="110">
          <template #default="{ row }">
            <div class="kk-metric">
              <el-progress
                :percentage="Math.min(100, Math.round(row.cpu ?? 0))"
                :stroke-width="10"
                :show-text="false"
              />
              <span>{{ numText(row.cpu) }}%</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="内存" width="120">
          <template #default="{ row }">{{ mbText(row.mem_mb) }}</template>
        </el-table-column>
        <el-table-column label="磁盘" width="150">
          <template #default="{ row }">
            <el-tag :type="row.disk_alert ? 'danger' : 'info'" size="small">
              {{ numText(row.disk_pct, 0) }}%
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Agent" width="100">
          <template #default="{ row }">{{ row.agent_ver || "-" }}</template>
        </el-table-column>
        <el-table-column label="最近心跳" width="130">
          <template #default="{ row }">{{ ageText(row.age_sec) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="gotoDetail(row.pod)">详情</el-button>
            <el-button
              link
              type="primary"
              @click="
                selection = [row];
                openCollect();
              "
            >
              采集
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有主机上报，确认 Agent 与 Broker 已连通" />
        </template>
      </el-table>

      <div class="kk-batch">
        <span class="kk-sub">已选 {{ selection.length }} 台</span>
        <el-button type="primary" :disabled="!selection.length" @click="openCollect">
          批量采集
        </el-button>
        <el-button :disabled="!selection.length" @click="openCommandCenter">
          批量执行命令
        </el-button>
        <span class="kk-sub">最后加载：{{ tsText(Math.floor(Date.now() / 1000)) }}</span>
      </div>
    </el-card>

    <el-dialog v-model="dialog.visible" title="批量采集指标" width="420px">
      <el-checkbox-group v-model="dialog.items">
        <el-checkbox v-for="it in collectItems" :key="it" :label="it" :value="it">
          {{ it }}
        </el-checkbox>
      </el-checkbox-group>
      <p class="kk-sub">将对已选 {{ selection.length }} 台主机下发采集命令，结果在「命令中心」查看。</p>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="dialog.submitting" @click="submitCollect">
          下发
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.kk-card {
  margin: 16px;
}
.kk-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}
.kk-stat span {
  margin-right: 18px;
  color: #606266;
}
.kk-stat b {
  font-size: 16px;
  color: #303133;
}
.kk-ok {
  color: #67c23a !important;
}
.kk-bad {
  color: #f56c6c !important;
}
.kk-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}
.kk-sub {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}
.kk-metric {
  display: flex;
  gap: 8px;
  align-items: center;
}
.kk-metric .el-progress {
  flex: 1;
}
.kk-batch {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 12px;
}
</style>
