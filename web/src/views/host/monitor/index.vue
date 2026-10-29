<script setup lang="ts">
import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch
} from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

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
  nOr,
  nowSec,
  numText,
  tsText
} from "@/utils/kk";
import { usePolls, useSeq } from "@/utils/kkPoll";

defineOptions({ name: "HostMonitor" });

const router = useRouter();

const loading = ref(false);
const rows = ref<HostSummary[]>([]);
/** 服务端按同一套条件算出的台数：分页控件读它，不读当前页的行数 */
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

/** 500 台规模（QR-W7）：一次全量渲染会把 500 行 × 富单元格（仪表条 / 心跳刻度 / 链接）
 *  铺进 DOM，还要每 10s 随轮询整体重渲染——分页把渲染规模压到一页。
 *  这里刻意做**前端**分页而不是后端分页：summary 视图是标量行，500 台的 JSON 不是瓶颈，
 *  瓶颈在节点数。筛选与排序下推服务端后（QR-S36）拉回的行集已经与页面所见一致，
 *  改后端分页只是换一种取法，代价换不到额外收益。
 *  跨页勾选由 row-key + reserve-selection 保住，「分几页勾完再批量下发」的老路径不断。 */
const pageSize = ref(100);
const offset = ref(0);

const paged = computed(() =>
  rows.value.slice(offset.value, offset.value + pageSize.value)
);

/** el-pagination 用 1 起始页码，切片要的是 offset —— 换算只在这里做一次 */
const pageNo = computed({
  get: () => Math.floor(offset.value / pageSize.value) + 1,
  set: (v: number) => {
    offset.value = (v - 1) * pageSize.value;
  }
});

/** 换筛选条件回到第一页，否则会停在筛选后不存在的页上显示空表。
 *  条件即请求：勾选与下拉一改就查服务端（关键字另有防抖）。 */
watch([onlyOnline, onlyAlert, hostType, protoVer], () => {
  offset.value = 0;
  load();
});

/** 关键字防抖：边打字边打服务端等于自己 DoS 自己，静默 300ms 后发一枪。
 *  这是**交互**计时器不是轮询，所以不走 usePolls；但卸载一定要清——
 *  否则切走页面还会补发一次请求。 */
let kwTimer: ReturnType<typeof setTimeout> | null = null;
watch(keyword, () => {
  offset.value = 0;
  if (kwTimer !== null) clearTimeout(kwTimer);
  kwTimer = setTimeout(() => {
    kwTimer = null;
    load(true);
  }, 300);
});

onBeforeUnmount(() => {
  if (kwTimer !== null) clearTimeout(kwTimer);
});

watch(pageSize, () => {
  offset.value = 0;
});

/** 轮询让行数变少时夹住 offset，避免出现一张空的尾页 */
watch(
  () => rows.value.length,
  n => {
    const max = Math.max(0, Math.ceil(n / pageSize.value - 1) * pageSize.value);
    if (offset.value > max) offset.value = max;
  }
);

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
  offset.value = 0;
  load();
}

