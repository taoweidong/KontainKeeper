<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { listHosts, type HostSummary } from "@/api/containers";
import { createCommand, listCollectItems } from "@/api/commands";
import { exportHosts } from "@/api/exporting";
import {
  ageText,
  downloadBlob,
  errText,
  fileStamp,
  mbText,
  numText,
  tsText
} from "@/utils/kk";
import { usePolls } from "@/utils/kkPoll";

defineOptions({ name: "HostMonitor" });

const router = useRouter();

const loading = ref(false);
const rows = ref<HostSummary[]>([]);
const online = ref(0);
const alerts = ref(0);
const outdated = ref(0);
const keyword = ref("");
const onlyOnline = ref(false);
const onlyAlert = ref(false);
/** 最近一次加载成功的时间（秒级时间戳），0 = 尚未加载 */
const lastLoadedAt = ref(0);
/** 静默轮询失败：不刷 toast，改由表头的同步读数如实说明（W5） */
const pollFailed = ref(false);
/** 轮询间隔（秒），0 = 停。总览是唯一常驻轮询的页面，10s 足够且不给服务端放大压力 */
const interval = ref(10);

const collectItems = ref<string[]>([]);
const dialog = reactive({
  visible: false,
  items: [] as string[],
  submitting: false
});
const selection = ref<HostSummary[]>([]);

// 作用域版 setPoll：卸载时只清本页注册的 key（此前漏调 usePolls，切页后 host-monitor 轮询不会停）
const { setPoll } = usePolls();

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  return rows.value.filter(r => {
    if (onlyOnline.value && !r.online) return false;
    if (onlyAlert.value && !r.disk_alert) return false;
    if (
      kw &&
      !r.pod.toLowerCase().includes(kw) &&
      !(r.image || "").toLowerCase().includes(kw)
    )
      return false;
    return true;
  });
});

