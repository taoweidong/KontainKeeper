<script setup lang="ts">
/** 主机多选（A5.4）：抽屉式替代 el-select multiple。

500 台时下拉列表极长、没有「仅在线」筛选、没有全选/反选——选一次主机要点开
一个几百项的列表逐条找。抽屉里给搜索 + 仅在线开关 + 表格多选 + 全选/反选/清空。

对外契约仍是 `v-model:pods`（字符串数组），与总览页 `?pods=a,b,c` 跳转完全兼容，
页面改造不破坏既有链路。可选的 `:hosts` 让父页把已经拉到的清单传进来，
抽屉不再打第二遍全量接口（QR-W7）。
*/
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import { listHosts, type HostSummary } from "@/api/containers";
import {
  errText,
  hostStateClass,
  hostStateTagType,
  hostStateText,
  invertSelection,
  pctText,
  unionSelection
} from "@/utils/kk";
import { useSeq } from "@/utils/kkPoll";

defineOptions({ name: "HostPicker" });

const props = defineProps<{
  pods: string[];
  hosts?: HostSummary[];
  /** 父页「主机清单读到过没有」：显式传入时优先采用——空数组与「接口挂了」
   *  不再同形（QR29-W4：机队真的 0 台时不能谎报「主机清单不可达」）。 */
  hostsRead?: boolean;
}>();
const emit = defineEmits<{ "update:pods": [string[]] }>();

/** 父页没给清单时自己懒加载一份 */
const fetched = ref<HostSummary[]>([]);
/** 可选项来源：父页传了就用父页的——shell / collect 在挂载时已经拉过一次全量清单，
 *  这里再打一遍就是 500 台 × 两份 JSON（QR-W7）；父页没传（详情类页面）才懒加载。 */
const options = computed(() =>
  props.hosts?.length ? props.hosts : fetched.value
);
/** 清单读到过没有：父页显式告知（hostsRead）优先；没告知时回落旧启发式——
 *  父页给了清单即算读到，懒加载那路要等自己成功一次。
 *  没读到时空表不能说「没有匹配的主机」——那是把接口故障劝用户改成搜索条件
 *  （QR-W16）；反过来机队真的 0 台也不能谎报「不可达」（QR29-W4）。 */
const read = ref(false);
const listRead = computed(() =>
  props.hostsRead !== undefined
    ? props.hostsRead
    : !!props.hosts?.length || read.value
);
const loading = ref(false);
const drawer = ref(false);
const keyword = ref("");
const onlyOnline = ref(false);
const table = ref<any>();
/** 抽屉内临时选择：点「确定」才写回父级，取消不污染 */
const draft = ref<string[]>([]);

/** 抽屉内可选项：在线优先排序，避免误选离线机后困惑「为什么没结果」 */
const candidates = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  return options.value
    .filter(h => {
      if (onlyOnline.value && !h.online) return false;
      if (!kw) return true;
      return (
        h.pod.toLowerCase().includes(kw) ||
        (h.image || "").toLowerCase().includes(kw)
      );
    })
    .slice()
    .sort(
      (a, b) =>
        Number(b.online) - Number(a.online) || a.pod.localeCompare(b.pod)
    );
});

/** 抽屉表格前端分页（QR1120-W5）：500 台一次铺进 DOM 是勾选卡顿的另一半。
 *  row-key + reserve-selection 跨页保勾；全选/反选仍对**全量** candidates 操作，
 *  分页只压渲染规模，不改选择语义。 */
const page = ref(1);
const pageSize = ref(50);
const pagedCandidates = computed(() => {
  const start = (page.value - 1) * pageSize.value;
  return candidates.value.slice(start, start + pageSize.value);
});
// 换筛选条件回第一页；候选变少后夹住页码，避免停在空的尾页
watch([keyword, onlyOnline], () => {
  page.value = 1;
});
watch(
  () => candidates.value.length,
  () => {
    const maxPage = Math.max(
      1,
      Math.ceil(candidates.value.length / pageSize.value)
    );
    if (page.value > maxPage) page.value = maxPage;
  }
);

const selected = computed(() =>
  options.value.filter(h => props.pods.includes(h.pod))
);
const shown = computed(() => selected.value.slice(0, 5));
const restCount = computed(() =>
  Math.max(0, selected.value.length - shown.value.length)
);

/** 主机清单懒拉取（只在首次打开时打一次）。
 *  `useSeq` 守卫的是连点/开了又关再开的并发两路请求：迟到那路必须整条丢弃，
 *  否则它会把 loading 提前熄灭，还弹出一条指向早已关掉的抽屉的失败 toast（QR-W4）。 */
const beginLoad = useSeq();

async function loadHosts() {
  const isLatest = beginLoad();
  loading.value = true;
  try {
    const items = (await listHosts("summary")).items;
    if (!isLatest()) return;
    fetched.value = items;
    read.value = true;
  } catch (e: any) {
    if (!isLatest()) return;
    ElMessage.error("加载主机列表失败：" + errText(e));
  } finally {
    if (isLatest()) loading.value = false;
  }
}

