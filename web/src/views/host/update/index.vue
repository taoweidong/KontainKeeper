<script setup lang="ts">
/** 版本与更新（D2.4）：当前待分发版本 + 落后主机一键升级 + 升级台账。

把「上传新版本」之后要做的所有事拢到一屏：
1. 顶部一栏展示服务端当前版本、落后台数、最近上传时间——一眼能看出「要不要升」；
2. 中段一栏「落后主机清单」，勾选后右下角「批量升级」按钮一键下发；
3. 下半部分是升级台账，每次升级一条一行，从台账可一眼看见「pending/queued/done/failed」；
   离线主机在 `queued` 行，**对运维如实说「已排队，重连即升」**，不让它像失败一样需要重试。

为什么不在这里做上传：上传是写二进制，单独走 `/agent` 上传面；本页只读「待分发」是哪一个版本，
避免把上传失败/校验失败/上传一半混进运维主流程。
*/
import { computed, h, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

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

defineOptions({ name: "HostUpdate" });

const route = useRoute();
const loading = ref(false);
const current = ref<AgentCurrent | null>(null);
const outdated = ref<HostSummary[]>([]);
const updates = ref<UpdateRow[]>([]);
const interval = ref(10);
const selection = ref<HostSummary[]>([]);
const upgrading = ref(false);
/** 静默轮询失败：状态进页头读数，不再刷 toast（W5） */
const pollFailed = ref(false);

// 作用域版 setPoll：卸载时只清本页注册的 key
const { setPoll } = usePolls();
/** 轮询与「升级后 reload」是两个入口，在途防重入只护定时器那路：
 *  迟到的回包既会把旧勾选模型盖回来，也会提前熄灭 loading（QR-W4）。 */
const beginLoad = useSeq();

/** 表格实例：勾选模型反向同步用（只依赖这两个方法，按结构收窄） */
const tableRef = ref<{
  clearSelection: () => void;
  toggleRowSelection: (row: HostSummary, selected?: boolean) => void;
}>();

/** ?pods= 预填只应用一次；之后仅当 query 本身变化（watch）才重设，轮询刷新保留用户勾选 */
let podsPrefilled = false;

const hasLatest = computed(() => !!current.value?.version);

/** 读到过真值没有：与「服务端说没有落后主机」是两件事（QR-W16） */
const read = ref(false);
/** 升级台账是独立一路（load 内自带 catch），它不能连带别的路一起谎称「暂无记录」 */
const ledgerRead = ref(false);

/** 「落后 N / M 台」三态分开说：没读到 / 读到了但还没有待分发版本 / 有版本。
 *  前两态渲染 0 是假话——服务端注释写明「没上传过版本时 0 表示『无从定义』，不是『全都不落后』」。
 *  hosts_total 不受此限：它始终是真读数，0 就该显示 0。 */
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
    if (!silent) ElMessage.error("加载升级信息失败：" + errText(e));
  } finally {
    if (isLatest()) loading.value = false;
  }
}

/** 用户勾选 / 表头全选 → 模型：两个事件的第一参数都是勾选后的行全集 */
function onSelect(rows: HostSummary[]) {
  selection.value = rows;
}

/** 数据刷新后把模型对齐到新行对象：丢掉已不再落后的主机，展示与提交都不引用过期数据 */
function reconcileSelection() {
  const pods = new Set(selection.value.map(h => h.pod));
  selection.value = outdated.value.filter(h => pods.has(h.pod));
}

/** 换数据后表格内部勾选态会被重置，按模型回填（row-key 用主机唯一标识 pod） */
function applySelection() {
  const table = tableRef.value;
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

/** 仅当 ?pods= 本身变化时才重设勾选；轮询 load() 不触发这里，用户手改的勾选不被冲掉 */
watch(
  () => route.query.pods,
  () => prefillFromQuery()
);

/** 被跳过的台数超过这个值就只列前若干条，其余按原因聚合报数。
 *  500 台批量升级若被跳过几十台，只给一个数字运维无法判断该不该重试
 *  （`no_binary` 该去上传、`already_latest` 重试也没用）。 */
const SKIP_DETAIL_MAX = 10;

/** 升级后逐条列出跳过的主机与原因（QR-W9 重做）。
 *  只用 VNode 文本节点走 Vue 转义：主机名由 Agent 自报，不可信。 */
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
    ElMessage.warning("请先勾选要升级的主机");
    return;
  }
  const version = current.value?.version || "";
  if (!version) {
    ElMessage.warning("服务端没有待分发版本，先去上传");
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
    ElMessage.success(lines.join("，"));
    selection.value = [];
    // 先刷数据再弹明细：让台账与弹窗内容对得上，而不是反过来
    await load();
    await showSkipped(r.skipped);
  } catch (e: any) {
    ElMessage.error("升级失败：" + errText(e));
  } finally {
    upgrading.value = false;
  }
}