/** silent=true 供轮询复用：表格数据原位更新，不闪整页 loading，失败也不刷 toast */
async function load(silent = false) {
  if (!silent) loading.value = true;
  try {
    const data = await listHosts("summary");
    rows.value = data.items;
    online.value = data.online;
    alerts.value = data.alerts;
    outdated.value = data.outdated;
    lastLoadedAt.value = Math.floor(Date.now() / 1000);
    pollFailed.value = false;
  } catch (e: any) {
    // 自动轮询失败只在表头的同步读数上如实说明：后端宕机时每 10s 弹一次
    // ElMessage 会把真正的错误淹成噪音（W5）。
    pollFailed.value = silent;
    if (!silent) ElMessage.error("加载主机列表失败：" + errText(e));
  } finally {
    loading.value = false;
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

/** 导出全量主机清单（资产盘点场景），与「仅在线/仅告警」的前端过滤无关。 */
async function onExport() {
  exporting.value = true;
  try {
    downloadBlob(await exportHosts(), `主机清单_${fileStamp()}.csv`);
  } catch (e: any) {
    ElMessage.error("导出失败：" + errText(e));
  } finally {
    exporting.value = false;
  }
}

async function openCollect() {
  if (!selection.value.length) {
    ElMessage.warning("请先在表格里勾选主机");
    return;
  }
  if (!collectItems.value.length) {
    try {
      collectItems.value = (await listCollectItems()).items;
    } catch (e: any) {
      ElMessage.error("加载采集项失败：" + errText(e));
      return; // 采集项拿不到就不开弹窗，避免勾选区空白
    }
  }
  dialog.items = ["cpu", "mem", "disk"];
  dialog.visible = true;
}

/** 行级「采集」：单台主机等价于「勾选这一台 → 批量采集」的快捷路径 */
function collectOne(row: HostSummary) {
  selection.value = [row];
  openCollect();
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
    router.push({ name: "CommandCollect" });
  } catch (e: any) {
    ElMessage.error("下发失败：" + errText(e));
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

/** 离线要把服务端记的原因说出来：updating 是设计内的自更新窗口，不是故障，
 *  运维看到它不该去重启 Agent（B6 的语义此前在 UI 上完全丢失）。
 *  取值集合只有 online / updating / LWT 空，其余一律按「离线」呈现。 */
const OFFLINE_REASON_LABEL: Record<string, string> = { updating: "更新中" };

function stateClass(row: HostSummary): string {
  if (row.online) return "";
  return row.status_reason === "updating"
    ? "kk-state--updating"
    : "kk-state--idle";
}

function stateText(row: HostSummary): string {
  if (row.online) return "在线";
  return OFFLINE_REASON_LABEL[row.status_reason || ""] || "离线";
}

/** 计量条填充：钳到 0–100，坏值不画出越界的条 */
function fillPct(v: number | null | undefined): number {
  return Math.max(0, Math.min(100, Math.round(v ?? 0)));
}

/** 心跳新鲜度三格，按绝对秒数判级（默认 60s 上报间隔下 ≈ 0.5 / 2 / 10 个周期）。
 *  三格是判级不是历史波形——列头图例写明阈值，避免误读成时间序列。 */
function tickState(row: HostSummary): { n: number; cls: string } {
  if (!row.online) return { n: 0, cls: "" };
  const age = row.age_sec ?? 0;
  if (age <= 30) return { n: 3, cls: "" };
  if (age <= 120) return { n: 2, cls: "kk-ticks--mid" };
  if (age <= 600) return { n: 1, cls: "kk-ticks--low" };
  return { n: 0, cls: "" };
}

onMounted(async () => {
  await load();
  restartTimer();
});
</script>

<template>
  <div>
    <el-card shadow="never" class="kk-card">
      <template #header>
        <div class="kk-band">
          <div class="kk-band__fleet">
            <span
              class="kk-band__strip"
              role="img"
              :aria-label="`在线 ${online} 台，离线 ${Math.max(0, rows.length - online)} 台`"
            >
              <i class="kk-band__seg" :style="{ flex: String(online) }" />
              <i
                class="kk-band__seg kk-band__seg--offline"
                :style="{ flex: String(Math.max(0, rows.length - online)) }"
              />
            </span>
            <span class="kk-band__readout"
              ><b>{{ rows.length }}</b
              >台主机</span
            >
            <span class="kk-band__readout"
              ><b>{{ online }}</b
              >在线</span
            >
            <span class="kk-band__readout kk-band__readout--alert">
              <b>{{ alerts }}</b
              >磁盘告警
            </span>
            <span class="kk-band__readout kk-band__readout--stale">
              <b>{{ outdated }}</b
              >待升级
            </span>
            <el-tooltip
              content="自动刷新最近一次成功的时间；失败时表格保持上一批数据"
              placement="bottom"
            >
              <span
                class="kk-band__sync"
                :class="{ 'kk-band__sync--stale': pollFailed }"
              >
                {{
                  pollFailed
                    ? "自动刷新失败，读数可能已过期"
                    : lastLoadedAt
                      ? `已同步 ${tsText(lastLoadedAt)}`
                      : "尚未同步"
                }}
              </span>
            </el-tooltip>
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
            <el-select
              v-model="interval"
              style="width: 120px"
              @change="restartTimer"
            >
              <el-option label="5 秒" :value="5" />
              <el-option label="10 秒" :value="10" />
              <el-option label="30 秒" :value="30" />
              <el-option label="不自动刷新" :value="0" />
            </el-select>
            <el-button :loading="loading" @click="load()">刷新</el-button>
            <el-button type="primary" :loading="exporting" @click="onExport">
              导出清单
            </el-button>
          </div>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="filtered"
        :row-class-name="rowClass"
        size="small"
        class="kk-fill-table"
        @selection-change="onSelectionChange"
        @row-click="onRowClick"
      >
        <el-table-column type="selection" width="46" />
        <el-table-column label="主机" min-width="200">
          <template #default="{ row }">
            <el-link type="primary" class="kk-num" @click="gotoDetail(row.pod)">
              {{ row.pod }}
            </el-link>
            <div class="kk-sub">{{ row.image || "-" }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="92">
          <template #default="{ row }">
            <span class="kk-state" :class="stateClass(row)">{{
              stateText(row)
            }}</span>
          </template>
        </el-table-column>
        <el-table-column label="CPU" width="132">
          <template #default="{ row }">
            <span class="kk-meter">
              <span class="kk-meter__bar">
                <i :style="{ width: fillPct(row.cpu) + '%' }" />
              </span>
              <span class="kk-meter__num">{{ numText(row.cpu) }}</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="内存" width="110" align="right">
          <template #default="{ row }">
            <span class="kk-num">{{ mbText(row.mem_mb) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="磁盘" width="132">
          <template #default="{ row }">
            <!-- 告警态由条本身变红承担，不再套一层 tag：一列只有一种强调方式 -->
            <span
              class="kk-meter"
              :class="{ 'kk-meter--alert': row.disk_alert }"
            >
              <span class="kk-meter__bar">
                <i :style="{ width: fillPct(row.disk_pct) + '%' }" />
              </span>
              <span class="kk-meter__num">{{ numText(row.disk_pct, 0) }}</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="Agent" width="120">
          <template #default="{ row }">
            <!-- 落后于待分发版本时用「待升级」同色——一眼看出哪些要升（D2.4） -->
            <span class="kk-num" :class="{ 'kk-warn': row.agent_outdated }">
              {{ row.agent_ver || "-" }}
            </span>
          </template>
        </el-table-column>
        <el-table-column width="158">
          <template #header>
            <el-tooltip
              content="心跳新鲜度分级：满格 ≤30s，两格 ≤2 分钟，一格 ≤10 分钟，空格更久"
              placement="bottom"
            >
              <span>最近心跳</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span
              class="kk-ticks"
              :class="tickState(row).cls"
              aria-hidden="true"
            >
              <i
                v-for="n in 3"
                :key="n"
                :class="{ on: n <= tickState(row).n }"
              />
            </span>
            <span class="kk-num kk-ml">{{ ageText(row.age_sec) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="gotoDetail(row.pod)"
              >详情</el-button
            >
            <el-button link type="primary" @click="collectOne(row)"
              >采集</el-button
            >
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有主机上报，确认 Agent 与 Broker 已连通" />
        </template>
      </el-table>

      <div class="kk-batch kk-sticky-bar">
        <span class="kk-sub">已选 {{ selection.length }} 台</span>
        <el-button
          type="primary"
          :disabled="!selection.length"
          @click="openCollect"
        >
          批量采集
        </el-button>
        <el-button :disabled="!selection.length" @click="openCommandCenter">
          批量执行命令
        </el-button>
        <!-- 升级按钮只对选中的落后主机开放：
             - 全是最新版本时灰着，避免「点了却跳过全部」造成的体验割裂；
             - 有落后时直接跳到「版本与更新」页并在 query 里带上已选主机，
               进入页面后 selection 已被预填好。 -->
        <el-button
          type="warning"
          :disabled="
            !selection.length || !selection.some(h => h.agent_outdated)
          "
          @click="gotoUpgrade"
        >
          批量升级
        </el-button>
        <el-button
          :disabled="!selection.length"
          :loading="exporting"
          @click="onExport"
        >
          导出选中清单
        </el-button>
      </div>
    </el-card>

    <el-dialog v-model="dialog.visible" title="批量采集指标" width="420px">
      <el-checkbox-group v-model="dialog.items">
        <el-checkbox
          v-for="it in collectItems"
          :key="it"
          :label="it"
          :value="it"
        >
          {{ it }}
        </el-checkbox>
      </el-checkbox-group>
      <p class="kk-sub">
        将对已选
        {{ selection.length }} 台主机下发采集命令，结果在「命令中心」查看。
      </p>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="dialog.submitting"
          @click="submitCollect"
        >
          下发
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<!-- 通用类（kk-band/kk-state/kk-meter/kk-ticks/kk-batch 等）统一在 style/kk.scss -->