async function open() {
  drawer.value = true;
  draft.value = [...props.pods];
  if (!options.value.length) await loadHosts();
  syncTableSelection();
}

function syncTableSelection() {
  // 等表格渲染完再回显勾选状态。draft 转 Set（QR1120-W5）：原先对全量 options
  // 逐行 draft.includes 是 O(n×m)，500×500 每次开抽屉/切筛选都跑一遍。
  // 回填仍对全量 options：不在当前页的行由 reserve-selection 按 row-key 记住。
  requestAnimationFrame(() => {
    const t = table.value;
    if (!t) return;
    const picked = new Set(draft.value);
    t.clearSelection();
    for (const h of options.value) {
      if (picked.has(h.pod)) t.toggleRowSelection(h, true);
    }
  });
}

function onSelect(rows: HostSummary[]) {
  draft.value = rows.map(r => r.pod);
}

function selectAll() {
  // 并集（QR1120-W19）：筛选外的已选（如离线机）不被候选集覆盖
  draft.value = unionSelection(
    draft.value,
    candidates.value.map(h => h.pod)
  );
  syncTableSelection();
}

function invert() {
  // 对称差（QR1120-W19）：只翻候选内的勾选，筛选外已选原样保留
  draft.value = invertSelection(
    draft.value,
    candidates.value.map(h => h.pod)
  );
  syncTableSelection();
}

function clearAll() {
  draft.value = [];
  syncTableSelection();
}

function confirm() {
  emit("update:pods", [...draft.value]);
  drawer.value = false;
}

/** 父级被外部改写（如 URL 预选）时同步 draft，避免抽屉里看到旧状态 */
watch(
  () => props.pods,
  v => {
    draft.value = [...v];
  }
);
</script>

<template>
  <div>
    <div class="kk-picker">
      <el-button @click="open">
        选择主机（已选 {{ selected.length }} 台）
      </el-button>
      <!-- 在线三态（QR1120-W10）：自更新窗口显「更新中」琥珀标签，不是普通离线 -->
      <el-tag
        v-for="h in shown"
        :key="h.pod"
        size="small"
        closable
        :type="hostStateTagType(h)"
        @close="
          emit(
            'update:pods',
            props.pods.filter(p => p !== h.pod)
          )
        "
      >
        {{ h.pod }} · {{ hostStateText(h) }}
      </el-tag>
      <el-tag v-if="restCount" size="small" type="info"
        >+{{ restCount }}</el-tag
      >
      <span v-if="!selected.length" class="kk-sub">未选择主机</span>
    </div>

    <el-drawer v-model="drawer" title="选择目标主机" size="52%">
      <div class="kk-toolbar kk-mb">
        <el-input
          v-model="keyword"
          placeholder="按主机名 / 镜像搜索"
          clearable
          style="width: 240px"
        />
        <div class="kk-actions">
          <el-checkbox v-model="onlyOnline" @change="syncTableSelection"
            >仅在线</el-checkbox
          >
          <el-button size="small" @click="selectAll">全选</el-button>
          <el-button size="small" @click="invert">反选</el-button>
          <el-button size="small" @click="clearAll">清空</el-button>
        </div>
      </div>

      <el-table
        ref="table"
        v-loading="loading"
        :data="pagedCandidates"
        size="small"
        :height="'calc(100vh - var(--kk-drawer-h))'"
        row-key="pod"
        @selection-change="onSelect"
      >
        <el-table-column type="selection" width="46" reserve-selection />
        <el-table-column label="主机" min-width="180">
          <template #default="{ row }">
            {{ row.pod }}
            <div class="kk-sub">{{ row.image || "-" }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <!-- 圆点词汇与总览/升级页同源（QR1120-W10），updating 方点不丢 -->
            <span :class="hostStateClass(row as HostSummary)">
              {{ hostStateText(row as HostSummary) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="Agent" width="100">
          <template #default="{ row }">{{ row.agent_ver || "-" }}</template>
        </el-table-column>
        <el-table-column label="CPU" width="90">
          <template #default="{ row }">{{ pctText(row.cpu) }}</template>
        </el-table-column>
        <template #empty>
          <el-empty
            :description="
              listRead
                ? '没有匹配的主机，试试清除搜索或关闭「仅在线」'
                : '没读到（主机清单不可达）'
            "
          />
        </template>
      </el-table>

      <!-- 前端分页（QR1120-W5）：total 跟筛后候选走，翻页不打后端；
           跨页勾选由 row-key + reserve-selection 保住 -->
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        class="kk-mt"
        small
        :total="candidates.length"
        :page-sizes="[50, 100, 200]"
        layout="total, sizes, prev, pager, next"
      />

      <template #footer>
        <div class="kk-toolbar">
          <span class="kk-sub">
            已选 {{ draft.length }} 台（离线主机命令会由 Broker
            排队，重连后自动补投）
          </span>
          <div class="kk-actions">
            <el-button @click="drawer = false">取消</el-button>
            <el-button type="primary" @click="confirm">确定</el-button>
          </div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>