/** host_type 是 Agent 自报的虚拟化形态；空值 = v3 老 Agent 没上报这一项 */
const HOST_TYPE_LABEL: Record<string, string> = {
  metal: "物理机",
  vm: "虚拟机",
  container: "容器"
};

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
  } catch (e: any) {
    if (!isLatest()) return;
    // 自动轮询失败只在表头的同步读数上如实说明：后端宕机时每 10s 弹一次
    // ElMessage 会把真正的错误淹成噪音（W5）。
    pollFailed.value = silent;
    if (!silent) ElMessage.error("加载主机列表失败：" + errText(e));
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
    ElMessage.error("导出失败：" + errText(e));
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
    ElMessage.error("加载采集项失败：" + errText(e));
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
    ElMessage.warning("请先在表格里勾选主机");
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
    ElMessage.warning("至少勾选一个采集项");
    return;
  }
  if (!collectTargets.value.length) {
    ElMessage.warning("没有目标主机");
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
    ElMessage.success(`已下发 ${res.items.length} 条采集命令`);
    dialog.visible = false;
    // 与「批量命令 / 批量升级」同一套 ?pods= 契约：落到采集页要带回刚下发的
    // 主机，否则运维得在采集页把同一批机器重新勾一遍（FE-3）。
    router.push({ name: "CommandCollect", query: { pods: pods.join(",") } });
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

/** 计量条填充：钳到 0–100，坏值不画出越界的条。
 *  NaN/Infinity 必须显式挡掉（QR29-W5）：Math.min/max 会透传 NaN，
 *  界面会渲染出字面 NaN，插件写坏指标即可触发。 */
function fillPct(v: number | null | undefined): number {
  if (v === null || v === undefined || !Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(100, Math.round(v)));
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
            <!-- 首次加载失败时一个读数都没有：条与四个格子一律不渲染 0（QR-W16）。
                 0 台主机是肯定语句，运维会当成「 fleet 空了」去做下一步动作。 -->
            <span
              v-if="read"
              class="kk-band__strip"
              role="img"
              :aria-label="`在线 ${online} 台，离线 ${Math.max(0, total - online)} 台`"
            >
              <i class="kk-band__seg" :style="{ flex: String(online) }" />
              <i
                class="kk-band__seg kk-band__seg--offline"
                :style="{ flex: String(Math.max(0, total - online)) }"
              />
            </span>
            <span class="kk-band__readout"
              ><b>{{ nOr(read, total) }}</b
              >台主机</span
            >
            <el-tooltip
              v-if="isFiltered"
              content="筛选条件生效中：以上读数只统计筛后的子集，「导出清单」与之一致"
              placement="bottom"
            >
              <el-tag size="small" type="info">已筛选</el-tag>
            </el-tooltip>
            <span class="kk-band__readout"
              ><b>{{ nOr(read, online) }}</b
              >在线</span
            >
            <span class="kk-band__readout kk-band__readout--alert">
              <b>{{ nOr(read, alerts) }}</b
              >磁盘告警
            </span>
            <span class="kk-band__readout kk-band__readout--stale">
              <b>{{ nOr(read, outdated) }}</b
              >待升级
            </span>
            <el-tooltip
              content="自动刷新最近一次成功的时间；失败时表格保持上一批数据"
              placement="bottom"
            >
              <span class="kk-sync" :class="{ 'kk-sync--stale': pollFailed }">
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
        <!-- 筛选条：条件全部下推服务端（QR-S36），列表与「导出清单」读同一份参数。
             类型 / 协议 来自 v4 元信息，只有服务端能筛——
             关键字同时匹配主机名、镜像、系统与分组。 -->
        <div class="kk-actions kk-mt">
          <el-input
            v-model="keyword"
            placeholder="主机名 / 镜像 / 系统 / 分组"
            clearable
            style="width: 210px"
          />
          <el-select
            v-model="hostType"
            placeholder="类型"
            clearable
            style="width: 110px"
          >
            <el-option label="物理机" value="metal" />
            <el-option label="虚拟机" value="vm" />
            <el-option label="容器" value="container" />
          </el-select>
          <el-tooltip
            content="按上报协议筛：筛 v3 是关兼容窗口（KK_DROP_PROTO_V3）前的例行检查"
            placement="bottom"
          >
            <el-select
              v-model="protoVer"
              placeholder="协议"
              clearable
              style="width: 104px"
            >
              <el-option label="v4" value="4" />
              <el-option label="v3" value="3" />
            </el-select>
          </el-tooltip>
          <el-checkbox v-model="onlyOnline">仅在线</el-checkbox>
          <el-checkbox v-model="onlyAlert">仅告警</el-checkbox>
          <el-button
            v-if="isFiltered"
            link
            type="primary"
            @click="clearFilters"
          >
            清空筛选
          </el-button>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="paged"
        :row-class-name="rowClass"
        :default-sort="{ prop: 'last_seen', order: 'descending' }"
        row-key="pod"
        size="small"
        class="kk-fill-table"
        @selection-change="onSelectionChange"
        @row-click="onRowClick"
        @sort-change="onSortChange"
      >
        <el-table-column type="selection" width="46" reserve-selection />
        <el-table-column
          label="主机"
          prop="pod"
          sortable="custom"
          min-width="180"
        >
          <template #default="{ row }">
            <el-link
              type="primary"
              class="kk-num"
              @click.stop="gotoDetail(row.pod)"
            >
              {{ row.pod }}
            </el-link>
            <div class="kk-sub">{{ row.image || "-" }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="92">
          <template #default="{ row }">
            <span class="kk-state" :class="stateClass(row as HostSummary)">{{
              stateText(row as HostSummary)
            }}</span>
          </template>
        </el-table-column>
        <!-- v4 元信息两列：类型带分组、系统带地址。
             容器列没上——内置 Agent 不发 docker 摘要，恒为 0 的一列只会教运维
             「这一列没数据就是没问题」（QR-S38）。 -->
        <el-table-column label="类型" width="108">
          <template #default="{ row }">
            <span>{{ HOST_TYPE_LABEL[row.host_type] || "-" }}</span>
            <div v-if="row.group_name" class="kk-sub">
              {{ row.group_name }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="系统 / 地址" min-width="132">
          <template #default="{ row }">
            <div>{{ row.os_name || "-" }}</div>
            <div class="kk-sub kk-num">{{ row.ip || "-" }}</div>
          </template>
        </el-table-column>
        <el-table-column label="CPU" prop="cpu" sortable="custom" width="132">
          <template #default="{ row }">
            <span class="kk-meter">
              <span class="kk-meter__bar">
                <i :style="{ width: fillPct(row.cpu) + '%' }" />
              </span>
              <span class="kk-meter__num">{{ numText(row.cpu) }}</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column
          label="内存"
          prop="mem_mb"
          sortable="custom"
          width="110"
          align="right"
        >
          <template #default="{ row }">
            <span class="kk-num">{{ mbText(row.mem_mb) }}</span>
          </template>
        </el-table-column>
        <el-table-column
          label="磁盘"
          prop="disk_pct"
          sortable="custom"
          width="132"
        >
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
        <el-table-column label="Agent" width="150">
          <template #default="{ row }">
            <!-- 落后于待分发版本时用「待升级」同色——一眼看出哪些要升（D2.4） -->
            <span class="kk-num" :class="{ 'kk-warn': row.agent_outdated }">
              {{ row.agent_ver || "-" }}
            </span>
            <!-- 只标 v3：兼容窗口是要关掉的，剩下的旧协议主机就是拦路的那几台 -->
            <el-tooltip
              v-if="row.proto_ver === 3"
              content="这台仍以 v3 上报：设 KK_DROP_PROTO_V3 关掉兼容窗口前，先把它升上来"
              placement="bottom"
            >
              <el-tag class="kk-ml" size="small" type="warning">v3</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column prop="last_seen" sortable="custom" width="158">
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
              :class="tickState(row as HostSummary).cls"
              aria-hidden="true"
            >
              <i
                v-for="n in 3"
                :key="n"
                :class="{ on: n <= tickState(row as HostSummary).n }"
              />
            </span>
            <span class="kk-num kk-ml">{{ ageText(row.age_sec) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="gotoDetail(row.pod)"
              >详情</el-button
            >
            <el-button
              link
              type="primary"
              @click.stop="collectOne(row as HostSummary)"
              >采集</el-button
            >
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="emptyText" />
        </template>
      </el-table>

      <div v-if="total > pageSize" class="kk-pager">
        <el-pagination
          v-model:current-page="pageNo"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[100, 200, 500]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          small
        />
      </div>

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
        <!-- 原来这里还有一个「导出选中清单」：它调的是全量导出，跟勾选无关。
             按勾选导出需要服务端的 pods 契约（500 台放不进 query），登记在 QR-S38；
             名义不符的入口先摘掉，导出走页头的「导出清单」。 -->
      </div>
    </el-card>

    <el-dialog
      v-model="dialog.visible"
      :title="collectTargets.length > 1 ? '批量采集指标' : '采集指标'"
      width="420px"
    >
      <el-checkbox-group v-model="dialog.items">
        <el-checkbox v-for="it in collectItems" :key="it" :value="it">
          {{ it }}
        </el-checkbox>
      </el-checkbox-group>
      <p class="kk-sub">
        将对
        {{ collectTargets.length }} 台主机下发采集命令，结果在「命令中心」查看。
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