/** 选「所有落后主机」按钮的语义：是「待升级的潜在目标」，不是「会全网推」
 *  —— 还是要在勾选栏勾一下才真正下发，避免误点把 500 台全推下去 */
function selectAllOutdated() {
  selection.value = [...outdated.value];
  applySelection(); // 模型与表格勾选态同步
  ElMessage.info(
    `已选 ${selection.value.length} 台落后主机；右下角「批量升级」确认后再下发`
  );
}

function clearSelection() {
  selection.value = [];
  tableRef.value?.clearSelection(); // 模型与表格勾选态同步
}

/** 换间隔只改一处真相：与总览页同构（watch(interval) 重建定时器），
 *  不再把 setPoll 写在模板的 @change 里做类型强转（FE-27）。 */
function restartTimer() {
  setPoll("host-update", () => load(true), interval.value * 1000);
}

watch(interval, restartTimer);

onMounted(async () => {
  await load();
  restartTimer();
});
</script>

<template>
  <div v-loading="loading">
    <el-card shadow="never" class="kk-card">
      <template #header>
        <div class="kk-toolbar">
          <div class="kk-stat">
            <span
              >当前版本 <b>{{ fleetInfo.version }}</b></span
            >
            <span>
              落后主机
              <b :class="fleetInfo.cls">
                {{ fleetInfo.outdated }}
              </b>
              / {{ fleetInfo.total }}</span
            >
            <span v-if="current?.uploaded_at" class="kk-sub">
              {{
                ageText(
                  Math.max(
                    0,
                    Math.floor(Date.now() / 1000) - current.uploaded_at
                  )
                )
              }}上传
            </span>
            <span class="kk-sync" :class="{ 'kk-sync--stale': pollFailed }">
              {{
                pollFailed
                  ? "自动刷新失败，读数可能已过期"
                  : `每 ${interval} 秒自动刷新`
              }}
            </span>
          </div>
          <div class="kk-actions">
            <el-select v-model="interval" style="width: 120px">
              <el-option label="5 秒" :value="5" />
              <el-option label="10 秒" :value="10" />
              <el-option label="30 秒" :value="30" />
            </el-select>
            <el-button @click="load()">刷新</el-button>
          </div>
        </div>
      </template>

      <el-table
        ref="tableRef"
        :data="outdated"
        row-key="pod"
        class="kk-fill-table"
        @select="onSelect"
        @select-all="onSelect"
      >
        <el-table-column type="selection" width="46" />
        <el-table-column label="主机" min-width="180">
          <template #default="{ row }">{{ row.pod }}</template>
        </el-table-column>
        <el-table-column label="当前版本" width="120">
          <template #default="{ row }">
            <el-tag size="small" type="warning">{{
              row.agent_ver || "-"
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="目标版本" width="120">
          <template #default>→ {{ current?.version || "-" }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.online ? 'success' : 'info'" size="small">
              {{ row.online ? "在线" : "离线" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近心跳" width="130">
          <template #default="{ row }">{{ ageText(row.age_sec) }}</template>
        </el-table-column>
        <template #empty>
          <el-empty
            :description="
              !read
                ? '没读到（主机与版本接口不可达）'
                : hasLatest
                  ? '所有主机都是最新版本'
                  : '还没有待分发版本，先去上传 Agent 二进制'
            "
          />
        </template>
      </el-table>

      <div class="kk-batch kk-sticky-bar">
        <span class="kk-sub">已选 {{ selection.length }} 台</span>
        <el-button :disabled="!outdated.length" @click="selectAllOutdated">
          选中所有落后主机
        </el-button>
        <el-button :disabled="!selection.length" @click="clearSelection">
          清空选择
        </el-button>
        <el-button
          type="primary"
          :disabled="!selection.length || !hasLatest"
          :loading="upgrading"
          @click="onUpgrade"
        >
          批量升级
        </el-button>
        <el-tooltip
          v-if="selection.some(h => !h.online)"
          placement="top"
          content="选中的离线主机将标记为 queued，桥接在它们上线后自动补投——无需重试"
        >
          <el-tag type="info" size="small">含离线主机</el-tag>
        </el-tooltip>
      </div>
    </el-card>

    <el-card shadow="never" class="kk-card">
      <template #header>
        <span>升级台账（最近 50 条）</span>
      </template>
      <el-table :data="updates" size="small" class="kk-fill-table">
        <el-table-column
          prop="id"
          label="台账 ID"
          width="170"
          show-overflow-tooltip
        />
        <el-table-column prop="pod" label="主机" min-width="120" />
        <el-table-column label="从 → 到" min-width="200">
          <template #default="{ row }">
            {{ row.from_version || "-" }} → {{ row.to_version }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="原因" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.reason || "-" }}
          </template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ tsText(row.created_at) }}</template>
        </el-table-column>
        <template #empty>
          {{ ledgerRead ? "暂无升级记录" : "没读到（升级台账接口不可达）" }}
        </template>
      </el-table>
    </el-card>
  </div>
</template>
