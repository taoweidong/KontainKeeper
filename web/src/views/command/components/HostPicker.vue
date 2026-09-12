<script setup lang="ts">
/** 主机多选（A5.4）：抽屉式替代 el-select multiple。

500 台时下拉列表极长、没有「仅在线」筛选、没有全选/反选——选一次主机要点开
一个几百项的列表逐条找。抽屉里给搜索 + 仅在线开关 + 表格多选 + 全选/反选/清空。

对外契约仍是 `v-model:pods`（字符串数组），与总览页 `?pods=a,b,c` 跳转完全兼容，
页面改造不破坏既有链路。
*/
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import { listHosts, type HostSummary } from "@/api/containers";

defineOptions({ name: "HostPicker" });

const props = defineProps<{ pods: string[] }>();
const emit = defineEmits<{ "update:pods": [string[]] }>();

const hosts = ref<HostSummary[]>([]);
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
  return hosts.value
    .filter(h => {
      if (onlyOnline.value && !h.online) return false;
      if (!kw) return true;
      return (
        h.pod.toLowerCase().includes(kw) || (h.image || "").toLowerCase().includes(kw)
      );
    })
    .slice()
    .sort((a, b) => Number(b.online) - Number(a.online) || a.pod.localeCompare(b.pod));
});

const selected = computed(() =>
  hosts.value.filter(h => props.pods.includes(h.pod))
);
const shown = computed(() => selected.value.slice(0, 5));
const restCount = computed(() => Math.max(0, selected.value.length - shown.value.length));

async function open() {
  drawer.value = true;
  draft.value = [...props.pods];
  if (!hosts.value.length) {
    loading.value = true;
    try {
      hosts.value = (await listHosts("summary")).items;
    } catch (e: any) {
      ElMessage.error("加载主机列表失败：" + (e?.message ?? e));
    } finally {
      loading.value = false;
    }
  }
  syncTableSelection();
}

function syncTableSelection() {
  // 等表格渲染完再回显勾选状态
  requestAnimationFrame(() => {
    const t = table.value;
    if (!t) return;
    t.clearSelection();
    for (const h of hosts.value) {
      if (draft.value.includes(h.pod)) t.toggleRowSelection(h, true);
    }
  });
}

function onSelect(rows: HostSummary[]) {
  draft.value = rows.map(r => r.pod);
}

function selectAll() {
  draft.value = candidates.value.map(h => h.pod);
  syncTableSelection();
}

function invert() {
  const set = new Set(draft.value);
  draft.value = candidates.value.filter(h => !set.has(h.pod)).map(h => h.pod);
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
      <el-tag
        v-for="h in shown"
        :key="h.pod"
        size="small"
        closable
        :type="h.online ? 'success' : 'info'"
        @close="emit('update:pods', props.pods.filter(p => p !== h.pod))"
      >
        {{ h.pod }}
      </el-tag>
      <el-tag v-if="restCount" size="small" type="info">+{{ restCount }}</el-tag>
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
          <el-checkbox v-model="onlyOnline" @change="syncTableSelection">仅在线</el-checkbox>
          <el-button size="small" @click="selectAll">全选</el-button>
          <el-button size="small" @click="invert">反选</el-button>
          <el-button size="small" @click="clearAll">清空</el-button>
        </div>
      </div>

      <el-table
        ref="table"
        v-loading="loading"
        :data="candidates"
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
            <el-tag :type="row.online ? 'success' : 'info'" size="small">
              {{ row.online ? "在线" : "离线" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Agent" width="100">
          <template #default="{ row }">{{ row.agent_ver || "-" }}</template>
        </el-table-column>
        <el-table-column label="CPU" width="90">
          <template #default="{ row }">{{ row.cpu ?? "-" }}%</template>
        </el-table-column>
        <template #empty>
          <el-empty description="没有匹配的主机，试试清除搜索或关闭「仅在线」" />
        </template>
      </el-table>

      <template #footer>
        <div class="kk-toolbar">
          <span class="kk-sub">
            已选 {{ draft.length }} 台（离线主机命令会由 Broker 排队，重连后自动补投）
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
